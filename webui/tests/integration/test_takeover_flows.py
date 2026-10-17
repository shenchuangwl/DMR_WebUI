"""模型 2 接管全流程（M3K）：完整装配的内核（真实恢复、账户数据流在线）+ 进程内替身，全部经控制接口走一遍。

设好接管清单 → 启动模型 2 → 在替身上「手动开仓」清单内的仓 → 10 秒内被识别并接管、止损随即挂上 → 部分平仓 50% → 平掉此仓；
清单以外的仓进入待确认。其余覆盖：启动时已有的仓、数量变化、取消托管的两种方式、交给系统管理止损、恢复托管、中断点 7。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "takeover"))

from takeover_kit import LONG, SHORT, m2_rig  # noqa: E402

from dmr_kernel.core.crashpoints import CrashRegistry  # noqa: E402
from dmr_kernel.core.types import (  # noqa: E402
    ExitReason,
    ExitRuleset,
    Ownership,
    PendingReason,
    PositionStatus,
    ProtectionState,
)
from tests.support.kernel import abandon, make_kernel, settle  # noqa: E402

D = Decimal
SYM = "BTCUSDT"


def _system_stops(algos):
    return [a for a in algos if (a.client_algo_id or "").startswith("DMR") or not (a.client_algo_id or "").startswith("web")]


# ====================================================================== 验收主线


async def test_acceptance_flow_takeover_partial_close_close_and_out_of_scope(tmp_path):
    async with m2_rig(tmp_path) as rig:
        t_open = rig.clock.now_ms()
        await rig.manual_open(SYM, "0.002")  # 名义 100 USDT，在 5–500 之内
        rec = rig.pos()
        assert rec is not None and rec.status is PositionStatus.MANAGED  # 接管后保护挂上，转托管中
        assert rec.ownership is Ownership.TAKEOVER and rec.exit_ruleset is ExitRuleset.TAKEOVER
        assert rec.protection_state is ProtectionState.SYSTEM and rec.stop_client_id
        assert rec.qty == D("0.002") and rec.avg_price == D("50000") and rec.open_time_estimated is False
        assert rec.open_time_ms == t_open and rec.anchor_ms == t_open
        assert rig.clock.now_ms() - t_open < 10_000  # 10 秒内
        assert len(await rig.algos()) == 1

        # ---- 部分平仓 50%
        preview = await rig.ok("/ops/partial_close/preview", {"position_id": rec.id, "ratio": 0.5})
        confirm = {"op_id": rig.op_id("pc"), "position_id": rec.id, "position_version": preview["position_version"],
                   "preview_qty": preview["live_qty"], "close_qty": preview["close_qty"]}
        done = await rig.ok("/ops/partial_close/confirm", confirm)
        assert done["status"] == "succeeded"
        await rig.kernel.jobs["backfill_trades"]()
        await rig.settle()
        rec = rig.pos()
        assert rec.qty == D("0.001") and rec.ownership is Ownership.TAKEOVER and rec.status is PositionStatus.MANAGED
        assert len(await rig.algos()) == 1  # 全平型止损覆盖剩余仓位

        # ---- 平掉此仓
        closed = await rig.ok("/ops/close_one", {"op_id": rig.op_id("c1"), "position_id": rec.id,
                                                  "position_version": rec.version})
        assert closed["status"] == "succeeded", closed
        await rig.kernel.jobs["backfill_trades"]()
        await rig.settle()
        assert rig.pos() is None
        assert await rig.algos() == []  # 归零后系统保护单被清理

        # ---- 清单以外的仓进入待确认，并有告警
        await rig.manual_open("ETHUSDT", "0.05")
        eth = rig.pos("ETHUSDT")
        assert eth.status is PositionStatus.PENDING and eth.pending_reason is PendingReason.OUT_OF_SCOPE
        await rig.kernel.notifier.flush()
        alerts = await rig.get("/alerts")
        assert any("ETHUSDT" in str(a) for a in alerts.json().get("items", alerts.json()))

        # ---- 忽略
        ignored = await rig.takeover_op("ignore", eth.id)
        assert ignored.status_code == 200, ignored.text
        eth = rig.pos("ETHUSDT")
        assert eth.status is PositionStatus.IGNORED and eth.ownership is Ownership.NONE


async def test_position_opened_before_start_and_added_to_afterwards_is_never_auto_taken(tmp_path):
    def prepare(standin):
        standin.engine.place_market_order(symbol=SYM, side="BUY", position_side="LONG", qty="0.002",
                                          client_order_id="web_pre", source="manual")

    async with m2_rig(tmp_path, prepare=prepare) as rig:
        rec = rig.pos()
        assert rec.status is PositionStatus.PENDING and rec.pending_reason is PendingReason.PREEXISTING
        await rig.manual_open(SYM, "0.001")  # 加仓：仍是待确认，不会因此被自动接管
        await rig.kernel.jobs["backfill_trades"]()
        await rig.settle()
        rec = rig.pos()
        assert rec.status is PositionStatus.PENDING and rec.pending_reason is PendingReason.PREEXISTING
        assert rec.qty == D("0.003")
        assert await rig.algos() == []


async def test_manual_takeover_of_a_pending_position_only_when_model_2_is_active(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        await rig.manual_open(SYM, "0.002")  # 模型 2 未运行
        rec = rig.pos()
        assert rec.status is PositionStatus.PENDING and rec.pending_reason is PendingReason.MODEL_NOT_ACTIVE
        refused = await rig.takeover_op("takeover", rec.id)  # 闸关着：模型 2 未在运行，不能手动接管吗？F13 只要求模型 2 生效
        # F13：手动接管只在模型 2 生效时可用（生效，不要求运行中）
        assert refused.status_code == 200, refused.text
        await rig.settle()
        rec = rig.pos()
        assert rec.ownership is Ownership.TAKEOVER and rec.protection_state is ProtectionState.SYSTEM
        assert len(await rig.algos()) == 1


async def test_takeover_is_refused_when_model_2_is_not_the_active_model(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        await rig.manual_open(SYM, "0.002")
        rec = rig.pos()
        assert rec.status is PositionStatus.PENDING
        from dmr_kernel.core.types import Model
        await rig.kernel.modelstate.set_active_model(
            Model.M1, expected_version=rig.kernel.modelstate.current().version, operator="t", op_id="to-m1")
        refused = await rig.takeover_op("takeover", rec.id)
        assert refused.status_code == 409 and refused.json()["code"] == "not_allowed"
        assert "模型 2" in refused.json()["message"]
        assert rig.pos().status is PositionStatus.PENDING


# ====================================================================== 取消托管、交给系统管理止损


async def _taken_over(rig, qty="0.002"):
    await rig.manual_open(SYM, qty)
    rec = rig.pos()
    assert rec.status is PositionStatus.MANAGED and rec.ownership is Ownership.TAKEOVER
    return rec


async def test_cancel_takeover_handover_keeps_the_stop_until_the_position_is_flat(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _taken_over(rig)
        stop_cid = rec.stop_client_id
        response = await rig.takeover_op("cancel_takeover", rec.id, mode="handover")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["result"]["status"] == "ignored" and body["result"]["ownership"] == "none"
        await rig.settle()
        rec = rig.pos()
        assert rec.status is PositionStatus.IGNORED and rec.handed_over_stop["client_algo_id"] == stop_cid
        # 对账不会撤掉已移交的止损
        for _ in range(3):
            rig.advance(120_000)
            await rig.kernel.jobs["reconcile_orders"]()
            await rig.kernel.jobs["reconcile_positions"]()
            await rig.settle()
        assert [a.client_algo_id for a in await rig.algos()] == [stop_cid]
        # 仓位归零后被系统撤销
        await rig.manual_reduce(SYM, "0.002")
        await rig.kernel.jobs["backfill_trades"]()
        await rig.settle()
        assert rig.pos() is None and await rig.algos() == []


async def test_cancel_takeover_cancel_stop_removes_the_system_stop_first(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _taken_over(rig)
        response = await rig.takeover_op("cancel_takeover", rec.id, mode="cancel_stop")
        assert response.status_code == 200, response.text
        await rig.settle()
        assert await rig.algos() == []
        rec = rig.pos()
        assert rec.status is PositionStatus.IGNORED and rec.handed_over_stop is None


async def test_cancel_takeover_default_mode_is_handover(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _taken_over(rig)
        response = await rig.takeover_op("cancel_takeover", rec.id)
        assert response.status_code == 200, response.text
        assert len(await rig.algos()) == 1
        assert rig.pos().handed_over_stop is not None


async def test_hand_stop_to_system_replaces_the_users_stop_only_after_the_button(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        await rig.manual_open(SYM, "0.002")  # 模型 2 未运行 → 待确认
        user = await rig.manual_stop(SYM, "48000")  # 用户自己的全平止损
        rig.advance(1_000)
        await rig.kernel.jobs["reconcile_orders"]()
        rec = rig.pos()
        taken = await rig.takeover_op("takeover", rec.id)
        assert taken.status_code == 200, taken.text
        await rig.settle()
        rec = rig.pos()
        assert rec.protection_state is ProtectionState.EXTERNAL and rec.status is PositionStatus.MANAGED
        assert [a.client_algo_id for a in await rig.algos()] == [user["client_algo_id"]]  # 系统没动用户的止损
        response = await rig.takeover_op("hand_stop_to_system", rec.id)
        assert response.status_code == 200, response.text
        await rig.settle()
        rec = rig.pos()
        assert rec.protection_state is ProtectionState.SYSTEM
        algos = await rig.algos()
        assert len(algos) == 1 and algos[0].client_algo_id == rec.stop_client_id != user["client_algo_id"]


async def test_hand_stop_to_system_is_refused_when_protection_is_not_external(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _taken_over(rig)
        response = await rig.takeover_op("hand_stop_to_system", rec.id)
        assert response.status_code == 409 and response.json()["code"] == "not_allowed"


async def test_a_users_profit_locking_stop_above_the_entry_counts_as_protection(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        await rig.manual_open(SYM, "0.002")
        # 价格上行后，用户把止损移到开仓价（50000）之上：触发价低于现价即算，与开仓价无关
        rig.standin.feed.set_price(SYM, "52000")
        await rig.manual_stop(SYM, "51000")
        rig.advance(1_000)
        await rig.kernel.jobs["reconcile_orders"]()
        await rig.takeover_op("takeover", rig.pos().id)
        await rig.settle()
        rec = rig.pos()
        assert rec.protection_state is ProtectionState.EXTERNAL


# ====================================================================== 数量变化


async def test_taken_over_position_stays_taken_over_when_added_to_and_cleans_up_when_flat(tmp_path):
    async with m2_rig(tmp_path) as rig:
        await _taken_over(rig)
        await rig.manual_open(SYM, "0.001")
        rec = rig.pos()
        assert rec.qty == D("0.003") and rec.ownership is Ownership.TAKEOVER and not rec.is_mixed
        await rig.manual_reduce(SYM, "0.003")
        await rig.settle()
        assert rig.pos() is None and await rig.algos() == []


async def test_out_of_range_notional_is_rejudged_after_a_quantity_change(tmp_path):
    async with m2_rig(tmp_path, config={"takeover_max_notional": "150"}) as rig:
        await rig.manual_open(SYM, "0.004")  # 名义 200 > 150
        rec = rig.pos()
        assert rec.status is PositionStatus.PENDING and rec.pending_reason is PendingReason.NOTIONAL_OUT_OF_RANGE
        await rig.manual_reduce(SYM, "0.002")  # 数量变化后重判：100，在范围内 → 自动接管
        rec = rig.pos()
        assert rec.ownership is Ownership.TAKEOVER and rec.status is PositionStatus.MANAGED


# ====================================================================== 来源不明、旧程序遗留


async def test_legacy_and_unknown_origin_orders_are_pending(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rig.engine_order(SYM, "BUY", "LONG", "0.002", "DMR_LONG_buy_1")  # 旧程序格式
        await rig.settle()
        rec = rig.pos()
        assert rec.status is PositionStatus.PENDING and rec.pending_reason is PendingReason.LEGACY
        rig.engine_order("ETHUSDT", "BUY", "LONG", "0.05", "x-cvBPrNm9abcdef")  # ccxt 自动生成的编号
        await rig.settle()
        eth = rig.pos("ETHUSDT")
        assert eth.status is PositionStatus.PENDING and eth.pending_reason is PendingReason.UNKNOWN_ORIGIN


# ====================================================================== 恢复托管


async def test_restore_of_an_origin_to_verify_position_claims_the_kept_stop(tmp_path):
    """内核换了一个全新的账本（登记簿丢了）：交易所上有系统自己开的仓和带系统前缀的止损 → 重启恢复标「来源待核对」→ 恢复托管。"""
    from dmr_kernel.core.intents import IntentSpec, key_op
    from dmr_kernel.core.types import IntentAction, IntentSource, IntentState
    from tests.support.kernel import make_standin_world

    standin = make_standin_world(equity="100000")
    kernel, standin = make_kernel(tmp_path / "a", standin=standin, recovery_factory=None)
    await kernel.start(serve_http=False, run_jobs=False)
    op = "seed-1"
    spec = IntentSpec(action=IntentAction.OPEN, model="m1", source=IntentSource.TEST_ENTRY, symbol=SYM, side=LONG,
                      idem_key=key_op(op, SYM, LONG), params={"notional": "100"}, reason="seed", operator="t", op_id=op)
    sub = await kernel.coordinator.submit(spec)
    assert (await kernel.coordinator.wait_terminal(sub.intent_id, 5)).state is IntentState.COMPLETED
    await settle(kernel)
    original = kernel.registry.get_active(SYM, LONG)
    assert original.ownership is Ownership.STRATEGY and original.stop_client_id
    stop_cid = original.stop_client_id
    await abandon(kernel)

    # ---- 全新的账本：登记簿里什么都没有；设好模型 2 与接管清单（恢复托管任何模型下都可用，这里用模型 2 方便走接管兜底之外的路径）
    kernel2, _ = make_kernel(tmp_path / "b", standin=standin, recovery_factory=None,
                             config_overrides={"takeover_symbols": ["BTCUSDT"]})
    await kernel2.start(serve_http=False, run_jobs=False)
    try:
        await settle(kernel2)
        pending = kernel2.registry.get_active(SYM, LONG)
        assert pending.status is PositionStatus.PENDING and pending.pending_reason is PendingReason.ORIGIN_TO_VERIFY
        assert pending.stop_client_id == stop_cid
        from takeover_kit import Rig
        from tests.support.kernel import api_client
        async with api_client(kernel2, kernel2.app.state.ctx.local_token) as client:
            rig = Rig(kernel2, standin, client)
            response = await rig.takeover_op("restore", pending.id)
            assert response.status_code == 200, response.text
            await rig.settle()
            restored = rig.pos()
            assert restored.status is PositionStatus.MANAGED and restored.ownership is Ownership.STRATEGY
            assert restored.exit_ruleset is ExitRuleset.STRATEGY
            assert restored.protection_state is ProtectionState.SYSTEM and restored.stop_client_id == stop_cid
            assert [a.client_algo_id for a in await rig.algos()] == [stop_cid]
    finally:
        await kernel2.stop()


# ====================================================================== 中断点 7


async def test_crashpoint_7_takeover_half_registered_is_completed_after_restart(tmp_path):
    crash = CrashRegistry(live=False)
    crash.arm("takeover_half_registered")
    from tests.support.kernel import make_standin_world, started_kernel  # noqa: F401
    standin = __import__("tests.support.kernel", fromlist=["make_standin_world"]).make_standin_world(equity="100000")
    kernel, standin = make_kernel(
        tmp_path, standin=standin, crash=crash, recovery_factory=None,
        config_overrides={"takeover_symbols": ["BTCUSDT"], "takeover_min_notional": "5", "takeover_max_notional": "500"})
    await kernel.start(serve_http=False, run_jobs=False)
    from dmr_kernel.core.types import Model, Gate
    await kernel.modelstate.set_active_model(Model.M2, expected_version=kernel.modelstate.current().version,
                                             operator="t", op_id="m2")
    await kernel.jobs["reconcile_positions"]()
    await kernel.modelstate.set_gate(Gate.RUNNING, reason="test")
    await settle(kernel)
    standin.clock.advance(1_000)
    from tests.support.inproc import AUTH_HEADER
    import json
    status, _, _ = await standin.disp.handle("POST", "/dev/manual/order", {}, json.dumps(
        {"symbol": SYM, "side": "BUY", "position_side": "LONG", "qty": "0.002"}).encode(),
        {AUTH_HEADER: standin.manual_token})
    assert status == 200
    for _ in range(50):
        await settle(kernel, 1)
        if kernel.takeover.crashed is not None:
            break
    assert kernel.takeover.crashed is not None and kernel.takeover.crashed.name == "takeover_half_registered"
    assert "takeover_half_registered" not in crash.armed
    await abandon(kernel)

    # ---- 重启：同一账本、同一交易所
    kernel2, _ = make_kernel(tmp_path, standin=standin, recovery_factory=None)
    await kernel2.start(serve_http=False, run_jobs=False)
    try:
        await settle(kernel2)
        rec = kernel2.registry.get_active(SYM, LONG)
        assert rec is not None and rec.ownership is Ownership.TAKEOVER
        assert rec.status is PositionStatus.MANAGED and rec.protection_state is ProtectionState.SYSTEM
        result = await kernel2.adapter.fetch_open_algo_orders(SYM)
        assert len(result.value) == 1  # 只有一张止损
    finally:
        await kernel2.stop()


async def test_crashpoint_7_uncommitted_registration_becomes_pending_after_restart(tmp_path):
    """登记没有提交就被杀：交易所有仓、登记簿没有它，重启按 F9 第 4 步进入待确认（不自动接管）。"""
    from tests.support.kernel import make_standin_world
    standin = make_standin_world(equity="100000")
    kernel, standin = make_kernel(
        tmp_path, standin=standin, recovery_factory=None,
        config_overrides={"takeover_symbols": ["BTCUSDT"], "takeover_min_notional": "5", "takeover_max_notional": "500"})
    await kernel.start(serve_http=False, run_jobs=False)
    standin.clock.advance(1_000)
    await abandon(kernel)  # 内核被杀的瞬间，手动仓刚好成交
    standin.engine.place_market_order(symbol=SYM, side="BUY", position_side="LONG", qty="0.002",
                                      client_order_id="web_x1", source="manual")
    kernel2, _ = make_kernel(tmp_path, standin=standin, recovery_factory=None)
    await kernel2.start(serve_http=False, run_jobs=False)
    try:
        await settle(kernel2)
        rec = kernel2.registry.get_active(SYM, LONG)
        assert rec.status is PositionStatus.PENDING and rec.ownership is Ownership.NONE
        assert (await kernel2.adapter.fetch_open_algo_orders(SYM)).value == []
    finally:
        await kernel2.stop()
