"""重启恢复（T0.20）对进程内替身交易所：真实账本、真实的意图/订单/登记簿/入账服务、真实的协调者、保护单管理、对账、周期复核
（守卫）、行情模块、操作记录、真实的适配层与账户数据流，只有风控闸与通知器是假的。全部走 ``tests/support/inproc.py``：不开套接字，
替身与被测对象共用一个 ManualClock。

「杀进程」= 进程内崩溃点（CrashRegistry.arm + CrashSignal）或直接丢弃整个世界实例（不走关停）；「重启」= 用同一个账本文件与同一个
替身交易所重新搭一个世界，再跑 ``Recovery.run()``。时钟不动时恢复的耗时是 0 毫秒，所以「30 秒内」用 ManualClock 量。

行情用 StaticFeed 设定的 15 分钟 K 线（中心价 50000、半幅 500：每根真实波幅与 ATR 都是 1000）：多仓初始止损 = 48500。

覆盖：中断点 1、2、3、5；停机期间止损触发（补记为止损出场并清理残留保护单）；停机期间的外部手动开仓（待确认、不自动接管；
其上有系统前缀止损的标「来源待核对」且止损保留）；中断的一键全平（记为中断、清操作锁、不续做）；已有止损而登记簿没记住时认领、不补第二张。
另外覆盖中断点 4（部分平仓单已发出，回执未到；第 3、6 步）：协调者内存里的「暂停到期恢复」记录随进程丢了，仓位由恢复放回托管中——
订单回查得到就在恢复里放回；回查不到（请求没到交易所）就等协调者后来判完意图再放回，部分平仓永不重发。
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.crashpoints import CrashRegistry, CrashSignal
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.guard import Guard, LegacyMutex, legacy_runtime_dir
from dmr_kernel.core.ingest import Ingest
from dmr_kernel.core.intents import IntentService, IntentSpec
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.protection import Protection
from dmr_kernel.core.reconcile import Reconciler
from dmr_kernel.core.recovery import Recovery
from dmr_kernel.core.registry import PRESUMED_STOP_NOTE, Registry
from dmr_kernel.core.types import (
    AlertLevel,
    ExitReason,
    Gate,
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    OpLock,
    OperationStatus,
    OperationType,
    OrderState,
    Ownership,
    PendingReason,
    PositionSide,
    PositionStatus,
    ProtectionState,
    RiskDecision,
    Suppression,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.rules import InstrumentRules
from dmr_kernel.exchange.standin import StandinAdapter
from dmr_kernel.marketdata.candles import CandleService
from dmr_kernel.marketdata.clock import ServerClock
from dmr_kernel.ops.operations import Operations
from dmr_kernel.settings import load_settings
from tests.support.inproc import AUTH_HEADER, T0_MS, make_stack
from tests.support.ledger import open_ledger

D = Decimal
LONG = PositionSide.LONG
S = IntentState
O = OrderState
SYM = "BTCUSDT"
TF = "15m"
TF_MS = 900_000
ORDER, ALGO = "/api/private/order", "/api/private/algo_order"
CENTER, HALF = 50_000.0, 500.0
DOWNTIME_MS = 20_000
STOP_PRICE = D("48500")


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


def bar_rows(centers: list[float], start_ms: int, half: float = HALF) -> list[list[Any]]:
    return [[start_ms + i * TF_MS, c, c + half, c - half, c, 1.0, start_ms + (i + 1) * TF_MS - 1]
            for i, c in enumerate(centers)]


class World:
    """一次「进程生命」里的全部对象。``restart()`` 丢弃它（不走关停）、用同一个账本与同一个替身另搭一个。"""

    def __init__(self, tmp_path, *, stack=None, ledger=None, **config: Any) -> None:
        if stack is None:
            stack = make_stack(config={"trade_notional_usdt": D("100"), **config}, request_timeout_s=0.05)
            ledger = open_ledger(tmp_path, stack.clock)
        self.tmp_path = tmp_path
        self.stack, self.ledger = stack, ledger
        self.clock, self.disp, self.feed, self.engine = stack.clock, stack.disp, stack.feed, stack.engine
        self.config, self.cap, self.transport = stack.config, stack.cap, stack.transport
        self.bus = EventBus()
        self.events: list = []
        self.bus.subscribe(None, self.events.append, name="world.collect")
        self.adapter = StandinAdapter(stack.transport, capability=stack.cap, bus=self.bus, clock=self.clock,
                                      config_get=self.config.__getitem__, request_timeout_s=0.05)
        self.notifier = FakeNotifier()
        self.crash = CrashRegistry(live=False)
        self.instance = "T" + ledger.nonce
        cfg = self.config.__getitem__
        self.settings = load_settings({"DMR_ENV": "standin"}, webui_root=tmp_path / "DMR_WebUI" / "webui")
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
        self.reconciler = Reconciler(
            adapter=self.adapter, registry=self.registry, modelstate=self.modelstate, coordinator=self.coordinator,
            ingest=self.ingest, orders=self.orders, limits=self.limits, ledger=ledger, bus=self.bus, clock=self.clock,
            config_get=cfg, notifier=self.notifier, instance=self.instance)
        legacy = LegacyMutex(legacy_runtime_dir(self.settings), enabled=False)
        self.guard = Guard(settings=self.settings, adapter=self.adapter, modelstate=self.modelstate, bus=self.bus,
                           clock=self.clock, config_get=cfg, notifier=self.notifier, legacy=legacy)
        self.operations = Operations(ledger, self.clock, self.bus)
        self.stream = self.adapter.account_stream()
        self.recovery = Recovery(
            settings=self.settings, adapter=self.adapter, stream=self.stream, ledger=ledger, bus=self.bus,
            clock=self.clock, config_get=cfg, modelstate=self.modelstate, registry=self.registry,
            intents=self.intents, orders=self.orders, ingest=self.ingest, coordinator=self.coordinator,
            reconciler=self.reconciler, protection=self.protection, guard=self.guard, candles=self.candles,
            operations=self.operations, notifier=self.notifier)

    # ------------------------------------------------------------ 行情

    def set_bars(self, history: int = 40) -> None:
        centers = [CENTER] * history + [CENTER]
        self.feed.set_candles(SYM, TF, bar_rows(centers, T0_MS - history * TF_MS))

    # ------------------------------------------------------------ 生命周期

    async def boot(self) -> "World":
        """装配里「启动订阅者」那一步：恢复之前先把这些都起来（数据流和行情由恢复自己去起、去备）。"""
        await self.bus.start()
        await self.modelstate.load()
        await self.registry.start()
        await self.intents.start()
        await self.ingest.start()
        await self.rules.refresh(self.adapter)
        await self.coordinator.start()
        await self.protection.start()
        return self

    async def start_live(self, *, stream: bool = True) -> "World":
        """第一次生命：正常运行（开着闸、有行情、可以开仓），不经过恢复。"""
        await self.boot()
        await self.modelstate.set_gate(Gate.RUNNING, reason="测试")
        await self.refresh_limits()
        if stream:
            await self.stream.start()
        assert await self.candles.sync_server_time()
        await self.candles.refresh([SYM], TF)
        await self.settle()
        return self

    async def recover(self):
        result = await self.recovery.run()
        await self.settle()
        return result

    async def kill(self) -> None:
        """进程被杀：不走关停。这里只是让这个实例留下的任务不再动作（同一个进程里它们还活着，真被杀的进程里没有）。"""
        await self.stream.stop()
        await self.protection.stop()
        await self.coordinator.stop(0.0)
        await self.bus.stop()

    def restart(self) -> "World":
        return World(self.tmp_path, stack=self.stack, ledger=self.ledger)

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
        if ms:
            self.clock.advance(ms)
        await self.coordinator.resolve_unknown_orders()
        await self.protection.tick()
        await self.settle()

    async def wait_for(self, predicate, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        while not await predicate():
            if loop.time() - start > timeout:
                raise AssertionError("等待条件超时")
            await asyncio.sleep(0.002)

    # ------------------------------------------------------------ 读与操作

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

    async def open_long(self) -> int:
        return (await self.coordinator.submit(open_spec())).intent_id

    def system_id(self, intent: int, leg: str = "s", model: str = "0") -> str:
        return ids.make_client_id(self.instance, model, intent, leg)

    def close(self) -> None:
        self.ledger.close_sync()


_seq = 0


def open_spec(**kw: Any) -> IntentSpec:
    global _seq
    _seq += 1
    fields: dict[str, Any] = dict(action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol=SYM,
                                  side=LONG, idem_key=f"rc:{_seq}", ttl_s=60)
    fields.update(kw)
    return IntentSpec(**fields)


def close_spec(**kw: Any) -> IntentSpec:
    return open_spec(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR, ttl_s=None, **kw)


def partial_spec(qty: str, **kw: Any) -> IntentSpec:
    return open_spec(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR, ttl_s=None,
                     qty=D(qty), **kw)


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


async def protected_position(world: World):
    """开仓、等初始止损挂上；返回（登记记录, 交易所上的止损）。"""
    await world.start_live()
    await world.open_long()

    async def stop_exists() -> bool:
        return len(await world.exchange_algos()) > 0

    await world.wait_for(stop_exists)
    await world.settle()
    pos = world.position()
    (stop,) = await world.exchange_algos()
    return pos, stop


async def assert_one_system_stop_matches_the_registry(world: World, position_id: int):
    (stop,) = await world.exchange_algos()  # 恰好一张，没有第二张
    row = await world.row(position_id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == STOP_PRICE
    assert (row.stop_client_id, row.stop_algo_id) == (stop.client_algo_id, stop.algo_id)
    assert stop.trigger_price == STOP_PRICE and stop.close_position is True
    return stop


# ================================================================ 干净启动：什么都不用恢复


async def test_a_clean_start_recovers_nothing_and_lifts_the_recovering_suppression(world):
    await world.boot()
    result = await world.recover()
    assert result.tradable is True and result.problems == () and result.duration_ms <= 30_000
    assert Suppression.RECOVERING not in world.suppressions() and world.registry.holding is False
    assert world.modelstate.current().gate is Gate.STOPPED  # 恢复完状态仍是已停止
    assert world.stream.state == "connected"  # 数据流由恢复启动
    summary = result.steps["summary"]
    assert summary == {"stops_rebuilt": 0, "closed_by_fills": 0, "pending_registered": 0, "orphans_cleaned": 0,
                       "operations_interrupted": 0}


# ================================================================ 中断点 1：开仓单已发出，回执未到（open_sent）


@pytest.mark.parametrize("stream_in_first_life", [False, True])
async def test_crash_point_1_the_unrecorded_open_is_registered_protected_and_never_resent(world, stream_in_first_life):
    await world.start_live(stream=stream_in_first_life)
    world.crash.arm("open_sent")
    submitted = await world.coordinator.submit(open_spec())
    with pytest.raises(CrashSignal):
        await world.coordinator.wait_terminal(submitted.intent_id, 5.0)
    row = (await world.orders.for_intent(submitted.intent_id))[0]
    assert row.state is O.PENDING_SEND and row.sent_ms is not None  # 订单行已落盘，结果没记
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.002")  # 请求确实到了交易所
    if stream_in_first_life:
        await world.settle()  # 数据流在线时成交回报先到：登记簿已登记，但协调者已死、止损没挂
        assert world.position() is not None and await world.exchange_algos() == []
    else:
        assert world.position() is None  # 数据流没连：登记簿完全不知道这个仓
    await world.kill()

    again = world.restart()
    await again.boot()
    again.clock.advance(DOWNTIME_MS)
    result = await again.recover()

    assert result.tradable is True and result.duration_ms <= 30_000  # 第 2–5 步在 30 秒内做完
    rec = again.position()
    assert rec is not None and rec.status is PositionStatus.MANAGED and rec.ownership is Ownership.STRATEGY
    assert rec.qty == D("0.002") and rec.opened_by_intent_id == submitted.intent_id
    await assert_one_system_stop_matches_the_registry(again, rec.id)  # 已登记而无止损的仓补上了，且只有一张
    done = await again.intents.get(submitted.intent_id)
    assert done.state is S.COMPLETED and (await again.orders.for_intent(submitted.intent_id))[0].state is O.FILLED
    assert len(again.sent("POST", ORDER)) == 1 and len(again.sent("POST", ALGO)) == 1  # 开仓单没有重发，止损只挂一次
    assert again.registry.counts()["pending"] == 0  # 系统自己开的仓不是待确认仓
    assert Suppression.RECOVERING not in again.suppressions() and Suppression.UNPROTECTED not in again.suppressions()


# ================================================================ 中断点 2：已开仓，初始止损未挂上（open_filled_before_stop）


async def test_crash_point_2_a_registered_position_without_a_stop_gets_exactly_one_within_thirty_seconds(world):
    await world.start_live()
    world.crash.arm("open_filled_before_stop")
    await world.open_long()
    await world.settle()
    pos = world.position()
    assert world.protection.crashed is not None and await world.exchange_algos() == []
    assert pos is not None and pos.protection_state is not ProtectionState.SYSTEM
    await world.kill()

    again = world.restart()
    await again.boot()
    again.clock.advance(DOWNTIME_MS)
    started = again.clock.now_ms()
    wall = asyncio.get_running_loop().time()
    result = await asyncio.wait_for(again.recover(), 30)  # 真实时间也在 30 秒以内（注入时钟只量得到模拟时间）

    assert result.tradable is True and again.clock.now_ms() - started <= 30_000
    assert asyncio.get_running_loop().time() - wall < 30
    assert [e["position_id"] for e in result.steps["protection"]["rebuilt"]] == [pos.id]
    stop = await assert_one_system_stop_matches_the_registry(again, pos.id)
    assert stop.client_algo_id.startswith("DK")
    assert len(again.sent("POST", ALGO)) == 1  # 没有补出第二张
    assert Suppression.UNPROTECTED not in again.suppressions()
    await again.tick(60_000)  # 之后的定时驱动也不会再挂
    assert len(await again.exchange_algos()) == 1 and len(again.sent("POST", ALGO)) == 1


async def test_a_stop_that_is_already_on_the_exchange_but_not_in_the_registry_is_adopted_not_doubled(world):
    """挂上了而没来得及记（或记的那一笔丢了）：登记簿说「无保护」，交易所上其实有系统止损。恢复交给 rebuild 认领，不补第二张。"""
    pos, stop = await protected_position(world)
    await world.registry.set_protection(pos.id, state=ProtectionState.NONE, clear_stop=True)  # 登记簿丢了这张止损
    await world.kill()

    again = world.restart()
    await again.boot()
    again.clock.advance(DOWNTIME_MS)
    result = await again.recover()

    assert result.tradable is True
    adopted = await assert_one_system_stop_matches_the_registry(again, pos.id)
    assert adopted.algo_id == stop.algo_id  # 认领的就是原来那张
    assert len(again.sent("POST", ALGO)) == 1  # 两个进程加起来只挂过一次


async def test_running_the_recovery_twice_places_nothing_more(world):
    world_ = world
    await world_.start_live()
    world_.crash.arm("open_filled_before_stop")
    await world_.open_long()
    await world_.settle()
    await world_.kill()
    again = world_.restart()
    await again.boot()
    first = await again.recover()
    second = await again.recover()
    assert first.tradable and second.tradable
    assert len(await again.exchange_algos()) == 1 and len(again.sent("POST", ALGO)) == 1
    assert second.steps["summary"]["stops_rebuilt"] == 0  # 第二次：登记的系统止损就在交易所上，不再交给 rebuild


# ================================================================ 中断点 3：已撤旧止损，新止损未建成（stop_cancelled_before_new）


async def test_crash_point_3_the_missing_stop_is_rebuilt_at_the_registered_price_and_only_once(world):
    pos, before = await protected_position(world)
    after_bars = [CENTER + 250 * k for k in range(1, 7)]  # 开仓之后 6 根 K 线收盘：该移动止损了
    world.feed.set_candles(SYM, TF, bar_rows([CENTER] * 40 + [CENTER] + after_bars, T0_MS - 40 * TF_MS))
    world.clock.advance(7 * TF_MS)
    await world.candles.refresh([SYM], TF)
    world.feed.set_price(SYM, "51000")
    world.crash.arm("stop_cancelled_before_new")
    with pytest.raises(CrashSignal):  # 撤掉旧止损之后、挂新止损之前被杀
        await world.protection.trailing(T0_MS + 6 * TF_MS)
    await world.settle()
    assert world.coordinator.crashed is not None
    assert await world.exchange_algos() == []  # 旧单已撤，新单还没挂：这一刻没有止损
    registered = await world.row(pos.id)
    assert registered.stop_price == STOP_PRICE  # 登记簿里还是旧止损
    await world.kill()

    again = world.restart()
    await again.boot()
    again.clock.advance(DOWNTIME_MS)
    result = await again.recover()

    assert result.tradable is True
    assert [e["position_id"] for e in result.steps["protection"]["rebuilt"]] == [pos.id]
    stop = await assert_one_system_stop_matches_the_registry(again, pos.id)  # 按登记的止损价补上
    assert stop.algo_id != before.algo_id
    assert len(again.sent("POST", ALGO)) == 2  # 当初一次、恢复一次；没有第三次
    await again.tick(10_000)
    assert len(await again.exchange_algos()) == 1 and len(again.sent("POST", ALGO)) == 2


# ================================================================ 中断点 4：部分平仓单已发出，回执未到（partial_close_sent）


async def test_crash_point_4_an_executed_partial_close_is_settled_and_the_position_goes_back_to_managed(world):
    pos, stop = await protected_position(world)
    world.crash.arm("partial_close_sent")
    submitted = await world.coordinator.submit(partial_spec("0.001", position_id=pos.id))
    with pytest.raises(CrashSignal):
        await world.coordinator.wait_terminal(submitted.intent_id, 5.0)
    await world.settle()
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.001")  # 请求到了交易所并成交
    assert world.position().status is PositionStatus.CLOSING  # 暂停已落盘；「到期恢复」的记录在死掉的协调者内存里
    await world.kill()

    again = world.restart()
    await again.boot()
    again.clock.advance(DOWNTIME_MS)
    result = await again.recover()

    assert result.tradable is True and result.steps["closing"]["resumed"] == [pos.id]
    rec = again.position()
    assert rec.status is PositionStatus.MANAGED and rec.qty == D("0.001")  # 放回托管中，数量是平掉一半后的
    done = await again.intents.get(submitted.intent_id)
    assert done.state is S.COMPLETED and (await again.orders.for_intent(submitted.intent_id))[0].state is O.FILLED
    assert len(again.sent("POST", ORDER)) == 2  # 开仓 + 这一次部分平仓；部分平仓没有重发
    (alert,) = again.notifier.of("recovery_closing_resumed")
    assert alert["level"] is AlertLevel.INFO and "部分平仓" in alert["message"] and "COMPLETED" in alert["message"]
    await assert_one_system_stop_matches_the_registry(again, pos.id)  # 全平型止损没动，也没有补出第二张
    assert len(again.sent("POST", ALGO)) == 1 and again.sent("DELETE", ALGO) == []


async def test_crash_point_4_a_partial_close_that_never_reached_the_exchange_is_never_resent(world):
    """请求没到交易所：回查查不到，恢复先保持平仓中；协调者之后判完意图（连续 3 次不存在、过了时间窗），恢复放回托管中。"""
    pos, stop = await protected_position(world)
    world.disp.switches.update({"order_timeout": {"enabled": True, "mode": "drop", "hang_s": 30, "count": 1}})
    world.crash.arm("partial_close_sent")
    submitted = await world.coordinator.submit(partial_spec("0.001", position_id=pos.id))
    with pytest.raises(CrashSignal):
        await world.coordinator.wait_terminal(submitted.intent_id, 5.0)
    assert (await world.exchange_positions())[(SYM, LONG)].qty == D("0.002")  # 请求被丢了，交易所没动
    assert world.position().status is PositionStatus.CLOSING
    await world.kill()

    again = world.restart()
    await again.boot()
    again.clock.advance(DOWNTIME_MS)
    result = await again.recover()

    assert result.tradable is True  # 订单还没查清不是恢复的问题：协调者继续回查
    assert result.steps["closing"]["kept"] == [pos.id] and result.steps["closing"]["resumed"] == []
    assert again.position().status is PositionStatus.CLOSING
    assert again.notifier.of("recovery_closing_unresolved") and again.notifier.of("recovery_closing_resumed") == []

    settled = None
    for _ in range(10):  # 调度器每隔几秒调一次回查；这里用时钟推进代替
        settled = await again.intents.get(submitted.intent_id)
        if settled.state in IntentState.terminal():
            break
        again.clock.advance(6_000)
        await again.coordinator.resolve_unknown_orders()
        await again.settle()
    assert settled.state is S.REJECTED  # 判定：请求没有到交易所
    rec = again.position()
    assert rec.status is PositionStatus.MANAGED and rec.qty == D("0.002")  # 协调者自己不会放回，恢复放回了
    (alert,) = again.notifier.of("recovery_closing_resumed")
    assert "REJECTED" in alert["message"] and "部分平仓" in alert["message"]
    assert len(again.sent("POST", ORDER)) == 2  # 开仓 + 被丢掉的那一次；部分平仓永不重发
    await assert_one_system_stop_matches_the_registry(again, pos.id)


# ================================================================ 中断点 5：平仓单已成交，保护单未清理（close_filled_before_cleanup）


async def test_crash_point_5_the_leftover_stop_of_a_closed_position_is_cleaned_up(world):
    pos, before = await protected_position(world)
    world.crash.arm("close_filled_before_cleanup")
    closing = await world.coordinator.submit(close_spec(position_id=pos.id))
    await world.coordinator.wait_terminal(closing.intent_id, 5.0)
    await world.settle()
    assert world.position() is None and await world.exchange_positions() == {}
    assert world.protection.crashed is not None
    assert len(await world.exchange_algos()) == 1  # 清理之前被杀：止损还挂在零仓上
    assert (await world.row(pos.id)).stop_algo_id == before.algo_id
    await world.kill()

    again = world.restart()
    await again.boot()
    again.clock.advance(DOWNTIME_MS)
    result = await again.recover()

    assert result.tradable is True
    assert result.steps["reconcile"]["closed_cleanup"] == [pos.id]
    assert await again.exchange_algos() == []  # 残留的保护单撤掉了
    cleaned = await again.row(pos.id)
    assert cleaned.status is PositionStatus.CLOSED and cleaned.stop_algo_id is None and cleaned.stop_client_id is None
    assert len(again.sent("POST", ORDER)) == 2 and len(again.sent("POST", ALGO)) == 1  # 开仓 + 平仓；没有任何新的开仓与挂单
    assert Suppression.UNPROTECTED not in again.suppressions()


# ================================================================ 停机期间止损触发


@pytest.mark.parametrize("link", ["unavailable", "available"])
async def test_a_stop_that_fired_while_the_kernel_was_down_is_booked_as_a_stop_and_the_leftovers_are_cleaned(world, link):
    """触发后订单号可关联（available）时成交直接认领到系统止损单；不可关联（默认）时靠「推定为系统止损触发」；两种都记为止损。"""
    world.disp.switches.update({"behaviors": {"triggered_order_link": link}})
    pos, stop = await protected_position(world)
    # 一张系统前缀的残留条件单（比如换止损时没撤干净的旧单）：登记簿也不记得它
    leftover = world.engine.place_algo_order(
        symbol=SYM, side="SELL", position_side="LONG", algo_type="STOP_MARKET", trigger_price="40000",
        working_type="CONTRACT_PRICE", close_position=False, qty="0.002", client_algo_id=world.system_id(4242),
        source="system")
    await world.kill()  # 内核停机……
    world.clock.advance(DOWNTIME_MS)
    await world.manual("/dev/price_override", {"symbol": SYM, "price": "48000"})  # ……价格越过止损价，替身触发止损
    assert await world.exchange_positions() == {}  # 仓位没了
    assert [a.algo_id for a in await world.exchange_algos()] == [str(leftover["algo_id"])]  # 残留单还挂着
    world.clock.advance(120_000)  # 残留单够老了（超过一个条件单对账周期）

    again = world.restart()
    await again.boot()
    result = await again.recover()

    assert result.tradable is True
    assert again.position() is None
    closed = await again.row(pos.id)
    assert closed.status is PositionStatus.CLOSED and closed.exit_reason is ExitReason.STOP  # 补记为止损出场
    assert (PRESUMED_STOP_NOTE in closed.stop_note) is (link == "unavailable")
    assert result.steps["fills"]["closed_by_fills"] == [
        {"position_id": pos.id, "symbol": SYM, "side": "LONG", "exit_reason": "stop"}]
    trade = await again.ledger.read(_trade_of, pos.id)
    assert trade is not None and trade.exit_reason == "stop"
    assert await again.exchange_algos() == []  # 残留保护单清理掉了（对账的孤儿条件单五条）
    assert result.steps["orphans"]["cancelled"] == 1 and result.steps["summary"]["orphans_cleaned"] == 1
    assert len(again.sent("POST", ORDER)) == 1  # 恢复没有发任何订单（止损的市价单是交易所自己生成的）
    assert Suppression.UNPROTECTED not in again.suppressions() and again.registry.cooldown_until(SYM) is None


def _trade_of(session, position_id):
    from dmr_kernel.ledger import roundtrip

    return roundtrip.get_trade(session, position_id)


# ================================================================ 停机期间的外部手动开仓


async def test_a_manual_open_during_downtime_becomes_pending_and_is_not_taken_over(world):
    await world.start_live()
    await world.kill()
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "BUY", "position_side": "LONG", "qty": "0.002"})
    world.clock.advance(DOWNTIME_MS)

    again = world.restart()
    await again.boot()
    result = await again.recover()

    assert result.tradable is True
    rec = again.position()
    assert rec.status is PositionStatus.PENDING and rec.ownership is Ownership.NONE
    assert rec.pending_reason is PendingReason.PREEXISTING and rec.qty == D("0.002")  # 数量取自快照，没有被成交再加一次
    counts = again.registry.counts()
    assert counts["pending"] == 1 and counts["taking_over"] == 0 and counts["managed"] == 0
    assert again.sent("POST", ALGO) == [] and again.sent("POST", ORDER) == []  # 不自动接管：系统没挂止损、没下任何单
    assert await again.exchange_algos() == []
    assert result.steps["reconcile"]["pending_registered"] == [
        {"position_id": rec.id, "symbol": SYM, "side": "LONG", "reason": "preexisting"}]
    assert again.notifier.of("recovery_unregistered_position")[0]["level"] is AlertLevel.WARNING
    assert Suppression.UNPROTECTED not in again.suppressions()  # 待确认仓不计入「无保护持仓」
    await again.tick(60_000)
    assert again.position().status is PositionStatus.PENDING and again.sent("POST", ALGO) == []


async def test_a_manual_open_with_a_system_prefixed_stop_is_flagged_origin_to_verify_and_the_stop_is_kept(world):
    await world.start_live()
    await world.kill()
    await world.manual("/dev/manual/order", {"symbol": SYM, "side": "BUY", "position_side": "LONG", "qty": "0.002"})
    stop = world.engine.place_algo_order(
        symbol=SYM, side="SELL", position_side="LONG", algo_type="STOP_MARKET", trigger_price="45000",
        working_type="CONTRACT_PRICE", close_position=True, client_algo_id=world.system_id(4243), source="system")
    world.clock.advance(120_000)  # 这张止损够老了：孤儿清理的「建立已超过一个周期」满足，其余条件要靠登记引用来挡

    again = world.restart()
    await again.boot()
    result = await again.recover()

    rec = again.position()
    assert rec.status is PositionStatus.PENDING and rec.pending_reason is PendingReason.ORIGIN_TO_VERIFY
    assert (rec.stop_client_id, rec.stop_algo_id, rec.stop_price) == (
        stop["client_algo_id"], str(stop["algo_id"]), D("45000"))
    assert result.steps["reconcile"]["origin_to_verify"] == [rec.id]
    (kept,) = await again.exchange_algos()  # 这张止损保留：没撤、没被孤儿清理碰
    assert kept.algo_id == str(stop["algo_id"])
    assert again.sent("DELETE", ALGO) == [] and again.sent("POST", ALGO) == []
    assert again.registry.counts()["taking_over"] == 0 and again.registry.counts()["managed"] == 0
    assert result.tradable is True


# ================================================================ 中断的一键全平


async def test_an_interrupted_close_all_is_recorded_as_interrupted_the_lock_is_cleared_and_nothing_is_continued(world):
    pos, stop = await protected_position(world)
    record, _ = await world.operations.begin("op-close-all", OperationType.CLOSE_ALL, operator="alice",
                                             session_id=None, scope="managed", params={"scope": "managed"})
    await world.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner="op-close-all")
    # 逐仓平仓意图已落盘，进程在发出之前被杀
    queued = await world.intents.create(close_spec(position_id=pos.id, op_id="op-close-all", idem_key="op:close-all:1"))
    assert queued[0].state is S.CREATED
    await world.kill()

    again = world.restart()
    await again.boot()
    await again.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner="op-close-all")  # 假设启动时锁还在
    result = await again.recover()

    op = await again.operations.get("op-close-all")
    assert op.status is OperationStatus.ABORTED and op.result["interrupted"] is True
    assert "不自动续做" in op.result["reason"]
    (row,) = op.per_position
    assert row["position_id"] == pos.id and row["result"] == "residual" and "0.002" in row["detail"]
    assert again.modelstate.current().op_locks == frozenset()  # 操作锁清除
    assert (await again.intents.get(queued[0].id)).state is S.CANCELLED  # 没发出去的平仓意图取消，不续做
    assert (await again.exchange_positions())[(SYM, LONG)].qty == D("0.002")  # 仓位还在，没有被平
    assert len(again.sent("POST", ORDER)) == 1  # 只有当初那次开仓
    assert result.steps["summary"]["operations_interrupted"] == 1
    assert Suppression.RECOVERING not in again.suppressions() and again.modelstate.current().gate is Gate.STOPPED
    await assert_one_system_stop_matches_the_registry(again, pos.id)  # 仓位仍有那张止损，没有补出第二张
    assert len(again.sent("POST", ALGO)) == 1
    _ = stop
