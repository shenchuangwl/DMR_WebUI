"""协调者单元测试的公共设施。

``Rig`` 把协调者需要的东西装在一起：真实的临时账本、意图服务、订单服务、登记簿、模型状态、合约数限额、合约规则，
假的适配层（``HookedAdapter``：写调用前可以插入钩子来阻塞或探测账本）、假的风控闸、假的通知器。
交易所一侧的状态（持仓、条件单、挂单、价格、杠杆）放在 ``rig.positions`` 等属性里，适配层的读方法直接读它们；
要让某个读方法失败就用 ``rig.adapter.set_read(名字, Unknown(...))``，``rig.wire()`` 恢复。

用法：``await rig.start()`` 之后 ``rec = await rig.go(spec())``；``go`` 提交意图、等所有后台派发任务和事件处理跑完，
返回此刻的意图记录（可能是终态，也可能停在 RECONCILING 之类）。
"""
from __future__ import annotations

import asyncio
import itertools
from decimal import Decimal
from typing import Any, Awaitable, Callable

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.crashpoints import CrashRegistry
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.intents import IntentService, IntentSpec
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    AlgoOrderInfo,
    AlgoType,
    Gate,
    Instrument,
    IntentAction,
    IntentSource,
    LeverageInfo,
    OrderInfo,
    OrderSide,
    PositionInfo,
    PositionSide,
    RejectKind,
    RiskDecision,
    Ticker,
    Unknown,
    WorkingType,
    WriteAccepted,
    WriteRejected,
    WriteUnknown,
)
from dmr_kernel.exchange.adapter import WriteCapability
from dmr_kernel.exchange.rules import InstrumentRules
from tests.support.fake_adapter import FakeAdapter
from tests.support.inproc import default_config

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
BUY, SELL = OrderSide.BUY, OrderSide.SELL
SYM = "BTCUSDT"
PRICES = {"BTCUSDT": D("50000"), "ETHUSDT": D("3000"), "DOGEUSDT": D("0.2"), "SOLUSDT": D("100"),
          "XRPUSDT": D("0.5")}


def instrument(symbol: str, tick: str, step: str, min_qty: str, min_notional: str) -> Instrument:
    return Instrument(symbol=symbol, base=symbol[:-4], quote="USDT", contract_type="PERPETUAL", status="TRADING",
                      tick_size=D(tick), step_size=D(step), min_qty=D(min_qty), min_notional=D(min_notional),
                      market_step_size=D(step), market_max_qty=D("100000"))


INSTRUMENTS = [
    instrument("BTCUSDT", "0.1", "0.001", "0.001", "100"),
    instrument("ETHUSDT", "0.01", "0.001", "0.001", "20"),
    instrument("DOGEUSDT", "0.00001", "1", "1", "5"),
    instrument("SOLUSDT", "0.01", "0.01", "0.01", "5"),
    instrument("XRPUSDT", "0.0001", "0.1", "0.1", "5"),
]


# ---------------------------------------------------------------- 假的风控闸与通知器


class FakeRiskGate:
    """只记录调用；``decision`` 是下一次判定的结果，``error`` 不为空就抛出。"""

    def __init__(self) -> None:
        self.decision = RiskDecision(True)
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def evaluate(self, *, model, action, now_ms) -> RiskDecision:
        self.calls.append({"model": model, "action": action, "now_ms": now_ms})
        if self.error is not None:
            raise self.error
        return self.decision


class FakeNotifier:
    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def raise_alert(self, level, kind, message, *, dedupe_key=None) -> None:
        self.alerts.append({"level": level, "kind": kind, "message": message, "dedupe_key": dedupe_key})

    def kinds(self) -> list[str]:
        return [a["kind"] for a in self.alerts]


# ---------------------------------------------------------------- 带钩子的假适配层


class HookedAdapter(FakeAdapter):
    """写方法在记录之前先 ``await hook(名字, 参数)``：测试可以用它阻塞发单、探测发单那一刻账本里有什么。"""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.hook: Callable[[str, dict], Awaitable[None]] | None = None

    async def _enter(self, name: str, kw: dict) -> None:
        if self.hook is not None:
            await self.hook(name, kw)

    async def _create_market_order(self, **kw):
        await self._enter("create_market_order", kw)
        return await super()._create_market_order(**kw)

    async def _create_stop_order(self, **kw):
        await self._enter("create_stop_order", kw)
        return await super()._create_stop_order(**kw)

    async def _cancel_order(self, **kw):
        await self._enter("cancel_order", kw)
        return await super()._cancel_order(**kw)

    async def _cancel_algo_order(self, **kw):
        await self._enter("cancel_algo_order", kw)
        return await super()._cancel_algo_order(**kw)

    async def _set_leverage(self, **kw):
        await self._enter("set_leverage", kw)
        return await super()._set_leverage(**kw)

    async def _set_margin_mode(self, **kw):
        await self._enter("set_margin_mode", kw)
        return await super()._set_margin_mode(**kw)


