"""模型 2 接管用的账本读写（signature 一律 ``fn(session, ...)``，经 ``Ledger.read``/``Ledger.write`` 调用）。

- ``key_fills``：某「合约 + 方向」入账的成交（按成交时间、编号升序），模型 2 据此找「仓位从零变非零的那一笔」。
- ``ignore_mixed``：混合仓（策略仓被外部加仓而来，归属仍是策略）取消托管后转外部（已忽略）。
  ``repo_registry.ignore`` 对系统自己开的仓一律拒绝，混合仓要单独放行；转出后不再是混合仓、没有出场规则集。
- ``restore_original``：F13「恢复托管」——「来源待核对」的待确认仓按开仓单的系统编号恢复成原来的归属（策略仓或 AI 仓）：
  托管中、出场规则集「策略规则」、开仓时间与锚点取开仓成交的时间；保护状态与止损编号不动（恢复时保留下来的止损按编号认领）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import ExitRuleset, Ownership, PositionSide, PositionStatus
from dmr_kernel.ledger import repo_registry
from dmr_kernel.ledger.models import Fill
from dmr_kernel.ledger.records import FillRecord
from dmr_kernel.ledger.repo_registry import (
    Change,
    IllegalTransition,
    _audit,
    _brief,
    _check_version,
    _finish,
    _load,
    _set,
)
from dmr_kernel.ledger.records import PositionRecord

__all__ = ["ignore_mixed", "key_fills", "restore_original"]


def key_fills(session: Session, symbol: str, side: PositionSide, *, limit: int = 5000) -> list[FillRecord]:
    """该键最近 ``limit`` 笔入账成交，按成交时间、编号升序。"""
    stmt = (select(Fill).where(Fill.symbol == symbol, Fill.position_side == side)
            .order_by(Fill.time_ms.desc(), Fill.id.desc()).limit(limit))
    rows = [FillRecord.from_row(row) for row in session.scalars(stmt)]
    rows.reverse()
    return rows


def ignore_mixed(session: Session, position_id: int, *, expected_version: int | None, now_ms: int) -> Change:
    """混合仓 → 外部（已忽略）。其它状态的仓请用 ``repo_registry.ignore``。"""
    pos = _load(session, position_id)
    _check_version(pos, expected_version)
    if pos.status is PositionStatus.CLOSED:
        raise IllegalTransition(f"持仓 {position_id} 已平，不能忽略")
    if not pos.is_mixed:
        raise IllegalTransition(f"持仓 {position_id} 不是混合仓")
    before = PositionRecord.from_row(pos)
    _set(pos, status=PositionStatus.IGNORED, ownership=Ownership.NONE, exit_ruleset=None, signal_exit=False,
         is_mixed=False)
    change = _finish(session, pos, before, now_ms, True)
    _audit(session, now_ms, "position.ignore", pos.id, before=_brief(before), after=_brief(change.record))
    return change


def restore_original(session: Session, position_id: int, *, ownership: Ownership, open_time_ms: int,
                     opened_by_intent_id: int | None, note: str, expected_version: int | None,
                     now_ms: int) -> Change:
    """待确认仓 → 托管中的策略仓/AI 仓（F13 恢复托管）。"""
    if ownership not in (Ownership.STRATEGY, Ownership.AI):
        raise ValueError(f"恢复托管只能恢复为策略仓或 AI 仓：{ownership.value}")
    pos = _load(session, position_id)
    _check_version(pos, expected_version)
    if pos.status is not PositionStatus.PENDING:
        raise IllegalTransition(f"持仓 {position_id} 的状态是 {pos.status.value}，只有待确认仓能恢复托管")
    before = PositionRecord.from_row(pos)
    _set(pos, status=PositionStatus.MANAGED, ownership=ownership, is_mixed=False, exit_ruleset=ExitRuleset.STRATEGY,
         signal_exit=False, pending_reason=None, pending_flags=[], open_time_ms=open_time_ms,
         open_time_estimated=False, anchor_ms=open_time_ms, opened_by_intent_id=opened_by_intent_id, origin_note=note)
    change = _finish(session, pos, before, now_ms, True)
    _audit(session, now_ms, "position.restore_original", pos.id, before=_brief(before), after=_brief(change.record))
    return change
