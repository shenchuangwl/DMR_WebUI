"""实时缓存（第 7 层 api）：订阅总线事件，缓存各数据源最近一次的值，供 ``/state``、``/positions``、``/orders`` 读。

只存「收到过的」：一个事件都没收到过的数据源就是 ``None``，由视图层给 ``known=false`` 与原因；这里不编造任何值，也不去问交易所。
- 账户快照、持仓快照、挂单快照：比已缓存的更旧的丢弃（两轮对账重叠时先发起的后返回），同一时刻的后到者生效。
- 对账结果：每种（positions、orders）只留最近一次，不管成败。
- 数据流：最近一次状态；「最近一帧时间」由任何数据流帧（订单、条件单、账户）到达时按时钟记下，或「已连接/已恢复」状态的时刻。
- 持仓模式与交易所限制：目前没有总线事件带这两项，由装配（定时任务读到后）调 ``note_position_mode`` / ``note_restrictions`` 登记。
"""
from __future__ import annotations

from dataclasses import dataclass

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import (
    AccountSnapshotTaken,
    EventBus,
    OrdersSnapshotTaken,
    PositionSnapshotTaken,
    ReconcileCompleted,
    StreamAccountReceived,
    StreamAlgoReceived,
    StreamOrderReceived,
    StreamStatusChanged,
    Subscription,
)
from dmr_kernel.core.types import PositionInfo, PositionSide

__all__ = ["LiveCache", "PositionModeNote", "RestrictionsNote"]

_FRAME_EVENTS = (StreamOrderReceived, StreamAlgoReceived, StreamAccountReceived)
_FRAME_STATES = frozenset({"connected", "recovered_stable"})  # 这两种状态说明刚连上、帧在来
_EVENTS = (AccountSnapshotTaken, PositionSnapshotTaken, OrdersSnapshotTaken, StreamStatusChanged,
           ReconcileCompleted) + _FRAME_EVENTS


@dataclass(frozen=True)
class PositionModeNote:
    dual_side: bool  # True = 对冲模式（双向持仓）
    at_ms: int


@dataclass(frozen=True)
class RestrictionsNote:
    restricted: bool
    detail: tuple[tuple[str, str], ...]
    at_ms: int


class LiveCache:
    def __init__(self, bus: EventBus, clock: Clock) -> None:
        self._bus = bus
        self._clock = clock
        self._sub: Subscription | None = None
        self._account: AccountSnapshotTaken | None = None
        self._positions: PositionSnapshotTaken | None = None
        self._quotes: dict[tuple[str, PositionSide], PositionInfo] = {}
        self._orders: OrdersSnapshotTaken | None = None
        self._stream: StreamStatusChanged | None = None
        self._last_frame_ms: int | None = None
        self._reconcile: dict[str, ReconcileCompleted] = {}
        self._position_mode: PositionModeNote | None = None
        self._restrictions: RestrictionsNote | None = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """订阅总线（重复调用无害）。在事件循环里调用。"""
        if self._sub is None:
            self._sub = self._bus.subscribe(_EVENTS, self._on_event, name="api-livecache")

    def stop(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None

    # ------------------------------------------------------------ 读

    @property
    def account(self) -> AccountSnapshotTaken | None:
        return self._account

    @property
    def positions(self) -> PositionSnapshotTaken | None:
        return self._positions

    @property
    def orders(self) -> OrdersSnapshotTaken | None:
        return self._orders

    @property
    def stream(self) -> StreamStatusChanged | None:
        return self._stream

    @property
    def last_frame_ms(self) -> int | None:
        return self._last_frame_ms

    @property
    def position_mode(self) -> PositionModeNote | None:
        return self._position_mode

    @property
    def restrictions(self) -> RestrictionsNote | None:
        return self._restrictions

    def reconcile(self, kind: str) -> ReconcileCompleted | None:
        return self._reconcile.get(kind)

    def position_quote(self, symbol: str, side: PositionSide) -> PositionInfo | None:
        """最近一次持仓快照里这一侧的读数（标记价、浮动盈亏等）；没有快照或数量为 0 都是 None。"""
        return self._quotes.get((symbol, PositionSide(side)))

    # ------------------------------------------------------------ 装配登记（没有对应的总线事件）

    def note_frame(self, at_ms: int | None) -> None:
        """登记数据流自己记的最近一帧时间（含心跳帧，心跳不发业务事件）；装配层每 5 秒调用一次。只前进不后退。"""
        if at_ms is not None:
            self._stamp_frame(int(at_ms))

    def note_position_mode(self, dual_side: bool, at_ms: int | None = None) -> None:
        self._position_mode = PositionModeNote(bool(dual_side), self._clock.now_ms() if at_ms is None else at_ms)

    def note_restrictions(self, restricted: bool, detail: tuple[tuple[str, str], ...] = (),
                          at_ms: int | None = None) -> None:
        self._restrictions = RestrictionsNote(bool(restricted), tuple(detail),
                                              self._clock.now_ms() if at_ms is None else at_ms)

    # ------------------------------------------------------------ 事件

    def _on_event(self, event: object) -> None:
        if isinstance(event, AccountSnapshotTaken):
            if self._account is None or event.at_ms >= self._account.at_ms:
                self._account = event
        elif isinstance(event, PositionSnapshotTaken):
            if self._positions is None or event.at_ms >= self._positions.at_ms:
                self._positions = event
                self._quotes = {(info.symbol, PositionSide(info.side)): info for info in event.positions
                                if info.qty != 0}
        elif isinstance(event, OrdersSnapshotTaken):
            if self._orders is None or event.at_ms >= self._orders.at_ms:
                self._orders = event
        elif isinstance(event, StreamStatusChanged):
            self._stream = event
            if event.state in _FRAME_STATES:
                self._stamp_frame(event.at_ms)
        elif isinstance(event, ReconcileCompleted):
            self._reconcile[event.kind] = event
        elif isinstance(event, _FRAME_EVENTS):
            self._stamp_frame(self._clock.now_ms())

    def _stamp_frame(self, at_ms: int) -> None:
        if self._last_frame_ms is None or at_ms > self._last_frame_ms:
            self._last_frame_ms = at_ms
