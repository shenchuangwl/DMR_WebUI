"""任务装配对进程内替身交易所（T0.22）：真实账本、真实的全部组件、真实的控制接口应用（同一个事件循环里用 httpx 的 ASGI 传输访问），
只有「重启恢复」是假的（真实恢复的联调在下一波：tests/integration 里的恢复用例与 T0.27）。不开套接字，替身与内核共用一个 ManualClock。

覆盖：启动后控制接口回答健康与 /state、作业喂给 /state 的内容、持仓登记后合约数上限的快照及时刷新（同一根 K 线上的第二个开仓不能越过上限）、
进程被杀后用同一账本与同一替身重新装配。
"""
from __future__ import annotations

from decimal import Decimal

from dmr_kernel.alerts.heartbeat import Heartbeat
from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.types import Gate, IntentAction, IntentSource, IntentState, Ownership, PositionSide
from tests.support.kernel import FakeRecovery, abandon, api_client, make_kernel, settle, started_kernel

D = Decimal
LONG = PositionSide.LONG
POSITIONS = "/api/private/positions"
_seq = 0


def open_spec(symbol: str) -> IntentSpec:
    global _seq
    _seq += 1
    return IntentSpec(action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol=symbol, side=LONG,
                      idem_key=f"asm:{_seq}", ttl_s=60)


async def get_state(client) -> dict:
    response = await client.get("/api/v1/state")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- 控制接口


async def test_a_started_kernel_answers_health_and_state_with_the_local_token(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, standin):
        token = kernel.app.state.ctx.local_token
        async with api_client(kernel) as anonymous:
            assert (await anonymous.get("/api/v1/health")).status_code == 200  # 健康检查不要令牌
            assert (await anonymous.get("/api/v1/state")).status_code == 401  # 其它都要
        async with api_client(kernel, token) as client:
            state = await get_state(client)
        assert state["env"] == "standin" and state["standin"] is True
        assert state["model"]["gate"] == "stopped"  # 重启后闸一律已停止
        assert state["model"]["suppressions"] == []  # 假恢复什么抑制也没加
        assert state["me"]["principal"] == "cli"
        assert state["heartbeat_ms"] == standin.clock.now_ms()  # 第一次心跳在恢复之前就写了


