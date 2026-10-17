"""对账测试的装配与小工具。

``Env`` 把对账模块的协作者装配好：账本、登记簿、模型状态、订单、入账、意图、合约数上限都是真实的（临时账本里的真实
SQLite、真实事件总线），交易所是可编程的 ``FakeAdapter``，协调者（``FakeCoordinator``）与通知器只记录。

交易所那一侧的状态放在 ``Env`` 的属性里（``positions``、``open_orders``、``algos``、``trades``、``order_views``、
``skew_ms``、``equity``），读方法按这些属性现算；``fail(name)`` 让某个读方法返回 Unknown，``heal(name)`` 恢复。
"""
from __future__ import annotations

from decimal import Decimal
from itertools import count
from typing import Any, NamedTuple

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.configdefs import DEFINITIONS
from dmr_kernel.core.coordinator import SubmitResult
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.ingest import Ingest
from dmr_kernel.core.intents import IntentService, IntentSpec
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.reconcile import Reconciler
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    AccountInfo,
    AlertLevel,
    AlgoOrderInfo,
    AlgoType,
    CancelOutcome,
    CancelResult,
    Environment,
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    OrderInfo,
    OrderPurpose,
    OrderSide,
    Ownership,
    PendingReason,
    PositionSide,
    ProtectionState,
    TradeInfo,
    Unknown,
    WorkingType,
    WriteAccepted,
)
from dmr_kernel.exchange import ids
from dmr_kernel.ledger.records import IntentRecord, PositionRecord
from dmr_kernel.ledger.writer import Ledger
from tests.support.fake_adapter import FakeAdapter
from tests.support.ledger import START_MS

D = Decimal
T0 = START_MS
SYM = "BTCUSDT"
SYM2 = "ETHUSDT"
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
BUY, SELL = OrderSide.BUY, OrderSide.SELL

_SEQ = count(1)
# 从 CREATED 走到某个状态要经过的迁移（意图状态机的合法路径）
_PATHS: dict[IntentState, tuple[IntentState, ...]] = {
    IntentState.CREATED: (),
    IntentState.ACTIVE: (IntentState.VALIDATED, IntentState.DISPATCH_READY, IntentState.ACTIVE),
    IntentState.RECONCILING: (IntentState.VALIDATED, IntentState.DISPATCH_READY, IntentState.RECONCILING),
    IntentState.COMPLETED: (IntentState.VALIDATED, IntentState.DISPATCH_READY, IntentState.ACTIVE,
                            IntentState.COMPLETED),
    IntentState.REJECTED: (IntentState.REJECTED,),
    IntentState.CANCELLED: (IntentState.CANCELLED,),
}


class Alert(NamedTuple):
    level: AlertLevel
    kind: str
    message: str
    dedupe_key: str | None


class FakeNotifier:
    """与 alerts.notifier.Notifier 同形的记录器：只记下 raise_alert 的参数。"""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def raise_alert(self, level: AlertLevel, kind: str, message: str, *, dedupe_key: str | None = None) -> None:
        self.alerts.append(Alert(level, kind, message, dedupe_key))


class FakeCoordinator:
    """只做对账用得到的两件事：``key_busy`` 与 ``submit``。``submit`` 真的把意图写进账本（经 IntentService），
    这样对账之后查「幂等键」「在途意图」看到的是真实的账本。"""

    def __init__(self, intents: IntentService) -> None:
        self._intents = intents
        self.busy: set[tuple[str, PositionSide]] = set()
        self.submitted: list[IntentSpec] = []
        self.submit_error: Exception | None = None

    def key_busy(self, symbol: str, side: PositionSide) -> bool:
        return (symbol, PositionSide(side)) in self.busy

    async def submit(self, spec: IntentSpec) -> SubmitResult:
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append(spec)
        record, created = await self._intents.create(spec)
        return SubmitResult(intent_id=record.id, created=created, state=record.state)


# ---------------------------------------------------------------- 数据工厂


def pos(symbol: str = SYM, side: PositionSide = LONG, qty: str = "1", entry: str = "100", mark: str | None = "100",
        liq: str | None = None) -> Any:
    from dmr_kernel.core.types import PositionInfo

    return PositionInfo(symbol=symbol, side=side, qty=D(qty), entry_price=D(entry),
                        mark_price=None if mark is None else D(mark),
                        liquidation_price=None if liq is None else D(liq), leverage=2, margin_mode="isolated",
                        update_time_ms=T0)


