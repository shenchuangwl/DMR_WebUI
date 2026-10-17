"""交易意图（intent 表）的仓储函数。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录或基本类型。
状态迁移表在 ``core.intents``（上层）里；这里只负责「在同一个事务里检查并改状态」，调用方把允许的来源状态传进来
（``legal_from``），这样检查与修改之间没有空隙。

- ``insert``：幂等键重复不写第二行，原样返回已有记录（``(记录, 是否新建)``）。
- ``set_state``/``reopen``：返回 ``(记录, 迁移前的状态, 结果)``；结果是 ``ok``/``missing``/``unexpected``/``illegal``，
  后三种不改任何东西。
- ``reserved_slot`` 与状态一起维护：进入 RESERVED 时占名额，进入任何终态时释放（报告 F1 第 5 步）。
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import IntentAction, IntentSource, IntentState, PositionSide
from dmr_kernel.ledger import repo_audit
from dmr_kernel.ledger.models import Intent
from dmr_kernel.ledger.records import IntentRecord

TERMINAL = frozenset(IntentState.terminal())
# 过期扫描只管还没发出订单、也能迁到 EXPIRED 的状态（DISPATCH_READY 不能直接 EXPIRED）
EXPIRABLE = (IntentState.CREATED, IntentState.VALIDATED, IntentState.RESERVED)

OK = "ok"
MISSING = "missing"
UNEXPECTED = "unexpected"
ILLEGAL = "illegal"


def insert(session: Session, *, idem_key: str, action: IntentAction, model: str, source: IntentSource, symbol: str,
           side: PositionSide, qty: Decimal | None, ratio: Decimal | None, params: Mapping[str, Any], reason: str,
           created_ms: int, expires_ms: int | None, operator: str | None, op_id: str | None,
           position_id: int | None, position_generation: int | None, parent_intent_id: int | None,
           ai_eval_ref: str | None) -> tuple[IntentRecord, bool]:
    """新建一个 CREATED 的意图；幂等键已存在则不写，返回 ``(已有记录, False)``。"""
    existing = session.scalars(select(Intent).where(Intent.idem_key == idem_key)).first()
    if existing is not None:
        return IntentRecord.from_row(existing), False
    row = Intent(idem_key=idem_key, action=action, model=model, source=source, symbol=symbol, side=side, qty=qty,
                 ratio=ratio, params=dict(params), reason=reason, state=IntentState.CREATED, state_reason="",
                 created_ms=created_ms, expires_ms=expires_ms, updated_ms=created_ms, operator=operator, op_id=op_id,
                 position_id=position_id, position_generation=position_generation,
                 parent_intent_id=parent_intent_id, ai_eval_ref=ai_eval_ref, reserved_slot=False)
    session.add(row)
    session.flush()
    return IntentRecord.from_row(row), True


def get(session: Session, intent_id: int) -> IntentRecord | None:
    row = session.get(Intent, intent_id)
    return None if row is None else IntentRecord.from_row(row)


def get_by_key(session: Session, idem_key: str) -> IntentRecord | None:
    row = session.scalars(select(Intent).where(Intent.idem_key == idem_key)).first()
    return None if row is None else IntentRecord.from_row(row)


def non_terminal(session: Session, *, symbol: str | None = None, side: PositionSide | None = None,
                 action: IntentAction | None = None, model: str | None = None) -> list[IntentRecord]:
    """所有未到终态的意图（按编号升序），可按合约、方向、动作、模型筛选。"""
    stmt = select(Intent).where(Intent.state.not_in(sorted(TERMINAL, key=lambda s: s.value)))
    if symbol is not None:
        stmt = stmt.where(Intent.symbol == symbol)
    if side is not None:
        stmt = stmt.where(Intent.side == side)
    if action is not None:
        stmt = stmt.where(Intent.action == action)
    if model is not None:
        stmt = stmt.where(Intent.model == model)
    return [IntentRecord.from_row(row) for row in session.scalars(stmt.order_by(Intent.id))]


def _apply_state(row: Intent, to: IntentState, reason: str, now_ms: int) -> None:
    row.state = to
    row.state_reason = reason
    row.updated_ms = now_ms
    if to is IntentState.RESERVED:
        row.reserved_slot = True
    elif to in TERMINAL:
        row.reserved_slot = False


def set_state(session: Session, intent_id: int, to: IntentState, *, legal_from: frozenset[IntentState],
              expect: frozenset[IntentState] | None, reason: str,
              now_ms: int) -> tuple[IntentRecord | None, IntentState | None, str]:
    """当前状态在 ``legal_from`` 里（并且在 ``expect`` 里，如果给了）才迁到 ``to``。"""
    row = session.get(Intent, intent_id)
    if row is None:
        return None, None, MISSING
    previous = row.state
    if expect is not None and previous not in expect:
        return IntentRecord.from_row(row), previous, UNEXPECTED
    if previous not in legal_from:
        return IntentRecord.from_row(row), previous, ILLEGAL
    _apply_state(row, to, reason, now_ms)
    session.flush()
    return IntentRecord.from_row(row), previous, OK


def reopen(session: Session, intent_id: int, *, legal_from: frozenset[IntentState], reason: str,
           now_ms: int) -> tuple[IntentRecord | None, IntentState | None, str]:
    """终态 → ACTIVE（晚到成交）。成功时在同一个事务里写一条审计记录。"""
    row = session.get(Intent, intent_id)
    if row is None:
        return None, None, MISSING
    previous = row.state
    if previous not in legal_from:
        return IntentRecord.from_row(row), previous, ILLEGAL
    _apply_state(row, IntentState.ACTIVE, reason, now_ms)
    session.flush()
    repo_audit.append(session, at_ms=now_ms, operator="system", action="intent.reopen_late_fill",
                      target=f"intent:{intent_id}", before={"state": previous.value},
                      after={"state": IntentState.ACTIVE.value, "reason": reason}, result="ok")
    return IntentRecord.from_row(row), previous, OK


def expire_due(session: Session, now_ms: int, *, reason: str) -> list[IntentRecord]:
    """CREATED/VALIDATED/RESERVED 且到期（``expires_ms <= now_ms``）的意图 → EXPIRED，并释放预占的名额。"""
    stmt = (select(Intent).where(Intent.state.in_(EXPIRABLE), Intent.expires_ms.is_not(None),
                                 Intent.expires_ms <= now_ms).order_by(Intent.id))
    expired: list[IntentRecord] = []
    for row in session.scalars(stmt).all():
        _apply_state(row, IntentState.EXPIRED, reason, now_ms)
        session.flush()
        expired.append(IntentRecord.from_row(row))
    return expired