# ---------------------------------------------------------------- 造交易所视图与写结果

_xid = itertools.count(9001)


def order_view(kw: dict, *, status: str = "FILLED", filled: Decimal | None = None, avg: str | None = "50000",
               raw: str | None = None) -> OrderInfo:
    """按市价单的调用参数造订单视图（status：NEW | PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED）。"""
    qty = kw["qty"]
    if filled is None:
        filled = qty if status == "FILLED" else D("0")
    return OrderInfo(symbol=kw["symbol"], client_order_id=kw["client_order_id"], exchange_order_id=str(next(_xid)),
                     side=kw["side"], position_side=kw["position_side"], order_type="MARKET", qty=qty,
                     filled_qty=filled, avg_price=None if avg is None or filled == 0 else D(avg), status=status,
                     raw_status=raw or status, close_position=False, reduce_only=False, update_time_ms=0)


def fill_ok(avg: str = "50000") -> Callable[..., WriteAccepted]:
    return lambda **kw: WriteAccepted(order=order_view(kw, status="FILLED", avg=avg))


def ack_ok() -> Callable[..., WriteAccepted]:
    return lambda **kw: WriteAccepted(order=order_view(kw, status="NEW", avg=None))


def partial_ok(ratio: str = "0.5", avg: str = "50000") -> Callable[..., WriteAccepted]:
    return lambda **kw: WriteAccepted(order=order_view(kw, status="PARTIALLY_FILLED", filled=kw["qty"] * D(ratio),
                                                       avg=avg))


def algo_view(kw: dict, *, raw: str = "NEW", algo_id: str | None = None) -> AlgoOrderInfo:
    return AlgoOrderInfo(symbol=kw["symbol"], client_algo_id=kw["client_algo_id"],
                         algo_id=algo_id or str(next(_xid)), side=kw["side"], position_side=kw["position_side"],
                         algo_type=kw.get("algo_type", AlgoType.STOP_MARKET), trigger_price=kw["trigger_price"],
                         working_type=kw["working_type"], close_position=kw["close_position"], qty=kw.get("qty"),
                         raw_status=raw, actual_order_id=None, create_time_ms=0, update_time_ms=0)


def algo_ok(raw: str = "NEW", algo_id: str | None = None) -> Callable[..., WriteAccepted]:
    return lambda **kw: WriteAccepted(algo=algo_view(kw, raw=raw, algo_id=algo_id))


def rejected(kind: RejectKind, code: int | None, msg: str = "被拒绝") -> WriteRejected:
    return WriteRejected(kind, code, msg)


def unknown_write(reason: str = "请求超时") -> WriteUnknown:
    return WriteUnknown(reason)


def position(symbol: str = SYM, side: PositionSide = LONG, qty: str = "0.01", price: str = "50000") -> PositionInfo:
    return PositionInfo(symbol=symbol, side=side, qty=D(qty), entry_price=D(price), mark_price=D(price))


def stop_algo(symbol: str = SYM, side: PositionSide = LONG, *, cid: str | None = None, algo_id: str = "77",
              trigger: str = "45000", raw: str = "NEW", algo_type: AlgoType = AlgoType.STOP_MARKET) -> AlgoOrderInfo:
    return AlgoOrderInfo(symbol=symbol, client_algo_id=cid, algo_id=algo_id, side=side.close_side(),
                         position_side=side, algo_type=algo_type, trigger_price=D(trigger),
                         working_type=WorkingType.CONTRACT_PRICE, close_position=True, qty=None, raw_status=raw,
                         actual_order_id=None, create_time_ms=0, update_time_ms=0)


def script(*values: Any) -> Callable[..., Any]:
    """依次返回给定的值，最后一个值重复；用于 ``adapter.set_read``：每次调用换一个读数。"""
    items = list(values)

    def next_value(**kw: Any) -> Any:
        return items.pop(0) if len(items) > 1 else items[0]

    return next_value


# ---------------------------------------------------------------- 造意图


_seq = itertools.count(1)


def spec(**kw: Any) -> IntentSpec:
    """默认是模型 1 的信号开仓（BTCUSDT 多）；幂等键自动不重复，有效期 60 秒。"""
    fields: dict[str, Any] = dict(action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol=SYM,
                                  side=LONG, idem_key=f"t:{next(_seq)}", ttl_s=60)
    fields.update(kw)
    return IntentSpec(**fields)


def close_spec(**kw: Any) -> IntentSpec:
    """平掉整个仓（操作员发起）。"""
    fields: dict[str, Any] = dict(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR, ttl_s=None)
    fields.update(kw)
    return spec(**fields)


