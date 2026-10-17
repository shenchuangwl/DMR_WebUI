"""接管类操作（第 6 层 ops，报告 F13）：接管、忽略、恢复托管、取消托管、交给系统管理止损。

``TakeoverOps(deps, service=, protection=, ledger=)``，五个入口都是 ``async (caller, *, op_id, position_id, position_version, ...)``，
走 ``ops.common.admit`` 的统一步骤（操作号去重、席位、持仓状态版本、预检、登记），返回 ``{"op_id","status","result"}``；
``result`` 总含 ``{"position_id","status","ownership","protection_state"}``（做完之后登记簿里的现状）。拒绝：409 ``not_allowed``/``stale_version``，
暂时读不到交易所或保护单管理还在忙：503 ``unavailable``。预检不过的请求不占操作号。

- ``takeover``：只对待确认仓、只在模型 2 是生效模型时；走 ``TakeoverService.take_over``（均价取交易所持仓均价，开仓时间取成交记录，
  取不到标注为估计），接管后等保护单管理做完初始止损再返回。
- ``ignore``：待确认仓（或已忽略的）→ 外部（已忽略），有效到它归零。接管仓与混合仓请用取消托管。
- ``restore``：只对「来源待核对」的待确认仓，任何模型下可用。按成交记录找到开仓订单，系统编号对得上就恢复原来的归属（策略仓/AI 仓），
  保留下来的系统止损由保护单管理按编号认领；查不到登记为手动接管。
- ``cancel_takeover``：只对接管仓与混合仓，``mode`` 为 ``handover``（缺省，保留止损并移交）或 ``cancel_stop``（先撤系统止损并确认）；
  与保护单管理串行（``Protection.cancel_takeover``），之后转外部（已忽略）。混合仓归属仍是策略，由 ``Registry.ignore_mixed`` 处理。
- ``hand_stop_to_system``：只对保护状态为「外部保护」的接管仓，``Protection.hand_stop_to_system``。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from dmr_kernel.core.modelstate import ModelStateSnapshot
from dmr_kernel.core.registry import StaleVersion
from dmr_kernel.core.types import (
    Model,
    OperationStatus,
    OperationType,
    OrderOrigin,
    Ownership,
    PendingReason,
    PositionStatus,
    ProtectionState,
)
from dmr_kernel.exchange import ids
from dmr_kernel.ledger import repo_takeover
from dmr_kernel.ledger.records import PositionRecord
from dmr_kernel.models.m2_takeover import TakeoverRefused, TakeoverService, open_run
from dmr_kernel.ops.common import (
    Caller,
    OpRejected,
    OpsDeps,
    admit,
    fail_op,
    finish_op,
    invalid,
    not_allowed,
    response_of,
    run_protected,
    stale_version,
    unavailable,
)

log = logging.getLogger(__name__)

__all__ = ["CANCEL_MODES", "TakeoverOps"]

CANCEL_MODES = ("handover", "cancel_stop")


class TakeoverOps:
    def __init__(self, deps: OpsDeps, *, service: TakeoverService, protection: Any, ledger: Any) -> None:
        self._deps = deps
        self._service = service
        self._protection = protection
        self._ledger = ledger
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------ 共用

    def _result(self, position_id: int, **extra: Any) -> dict[str, Any]:
        rec = self._deps.registry.by_id(position_id)
        if rec is None:
            return {"position_id": position_id, "status": "closed", "ownership": "none", "protection_state": "none",
                    **extra}
        return {"position_id": rec.id, "status": rec.status.value, "ownership": rec.ownership.value,
                "protection_state": rec.protection_state.value, "version": rec.version, **extra}

    async def _run(self, caller: Caller, op_type: OperationType, op_id: str, position_id: int, position_version: int,
                   precheck: Any, work: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
        deps = self._deps
        async with self._locks.setdefault(position_id, asyncio.Lock()):
            admission = await admit(
                deps, caller, op_id=op_id, op_type=op_type, allowed_key=op_type.value, position_id=position_id,
                position_version=position_version, scope=f"position:{position_id}", params={"position_id": position_id, **(params or {})},
                precheck=precheck)
            if not admission.is_new:
                return response_of(admission.record)
            try:
                status, result = await work(admission.position)
            except OpRejected as exc:
                await finish_op(deps, op_id, OperationStatus.REJECTED, {"message": exc.message})
                raise
            except StaleVersion as exc:
                await finish_op(deps, op_id, OperationStatus.REJECTED, {"message": str(exc)})
                raise stale_version(str(exc)) from None
            except BaseException as exc:
                await fail_op(deps, op_id, exc)
                raise
            return response_of(await finish_op(deps, op_id, status, result))

    # ------------------------------------------------------------ 接管

    async def takeover(self, caller: Caller, *, op_id: str, position_id: int, position_version: int) -> dict[str, Any]:
        return await run_protected(self._takeover(caller, op_id, position_id, position_version))

    async def _takeover(self, caller: Caller, op_id: str, position_id: int, position_version: int) -> dict[str, Any]:
        async def precheck(state: ModelStateSnapshot, position: PositionRecord | None) -> None:
            assert position is not None
            if state.active_model is not Model.M2:
                raise not_allowed("模型 2 不是生效模型：手动接管只在模型 2 生效时可用")
            if position.status is not PositionStatus.PENDING:
                raise not_allowed("只有待确认仓可以接管")

        async def work(position: PositionRecord) -> tuple[OperationStatus, dict[str, Any]]:
            await self._take_over(position.id, position_version)
            return OperationStatus.SUCCEEDED, self._result(position.id)

        return await self._run(caller, OperationType.TAKEOVER, op_id, position_id, position_version, precheck, work)

    async def _take_over(self, position_id: int, expected_version: int | None) -> PositionRecord:
        try:
            rec = await self._service.take_over(position_id, expected_version=expected_version)
        except TakeoverRefused as exc:
            raise (unavailable(exc.message) if exc.unavailable else not_allowed(exc.message)) from None
        await self._await_protection(rec.id, "takeover")
        return rec

    async def _await_protection(self, position_id: int, reason: str) -> None:
        """等保护单管理做完这个仓的保护周期（它也订阅了登记事件，同一个仓同一时刻只有一个周期在跑）。"""
        try:
            await self._protection.ensure_protected(position_id, reason=reason)
        except Exception:
            log.exception("接管后确认保护状态出错（持仓 %s）；保护单管理会继续重试", position_id)

    # ------------------------------------------------------------ 忽略

    async def ignore(self, caller: Caller, *, op_id: str, position_id: int, position_version: int) -> dict[str, Any]:
        return await run_protected(self._ignore(caller, op_id, position_id, position_version))

    async def _ignore(self, caller: Caller, op_id: str, position_id: int, position_version: int) -> dict[str, Any]:
        async def precheck(state: ModelStateSnapshot, position: PositionRecord | None) -> None:
            assert position is not None
            if position.status not in (PositionStatus.PENDING, PositionStatus.IGNORED):
                raise not_allowed("只有外部或待确认的仓可以忽略；接管仓与混合仓请用「取消托管」")

        async def work(position: PositionRecord) -> tuple[OperationStatus, dict[str, Any]]:
            await self._deps.registry.ignore(position.id, expected_version=position_version)
            return OperationStatus.SUCCEEDED, self._result(position.id)

        return await self._run(caller, OperationType.IGNORE, op_id, position_id, position_version, precheck, work)

    # ------------------------------------------------------------ 恢复托管

    async def restore(self, caller: Caller, *, op_id: str, position_id: int, position_version: int) -> dict[str, Any]:
        return await run_protected(self._restore(caller, op_id, position_id, position_version))

    async def _restore(self, caller: Caller, op_id: str, position_id: int, position_version: int) -> dict[str, Any]:
        async def precheck(state: ModelStateSnapshot, position: PositionRecord | None) -> None:
            assert position is not None
            if position.status is not PositionStatus.PENDING or position.pending_reason is not PendingReason.ORIGIN_TO_VERIFY:
                raise not_allowed("只有标为「来源待核对」的待确认仓可以恢复托管")

        async def work(position: PositionRecord) -> tuple[OperationStatus, dict[str, Any]]:
            deps = self._deps
            fills = await self._ledger.read(repo_takeover.key_fills, position.symbol, position.side)
            run = open_run(fills, position.side)
            opening = run[0] if run else None
            parsed = ids.parse_client_id(opening.client_order_id if opening else None)
            if opening is not None and parsed.origin is OrderOrigin.SYSTEM:
                ownership = Ownership.AI if parsed.model_code == "3" else Ownership.STRATEGY
                await deps.registry.restore_original(
                    position.id, ownership=ownership, open_time_ms=opening.time_ms,
                    opened_by_intent_id=opening.intent_id, expected_version=position_version,
                    note=f"恢复托管：按开仓单 {opening.client_order_id} 恢复原归属")
                await self._protection.rebuild(position.id, reason="restore")  # 按编号认领保留下来的系统止损
                return OperationStatus.SUCCEEDED, self._result(position.id, restored_as=ownership.value,
                                                               opening_order=opening.client_order_id)
            await self._take_over(position.id, position_version)  # 查不到开仓订单：登记为手动接管
            return OperationStatus.SUCCEEDED, self._result(position.id, restored_as="takeover")

        return await self._run(caller, OperationType.RESTORE, op_id, position_id, position_version, precheck, work)

    # ------------------------------------------------------------ 取消托管

    async def cancel_takeover(self, caller: Caller, *, op_id: str, position_id: int, position_version: int,
                              mode: str = "handover") -> dict[str, Any]:
        if mode not in CANCEL_MODES:
            raise invalid(f"取消托管的方式只能是 handover（保留止损并移交）或 cancel_stop（撤掉系统止损）：{mode!r}")
        return await run_protected(self._cancel_takeover(caller, op_id, position_id, position_version, mode))

    async def _cancel_takeover(self, caller: Caller, op_id: str, position_id: int, position_version: int,
                               mode: str) -> dict[str, Any]:
        async def precheck(state: ModelStateSnapshot, position: PositionRecord | None) -> None:
            assert position is not None
            if position.status not in (PositionStatus.MANAGED, PositionStatus.TAKING_OVER) or not (
                    position.ownership is Ownership.TAKEOVER or position.is_mixed):
                raise not_allowed("只有托管中的接管仓与混合仓可以取消托管")

        async def work(position: PositionRecord) -> tuple[OperationStatus, dict[str, Any]]:
            deps = self._deps

            async def finalize() -> None:  # 在保护单管理的持仓锁里把登记簿改成外部（已忽略）；版本在受理时已校验
                current = deps.registry.by_id(position.id)
                if current is not None and current.is_mixed:
                    await deps.registry.ignore_mixed(position.id, expected_version=None)
                else:
                    await deps.registry.ignore(position.id, expected_version=None)

            word = await self._protection.cancel_takeover(position.id, mode=mode, finalize=finalize)
            if word == "done":
                return OperationStatus.SUCCEEDED, self._result(position.id, mode=mode)
            if word == "busy":
                raise unavailable("保护单管理还有未出结论的挂止损或换止损，请稍后重试；状态没有改变")
            if word == "stop_cancel_failed":
                raise unavailable("撤掉系统止损没有成功，状态没有改变，请重试或改选保留止损并移交")
            if word == "closed":
                raise not_allowed("这个持仓已经平掉")
            raise not_allowed("只有托管中的接管仓与混合仓可以取消托管")

        return await self._run(caller, OperationType.CANCEL_TAKEOVER, op_id, position_id, position_version, precheck,
                               work, params={"mode": mode})

    # ------------------------------------------------------------ 交给系统管理止损

    async def hand_stop_to_system(self, caller: Caller, *, op_id: str, position_id: int,
                                  position_version: int) -> dict[str, Any]:
        return await run_protected(self._hand(caller, op_id, position_id, position_version))

    async def _hand(self, caller: Caller, op_id: str, position_id: int, position_version: int) -> dict[str, Any]:
        async def precheck(state: ModelStateSnapshot, position: PositionRecord | None) -> None:
            assert position is not None
            if (position.ownership is not Ownership.TAKEOVER or position.status is not PositionStatus.MANAGED
                    or position.protection_state is not ProtectionState.EXTERNAL):
                raise not_allowed("只有保护状态为「外部保护」的接管仓可以交给系统管理止损")

        async def work(position: PositionRecord) -> tuple[OperationStatus, dict[str, Any]]:
            word = await self._protection.hand_stop_to_system(position.id)
            if word == "skipped":
                raise not_allowed("只有保护状态为「外部保护」的接管仓可以交给系统管理止损")
            if word == "unknown":
                raise unavailable("现在读不到交易所的条件单或价格，请稍后重试；用户的止损没有动")
            status = OperationStatus.SUCCEEDED if word == "protected" else OperationStatus.PARTIAL
            return status, self._result(position.id, outcome=word)

        return await self._run(caller, OperationType.HAND_STOP_TO_SYSTEM, op_id, position_id, position_version, precheck,
                               work)
