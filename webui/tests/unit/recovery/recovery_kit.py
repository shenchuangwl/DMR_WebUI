"""重启恢复（core.recovery）测试的装配与小工具。

``Env`` 把恢复的协作者装配好：账本、总线、模型状态、登记簿、意图、订单、入账、操作记录都是真实的（临时账本里的真实 SQLite、
真实事件总线），交易所是可编程的 ``FakeAdapter``（读数按 Env 属性现算），守卫、数据流、保护单管理、对账、协调者、K 线、
通知器是假的：只记录调用、按需预设结果。所有假对象共用一份调用日志 ``env.calls``，用来核对步骤的先后。

交易所那一侧的状态放在 ``Env`` 的属性里（``positions``、``open_orders``、``algos``、``trades``、``order_views``、
``algo_views``、``skew_ms``）；``fail(name)`` 让某个读方法一直返回 Unknown，``fail_next(name, n)`` 只让前 n 次返回 Unknown。
时间用 ManualClock，不 sleep。协程夹具在本工程的测试约定里不可用：夹具只造 Env，测试里 ``env = await make_env().boot()``。
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from itertools import count
from typing import Any, NamedTuple

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.configdefs import DEFINITIONS
from dmr_kernel.core.coordinator import SubmitResult
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.ingest import Ingest
from dmr_kernel.core.intents import IntentService, IntentSpec
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.recovery import Recovery
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    AccountInfo,
    AlertLevel,
    AlgoOrderInfo,
    AlgoType,
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
    PositionInfo,
    PositionSide,
    ProtectionState,
    Suppression,
    TradeInfo,
    Unknown,
    WorkingType,
    WriteAccepted,
)
from dmr_kernel.exchange import ids
from dmr_kernel.ledger.records import IntentRecord, OrderRecord, PositionRecord
from dmr_kernel.ledger.writer import Ledger
from dmr_kernel.ops.operations import Operations
from dmr_kernel.settings import load_settings
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
    IntentState.VALIDATED: (IntentState.VALIDATED,),
    IntentState.DISPATCH_READY: (IntentState.VALIDATED, IntentState.DISPATCH_READY),
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
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def raise_alert(self, level: AlertLevel, kind: str, message: str, *, dedupe_key: str | None = None) -> None:
        self.alerts.append(Alert(level, kind, message, dedupe_key))


# ---------------------------------------------------------------- 假的协作者（只记录）


class FakeGuard:
    def __init__(self, env: "Env") -> None:
        self.env = env
        self.problems: list[str] = []
        self.started = False

    async def startup(self) -> list[str]:
        self.env.calls.append(("guard.startup", "recovering" if self.env.suppressed(self.env.recovering) else ""))
        return list(self.problems)

    async def start(self) -> None:
        self.env.calls.append("guard.start")
        self.started = True


class FakeStream:
    def __init__(self, env: "Env") -> None:
        self.env = env
        self.started = 0
        self.on_start: Any = None  # 数据流一起来就送进来的成交之类：一个无参协程函数

    async def start(self) -> None:
        self.env.calls.append(("stream.start", self.env.registry.holding))
        self.started += 1
        if self.on_start is not None:
            await self.on_start()

    async def stop(self) -> None:
        self.env.calls.append("stream.stop")


class FakeProtection:
    def __init__(self, env: "Env") -> None:
        self.env = env
        self.results: dict[int, Any] = {}  # 持仓号 -> 状态词、状态词序列（逐次返回）、或异常
        self.default = "protected"
        self.rebuilt: list[tuple[int, str]] = []
        self.cleaned: list[int] = []
        self.gate: Any = None  # 给了就是一个 awaitable 工厂：每次重建先等它（并发测试用）
        self.advance_ms = 0

    async def rebuild(self, position_id: int, *, reason: str) -> str:
        self.env.calls.append(("rebuild", position_id, reason))
        self.rebuilt.append((position_id, reason))
        if self.gate is not None:
            await self.gate()
        if self.advance_ms:
            self.env.clock.advance(self.advance_ms)
        result = self.results.get(position_id, self.default)
        if isinstance(result, list):
            result = result.pop(0) if len(result) > 1 else result[0]
        if isinstance(result, BaseException):
            raise result
        return result

    async def cleanup_after_close(self, position_id: int) -> None:
        self.env.calls.append(("cleanup_after_close", position_id))
        self.cleaned.append(position_id)

    async def check_unprotected(self) -> None:
        self.env.calls.append("check_unprotected")


class FakeReconciler:
    def __init__(self, env: "Env") -> None:
        self.env = env
        self.positions_result: Any = "ok"
        self.orders_result: Any = "ok"

    async def reconcile_positions(self) -> str:
        self.env.calls.append("reconcile_positions")
        if isinstance(self.positions_result, BaseException):
            raise self.positions_result
        return self.positions_result

    async def reconcile_orders(self) -> str:
        self.env.calls.append("reconcile_orders")
        if isinstance(self.orders_result, BaseException):
            raise self.orders_result
        return self.orders_result


class FakeCoordinator:
    """恢复用到的几件事：``resolve_unknown_orders``、``key_busy``、``submit``、``idle``。``submit`` 真的把意图写进账本。"""

    def __init__(self, env: "Env") -> None:
        self.env = env
        self.busy: set[tuple[str, PositionSide]] = set()
        self.submitted: list[IntentSpec] = []

    async def resolve_unknown_orders(self) -> None:
        self.env.calls.append("resolve_unknown_orders")

    def key_busy(self, symbol: str, side: PositionSide) -> bool:
        return (symbol, PositionSide(side)) in self.busy

    async def submit(self, spec: IntentSpec) -> SubmitResult:
        self.env.calls.append(("submit", spec.idem_key))
        self.submitted.append(spec)
        record, created = await self.env.intents.create(spec)
        return SubmitResult(intent_id=record.id, created=created, state=record.state)

    async def idle(self, timeout_s: float = 10.0) -> None:
        self.env.calls.append("coordinator.idle")


class FakeCandles:
    def __init__(self, env: "Env") -> None:
        self.env = env
        self.synced = 0
        self.warmed: list[tuple[tuple[str, ...], str]] = []
        self.refreshed: list[tuple[tuple[str, ...], str]] = []

    async def sync_server_time(self) -> bool:
        self.env.calls.append("candles.sync")
        self.synced += 1
        return True

    async def warm_up(self, symbols, timeframe, limit=500) -> None:
        symbols = tuple(symbols)
        self.env.calls.append(("candles.warm_up", symbols, timeframe))
        self.warmed.append((symbols, timeframe))

    async def refresh(self, symbols, timeframe) -> dict[str, bool]:
        symbols = tuple(symbols)
        self.env.calls.append(("candles.refresh", symbols, timeframe))
        self.refreshed.append((symbols, timeframe))
        return {s: True for s in symbols}


# ---------------------------------------------------------------- 数据工厂


def pos(symbol: str = SYM, side: PositionSide = LONG, qty: str = "1", entry: str = "100", mark: str | None = "100",
        liq: str | None = None) -> PositionInfo:
    return PositionInfo(symbol=symbol, side=side, qty=D(qty), entry_price=D(entry),
                        mark_price=None if mark is None else D(mark),
                        liquidation_price=None if liq is None else D(liq), leverage=2, margin_mode="isolated",
                        update_time_ms=T0)


def algo(symbol: str = SYM, side: PositionSide = LONG, *, cid: str | None, algo_id: str = "9001",
         trigger: str = "95", create_ms: int | None = None, raw_status: str = "NEW",
         algo_type: AlgoType = AlgoType.STOP_MARKET, close_position: bool = True,
         actual_order_id: str | None = None) -> AlgoOrderInfo:
    return AlgoOrderInfo(symbol=symbol, client_algo_id=cid, algo_id=algo_id, side=side.close_side(),
                         position_side=side, algo_type=algo_type, trigger_price=D(trigger),
                         working_type=WorkingType.CONTRACT_PRICE, close_position=close_position, qty=None,
                         raw_status=raw_status, actual_order_id=actual_order_id,
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


def order_view(*, symbol: str = SYM, side: PositionSide = LONG, cid: str | None, oid: str, qty: str = "1",
               filled: str | None = None, price: str = "100", status: str = "FILLED", opening: bool = True,
               time_ms: int | None = None) -> OrderInfo:
    filled = qty if filled is None else filled
    return OrderInfo(symbol=symbol, client_order_id=cid, exchange_order_id=oid,
                     side=side.open_side() if opening else side.close_side(), position_side=side,
                     order_type="MARKET", qty=D(qty), filled_qty=D(filled),
                     avg_price=D(price) if D(filled) > 0 else None, status=status, raw_status=status,
                     update_time_ms=T0 if time_ms is None else time_ms)


class Env:
    def __init__(self, tmp_path, clock: ManualClock, ledger: Ledger, bus: EventBus, *,
                 config: dict[str, Any] | None = None) -> None:
        self.clock, self.ledger, self.bus = clock, ledger, bus
        self.config: dict[str, Any] = {key: d.initial(Environment.STANDIN) for key, d in DEFINITIONS.items()}
        self.config.update(config or {})
        cfg = self.config.__getitem__
        self.settings = load_settings({"DMR_ENV": "standin"}, webui_root=tmp_path / "DMR_WebUI" / "webui")
        self.instance = "T" + ledger.nonce
        self.calls: list[Any] = []
        self.adapter = _SpyAdapter(self)
        self.notifier = FakeNotifier()
        self.modelstate = ModelStateService(ledger, bus, clock)
        self.registry = Registry(ledger, bus, clock, config_get=cfg)
        self.intents = IntentService(ledger, bus, clock, notifier=self.notifier)
        self.orders = OrderService(ledger, bus, clock, self.adapter, instance=self.instance)
        self.ingest = Ingest(ledger, bus, clock, self.adapter, instance=self.instance, config_get=cfg)
        self.operations = Operations(ledger, clock, bus)
        self.guard = FakeGuard(self)
        self.stream = FakeStream(self)
        self.protection = FakeProtection(self)
        self.reconciler = FakeReconciler(self)
        self.coordinator = FakeCoordinator(self)
        self.candles = FakeCandles(self)
        self.recovering = Suppression.RECOVERING
        self.events: list[Any] = []
        bus.subscribe(None, self.events.append, name="test.collect")
        # 交易所那一侧
        self.skew_ms = 0
        self.positions: list[PositionInfo] = []
        self.open_orders: list[OrderInfo] = []
        self.algos: list[AlgoOrderInfo] = []
        self.trades: list[TradeInfo] = []
        self.order_views: dict[str, OrderInfo] = {}
        self.algo_views: dict[str, AlgoOrderInfo] = {}
        self.failing: set[str] = set()
        self.failing_next: dict[str, int] = defaultdict(int)
        self.failing_trade_symbols: set[str] = set()
        self._install_reads()
        # 释放暂缓的成交：记一笔日志再交给真实的登记簿
        self.released: list[int] = []
        original = self.registry.release_fills

        async def spy(snapshot_ms: int):
            self.calls.append(("registry.release", snapshot_ms))
            self.released.append(snapshot_ms)
            return await original(snapshot_ms)

        self.registry.release_fills = spy  # type: ignore[method-assign]
        self._build_recovery_args()

    # ---------------------------------------------------------------- 恢复对象

    def _build_recovery_args(self) -> None:
        self.recovery_args: dict[str, Any] = dict(
            settings=self.settings, adapter=self.adapter, stream=self.stream, ledger=self.ledger, bus=self.bus,
            clock=self.clock, config_get=self.config.__getitem__, modelstate=self.modelstate, registry=self.registry,
            intents=self.intents, orders=self.orders, ingest=self.ingest, coordinator=self.coordinator,
            reconciler=self.reconciler, protection=self.protection, guard=self.guard, candles=self.candles,
            operations=self.operations, notifier=self.notifier)

    def recovery(self, **override: Any) -> Recovery:
        return Recovery(**{**self.recovery_args, **override})

    async def run(self, **override: Any):
        return await self.recovery(**override).run()

    # ---------------------------------------------------------------- 交易所

    def _install_reads(self) -> None:
        def reader(name: str, fn) -> None:
            def read(**kwargs):
                if name in self.failing:
                    return Unknown(f"{name} 故障（测试注入）")
                if self.failing_next[name] > 0:
                    self.failing_next[name] -= 1
                    return Unknown(f"{name} 暂时读不到（测试注入）")
                return Known(fn(**kwargs))

            self.adapter.set_read(name, read)

        def account() -> AccountInfo:
            return AccountInfo(equity=D("1000"), wallet_balance=D("995"), available_balance=D("950"),
                               unrealized_pnl=D("5"), update_time_ms=self.clock.now_ms())

        def trades(symbol, start_ms, end_ms, limit):
            if symbol in self.failing_trade_symbols:
                raise _TradesUnknown()
            return [t for t in self.trades if t.symbol == symbol]

        def algo_lookup(symbol, client_algo_id, algo_id):
            for key in (algo_id, client_algo_id):
                if key and key in self.algo_views:
                    return self.algo_views[key]
            return next((a for a in self.algos if (algo_id and a.algo_id == algo_id)
                         or (client_algo_id and a.client_algo_id == client_algo_id)), None)

        reader("server_time", lambda: self.clock.now_ms() + self.skew_ms)
        reader("fetch_positions", lambda: list(self.positions))
        reader("fetch_account", account)
        reader("fetch_open_orders", lambda symbol=None: [o for o in self.open_orders if symbol in (None, o.symbol)])
        reader("fetch_open_algo_orders", lambda symbol=None: [a for a in self.algos if symbol in (None, a.symbol)])
        reader("fetch_order", lambda symbol, client_order_id, exchange_order_id:
               self.order_views.get(exchange_order_id or client_order_id)
               or self.order_views.get(client_order_id or ""))
        reader("fetch_algo_order", algo_lookup)
        # 成交接口按合约单独注入故障：返回 Unknown（不能当成「没有成交」）
        self.adapter.set_read("fetch_trades", self._trades_read(trades))

    def _trades_read(self, fn):
        def read(**kwargs):
            if "fetch_trades" in self.failing:
                return Unknown("fetch_trades 故障（测试注入）")
            try:
                return Known(fn(**kwargs))
            except _TradesUnknown:
                return Unknown(f"{kwargs['symbol']} 的成交读不到（测试注入）")

        return read

    def fail(self, *names: str) -> None:
        self.failing.update(names)

    def fail_next(self, name: str, times: int) -> None:
        self.failing_next[name] = times

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

    def suppressed(self, which) -> bool:
        return which in self.modelstate.current().suppressions

    def suppressions(self) -> set[Any]:
        return set(self.modelstate.current().suppressions)

    def alerts(self, kind: str | None = None) -> list[Alert]:
        return [a for a in self.notifier.alerts if kind is None or a.kind == kind]

    def calls_named(self, name: str) -> list[Any]:
        return [c for c in self.calls if c == name or (isinstance(c, tuple) and c[0] == name)]

    def index_of(self, name: str, *, nth: int = 0) -> int:
        hits = [i for i, c in enumerate(self.calls) if c == name or (isinstance(c, tuple) and c[0] == name)]
        return hits[nth]

    # ---------------------------------------------------------------- 造登记簿与账本数据

    async def new_intent(self, *, action: IntentAction = IntentAction.OPEN, symbol: str = SYM,
                         side: PositionSide = LONG, state: IntentState = IntentState.COMPLETED,
                         params: dict[str, Any] | None = None, key: str | None = None, model: str = "m1",
                         source: IntentSource = IntentSource.SIGNAL, op_id: str | None = None,
                         position_id: int | None = None, reason: str = "") -> IntentRecord:
        spec = IntentSpec(action=action, model=model, source=source, symbol=symbol, side=side,
                          idem_key=key or f"kit:{next(_SEQ)}", params=params or {}, op_id=op_id,
                          position_id=position_id, reason=reason)
        record, _ = await self.intents.create(spec)
        for step in _PATHS[state]:
            record = await self.intents.transition(record.id, step, reason="测试")
        return record

    async def add_position(self, *, symbol: str = SYM, side: PositionSide = LONG, qty: str = "1",
                           avg: str = "100", status: str = "managed",
                           stop: tuple[str, str, str] | None = None,
                           protection: ProtectionState = ProtectionState.SYSTEM) -> PositionRecord:
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
            rec = await self.registry.set_protection(rec.id, state=protection, stop_price=D(price),
                                                     stop_client_id=cid, stop_algo_id=algo_id)
        return rec

    async def system_stop(self, *, symbol: str = SYM, side: PositionSide = LONG, trigger: str = "95",
                          algo_id: str = "9001", age_s: int = 120, state: str = "acked",
                          on_exchange: bool = True) -> AlgoOrderInfo:
        """账本里有一张系统止损（意图 + 订单行）。state：acked（已受理）| pending（落盘后还没有回音）。
        默认也放进交易所的条件单。返回交易所那一侧的条件单视图。"""
        intent = await self.new_intent(action=IntentAction.STOP_PLACE, symbol=symbol, side=side, model="op",
                                       source=IntentSource.PROTECTION, state=IntentState.COMPLETED)
        order = await self.orders.create_pending(
            intent=intent, leg="s", purpose=OrderPurpose.STOP, kind="algo", order_type="STOP_MARKET",
            side=side.close_side(), position_side=side, qty=None, trigger_price=D(trigger),
            working_type=WorkingType.CONTRACT_PRICE, close_position=True)
        info = algo(symbol, side, cid=order.client_order_id, algo_id=algo_id, trigger=trigger,
                    create_ms=self.clock.now_ms() - age_s * 1000)
        if state == "acked":
            await self.orders.apply_write_result(order.client_order_id, WriteAccepted(algo=info))
        else:
            await self.orders.record_sent(order.client_order_id)
        if on_exchange:
            self.algos.append(info)
        self.last_stop_order = order
        return info

    async def open_order_row(self, *, symbol: str = SYM, side: PositionSide = LONG, qty: str = "1",
                             exchange_view: OrderInfo | None | bool = True, price: str = "100",
                             intent_state: IntentState = IntentState.ACTIVE) -> OrderRecord:
        """开仓单已发出、回执没到（订单行 PENDING_SEND，已记发出时间）。``exchange_view`` 为 True：交易所那一侧已全部成交，
        并有一笔对应的成交可补拉；给一个 OrderInfo 就用它；None/False：交易所查不到（明确不存在）。"""
        intent = await self.new_intent(symbol=symbol, side=side, state=intent_state)
        order = await self.orders.create_pending(
            intent=intent, leg="o", purpose=OrderPurpose.OPEN, kind="market", order_type="MARKET",
            side=side.open_side(), position_side=side, qty=D(qty))
        await self.orders.record_sent(order.client_order_id)
        oid = f"X{next(_SEQ)}"
        if exchange_view is True:
            view = order_view(symbol=symbol, side=side, cid=order.client_order_id, oid=oid, qty=qty, price=price)
            self.order_views[order.client_order_id] = view
            self.order_views[oid] = view
            self.trades.append(trade(f"t{oid}", symbol=symbol, side=side.open_side(), position_side=side, qty=qty,
                                     price=price, order_id=oid, time_ms=self.clock.now_ms()))
        elif isinstance(exchange_view, OrderInfo):
            self.order_views[order.client_order_id] = exchange_view
        self.last_intent = intent
        return order

    async def close_order_row(self, *, symbol: str = SYM, side: PositionSide = LONG, qty: str = "1",
                              price: str = "110") -> OrderRecord:
        """平仓单已发出、回执没到；交易所那一侧已全部成交，并有一笔对应的成交可补拉（成交时间 = 现在）。"""
        intent = await self.new_intent(action=IntentAction.CLOSE, symbol=symbol, side=side, model="op",
                                       source=IntentSource.OPERATOR, state=IntentState.ACTIVE)
        order = await self.orders.create_pending(
            intent=intent, leg="c", purpose=OrderPurpose.CLOSE, kind="market", order_type="MARKET",
            side=side.close_side(), position_side=side, qty=D(qty))
        await self.orders.record_sent(order.client_order_id)
        oid = f"X{next(_SEQ)}"
        view = order_view(symbol=symbol, side=side, cid=order.client_order_id, oid=oid, qty=qty, price=price,
                          opening=False)
        self.order_views[order.client_order_id] = view
        self.order_views[oid] = view
        self.trades.append(trade(f"t{oid}", symbol=symbol, side=side.close_side(), position_side=side, qty=qty,
                                 price=price, order_id=oid, time_ms=self.clock.now_ms()))
        return order

    def own_id(self, *, intent: int = 77, leg: str = "s", model: str = "0") -> str:
        return ids.make_client_id(self.instance, model, intent, leg)

    def other_instance_id(self, intent: int = 77) -> str:
        return ids.make_client_id("ZZ9", "0", intent, "s")


class _TradesUnknown(Exception):
    pass


class _SpyAdapter(FakeAdapter):
    """FakeAdapter，另把每次读方法的调用记进 Env 的调用日志。"""

    def __init__(self, env: Env) -> None:
        super().__init__()
        self._env = env

    async def _read(self, name: str, **kwargs):
        self._env.calls.append(("read", name))
        return await super()._read(name, **kwargs)

