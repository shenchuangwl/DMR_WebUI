"""对账（core.reconcile）用的仓储函数。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录或基本类型。

- ``record_round``：一轮对账的记录（``reconcile_run``）连同这一轮新发现、有变化的差异（``reconcile_diff``）与已解决的差异，
  在同一个事务里写完。同一个「合约 + 方向 + 种类」同一时刻只有一行未解决的差异：再次发现就更新这一行
  （保留最先发现的那一轮的编号），解决之后再发现是新的一行。
- ``list_diffs`` / ``recent_runs``：读。
- ``confirm_mismatch``：人工确认「对账不一致」：把未解决的数量差异记为已解决，并写一条审计记录。
- ``cancel_key_state``：孤儿条件单、待清理旧止损的撤单意图用的幂等键：第一次 ``<base>``，之后每多一次终态的尝试
  加一个序号（``<base>:1``…），上一次还没结束就不再提交（同时给出那次尝试的键、状态与最后变化时间），试满上限就放弃。
- ``algo_in_flight``：是否有未到终态的意图在引用某张条件单（订单行所属的意图，或意图参数里点名的旧单）；
  ``exclude_key`` 给了就不算以它开头的撤单尝试（调用方自己的）。
- ``system_fills_pending``：该「合约 + 方向」上有没有系统订单已成交、成交明细却还没入账（登记簿暂时没有这个仓，
  不能因此把它判成外部持仓）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import IntentState, PositionSide
from dmr_kernel.ledger import repo_audit, repo_intents
from dmr_kernel.ledger.models import Fill, Intent, Order, ReconcileDiff, ReconcileRun
from dmr_kernel.ledger.records import ReconcileDiffRecord, ReconcileRunRecord

QTY_MISMATCH = "qty_mismatch"  # 登记仓的数量与交易所对不上，补拉成交后仍无法解释
UNREGISTERED = "unregistered"  # 交易所有仓而登记簿没有登记

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
_TERMINAL = frozenset(IntentState.terminal())


@dataclass(frozen=True)
class DiffSpec:
    symbol: str
    side: PositionSide | None
    kind: str
    local_qty: Decimal | None
    exchange_qty: Decimal | None
    note: str = ""


@dataclass(frozen=True)
class ResolveSpec:
    """把这个「合约 + 方向」上未解决的差异记为已解决；``kinds`` 为空表示所有种类。"""

    symbol: str
    side: PositionSide | None
    kinds: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class CancelKeyState:
    next_key: str | None  # 这次可以用的幂等键；上一次还没结束或已试满则是 None
    attempts: int  # 已经结束的尝试次数
    in_flight: bool  # 上一次尝试还没到终态
    exhausted: bool  # 已试满上限
    pending_key: str | None = None  # 还没结束的那次尝试的幂等键（in_flight 时）
    pending_state: IntentState | None = None  # 它的状态
    pending_since_ms: int | None = None  # 它最后一次状态变化的时间


# ---------------------------------------------------------------- 对账记录与差异


def _open_row(session: Session, symbol: str, side: PositionSide | None, kind: str) -> ReconcileDiff | None:
    stmt = select(ReconcileDiff).where(ReconcileDiff.symbol == symbol, ReconcileDiff.kind == kind,
                                       ReconcileDiff.resolved_ms.is_(None))
    stmt = stmt.where(ReconcileDiff.side.is_(None) if side is None else ReconcileDiff.side == side)
    return session.scalars(stmt.order_by(ReconcileDiff.id)).first()


def _resolve(session: Session, spec: ResolveSpec, at_ms: int) -> int:
    stmt = select(ReconcileDiff).where(ReconcileDiff.symbol == spec.symbol, ReconcileDiff.resolved_ms.is_(None))
    stmt = stmt.where(ReconcileDiff.side.is_(None) if spec.side is None else ReconcileDiff.side == spec.side)
    if spec.kinds:
        stmt = stmt.where(ReconcileDiff.kind.in_(spec.kinds))
    count = 0
    for row in session.scalars(stmt):
        row.resolved_ms = at_ms
        if spec.note:
            row.note = f"{row.note}；{spec.note}" if row.note else spec.note
        count += 1
    session.flush()
    return count


def record_round(session: Session, *, at_ms: int, kind: str, status: str, duration_ms: int | None, detail: str,
                 diffs: Sequence[DiffSpec] = (), resolves: Sequence[ResolveSpec] = ()
                 ) -> tuple[ReconcileRunRecord, list[ReconcileDiffRecord]]:
    """写一轮对账的记录。先处理 ``resolves``，再写 ``diffs``（同一个键在同一轮先解决后又发现，得到新的一行）。
    返回（对账记录，本轮写入或更新的差异行）。"""
    run = ReconcileRun(at_ms=at_ms, kind=kind, status=status, duration_ms=duration_ms, detail=detail)
    session.add(run)
    session.flush()
    for spec in resolves:
        _resolve(session, spec, at_ms)
    written: list[ReconcileDiffRecord] = []
    for spec in diffs:
        row = _open_row(session, spec.symbol, spec.side, spec.kind)
        if row is None:
            row = ReconcileDiff(run_id=run.id, symbol=spec.symbol, side=spec.side, local_qty=spec.local_qty,
                                exchange_qty=spec.exchange_qty, kind=spec.kind, resolved_ms=None, note=spec.note)
            session.add(row)
        else:
            row.local_qty, row.exchange_qty, row.note = spec.local_qty, spec.exchange_qty, spec.note
        session.flush()
        written.append(ReconcileDiffRecord.from_row(row))
    return ReconcileRunRecord.from_row(run), written


def list_diffs(session: Session, *, open_only: bool = True) -> list[ReconcileDiffRecord]:
    stmt = select(ReconcileDiff)
    if open_only:
        stmt = stmt.where(ReconcileDiff.resolved_ms.is_(None))
    return [ReconcileDiffRecord.from_row(row) for row in session.scalars(stmt.order_by(ReconcileDiff.id))]


def recent_runs(session: Session, *, kind: str | None = None, limit: int = DEFAULT_LIMIT) -> list[ReconcileRunRecord]:
    """按编号从新到旧取对账记录。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_LIMIT} 之间的整数：{limit!r}")
    stmt = select(ReconcileRun)
    if kind is not None:
        stmt = stmt.where(ReconcileRun.kind == kind)
    return [ReconcileRunRecord.from_row(row) for row in session.scalars(stmt.order_by(ReconcileRun.id.desc()).limit(limit))]


