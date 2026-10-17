"""内核内部的事件总线（第 0b 层）与事件类型。

同层模块之间不直接互相导入，通过这里的事件通知（计划报告 4.3）。总线不承载业务逻辑。

- ``publish`` 不阻塞：事件放进每个匹配订阅者自己的队列；订阅者按发布顺序逐个处理。
- 处理函数可以是普通函数或协程；抛出的异常记日志，不影响该订阅者处理后续事件。
- ``drain`` 等到所有队列为空且没有正在处理的事件（测试与关停时用）。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable

from dmr_kernel.core.types import (
    AlertLevel,
    AlgoOrderInfo,
    OrderInfo,
    OrderOrigin,
    PositionInfo,
    PositionSide,
    StreamAccount,
    StreamAlgo,
    StreamOrder,
    TradeInfo,
)

log = logging.getLogger(__name__)

Handler = Callable[["Event"], Awaitable[None] | None]


@dataclass(frozen=True)
class Event:
    """所有内部事件的基类。"""


class Subscription:
    def __init__(self, bus: "EventBus", name: str, types: tuple[type, ...] | None, handler: Handler):
        self.bus = bus
        self.name = name
        self.types = types
        self.handler = handler
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.busy = False
        self.cancelled = False

    def matches(self, event: Event) -> bool:
        return self.types is None or isinstance(event, self.types)

    def cancel(self) -> None:
        self.cancelled = True
        self.bus._remove(self)
        if self.task is not None:
            self.task.cancel()

    async def run(self) -> None:
        while True:
            event = await self.queue.get()
            self.busy = True
            try:
                result = self.handler(event)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("事件处理失败：订阅者=%s 事件=%s", self.name, type(event).__name__)
            finally:
                self.busy = False
                self.queue.task_done()


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._started = False

    def subscribe(self, types: type | tuple[type, ...] | None, handler: Handler, *, name: str) -> Subscription:
        if types is not None and not isinstance(types, tuple):
            types = (types,)
        sub = Subscription(self, name, types, handler)
        self._subs.append(sub)
        if self._started:
            sub.task = asyncio.get_running_loop().create_task(sub.run(), name=f"bus:{name}")
        return sub

    def _remove(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def publish(self, event: Event) -> None:
        for sub in list(self._subs):
            if not sub.cancelled and sub.matches(event):
                sub.queue.put_nowait(event)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        for sub in self._subs:
            if sub.task is None:
                sub.task = loop.create_task(sub.run(), name=f"bus:{sub.name}")

    async def drain(self, timeout: float = 10.0) -> None:
        async def settled() -> None:
            while True:
                pending = [s for s in self._subs if not s.queue.empty() or s.busy]
                if not pending:
                    # 处理函数可能又发布了新事件：再让出一次确认真的空了
                    await asyncio.sleep(0)
                    if not [s for s in self._subs if not s.queue.empty() or s.busy]:
                        return
                for s in pending:
                    await s.queue.join()

        await asyncio.wait_for(settled(), timeout)

    async def stop(self) -> None:
        tasks = [s.task for s in self._subs if s.task is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for s in self._subs:
            s.task = None
        self._started = False


# ====================================================================== 事件类型
# 账户数据流（exchange.stream 发布，core.ingest 等订阅）


@dataclass(frozen=True)
class StreamStatusChanged(Event):
    """数据流连接状态：connected | interrupted | recovered_stable | stopped。"""

    state: str
    reason: str
    at_ms: int


@dataclass(frozen=True)
class StreamOrderReceived(Event):
    payload: StreamOrder


@dataclass(frozen=True)
class StreamAlgoReceived(Event):
    payload: StreamAlgo


@dataclass(frozen=True)
class StreamAccountReceived(Event):
    payload: StreamAccount


@dataclass(frozen=True)
class ListenKeyExpired(Event):
    at_ms: int


# 入账（core.ingest 发布）


@dataclass(frozen=True)
class FillIngested(Event):
    """一笔成交已去重入账。origin 按编号识别；system 成交带 intent_id 与订单用途。"""

    fill_id: int
    trade: TradeInfo
    client_order_id: str | None
    origin: OrderOrigin
    intent_id: int | None
    order_purpose: str | None
    path: str  # stream | rest
    at_ms: int
    # 成交落在一张我们判为拒绝或撤销的订单上、并带来了新的成交量（入账侧据合并前后的订单判定）：真正的「晚到成交」
    on_dead_order: bool = False


@dataclass(frozen=True)
class IncomeIngested(Event):
    income_id: str
    income_type: str
    symbol: str | None
    amount: Decimal
    at_ms: int


@dataclass(frozen=True)
class OrderStateChanged(Event):
    client_order_id: str
    state: str
    intent_id: int | None
    symbol: str
    position_side: PositionSide
    at_ms: int


@dataclass(frozen=True)
class AlgoOrderObserved(Event):
    """看到一张条件单的新状态（数据流事件或条件单对账）。"""

    algo: AlgoOrderInfo
    source: str  # stream | reconcile | query
    at_ms: int


# 对账与持仓（core.reconcile、core.registry 发布）


@dataclass(frozen=True)
class PositionSnapshotTaken(Event):
    positions: tuple[PositionInfo, ...]
    server_time_ms: int
    at_ms: int


@dataclass(frozen=True)
class UnregisteredPositionFound(Event):
    """交易所有仓而登记簿没有登记（或登记数量对不上且有外部成交可解释）。由模型 2 归类。"""

    symbol: str
    side: PositionSide
    qty: Decimal
    entry_price: Decimal
    trigger: str  # external_fill | reconcile | qty_mismatch | recovery
    at_ms: int


@dataclass(frozen=True)
class PositionRegistered(Event):
    position_id: int
    symbol: str
    side: PositionSide
    ownership: str
    status: str
    at_ms: int


@dataclass(frozen=True)
class PositionChanged(Event):
    """数量、状态、保护或归属有变化；version 是新的持仓状态版本。"""

    position_id: int
    symbol: str
    side: PositionSide
    cause: str
    version: int
    at_ms: int


@dataclass(frozen=True)
class PositionClosed(Event):
    position_id: int
    symbol: str
    side: PositionSide
    exit_reason: str
    at_ms: int


@dataclass(frozen=True)
class ProtectionChanged(Event):
    position_id: int
    state: str
    stop_price: Decimal | None
    at_ms: int


@dataclass(frozen=True)
class ReconcileCompleted(Event):
    kind: str  # positions | orders
    status: str  # ok | unknown | mismatch
    at_ms: int


@dataclass(frozen=True)
class OrdersSnapshotTaken(Event):
    """挂单与条件单对账的快照。保护单管理据此核对系统止损是否还在、外部保护是否仍然有效。"""

    open_orders: tuple[OrderInfo, ...]
    algo_orders: tuple[AlgoOrderInfo, ...]
    at_ms: int


@dataclass(frozen=True)
class AccountSnapshotTaken(Event):
    """持仓对账时读回的账户权益（风控闸的数据来源：4.6「账户权益取持仓对账时读回的值」）。"""

    equity: Decimal
    available: Decimal
    wallet: Decimal
    at_ms: int


@dataclass(frozen=True)
class RiskStateChanged(Event):
    """风控闸状态变化。account_tripped 由 core.guard 映射成「风控触发」抑制；model_tripped 只停模型 1 新开仓。"""

    account_tripped: bool
    model_tripped: bool
    reasons: tuple[str, ...]
    at_ms: int


# 行情、名单、状态、操作、告警、配置


@dataclass(frozen=True)
class CandlesReady(Event):
    """某周期的一根 K 线已收盘且已取到。bar_open_ms 是这根已收盘 K 线的开盘时间。"""

    timeframe: str
    bar_open_ms: int
    symbols: tuple[str, ...]
    at_ms: int


@dataclass(frozen=True)
class ListSnapshotUpdated(Event):
    snapshot_id: int
    valid: bool
    at_ms: int


@dataclass(frozen=True)
class ModelStateChanged(Event):
    version: int
    at_ms: int


@dataclass(frozen=True)
class IntentStateChanged(Event):
    intent_id: int
    state: str
    action: str
    symbol: str
    side: PositionSide
    at_ms: int


@dataclass(frozen=True)
class OperationProgress(Event):
    op_id: str
    op_type: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    at_ms: int = 0


@dataclass(frozen=True)
class AlertRaised(Event):
    alert_id: int
    level: AlertLevel
    kind: str
    message: str
    at_ms: int


@dataclass(frozen=True)
class ConfigChanged(Event):
    key: str
    at_ms: int
