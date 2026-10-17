"""通用的操作记录（第 6 层 ops）：改变状态的操作都先在这里登记，同一个操作号只执行一次（docs/设计约定.md 第 10 节）。

M2 的启停、平仓、切换、接管都复用它。用法::

    record, is_new = await operations.begin(op_id, OperationType.CLOSE_ALL, operator=..., session_id=...,
                                            scope="managed", state_version=12, params={...})
    if not is_new:
        ...  # 同一个操作号已经提交过：record 是第一次的记录（可能还在进行中，也可能已有结果），不要再执行一遍
    ...      # 执行
    await operations.finish(op_id, OperationStatus.PARTIAL, result={...}, per_position=[...])

- ``begin``：新操作记为进行中（running；也可以以 accepted 起头）；重复的操作号原样返回第一次的记录，不改任何东西、不写审计、
  不发事件（所以重复提交的参数不会覆盖第一次的）。
- ``finish``：只能结束一次（再结束抛 ``OperationFinished``）；状态必须是终态。
- 每次 begin（新建）与 finish 写一条审计（``operation.begin`` / ``operation.finish``，目标是操作号，与记录同一个事务）并发布
  ``OperationProgress``（事件流据此推 ``operation`` 帧）。
- ``params``、``result`` 里键名像口令、令牌的值会被抹成 ``***``（最后一道防线；调用方本就不该放进来）。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import EventBus, OperationProgress
from dmr_kernel.core.types import OperationStatus, OperationType
from dmr_kernel.ledger import repo_operations
from dmr_kernel.ledger.records import OperationRecord
from dmr_kernel.ledger.repo_operations import ACTIVE, OperationFinished, OperationNotFound
from dmr_kernel.ledger.writer import Ledger

__all__ = ["ACTIVE", "OP_ID_PATTERN", "OperationFinished", "OperationNotFound", "Operations", "check_op_id"]

# 客户端生成的操作号：1 到 64 位的字母、数字或 . _ : -（uuid 也行）。限制字符是为了日志与审计里不出现怪东西
OP_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def check_op_id(op_id: Any) -> str:
    """操作号合法就原样返回，否则抛 ``ValueError``（中文原因）。"""
    if not isinstance(op_id, str) or not OP_ID_PATTERN.fullmatch(op_id):
        raise ValueError("操作号必须是 1 到 64 位的字母、数字或 . _ : -")
    return op_id


def _status(status: Any) -> OperationStatus:
    try:
        return OperationStatus(status)
    except ValueError:
        raise ValueError(f"不认识的操作状态：{status!r}") from None


class Operations:
    def __init__(self, ledger: Ledger, clock: Clock, bus: EventBus | None = None) -> None:
        self._ledger = ledger
        self._clock = clock
        self._bus = bus

    def _announce(self, op_id: str, op_type: OperationType, status: OperationStatus, at_ms: int) -> None:
        if self._bus is not None:
            self._bus.publish(OperationProgress(op_id=op_id, op_type=op_type.value, status=status.value, at_ms=at_ms))

    async def begin(self, op_id: str, op_type: OperationType | str, *, operator: str, session_id: int | None,
                    scope: str | None = None, state_version: int | None = None,
                    params: Mapping[str, Any] | None = None,
                    status: OperationStatus | str = OperationStatus.RUNNING) -> tuple[OperationRecord, bool]:
        """登记一个操作。返回 ``(记录, 是否新建)``：操作号已存在时是第一次的记录和 ``False``。

        ``operator`` 是谁做的（会话的操作员名，或本机令牌的 ``cli``）；``session_id`` 本机令牌没有就给 None；
        ``scope`` 是操作的范围（配置键、平仓范围、目标模型……）；``state_version`` 是请求带来的状态版本。"""
        op_id = check_op_id(op_id)
        try:
            op_type = OperationType(op_type)
        except ValueError:
            raise ValueError(f"不认识的操作类型：{op_type!r}") from None
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("操作人不能为空：每个操作都要留下是谁做的")
        if session_id is not None and (isinstance(session_id, bool) or not isinstance(session_id, int)):
            raise ValueError(f"会话号必须是整数或 None：{session_id!r}")
        if params is not None and not isinstance(params, Mapping):
            raise ValueError("params 必须是字典")
        try:
            status = OperationStatus(status)
        except ValueError:
            status = None
        if status not in ACTIVE:
            raise ValueError("新建操作只能是进行中的状态（accepted 或 running）")
        now = self._clock.now_ms()
        record, is_new = await self._ledger.write(
            repo_operations.begin, op_id=op_id, op_type=op_type, operator=operator, session_id=session_id,
            scope=scope, state_version=state_version, params=dict(params or {}), now_ms=now, status=status)
        if is_new:
            self._announce(op_id, op_type, status, now)
        return record, is_new

    async def finish(self, op_id: str, status: OperationStatus | str, *, result: Any,
                     per_position: Any = None) -> OperationRecord:
        """把进行中的操作结束到终态并记下结果。操作不存在抛 ``OperationNotFound``，已经结束抛 ``OperationFinished``。"""
        op_id = check_op_id(op_id)
        status = _status(status)
        if status in ACTIVE:
            raise ValueError(f"结束操作必须给终态（不能是 {status.value}）")
        now = self._clock.now_ms()
        record = await self._ledger.write(repo_operations.finish, op_id=op_id, status=status, result=result,
                                          per_position=per_position, now_ms=now)
        self._announce(op_id, record.op_type, status, now)
        return record

    async def get(self, op_id: str) -> OperationRecord | None:
        return await self._ledger.read(repo_operations.get, check_op_id(op_id))
