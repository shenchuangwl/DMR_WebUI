"""协调者对进程内替身交易所（T0.16）：真实账本、真实的意图/订单/登记簿/入账服务、真实的适配层与账户数据流，
只有风控闸与通知器是假的。全部走 ``tests/support/inproc.py``：不开套接字，替身与被测对象共用一个 ManualClock。

「世界」(``World``) 把一整套装配在一起；``World.restart()`` 造「重启之后」的新进程：同一个账本、同一个替身交易所，
全新的总线、适配层与服务。
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.crashpoints import CrashRegistry, CrashSignal
from dmr_kernel.core.events import EventBus, IntentStateChanged
from dmr_kernel.core.ingest import Ingest
from dmr_kernel.core.intents import IntentService, IntentSpec
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    AlgoType,
    Gate,
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    OrderSide,
    OrderState,
    OrderPurpose,
    Ownership,
    PositionSide,
    PositionStatus,
    RiskDecision,
    Suppression,
)
from dmr_kernel.exchange.rules import InstrumentRules
from dmr_kernel.exchange.standin import StandinAdapter
from dmr_kernel.ledger import repo_ingest
from tests.support.inproc import AUTH_HEADER, make_stack
from tests.support.ledger import open_ledger

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
BUY, SELL = OrderSide.BUY, OrderSide.SELL
S = IntentState
O = OrderState
SYM = "BTCUSDT"


class FakeRiskGate:
    def __init__(self) -> None:
        self.decision = RiskDecision(True)
        self.calls: list[dict[str, Any]] = []

    async def evaluate(self, *, model, action, now_ms) -> RiskDecision:
        self.calls.append({"model": model, "action": action, "now_ms": now_ms})
        return self.decision


class FakeNotifier:
    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def raise_alert(self, level, kind, message, *, dedupe_key=None) -> None:
        self.alerts.append({"level": level, "kind": kind, "message": message, "dedupe_key": dedupe_key})

    def kinds(self) -> list[str]:
        return [a["kind"] for a in self.alerts]


class HookTransport:
    """包在传输外面：匹配的请求先（或后）交给钩子——可以阻塞，也可以趁机去改交易所那一侧的状态。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.rules: list[dict[str, Any]] = []

    def on(self, method: str, path: str, fn, *, after: bool = False, times: int = 1) -> None:
        self.rules.append({"method": method, "path": path, "fn": fn, "after": after, "times": times})

    async def _fire(self, method: str, path: str, params, body, *, after: bool) -> None:
        for rule in self.rules:
            if rule["method"] == method and rule["path"] == path and rule["after"] is after and rule["times"] > 0:
                rule["times"] -= 1
                await rule["fn"](params, body)

    async def request(self, method, path, *, params=None, body=None, auth=True, timeout_s):
        await self._fire(method, path, params, body, after=False)
        response = await self.inner.request(method, path, params=params, body=body, auth=auth, timeout_s=timeout_s)
        await self._fire(method, path, params, body, after=True)
        return response

    async def ws_connect(self, path, *, params, timeout_s):
        return await self.inner.ws_connect(path, params=params, timeout_s=timeout_s)

    async def close(self) -> None:
        await self.inner.close()


ORDER, ALGO = "/api/private/order", "/api/private/algo_order"


class World:
    def __init__(self, tmp_path, *, live: bool = False, stack=None, ledger=None, **config: Any) -> None:
        if stack is None:
            stack = make_stack(config={"trade_notional_usdt": D("100"), **config}, request_timeout_s=0.05)
            ledger = open_ledger(tmp_path, stack.clock)
        self.stack, self.ledger = stack, ledger
        self.clock, self.disp, self.engine, self.feed = stack.clock, stack.disp, stack.engine, stack.feed
        self.config, self.cap, self.transport = stack.config, stack.cap, stack.transport
        if not isinstance(self.transport.inner, HookTransport):
            self.transport.inner = HookTransport(self.transport.inner)
        self.hooks: HookTransport = self.transport.inner
        self.live = live
        self.bus = EventBus()
        self.events: list = []
        self.bus.subscribe(None, self.events.append, name="world.collect")
        self.adapter = StandinAdapter(stack.transport, capability=stack.cap, bus=self.bus, clock=self.clock,
                                      config_get=self.config.__getitem__, request_timeout_s=0.05)
        self.notifier = FakeNotifier()
        self.riskgate = FakeRiskGate()
        self.crash = CrashRegistry(live=live)
        self.instance = "T" + ledger.nonce
        cfg = self.config.__getitem__
        self.modelstate = ModelStateService(ledger, self.bus, self.clock)
        self.registry = Registry(ledger, self.bus, self.clock, config_get=cfg)
        self.intents = IntentService(ledger, self.bus, self.clock, notifier=self.notifier)
        self.orders = OrderService(ledger, self.bus, self.clock, self.adapter, instance=self.instance)
        self.ingest = Ingest(ledger, self.bus, self.clock, self.adapter, instance=self.instance, config_get=cfg)
        self.limits = Limits(self.clock, config_get=cfg)
        self.rules = InstrumentRules(self.clock)
        self.list_result: Any = (True, "")
        self.coordinator = Coordinator(
            adapter=self.adapter, capability=self.cap, ledger=ledger, bus=self.bus, clock=self.clock, config_get=cfg,
            modelstate=self.modelstate, registry=self.registry, intents=self.intents, orders=self.orders,
            riskgate=self.riskgate, limits=self.limits, rules=self.rules, notifier=self.notifier, crash=self.crash,
            list_check=lambda symbol, side: self.list_result)
        self.stream = self.adapter.account_stream()

    # ------------------------------------------------------------ 生命周期

    async def start(self, *, stream: bool = True) -> "World":
        await self.bus.start()
        await self.modelstate.load()
        await self.modelstate.set_gate(Gate.RUNNING, reason="测试")
        await self.registry.start()
        await self.intents.start()
        await self.ingest.start()
        await self.rules.refresh(self.adapter)
        await self.refresh_limits()
        await self.coordinator.start()
        if stream:
            await self.stream.start()
        await self.settle()
        return self

    async def kill(self) -> None:
        """模拟进程被杀：此后不再处理任何事件（数据流断开、总线停止）。"""
        await self.stream.stop()
        await self.bus.stop()

    def restart(self) -> "World":
        """「重启」：同一个账本、同一个替身交易所，其余全部重新造。"""
        return World(None, live=self.live, stack=self.stack, ledger=self.ledger)

    async def settle(self, rounds: int = 4) -> None:
        """让数据流读循环、后台派发任务和事件处理都跑完（处理函数可能再发布事件，所以反复几轮）。"""
        for _ in range(rounds):
            for _ in range(6):
                await asyncio.sleep(0)
            await self.coordinator.idle(10.0)
            await self.bus.drain(10.0)

    async def refresh_limits(self) -> None:
        """让合约数限额的两份快照是新的（快照 20 秒、120 秒没更新就过期）。"""
        now = self.clock.now_ms()
        positions = await self.adapter.fetch_positions()
        orders = await self.adapter.fetch_open_orders()
        algos = await self.adapter.fetch_open_algo_orders()
        assert all(isinstance(r, Known) for r in (positions, orders, algos))
        self.limits.update_positions(positions.value, now)
        self.limits.update_orders(orders.value, algos.value, now)

    # ------------------------------------------------------------ 提交与读取

    async def go(self, spec: IntentSpec):
        result = await self.coordinator.submit(spec)
        await self.settle()
        return await self.intents.get(result.intent_id)

    async def go_all(self, *specs: IntentSpec):
        ids_ = [(await self.coordinator.submit(s)).intent_id for s in specs]
        await self.settle()
        return [await self.intents.get(i) for i in ids_]

    async def only_order(self, intent_id: int):
        rows = await self.orders.for_intent(intent_id)
        assert len(rows) == 1, rows
        return rows[0]

    async def fills(self, **filters):
        return await self.ledger.read(repo_ingest.list_fills, **filters)

    async def exchange_positions(self) -> dict[tuple[str, PositionSide], Any]:
        result = await self.adapter.fetch_positions()
        assert isinstance(result, Known), result
        return {(p.symbol, p.side): p for p in result.value}

    async def exchange_algos(self, symbol: str = SYM) -> list:
        result = await self.adapter.fetch_open_algo_orders(symbol)
        assert isinstance(result, Known), result
        return list(result.value)

    async def manual(self, path: str, body: dict) -> Any:
        status, payload, _ = await self.disp.handle("POST", path, {}, json.dumps(body).encode(),
                                                    {AUTH_HEADER: self.stack.manual_token})
        assert status == 200, payload
        return payload

    def sent(self, method: str, path: str) -> list:
        return self.transport.sent(method, path)

    def alerts(self, kind: str) -> list[dict[str, Any]]:
        return [a for a in self.notifier.alerts if a["kind"] == kind]

    def close(self) -> None:
        self.ledger.close_sync()


