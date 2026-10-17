"""操作记录（operation 表）的仓储函数：同一操作号只执行一次（op_id 唯一）。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录或基本类型。

- ``begin``：操作号已存在就什么都不写，原样返回已有记录（``(记录, False)``）；新建时在同一个事务里写一条 ``operation.begin`` 审计。
  写线程逐个执行写函数，所以「查有没有 → 插入」之间没有空隙；op_id 上的唯一索引是最后一道保险。
- ``finish``：只能把进行中（accepted、running）的操作转到终态，一次。成功时在同一个事务里写一条 ``operation.finish`` 审计。
  操作不存在抛 ``OperationNotFound``，已经结束抛 ``OperationFinished``（带当前记录）；两种情况都不改任何东西。
- 口令、令牌类的键（键名含 password、passwd、secret、token）的值不进记录与审计：调用方本不该把它们放进 params、result，
  这里是最后一道（``redact``）。
- 审计只追加：begin、finish 各一条，目标都是操作号；重复提交不写。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import OperationStatus, OperationType
from dmr_kernel.ledger import repo_audit
from dmr_kernel.ledger.models import Operation
from dmr_kernel.ledger.records import OperationRecord

__all__ = [
    "ACTIVE", "AUDIT_BEGIN", "AUDIT_FINISH", "MASK", "OperationFinished", "OperationNotFound", "begin", "finish",
    "get", "redact",
]

ACTIVE = (OperationStatus.ACCEPTED, OperationStatus.RUNNING)  # 进行中；其余状态都是终态
AUDIT_BEGIN = "operation.begin"
AUDIT_FINISH = "operation.finish"
SYSTEM = "system"
MASK = "***"
_SENSITIVE_KEY = re.compile(r"password|passwd|secret|token", re.IGNORECASE)


class OperationNotFound(LookupError):
    """没有这个操作号。"""


class OperationFinished(RuntimeError):
    """操作已经结束，不能再结束一次。``record`` 是它现在的记录。"""

    def __init__(self, record: OperationRecord) -> None:
        super().__init__(f"操作 {record.op_id} 已经结束（{record.status.value}），不能再次结束")
        self.record = record


def redact(value: Any) -> Any:
    """递归地把键名像口令、令牌的值换成 ``***``（字典与列表；其余原样）。"""
    if isinstance(value, Mapping):
        return {key: MASK if isinstance(key, str) and _SENSITIVE_KEY.search(key) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def _find(session: Session, op_id: str) -> Operation | None:
    return session.scalars(select(Operation).where(Operation.op_id == op_id)).first()


def get(session: Session, op_id: str) -> OperationRecord | None:
    row = _find(session, op_id)
    return None if row is None else OperationRecord.from_row(row)


def begin(session: Session, *, op_id: str, op_type: OperationType, operator: str, session_id: int | None,
          scope: str | None, state_version: int | None, params: Mapping[str, Any], now_ms: int,
          status: OperationStatus = OperationStatus.RUNNING) -> tuple[OperationRecord, bool]:
    """新建一条进行中的操作记录；操作号已存在则不写，返回 ``(已有记录, False)``。"""
    if status not in ACTIVE:
        raise ValueError(f"新建操作只能是进行中的状态（accepted 或 running）：{status!r}")
    existing = _find(session, op_id)
    if existing is not None:
        return OperationRecord.from_row(existing), False
    safe_params = redact(dict(params))
    row = Operation(op_id=op_id, op_type=op_type, scope=scope, status=status, operator=operator,
                    session_id=session_id, state_version=state_version, params=safe_params, per_position=None,
                    result=None, started_ms=now_ms, ended_ms=None)
    session.add(row)
    session.flush()
    record = OperationRecord.from_row(row)
    repo_audit.append(session, at_ms=now_ms, operator=operator, action=AUDIT_BEGIN, target=op_id,
                      after={"op_type": op_type.value, "scope": scope, "state_version": state_version,
                             "params": safe_params},
                      result=status.value)
    return record, True


def finish(session: Session, *, op_id: str, status: OperationStatus, result: Any, per_position: Any,
           now_ms: int) -> OperationRecord:
    """把进行中的操作结束到终态 ``status``，记下结果（与逐仓结果）和结束时间。"""
    if status in ACTIVE:
        raise ValueError(f"结束操作必须给终态，不能是进行中的状态：{status!r}")
    row = _find(session, op_id)
    if row is None:
        raise OperationNotFound(f"没有这个操作号：{op_id}")
    if row.status not in ACTIVE:
        raise OperationFinished(OperationRecord.from_row(row))
    previous = row.status
    safe_result, safe_per_position = redact(result), redact(per_position)
    row.status = status
    row.result = safe_result
    row.per_position = safe_per_position
    row.ended_ms = now_ms
    session.flush()
    record = OperationRecord.from_row(row)
    after: dict[str, Any] = {"status": status.value, "result": safe_result}
    if safe_per_position is not None:
        after["per_position"] = safe_per_position
    repo_audit.append(session, at_ms=now_ms, operator=row.operator or SYSTEM, action=AUDIT_FINISH, target=op_id,
                      before={"status": previous.value}, after=after, result=status.value)
    return record
