"""部分平仓：预览与确认（第 6 层 ops，报告 F4）。

``PartialClose(deps)``：

- ``await preview(caller, *, position_id, ratio=None, qty=None) -> dict``：只读，不登记操作、不改任何东西。ratio（0–1 的小数）与 qty 二选一；
  即时读交易所持仓与标记价，平仓数量按数量步长向下取整。返回
  ``{"position_id","position_version","live_qty","close_qty","remaining_qty","remaining_notional","min_notional","mark_price","warnings"}``。
  取整为 0、等于全平（含 qty 超过持仓）、剩余名义金额低于最小名义金额、实测第 2 项「不豁免」时平仓单名义金额低于最小名义金额
  → 409 ``not_allowed`` 并提示改用「平掉此仓」；读数未知 → 503 ``unavailable``（绝不当零）。
- ``await confirm(caller, *, op_id, position_id, position_version, preview_qty, close_qty) -> {"op_id","status","result"}``：
  统一步骤（``ops.common.admit``）加 F4 第 3 步复核（持仓是托管中；该键没有别的未结束意图；即时持仓与预览相差不超过一个步长，
  否则 409 ``stale_version`` 并要求重新预览；剩余与平仓单名义金额按当刻标记价重算）。复核不过的请求不占操作号。通过后提交
  PARTIAL_CLOSE 意图（来源 operator）并等到终态（``close_one_timeout_s``，降级加倍）：
  结果 ``{"intent_id","state","filled_qty","outcome"[,"reason"]}``，outcome 为 closed / closed_externally / failed / unknown。
  超时只报「未知」，部分平仓永不自动重发（回查期间由协调者负责）。
- 同一持仓的确认串行（每个持仓一把锁）：两个会话同时点 50%，先到的生效，后到的因持仓已变而 409。
- 中断点 4（``partial_close_sent``）：协调者在订单发出后抛 CrashSignal，这里不吞（操作留在进行中，由重启恢复记为中断）。
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from dmr_kernel.algo.rounding import floor_to_step
from dmr_kernel.core.intents import IntentSpec, key_op
from dmr_kernel.core.modelstate import ModelStateSnapshot
from dmr_kernel.core.types import (
    IntentAction,
    IntentSource,
    Known,
    OperationType,
    PositionStatus,
    ProtectionState,
    Unknown,
)
from dmr_kernel.ledger.records import PositionRecord
from dmr_kernel.ops.common import (
    Caller,
    OpRejected,
    OpsDeps,
    admit,
    close_outcome,
    fail_op,
    filled_close_qty,
    finish_op,
    intent_result,
    invalid,
    jsonable_result,
    not_allowed,
    not_found,
    require_allowed,
    require_seat,
    response_of,
    run_intent,
    run_protected,
    stale_version,
    unavailable,
)

log = logging.getLogger(__name__)

__all__ = ["PartialClose"]

HINT = "请改用「平掉此仓」"


def _decimal(name: str, value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise invalid(f"{name} 必须是数字")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise invalid(f"{name} 必须是数字") from None
    if not number.is_finite():
        raise invalid(f"{name} 必须是有限的数字")
    return number


class PartialClose:
    def __init__(self, deps: OpsDeps) -> None:
        self._deps = deps
        self._locks: dict[int, asyncio.Lock] = {}

    # ================================================================ 读数

    def _managed(self, position_id: int) -> PositionRecord:
        position = self._deps.registry.by_id(position_id)
        if position is None:
            raise not_found(f"没有这个持仓（编号 {position_id}，或已平仓）")
        return position

    @staticmethod
    def _require_managed(position: PositionRecord) -> None:
        if position.status is PositionStatus.CLOSING:
            raise not_allowed("该持仓正在平仓中，不能再部分平仓")
        if position.status is not PositionStatus.MANAGED:
            raise not_allowed("只有托管中的持仓可以部分平仓（待确认、接管中与外部持仓不在其内）")

    async def _live(self, position: PositionRecord) -> Decimal:
        live = await self._deps.coordinator.live_position(position.symbol, position.side)
        if not isinstance(live, Known):
            raise unavailable(f"即时持仓读不到（{live.reason}），结果未知，不能部分平仓")
        if live.value <= 0:
            raise not_allowed("交易所上该持仓已为零：已由其它途径平掉，请刷新")
        return live.value

    async def _mark(self, symbol: str) -> Decimal:
        result = await self._deps.adapter.fetch_tickers([symbol])
        if not isinstance(result, Known):
            raise unavailable(f"读不到 {symbol} 的标记价（{result.reason}）")
        ticker = result.value.get(symbol)
        if ticker is None or ticker.mark <= 0:
            raise unavailable(f"没有 {symbol} 的标记价")
        return ticker.mark

    def _instrument(self, symbol: str):
        inst = self._deps.rules.get(symbol)
        if inst is None:
            raise unavailable(f"合约规则未知（{symbol}），不能部分平仓")
        return inst

    def _check_sizes(self, inst: Any, live: Decimal, close: Decimal, mark: Decimal) -> Decimal:
        """数量与名义金额复核，返回剩余数量。"""
        if close <= 0:
            raise not_allowed(f"平仓数量按步长取整后为 0，{HINT}")
        if close >= live:
            raise not_allowed(f"平仓数量不小于持仓（等于全平），{HINT}")
        remaining = live - close
        if remaining * mark < inst.min_notional:
            raise not_allowed(f"剩余名义金额 {format(remaining * mark, 'f')} 低于最小名义金额 "
                              f"{format(inst.min_notional, 'f')}，{HINT}")
        if not self._deps.config_get("ex_close_min_notional_exempt") and close * mark < inst.min_notional:
            raise not_allowed(f"平仓单名义金额 {format(close * mark, 'f')} 低于最小名义金额 "
                              f"{format(inst.min_notional, 'f')}，{HINT}")
        return remaining

    # ================================================================ 预览

    async def preview(self, caller: Caller, *, position_id: int, ratio: Any = None, qty: Any = None) -> dict[str, Any]:
        deps = self._deps
        if (ratio is None) == (qty is None):
            raise invalid("ratio 与 qty 必须给且只给一个")
        if ratio is not None:
            ratio = _decimal("ratio", ratio)
            if not 0 < ratio <= 1:
                raise invalid("ratio 必须大于 0 且不超过 1")
        else:
            qty = _decimal("qty", qty)
            if qty <= 0:
                raise invalid("qty 必须大于 0")
        require_allowed(deps.modelstate.current(), "partial_close")
        require_seat(caller)
        position = self._managed(position_id)
        self._require_managed(position)
        live = await self._live(position)
        mark = await self._mark(position.symbol)
        inst = self._instrument(position.symbol)
        step = inst.market_step_size or inst.step_size
        if qty is not None and qty >= live:
            close = live  # 超过持仓按持仓算：等于全平
        else:
            close = floor_to_step(live * ratio if ratio is not None else qty, step)
        remaining = self._check_sizes(inst, live, close, mark)
        warnings = []
        if position.protection_state is ProtectionState.EXTERNAL:
            warnings.append("该持仓的止损单不是系统挂的，部分平仓后不会随数量调整，请核对止损")
        return jsonable_result({
            "position_id": position.id, "position_version": position.version, "live_qty": live, "close_qty": close,
            "remaining_qty": remaining, "remaining_notional": remaining * mark, "min_notional": inst.min_notional,
            "mark_price": mark, "warnings": warnings})

    # ================================================================ 确认

    async def confirm(self, caller: Caller, *, op_id: str, position_id: int, position_version: int,
                      preview_qty: Any, close_qty: Any) -> dict[str, Any]:
        preview_qty = _decimal("preview_qty", preview_qty)
        close_qty = _decimal("close_qty", close_qty)
        if preview_qty <= 0 or close_qty <= 0:
            raise invalid("preview_qty 与 close_qty 必须大于 0")
        return await run_protected(self._confirm(caller, op_id, position_id, position_version, preview_qty, close_qty))

    async def _confirm(self, caller: Caller, op_id: str, position_id: int, position_version: int,
                       preview_qty: Decimal, close_qty: Decimal) -> dict[str, Any]:
        deps = self._deps
        lock = self._locks.setdefault(position_id, asyncio.Lock())
        async with lock:
            async def recheck(state: ModelStateSnapshot, position: PositionRecord | None) -> None:
                await self._recheck(position, preview_qty, close_qty)

            admission = await admit(
                deps, caller, op_id=op_id, op_type=OperationType.PARTIAL_CLOSE, allowed_key="partial_close",
                position_id=position_id, position_version=position_version, scope=f"position:{position_id}",
                params={"position_id": position_id, "preview_qty": preview_qty, "close_qty": close_qty},
                precheck=recheck)
            if not admission.is_new:
                return response_of(admission.record)
            position = admission.position
            spec = IntentSpec(
                action=IntentAction.PARTIAL_CLOSE, model=admission.state.active_model.value, source=IntentSource.OPERATOR,
                symbol=position.symbol, side=position.side, idem_key=key_op(op_id, position.symbol, position.side),
                qty=close_qty, reason="manual partial close", operator=caller.operator, op_id=op_id,
                position_id=position.id, position_generation=position.generation)
            try:
                rec = await run_intent(deps, spec)
                filled = await filled_close_qty(deps, rec.id)
                outcome, status = close_outcome(rec.state, filled)
                record = await finish_op(deps, op_id, status, intent_result(rec, filled, outcome))
            except Exception as exc:  # CrashSignal 是 BaseException：不吞，操作留在进行中
                await fail_op(deps, op_id, exc)
                raise
            return response_of(record)

    async def _recheck(self, position: PositionRecord | None, preview_qty: Decimal, close_qty: Decimal) -> None:
        deps = self._deps
        assert position is not None
        self._require_managed(position)
        if deps.coordinator.key_busy(position.symbol, position.side):
            raise not_allowed("该合约方向有别的减仓或保护变更在途，请等它结束后重新预览")
        inst = self._instrument(position.symbol)
        step = inst.market_step_size or inst.step_size
        if close_qty % step != 0:
            raise invalid(f"平仓数量必须是数量步长 {format(step, 'f')} 的整数倍")
        live = await self._live(position)
        if abs(live - preview_qty) > step:
            raise stale_version(f"即时持仓（{format(live, 'f')}）与预览时（{format(preview_qty, 'f')}）相差超过一个步长，"
                                f"请重新预览")
        mark = await self._mark(position.symbol)
        self._check_sizes(inst, live, close_qty, mark)