_seq = 0


def spec(**kw: Any) -> IntentSpec:
    """默认是模型 1 的信号开仓（BTCUSDT 多）；幂等键自动不重复，有效期 60 秒。"""
    global _seq
    _seq += 1
    fields: dict[str, Any] = dict(action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol=SYM,
                                  side=LONG, idem_key=f"it:{_seq}", ttl_s=60)
    fields.update(kw)
    return IntentSpec(**fields)


def close_spec(**kw: Any) -> IntentSpec:
    """平掉整个仓（操作员发起）。"""
    fields: dict[str, Any] = dict(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR, ttl_s=None)
    fields.update(kw)
    return spec(**fields)


def partial_spec(qty: str, **kw: Any) -> IntentSpec:
    fields: dict[str, Any] = dict(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                                  ttl_s=None, qty=D(qty))
    fields.update(kw)
    return spec(**fields)


def stop_place_spec(trigger: str, **kw: Any) -> IntentSpec:
    fields: dict[str, Any] = dict(action=IntentAction.STOP_PLACE, model="op", source=IntentSource.PROTECTION,
                                  ttl_s=None, params={"trigger_price": trigger, "working_type": "CONTRACT_PRICE"})
    fields.update(kw)
    return spec(**fields)


def stop_replace_spec(new: str, old: dict[str, Any], mode: str = "gap", **kw: Any) -> IntentSpec:
    params = {"old": old, "trigger_price": new, "working_type": "CONTRACT_PRICE", "mode": mode}
    fields: dict[str, Any] = dict(action=IntentAction.STOP_REPLACE, model="op", source=IntentSource.PROTECTION,
                                  ttl_s=None, params=params)
    fields.update(kw)
    return spec(**fields)


def order_cancel_spec(cid: str, **kw: Any) -> IntentSpec:
    fields: dict[str, Any] = dict(action=IntentAction.ORDER_CANCEL, model="op", source=IntentSource.OPERATOR,
                                  ttl_s=None, params={"client_order_id": cid})
    fields.update(kw)
    return spec(**fields)


def stop_cancel_spec(cid: str | None, algo_id: str | None, **kw: Any) -> IntentSpec:
    fields: dict[str, Any] = dict(action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.PROTECTION,
                                  ttl_s=None, params={"client_algo_id": cid, "algo_id": algo_id})
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
    closed = set()
    for world in made:
        if id(world.ledger) not in closed:
            closed.add(id(world.ledger))
            world.close()


@pytest.fixture
def world(make_world) -> World:
    return make_world()


# ================================================================ 开仓走完整条链


async def test_an_open_walks_through_the_standin_and_ends_as_one_fill_and_one_registered_position(world):
    await world.start()
    rec = await world.go(spec())
    assert rec.state is S.COMPLETED and "已全部成交" in rec.state_reason
    order = await world.only_order(rec.id)
    assert (order.state, order.qty, order.filled_qty) == (O.FILLED, D("0.002"), D("0.002"))  # 100 / 50000
    assert (order.side, order.position_side) == (BUY, LONG)
    # 交易所那一侧：持仓、杠杆与保证金模式都按配置设过了（替身默认是 20 倍全仓）
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.002")
    leverage = (await world.adapter.fetch_leverage(SYM)).value
    assert (leverage.leverage, leverage.margin_mode) == (2, "isolated")
    assert len(world.sent("POST", ORDER)) == 1
    # 账本：数据流送来的成交只有一行；登记簿据此登记了策略仓
    [fill] = await world.fills()
    assert (fill.client_order_id, fill.qty, fill.price, fill.path) == (order.client_order_id, D("0.002"),
                                                                       D("50000"), "stream")
    assert fill.intent_id == rec.id
    held = world.registry.get_active(SYM, LONG)
    assert (held.qty, held.avg_price, held.ownership, held.status) == (D("0.002"), D("50000"), Ownership.STRATEGY,
                                                                       PositionStatus.MANAGED)
    assert not world.coordinator.key_busy(SYM, LONG)
    assert world.limits.occupancy(degraded=False, now_ms=world.clock.now_ms()).reserved == 0


async def test_the_pending_send_order_row_is_on_disk_when_the_request_reaches_the_exchange(world):
    await world.start()
    seen = []

    async def probe(params, body):
        row = await world.orders.get(body["client_order_id"])
        intent = await world.intents.get(row.intent_id)
        seen.append((row.state, row.sent_ms, intent.state, body["qty"]))

    world.hooks.on("POST", ORDER, probe)
    rec = await world.go(spec())
    assert seen == [(O.PENDING_SEND, world.clock.now_ms(), S.DISPATCH_READY, "0.002")]
    assert rec.state is S.COMPLETED