def partial_spec(qty: str = "0.004", **kw: Any) -> IntentSpec:
    fields: dict[str, Any] = dict(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                                  ttl_s=None, qty=D(qty))
    fields.update(kw)
    return spec(**fields)


def stop_place_spec(trigger: str = "45000", **kw: Any) -> IntentSpec:
    fields: dict[str, Any] = dict(action=IntentAction.STOP_PLACE, model="op", source=IntentSource.PROTECTION,
                                  ttl_s=None, params={"trigger_price": trigger, "working_type": "CONTRACT_PRICE"})
    fields.update(kw)
    return spec(**fields)


def stop_replace_spec(new: str = "46000", old: str = "45000", *, old_cid: str | None = "DKTxx-0-9-s",
                      old_algo_id: str | None = "77", mode: str = "gap", **kw: Any) -> IntentSpec:
    params = {"old": {"client_algo_id": old_cid, "algo_id": old_algo_id, "trigger_price": old},
              "trigger_price": new, "working_type": "CONTRACT_PRICE", "mode": mode}
    fields: dict[str, Any] = dict(action=IntentAction.STOP_REPLACE, model="op", source=IntentSource.PROTECTION,
                                  ttl_s=None, params=params)
    fields.update(kw)
    return spec(**fields)


def stop_cancel_spec(cid: str | None = "DKTxx-0-9-s", algo_id: str | None = "77", **kw: Any) -> IntentSpec:
    fields: dict[str, Any] = dict(action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.PROTECTION,
                                  ttl_s=None, params={"client_algo_id": cid, "algo_id": algo_id})
    fields.update(kw)
    return spec(**fields)


def order_cancel_spec(cid: str | None = "DKTxx-1-9-o", **kw: Any) -> IntentSpec:
    fields: dict[str, Any] = dict(action=IntentAction.ORDER_CANCEL, model="op", source=IntentSource.OPERATOR,
                                  ttl_s=None, params={"client_order_id": cid})
    fields.update(kw)
    return spec(**fields)


# ---------------------------------------------------------------- 装配


