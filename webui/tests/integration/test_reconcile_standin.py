"""对账对进程内替身交易所（T0.17）：真实账本、真实的意图/订单/登记簿/入账/协调者/合约数上限、真实的适配层与账户数据流，
只有风控闸与通知器是假的。全部走 ``tests/support/inproc.py``：不开套接字，替身与被测对象共用一个 ManualClock。

覆盖：持仓查询失败（故障开关）时不清登记、不撤保护单，连续 3 次才置抑制；用户在交易所手动开的仓被发现并发布
UnregisteredPositionFound；系统遗留的孤儿止损在零持仓时被撤，持仓非零或读数未知时不撤；手动加仓造成的数量差异
由补拉的成交解释。
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.crashpoints import CrashRegistry
from dmr_kernel.core.events import (
    AccountSnapshotTaken,
    EventBus,
    OrdersSnapshotTaken,
    PositionSnapshotTaken,
    UnregisteredPositionFound,
)
from dmr_kernel.core.ingest import Ingest
from dmr_kernel.core.intents import IntentService, IntentSpec
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.reconcile import Reconciler
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    Gate,
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    PositionSide,
    PositionStatus,
    RiskDecision,
    Suppression,
)
from dmr_kernel.exchange.rules import InstrumentRules
from dmr_kernel.exchange.standin import StandinAdapter
from tests.support.inproc import AUTH_HEADER, make_stack
from tests.support.ledger import open_ledger

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
S = IntentState
SYM, ETH = "BTCUSDT", "ETHUSDT"
ALGO, ORDER = "/api/private/algo_order", "/api/private/order"


class FakeRiskGate:
    async def evaluate(self, *, model, action, now_ms) -> RiskDecision:
        return RiskDecision(True)


class FakeNotifier:
    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def raise_alert(self, level, kind, message, *, dedupe_key=None) -> None:
        self.alerts.append({"level": level, "kind": kind, "message": message, "dedupe_key": dedupe_key})

    def kinds(self) -> list[str]:
        return [a["kind"] for a in self.alerts]


class World:
    def __init__(self, tmp_path, **config: Any) -> None:
        self.stack = make_stack(config={"trade_notional_usdt": D("100"), **config}, request_timeout_s=0.05)
        self.ledger = open_ledger(tmp_path, self.stack.clock)
        self.clock, self.disp, self.config = self.stack.clock, self.stack.disp, self.stack.config
        self.transport = self.stack.transport
        self.bus = EventBus()
        self.events: list[Any] = []
        self.bus.subscribe(None, self.events.append, name="world.collect")
        self.adapter = StandinAdapter(self.transport, capability=self.stack.cap, bus=self.bus, clock=self.clock,
                                      config_get=self.config.__getitem__, request_timeout_s=0.05)
        self.notifier = FakeNotifier()
        self.instance = "T" + self.ledger.nonce
        cfg = self.config.__getitem__
        self.modelstate = ModelStateService(self.ledger, self.bus, self.clock)
        self.registry = Registry(self.ledger, self.bus, self.clock, config_get=cfg)
        self.intents = IntentService(self.ledger, self.bus, self.clock, notifier=self.notifier)
        self.orders = OrderService(self.ledger, self.bus, self.clock, self.adapter, instance=self.instance)
        self.ingest = Ingest(self.ledger, self.bus, self.clock, self.adapter, instance=self.instance, config_get=cfg)
        self.limits = Limits(self.clock, config_get=cfg)
        self.rules = InstrumentRules(self.clock)
        self.coordinator = Coordinator(
            adapter=self.adapter, capability=self.stack.cap, ledger=self.ledger, bus=self.bus, clock=self.clock,
            config_get=cfg, modelstate=self.modelstate, registry=self.registry, intents=self.intents,
            orders=self.orders, riskgate=FakeRiskGate(), limits=self.limits, rules=self.rules,
            notifier=self.notifier, crash=CrashRegistry(live=False), list_check=lambda symbol, side: (True, ""))
        self.reconciler = Reconciler(
            adapter=self.adapter, registry=self.registry, modelstate=self.modelstate, coordinator=self.coordinator,
            ingest=self.ingest, orders=self.orders, limits=self.limits, ledger=self.ledger, bus=self.bus,
            clock=self.clock, config_get=cfg, notifier=self.notifier, instance=self.instance)
        self.stream = self.adapter.account_stream()

    async def start(self, *, stream: bool = True) -> "World":
        await self.bus.start()
        await self.modelstate.load()
        await self.modelstate.set_gate(Gate.RUNNING, reason="测试")
        await self.registry.start()
        await self.intents.start()
        await self.ingest.start()
        await self.rules.refresh(self.adapter)
        await self.coordinator.start()
        if stream:
            await self.stream.start()
        await self.settle()
        return self

    async def settle(self, rounds: int = 4) -> None:
        for _ in range(rounds):
            for _ in range(6):
                await asyncio.sleep(0)
            await self.coordinator.idle(10.0)
            await self.bus.drain(10.0)

    async def refresh_limits(self) -> None:
        """让合约数限额的两份快照是新的（开仓前的名额检查需要）。"""
        now = self.clock.now_ms()
        positions = await self.adapter.fetch_positions()
        orders = await self.adapter.fetch_open_orders()
        algos = await self.adapter.fetch_open_algo_orders()
        assert all(isinstance(r, Known) for r in (positions, orders, algos))
        self.limits.update_positions(positions.value, now)
        self.limits.update_orders(orders.value, algos.value, now)

    async def go(self, intent_spec: IntentSpec):
        await self.refresh_limits()
        result = await self.coordinator.submit(intent_spec)
        await self.settle()
        return await self.intents.get(result.intent_id)

    async def manual(self, path: str, body: dict) -> Any:
        status, payload, _ = await self.disp.handle("POST", path, {}, json.dumps(body).encode(),
                                                    {AUTH_HEADER: self.stack.manual_token})
        assert status == 200, payload
        return payload

    async def exchange_positions(self) -> dict[tuple[str, PositionSide], Any]:
        result = await self.adapter.fetch_positions()
        assert isinstance(result, Known), result
        return {(p.symbol, p.side): p for p in result.value}

    async def exchange_algos(self, symbol: str = SYM) -> list:
        result = await self.adapter.fetch_open_algo_orders(symbol)
        assert isinstance(result, Known), result
        return list(result.value)

    def suppressions(self) -> set:
        return set(self.modelstate.current().suppressions)

    def events_of(self, *types: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, types)]

    def close(self) -> None:
        self.ledger.close_sync()


_seq = 0


def spec(**kw: Any) -> IntentSpec:
    global _seq
    _seq += 1
    fields: dict[str, Any] = dict(action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol=SYM,
                                  side=LONG, idem_key=f"rec:{_seq}", ttl_s=60)
    fields.update(kw)
    return IntentSpec(**fields)


def stop_place_spec(trigger: str, **kw: Any) -> IntentSpec:
    fields: dict[str, Any] = dict(action=IntentAction.STOP_PLACE, model="op", source=IntentSource.PROTECTION,
                                  ttl_s=None, params={"trigger_price": trigger, "working_type": "CONTRACT_PRICE"})
    fields.update(kw)
    return spec(**fields)


@pytest.fixture
def make_world(tmp_path):
    made: list[World] = []

    def factory(**kw: Any) -> World:
        world = World(tmp_path, **kw)
        made.append(world)
        return world

    yield factory
    for world in made:
        world.close()


# ================================================================ 持仓查询失败：不清登记，连续 3 次才置抑制


async def test_a_failing_position_query_keeps_the_registry_and_the_stop_and_sets_the_suppression_on_the_third(make_world):
    world = make_world()
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    assert (await world.go(stop_place_spec("49000"))).state is S.COMPLETED
    rec = world.registry.get_active(SYM, LONG)
    assert rec is not None and rec.status is PositionStatus.MANAGED
    assert await world.reconciler.reconcile_positions() == "ok"  # 正常时一致
    assert (await world.registry.fetch(rec.id)).consistent is True
    writes_before = len(world.transport.sent("POST")) + len(world.transport.sent("DELETE"))

    world.disp.switches.update({"position_query_fail": {"enabled": True, "mode": "error"}})
    for streak in (1, 2):
        assert await world.reconciler.reconcile_positions() == "unknown"
        assert Suppression.RECONCILE_UNKNOWN not in world.suppressions()  # 单次、两次都不置抑制
        assert world.reconciler.status()["unknown_streak"] == streak
    assert await world.reconciler.reconcile_positions() == "unknown"
    assert Suppression.RECONCILE_UNKNOWN in world.suppressions()  # 连续第三次才置

    # 查询失败不是「没有持仓」：登记原样，保护单还在，对账没有向交易所发过任何写请求
    kept = world.registry.get_active(SYM, LONG)
    assert (kept.id, kept.status, kept.qty, kept.version) == (rec.id, PositionStatus.MANAGED, rec.qty, rec.version)
    assert len(await world.exchange_algos()) == 1
    assert len(world.transport.sent("POST")) + len(world.transport.sent("DELETE")) == writes_before

    world.disp.switches.update({"position_query_fail": {"enabled": False}})
    assert await world.reconciler.reconcile_positions() == "ok"  # 一次成功即解除
    assert Suppression.RECONCILE_UNKNOWN not in world.suppressions()
    assert world.reconciler.status()["unknown_streak"] == 0


async def test_a_successful_round_against_the_standin_publishes_what_the_other_modules_need(make_world):
    world = make_world()
    await world.start()
    await world.go(spec())
    await world.go(stop_place_spec("49000"))
    world.events.clear()

    assert await world.reconciler.reconcile_positions() == "ok"
    assert await world.reconciler.reconcile_orders() == "ok"
    await world.settle()

    [positions] = world.events_of(PositionSnapshotTaken)
    assert [(p.symbol, p.side, p.qty) for p in positions.positions] == [(SYM, LONG, D("0.002"))]
    assert positions.server_time_ms == world.clock.now_ms()
    [account] = world.events_of(AccountSnapshotTaken)
    assert account.equity > 0
    [snapshot] = world.events_of(OrdersSnapshotTaken)
    assert [a.client_algo_id for a in snapshot.algo_orders] == [(await world.exchange_algos())[0].client_algo_id]
    assert world.limits.occupancy(degraded=False, now_ms=world.clock.now_ms()).keys == {(SYM, "LONG")}


# ================================================================ 用户在交易所手动开的仓被发现


async def test_a_position_opened_by_hand_on_the_exchange_is_discovered(make_world):
    world = make_world()
    await world.start(stream=False)  # 数据流没有送来这笔成交：只靠对账发现
    await world.manual("/dev/manual/order", {"symbol": ETH, "side": "BUY", "position_side": "LONG", "qty": "0.1"})

    assert await world.reconciler.reconcile_positions() == "ok"  # 外部仓不是「对账不一致」
    await world.settle()

    [found] = world.events_of(UnregisteredPositionFound)
    assert (found.symbol, found.side, found.qty, found.trigger) == (ETH, LONG, D("0.1"), "reconcile")
    assert found.entry_price == D("3000")
    assert world.registry.all_active() == []  # 归类是模型 2 的事，对账不登记
    assert world.suppressions() == set()

    assert await world.reconciler.reconcile_positions() == "ok"
    await world.settle()
    assert len(world.events_of(UnregisteredPositionFound)) == 1  # 没有变化就不重复报


async def test_a_system_open_whose_fills_are_not_booked_yet_is_not_mistaken_for_an_external_position(make_world):
    world = make_world()
    await world.start(stream=False)  # 没有数据流：成交明细要等补拉
    opened = await world.go(spec())
    assert opened.state is S.COMPLETED  # 市价单的 REST 应答已经是 FILLED
    assert world.registry.get_active(SYM, LONG) is None  # 成交还没入账，登记簿还没有这个仓

    assert await world.reconciler.reconcile_positions() == "ok"
    await world.settle()
    assert world.events_of(UnregisteredPositionFound) == []
    # 对账发现「系统订单已成交而成交明细还没入账」时，立即补拉该合约的成交（T0.27 修正），不等调度器的 60 秒补拉
    assert world.registry.get_active(SYM, LONG) is not None
    assert await world.ingest.backfill_trades([SYM]) == 0  # 已经入账，不会重复
    await world.settle()
    assert await world.reconciler.reconcile_positions() == "ok"
    await world.settle()
    assert world.events_of(UnregisteredPositionFound) == []


# ================================================================ 数量差异由补拉的成交解释


async def test_a_manual_add_that_the_stream_missed_is_explained_by_the_backfilled_trades(make_world):
    world = make_world()
    await world.start()
    await world.go(spec())
    rec = world.registry.get_active(SYM, LONG)
    await world.stream.stop()  # 数据流断了：用户在这期间手动加仓
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "BUY", "position_side": "LONG", "qty": "0.001"})
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.003")
    assert world.registry.get_active(SYM, LONG).qty == D("0.002")

    assert await world.reconciler.reconcile_positions() == "ok"
    await world.settle()

    stored = await world.registry.fetch(rec.id)
    assert stored.qty == D("0.003") and stored.is_mixed is True and stored.consistent is True  # 登记簿按外部调整跟上
    assert world.suppressions() == set()
    assert world.reconciler.status()["diffs"] == []


# ================================================================ 系统遗留的孤儿止损


async def orphan_world(make_world):
    """系统开了仓并挂了止损，但登记簿没有记下这张止损（模拟登记簿落后）；止损建立已超过一个条件单对账周期。"""
    world = make_world()
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    assert (await world.go(stop_place_spec("49000"))).state is S.COMPLETED
    [stop] = await world.exchange_algos()
    world.clock.advance(61_000)
    return world, stop


async def test_an_orphan_stop_is_kept_while_the_position_is_not_zero(make_world):
    world, stop = await orphan_world(make_world)

    assert await world.reconciler.reconcile_positions() == "ok"
    assert await world.reconciler.reconcile_orders() == "ok"
    await world.settle()

    assert [a.algo_id for a in await world.exchange_algos()] == [stop.algo_id]  # 持仓非零：撤掉会让仓位失去保护
    assert "orphan_stop_kept" in world.notifier.kinds()
    assert world.transport.sent("DELETE", ALGO) == []


async def test_an_orphan_stop_is_kept_while_the_position_reading_is_unknown(make_world):
    world, stop = await orphan_world(make_world)
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "LONG", "qty": "0.002"})
    await world.settle()
    assert (SYM, LONG) not in await world.exchange_positions()  # 仓位其实已经是零了……
    world.disp.switches.update({"position_query_fail": {"enabled": True, "mode": "error"}})

    assert await world.reconciler.reconcile_positions() == "unknown"  # ……但这一轮读不到
    assert await world.reconciler.reconcile_orders() == "ok"
    await world.settle()

    assert [a.algo_id for a in await world.exchange_algos()] == [stop.algo_id]  # 未知不当成零：不撤
    assert world.transport.sent("DELETE", ALGO) == []


async def test_an_orphan_stop_is_cancelled_through_the_coordinator_once_the_position_is_zero(make_world):
    world, stop = await orphan_world(make_world)
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "LONG", "qty": "0.002"})
    await world.settle()
    assert (SYM, LONG) not in await world.exchange_positions()  # 用户手动平掉了仓位，止损单还挂着（替身默认不自动撤）
    assert [a.algo_id for a in await world.exchange_algos()] == [stop.algo_id]

    assert await world.reconciler.reconcile_positions() == "ok"  # 新鲜的快照：这一侧明确为零
    assert await world.reconciler.reconcile_orders() == "ok"
    await world.settle()

    assert await world.exchange_algos() == []  # 经协调者的 STOP_CANCEL 撤掉了
    assert await world.intents.non_terminal() == []  # 撤单意图已到终态
    done = await world.intents.get_by_key(f"orphan:{stop.algo_id}")
    assert done.state is S.COMPLETED and done.action is IntentAction.STOP_CANCEL
    assert done.params == {"client_algo_id": stop.client_algo_id, "algo_id": stop.algo_id}
    assert len(world.transport.sent("DELETE", ALGO)) == 1


async def test_an_orphan_cancel_that_a_crash_left_undispatched_is_resumed_and_the_orphan_is_cancelled(make_world):
    # 复核补充：上一个进程把撤单意图落了盘、还没派发就被杀。协调者不会自己派发没有订单行的意图（它等提交者重新提交），
    # 对账若只看到「有在途的撤单尝试」就跳过，孤儿永远撤不掉，这个键也一直「在途」（F3 在这个键上永远不判）
    world, stop = await orphan_world(make_world)
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "LONG", "qty": "0.002"})
    await world.settle()
    stuck, _ = await world.intents.create(IntentSpec(
        action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.RECONCILE, symbol=SYM, side=LONG,
        idem_key=f"orphan:{stop.algo_id}", params={"client_algo_id": stop.client_algo_id, "algo_id": stop.algo_id}))
    await world.settle()
    await world.coordinator.resolve_unknown_orders()  # 协调者自己的回查不会去派发它
    await world.settle()
    assert (await world.intents.get(stuck.id)).state is S.CREATED
    assert world.coordinator.key_busy(SYM, LONG)
    world.clock.advance(61_000)

    assert await world.reconciler.reconcile_positions() == "ok"
    assert await world.reconciler.reconcile_orders() == "ok"
    await world.settle()

    assert await world.exchange_algos() == []  # 同一个幂等键再交一次，协调者派发了它
    assert (await world.intents.get(stuck.id)).state is S.COMPLETED
    assert await world.intents.non_terminal() == [] and not world.coordinator.key_busy(SYM, LONG)
    assert len(world.transport.sent("DELETE", ALGO)) == 1


async def test_a_young_orphan_is_left_for_the_next_period_and_then_cancelled(make_world):
    world = make_world()
    await world.start()
    await world.go(spec())
    await world.go(stop_place_spec("49000"))
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "LONG", "qty": "0.002"})
    await world.settle()
    world.clock.advance(30_000)  # 还不到一个条件单对账周期

    await world.reconciler.reconcile_positions()
    await world.reconciler.reconcile_orders()
    await world.settle()
    assert len(await world.exchange_algos()) == 1

    world.clock.advance(31_000)  # 现在超过了
    await world.reconciler.reconcile_positions()
    await world.reconciler.reconcile_orders()
    await world.settle()
    assert await world.exchange_algos() == []
