"""重启恢复（core.recovery）用的账本读取：签名一律 ``fn(session, ...)``，经 ``Ledger.read`` 调用；返回记录或基本类型。

- ``active_operations``：operation 表里还没到终态（accepted、running）的操作——进程被杀时没做完的批量操作与切换。
- ``closed_with_leftovers``：已平的记录里还留着止损编号、待清理旧止损或已移交止损的（平仓后、清理之前进程被杀，F9 第 4、8 步）。
- ``intents_of_operation``：属于某个操作号的意图（逐仓结果据此汇总）。
- ``recent_intent_symbols``：``since_ms`` 之后建立过意图的合约（成交补拉的范围：有持仓、挂单、近期意图的合约）。
- ``fills_of_key``：某「合约 + 方向」在 ``since_ms`` 之后入账的成交（判断未登记持仓的来源）。
- ``cancel_intents_since``：对账为清孤儿条件单提交的撤单意图（STOP_CANCEL，来源 reconcile）。
- ``last_close_intent``：某「合约 + 方向」最近一个平仓类意图（平仓中的仓恢复后提示结果）。
- ``system_fills_pending``：见 ``repo_reconcile.system_fills_pending``，这里不重复实现，恢复直接用那一个。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import IntentAction, IntentSource, PositionSide, PositionStatus
from dmr_kernel.ledger.models import Fill, Intent, Operation, Position
from dmr_kernel.ledger.records import FillRecord, IntentRecord, OperationRecord, PositionRecord
from dmr_kernel.ledger.repo_operations import ACTIVE

__all__ = [
    "active_operations", "cancel_intents_since", "closed_with_leftovers", "fills_of_key", "intents_of_operation",
    "last_close_intent", "recent_intent_symbols",
]

_CLOSE_ACTIONS = (IntentAction.CLOSE, IntentAction.PARTIAL_CLOSE)


def active_operations(session: Session) -> list[OperationRecord]:
    """还没到终态的操作（按建立顺序）。"""
    stmt = select(Operation).where(Operation.status.in_(sorted(ACTIVE, key=lambda s: s.value))).order_by(Operation.id)
    return [OperationRecord.from_row(row) for row in session.scalars(stmt)]


def closed_with_leftovers(session: Session, *, limit: int = 1000) -> list[PositionRecord]:
    """已平但还留着保护单信息的记录（当前止损编号、待清理旧止损、已移交止损任一）。只看最近 ``limit`` 条已平记录，按编号升序返回。"""
    stmt = (select(Position).where(Position.status == PositionStatus.CLOSED).order_by(Position.id.desc())
            .limit(limit))
    found = [PositionRecord.from_row(row) for row in session.scalars(stmt)]
    leftovers = [rec for rec in found if rec.stop_client_id or rec.stop_algo_id or rec.stale_stops
                 or rec.handed_over_stop]
    return sorted(leftovers, key=lambda rec: rec.id)


def intents_of_operation(session: Session, op_id: str) -> list[IntentRecord]:
    stmt = select(Intent).where(Intent.op_id == op_id).order_by(Intent.id)
    return [IntentRecord.from_row(row) for row in session.scalars(stmt)]


def recent_intent_symbols(session: Session, *, since_ms: int) -> list[str]:
    stmt = select(Intent.symbol).where(Intent.created_ms >= since_ms).distinct().order_by(Intent.symbol)
    return list(session.scalars(stmt))


def fills_of_key(session: Session, symbol: str, side: PositionSide, *, since_ms: int) -> list[FillRecord]:
    stmt = (select(Fill).where(Fill.symbol == symbol, Fill.position_side == side, Fill.time_ms >= since_ms)
            .order_by(Fill.time_ms, Fill.id))
    return [FillRecord.from_row(row) for row in session.scalars(stmt)]


def last_close_intent(session: Session, symbol: str, side: PositionSide) -> IntentRecord | None:
    stmt = (select(Intent).where(Intent.symbol == symbol, Intent.side == side, Intent.action.in_(_CLOSE_ACTIONS))
            .order_by(Intent.id.desc()).limit(1))
    row = session.scalars(stmt).first()
    return None if row is None else IntentRecord.from_row(row)


def cancel_intents_since(session: Session, *, since_ms: int) -> list[IntentRecord]:
    stmt = (select(Intent).where(Intent.action == IntentAction.STOP_CANCEL, Intent.source == IntentSource.RECONCILE,
                                 Intent.created_ms >= since_ms).order_by(Intent.id))
    return [IntentRecord.from_row(row) for row in session.scalars(stmt)]
