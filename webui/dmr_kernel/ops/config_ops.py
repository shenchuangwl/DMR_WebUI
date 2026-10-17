"""修改单项配置（第 6 层 ops）：经操作记录去重，交给 ``ConfigStore`` 校验并落盘，写操作日志。

``set_config(op_id, key, value, *, operator, session_id) -> dict``，返回 ``{key, old, new, applies, affects_existing}``
（``applies`` 是何时生效的中文说明；``affects_existing`` 是否影响已有持仓）。

- **同一个操作号只执行一次**，重复提交返回第一次的结果（值之后又被别人改过也不会被改回去）：成功的原样重放；被拒绝的再抛同样的
  ``ConfigError``；内部出错的告诉调用方换新的操作号。重放不看这一次带来的键和值，以第一次登记的为准。
- **校验失败**抛 ``ConfigError``（中文原因，``ConfigStore`` 的原文）并把操作记为被拒绝。部署级开关没开时改模型 3 的档位一律被
  ``ConfigStore.set`` 拒绝（连同值也不看是不是已经等于现值）。未知的键在登记之前就拒绝，不占用操作号、不进操作日志。
- 操作日志：操作开始（``operation.begin``）→ 配置改动（``config.set``，由 ``ConfigStore`` 在改动的同一事务里写）→ 操作结束
  （``operation.finish``）。
- 上一次登记之后、做完之前内核退出，留下一条仍在进行中的记录：同一个操作号再来时，按**第一次登记的参数**做完它（改配置是幂等的）。
  这种情况下返回的 ``old`` 可能已经等于 ``new``（第一次做了一半）。
- 调用方被取消（例如连接断了）不会留下做了一半的操作：登记、改动、结束放在一个受保护的任务里做完。
- 所有修改串行（一把锁），所以「同一个操作号并发提交」总是后到的看到先到的结果。
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from dmr_kernel.core import configdefs
from dmr_kernel.core.config import ConfigStore
from dmr_kernel.core.configdefs import ConfigError
from dmr_kernel.core.types import OperationStatus, OperationType
from dmr_kernel.ledger.records import OperationRecord
from dmr_kernel.ops.operations import ACTIVE, Operations, check_op_id

log = logging.getLogger(__name__)

__all__ = ["ConfigError", "ConfigOps"]

MAX_KEY_SHOWN = 64


def _json_safe(value: Any) -> Any:
    """请求里的值 -> 能存进操作记录的 JSON。HTTP 的 JSON 解析会放行 NaN、Infinity，别的调用方也可能给出奇怪的对象：
    存不了的一律存成文字，不让登记这一步因此失败（校验失败本身要能被记下来）。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return value if value.is_finite() else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_json_safe(item) for item in sorted(value, key=repr)]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return repr(value)


def _native(key: str, stored: Any) -> Any:
    """账本里的 JSON 值 -> 与第一次返回时同样的 Python 类型（金额是 Decimal）。认不出来就原样给。"""
    if stored is None:
        return None
    try:
        return configdefs.validate_value(key, stored)
    except ConfigError:
        return stored


def _decode(stored: Mapping[str, Any]) -> dict[str, Any]:
    key = stored["key"]
    return {"key": key, "old": _native(key, stored["old"]), "new": _native(key, stored["new"]),
            "applies": stored["applies"], "affects_existing": stored["affects_existing"]}


def _rejection(record: OperationRecord) -> str:
    result = record.result
    message = result.get("message") if isinstance(result, Mapping) else None
    return message if isinstance(message, str) and message else "这个操作号上一次提交被拒绝"


def _late_failure(task: asyncio.Future) -> None:
    """调用方已被取消，后台任务照样做完；它若出了预料之外的错，没人会 await 它，在这里记一笔。被拒绝（ConfigError）
    已经记在操作记录里，不算。"""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None and not isinstance(error, ConfigError):
        log.error("调用方已取消，配置修改在后台出错", exc_info=error)


class ConfigOps:
    def __init__(self, operations: Operations, config: ConfigStore) -> None:
        self._operations = operations
        self._config = config
        self._lock = asyncio.Lock()

    async def set_config(self, op_id: str, key: str, value: Any, *, operator: str,
                         session_id: int | None) -> dict[str, Any]:
        """改一项配置。``operator`` 是谁改的（会话的操作员名，或本机令牌的 ``cli``），``session_id`` 本机令牌没有就给 None。
        校验失败抛 ``ConfigError``（中文原因）；操作号或操作人不合法是调用方的程序错误，抛 ``ValueError``。"""
        op_id = check_op_id(op_id)
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("操作人不能为空：配置改动必须留下是谁改的")
        if not isinstance(key, str):
            raise ValueError("配置项名称必须是文本")
        if key not in configdefs.DEFINITIONS:
            raise ConfigError(f"未知的配置项：{key[:MAX_KEY_SHOWN]!r}")
        task = asyncio.ensure_future(self._run(op_id, key, value, operator, session_id))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_late_failure)
            raise

    async def _run(self, op_id: str, key: str, value: Any, operator: str, session_id: int | None) -> dict[str, Any]:
        async with self._lock:
            record, is_new = await self._operations.begin(
                op_id, OperationType.CONFIG, operator=operator, session_id=session_id, scope=key,
                params={"key": key, "value": _json_safe(value)})
            if not is_new:
                if record.op_type is not OperationType.CONFIG:
                    raise ConfigError(f"操作号 {op_id} 已用于另一种操作（{record.op_type.value}），请换一个新的操作号")
                if record.status is OperationStatus.SUCCEEDED:
                    return _decode(record.result)
                if record.status is OperationStatus.REJECTED:
                    raise ConfigError(_rejection(record))
                if record.status not in ACTIVE:
                    raise ConfigError(f"操作号 {op_id} 对应的上一次修改没有成功完成（{record.status.value}），"
                                      f"请换一个新的操作号重试")
                key, value = record.params["key"], record.params["value"]  # 上次没做完：按第一次登记的参数做完
            return await self._execute(op_id, key, value, operator)

    async def _execute(self, op_id: str, key: str, value: Any, operator: str) -> dict[str, Any]:
        try:
            change = await self._config.set(key, value, operator=operator)
        except ConfigError as exc:
            await self._operations.finish(op_id, OperationStatus.REJECTED, result={"message": str(exc)})
            raise
        except Exception as exc:
            log.exception("修改配置项 %s 时出现内部错误（操作号 %s）", key, op_id)
            try:  # 尽力把操作记为失败；记不上也不能盖掉原来的错误
                await self._operations.finish(op_id, OperationStatus.FAILED,
                                              result={"message": f"内部错误（{type(exc).__name__}），详情见内核日志"})
            except Exception:
                log.exception("操作 %s 无法记为失败", op_id)
            raise
        result = {"key": change.key, "old": change.old, "new": change.new, "applies": change.applies,
                  "affects_existing": change.affects_existing}
        await self._operations.finish(op_id, OperationStatus.SUCCEEDED, result=result)
        return result