async def test_the_same_intent_never_produces_a_second_order_on_the_exchange(world):
    await world.start()
    same = spec(idem_key="open:m1:BTCUSDT:LONG:1791943200000")
    first = await world.coordinator.submit(same)
    again = await world.coordinator.submit(same)  # 派发还在跑
    await world.settle()
    later = await world.coordinator.submit(same)  # 已经结束
    await world.settle()
    assert (first.created, again.created, later.created) == (True, False, False)
    assert first.intent_id == again.intent_id == later.intent_id
    assert len(world.sent("POST", ORDER)) == 1
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.002")
    assert len(await world.fills()) == 1


async def test_a_fill_that_is_ingested_after_the_intent_completed_leaves_it_completed(world):
    """应答里订单就是 FILLED，意图先完成；数据流没连，成交靠补拉随后入账。入账侧会把已完成的意图当作「晚到成交」
    重开成 ACTIVE，协调者要把它再完成，不能卡在 ACTIVE。"""
    await world.start(stream=False)
    rec = await world.go(spec())
    assert rec.state is S.COMPLETED and await world.fills() == []
    assert await world.ingest.backfill_trades([SYM]) == 1
    await world.settle()
    assert (await world.intents.get(rec.id)).state is S.COMPLETED
    assert not world.coordinator.key_busy(SYM, LONG)
    [fill] = await world.fills()
    assert fill.path == "rest" and fill.intent_id == rec.id
    assert world.registry.get_active(SYM, LONG).qty == D("0.002")


async def test_a_fill_of_a_completed_intents_own_filled_order_is_not_a_late_fill(world):
    """订单在应答里就是 FILLED、意图先完成，成交随后入账是常态，不是晚到成交（实施记录 R17）。"""
    await world.start(stream=False)
    rec = await world.go(spec())
    assert rec.state is S.COMPLETED
    await world.ingest.backfill_trades([SYM])
    await world.settle()
    assert world.alerts("intent_late_fill") == []


# ================================================================ 崩溃点、重启与超时


async def test_open_sent_crash_then_restart_finds_the_order_by_its_id_and_never_resends(world):
    await world.start(stream=False)  # 数据流没连：成交只能事后补拉
    world.crash.arm("open_sent")
    result = await world.coordinator.submit(spec(idem_key="open:m1:BTCUSDT:LONG:crash"))
    with pytest.raises(CrashSignal):
        await world.coordinator.wait_terminal(result.intent_id, 5.0)
    row = await world.only_order(result.intent_id)
    assert row.state is O.PENDING_SEND and row.sent_ms is not None  # 订单行已落盘，结果没记
    assert (await world.intents.get(result.intent_id)).state is S.DISPATCH_READY
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.002")  # 请求确实到了交易所
    await world.kill()

    again = world.restart()  # 全新的总线、适配层与服务；同一个账本、同一个替身交易所
    await again.start(stream=False)
    assert again.coordinator.key_busy(SYM, LONG)  # 没有重新提交，账本里未到终态的意图也算在途
    again.clock.advance(6_000)  # 订单发出已超过 5 秒
    await again.coordinator.resolve_unknown_orders()
    await again.settle()
    assert (await again.intents.get(result.intent_id)).state is S.COMPLETED
    assert (await again.only_order(result.intent_id)).state is O.FILLED
    assert len(again.sent("POST", ORDER)) == 1  # 两个进程加起来也只发过一次
    assert not again.coordinator.key_busy(SYM, LONG)
    assert await again.ingest.backfill_trades([SYM]) == 1  # 成交事后补拉入账，登记簿据此登记
    await again.settle()
    assert again.registry.get_active(SYM, LONG).qty == D("0.002")
    assert (await again.intents.get(result.intent_id)).state is S.COMPLETED


async def test_resubmitting_after_the_crash_turns_into_a_lookup_not_a_second_order(world):
    await world.start(stream=False)
    world.crash.arm("open_sent")
    same = spec(idem_key="open:m1:BTCUSDT:LONG:crash-2")
    first = await world.coordinator.submit(same)
    with pytest.raises(CrashSignal):
        await world.coordinator.wait_terminal(first.intent_id, 5.0)
    await world.kill()

    again = world.restart()
    await again.start(stream=False)
    replay = await again.coordinator.submit(same)  # 信号重放：同一个幂等键
    await again.settle()
    assert (replay.created, replay.intent_id) == (False, first.intent_id)
    assert (await again.intents.get(first.intent_id)).state is S.RECONCILING  # 按编号回查之前不重发
    again.clock.advance(6_000)
    await again.coordinator.resolve_unknown_orders()
    await again.settle()
    assert (await again.intents.get(first.intent_id)).state is S.COMPLETED
    assert len(again.sent("POST", ORDER)) == 1


async def test_partial_close_sent_crash_then_restart_finds_the_order_and_never_resends(world):
    await world.start(stream=False)
    assert (await world.go(spec(params={"notional": 200}))).state is S.COMPLETED  # 0.004
    world.crash.arm("partial_close_sent")
    result = await world.coordinator.submit(partial_spec("0.001"))
    with pytest.raises(CrashSignal):
        await world.coordinator.wait_terminal(result.intent_id, 5.0)
    row = await world.only_order(result.intent_id)
    assert row.state is O.PENDING_SEND and row.sent_ms is not None
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.003")  # 请求确实到了交易所
    await world.kill()

    again = world.restart()
    await again.start(stream=False)
    again.clock.advance(6_000)
    await again.coordinator.resolve_unknown_orders()
    await again.settle()
    assert (await again.intents.get(result.intent_id)).state is S.COMPLETED
    assert (await again.only_order(result.intent_id)).state is O.FILLED
    assert len(again.sent("POST", ORDER)) == 2  # 开仓一次、部分平仓一次，没有重发
    assert (await again.exchange_positions())[(SYM, LONG)].qty == D("0.003")


async def test_stop_cancelled_before_new_crash_then_restart_leaves_the_rebuild_to_recovery(world):
    await world.start(stream=False)
    assert (await world.go(spec())).state is S.COMPLETED
    old = await place_stop(world, "45000")
    world.crash.arm("stop_cancelled_before_new")
    result = await world.coordinator.submit(stop_replace_spec("46000", old_of(old)))
    with pytest.raises(CrashSignal):
        await world.coordinator.wait_terminal(result.intent_id, 5.0)
    assert await world.exchange_algos() == []  # 旧单已撤，新单还没挂：这一刻没有止损
    new_row = await world.only_order(result.intent_id)
    assert new_row.state is O.PENDING_SEND and new_row.sent_ms is None  # 新单的订单行已落盘，请求没发
    await world.kill()

    again = world.restart()
    await again.start(stream=False)
    for _ in range(3):  # 回查：交易所没有这张新单，连续 3 次
        again.clock.advance(6_000)
        await again.coordinator.resolve_unknown_orders()
        await again.settle()
    done = await again.intents.get(result.intent_id)
    result_ = done.params["result"]
    assert done.state is S.REJECTED and result_["outcome"] == "unprotected" and result_["stop_in_place"] is False
    assert await again.exchange_algos() == []  # 协调者自己不补挂：按登记的止损价重建是保护单管理与恢复的事
    assert len(again.sent("POST", ALGO)) == 1  # 只有当初那一次挂止损