class Rig:
    def __init__(self, ledger, clock: ManualClock, *, live: bool = False, **config: Any) -> None:
        self.ledger = ledger
        self.clock = clock
        self.config = default_config(**{"trade_notional_usdt": D("100"), **config})
        self.bus = EventBus()
        self.events: list = []
        self.bus.subscribe(None, self.events.append, name="rig.collect")
        self.cap = WriteCapability("coordinator")
        self.adapter = HookedAdapter(capability=self.cap)
        self.notifier = FakeNotifier()
        self.riskgate = FakeRiskGate()
        self.crash = CrashRegistry(live=live)
        self.modelstate = ModelStateService(ledger, self.bus, clock)
        self.registry = Registry(ledger, self.bus, clock, config_get=self.config.__getitem__)
        self.intents = IntentService(ledger, self.bus, clock, notifier=self.notifier)
        self.instance = "T" + ledger.nonce
        self.orders = OrderService(ledger, self.bus, clock, self.adapter, instance=self.instance)
        self.limits = Limits(clock, config_get=self.config.__getitem__)
        self.rules = InstrumentRules(clock)
        self.list_calls: list[tuple[str, PositionSide]] = []
        self.list_result: Any = (True, "")
        # 交易所一侧的状态
        self.positions: list[PositionInfo] = []
        self.algos: list[AlgoOrderInfo] = []
        self.open_orders: list[OrderInfo] = []
        self.prices: dict[str, Decimal] = dict(PRICES)
        self.leverage: dict[str, LeverageInfo] = {}
        self.wire()
        self.coordinator = self.build_coordinator()

    # ------------------------------------------------------------ 装配细节

    def build_coordinator(self, **overrides: Any) -> Coordinator:
        parts: dict[str, Any] = dict(
            adapter=self.adapter, capability=self.cap, ledger=self.ledger, bus=self.bus, clock=self.clock,
            config_get=self.config.__getitem__, modelstate=self.modelstate, registry=self.registry,
            intents=self.intents, orders=self.orders, riskgate=self.riskgate, limits=self.limits, rules=self.rules,
            notifier=self.notifier, crash=self.crash, list_check=self._list_check)
        parts.update(overrides)
        return Coordinator(**parts)

    def _list_check(self, symbol: str, side: PositionSide):
        self.list_calls.append((symbol, side))
        if isinstance(self.list_result, Exception):
            raise self.list_result
        return self.list_result

    def wire(self) -> None:
        """把适配层的读方法接到 ``rig`` 上的交易所状态（也用来在 ``set_read(..., Unknown)`` 之后恢复）。"""
        a = self.adapter
        a.set_read("load_instruments", list(INSTRUMENTS))
        a.set_read("fetch_positions", lambda: list(self.positions))
        a.set_read("fetch_open_algo_orders",
                   lambda symbol=None: [x for x in self.algos if symbol is None or x.symbol == symbol])
        a.set_read("fetch_open_orders",
                   lambda symbol=None: [x for x in self.open_orders if symbol is None or x.symbol == symbol])
        a.set_read("fetch_tickers", self._tickers)
        a.set_read("fetch_leverage",
                   lambda symbol: self.leverage.get(symbol, LeverageInfo(symbol, 2, "isolated")))

    def _tickers(self, symbols=None):
        now = self.clock.now_ms()
        wanted = self.prices if symbols is None else {s: self.prices[s] for s in symbols if s in self.prices}
        return {s: Ticker(symbol=s, last=p, mark=p, time_ms=now) for s, p in wanted.items()}

    def refresh_limits(self) -> None:
        """让合约数限额的两份快照是新的（快照 20 秒、120 秒没更新就过期）。"""
        now = self.clock.now_ms()
        self.limits.update_positions(self.positions, now)
        self.limits.update_orders(self.open_orders, self.algos, now)

    # ------------------------------------------------------------ 生命周期

    async def start(self, *, running: bool = True) -> "Rig":
        await self.bus.start()
        await self.modelstate.load()
        if running:
            await self.modelstate.set_gate(Gate.RUNNING, reason="测试")
        await self.registry.start()
        await self.intents.start()
        await self.rules.refresh(self.adapter)
        self.refresh_limits()
        await self.coordinator.start()
        return self

    async def settle(self) -> None:
        """等后台派发任务和事件处理都跑完（反复几轮：处理函数可能再发布事件）。"""
        for _ in range(4):
            await self.coordinator.idle(10.0)
            await self.bus.drain(10.0)

    async def go(self, intent_spec: IntentSpec):
        """提交一个意图，等一切安静下来，返回此刻的意图记录。"""
        result = await self.coordinator.submit(intent_spec)
        await self.settle()
        return await self.intents.get(result.intent_id)

    async def go_all(self, *specs: IntentSpec):
        ids_ = [(await self.coordinator.submit(s)).intent_id for s in specs]
        await self.settle()
        return [await self.intents.get(i) for i in ids_]

    # ------------------------------------------------------------ 读账本

    async def order_rows(self, intent_id: int):
        return await self.orders.for_intent(intent_id)

    async def only_order(self, intent_id: int):
        rows = await self.orders.for_intent(intent_id)
        assert len(rows) == 1, rows
        return rows[0]

    def sent(self, name: str) -> list[dict]:
        return self.adapter.write_calls(name)

    def names(self) -> list[str]:
        """适配层收到的写调用，按顺序。"""
        return [n for n, _ in self.adapter.writes]


async def register(rig: "Rig", symbol: str = SYM, side: PositionSide = LONG, qty: str = "0.01",
                   price: str = "50000"):
    """在登记簿里登记一个托管中的策略仓（部分平仓要把它置为平仓中）。"""
    from dmr_kernel.core.types import Ownership

    return await rig.registry.register_open(symbol=symbol, side=side, qty=D(qty), avg_price=D(price),
                                            open_time_ms=rig.clock.now_ms(), ownership=Ownership.STRATEGY,
                                            model="m1", intent_id=1)


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """让出事件循环直到条件成立（真实时间上限很短，只为避免测试挂死）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.005)


async def tick(rig: "Rig", ms: int = 6_000) -> None:
    """推进时钟，让协调者回查一轮（调度器每 5 秒调用 ``resolve_unknown_orders``），等所有后台任务和事件处理跑完。"""
    rig.clock.advance(ms)
    await rig.coordinator.resolve_unknown_orders()
    await rig.settle()


def algo_for(order, *, raw: str = "NEW", algo_id: str = "8801") -> AlgoOrderInfo:
    """按账本里的条件单订单行造交易所一侧的条件单视图。"""
    return AlgoOrderInfo(symbol=order.symbol, client_algo_id=order.client_order_id, algo_id=algo_id, side=order.side,
                         position_side=order.position_side, algo_type=AlgoType(order.order_type),
                         trigger_price=order.trigger_price, working_type=order.working_type,
                         close_position=order.close_position, qty=order.qty, raw_status=raw, actual_order_id=None,
                         create_time_ms=0, update_time_ms=0)


def market_view_for(order, *, status: str = "FILLED", filled: Decimal | None = None, avg: str | None = "50000"):
    """按账本里的市价订单行造交易所一侧的订单视图。"""
    return order_view({"symbol": order.symbol, "client_order_id": order.client_order_id, "side": order.side,
                       "position_side": order.position_side, "qty": order.qty}, status=status, filled=filled, avg=avg)
