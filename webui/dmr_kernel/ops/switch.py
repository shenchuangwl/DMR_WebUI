"""模型切换（第 6 层 ops，报告 F6）。

``Switch(deps, *, intents, close_all, bus, crash, poll_s=0.5)``（调用方都要是持有席位的会话；本机令牌不能切换）：

- ``await preview(caller, *, op_id, target)`` → 结果字典（也存在操作记录里，重复的操作号返回同一份）::
      {"target","model_version","current":{"active_model","gate","suppressions","op_locks"},"counts_by_ownership",
       "open_orders":[...],"expires_ms","notes":[...]}
  置「切换预览中」锁（有效期 ``switch_preview_ttl_s``），预览期间不新开仓（协调者据 ``can_open`` 跳过并写进意图原因，留痕）。
  ``model_version`` 是拿锁之后的状态版本，确认时拿它核对。
- ``await cancel(caller, *, op_id)`` → ``{"cancelled": True}``：只有发起预览的会话能取消。
- ``await confirm(caller, *, op_id, model_version, mode, password=None, verify=None)`` → ``(记录, 报文)``，``mode`` 是
  ``"keep"``（保留原规则继续托管，同步做完）或 ``"close_all_first"``（先走一键全平，范围固定全部托管持仓，立即返回
  ``status="running"``，后台做完；全部结果为已平或已由外部平掉才继续）。报文 ``{"status","active_model","gate","detail"}``，
  失败时另有 ``reason``（停不稳、平不完等：留在旧模型的已停止，不是错误码，HTTP 仍是 200）。
- ``await wait(op_id)``：等先平后切的后台任务；``await expire_due()``：清掉到期的预览（返回是否清了）；
  ``await on_seat_changed(holder_session_id)``：席位被释放或被别人拿走时取消预览（持有者自己续租不取消）；
  ``await start()`` / ``await stop()``：订阅总线上的 ``SeatChanged`` 事件（按类名与字段识别：ops 层不能导入 api 层），
  并起一个每秒清理到期预览的任务；
- ``crashed``：中断点 8（``switch_midway``，F6 第 4–6 步之间）触发时的 ``CrashSignal``。

执行（F6 第 3–8 步）：加「切换中」锁 → 关闸、撤未成交的开仓单 → ``switch_settle_s`` 内停稳（开仓意图全部终态、没有状态未知的订单、
没有接管中或保护重建中的持仓）→ **中断点 8** → 同一事务写新生效模型并解锁（``modelstate.set_active_model``，一律已停止）。
停不稳就释放锁，留在旧模型的已停止。

``BulkOps(close_all, switch)``：路由层从 ``app.state.bulk_ops`` 取这一对服务。
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from typing import Any

from dmr_kernel.core.crashpoints import CrashRegistry, CrashSignal
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.modelstate import OpLockConflict, StaleVersion, allowed_operations
from dmr_kernel.core.types import (
    Gate,
    IntentAction,
    Model,
    OperationStatus,
    OperationType,
    OpLock,
    Ownership,
    PositionStatus,
    ProtectionState,
    OrderState,
)
from dmr_kernel.ledger.records import OperationRecord
from dmr_kernel.ops.close_all import CloseAll
from dmr_kernel.ops.common import (
    Caller,
    OpRejected,
    OpsDeps,
    Verifier,
    admit,
    finish_op,
    invalid,
    jsonable_result,
    not_allowed,
    require_seat,
    run_protected,
    seat_required,
)

log = logging.getLogger(__name__)

__all__ = ["BulkOps", "MODES", "Switch"]

MODES = ("keep", "close_all_first")
MODE_LABEL = {"keep": "保留原规则继续托管", "close_all_first": "先全部平仓再切换"}
OP_ID_ROOM = 61
_UNKNOWN_ORDER = frozenset({OrderState.SENT_UNKNOWN, OrderState.PENDING_SEND})


@dataclasses.dataclass
class BulkOps:
    close_all: CloseAll
    switch: "Switch"


@dataclasses.dataclass
class _Preview:
    op_id: str
    session_id: int
    target: Model
    expires_ms: int


class Switch:
    def __init__(self, deps: OpsDeps, *, intents: Any, close_all: CloseAll, bus: EventBus | None,
                 crash: CrashRegistry | None, poll_s: float = 0.5) -> None:
        self._deps = deps
        self._intents = intents
        self._close_all = close_all
        self._bus = bus
        self._crash = crash
        self._poll_s = poll_s
        self._preview: _Preview | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._sub: Any = None
        self._sweeper: asyncio.Task | None = None
        self.crashed: CrashSignal | None = None

    # ================================================================ 预览

    @staticmethod
    def _need_session(caller: Caller) -> int:
        if not caller.is_session or caller.session_id is None:
            raise seat_required("切换模型只能由持有操作席位的会话进行，本机令牌不能切换")
        require_seat(caller)  # 先于允许表：没有席位的会话看到的是「需要席位」，不是别的原因
        return caller.session_id

    def _target_of(self, target: Any) -> Model:
        try:
            model = Model(target) if isinstance(target, str) else None
        except ValueError:
            model = None
        if model is None:
            raise invalid("目标模型必须是 m1、m2 或 m3")
        if model is self._deps.modelstate.current().active_model:
            raise invalid("目标模型就是当前生效模型，无需切换")
        return model

    async def preview(self, caller: Caller, *, op_id: str, target: Any) -> dict[str, Any]:
        return await run_protected(self._preview_op(caller, op_id, target))

    async def _preview_op(self, caller: Caller, op_id: str, target: Any) -> dict[str, Any]:
        deps = self._deps
        session_id = self._need_session(caller)
        model = self._target_of(target)
        admission = await admit(deps, caller, op_id=op_id, op_type=OperationType.SWITCH, allowed_key="switch",
                                scope=model.value, params={"target": model.value})
        if not admission.is_new:
            return jsonable_result(admission.record.result or {})
        now = deps.clock.now_ms()
        expires = now + int(deps.config_get("switch_preview_ttl_s")) * 1000
        try:
            await deps.modelstate.acquire_op_lock(OpLock.SWITCH_PREVIEW, owner=f"session:{session_id}",
                                                  expires_ms=expires)
        except OpLockConflict as exc:
            await finish_op(deps, op_id, OperationStatus.REJECTED, {"message": str(exc)})
            raise not_allowed(str(exc)) from None
        self._preview = _Preview(op_id, session_id, model, expires)
        state = deps.modelstate.current()
        counts = {owner.value: 0 for owner in (Ownership.STRATEGY, Ownership.TAKEOVER, Ownership.AI, Ownership.NONE)}
        for rec in deps.registry.all_active():
            counts[rec.ownership.value] += 1
        orders = [{"client_order_id": o.client_order_id, "symbol": o.symbol,
                   "position_side": getattr(o.position_side, "value", o.position_side),
                   "purpose": getattr(o.purpose, "value", o.purpose), "qty": o.qty,
                   "state": o.state.value} for o in await deps.orders.non_terminal()]
        notes = ["已有持仓的归属与退出规则不变；切走后策略仓仍按信号平仓"]
        if model is Model.M3:
            notes.append("切到模型 3：不开仓，也不接管新仓")
        if state.gate is Gate.RUNNING:
            notes.append("当前在运行中：确认切换时会先停止（关闸、撤未成交的开仓单）")
        notes.append("切换完成后开仓闸一律是已停止，需要手动启动")
        result = {
            "target": model.value, "model_version": state.version,
            "current": {"active_model": state.active_model.value, "gate": state.gate.value,
                        "suppressions": sorted(s.value for s in state.suppressions),
                        "op_locks": sorted(lock.value for lock in state.op_locks)},
            "counts_by_ownership": counts, "open_orders": orders, "expires_ms": expires, "notes": notes}
        result = jsonable_result(result)
        await finish_op(deps, op_id, OperationStatus.SUCCEEDED, result)
        return result

    # ================================================================ 取消、到期、席位

    async def cancel(self, caller: Caller, *, op_id: str) -> dict[str, Any]:
        return await run_protected(self._cancel_op(caller, op_id))

    async def _cancel_op(self, caller: Caller, op_id: str) -> dict[str, Any]:
        deps = self._deps
        self._need_session(caller)

        async def mine(state: Any, position: Any) -> None:
            if self._preview is not None and self._preview.session_id != caller.session_id:
                raise not_allowed("这个切换预览属于另一个会话，只有发起它的会话可以取消")

        admission = await admit(deps, caller, op_id=op_id, op_type=OperationType.SWITCH, allowed_key="switch_cancel",
                                params={"cancel": True}, precheck=mine)
        if not admission.is_new:
            return {"cancelled": True}
        await self._drop_preview()
        await finish_op(deps, op_id, OperationStatus.SUCCEEDED, {"cancelled": True})
        return {"cancelled": True}

    async def _drop_preview(self) -> None:
        self._preview = None
        await self._deps.modelstate.release_op_lock(OpLock.SWITCH_PREVIEW)

    async def expire_due(self) -> bool:
        state = self._deps.modelstate.current()
        if OpLock.SWITCH_PREVIEW not in state.op_locks:
            return False
        expires = self._preview.expires_ms if self._preview is not None else state.op_lock_expires_ms
        if expires is None or self._deps.clock.now_ms() < expires:
            return False
        await self._drop_preview()
        return True

    async def on_seat_changed(self, holder_session_id: int | None) -> bool:
        if OpLock.SWITCH_PREVIEW not in self._deps.modelstate.current().op_locks:
            return False
        if self._preview is not None and holder_session_id == self._preview.session_id:
            return False
        await self._drop_preview()
        return True

    def _on_event(self, event: Any) -> Any:
        if type(event).__name__ == "SeatChanged" and hasattr(event, "holder_session_id"):
            return self.on_seat_changed(event.holder_session_id)
        return None

    async def start(self) -> None:
        if self._bus is not None and self._sub is None:
            self._sub = self._bus.subscribe(None, self._on_event, name="switch-seat-watch")
        if self._sweeper is None:
            self._sweeper = asyncio.ensure_future(self._sweep())

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.expire_due()
            except Exception:
                log.exception("清理到期的切换预览出错")

    async def stop(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sweeper
            self._sweeper = None

    # ================================================================ 确认

    async def wait(self, op_id: str) -> None:
        done = self._done.get(op_id)
        if done is not None:
            await done.wait()

    def _payload_of(self, record: OperationRecord) -> dict[str, Any]:
        if isinstance(record.result, dict) and "status" in record.result:
            return jsonable_result(record.result)
        state = self._deps.modelstate.current()
        return {"status": "running", "active_model": state.active_model.value, "gate": state.gate.value,
                "detail": "先全部平仓，全部已平后再切换模型"}

    async def confirm(self, caller: Caller, *, op_id: str, model_version: int, mode: Any, password: str | None = None,
                      verify: Verifier | None = None) -> tuple[OperationRecord, dict[str, Any]]:
        return await run_protected(self._confirm_op(caller, op_id, model_version, mode, password, verify))

    async def _confirm_op(self, caller: Caller, op_id: str, model_version: int, mode: Any, password: str | None,
                          verify: Verifier | None) -> tuple[OperationRecord, dict[str, Any]]:
        deps = self._deps
        self._need_session(caller)
        if mode not in MODES:
            raise invalid("切换方式必须是 keep（保留原规则）或 close_all_first（先全部平仓再切换）")
        preview = self._preview

        async def mine_and_fresh(state: Any, position: Any) -> None:
            current = self._preview
            if current is None:
                raise not_allowed("切换预览已失效，请重新预览")
            if current.session_id != caller.session_id:
                raise not_allowed("这个切换预览属于另一个会话，只有发起它的会话可以确认")
            if deps.clock.now_ms() >= current.expires_ms:
                await self._drop_preview()
                raise not_allowed("切换预览已过期，请重新预览")

        admission = await admit(deps, caller, op_id=op_id, op_type=OperationType.SWITCH, allowed_key="switch_confirm",
                                model_version=model_version, reauth=True, password=password, verify=verify,
                                scope=preview.target.value if preview else None, params={"mode": mode},
                                precheck=mine_and_fresh)
        if not admission.is_new:
            return admission.record, self._payload_of(admission.record)
        target = self._preview.target
        old = admission.state.active_model
        if mode == "keep":
            return await self._commit(caller, op_id, target, old, mode, None, model_version)
        return await self._start_close_first(caller, admission.record, target, old)

    # ------------------------------------------------------------ 先平后切

    async def _start_close_first(self, caller: Caller, record: OperationRecord, target: Model,
                                 old: Model) -> tuple[OperationRecord, dict[str, Any]]:
        deps = self._deps
        op_id = record.op_id
        inner_id = f"{op_id[:OP_ID_ROOM]}:ca"
        fresh = dataclasses.replace(caller, reauth_ms=deps.clock.now_ms()) if caller.is_session else caller
        try:
            await self._close_all.start(fresh, op_id=inner_id, scope="all_managed",
                                        model_version=deps.modelstate.current().version, parent_op_id=op_id)
        except OpRejected as exc:
            await finish_op(deps, op_id, OperationStatus.REJECTED, {"status": "failed", "reason": exc.message,
                                                                    "active_model": old.value, "gate": "stopped"})
            raise
        self._preview = None
        done = asyncio.Event()
        self._done[op_id] = done
        self._tasks[op_id] = asyncio.ensure_future(self._after_close_all(caller, op_id, inner_id, target, old, done))
        return record, self._payload_of(record)

    async def _after_close_all(self, caller: Caller, op_id: str, inner_id: str, target: Model, old: Model,
                               done: asyncio.Event) -> None:
        deps = self._deps
        rows: list = []
        crashed = False
        try:
            await self._close_all.wait(inner_id)
            inner = await deps.operations.get(inner_id)
            rows = list(inner.per_position or []) if inner is not None else []
            if inner is None or inner.status is not OperationStatus.SUCCEEDED:
                bad = [f"{r['symbol']} {r['side']}：{r['result']}" for r in rows
                       if r["result"] not in ("closed", "closed_externally")]
                why = "一键全平没有全部完成" + (f"（{'；'.join(bad[:5])}）" if bad else "") + "，留在旧模型的已停止"
                if inner is not None and inner.status is OperationStatus.ABORTED:
                    why = "一键全平被中止，留在旧模型的已停止"
                await self._fail(op_id, old, why, rows)
                return
            permission = allowed_operations(deps.modelstate.current())["switch"]
            if not permission.allowed:
                await self._fail(op_id, old, f"{permission.reason}，留在旧模型的已停止", rows)
                return
            await self._commit(caller, op_id, target, old, "close_all_first", rows, None)
        except CrashSignal as signal:
            self.crashed = signal
            crashed = True
        except Exception as exc:
            log.exception("先平后切 %s 出现内部错误", op_id)
            await self._release_locks()
            with contextlib.suppress(Exception):
                await finish_op(deps, op_id, OperationStatus.FAILED,
                                {"status": "failed", "active_model": old.value, "gate": "stopped",
                                 "reason": f"内部错误（{type(exc).__name__}），详情见内核日志"})
        finally:
            if not crashed:
                done.set()
                self._tasks.pop(op_id, None)

    # ------------------------------------------------------------ 切换本体（F6 第 3–8 步）

    async def _release_locks(self) -> None:
        ms = self._deps.modelstate
        for lock in (OpLock.SWITCHING, OpLock.SWITCH_PREVIEW):
            with contextlib.suppress(OpLockConflict):
                await ms.release_op_lock(lock)
        self._preview = None

    async def _fail(self, op_id: str, old: Model, reason: str,
                    per_position: Any = None) -> tuple[OperationRecord, dict[str, Any]]:
        deps = self._deps
        await self._release_locks()
        if deps.modelstate.current().gate is not Gate.STOPPED:
            await deps.modelstate.set_gate(Gate.STOPPED, reason=f"切换失败（操作 {op_id}）")
        payload = {"status": "failed", "active_model": old.value, "gate": "stopped", "reason": reason}
        record = await finish_op(deps, op_id, OperationStatus.FAILED, payload, per_position=per_position)
        return record, payload

    async def _unsettled(self) -> list[str]:
        deps = self._deps
        reasons: list[str] = []
        opens = await self._intents.non_terminal(action=IntentAction.OPEN)
        if opens:
            reasons.append(f"仍有 {len(opens)} 个开仓意图没到终态")
        unknown = [o for o in await deps.orders.non_terminal() if o.state in _UNKNOWN_ORDER]
        if unknown:
            reasons.append(f"仍有 {len(unknown)} 个状态未知的订单")
        registered = deps.registry.registered()
        taking = [r for r in registered if r.status is PositionStatus.TAKING_OVER]
        if taking:
            reasons.append(f"仍有 {len(taking)} 个接管中的持仓")
        rebuilding = [r for r in registered if r.protection_state is ProtectionState.REBUILDING]
        if rebuilding:
            reasons.append(f"仍有 {len(rebuilding)} 个持仓的保护在重建中")
        return reasons

    async def _commit(self, caller: Caller, op_id: str, target: Model, old: Model, mode: str, per_position: Any,
                      expected_version: int | None) -> tuple[OperationRecord, dict[str, Any]]:
        deps = self._deps
        # 停稳的时限从停旧模型这一步开始算（F6：switch_settle_s 秒内停不稳就放弃）
        deadline = deps.clock.now_ms() + int(deps.config_get("switch_settle_s")) * 1000
        try:
            try:
                await deps.modelstate.acquire_op_lock(OpLock.SWITCHING, owner=op_id, expected_version=expected_version)
            except (OpLockConflict, StaleVersion) as exc:
                return await self._fail(op_id, old, f"{exc}，留在旧模型的已停止", per_position)
            await deps.modelstate.set_gate(Gate.STOPPED, reason=f"模型切换（操作 {op_id}）：先停旧模型")
            try:
                await deps.coordinator.cancel_unfilled_opens(model=None, reason=f"模型切换（操作 {op_id}）")
            except Exception:
                log.exception("切换时撤未成交的开仓单出错（操作 %s）", op_id)
            while True:
                reasons = await self._unsettled()
                if not reasons:
                    break
                if deps.clock.now_ms() >= deadline:
                    why = f"{int(deps.config_get('switch_settle_s'))} 秒内旧模型没有停稳：{'；'.join(reasons)}，留在旧模型的已停止"
                    return await self._fail(op_id, old, why, per_position)
                await asyncio.sleep(self._poll_s)
            if self._crash is not None:
                self._crash.hit("switch_midway")
            try:
                await deps.modelstate.set_active_model(
                    target, expected_version=deps.modelstate.current().version, operator=caller.operator, op_id=op_id,
                    mode=MODE_LABEL[mode])
            except (OpLockConflict, StaleVersion) as exc:
                return await self._fail(op_id, old, f"{exc}，留在旧模型的已停止", per_position)
            self._preview = None
            payload = {"status": "succeeded", "active_model": target.value, "gate": "stopped",
                       "detail": f"生效模型已由 {old.value} 切换为 {target.value}（{MODE_LABEL[mode]}）；开仓闸已停止，需要手动启动"}
            record = await finish_op(deps, op_id, OperationStatus.SUCCEEDED, payload, per_position=per_position)
            return record, payload
        except CrashSignal as signal:
            self.crashed = signal
            raise