async def test_a_lost_open_response_is_settled_by_looking_the_order_up_never_by_resending(world):
    await world.start(stream=False)
    world.disp.switches.update({"order_timeout": {"enabled": True, "mode": "accept_then_hang", "hang_s": 30,
                                                  "count": 1}})
    rec = await world.go(spec())
    assert rec.state is S.RECONCILING and world.coordinator.key_busy(SYM, LONG)  # 回查期间这个键算有在途订单
    assert (await world.only_order(rec.id)).state is O.SENT_UNKNOWN
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.002")  # 交易所其实成交了
    world.clock.advance(6_000)
    await world.coordinator.resolve_unknown_orders()
    await world.settle()
    assert (await world.intents.get(rec.id)).state is S.COMPLETED
    assert (await world.only_order(rec.id)).state is O.FILLED
    assert len(world.sent("POST", ORDER)) == 1 and not world.coordinator.key_busy(SYM, LONG)


async def test_a_lost_open_response_is_settled_by_the_stream_when_the_fill_arrives_first(world):
    await world.start()
    world.disp.switches.update({"order_timeout": {"enabled": True, "mode": "accept_then_hang", "hang_s": 30,
                                                  "count": 1}})
    rec = await world.go(spec())
    assert rec.state is S.COMPLETED  # 成交回报先到：订单已是 FILLED，应答再未知也不倒退
    assert (await world.only_order(rec.id)).state is O.FILLED
    assert len(world.sent("POST", ORDER)) == 1 and len(await world.fills()) == 1


async def test_a_dropped_open_request_is_rejected_after_three_empty_lookups_and_never_resent(world):
    await world.start()
    world.disp.switches.update({"order_timeout": {"enabled": True, "mode": "drop", "hang_s": 30, "count": 1}})
    rec = await world.go(spec())
    assert rec.state is S.RECONCILING
    for waited in (6, 12, 18):  # 3 次查到不存在，但离发出还不到 5 秒窗口 + 15 秒宽限
        world.clock.advance(6_000)
        await world.coordinator.resolve_unknown_orders()
        await world.settle()
        assert (await world.intents.get(rec.id)).state is S.RECONCILING, waited
    world.clock.advance(6_000)
    await world.coordinator.resolve_unknown_orders()
    await world.settle()
    done = await world.intents.get(rec.id)
    assert done.state is S.REJECTED and "回查" in done.state_reason
    assert (await world.only_order(rec.id)).state is O.REJECTED
    assert len(world.sent("POST", ORDER)) == 1  # 开仓单绝不重发
    assert await world.exchange_positions() == {} and not world.coordinator.key_busy(SYM, LONG)


# ================================================================ 开仓的放弃分支（交易所一侧的真实状态）


async def test_opens_on_the_same_bar_cannot_jump_the_symbol_cap_together(make_world):
    world = make_world(max_symbols=2)
    await world.start()
    gate = asyncio.Event()

    async def hold(params, body):  # 前两个意图卡在发单上：名额一直占着，后面的只能按占用情况被拒
        await gate.wait()

    world.hooks.on("POST", ORDER, hold, times=4)
    ids_ = [(await world.coordinator.submit(spec(symbol=s))).intent_id
            for s in ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "SOLUSDT")]
    for _ in range(300):
        late = [(await world.intents.get(i)).state for i in ids_[2:]]
        if late == [S.REJECTED, S.REJECTED]:
            break
        await asyncio.sleep(0.01)
    assert late == [S.REJECTED, S.REJECTED]  # 先提交的先占名额；同一根 K 线上的后两个挤不进去
    assert world.limits.occupancy(degraded=False, now_ms=world.clock.now_ms()).reserved == 2
    gate.set()
    await world.settle()
    recs = [await world.intents.get(i) for i in ids_]
    assert [r.state for r in recs] == [S.COMPLETED, S.COMPLETED, S.REJECTED, S.REJECTED]
    assert all("持仓上限" in r.state_reason for r in recs[2:])
    assert {symbol for symbol, _ in await world.exchange_positions()} == {"BTCUSDT", "ETHUSDT"}
    assert len(world.sent("POST", ORDER)) == 2


async def test_a_position_on_either_side_makes_the_open_give_up_without_any_order(world):
    await world.start()
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "SHORT", "qty": "0.001"})
    rec = await world.go(spec(side=LONG))  # 对侧有别人开的仓
    assert rec.state is S.REJECTED and "SHORT" in rec.state_reason and "已有持仓" in rec.state_reason
    assert world.sent("POST", ORDER) == []
    assert (await world.go(spec(symbol="ETHUSDT"))).state is S.COMPLETED  # 别的合约不受影响


async def test_a_users_untriggered_stop_makes_the_open_give_up_and_is_left_alone(world):
    await world.start()
    await world.manual("/dev/manual/algo_order", {"symbol": SYM, "side": "SELL", "position_side": "LONG",
                                                  "type": "STOP_MARKET", "trigger_price": "40000",
                                                  "close_position": True})
    rec = await world.go(spec())
    assert rec.state is S.REJECTED and "用户自己的" in rec.state_reason
    assert world.sent("POST", ORDER) == [] and world.sent("DELETE", ALGO) == []  # 不替用户撤
    assert len(await world.exchange_algos()) == 1


async def test_a_left_over_system_stop_is_cancelled_and_confirmed_gone_before_the_open(world):
    await world.start()
    first = await world.go(spec())
    assert first.state is S.COMPLETED
    stop = await world.go(stop_place_spec("40000"))
    assert stop.state is S.COMPLETED and len(await world.exchange_algos()) == 1
    closed = await world.go(close_spec())  # 平仓之后全平型止损还留在交易所（替身默认不自动撤销）
    assert closed.state is S.COMPLETED and await world.exchange_positions() == {}
    assert len(await world.exchange_algos()) == 1
    await world.refresh_limits()
    again = await world.go(spec())
    assert again.state is S.COMPLETED
    assert await world.exchange_algos() == []  # 先撤掉残留的系统止损，再开新仓
    assert len(world.sent("DELETE", ALGO)) == 1
    stop_row = await world.only_order(stop.id)
    assert stop_row.state is O.CANCELLED  # 撤单结果记进了那张止损的订单行


