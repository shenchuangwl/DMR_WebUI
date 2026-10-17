"""订单与条件单（orders 表）的仓储函数。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录或基本类型。

**订单状态的单调合并只写在 ``merge_view`` 这一处**：订单服务（``core.orders``，收到写结果、撤单结果、回查结果时）
和入账（``core.ingest``，收到数据流订单事件、成交时）都调用它，避免两处规则不一致。规则：

1. 已到终态的不再回到非终态（FILLED/CANCELLED/REJECTED 之间只有「证据更强」的升级：成交量等于数量或交易所报
   FILLED → FILLED；被误判为拒绝的订单后来有了成交 → CANCELLED）；
2. ``filled_qty`` 只增不减，均价跟着成交量最大的那份数据走；
3. 有成交且成交量不小于数量 → FILLED；有成交但状态还停在未确认 → PARTIALLY_FILLED；
4. 交易所报 EXPIRED/EXPIRED_IN_MATCH/CANCELED → CANCELLED，已成交部分照记；
5. ``raw_status`` 记最新的交易所原始状态，「最新」按订单的进展程度判断（NEW < 部分成交 < 作废类 < 全部成交）：
   乱序到达的旧状态不会把它改回去，所以先 EXPIRED 后 TRADE 与先 TRADE 后 EXPIRED 的结果一致；
6. 撤单中（CANCEL_PENDING）不会被撤单之前生成的 NEW、PARTIALLY_FILLED 视图改回去，只被终态改写。

本模块不导入 ``exchange``（同层不同组）：订单编号的解析在上层做好再传进来。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import literal_column, select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import (
    AlgoOrderInfo,
    OrderInfo,
    OrderPurpose,
    OrderSide,
    OrderState,
    PositionSide,
    WorkingType,
)
from dmr_kernel.ledger import repo_audit
from dmr_kernel.ledger.models import Order
from dmr_kernel.ledger.records import OrderRecord

S = OrderState
TERMINAL = frozenset(OrderState.terminal())
ZERO = Decimal("0")
ROWID = literal_column("orders.rowid")  # 同一毫秒建的订单按建立顺序排

# 交易所原始状态 → 订单状态。普通订单与条件单（Algo 接口）的状态词汇不同。
_REGULAR_STATE = {
    "NEW": S.ACKNOWLEDGED, "PARTIALLY_FILLED": S.PARTIALLY_FILLED, "FILLED": S.FILLED,
    "CANCELED": S.CANCELLED, "CANCELLED": S.CANCELLED, "EXPIRED": S.CANCELLED, "EXPIRED_IN_MATCH": S.CANCELLED,
    "REJECTED": S.REJECTED,
}
_ALGO_STATE = {
    "NEW": S.ACKNOWLEDGED, "TRIGGERING": S.ACKNOWLEDGED, "TRIGGERED": S.ACKNOWLEDGED, "FINISHED": S.FILLED,
    "CANCELED": S.CANCELLED, "CANCELLED": S.CANCELLED, "EXPIRED": S.CANCELLED, "REJECTED": S.REJECTED,
}
# 非终态的进展顺序（CANCEL_PENDING 是我们自己发起撤单后的旁支，不在顺序里）
_STATE_RANK = {S.PENDING_SEND: 0, S.SENT_UNKNOWN: 1, S.ACKNOWLEDGED: 2, S.PARTIALLY_FILLED: 3}
_UNCONFIRMED = (S.PENDING_SEND, S.SENT_UNKNOWN, S.ACKNOWLEDGED)
# 原始状态的进展程度；没见过的状态按 1 处理
_RAW_RANK = {
    "NEW": 0, "TRIGGERING": 1, "PENDING_CANCEL": 1, "PARTIALLY_FILLED": 2, "TRIGGERED": 2,
    "CANCELED": 3, "CANCELLED": 3, "EXPIRED": 3, "EXPIRED_IN_MATCH": 3, "REJECTED": 3,
    "FILLED": 4, "FINISHED": 4,
}
_RAW_TERMINAL_RANK = 3


def _target_state(view: OrderInfo | AlgoOrderInfo) -> OrderState | None:
    """视图对应的订单状态；认不出的状态返回 None（只记原始状态，不改订单状态）。"""
    if isinstance(view, AlgoOrderInfo):
        return _ALGO_STATE.get(view.raw_status)
    return _REGULAR_STATE.get(view.raw_status) or _REGULAR_STATE.get(view.status)


def _next_state(old: OrderState, target: OrderState | None, filled: Decimal, qty: Decimal | None) -> OrderState:
    state = old
    if old in TERMINAL:
        if target is S.FILLED:  # 交易所明确说全部成交：比先前记的撤销或拒绝更有说服力
            state = S.FILLED
    elif target is not None:
        if target in TERMINAL:
            state = target
        elif old is not S.CANCEL_PENDING and _STATE_RANK[target] > _STATE_RANK[old]:
            state = target
    if filled > ZERO:
        if qty is not None and filled >= qty:
            state = S.FILLED
        elif state in _UNCONFIRMED:
            state = S.PARTIALLY_FILLED
        elif state is S.REJECTED:
            state = S.CANCELLED  # 被判为拒绝的订单后来有了成交：成交证明它存在过
    return state


def _positive(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value > ZERO else None


def _pick_average(old: tuple[Decimal, Decimal | None], fills: tuple[Decimal, Decimal | None] | None,
                  view: tuple[Decimal, Decimal | None] | None) -> Decimal | None:
    """均价取自成交量最大的那份数据；成交量相同时交易所视图优先，其次是自己汇总的成交，最后才是旧值。"""
    best: tuple[Decimal, Decimal | None] | None = None
    for candidate in (old, fills, view):
        if candidate is None or candidate[1] is None:
            continue
        if best is None or candidate[0] >= best[0]:
            best = candidate
    return None if best is None else best[1]


def _newer_raw(old: str | None, new: str | None) -> bool:
    if new is None:
        return False
    if old is None:
        return True
    rank_old, rank_new = _RAW_RANK.get(old, 1), _RAW_RANK.get(new, 1)
    if rank_new != rank_old:
        return rank_new > rank_old
    return rank_new < _RAW_TERMINAL_RANK  # 同一档的终态原始状态不来回改（先到的留着）


def create_pending(session: Session, *, client_order_id: str, intent_id: int | None, leg: str, purpose: OrderPurpose,
                   symbol: str, side: OrderSide, position_side: PositionSide, kind: str, order_type: str,
                   qty: Decimal | None, trigger_price: Decimal | None, working_type: WorkingType | None,
                   close_position: bool, resend_of: str | None, now_ms: int) -> tuple[OrderRecord, bool]:
    """先落盘：新建 PENDING_SEND 的订单。编号已存在则不写，返回 ``(已有记录, False)``。"""
    existing = session.get(Order, client_order_id)
    if existing is not None:
        return OrderRecord.from_row(existing), False
    row = Order(client_order_id=client_order_id, intent_id=intent_id, leg=leg, purpose=purpose, symbol=symbol,
                side=side, position_side=position_side, kind=kind, order_type=order_type, qty=qty,
                trigger_price=trigger_price, working_type=working_type, close_position=close_position,
                exchange_order_id=None, algo_id=None, actual_order_id=None, state=S.PENDING_SEND, raw_status=None,
                filled_qty=ZERO, avg_price=None, error_code=None, error_msg=None, sent_ms=None, acked_ms=None,
                last_query_ms=None, absent_count=0, resend_of=resend_of, created_ms=now_ms, updated_ms=now_ms)
    session.add(row)
    session.flush()
    return OrderRecord.from_row(row), True


def _touch(session: Session, row: Order, before: OrderRecord, changes: dict[str, Any],
           now_ms: int) -> tuple[OrderRecord, OrderRecord]:
    """只写真正变了的列；一列都没变就不动 ``updated_ms``。"""
    changed = {name: value for name, value in changes.items() if getattr(row, name) != value}
    if changed:
        for name, value in changed.items():
            setattr(row, name, value)
        row.updated_ms = now_ms
        session.flush()
        return before, OrderRecord.from_row(row)
    return before, before


def merge_view(session: Session, client_order_id: str, view: OrderInfo | AlgoOrderInfo | None, *, now_ms: int,
               fills_qty: Decimal | None = None, fills_avg: Decimal | None = None,
               target: OrderState | None = None, queried: bool = False) -> tuple[OrderRecord, OrderRecord] | None:
    """把一份新的信息单调地并进订单。返回 ``(合并前, 合并后)``；没有这张订单返回 None。

    - ``view``：交易所的订单或条件单视图（None 表示这次没有视图，只有下面的成交汇总或明确的结论）；
    - ``fills_qty``/``fills_avg``：账本里这张订单已入账成交的合计数量与加权均价；
    - ``target``：调用方已经确认的结论（受理但没有视图 → ACKNOWLEDGED；撤单成功 → CANCELLED），视图能认出状态时以视图为准；
    - ``queried``：这是一次主动回查（记 ``last_query_ms``）。
    """
    row = session.get(Order, client_order_id)
    if row is None:
        return None
    before = OrderRecord.from_row(row)
    incoming = target
    if view is not None:
        incoming = _target_state(view) or target
    view_fill: tuple[Decimal, Decimal | None] | None = None
    if isinstance(view, OrderInfo):
        view_fill = (view.filled_qty, _positive(view.avg_price))
    fill_part = None if fills_qty is None else (fills_qty, _positive(fills_avg))
    filled = max([row.filled_qty] + [part[0] for part in (view_fill, fill_part) if part is not None])
    average = _pick_average((row.filled_qty, row.avg_price), fill_part, view_fill) if filled > ZERO else row.avg_price
    state = _next_state(row.state, incoming, filled, row.qty)

    changes: dict[str, Any] = {}
    if state is not row.state:
        changes["state"] = state
    if filled != row.filled_qty:
        changes["filled_qty"] = filled
    if average != row.avg_price:
        changes["avg_price"] = average
    if view is not None and _newer_raw(row.raw_status, view.raw_status):
        changes["raw_status"] = view.raw_status
    if isinstance(view, OrderInfo) and view.exchange_order_id and row.exchange_order_id is None:
        changes["exchange_order_id"] = view.exchange_order_id
    if isinstance(view, AlgoOrderInfo):
        if view.algo_id and row.algo_id is None:
            changes["algo_id"] = view.algo_id
        if view.actual_order_id and row.actual_order_id is None:
            changes["actual_order_id"] = view.actual_order_id
    if row.acked_ms is None and state not in (S.PENDING_SEND, S.SENT_UNKNOWN, S.REJECTED):
        changes["acked_ms"] = now_ms  # 交易所确认过这张订单存在
    exists = view is not None or target is not None or filled > ZERO
    if exists and row.absent_count:
        changes["absent_count"] = 0  # 连续「不存在」的次数被一份真实的信息打断
    if queried and row.last_query_ms != now_ms:
        changes["last_query_ms"] = now_ms
    return _touch(session, row, before, changes, now_ms)


def note_absent(session: Session, client_order_id: str, *, now_ms: int) -> tuple[OrderRecord, OrderRecord] | None:
    """交易所明确答复「不存在」：连续次数 +1。不改订单状态；已到终态的订单只记回查时间（它已经被确认存在过）。"""
    row = session.get(Order, client_order_id)
    if row is None:
        return None
    before = OrderRecord.from_row(row)
    changes: dict[str, Any] = {"last_query_ms": now_ms}
    if row.state not in TERMINAL:
        changes["absent_count"] = row.absent_count + 1
    return _touch(session, row, before, changes, now_ms)


def note_query_unknown(session: Session, client_order_id: str, *,
                       now_ms: int) -> tuple[OrderRecord, OrderRecord] | None:
    """回查的结果是「未知」：只记查询时间，不动状态，也不动「不存在」的计数。"""
    row = session.get(Order, client_order_id)
    if row is None:
        return None
    before = OrderRecord.from_row(row)
    return _touch(session, row, before, {"last_query_ms": now_ms}, now_ms)


def record_sent(session: Session, client_order_id: str, *, now_ms: int) -> tuple[OrderRecord, OrderRecord] | None:
    """记下首次发出请求的时间（只对还没有任何回音的 PENDING_SEND 订单，且只记一次）。"""
    row = session.get(Order, client_order_id)
    if row is None:
        return None
    before = OrderRecord.from_row(row)
    changes = {"sent_ms": now_ms} if row.state is S.PENDING_SEND and row.sent_ms is None else {}
    return _touch(session, row, before, changes, now_ms)


def mark_unknown(session: Session, client_order_id: str, *, now_ms: int, error_code: int | None,
                 error_msg: str) -> tuple[OrderRecord, OrderRecord] | None:
    """写请求的结果未知：PENDING_SEND → SENT_UNKNOWN。已经有回音（数据流先到了）的订单不倒退。"""
    row = session.get(Order, client_order_id)
    if row is None:
        return None
    before = OrderRecord.from_row(row)
    changes: dict[str, Any] = {}
    if row.state is S.PENDING_SEND:
        changes = {"state": S.SENT_UNKNOWN, "error_code": error_code, "error_msg": error_msg}
    return _touch(session, row, before, changes, now_ms)


def mark_cancel_unknown(session: Session, client_order_id: str, *, now_ms: int, error_code: int | None,
                        error_msg: str) -> tuple[OrderRecord, OrderRecord] | None:
    """撤单结果未知：已确认存在且未结束的订单 → CANCEL_PENDING，等回查。"""
    row = session.get(Order, client_order_id)
    if row is None:
        return None
    before = OrderRecord.from_row(row)
    changes: dict[str, Any] = {}
    if row.state in (S.ACKNOWLEDGED, S.PARTIALLY_FILLED):
        changes = {"state": S.CANCEL_PENDING, "error_code": error_code, "error_msg": error_msg}
    return _touch(session, row, before, changes, now_ms)


def reject(session: Session, client_order_id: str, *, now_ms: int, error_code: int | None, error_msg: str,
           from_states: frozenset[OrderState] | None, audit: bool) -> tuple[OrderRecord, OrderRecord] | None:
    """未到终态、没有任何成交、且（给了 ``from_states`` 时）当前状态在其中的订单 → REJECTED。

    有成交就证明订单存在，不能记为拒绝；已到终态的不改。``audit=True`` 时同一事务写审计记录（人为的判定）。
    """
    row = session.get(Order, client_order_id)
    if row is None:
        return None
    before = OrderRecord.from_row(row)
    allowed = (row.state not in TERMINAL and row.filled_qty == ZERO
               and (from_states is None or row.state in from_states))
    if not allowed:
        return before, before
    result = _touch(session, row, before, {"state": S.REJECTED, "error_code": error_code, "error_msg": error_msg},
                    now_ms)
    if audit:
        repo_audit.append(session, at_ms=now_ms, operator="system", action="order.mark_rejected",
                          target=client_order_id, before={"state": before.state.value},
                          after={"state": S.REJECTED.value, "reason": error_msg}, result="ok")
    return result


def link_actual_order(session: Session, algo_id: str, actual_order_id: str, *,
                      now_ms: int) -> tuple[OrderRecord, OrderRecord] | None:
    """条件单触发后生成的订单号。已经关联过的不改；不是我们的条件单返回 None。"""
    row = session.scalars(select(Order).where(Order.algo_id == algo_id).order_by(Order.created_ms, ROWID)).first()
    if row is None:
        return None
    before = OrderRecord.from_row(row)
    changes = {"actual_order_id": actual_order_id} if row.actual_order_id is None else {}
    return _touch(session, row, before, changes, now_ms)


# ---------------------------------------------------------------- 查询

def get(session: Session, client_order_id: str) -> OrderRecord | None:
    row = session.get(Order, client_order_id)
    return None if row is None else OrderRecord.from_row(row)


def _first(session: Session, column: Any, value: str) -> OrderRecord | None:
    row = session.scalars(select(Order).where(column == value).order_by(Order.created_ms, ROWID)).first()
    return None if row is None else OrderRecord.from_row(row)


def by_exchange_order_id(session: Session, exchange_order_id: str) -> OrderRecord | None:
    return _first(session, Order.exchange_order_id, exchange_order_id)


def by_algo_id(session: Session, algo_id: str) -> OrderRecord | None:
    return _first(session, Order.algo_id, algo_id)


def by_actual_order_id(session: Session, actual_order_id: str) -> OrderRecord | None:
    return _first(session, Order.actual_order_id, actual_order_id)


def for_intent(session: Session, intent_id: int) -> list[OrderRecord]:
    stmt = select(Order).where(Order.intent_id == intent_id).order_by(Order.created_ms, ROWID)
    return [OrderRecord.from_row(row) for row in session.scalars(stmt)]


def non_terminal(session: Session, *, symbol: str | None = None, side: PositionSide | OrderSide | None = None,
                 purpose: OrderPurpose | None = None) -> list[OrderRecord]:
    """未到终态的订单（按建立顺序）。``side`` 给持仓方向就按持仓方向筛，给买卖方向就按买卖方向筛。"""
    stmt = select(Order).where(Order.state.not_in(sorted(TERMINAL, key=lambda s: s.value)))
    if symbol is not None:
        stmt = stmt.where(Order.symbol == symbol)
    if isinstance(side, PositionSide):
        stmt = stmt.where(Order.position_side == side)
    elif isinstance(side, OrderSide):
        stmt = stmt.where(Order.side == side)
    elif side is not None:
        raise TypeError(f"side 必须是 PositionSide 或 OrderSide：{side!r}")
    if purpose is not None:
        stmt = stmt.where(Order.purpose == purpose)
    return [OrderRecord.from_row(row) for row in session.scalars(stmt.order_by(Order.created_ms, ROWID))]


def requery_candidates(session: Session, *, now_ms: int, pending_after_ms: int) -> list[OrderRecord]:
    """命运未定、需要按编号回查的订单：SENT_UNKNOWN、CANCEL_PENDING，以及落盘后超过 ``pending_after_ms``
    （从发出时间算，没记发出时间的从建立时间算）仍是 PENDING_SEND 的。"""
    stmt = select(Order).where(Order.state.in_([S.SENT_UNKNOWN, S.CANCEL_PENDING, S.PENDING_SEND]))
    found: list[OrderRecord] = []
    for row in session.scalars(stmt.order_by(Order.created_ms, ROWID)):
        if row.state is S.PENDING_SEND:
            since = row.sent_ms if row.sent_ms is not None else row.created_ms
            if now_ms - since <= pending_after_ms:
                continue
        found.append(OrderRecord.from_row(row))
    return found