async def test_the_jobs_feed_what_the_state_reports(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        token = kernel.app.state.ctx.local_token
        async with api_client(kernel, token) as client:
            before = await get_state(client)
            assert before["position_mode"]["known"] is False and before["account"]["known"] is False
            assert before["reconcile"]["positions"]["known"] is False
            for name in ("reconcile_positions", "reconcile_orders", "guard_periodic"):
                await kernel.jobs[name]()
            await settle(kernel)
            after = await get_state(client)
        assert after["position_mode"] == {"known": True, "dual_side": True}
        assert after["restrictions"] == {"known": True, "restricted": False}
        assert after["account"]["known"] is True and Decimal(after["account"]["equity"]) == D("1000")
        assert after["reconcile"]["positions"]["status"] == "ok" and after["reconcile"]["orders"]["status"] == "ok"
        assert after["limits"]["stale"] is False


# ---------------------------------------------------------------- 上限快照的及时刷新


async def open_then_open_again(kernel, standin):
    """开仓 BTC（成交回报由补拉带进来），马上再开仓 ETH，返回第二个意图的终态记录。"""
    await kernel.modelstate.set_gate(Gate.RUNNING, reason="test")
    await kernel.jobs["reconcile_positions"]()
    await kernel.jobs["reconcile_orders"]()
    standin.clock.advance(1000)  # 之后任何一轮持仓对账的时间都会比第一轮晚
    first = await kernel.coordinator.submit(open_spec("BTCUSDT"))
    assert (await kernel.coordinator.wait_terminal(first.intent_id, 5)).state is IntentState.COMPLETED
    await kernel.jobs["backfill_trades"]()  # 没有数据流时，成交回报由补拉带进来
    await settle(kernel)
    await kernel.idle()
    assert kernel.registry.get_active("BTCUSDT", LONG) is not None
    second = await kernel.coordinator.submit(open_spec("ETHUSDT"))
    return await kernel.coordinator.wait_terminal(second.intent_id, 5)


async def test_the_second_open_on_the_same_bar_cannot_slip_past_the_symbol_limit(tmp_path):
    """合约数上限 1：第一个开仓成交、登记之后，上限快照要马上跟上，紧接着的第二个开仓才会被拒绝（而不是等下一次定时对账）。"""
    overrides = {"max_symbols": 1, "trade_notional_usdt": D("100")}
    async with started_kernel(tmp_path, run_jobs=True, config_overrides=overrides) as (kernel, standin):
        kernel.scheduler.pause()  # 定时作业不来插手：这个测试自己驱动每一步
        record = await open_then_open_again(kernel, standin)
        # 登记之后马上对了一次持仓账（时间晚于第一轮），快照里已经有这个仓
        assert kernel.reconciler.status()["positions_at_ms"] == standin.clock.now_ms()
        assert ("BTCUSDT", "LONG") in kernel.limits.occupancy(degraded=False, now_ms=standin.clock.now_ms()).keys
        assert record.state is IntentState.REJECTED
        assert "已达持仓上限" in record.state_reason


async def test_without_the_reaction_the_snapshot_is_stale_and_the_second_open_gets_through(tmp_path):
    """对照组（说明这条反应为什么存在）：不跑定时作业就没有「登记后即时对账」，上限快照还是第一轮的，第二个开仓越过了上限。"""
    overrides = {"max_symbols": 1, "trade_notional_usdt": D("100")}
    async with started_kernel(tmp_path, run_jobs=False, config_overrides=overrides) as (kernel, standin):
        record = await open_then_open_again(kernel, standin)
        assert ("BTCUSDT", "LONG") not in kernel.limits.occupancy(degraded=False, now_ms=standin.clock.now_ms()).keys
        assert record.state is IntentState.COMPLETED


# ---------------------------------------------------------------- 进程被杀后重新装配


async def test_a_killed_kernel_is_replaced_by_a_new_assembly_on_the_same_ledger_and_exchange(tmp_path):
    first_recovery = FakeRecovery()
    kernel1, standin = make_kernel(tmp_path, recovery_factory=first_recovery)
    await kernel1.start(serve_http=False, run_jobs=True)
    await kernel1.modelstate.set_gate(Gate.RUNNING, reason="test")
    intent, _ = await kernel1.intents.create(open_spec("BTCUSDT"))
    await kernel1.registry.register_open(
        symbol="BTCUSDT", side=LONG, qty=D("0.01"), avg_price=D("50000"), open_time_ms=standin.clock.now_ms(),
        ownership=Ownership.STRATEGY, model="m1", intent_id=intent.id)
    version_before = kernel1.modelstate.current().version
    old_token = kernel1.app.state.ctx.local_token
    old_instance = kernel1.orders._instance
    standin.clock.advance(60_000)
    await abandon(kernel1)  # 被杀：不走关停顺序

    second_recovery = FakeRecovery()
    kernel2, _ = make_kernel(tmp_path, standin=standin, recovery_factory=second_recovery)
    try:
        await kernel2.start(serve_http=False, run_jobs=True)
        assert second_recovery.runs == 1 and first_recovery.runs == 1
        state = kernel2.modelstate.current()
        assert state.gate is Gate.STOPPED  # 重启后闸一律已停止，要人启动
        assert state.version > version_before
        assert kernel2.registry.get_active("BTCUSDT", LONG) is not None  # 登记簿从账本读回
        assert kernel2.orders._instance == old_instance  # 同一个账本 = 同一个实例号，编号前缀不变
        assert kernel2.app.state.ctx.local_token != old_token
        assert Heartbeat.read(kernel2.heartbeat.path)["at_ms"] == standin.clock.now_ms()  # 新内核写了新心跳
        assert len(standin.transports) == 2  # 每个内核各用一条传输（新进程 = 新连接）
    finally:
        await kernel2.stop()


# ---------------------------------------------------------------- 名单（M4）：状态条的名单条目来自装配好的名单同步


async def test_the_state_reports_the_current_view_of_the_attached_list_sync(tmp_path):
    from dmr_kernel.listsync import ListMember, ListView

    def view(valid: bool, reason: str, n: int, until: int | None) -> ListView:
        members = tuple(ListMember(symbol=s, direction=LONG, score=D(1), first_selected_ms=None, raw_code=s)
                        for s in ("ETHUSDT", "SOLUSDT")[:n])
        return ListView(valid=valid, reason=reason, snapshot_id=3, fetched_ms=1, valid_until_ms=until,
                        business_date=None, status=None, members=members, rejected=())

    class FakeListSync:
        def __init__(self) -> None:
            self.view = view(False, "HTTP 503", 0, None)

        async def poll(self) -> ListView:
            return self.view

        def current(self) -> ListView:
            return self.view

        def member(self, symbol: str):
            return next((m for m in self.view.members if m.symbol == symbol), None)

    async with started_kernel(tmp_path) as (kernel, standin):
        token = kernel.app.state.ctx.local_token
        async with api_client(kernel, token) as client:
            assert (await get_state(client))["list"]["known"] is False  # 测试装配没有名单同步
            fake = FakeListSync()
            kernel.set_listsync(fake)
            assert (await get_state(client))["list"] == {"known": True, "valid": False, "reason": "HTTP 503",
                                                        "member_count": 0, "valid_until_ms": None}
            until = standin.clock.now_ms() + 3_600_000
            fake.view = view(True, "", 2, until)
            assert (await get_state(client))["list"] == {"known": True, "valid": True, "reason": "",
                                                        "member_count": 2, "valid_until_ms": until}