async def test_the_gate_the_suppression_and_the_risk_gate_stop_an_open_but_never_a_close(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    await world.refresh_limits()
    posts_before = len(world.sent("POST", ORDER))

    await world.modelstate.set_gate(Gate.STOPPED, reason="测试：关闸")
    assert (await world.go(spec(symbol="ETHUSDT"))).state is S.REJECTED
    await world.modelstate.set_gate(Gate.RUNNING, reason="测试：开闸")

    await world.modelstate.add_suppression(Suppression.UNPROTECTED, reason="测试：有仓无保护")
    assert (await world.go(spec(symbol="ETHUSDT"))).state is S.REJECTED
    await world.modelstate.remove_suppression(Suppression.UNPROTECTED, reason="测试")

    world.riskgate.decision = RiskDecision(False, "日亏损已触发", "account")
    risky = await world.go(spec(symbol="ETHUSDT"))
    assert risky.state is S.REJECTED and "风控拒绝" in risky.state_reason
    assert len(world.sent("POST", ORDER)) == posts_before  # 三种情况下都没有发出开仓单

    # 三样都在，平仓照常执行
    await world.modelstate.set_gate(Gate.STOPPED, reason="测试：再关闸")
    await world.modelstate.add_suppression(Suppression.RISK_TRIPPED, reason="测试")
    closed = await world.go(close_spec())
    assert closed.state is S.COMPLETED and await world.exchange_positions() == {}


# ================================================================ 平仓


async def test_a_close_sends_the_opposite_side_with_the_live_quantity_and_the_position_ends_up_closed(world):
    await world.start()
    assert (await world.go(spec(side=SHORT))).state is S.COMPLETED
    rec = await world.go(close_spec(side=SHORT))
    assert rec.state is S.COMPLETED and "已全部成交" in rec.state_reason
    order = await world.only_order(rec.id)
    assert (order.purpose, order.side, order.position_side, order.qty) == (OrderPurpose.CLOSE, BUY, SHORT, D("0.002"))
    body = world.sent("POST", ORDER)[-1][3]
    assert (body["side"], body["position_side"], body["qty"]) == ("BUY", "SHORT", "0.002")
    assert "reduce_only" not in body and "close_position" not in body  # 不带只减仓标记
    assert await world.exchange_positions() == {}
    assert world.registry.get_active(SYM, SHORT) is None  # 平仓成交入账，登记簿据此归零
    assert [f.client_order_id for f in await world.fills()][-1] == order.client_order_id
    assert len(await world.fills()) == 2


async def test_a_close_of_a_position_that_is_already_gone_is_completed_without_any_order(world):
    await world.start()
    rec = await world.go(close_spec())
    assert rec.state is S.COMPLETED and "已由其它途径平掉" in rec.state_reason
    assert world.sent("POST", ORDER) == [] and await world.orders.for_intent(rec.id) == []


async def test_a_position_that_shrinks_between_the_read_and_the_order_is_resent_once_with_the_new_quantity(world):
    await world.start()
    assert (await world.go(spec(params={"notional": 200}))).state is S.COMPLETED  # 0.004

    async def shrink(params, body):  # 平仓单到达交易所之前，有人手动平掉了 0.001
        await world.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "LONG",
                                                 "qty": "0.001"})

    world.hooks.on("POST", ORDER, shrink)
    rec = await world.go(close_spec())
    assert rec.state is S.COMPLETED
    first, second = await world.orders.for_intent(rec.id)
    assert (first.state, first.error_code, first.qty) == (O.REJECTED, -2022, D("0.004"))  # 数量超出持仓
    assert (second.state, second.qty) == (O.FILLED, D("0.003"))  # 按新读数重发一次
    assert second.client_order_id != first.client_order_id
    assert await world.exchange_positions() == {}


async def test_a_dust_position_is_closed_with_a_take_profit_condition_the_exchange_triggers_later(world):
    world.disp.switches.update({"behaviors": {"triggered_order_link": "available", "push_algo_events": True}})
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED  # 0.002 BTC @ 50000
    world.feed.set_price(SYM, "20000")  # 价格大跌：名义金额 40，不足最小名义金额 50（没挂止损，不会被触发）
    rec = await world.go(close_spec())
    assert rec.state is S.ACTIVE  # 条件单挂着等触发
    order = await world.only_order(rec.id)
    assert (order.purpose, order.order_type, order.close_position) == (OrderPurpose.DUST_CLOSE,
                                                                     "TAKE_PROFIT_MARKET", True)
    assert order.trigger_price == D("20000.1") and order.state is O.ACKNOWLEDGED  # 最新价上方一个最小价位
    [algo] = await world.exchange_algos()
    assert (algo.algo_type, algo.side, algo.trigger_price) == (AlgoType.TAKE_PROFIT_MARKET, SELL, D("20000.1"))
    assert world.sent("POST", ORDER)[-1][3]["side"] == "BUY"  # 没有发过平仓的市价单
    world.feed.set_price(SYM, "20001")  # 价格越过触发价
    world.disp.tick()
    await world.settle()
    done = await world.intents.get(rec.id)
    assert done.state is S.COMPLETED and "碎仓条件单已触发并成交" in done.state_reason
    assert await world.exchange_positions() == {} and world.registry.get_active(SYM, LONG) is None


