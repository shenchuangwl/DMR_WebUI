"""保护单管理（T0.18）单元测试的公共设施。

``Rig`` 装一整套真实的内核部件：临时账本、事件总线、持仓登记簿、意图/订单服务、模型状态、合约数限额、合约规则，
以及**真实的协调者**（保护单管理发出的止损类意图由它执行，``params["result"]`` 也由它写），
只有适配层（``FakeExchange``：有状态的假交易所，可以按需排队失败结果）、行情（``FakeCandles``）、
通知器与风控闸是假的。时间用 ``ManualClock``：``rig.advance(ms)`` 推进，``await rig.protection.tick()`` 驱动重试。

约定：测试里的 K 线用 ``frame_of`` 造，每根的中心价 ``c``，最高 ``c+half``、最低 ``c-half``、收盘 ``c``；
只要相邻两根的中心价相差不超过 ``half``，每根的真实波幅就是 ``2*half``，ATR（14 期）也是 ``2*half``（``half=1`` 时为 2）。
于是多仓止损 = 窗口内最低中心价 − half − 2*half，便于手算期望值。
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from decimal import Decimal
from typing import Any, Awaitable, Callable

import pandas as pd

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.crashpoints import CrashRegistry
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.intents import IntentService
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.protection import Protection
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    AlgoOrderInfo,
    AlgoType,
    CancelOutcome,
    CancelResult,
    Instrument,
    LeverageInfo,
    OrderInfo,
    Ownership,
    PositionInfo,
    PositionSide,
    ProtectionState,
    RejectKind,
    RiskDecision,
    Suppression,
    Ticker,
    WorkingType,
    WriteAccepted,
    WriteRejected,
    WriteUnknown,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.adapter import WriteCapability
from dmr_kernel.exchange.rules import InstrumentRules
from tests.support.fake_adapter import FakeAdapter
from tests.support.inproc import default_config
from tests.support.ledger import START_MS

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
SYM = "SOLUSDT"
T0 = START_MS
TF = "15m"
TF_MS = 900_000
BAR0 = (T0 // TF_MS) * TF_MS  # 持仓所在的那根 K 线的开盘时间（对齐 15 分钟）
OPEN_MS = BAR0 + 3_000  # 成交时间：K 线开盘后 3 秒


def instrument(symbol: str, tick: str, step: str, min_qty: str, min_notional: str) -> Instrument:
    return Instrument(symbol=symbol, base=symbol[:-4], quote="USDT", contract_type="PERPETUAL", status="TRADING",
                      tick_size=D(tick), step_size=D(step), min_qty=D(min_qty), min_notional=D(min_notional),
                      market_step_size=D(step), market_max_qty=D("100000"))


INSTRUMENTS = [
    instrument("SOLUSDT", "0.01", "0.01", "0.01", "5"),
    instrument("BTCUSDT", "0.1", "0.001", "0.001", "100"),
    instrument("ETHUSDT", "0.01", "0.001", "0.001", "20"),
]


# ---------------------------------------------------------------- 假通知器、假风控闸


class FakeNotifier:
    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def raise_alert(self, level, kind, message, *, dedupe_key=None) -> None:
        self.alerts.append({"level": level, "kind": kind, "message": message, "dedupe_key": dedupe_key})

    def kinds(self) -> list[str]:
        return [a["kind"] for a in self.alerts]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [a for a in self.alerts if a["kind"] == kind]


class FakeRiskGate:
    async def evaluate(self, *, model, action, now_ms) -> RiskDecision:
        return RiskDecision(True)


# ---------------------------------------------------------------- K 线


def frame_of(centers: list[float], *, start_ms: int, half: float = 1.0) -> pd.DataFrame:
    """按中心价序列造已收盘 K 线表（列与 CandleService.frame 相同）；第 i 根的开盘时间 = start_ms + i*15 分钟。"""
    n = len(centers)
    c = pd.Series(centers, dtype="float64")
    return pd.DataFrame({
        "open_time_ms": pd.Series([start_ms + i * TF_MS for i in range(n)], dtype="int64"),
        "open": c, "high": c + half, "low": c - half, "close": c, "volume": pd.Series([1.0] * n, dtype="float64"),
    })


def flat_history(n: int = 40, *, center: float = 100.0, half: float = 1.0, end_ms: int = BAR0) -> pd.DataFrame:
    """n 根中心价不变的 K 线，最后一根在 end_ms 收盘（开盘时间 = end_ms − 15 分钟）。"""
    return frame_of([center] * n, start_ms=end_ms - n * TF_MS, half=half)


def history_then(after: list[float], *, n_history: int = 40, center: float = 100.0, half: float = 1.0) -> pd.DataFrame:
    """n_history 根平的历史（在 BAR0 收盘）+ 持仓所在的那根（BAR0 开盘，中心价 center，不计入窗口）+ after 里的后续 K 线。"""
    hist_start = BAR0 - n_history * TF_MS
    return frame_of([center] * n_history + [center] + list(after), start_ms=hist_start, half=half)


class FakeCandles:
    """只提供 ``frame``：测试里直接设定每个（合约，周期）的已收盘 K 线表。"""

    def __init__(self) -> None:
        self.frames: dict[tuple[str, str], pd.DataFrame] = {}

    def set(self, symbol: str, frame: pd.DataFrame, timeframe: str = TF) -> None:
        self.frames[(symbol, timeframe)] = frame

    def frame(self, symbol: str, timeframe: str) -> pd.DataFrame:
        found = self.frames.get((symbol, timeframe))
        if found is None:
            return frame_of([], start_ms=0)
        return found.copy()


# ---------------------------------------------------------------- 有状态的假交易所


class FakeExchange(FakeAdapter):
    """持仓、条件单、价格都放在属性里；读方法读它们，写方法改它们。要让某次写失败就 ``push_write_result``。"""

    def __init__(self, clock: ManualClock, **kw: Any) -> None:
        super().__init__(**kw)
        self.clock = clock
        self.held: dict[tuple[str, PositionSide], tuple[Decimal, Decimal]] = {}  # (合约, 方向) -> (数量, 均价)
        self.algos: list[AlgoOrderInfo] = []  # 全部条件单，含已结束的
        self.prices: dict[str, tuple[Decimal, Decimal]] = {}  # 合约 -> (最新价, 标记价)
        self.hook: Callable[[str, dict], Awaitable[None]] | None = None
        self._ids = itertools.count(5001)
        self.wire()

    # ------------------------------------------------------------ 状态读写

    def set_price(self, symbol: str, last: str | Decimal, mark: str | Decimal | None = None) -> None:
        self.prices[symbol] = (D(str(last)), D(str(last if mark is None else mark)))

    def set_held(self, symbol: str, side: PositionSide, qty: str | Decimal, entry: str | Decimal = "100") -> None:
        self.held[(symbol, side)] = (D(str(qty)), D(str(entry)))

    def add_stop(self, symbol: str, side: PositionSide, trigger: str | Decimal, *, cid: str | None = "auto",
                 algo_type: AlgoType = AlgoType.STOP_MARKET, close_position: bool = True, qty: str | None = None,
                 working_type: WorkingType = WorkingType.CONTRACT_PRICE, position_side: PositionSide | None = None,
                 raw: str = "NEW") -> AlgoOrderInfo:
        """直接在交易所一侧放一张条件单（不经适配层写方法）。cid="auto" 造系统编号，None 表示没有自定义编号。"""
        n = next(self._ids)
        if cid == "auto":
            cid = ids.make_client_id("TEST", "0", 800 + n, "s")
        view = AlgoOrderInfo(
            symbol=symbol, client_algo_id=cid, algo_id=str(n), side=side.close_side(),
            position_side=position_side or side, algo_type=algo_type, trigger_price=D(str(trigger)),
            working_type=working_type, close_position=close_position, qty=None if qty is None else D(qty),
            raw_status=raw, actual_order_id=None, create_time_ms=self.clock.now_ms(), update_time_ms=self.clock.now_ms())
        self.algos.append(view)
        return view

    def mutate(self, algo: AlgoOrderInfo, **changes: Any) -> AlgoOrderInfo:
        new = replace(algo, update_time_ms=self.clock.now_ms(), **changes)
        self.algos[self.algos.index(algo)] = new
        return new

    def open_algos(self, symbol: str | None = None) -> list[AlgoOrderInfo]:
        return [a for a in self.algos if a.is_open and (symbol is None or a.symbol == symbol)]

    def stops(self, symbol: str = SYM, side: PositionSide = LONG) -> list[AlgoOrderInfo]:
        """某个（合约，方向）上未结的止损类条件单。"""
        return [a for a in self.open_algos(symbol) if a.position_side is side and a.algo_type.is_stop_kind]

    def find(self, symbol: str, cid: str | None = None, algo_id: str | None = None) -> AlgoOrderInfo | None:
        for a in self.algos:
            if a.symbol == symbol and ((cid is not None and a.client_algo_id == cid)
                                       or (algo_id is not None and a.algo_id == algo_id)):
                return a
        return None

    # ------------------------------------------------------------ 读方法

    def wire(self) -> None:
        self.set_read("load_instruments", list(INSTRUMENTS))
        self.set_read("fetch_positions", self._positions)
        self.set_read("fetch_open_orders", lambda symbol=None: [])
        self.set_read("fetch_open_algo_orders",
                      lambda symbol=None: [a for a in self.algos if a.is_open and (symbol is None or a.symbol == symbol)])
        self.set_read("fetch_algo_order", self._algo_order)
        self.set_read("fetch_tickers", self._tickers)
        self.set_read("fetch_leverage", lambda symbol: LeverageInfo(symbol, 2, "isolated"))

    def _positions(self) -> list[PositionInfo]:
        return [PositionInfo(symbol=s, side=side, qty=q, entry_price=e, mark_price=e)
                for (s, side), (q, e) in self.held.items() if q > 0]

    def _algo_order(self, symbol: str, client_algo_id: str | None = None, algo_id: str | None = None):
        return self.find(symbol, client_algo_id, algo_id)  # 找不到是 None -> Known(None)

    def _tickers(self, symbols=None) -> dict[str, Ticker]:
        now = self.clock.now_ms()
        names = list(self.prices) if symbols is None else [s for s in symbols if s in self.prices]
        return {s: Ticker(symbol=s, last=self.prices[s][0], mark=self.prices[s][1], time_ms=now) for s in names}

    # ------------------------------------------------------------ 写方法

    async def _enter(self, name: str, kw: dict) -> None:
        if self.hook is not None:
            await self.hook(name, kw)

    async def _create_stop_order(self, **kw: Any):
        await self._enter("create_stop_order", kw)
        return self._write("create_stop_order", kw, lambda: self._accept_stop(kw))

    def _accept_stop(self, kw: dict) -> WriteAccepted:
        view = self.add_stop(kw["symbol"], kw["position_side"], kw["trigger_price"], cid=kw["client_algo_id"],
                             algo_type=kw["algo_type"], close_position=kw["close_position"],
                             working_type=kw["working_type"])
        return WriteAccepted(algo=view)

    async def _cancel_algo_order(self, **kw: Any):
        await self._enter("cancel_algo_order", kw)
        return self._write("cancel_algo_order", kw, lambda: self._do_cancel(kw))

    def _do_cancel(self, kw: dict) -> CancelResult:
        found = self.find(kw["symbol"], kw.get("client_algo_id"), kw.get("algo_id"))
        if found is None or not found.is_open:
            return CancelResult(CancelOutcome.NOT_FOUND, code=-2011, message="条件单不存在或已结束")
        done = self.mutate(found, raw_status="CANCELED")
        return CancelResult(CancelOutcome.CANCELLED, algo=done)

    async def _create_market_order(self, **kw: Any):
        await self._enter("create_market_order", kw)
        return self._write("create_market_order", kw, lambda: self._fill_market(kw))

    def _fill_market(self, kw: dict) -> WriteAccepted:
        key = (kw["symbol"], kw["position_side"])
        qty, entry = self.held.get(key, (D(0), D(0)))
        done = min(qty, kw["qty"])
        self.held[key] = (qty - done, entry)
        last = self.prices.get(kw["symbol"], (entry, entry))[0]
        return WriteAccepted(order=OrderInfo(
            symbol=kw["symbol"], client_order_id=kw["client_order_id"], exchange_order_id=str(next(self._ids)),
            side=kw["side"], position_side=kw["position_side"], order_type="MARKET", qty=kw["qty"], filled_qty=done,
            avg_price=last, status="FILLED", raw_status="FILLED", close_position=False, reduce_only=False,
            update_time_ms=self.clock.now_ms()))

    # ------------------------------------------------------------ 造写结果

    @staticmethod
    def reject(kind: RejectKind = RejectKind.OTHER, code: int = -2010, msg: str = "被拒绝") -> WriteRejected:
        return WriteRejected(kind, code, msg)

    def crossed(self) -> WriteRejected:
        return WriteRejected(RejectKind.IMMEDIATE_TRIGGER, -2021, "触发价已被越过，会立即触发")

    @staticmethod
    def unknown(reason: str = "请求超时") -> WriteUnknown:
        return WriteUnknown(reason)

    def lost_response(self, reason: str = "响应丢了") -> Callable[..., WriteUnknown]:
        """条件单其实建成了，但应答丢了：结果未知。"""

        def result(**kw: Any) -> WriteUnknown:
            self._accept_stop(kw)
            return WriteUnknown(reason)

        return result


# ---------------------------------------------------------------- 装配


class Rig:
    def __init__(self, ledger, clock: ManualClock, *, live: bool = False, wait_s: float = 3.0, **config: Any) -> None:
        self.ledger = ledger
        self.clock = clock
        self.config = default_config(**config)
        self.bus = EventBus()
        self.events: list = []
        self.bus.subscribe(None, self.events.append, name="rig.collect")
        self.cap = WriteCapability("coordinator")
        self.exchange = FakeExchange(clock, capability=self.cap)
        self.adapter = self.exchange
        self.candles = FakeCandles()
        self.notifier = FakeNotifier()
        self.crash = CrashRegistry(live=live)
        self.modelstate = ModelStateService(ledger, self.bus, clock)
        self.registry = Registry(ledger, self.bus, clock, config_get=self.config.__getitem__)
        self.intents = IntentService(ledger, self.bus, clock, notifier=self.notifier)
        self.instance = "T" + ledger.nonce
        self.orders = OrderService(ledger, self.bus, clock, self.adapter, instance=self.instance)
        self.limits = Limits(clock, config_get=self.config.__getitem__)
        self.rules = InstrumentRules(clock)
        self.coordinator = Coordinator(
            adapter=self.adapter, capability=self.cap, ledger=ledger, bus=self.bus, clock=clock,
            config_get=self.config.__getitem__, modelstate=self.modelstate, registry=self.registry,
            intents=self.intents, orders=self.orders, riskgate=FakeRiskGate(), limits=self.limits, rules=self.rules,
            notifier=self.notifier, crash=self.crash)
        self.protection = self.build_protection(wait_s=wait_s)
        self.exchange.set_price(SYM, "100")
        self.candles.set(SYM, history_then([]))

    def build_protection(self, **overrides: Any) -> Protection:
        parts: dict[str, Any] = dict(
            coordinator=self.coordinator, registry=self.registry, modelstate=self.modelstate, candles=self.candles,
            rules=self.rules, adapter=self.adapter, ledger=self.ledger, bus=self.bus, clock=self.clock,
            config_get=self.config.__getitem__, notifier=self.notifier, crash=self.crash, limits=self.limits)
        parts.update(overrides)
        return Protection(**parts)

    # ------------------------------------------------------------ 生命周期

    async def start(self, *, protection: bool = False) -> "Rig":
        await self.bus.start()
        await self.modelstate.load()
        await self.registry.start()
        await self.intents.start()
        await self.rules.refresh(self.adapter)
        await self.coordinator.start()
        if protection:
            await self.protection.start()
        return self

    async def settle(self) -> None:
        """等协调者的派发任务、事件处理、保护单管理的后台任务都跑完（处理函数可能再发布事件，所以反复几轮）。"""
        for _ in range(4):
            await self.coordinator.idle(10.0)
            await self.bus.drain(10.0)
            await self.protection.idle(10.0)

    def advance(self, ms: int) -> None:
        self.clock.advance(ms)

    async def tick(self, ms: int = 0) -> None:
        """推进时钟，让协调者回查一轮，再驱动保护单管理的定时工作，最后等一切安静。"""
        if ms:
            self.clock.advance(ms)
        await self.coordinator.resolve_unknown_orders()
        await self.protection.tick()
        await self.settle()

    # ------------------------------------------------------------ 造持仓

    async def open_strategy(self, symbol: str = SYM, side: PositionSide = LONG, qty: str = "1", price: str = "100",
                            open_ms: int = OPEN_MS):
        """登记一个托管中的策略仓（保护「无保护」），并让交易所一侧也有这个仓。"""
        self.exchange.set_held(symbol, side, qty, price)
        return await self.registry.register_open(
            symbol=symbol, side=side, qty=D(qty), avg_price=D(price), open_time_ms=open_ms,
            ownership=Ownership.STRATEGY, model="m1", intent_id=1)

    async def open_takeover(self, symbol: str = SYM, side: PositionSide = LONG, qty: str = "1", price: str = "100",
                            open_ms: int = OPEN_MS, signal_exit: bool = False):
        """登记一个接管中的接管仓（保护「无保护」），交易所一侧也有这个仓。"""
        self.exchange.set_held(symbol, side, qty, price)
        return await self.registry.begin_takeover(
            symbol=symbol, side=side, qty=D(qty), avg_price=D(price), open_time_ms=open_ms, open_time_estimated=False,
            anchor_ms=open_ms, signal_exit=signal_exit)

    async def with_system_stop(self, rec, price: str = "97", *, cid: str | None = "auto"):
        """不经保护单管理，直接让交易所有一张系统止损、登记簿记成「系统保护」。返回（持仓记录, 条件单）。"""
        algo = self.exchange.add_stop(rec.symbol, rec.side, price, cid=cid)
        out = await self.registry.set_protection(
            rec.id, state=ProtectionState.SYSTEM, stop_price=D(price), stop_client_id=algo.client_algo_id,
            stop_algo_id=algo.algo_id)
        return out, algo

    # ------------------------------------------------------------ 读

    async def row(self, position_id: int):
        """从账本里读持仓（含已平的）。"""
        rec = await self.registry.fetch(position_id)
        assert rec is not None
        return rec

    def suppressed(self) -> bool:
        return Suppression.UNPROTECTED in self.modelstate.current().suppressions

    def writes(self, name: str) -> list[dict]:
        return self.exchange.write_calls(name)

    def names(self) -> list[str]:
        """适配层收到的写调用名，按顺序。"""
        return [n for n, _ in self.exchange.writes]

    async def intents_of(self, action: str) -> list:
        """账本里某个动作（open/close/stop_place/stop_replace/stop_cancel……）的全部意图，按编号升序。"""
        return [r for r in await self.ledger.read(_all_intents) if r.action.value == action]


def _all_intents(session):
    from sqlalchemy import select

    from dmr_kernel.ledger.models import Intent
    from dmr_kernel.ledger.records import IntentRecord

    return [IntentRecord.from_row(r) for r in session.scalars(select(Intent).order_by(Intent.id))]