def confirm_mismatch(session: Session, *, at_ms: int, operator: str) -> int:
    """人工确认「对账不一致」：未解决的数量差异记为已解决（只动数量差异，未登记持仓的记录不动），同事务写审计。
    返回解决的行数。"""
    rows = session.scalars(select(ReconcileDiff).where(ReconcileDiff.kind == QTY_MISMATCH,
                                                       ReconcileDiff.resolved_ms.is_(None))).all()
    for row in rows:
        row.resolved_ms = at_ms
        row.note = f"{row.note}；人工确认（{operator}）" if row.note else f"人工确认（{operator}）"
    session.flush()
    repo_audit.append(session, at_ms=at_ms, operator=operator, action="reconcile.confirm_mismatch", target="reconcile",
                      before={"open": len(rows)}, after={"resolved": len(rows)}, result="ok")
    return len(rows)


# ---------------------------------------------------------------- 撤单的幂等键与在途判断


def cancel_key_state(session: Session, base_key: str, *, max_attempts: int) -> CancelKeyState:
    """从 ``base_key`` 开始依次看 ``base_key``、``base_key:1``…哪个意图还不存在。"""
    attempts = 0
    while True:
        key = base_key if attempts == 0 else f"{base_key}:{attempts}"
        intent = repo_intents.get_by_key(session, key)
        if intent is None:
            if attempts >= max_attempts:
                return CancelKeyState(None, attempts, False, True)
            return CancelKeyState(key, attempts, False, False)
        if intent.state not in _TERMINAL:
            return CancelKeyState(None, attempts, True, False, pending_key=key, pending_state=intent.state,
                                  pending_since_ms=intent.updated_ms)
        attempts += 1


def _mentions(params: Mapping[str, Any] | None, client_algo_id: str | None, algo_id: str | None) -> bool:
    """意图参数里（顶层或 ``old`` 里）有没有点名这张条件单。"""
    if not isinstance(params, Mapping):
        return False
    for source in (params, params.get("old")):
        if not isinstance(source, Mapping):
            continue
        if client_algo_id is not None and source.get("client_algo_id") == client_algo_id:
            return True
        if algo_id is not None and source.get("algo_id") == algo_id:
            return True
    return False


def _own_attempt(idem_key: str, exclude_key: str | None) -> bool:
    """``idem_key`` 是不是以 ``exclude_key`` 为底的撤单尝试（``<base>`` 或 ``<base>:<序号>``）。"""
    return exclude_key is not None and (idem_key == exclude_key or idem_key.startswith(exclude_key + ":"))


def algo_in_flight(session: Session, *, symbol: str, side: PositionSide, client_algo_id: str | None,
                   algo_id: str | None, exclude_key: str | None = None) -> bool:
    """有没有未到终态的意图在引用这张条件单：它自己的订单行所属的意图还没结束，或者该键上某个未结束的意图
    （撤单、移动止损）在参数里点名了它。``exclude_key``：调用方自己的撤单尝试（幂等键以它为底）不算。"""
    clauses = []
    if client_algo_id is not None:
        clauses.append(Order.client_order_id == client_algo_id)
    if algo_id is not None:
        clauses.append(Order.algo_id == algo_id)
    if clauses:
        for order in session.scalars(select(Order).where(or_(*clauses))):
            if order.intent_id is None:
                continue
            intent = session.get(Intent, order.intent_id)
            if intent is not None and intent.state not in _TERMINAL and not _own_attempt(intent.idem_key, exclude_key):
                return True
    return any(_mentions(intent.params, client_algo_id, algo_id)
               for intent in repo_intents.non_terminal(session, symbol=symbol, side=side)
               if not _own_attempt(intent.idem_key, exclude_key))


def system_fills_pending(session: Session, symbol: str, side: PositionSide, *, since_ms: int) -> bool:
    """该「合约 + 方向」上，``since_ms`` 之后有过动静的系统市价单里，是否有订单行记的已成交量大于已入账的成交明细合计。"""
    stmt = select(Order).where(Order.symbol == symbol, Order.position_side == side, Order.kind == "market",
                               Order.updated_ms >= since_ms)
    for order in session.scalars(stmt):
        filled = order.filled_qty
        if filled is None or filled <= 0:
            continue
        match = [Fill.client_order_id == order.client_order_id]
        if order.exchange_order_id:
            match.append(Fill.exchange_order_id == order.exchange_order_id)
        booked = sum((qty for qty in session.scalars(select(Fill.qty).where(Fill.symbol == symbol, or_(*match)))),
                     Decimal("0"))
        if filled > booked:
            return True
    return False