async def test_a_min_notional_rejection_on_a_close_switches_to_the_condition_order_path(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED

    async def crash_the_price(params, body):  # 读完持仓、检查过名义金额之后，价格在发单前跌了
        world.feed.set_price(SYM, "20000")

    world.hooks.on("POST", ORDER, crash_the_price)
    rec = await world.go(close_spec())
    assert rec.state is S.ACTIVE
    market, dust = await world.orders.for_intent(rec.id)
    assert (market.purpose, market.state, market.error_code) == (OrderPurpose.CLOSE, O.REJECTED, -4164)
    assert (dust.purpose, dust.state, dust.trigger_price) == (OrderPurpose.DUST_CLOSE, O.ACKNOWLEDGED, D("20000.1"))


async def test_a_close_whose_request_is_dropped_is_resent_under_a_new_id_after_three_empty_lookups(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    world.disp.switches.update({"order_timeout": {"enabled": True, "mode": "drop", "hang_s": 30, "count": 1}})
    rec = await world.go(close_spec())
    assert rec.state is S.RECONCILING
    for _ in range(2):
        world.clock.advance(6_000)
        await world.coordinator.resolve_unknown_orders()
        await world.settle()
        assert (await world.intents.get(rec.id)).state is S.RECONCILING  # 才查到不存在 1、2 次
    assert len(world.sent("POST", ORDER)) == 2
    world.clock.advance(6_000)
    await world.coordinator.resolve_unknown_orders()
    await world.settle()
    assert (await world.intents.get(rec.id)).state is S.COMPLETED
    first, second = await world.orders.for_intent(rec.id)
    assert first.state is O.REJECTED and first.client_order_id != second.client_order_id
    assert second.resend_of == first.client_order_id and (second.state, second.qty) == (O.FILLED, D("0.002"))
    assert len(world.sent("POST", ORDER)) == 3 and await world.exchange_positions() == {}


async def test_a_close_whose_response_is_lost_but_was_executed_is_not_resent(world):
    await world.start(stream=False)
    assert (await world.go(spec())).state is S.COMPLETED
    world.disp.switches.update({"order_timeout": {"enabled": True, "mode": "accept_then_hang", "hang_s": 30,
                                                  "count": 1}})
    rec = await world.go(close_spec())
    assert rec.state is S.RECONCILING and await world.exchange_positions() == {}  # 交易所其实已经平掉了
    world.clock.advance(6_000)
    await world.coordinator.resolve_unknown_orders()
    await world.settle()
    assert (await world.intents.get(rec.id)).state is S.COMPLETED
    assert len(world.sent("POST", ORDER)) == 2  # 开仓一次、平仓一次，没有重发


# ================================================================ 止损类意图


async def place_stop(world, trigger: str = "45000", side: PositionSide = LONG):
    rec = await world.go(stop_place_spec(trigger, side=side))
    assert rec.state is S.COMPLETED, rec.state_reason
    return await world.only_order(rec.id)


def old_of(order) -> dict[str, Any]:
    return {"client_algo_id": order.client_order_id, "algo_id": order.algo_id,
            "trigger_price": format(order.trigger_price, "f")}


async def test_a_stop_place_puts_a_whole_position_stop_on_the_exchange(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    order = await place_stop(world, "45000")
    assert order.state is O.ACKNOWLEDGED and order.algo_id and order.close_position
    [algo] = await world.exchange_algos()
    assert (algo.client_algo_id, algo.algo_id, algo.algo_type) == (order.client_order_id, order.algo_id,
                                                                  AlgoType.STOP_MARKET)
    assert (algo.side, algo.position_side, algo.trigger_price, algo.close_position, algo.qty) == (
        SELL, LONG, D("45000"), True, None)
    assert not world.coordinator.key_busy(SYM, LONG)  # 挂在交易所的止损单不算在途
    assert world.registry.get_active(SYM, LONG).stop_price is None  # 协调者不改保护状态，由保护单管理处理


async def test_a_triggered_system_stop_does_not_leave_its_intent_reopened_and_the_key_busy(world):
    """真实链路：替身把触发后的订单关联到系统止损单，成交按止损单的意图号入账，入账侧把已完成的 STOP_PLACE 重开；
    止损单已触发并成交，协调者把意图再完成一次，该键不再算有在途动作。"""
    world.disp.switches.update({"behaviors": {"triggered_order_link": "available", "push_algo_events": True}})
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    order = await place_stop(world, "45000")
    world.feed.set_price(SYM, "44000")
    world.disp.tick()  # 触发：止损单变成一张市价平仓单并成交
    await world.settle()
    await world.settle()
    [closing] = [f for f in await world.fills() if f.client_order_id == order.client_order_id]
    assert closing.intent_id == order.intent_id and closing.price == D("44000")
    assert (await world.orders.get(order.client_order_id)).state is O.FILLED
    states = [e.state for e in world.events if isinstance(e, IntentStateChanged) and e.intent_id == order.intent_id]
    assert states[-2:] == ["ACTIVE", "COMPLETED"]  # 被入账侧重开过，又被完成
    assert (await world.intents.get(order.intent_id)).state is S.COMPLETED
    assert not world.coordinator.key_busy(SYM, LONG)


async def test_a_stop_that_would_trigger_at_once_is_rejected_as_immediate_trigger(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    rec = await world.go(stop_place_spec("51000"))  # 多仓的止损触发价在最新价上方：建单就会触发
    assert rec.state is S.REJECTED and rec.state_reason == "immediate_trigger"
    assert await world.exchange_algos() == [] and (await world.exchange_positions())[(SYM, LONG)].qty == D("0.002")


async def test_a_second_whole_position_stop_on_the_same_leg_is_rejected_with_its_error_code(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    await place_stop(world, "45000")
    rec = await world.go(stop_place_spec("44000"))
    assert rec.state is S.REJECTED and "-4130" in rec.state_reason
    assert len(await world.exchange_algos()) == 1


async def test_a_stop_whose_creation_fails_is_rejected_with_the_code_or_looked_up_when_unknown(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    world.disp.switches.update({"algo_create_fail": {"enabled": True, "mode": "reject", "code": -2010, "count": 1}})
    rejected = await world.go(stop_place_spec("45000"))
    assert rejected.state is S.REJECTED and "-2010" in rejected.state_reason

    world.disp.switches.update({"algo_create_fail": {"enabled": True, "mode": "unknown", "count": 1}})
    unknown = await world.go(stop_place_spec("45000"))
    assert unknown.state is S.RECONCILING  # 503：结果未知，按编号回查条件单
    for _ in range(3):
        world.clock.advance(6_000)
        await world.coordinator.resolve_unknown_orders()
        await world.settle()
    ended = await world.intents.get(unknown.id)
    assert ended.state is S.REJECTED and await world.exchange_algos() == []  # 连续 3 次不存在


async def test_a_stop_whose_response_is_lost_is_found_by_its_id_and_completed(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    world.disp.switches.update({"order_timeout": {"enabled": True, "mode": "accept_then_hang", "scope": "algo",
                                                  "hang_s": 30, "count": 1}})
    rec = await world.go(stop_place_spec("45000"))
    assert rec.state is S.RECONCILING
    world.clock.advance(6_000)
    await world.coordinator.resolve_unknown_orders()
    await world.settle()
    assert (await world.intents.get(rec.id)).state is S.COMPLETED
    assert len(await world.exchange_algos()) == 1 and len(world.sent("POST", ALGO)) == 1  # 没有挂第二张


async def test_stop_replace_in_gap_mode_cancels_the_old_stop_then_places_the_new_one(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    old = await place_stop(world, "45000")
    rec = await world.go(stop_replace_spec("46000", old_of(old)))
    assert rec.state is S.COMPLETED
    result = rec.params["result"]
    assert (result["outcome"], result["mode"], result["stop_in_place"]) == ("replaced", "gap", True)
    [algo] = await world.exchange_algos()
    assert algo.trigger_price == D("46000") and algo.client_algo_id == result["client_algo_id"] != old.client_order_id
    assert (await world.orders.get(old.client_order_id)).state is O.CANCELLED


async def test_stop_replace_reports_gap_exceeded_when_the_new_stop_comes_too_late(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    old = await place_stop(world, "45000")

    async def lose_time(params, body):  # 旧单撤掉之后过了 31 秒才轮到新单
        world.clock.advance(31_000)

    world.hooks.on("DELETE", ALGO, lose_time, after=True)
    rec = await world.go(stop_replace_spec("46000", old_of(old)))
    result = rec.params["result"]
    assert rec.state is S.REJECTED and result["outcome"] == "gap_exceeded"
    assert result["stop_in_place"] is True and result["gap_ms"] >= 31_000  # 新单已在位，只是空窗超了
    assert [a.trigger_price for a in await world.exchange_algos()] == [D("46000")]


async def test_stop_replace_reports_stop_crossed_when_neither_price_can_be_placed(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    old = await place_stop(world, "45000")
    world.feed.set_price(SYM, "44000")  # 现价已在两个触发价之下，但替身没有 tick，旧单还没触发
    rec = await world.go(stop_replace_spec("46000", old_of(old)))
    result = rec.params["result"]
    assert rec.state is S.REJECTED and result["outcome"] == "stop_crossed" and result["stop_in_place"] is False
    assert await world.exchange_algos() == []  # 旧单已撤，新单旧价都被拒（−2021）：现在没有止损


async def test_stop_replace_restores_the_old_price_when_only_the_new_price_is_crossed(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    old = await place_stop(world, "45000")
    rec = await world.go(stop_replace_spec("51000", old_of(old)))  # 新价在现价上方：被拒，按旧价重挂
    result = rec.params["result"]
    assert rec.state is S.REJECTED and result["outcome"] == "restored_old" and result["stop_in_place"] is True
    [algo] = await world.exchange_algos()
    assert algo.trigger_price == D("45000") and algo.client_algo_id == result["client_algo_id"]


async def test_stop_replace_make_before_break_keeps_both_until_the_old_one_is_cancelled(world):
    world.disp.switches.update({"behaviors": {"dual_close_position_stop": "allow"}})
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    old = await place_stop(world, "45000")
    rec = await world.go(stop_replace_spec("46000", old_of(old), mode="make_before_break"))
    result = rec.params["result"]
    assert rec.state is S.COMPLETED and (result["outcome"], result["cleanup"]) == ("replaced", [])
    assert [a.trigger_price for a in await world.exchange_algos()] == [D("46000")]
    # 先挂新单、确认存在，才撤旧单
    methods = [(m, path) for m, path, *_ in world.transport.requests if path == ALGO and m in ("POST", "DELETE")]
    assert methods[-2:] == [("POST", ALGO), ("DELETE", ALGO)]


async def test_stop_replace_make_before_break_keeps_the_old_stop_when_the_exchange_refuses_a_second_one(world):
    await world.start()  # 替身默认：同向第二张全平型止损被 −4130 拒绝
    assert (await world.go(spec())).state is S.COMPLETED
    old = await place_stop(world, "45000")
    rec = await world.go(stop_replace_spec("46000", old_of(old), mode="make_before_break"))
    result = rec.params["result"]
    assert rec.state is S.REJECTED and result["outcome"] == "kept_old" and result["stop_in_place"] is True
    assert [a.trigger_price for a in await world.exchange_algos()] == [D("45000")]  # 旧单原样保留


async def test_stop_cancel_cancels_a_stop_and_a_missing_one_is_also_completed(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    old = await place_stop(world, "45000")
    spec_ = stop_cancel_spec(old.client_order_id, old.algo_id)
    first = await world.go(spec_)
    assert first.state is S.COMPLETED and await world.exchange_algos() == []
    assert (await world.orders.get(old.client_order_id)).state is O.CANCELLED
    second = await world.go(stop_cancel_spec(old.client_order_id, old.algo_id))  # 已经不在了
    assert second.state is S.COMPLETED


# ================================================================ 部分平仓


async def test_a_partial_close_pauses_the_position_until_the_order_ends_and_leaves_the_rest(world):
    await world.start()
    assert (await world.go(spec(params={"notional": 200}))).state is S.COMPLETED  # 0.004
    position_id = world.registry.get_active(SYM, LONG).id
    rec = await world.go(partial_spec("0.001"))
    assert rec.state is S.COMPLETED
    order = await world.only_order(rec.id)
    assert (order.purpose, order.side, order.qty) == (OrderPurpose.PARTIAL_CLOSE, SELL, D("0.001"))
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.003")
    held = world.registry.get_active(SYM, LONG)
    assert (held.id, held.qty, held.status) == (position_id, D("0.003"), PositionStatus.MANAGED)  # 暂停已解除
    closing = [e for e in world.events if type(e).__name__ == "PositionChanged" and e.cause.startswith("partial_close")]
    assert [e.cause for e in closing] == ["partial_close", "partial_close_resume"]


async def test_a_partial_close_asks_for_no_more_than_the_live_position(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED  # 0.002
    rec = await world.go(partial_spec("0.01"))  # 请求的比持仓还多：取即时持仓
    assert rec.state is S.COMPLETED and (await world.only_order(rec.id)).qty == D("0.002")
    assert await world.exchange_positions() == {}


# ================================================================ 撤单


async def test_cancel_unfilled_opens_cancels_the_resting_remainder_on_the_exchange(world):
    await world.start()
    world.disp.switches.update({"partial_fill": {"enabled": True, "ratio": "0.5", "remainder": "later",
                                                 "delay_s": 600, "count": 1}})
    rec = await world.go(spec(params={"notional": 200}))  # 0.004，成交一半，余量挂着
    assert rec.state is S.ACTIVE
    order = await world.only_order(rec.id)
    assert (order.state, order.filled_qty) == (O.PARTIALLY_FILLED, D("0.002"))
    assert await world.coordinator.open_intents_settled(0.05) is False  # 还有没结束的开仓意图
    assert await world.coordinator.cancel_unfilled_opens(model="m1", reason="模型切换") == 1
    await world.settle()
    done = await world.intents.get(rec.id)
    assert done.state is S.COMPLETED and "部分成交" in done.state_reason and "模型切换" in done.state_reason
    assert (await world.only_order(rec.id)).state is O.CANCELLED
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.002")  # 已成交的部分保留
    assert await world.coordinator.open_intents_settled(1.0) is True
    assert await world.coordinator.cancel_unfilled_opens(model="m1", reason="再来一次") == 0


async def test_an_order_cancel_intent_cancels_a_resting_open_order(world):
    await world.start()
    world.disp.switches.update({"partial_fill": {"enabled": True, "ratio": "0.5", "remainder": "later",
                                                 "delay_s": 600, "count": 1}})
    opened = await world.go(spec(params={"notional": 200}))
    order = await world.only_order(opened.id)
    cancel = await world.go(order_cancel_spec(order.client_order_id))
    assert cancel.state is S.COMPLETED
    assert (await world.orders.get(order.client_order_id)).state is O.CANCELLED
    assert (await world.intents.get(opened.id)).state is S.COMPLETED  # 开仓意图随订单作废而完成（有部分成交）


# ================================================================ T0.16b 后续改进：残留条件单、持仓变了的告警、停止后不能重启


async def test_a_dust_take_profit_left_on_the_exchange_is_cancelled_and_confirmed_gone_before_the_next_open(world):
    """碎仓平仓挂的全平型止盈单，在仓位被别的途径平掉之后残留在交易所，会作用到之后新开的仓上：开仓前先撤并确认不存在。"""
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED  # 0.002 BTC @ 50000
    world.feed.set_price(SYM, "20000")  # 价格大跌：名义金额 40 < 最小名义金额 50
    close = await world.go(close_spec())
    assert close.state is S.ACTIVE  # 全平型止盈条件单挂着等触发
    dust = await world.only_order(close.id)
    assert dust.purpose is OrderPurpose.DUST_CLOSE
    [resting] = await world.exchange_algos()
    assert (resting.algo_type, resting.close_position, resting.trigger_price) == (
        AlgoType.TAKE_PROFIT_MARKET, True, D("20000.1"))
    # 别的途径把仓位平掉了（替身对平仓单豁免最小名义金额，手动市价单才平得掉这个碎仓），止盈条件单还留在交易所
    world.disp.switches.update({"behaviors": {"close_min_notional_exempt": True}})
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "LONG", "qty": "0.002"})
    await world.settle()
    assert await world.exchange_positions() == {} and len(await world.exchange_algos()) == 1
    await world.refresh_limits()
    again = await world.go(spec())
    assert again.state is S.COMPLETED
    assert await world.exchange_algos() == []  # 先撤掉残留的止盈单，再开仓
    assert len(world.sent("DELETE", ALGO)) == 1
    assert (await world.orders.get(dust.client_order_id)).state is O.CANCELLED  # 撤单结果记进了那张条件单的订单行
    assert (await world.intents.get(close.id)).state is S.REJECTED  # 碎仓意图随之结束：条件单被撤销，没有成交
    # 新仓没有被残留的止盈单平掉
    world.feed.set_price(SYM, "20001")
    world.disp.tick()
    await world.settle()
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.005")  # 100 / 20000


async def test_a_users_whole_position_take_profit_makes_the_open_give_up_and_is_left_alone(world):
    await world.start()
    await world.manual("/dev/manual/algo_order", {"symbol": SYM, "side": "SELL", "position_side": "LONG",
                                                  "type": "TAKE_PROFIT_MARKET", "trigger_price": "60000",
                                                  "close_position": True})
    rec = await world.go(spec())
    assert rec.state is S.REJECTED
    assert "用户自己的" in rec.state_reason and "TAKE_PROFIT_MARKET" in rec.state_reason  # 留痕：谁的单、什么类型
    assert world.sent("POST", ORDER) == [] and world.sent("DELETE", ALGO) == []  # 不替用户撤
    [algo] = await world.exchange_algos()
    assert (algo.algo_type, algo.close_position, algo.is_open) == (AlgoType.TAKE_PROFIT_MARKET, True, True)
    assert (await world.go(spec(symbol="ETHUSDT"))).state is S.COMPLETED  # 别的合约不受影响


async def test_a_users_take_profit_with_a_quantity_does_not_block_the_open_and_is_left_alone(world):
    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED  # 先开平一次：杠杆与保证金模式设成配置的样子（否则有挂单就改不了）
    assert (await world.go(close_spec())).state is S.COMPLETED
    await world.manual("/dev/manual/algo_order", {"symbol": SYM, "side": "SELL", "position_side": "LONG",
                                                  "type": "TAKE_PROFIT_MARKET", "trigger_price": "60000",
                                                  "qty": "0.001"})  # 带数量的，不是全平型
    await world.refresh_limits()
    rec = await world.go(spec())
    assert rec.state is S.COMPLETED, rec.state_reason
    assert world.sent("DELETE", ALGO) == []
    [algo] = await world.exchange_algos()
    assert (algo.close_position, algo.qty) == (False, D("0.001"))


async def test_a_close_whose_request_was_dropped_is_held_with_one_warning_when_the_position_changed_meanwhile(world):
    from dmr_kernel.core.types import AlertLevel

    await world.start()
    assert (await world.go(spec(params={"notional": 200}))).state is S.COMPLETED  # 0.004
    world.disp.switches.update({"order_timeout": {"enabled": True, "mode": "drop", "hang_s": 30, "count": 1}})
    rec = await world.go(close_spec())
    assert rec.state is S.RECONCILING  # 请求没有到交易所
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "LONG", "qty": "0.001"})
    await world.settle()  # 有人手动减了 0.001：持仓从 0.004 变成 0.003
    for _ in range(5):
        world.clock.advance(6_000)
        await world.coordinator.resolve_unknown_orders()
        await world.settle()
    held = await world.intents.get(rec.id)
    assert held.state is S.RECONCILING and world.coordinator.key_busy(SYM, LONG)  # 按规格不重发，意图保持 RECONCILING
    assert len(world.sent("POST", ORDER)) == 2  # 开仓一次、被丢掉的平仓一次
    [warning] = world.alerts("close_unknown_position_changed")  # 只发一次
    assert warning["level"] is AlertLevel.WARNING and str(rec.id) in warning["dedupe_key"]
    assert "0.003" in warning["message"] and "0.004" in warning["message"] and "人工核对" in warning["message"]
    # 人工核对后把剩下的平掉，意图自行了结，不再有新告警
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "LONG", "qty": "0.003"})
    await world.settle()
    world.clock.advance(6_000)
    await world.coordinator.resolve_unknown_orders()
    await world.settle()
    done = await world.intents.get(rec.id)
    assert done.state is S.COMPLETED and "已由其它途径平掉" in done.state_reason
    assert not world.coordinator.key_busy(SYM, LONG) and len(world.alerts("close_unknown_position_changed")) == 1


async def test_a_stopped_coordinator_refuses_to_start_again_and_a_rebuilt_one_takes_over(world):
    from dmr_kernel.core.coordinator import CoordinatorStopped

    await world.start()
    assert (await world.go(spec())).state is S.COMPLETED
    await world.coordinator.stop(1.0)
    with pytest.raises(RuntimeError, match="协调者停止后不能重新启动，请重建实例"):
        await world.coordinator.start()
    with pytest.raises(CoordinatorStopped):  # 悄悄返回的话，它既不受理也不回查，却看起来已启动
        await world.coordinator.submit(spec(symbol="ETHUSDT"))
    await world.kill()
    again = world.restart()  # 按提示重建
    await again.start()
    rec = await again.go(spec(symbol="ETHUSDT"))
    assert rec.state is S.COMPLETED
    assert set(await again.exchange_positions()) == {(SYM, LONG), ("ETHUSDT", LONG)}