def algo(symbol: str = SYM, side: PositionSide = LONG, *, cid: str | None, algo_id: str = "9001",
         trigger: str = "95", create_ms: int | None = None, raw_status: str = "NEW",
         algo_type: AlgoType = AlgoType.STOP_MARKET, close_position: bool = True) -> AlgoOrderInfo:
    return AlgoOrderInfo(symbol=symbol, client_algo_id=cid, algo_id=algo_id, side=side.close_side(),
                         position_side=side, algo_type=algo_type, trigger_price=D(trigger),
                         working_type=WorkingType.CONTRACT_PRICE, close_position=close_position, qty=None,
                         raw_status=raw_status, actual_order_id=None,
                         create_time_ms=T0 if create_ms is None else create_ms, update_time_ms=T0)


def open_order(symbol: str = SYM, side: PositionSide = LONG, *, cid: str | None = "web_1", oid: str = "5001",
               status: str = "NEW") -> OrderInfo:
    return OrderInfo(symbol=symbol, client_order_id=cid, exchange_order_id=oid, side=side.open_side(),
                     position_side=side, order_type="LIMIT", qty=D("1"), filled_qty=D("0"), avg_price=None,
                     status=status, raw_status=status, update_time_ms=T0)


def trade(tid: str, *, symbol: str = SYM, side: OrderSide = BUY, position_side: PositionSide = LONG,
          qty: str = "0.5", price: str = "100", time_ms: int | None = None, order_id: str | None = None) -> TradeInfo:
    return TradeInfo(trade_id=tid, exchange_order_id=order_id or f"ord-{tid}", symbol=symbol, side=side,
                     position_side=position_side, qty=D(qty), price=D(price), commission=D("0.01"),
                     commission_asset="USDT", realized_pnl=D("0"), time_ms=T0 if time_ms is None else time_ms)


def web_view(trade_info: TradeInfo, cid: str = "web_abc") -> OrderInfo:
    """手动单的订单视图（入账回查订单号时据此判定「外部」）。"""
    return OrderInfo(symbol=trade_info.symbol, client_order_id=cid, exchange_order_id=trade_info.exchange_order_id,
                     side=trade_info.side, position_side=trade_info.position_side, order_type="MARKET",
                     qty=trade_info.qty, filled_qty=trade_info.qty, avg_price=trade_info.price, status="FILLED",
                     raw_status="FILLED", update_time_ms=trade_info.time_ms)


