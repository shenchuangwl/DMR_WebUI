"""alert 表的仓储函数：告警的本地留痕与外部送达记录。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录或基本类型。

- ``insert``：记一条告警。没配外部渠道时调用方直接记 ``delivered=True``（本地即送达），``attempts`` 为 0。
- ``record_attempt``：记一次外部送达尝试：``attempts`` 加 1；成功就置 ``delivered`` 与 ``delivered_ms``。
  已送达的告警不会被改回未送达。
- ``recent``：按编号从新到旧取告警，``before_id`` 往前翻页，可按级别筛选。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import AlertLevel
from dmr_kernel.ledger.models import Alert
from dmr_kernel.ledger.records import AlertRecord

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def insert(session: Session, *, at_ms: int, level: AlertLevel, kind: str, message: str, delivered: bool = False,
           delivered_ms: int | None = None, attempts: int = 0) -> AlertRecord:
    row = Alert(at_ms=at_ms, level=level, kind=kind, message=message, delivered=delivered,
                delivered_ms=delivered_ms, attempts=attempts)
    session.add(row)
    session.flush()
    return AlertRecord.from_row(row)


def get(session: Session, alert_id: int) -> AlertRecord | None:
    row = session.get(Alert, alert_id)
    return None if row is None else AlertRecord.from_row(row)


def record_attempt(session: Session, alert_id: int, *, delivered: bool, at_ms: int) -> AlertRecord:
    row = session.get(Alert, alert_id)
    if row is None:
        raise LookupError(f"告警 {alert_id} 不存在")
    row.attempts = (row.attempts or 0) + 1
    if delivered and not row.delivered:
        row.delivered = True
        row.delivered_ms = at_ms
    session.flush()
    return AlertRecord.from_row(row)


def recent(session: Session, limit: int = DEFAULT_LIMIT, *, before_id: int | None = None,
           level: AlertLevel | None = None) -> list[AlertRecord]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_LIMIT} 之间的整数：{limit!r}")
    stmt = select(Alert)
    if before_id is not None:
        stmt = stmt.where(Alert.id < before_id)
    if level is not None:
        stmt = stmt.where(Alert.level == level)
    stmt = stmt.order_by(Alert.id.desc()).limit(limit)
    return [AlertRecord.from_row(row) for row in session.scalars(stmt)]
