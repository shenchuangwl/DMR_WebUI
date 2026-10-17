"""平掉此仓（第 6 层 ops，报告 F12）。

``CloseOne(deps).confirm(caller, *, op_id, position_id, position_version) -> {"op_id","status","result"}``：
单次确认、不需要口令；统一步骤（允许表 ``close_one``、席位、持仓版本）之后提交 CLOSE 意图（来源 operator，
``params.resend_on_unknown=True``，出场原因记「手动」），等到终态（``close_one_timeout_s``，降级加倍）。
结果 ``{"intent_id","state","filled_qty","outcome"[,"reason"]}``，outcome：closed / closed_externally / failed / unknown。

- 只有托管中的持仓可平；平仓中的持仓只有在「部分平仓暂停」时可点——批量平仓锁在手时被它处理的仓不能点（409 ``not_allowed``）。
  待确认、接管中、外部（已忽略）的仓不归系统，409 ``not_allowed``。
- 允许表标明有抑制原因时必须先读到即时持仓（读不到 503 ``unavailable``，什么都不发）。
- 手动平掉策略仓（归属为策略）并且真的平掉了（outcome=closed）之后，该合约冷却 ``cooldown_bars`` 根短周期 K 线（0 = 不冷却）；
  失败、未知、已由其它途径平掉都不冷却。
- 碎仓由协调者改走全平型条件单。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from dmr_kernel.core.clock import interval_ms
from dmr_kernel.core.intents import IntentSpec, key_op
from dmr_kernel.core.modelstate import ModelStateSnapshot, allowed_operations
from dmr_kernel.core.types import (
    IntentAction,
    IntentSource,
    Known,
    OpLock,
    OperationType,
    Ownership,
    PositionStatus,
)
from dmr_kernel.ledger.records import PositionRecord
from dmr_kernel.ops.common import (
    Caller,
    OpsDeps,
    admit,
    close_outcome,
    fail_op,
    filled_close_qty,
    finish_op,
    intent_result,
    not_allowed,
    response_of,
    run_intent,
    run_protected,
    unavailable,
)

log = logging.getLogger(__name__)

__all__ = ["CloseOne"]


class CloseOne:
    def __init__(self, deps: OpsDeps) -> None:
        self._deps = deps
        self._locks: dict[int, asyncio.Lock] = {}

    async def confirm(self, caller: Caller, *, op_id: str, position_id: int, position_version: int) -> dict[str, Any]:
        return await run_protected(self._confirm(caller, op_id, position_id, position_version))

    async def _confirm(self, caller: Caller, op_id: str, position_id: int, position_version: int) -> dict[str, Any]:
        deps = self._deps
        async with self._locks.setdefault(position_id, asyncio.Lock()):
            async def recheck(state: ModelStateSnapshot, position: PositionRecord | None) -> None:
                await self._recheck(state, position)

            admission = await admit(
                deps, caller, op_id=op_id, op_type=OperationType.CLOSE_ONE, allowed_key="close_one",
                position_id=position_id, position_version=position_version, scope=f"position:{position_id}",
                params={"position_id": position_id}, precheck=recheck)
            if not admission.is_new:
                return response_of(admission.record)
            position = admission.position
            spec = IntentSpec(
                action=IntentAction.CLOSE, model=admission.state.active_model.value, source=IntentSource.OPERATOR,
                symbol=position.symbol, side=position.side, idem_key=key_op(op_id, position.symbol, position.side),
                params={"resend_on_unknown": True}, reason="manual close_one", operator=caller.operator, op_id=op_id,
                position_id=position.id, position_generation=position.generation)
            try:
                rec = await run_intent(deps, spec)
                filled = await filled_close_qty(deps, rec.id)
                outcome, status = close_outcome(rec.state, filled)
                if outcome == "closed" and position.ownership is Ownership.STRATEGY:
                    await self._cool_down(position.symbol)
                record = await finish_op(deps, op_id, status, intent_result(rec, filled, outcome))
            except Exception as exc:
                await fail_op(deps, op_id, exc)
                raise
            return response_of(record)

    async def _recheck(self, state: ModelStateSnapshot, position: PositionRecord | None) -> None:
        assert position is not None
        deps = self._deps
        if position.status is PositionStatus.CLOSING:
            if OpLock.BULK_CLOSING in state.op_locks:
                raise not_allowed("该持仓正由批量平仓处理，不能单独平掉")
        elif position.status is not PositionStatus.MANAGED:
            raise not_allowed("只有托管中的持仓可以平掉（待确认、接管中与外部持仓不归系统管理）")
        if allowed_operations(state)["close_one"].needs_live_read:
            live = await deps.coordinator.live_position(position.symbol, position.side)
            if not isinstance(live, Known):
                raise unavailable(f"有抑制原因时平仓须先取到即时持仓，现在读不到（{live.reason}）")

    async def _cool_down(self, symbol: str) -> None:
        deps = self._deps
        bars = int(deps.config_get("cooldown_bars"))
        if bars <= 0:
            return
        until = deps.clock.now_ms() + bars * interval_ms(str(deps.config_get("timeframe_short")))
        await deps.registry.set_cooldown(symbol, until, "手动平掉此仓")
