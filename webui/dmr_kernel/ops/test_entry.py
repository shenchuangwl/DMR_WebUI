"""测试造仓（第 6 层 ops，报告 4.11；仅非实盘）。

``TestEntry(deps).open(caller, *, op_id, symbol, side, notional) -> {"op_id","status","result"}``：
提交 ``OPEN`` 意图（来源 ``test_entry``，绕过开仓闸与名单——协调者对这个来源不看闸），等到终态，
结果 ``{"intent_id","state"[,"reason"]}``。实盘 409 ``not_allowed``（「仅非实盘环境可用」），不登记操作。
参数：合约必须在合约清单里，方向 LONG / SHORT（不分大小写），金额是大于 0 的有限数；不合法 422 ``invalid``。
金额低于最小名义金额、该方向已有仓等由协调者拒绝，操作记为失败并带原因。
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from dmr_kernel.core.intents import IntentSpec, key_op
from dmr_kernel.core.types import IntentAction, IntentSource, IntentState, OperationStatus, OperationType, PositionSide
from dmr_kernel.ops.common import (
    Caller,
    OpsDeps,
    admit,
    fail_op,
    finish_op,
    invalid,
    not_allowed,
    response_of,
    run_intent,
    run_protected,
)

log = logging.getLogger(__name__)

__all__ = ["TestEntry"]

__test__ = False  # 不是 pytest 的测试类


class TestEntry:
    __test__ = False

    def __init__(self, deps: OpsDeps) -> None:
        self._deps = deps

    async def open(self, caller: Caller, *, op_id: str, symbol: Any, side: Any, notional: Any) -> dict[str, Any]:
        return await run_protected(self._open(caller, op_id, symbol, side, notional))

    async def _open(self, caller: Caller, op_id: str, symbol: Any, side: Any, notional: Any) -> dict[str, Any]:
        deps = self._deps
        if deps.is_live:
            raise not_allowed("测试造仓仅非实盘环境可用")
        if not isinstance(symbol, str) or deps.rules.get(symbol.strip().upper()) is None:
            raise invalid(f"不认识的合约：{symbol!r}")
        symbol = symbol.strip().upper()
        try:
            position_side = PositionSide(str(side).strip().upper())
        except ValueError:
            raise invalid("方向必须是 LONG 或 SHORT") from None
        if position_side not in (PositionSide.LONG, PositionSide.SHORT):
            raise invalid("方向必须是 LONG 或 SHORT")
        try:
            amount = notional if isinstance(notional, Decimal) else Decimal(str(notional))
        except (InvalidOperation, ValueError):
            raise invalid("金额必须是数字") from None
        if isinstance(notional, bool) or not amount.is_finite() or amount <= 0:
            raise invalid("金额必须是大于 0 的有限数")
        admission = await admit(
            deps, caller, op_id=op_id, op_type=OperationType.TEST_OPEN, scope=f"{symbol}:{position_side.value}",
            params={"symbol": symbol, "side": position_side.value, "notional": amount})
        if not admission.is_new:
            return response_of(admission.record)
        spec = IntentSpec(
            action=IntentAction.OPEN, model=admission.state.active_model.value, source=IntentSource.TEST_ENTRY,
            symbol=symbol, side=position_side, idem_key=key_op(op_id, symbol, position_side),
            params={"notional": format(amount, "f")}, reason="test entry", operator=caller.operator, op_id=op_id)
        try:
            rec = await run_intent(deps, spec)
            result: dict[str, Any] = {"intent_id": rec.id, "state": rec.state.value}
            if rec.state is IntentState.COMPLETED:
                status = OperationStatus.SUCCEEDED
            elif rec.state in IntentState.terminal():
                status = OperationStatus.FAILED
                result["reason"] = rec.state_reason or "意图未完成"
            else:
                status = OperationStatus.UNKNOWN
                result["reason"] = rec.state_reason or "超时仍未到终态"
            record = await finish_op(deps, op_id, status, result)
        except Exception as exc:
            await fail_op(deps, op_id, exc)
            raise
        return response_of(record)
