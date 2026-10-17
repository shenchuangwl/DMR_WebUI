"""保护单管理对进程内替身交易所（T0.18）：真实账本、真实的意图/订单/登记簿/入账服务、真实的协调者与行情模块、真实的适配层与
账户数据流，只有风控闸与通知器是假的。全部走 ``tests/support/inproc.py``：不开套接字，替身与被测对象共用一个 ManualClock。

行情用 StaticFeed 设定的 15 分钟 K 线（中心价 50000、半幅 500：每根真实波幅与 ATR 都是 1000）：
多仓初始止损 = 最近 6 根最低价 49500 − ATR 1000 = 48500，离基准价 50000 正好 3%（在 A=1%…B=5% 之间）；
开仓之后的 K 线中心价每根 +250：窗口 0 最低价 49750、止损 48750。

涵盖报告 4.11 的通过标准：从成交到止损存在目标 5 秒、最迟 30 秒；−2021 的两种交易所行为；止损建单失败故障下策略仓在重试窗口后被
市价平掉、接管仓转无保护并置抑制与最高级告警；手动撤掉系统止损后 90 秒内重新存在；移动止损的两种更新方式；崩溃点。
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.crashpoints import CrashRegistry
from dmr_kernel.core.events import EventBus, OrdersSnapshotTaken
from dmr_kernel.core.ingest import Ingest
from dmr_kernel.core.intents import IntentService, IntentSpec
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.protection import Protection
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    AlertLevel,
    AlgoType,
    ExitReason,
    Gate,
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    OrderSide,
    PositionSide,
    ProtectionState,
    RiskDecision,
    Suppression,
)
from dmr_kernel.exchange.rules import InstrumentRules
from dmr_kernel.exchange.standin import StandinAdapter
from dmr_kernel.marketdata.candles import CandleService
from dmr_kernel.marketdata.clock import ServerClock
from tests.support.inproc import AUTH_HEADER, T0_MS, make_stack
from tests.support.ledger import open_ledger

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
S = IntentState
SYM = "BTCUSDT"
TF = "15m"
TF_MS = 900_000
ORDER, ALGO = "/api/private/order", "/api/private/algo_order"
CENTER, HALF = 50_000.0, 500.0
STEP_MS = 10_000  # 默认窗口 30 秒 / 3 次重试


class FakeRiskGate:
    async def evaluate(self, *, model, action, now_ms) -> RiskDecision:
        return RiskDecision(True)


class FakeNotifier:
    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def raise_alert(self, level, kind, message, *, dedupe_key=None) -> None:
        self.alerts.append({"level": level, "kind": kind, "message": message, "dedupe_key": dedupe_key})

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [a for a in self.alerts if a["kind"] == kind]


class HookTransport:
    """包在传输外面：匹配的请求先（或后）交给钩子——可以趁机去改交易所那一侧的状态。"""

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


def bar_rows(centers: list[float], start_ms: int, half: float = HALF) -> list[list[Any]]:
    return [[start_ms + i * TF_MS, c, c + half, c - half, c, 1.0, start_ms + (i + 1) * TF_MS - 1]
            for i, c in enumerate(centers)]


class World:
    def __init__(self, tmp_path, *, stack=None, ledger=None, live: bool = False, **config: Any) -> None:
        if stack is None:
            stack = make_stack(config={"trade_notional_usdt": D("100"), **config}, request_timeout_s=0.05)
            ledger = open_ledger(tmp_path, stack.clock)
        self.stack, self.ledger = stack, ledger
        self.clock, self.disp, self.feed = stack.clock, stack.disp, stack.feed
        self.config, self.cap, self.transport = stack.config, stack.cap, stack.transport
        if not isinstance(self.transport.inner, HookTransport):
            self.transport.inner = HookTransport(self.transport.inner)
        self.hooks: HookTransport = self.transport.inner
        self.bus = EventBus()
        self.events: list = []
        self.bus.subscribe(None, self.events.append, name="world.collect")
        self.adapter = StandinAdapter(stack.transport, capability=stack.cap, bus=self.bus, clock=self.clock,
                                      config_get=self.config.__getitem__, request_timeout_s=0.05)
        self.notifier = FakeNotifier()
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
        self.coordinator = Coordinator(
            adapter=self.adapter, capability=self.cap, ledger=ledger, bus=self.bus, clock=self.clock, config_get=cfg,
            modelstate=self.modelstate, registry=self.registry, intents=self.intents, orders=self.orders,
            riskgate=FakeRiskGate(), limits=self.limits, rules=self.rules, notifier=self.notifier, crash=self.crash)
        self.server_clock = ServerClock(self.clock)
        self.candles = CandleService(self.adapter, ledger, self.bus, self.clock, self.server_clock, config_get=cfg)
        self.protection = Protection(
            coordinator=self.coordinator, registry=self.registry, modelstate=self.modelstate, candles=self.candles,
            rules=self.rules, adapter=self.adapter, ledger=ledger, bus=self.bus, clock=self.clock, config_get=cfg,
            notifier=self.notifier, crash=self.crash, limits=self.limits, wait_s=2.0)
        self.stream = self.adapter.account_stream()

    # ------------------------------------------------------------ 行情

    def set_bars(self, after: list[float] | None = None, *, history: int = 40) -> None:
        """历史（在开仓时刻 T0 收盘）+ 开仓所在的那根 + 开仓之后的 K 线；之后推进时钟再 refresh 才会看到后面的 K 线。"""
        centers = [CENTER] * history + [CENTER] + list(after or [])
        self.feed.set_candles(SYM, TF, bar_rows(centers, T0_MS - history * TF_MS))

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
        assert await self.candles.sync_server_time()
        await self.candles.refresh([SYM], TF)
        await self.protection.start()
        await self.settle()
        return self

    async def kill(self) -> None:
        await self.stream.stop()
        await self.bus.stop()

    def restart(self) -> "World":
        return World(None, stack=self.stack, ledger=self.ledger)

    async def settle(self, rounds: int = 4) -> None:
        for _ in range(rounds):
            for _ in range(6):
                await asyncio.sleep(0)
            await self.coordinator.idle(10.0)
            await self.bus.drain(10.0)
            await self.protection.idle(10.0)

    async def refresh_limits(self) -> None:
        now = self.clock.now_ms()
        positions = await self.adapter.fetch_positions()
        orders = await self.adapter.fetch_open_orders()
        algos = await self.adapter.fetch_open_algo_orders()
        assert all(isinstance(r, Known) for r in (positions, orders, algos))
        self.limits.update_positions(positions.value, now)
        self.limits.update_orders(orders.value, algos.value, now)

    async def tick(self, ms: int = 0) -> None:
        """推进时钟，协调者回查一轮，保护单管理驱动一轮（调度器会每隔几秒做的事），等一切安静。"""
        if ms:
            self.clock.advance(ms)
        await self.coordinator.resolve_unknown_orders()
        await self.protection.tick()
        await self.settle()

    async def wait_for(self, predicate, timeout: float = 5.0) -> float:
        """让出事件循环直到条件成立（真实时间），返回等了多久；超时就失败。"""
        loop = asyncio.get_running_loop()
        start = loop.time()
        while not await predicate():
            if loop.time() - start > timeout:
                raise AssertionError("等待条件超时")
            await asyncio.sleep(0.002)
        return loop.time() - start

    # ------------------------------------------------------------ 读与操作

    async def open_long(self) -> int:
        result = await self.coordinator.submit(open_spec())
        return result.intent_id

    async def exchange_algos(self) -> list:
        result = await self.adapter.fetch_open_algo_orders(SYM)
        assert isinstance(result, Known), result
        return list(result.value)

    async def exchange_positions(self) -> dict:
        result = await self.adapter.fetch_positions()
        assert isinstance(result, Known), result
        return {(p.symbol, p.side): p for p in result.value}

    async def manual(self, path: str, body: dict) -> Any:
        status, payload, _ = await self.disp.handle("POST", path, {}, json.dumps(body).encode(),
                                                    {AUTH_HEADER: self.stack.manual_token})
        assert status == 200, payload
        return payload

    async def snapshot(self) -> None:
        """像条件单对账那样：读回挂单与条件单，发布 OrdersSnapshotTaken。"""
        orders = await self.adapter.fetch_open_orders()
        algos = await self.adapter.fetch_open_algo_orders()
        assert isinstance(orders, Known) and isinstance(algos, Known)
        self.bus.publish(OrdersSnapshotTaken(open_orders=tuple(orders.value), algo_orders=tuple(algos.value),
                                             at_ms=self.clock.now_ms()))
        await self.settle()

    def sent(self, method: str, path: str) -> list:
        return self.transport.sent(method, path)

    def position(self, side: PositionSide = LONG):
        return self.registry.get_active(SYM, side)

    async def row(self, position_id: int):
        rec = await self.registry.fetch(position_id)
        assert rec is not None
        return rec

    def suppressions(self) -> frozenset:
        return self.modelstate.current().suppressions

    def close(self) -> None:
        self.ledger.close_sync()


_seq = 0


def open_spec(**kw: Any) -> IntentSpec:
    global _seq
    _seq += 1
    fields: dict[str, Any] = dict(action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol=SYM,
                                  side=LONG, idem_key=f"pt:{_seq}", ttl_s=60)
    fields.update(kw)
    return IntentSpec(**fields)


def close_spec(**kw: Any) -> IntentSpec:
    return open_spec(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR, ttl_s=None, **kw)


@pytest.fixture
def make_world(tmp_path):
    made: list[World] = []

    def factory(**kw: Any) -> World:
        world = World(tmp_path, **kw)
        world.set_bars()
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


async def stop_exists(world: World) -> bool:
    return len(await world.exchange_algos()) > 0


async def take_over_manually(world: World, qty: str = "0.002"):
    """用户在网页上手动开了一个仓，被登记为接管仓（模型 2 的自动接管本轮还没有，这里直接登记）。"""
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "BUY", "position_side": "LONG", "qty": qty})
    await world.settle()
    now = world.clock.now_ms()
    return await world.registry.begin_takeover(
        symbol=SYM, side=LONG, qty=D(qty), avg_price=D("50000"), open_time_ms=now, open_time_estimated=True,
        anchor_ms=now, signal_exit=False)


# ================================================================ 从成交到止损存在：目标 5 秒，最迟 30 秒


async def test_the_stop_exists_within_the_five_second_target_after_the_fill(world):
    await world.start()
    intent_id = await world.open_long()
    waited = await world.wait_for(lambda: stop_exists(world), 5.0)  # 真实时间的短时限
    assert waited < 5.0
    await world.settle()
    (stop,) = await world.exchange_algos()
    assert stop.algo_type is AlgoType.STOP_MARKET and stop.close_position is True and stop.qty is None
    assert stop.position_side is LONG and stop.side is OrderSide.SELL and stop.trigger_price == D("48500")
    assert stop.client_algo_id.startswith("DK")
    pos = world.position()
    row = await world.row(pos.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("48500")
    assert (row.stop_client_id, row.stop_algo_id) == (stop.client_algo_id, stop.algo_id)
    assert world.clock.now_ms() - row.open_time_ms <= 30_000  # 最迟 30 秒（时钟没动：同一时刻）
    assert Suppression.UNPROTECTED not in world.suppressions() and len(world.sent("POST", ALGO)) == 1
    assert (await world.intents.get(intent_id)).state is S.COMPLETED


async def test_with_one_transient_failure_the_stop_still_exists_within_thirty_seconds(world):
    await world.start()
    world.disp.switches.update({"algo_create_fail": {"enabled": True, "mode": "reject", "code": -2010, "count": 1}})
    await world.open_long()
    await world.settle()
    pos = world.position()
    assert await world.exchange_algos() == []  # 第一次被拒
    assert (await world.row(pos.id)).protection_state is ProtectionState.REBUILDING
    assert Suppression.UNPROTECTED not in world.suppressions()  # 重试窗口里不算无保护
    opened_at = world.clock.now_ms()
    await world.tick(STEP_MS)  # 第一次重试
    (stop,) = await world.exchange_algos()
    assert world.clock.now_ms() - opened_at <= 30_000 and stop.trigger_price == D("48500")
    assert (await world.row(pos.id)).protection_state is ProtectionState.SYSTEM


# ================================================================ −2021 的两种交易所行为


async def _price_drops_below_the_stop(world: World) -> None:
    async def drop(params, body):  # 止损建单请求到达交易所之前，价格跌到了止损价之下
        world.feed.set_price(SYM, "48000")

    world.hooks.on("POST", ALGO, drop)


async def test_reject_2021_makes_the_protection_market_close_a_strategy_position_and_cool_it_down(world):
    await world.start()
    await _price_drops_below_the_stop(world)
    await world.open_long()
    await world.settle()
    assert await world.exchange_positions() == {}  # 保护性平仓平掉了
    assert len(world.sent("POST", ALGO)) == 1  # 不重试
    assert len(world.sent("POST", ORDER)) == 2  # 开仓 + 保护性平仓
    assert await world.exchange_algos() == []
    assert world.registry.cooldown_until(SYM) == T0_MS + 4 * TF_MS  # 冷却 cooldown_bars 根 K 线
    closed = [e for e in world.events if type(e).__name__ == "PositionClosed"]
    assert len(closed) == 1 and closed[0].exit_reason == ExitReason.PROTECTIVE.value
    assert world.position() is None and Suppression.UNPROTECTED not in world.suppressions()


async def test_trigger_immediately_means_the_stop_fills_at_once_and_the_exit_is_booked_as_a_stop(world):
    world.disp.switches.update({"behaviors": {"stop_crossed_on_create": "trigger_immediately",
                                              "triggered_order_link": "available", "push_algo_events": True}})
    await world.start()
    await _price_drops_below_the_stop(world)
    await world.open_long()
    await world.settle()
    assert await world.exchange_positions() == {} and world.position() is None
    assert len(world.sent("POST", ORDER)) == 1  # 没有保护性平仓：止损单受理后马上成交
    assert await world.exchange_algos() == []  # 触发后的条件单是 FINISHED，没有残留
    closed = [e for e in world.events if type(e).__name__ == "PositionClosed"]
    assert len(closed) == 1 and closed[0].exit_reason == ExitReason.STOP.value  # 按止损出场处理
    assert world.registry.cooldown_until(SYM) is None and Suppression.UNPROTECTED not in world.suppressions()


async def test_trigger_immediately_without_linked_orders_still_ends_flat_with_nothing_left_behind(world):
    """触发后订单号不可关联（默认的保守取值，实测第 12 项）：登记簿靠「系统保护、有止损价、成交价已越过」推定是止损触发。
    止损单受理后马上成交时，成交回报可能先于保护单管理记下「系统保护」到达，出场原因因此可能记成外部；
    无论哪种，仓位按成交归零、没有保护性平仓、没有残留的条件单。"""
    world.disp.switches.update({"behaviors": {"stop_crossed_on_create": "trigger_immediately"}})
    await world.start()
    await _price_drops_below_the_stop(world)
    await world.open_long()
    await world.settle()
    assert await world.exchange_positions() == {} and world.position() is None
    assert len(world.sent("POST", ORDER)) == 1 and await world.exchange_algos() == []
    closed = [e for e in world.events if type(e).__name__ == "PositionClosed"]
    assert len(closed) == 1 and closed[0].exit_reason in (ExitReason.STOP.value, ExitReason.EXTERNAL.value)
    assert Suppression.UNPROTECTED not in world.suppressions()


# ================================================================ 止损建单失败（algo_create_fail）：策略仓平掉，接管仓转无保护


async def test_when_stops_cannot_be_created_a_strategy_position_is_market_closed_after_the_retry_window(world):
    await world.start()
    world.disp.switches.update({"algo_create_fail": {"enabled": True, "mode": "reject", "code": -2010}})
    await world.open_long()
    await world.settle()
    opened_at = world.clock.now_ms()
    for _ in range(3):
        assert (await world.exchange_positions()).get((SYM, LONG)) is not None  # 窗口内仓还在
        await world.tick(STEP_MS)
    elapsed = world.clock.now_ms() - opened_at
    assert elapsed <= 30_000 + 15_000  # 4.11：protect_retry_window_s + close_one_timeout_s
    assert await world.exchange_positions() == {} and world.position() is None
    assert len(world.sent("POST", ALGO)) == 4  # 第 1 次尝试 + 3 次重试
    assert world.registry.cooldown_until(SYM) is not None
    assert Suppression.UNPROTECTED not in world.suppressions()  # 仓已经没了，抑制随之解除


async def test_when_stops_cannot_be_created_a_takeover_position_goes_unprotected_with_suppression_and_a_critical_alert(world):
    await world.start()
    world.disp.switches.update({"algo_create_fail": {"enabled": True, "mode": "reject", "code": -2010}})
    pos = await take_over_manually(world)
    await world.settle()
    started = world.clock.now_ms()
    for _ in range(3):
        await world.tick(STEP_MS)
    assert world.clock.now_ms() - started <= 30_000  # 4.11：protect_retry_window_s 秒内转无保护
    row = await world.row(pos.id)
    assert row.protection_state is ProtectionState.NONE and "无保护" in row.stop_note
    assert Suppression.UNPROTECTED in world.suppressions()
    (alert,) = world.notifier.of("protect_unprotected")
    assert alert["level"] is AlertLevel.CRITICAL
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.002")  # 接管仓不自动平
    assert world.sent("POST", ORDER) == []  # 系统没有发过任何市价单（用户手动的那一笔不经适配层）
    attempts = len(world.sent("POST", ALGO))
    await world.tick(STEP_MS)  # 之后持续重试
    assert len(world.sent("POST", ALGO)) == attempts + 1
    world.disp.switches.update({"algo_create_fail": {"enabled": False}})  # 故障排除
    await world.tick(STEP_MS)
    row = await world.row(pos.id)
    assert row.protection_state is ProtectionState.SYSTEM and Suppression.UNPROTECTED not in world.suppressions()
    assert len(await world.exchange_algos()) == 1


# ================================================================ 手动撤掉系统止损：90 秒内重新存在


async def _protected_position(world: World):
    await world.start()
    await world.open_long()
    await world.wait_for(lambda: stop_exists(world))
    await world.settle()
    pos = world.position()
    return pos, await world.row(pos.id)


async def test_a_stop_cancelled_by_hand_is_back_within_ninety_seconds_found_by_the_order_snapshot(world):
    pos, before = await _protected_position(world)
    cancelled_at = world.clock.now_ms()
    await world.manual("/dev/manual/cancel", {"symbol": SYM, "client_algo_id": before.stop_client_id})
    assert await world.exchange_algos() == []
    world.clock.advance(60_000)  # 条件单对账周期 60 秒：推送没打开，只能靠对账快照发现
    await world.snapshot()
    (stop,) = await world.exchange_algos()
    assert world.clock.now_ms() - cancelled_at <= 90_000
    row = await world.row(pos.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == before.stop_price == D("48500")
    assert row.stop_algo_id == stop.algo_id != before.stop_algo_id
    assert world.notifier.of("stop_missing_rebuilt")


async def test_a_stop_cancelled_by_hand_is_rebuilt_at_once_when_the_stream_pushes_algo_events(make_world):
    world = make_world()
    world.disp.switches.update({"behaviors": {"push_algo_events": True}})
    pos, before = await _protected_position(world)
    await world.manual("/dev/manual/cancel", {"symbol": SYM, "client_algo_id": before.stop_client_id})
    await world.wait_for(lambda: stop_exists(world))  # 不需要对账快照，也不需要推进时钟
    await world.settle()
    row = await world.row(pos.id)
    assert row.stop_algo_id != before.stop_algo_id and row.stop_price == D("48500")
    assert len(await world.exchange_algos()) == 1 and len(world.sent("POST", ALGO)) == 2


# ================================================================ 移动止损：两种更新方式


async def _after_six_bars(world: World, price: str = "51000"):
    """开仓并保护；之后过了 7 根 K 线（105 分钟），其中开仓之后的 6 根已收盘、中心价每根 +250。"""
    pos, before = await _protected_position(world)
    after = [CENTER + 250 * k for k in range(1, 7)]
    world.set_bars(after)
    world.clock.advance(7 * TF_MS)
    await world.candles.refresh([SYM], TF)
    world.feed.set_price(SYM, price)
    return pos, before


async def test_trailing_in_gap_mode_cancels_the_old_stop_and_then_places_the_new_one(world):
    pos, before = await _after_six_bars(world)
    assert before.stop_price == D("48500")
    await world.protection.trailing(T0_MS + 6 * TF_MS)
    await world.settle()
    (stop,) = await world.exchange_algos()
    assert stop.trigger_price == D("48750") and stop.algo_id != before.stop_algo_id
    row = await world.row(pos.id)
    assert row.stop_price == D("48750") and row.stop_cycle == 1 and row.protection_state is ProtectionState.SYSTEM
    posts, deletes = world.sent("POST", ALGO), world.sent("DELETE", ALGO)
    assert len(posts) == 2 and len(deletes) == 1  # 初始止损 + 新止损；撤了旧的
    order = [r[0] for r in world.transport.requests if r[1] == ALGO]
    assert order == ["POST", "DELETE", "POST"]  # gap：先撤旧、后挂新


async def test_trailing_with_make_before_break_places_the_new_stop_first_and_then_cancels_the_old(make_world):
    world = make_world(ex_stop_update_mode="make_before_break")
    world.disp.switches.update({"behaviors": {"dual_close_position_stop": "allow"}})  # 同向允许并存两张全平型止损
    pos, before = await _after_six_bars(world)
    both: list[int] = []

    async def count(params, body):  # 撤旧单的请求到达时：新旧两张并存
        both.append(len(world.disp.engine.open_algo_orders(SYM)))

    world.hooks.on("DELETE", ALGO, count)
    await world.protection.trailing(T0_MS + 6 * TF_MS)
    await world.settle()
    assert both == [2]
    (stop,) = await world.exchange_algos()
    assert stop.trigger_price == D("48750")
    order = [r[0] for r in world.transport.requests if r[1] == ALGO]
    assert order == ["POST", "POST", "DELETE"]  # make_before_break：先挂新、确认存在后撤旧
    row = await world.row(pos.id)
    assert row.stop_price == D("48750") and row.stale_stops == [] and row.stop_cycle == 1


async def test_trailing_does_not_move_a_stop_that_the_price_has_already_crossed(world):
    pos, before = await _after_six_bars(world, price="48700")  # 现价已低于候选止损价 48750
    await world.protection.trailing(T0_MS + 6 * TF_MS)
    await world.settle()
    (stop,) = await world.exchange_algos()
    assert stop.algo_id == before.stop_algo_id and stop.trigger_price == D("48500")  # 旧止损保留
    assert (await world.row(pos.id)).stop_cycle == 1  # 周期计数照样前进


# ================================================================ 崩溃点与重启


async def test_a_crash_between_the_fill_and_the_stop_is_repaired_after_a_restart(world):
    await world.start()
    world.crash.arm("open_filled_before_stop")
    await world.open_long()
    await world.settle()
    assert world.protection.crashed is not None and await world.exchange_algos() == []
    pos = world.position()
    assert pos is not None and pos.protection_state is not ProtectionState.SYSTEM
    await world.kill()
    reborn = world.restart()
    await reborn.start()
    await reborn.tick()  # 已登记而无止损的仓补上（F9 第 5 步）
    (stop,) = await reborn.exchange_algos()
    row = await reborn.row(pos.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_algo_id == stop.algo_id
    assert len(reborn.sent("POST", ALGO)) == 1  # 只有一张，没有补出第二张


async def test_closing_the_position_cleans_up_the_stop_and_a_crash_before_cleanup_is_repaired(world):
    pos, before = await _protected_position(world)
    world.crash.arm("close_filled_before_cleanup")
    result = await world.coordinator.submit(close_spec(position_id=pos.id))
    await world.coordinator.wait_terminal(result.intent_id, 5.0)
    await world.settle()
    assert world.position() is None and await world.exchange_positions() == {}
    assert world.protection.crashed is not None
    assert len(await world.exchange_algos()) == 1  # 清理之前被杀：止损还挂在零仓上
    assert (await world.row(pos.id)).stop_algo_id == before.stop_algo_id
    await world.kill()
    reborn = world.restart()
    await reborn.start()
    await reborn.protection.cleanup_after_close(pos.id)
    assert await reborn.exchange_algos() == []
    assert (await reborn.row(pos.id)).stop_algo_id is None


async def test_a_normal_close_cleans_the_stop_up_through_the_closed_event(world):
    pos, before = await _protected_position(world)
    result = await world.coordinator.submit(close_spec(position_id=pos.id))
    await world.coordinator.wait_terminal(result.intent_id, 5.0)
    await world.settle()
    assert world.position() is None and await world.exchange_algos() == []
    assert (await world.row(pos.id)).stop_algo_id is None


# ================================================================ 外部保护与交给系统管理止损（F3 第 6 步、F13）


async def _manual_position_with_users_stop(world: World, trigger: str = "45000"):
    """用户在网页上手动开仓、手动挂止损；随后仓位被登记为接管仓。返回（接管仓记录, 用户的条件单）。"""
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "BUY", "position_side": "LONG", "qty": "0.002"})
    await world.manual("/dev/manual/algo_order", {
        "symbol": SYM, "side": "SELL", "position_side": "LONG", "type": "STOP_MARKET", "trigger_price": trigger,
        "working_type": "CONTRACT_PRICE", "close_position": True, "qty": None})
    await world.settle()
    (user,) = await world.exchange_algos()
    assert not user.client_algo_id.startswith("DK")  # 用户的单没有系统前缀
    now = world.clock.now_ms()
    pos = await world.registry.begin_takeover(
        symbol=SYM, side=LONG, qty=D("0.002"), avg_price=D("50000"), open_time_ms=now, open_time_estimated=True,
        anchor_ms=now, signal_exit=False)
    await world.settle()
    return pos, user


async def test_a_users_own_stop_is_external_protection_and_the_system_steps_in_when_it_is_cancelled(world):
    await world.start()
    pos, user = await _manual_position_with_users_stop(world)
    row = await world.row(pos.id)
    assert row.protection_state is ProtectionState.EXTERNAL and row.status.value == "managed"
    assert world.sent("POST", ALGO) == [] and Suppression.UNPROTECTED not in world.suppressions()  # 系统不另挂
    await world.snapshot()  # 每次对账快照都复核：仍然有效
    assert (await world.row(pos.id)).protection_state is ProtectionState.EXTERNAL
    await world.manual("/dev/manual/cancel", {"symbol": SYM, "algo_id": user.algo_id})  # 用户撤掉了自己的止损
    await world.snapshot()
    (ours,) = await world.exchange_algos()
    assert ours.client_algo_id.startswith("DK") and ours.trigger_price == D("48500")
    row = await world.row(pos.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_algo_id == ours.algo_id
    assert world.notifier.of("external_protection_lost")[0]["level"] is AlertLevel.WARNING


async def test_handing_the_users_stop_to_the_system_replaces_it_with_the_systems_own(world):
    await world.start()
    pos, user = await _manual_position_with_users_stop(world)
    assert (await world.protection.hand_stop_to_system(pos.id)) == "protected"
    await world.settle()
    (ours,) = await world.exchange_algos()  # 用户的止损撤掉了，只剩系统的
    assert ours.client_algo_id.startswith("DK") and ours.algo_id != user.algo_id and ours.trigger_price == D("48500")
    row = await world.row(pos.id)
    assert row.protection_state is ProtectionState.SYSTEM and (row.stop_client_id, row.stop_algo_id) == (
        ours.client_algo_id, ours.algo_id)
    order = [r[0] for r in world.transport.requests if r[1] == ALGO]
    assert order == ["DELETE", "POST"]  # 默认 gap 方式：同向不允许并存，先撤用户的、后挂系统的
