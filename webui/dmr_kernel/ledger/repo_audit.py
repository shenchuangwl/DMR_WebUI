"""审计日志的仓储函数。只追加：这里只有插入（``append``）与查询（``recent``），没有更新和删除；
库里的触发器同样拒绝一切 UPDATE 与 DELETE（绕过本模块也改不了）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.ledger.models import AuditLog
from dmr_kernel.ledger.records import AuditLogRecord

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def append(session: Session, *, at_ms: int, operator: str, action: str, target: str = "", before: Any = None,
           after: Any = None, request_id: str | None = None, result: str = "ok") -> AuditLogRecord:
    """追加一条审计记录，返回写入后的记录（JSON 值已按库里的样子规范化，Decimal 变成字符串）。"""
    row = AuditLog(at_ms=at_ms, operator=operator, action=action, target=target, before=before, after=after,
                   request_id=request_id, result=result)
    session.add(row)
    session.flush()
    return AuditLogRecord.from_row(row)


def recent(session: Session, *, limit: int = DEFAULT_LIMIT, before_id: int | None = None,
           operator: str | None = None, action: str | None = None, target: str | None = None) -> list[AuditLogRecord]:
    """按编号从新到旧取审计记录；``before_id`` 用来往前翻页，其余参数是精确匹配的筛选条件。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_LIMIT} 之间的整数：{limit!r}")
    stmt = select(AuditLog)
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    if operator is not None:
        stmt = stmt.where(AuditLog.operator == operator)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if target is not None:
        stmt = stmt.where(AuditLog.target == target)
    stmt = stmt.order_by(AuditLog.id.desc()).limit(limit)
    return [AuditLogRecord.from_row(row) for row in session.scalars(stmt)]