class Env:
    def __init__(self, clock: ManualClock, ledger: Ledger, bus: EventBus, *, config: dict[str, Any] | None = None,
                 instance_filter: bool = False) -> None:
        self.clock, self.ledger, self.bus = clock, ledger, bus
        self.config: dict[str, Any] = {key: d.initial(Environment.STANDIN) for key, d in DEFINITIONS.items()}
        self.config.update(config or {})
        cfg = self.config.__getitem__
        self.instance = "T" + ledger.nonce
        self.adapter = FakeAdapter()
        self.notifier = FakeNotifier()
        self.modelstate = ModelStateService(ledger, bus, clock)
        self.registry = Registry(ledger, bus, clock, config_get=cfg)
        self.intents = IntentService(ledger, bus, clock, notifier=self.notifier)
        self.orders = OrderService(ledger, bus, clock, self.adapter, instance=self.instance)
        self.ingest = Ingest(ledger, bus, clock, self.adapter, instance=self.instance, config_get=cfg)
        self.limits = Limits(clock, config_get=cfg)
        self.coordinator = FakeCoordinator(self.intents)
        extra = {"instance": self.instance} if instance_filter else {}
        self.reconciler = Reconciler(
            adapter=self.adapter, registry=self.registry, modelstate=self.modelstate, coordinator=self.coordinator,
            ingest=self.ingest, orders=self.orders, limits=self.limits, ledger=ledger, bus=bus, clock=clock,
            config_get=cfg, notifier=self.notifier, **extra)
        self.events: list[Any] = []
        bus.subscribe(None, self.events.append, name="test.collect")
        # 交易所那一侧
        self.skew_ms = 0
        self.equity = D("1000")
        self.positions: list[Any] = []
        self.open_orders: list[OrderInfo] = []
        self.algos: list[AlgoOrderInfo] = []
        self.trades: list[TradeInfo] = []
        self.order_views: dict[str, OrderInfo] = {}
        self.failing: set[str] = set()
        self._install_reads()

    # ---------------------------------------------------------------- 交易所

    def _install_reads(self) -> None:
        def reader(name: str, fn) -> None:
            def read(**kwargs):
                if name in self.failing:
                    return Unknown(f"{name} 故障（测试注入）")
                return Known(fn(**kwargs))

            self.adapter.set_read(name, read)

        def account() -> AccountInfo:
            return AccountInfo(equity=self.equity, wallet_balance=self.equity - D("5"),
                               available_balance=self.equity - D("50"), unrealized_pnl=D("5"),
                               update_time_ms=self.clock.now_ms())

        reader("server_time", lambda: self.clock.now_ms() + self.skew_ms)
        reader("fetch_positions", lambda: list(self.positions))
        reader("fetch_account", account)
        reader("fetch_open_orders", lambda symbol=None: [o for o in self.open_orders if symbol in (None, o.symbol)])
        reader("fetch_open_algo_orders", lambda symbol=None: [a for a in self.algos if symbol in (None, a.symbol)])
        reader("fetch_trades", lambda symbol, start_ms, end_ms, limit: [t for t in self.trades if t.symbol == symbol])
        reader("fetch_order", lambda symbol, client_order_id, exchange_order_id:
               self.order_views.get(exchange_order_id or client_order_id))
        reader("fetch_algo_order", lambda symbol, client_algo_id, algo_id: next(
            (a for a in self.algos if (algo_id and a.algo_id == algo_id) or (client_algo_id and a.client_algo_id == client_algo_id)),
            None))

    def fail(self, *names: str) -> None:
        self.failing.update(names)

    def heal(self, *names: str) -> None:
        self.failing.difference_update(names or tuple(self.failing))

    # ---------------------------------------------------------------- 生命周期与观察

    async def boot(self) -> "Env":
        await self.modelstate.load()
        await self.registry.start()
        await self.ingest.start()
        await self.intents.start()
        await self.bus.start()
        return self

    async def settle(self) -> None:
        await self.bus.drain()

    async def clear_events(self) -> None:
        await self.bus.drain()
        self.events.clear()

    def events_of(self, *types: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, types)]

    def alerts(self, kind: str | None = None) -> list[Alert]:
        return [a for a in self.notifier.alerts if kind is None or a.kind == kind]

    def suppressions(self) -> set[Any]:
        return set(self.modelstate.current().suppressions)

    def completed(self, kind: str) -> list[Any]:
        from dmr_kernel.core.events import ReconcileCompleted

        return [e for e in self.events if isinstance(e, ReconcileCompleted) and e.kind == kind]

    # ---------------------------------------------------------------- 造登记簿与账本数据

    async def new_intent(self, *, action: IntentAction = IntentAction.OPEN, symbol: str = SYM,
                         side: PositionSide = LONG, state: IntentState = IntentState.COMPLETED,
                         params: dict[str, Any] | None = None, key: str | None = None, model: str = "m1",
                         source: IntentSource = IntentSource.SIGNAL) -> IntentRecord:
        spec = IntentSpec(action=action, model=model, source=source, symbol=symbol, side=side,
                          idem_key=key or f"kit:{next(_SEQ)}", params=params or {})
        record, _ = await self.intents.create(spec)
        for step in _PATHS[state]:
            record = await self.intents.transition(record.id, step, reason="测试")
        return record

    async def add_position(self, *, symbol: str = SYM, side: PositionSide = LONG, qty: str = "1",
                           avg: str = "100", status: str = "managed",
                           stop: tuple[str, str, str] | None = None) -> PositionRecord:
        """status：managed | closing | pending | ignored | taking_over。stop = (client_algo_id, algo_id, 止损价)。"""
        if status in ("managed", "closing"):
            intent = await self.new_intent(symbol=symbol, side=side)
            rec = await self.registry.register_open(
                symbol=symbol, side=side, qty=D(qty), avg_price=D(avg), open_time_ms=self.clock.now_ms(),
                ownership=Ownership.STRATEGY, model="m1", intent_id=intent.id)
            if status == "closing":
                rec = await self.registry.set_closing(rec.id, True, cause="test")
        elif status in ("pending", "ignored"):
            rec = await self.registry.register_pending(symbol=symbol, side=side, qty=D(qty), avg_price=D(avg),
                                                      reason=PendingReason.PREEXISTING)
            if status == "ignored":
                rec = await self.registry.ignore(rec.id, expected_version=None)
        elif status == "taking_over":
            rec = await self.registry.begin_takeover(
                symbol=symbol, side=side, qty=D(qty), avg_price=D(avg), open_time_ms=self.clock.now_ms(),
                open_time_estimated=False, anchor_ms=self.clock.now_ms(), signal_exit=False)
        else:
            raise ValueError(status)
        if stop is not None:
            cid, algo_id, price = stop
            rec = await self.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D(price),
                                                     stop_client_id=cid, stop_algo_id=algo_id)
        return rec

    async def system_stop(self, *, symbol: str = SYM, side: PositionSide = LONG, trigger: str = "95",
                          algo_id: str = "9001", age_s: int = 120, intent_state: IntentState = IntentState.COMPLETED,
                          cancel_pending: bool = False, open_on_exchange: bool = True,
                          add_to_exchange: bool = True) -> AlgoOrderInfo:
        """账本里有一张系统止损（意图 + 订单行，已受理），可选地停在 CANCEL_PENDING；默认也放进交易所的条件单。"""
        intent = await self.new_intent(action=IntentAction.STOP_PLACE, symbol=symbol, side=side, model="op",
                                       source=IntentSource.PROTECTION, state=intent_state)
        order = await self.orders.create_pending(
            intent=intent, leg="s", purpose=OrderPurpose.STOP, kind="algo", order_type="STOP_MARKET",
            side=side.close_side(), position_side=side, qty=None, trigger_price=D(trigger),
            working_type=WorkingType.CONTRACT_PRICE, close_position=True)
        info = algo(symbol, side, cid=order.client_order_id, algo_id=algo_id, trigger=trigger,
                    create_ms=self.clock.now_ms() - age_s * 1000)
        await self.orders.apply_write_result(order.client_order_id, WriteAccepted(algo=info))
        if cancel_pending:
            await self.orders.apply_cancel_result(order.client_order_id, CancelResult(CancelOutcome.UNKNOWN))
        if add_to_exchange and open_on_exchange:
            self.algos.append(info)
        return info

    async def system_open_fill(self, *, symbol: str = SYM, side: PositionSide = LONG,
                               qty: str = "0.5") -> tuple[Any, TradeInfo]:
        """系统开仓单已在订单行里成交（REST 应答就是 FILLED），但成交明细还没入账：返回（订单行，待入账的成交）。"""
        intent = await self.new_intent(symbol=symbol, side=side, state=IntentState.COMPLETED)
        order = await self.orders.create_pending(
            intent=intent, leg="o", purpose=OrderPurpose.OPEN, kind="market", order_type="MARKET",
            side=side.open_side(), position_side=side, qty=D(qty))
        view = OrderInfo(symbol=symbol, client_order_id=order.client_order_id, exchange_order_id="7001",
                         side=side.open_side(), position_side=side, order_type="MARKET", qty=D(qty),
                         filled_qty=D(qty), avg_price=D("100"), status="FILLED", raw_status="FILLED",
                         update_time_ms=self.clock.now_ms())
        order = await self.orders.apply_write_result(order.client_order_id, WriteAccepted(order=view))
        fill = trade("sys-1", symbol=symbol, side=side.open_side(), position_side=side, qty=qty, order_id="7001",
                     time_ms=self.clock.now_ms())
        return order, fill

    def own_id(self, *, intent: int = 77, leg: str = "s", model: str = "0") -> str:
        """一个系统格式的编号（本实例）。"""
        return ids.make_client_id(self.instance, model, intent, leg)

    def other_instance_id(self, intent: int = 77) -> str:
        return ids.make_client_id("ZZ9", "0", intent, "s")
