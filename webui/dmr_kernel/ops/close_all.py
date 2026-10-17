"""一键全平（第 6 层 ops，报告 F5）。

``CloseAll(deps, *, intents, bus, crash, poll_s=0.5)``：
- ``await preview(scope)``：``scope`` 是 ``"model"``（当前生效模型归属）或 ``"all_managed"``（全部托管持仓）。返回
  ``{"scope","model_version","included":[PositionRecord],"excluded":[{"position","reason"}],"mixed_included":[PositionRecord]}``。
  待确认与外部（已忽略）永不包含；模型 1 的默认范围含混合仓并单独标出；模型 3 的默认范围为空。允许表不允许（切换中、
  批量平仓中）409 ``not_allowed``；范围取值不对 422 ``invalid``。
- ``await start(caller, *, op_id, scope, model_version, password=None, verify=None, parent_op_id=None)`` → ``(记录, 是否新建)``：
  统一步骤（``ops.common.admit``，需口令）后关闸、加「批量平仓中」锁（这会取消切换预览），然后在后台任务里做完。
  新建的记录是进行中；重复的操作号返回第一次的记录。
- ``await wait(op_id)``：等该操作的后台任务结束（没有后台任务就立即返回）。
- ``await abort(caller, *, op_id, target_op_id)`` → ``{"aborted": True}``：中止进行中的批量平仓；返回时锁已释放、闸已停止、
  记录已是 ``aborted``。不再发新单与重发。重复的操作号返回同样的答复。
- ``await tick()``：把「状态未知」而保持平仓中的持仓，在 ``partial_close_pause_s`` 秒后恢复托管（装配层定时调用）。
- ``crashed``：中断点 6（``close_all_midway``）触发时的 ``CrashSignal``（模拟进程被杀：不收尾，锁与进行中的记录留给恢复）。

模块函数（给 M2A 的 ``/ops/stop`` 带 ``close_all`` 用）::

    install(close_all: CloseAll | None)       # 装配层把这个模块要用的实例交进来（None 表示撤下）
    await start_close_all(deps, caller, *, op_id, model_version, scope, parent_op_id)  # → 记录；没装配 501 not_implemented

执行（F5 第 4–12 步）：等在途开仓意图（最多 ``close_all_wait_open_s``）后才取清单；逐仓并发提交 CLOSE（幂等键
``op:<操作号>:<合约>:<方向>:r<轮>``）；每仓的时限是 ``close_one_timeout_s``（仅轮询降级时加倍），整体时限
``close_all_timeout_s``；五种逐仓结果 closed / closed_externally / failed / unknown / residual；收尾复查最多两轮
（``MAX_RECHECK_ROUNDS``）重平 failed 与 residual，unknown 不重发（重发由协调者按回查规则决定），只在最后再读一次；
时限到或被中止后锁立即释放、模型回到已停止。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from decimal import Decimal
from typing import Any

from dmr_kernel.core import intents as intent_keys
from dmr_kernel.core.crashpoints import CrashRegistry, CrashSignal
from dmr_kernel.core.events import EventBus, OperationProgress
from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.types import (
    ExitReason,
    Gate,
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    Model,
    OperationStatus,
    OperationType,
    OpLock,
    Ownership,
    PerPositionResult,
    PositionStatus,
    RunMode,
)
from dmr_kernel.core.modelstate import OpLockConflict
from dmr_kernel.ledger.records import OperationRecord, PositionRecord
from dmr_kernel.ops.common import (
    Caller,
    OpRejected,
    OpsDeps,
    Verifier,
    admit,
    finish_op,
    intent_wait_s,
    invalid,
    not_allowed,
    not_found,
    not_implemented,
    require_allowed,
    run_protected,
)

log = logging.getLogger(__name__)

__all__ = ["MAX_RECHECK_ROUNDS", "SCOPES", "CloseAll", "install", "start_close_all"]

MAX_RECHECK_ROUNDS = 2
SCOPES = ("model", "all_managed")
R = PerPositionResult
_GOOD = frozenset({R.CLOSED, R.CLOSED_EXTERNALLY})
_RETRY = frozenset({R.FAILED, R.RESIDUAL})
_EXTERNAL_EXITS = frozenset({ExitReason.STOP, ExitReason.EXTERNAL, ExitReason.LIQUIDATION, ExitReason.ADL,
                             ExitReason.SETTLEMENT})
_OWNER_MODEL = {Ownership.STRATEGY: 1, Ownership.TAKEOVER: 2, Ownership.AI: 3}
_OWNER_LABEL = {Ownership.STRATEGY: "策略开仓", Ownership.TAKEOVER: "手动接管", Ownership.AI: "AI"}
_MODEL_OWNERSHIP = {Model.M1: Ownership.STRATEGY, Model.M2: Ownership.TAKEOVER}
OP_ID_ROOM = 61


def _model_no(model: Model) -> str:
    return model.value[1:]


@dataclasses.dataclass
class _Run:
    op_id: str
    scope: str
    caller: Caller
    started_ms: int
    abort: asyncio.Event
    done: asyncio.Event
    rows: dict[int, dict[str, Any]] = dataclasses.field(default_factory=dict)
    paused: dict[int, int] = dataclasses.field(default_factory=dict)  # 我们暂停的持仓 → 暂停时间
    submitted: set[int] = dataclasses.field(default_factory=set)
    rounds: int = 0
    timed_out: bool = False
    dead: bool = False  # 中断点 6 触发后：不再做任何事
    unknown_open_orders: list = dataclasses.field(default_factory=list)


class CloseAll:
    def __init__(self, deps: OpsDeps, *, intents: Any, bus: EventBus | None, crash: CrashRegistry | None,
                 poll_s: float = 0.5) -> None:
        self._deps = deps
        self._intents = intents
        self._bus = bus
        self._crash = crash
        self._poll_s = poll_s
        self._runs: dict[str, _Run] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._paused_after: dict[int, int] = {}  # 状态未知而保持平仓中的持仓 → 开始恢复的时间
        self.crashed: CrashSignal | None = None

    # ================================================================ 范围与预览

    def _classify(self, scope: str, state: Any) -> tuple[list[PositionRecord], list[dict[str, Any]]]:
        included: list[PositionRecord] = []
        excluded: list[dict[str, Any]] = []
        registered = PositionStatus.registered()
        model = state.active_model
        for rec in self._deps.registry.all_active():
            if rec.status is PositionStatus.PENDING:
                excluded.append({"position": rec, "reason": "待确认的持仓永不包含在一键全平里，请先确认归属或忽略"})
            elif rec.status is PositionStatus.IGNORED:
                excluded.append({"position": rec, "reason": "外部持仓（已忽略）永不包含在一键全平里"})
            elif rec.status not in registered:
                continue
            elif scope == "all_managed":
                included.append(rec)
            elif model is Model.M3:
                excluded.append({"position": rec, "reason": "当前生效模型是模型 3：默认范围为空（可改选「全部托管持仓」）"})
            elif rec.ownership is _MODEL_OWNERSHIP.get(model):
                included.append(rec)
            else:
                owner = _OWNER_LABEL.get(rec.ownership, "其它")
                from_model = _OWNER_MODEL.get(rec.ownership)
                tag = f"（模型 {from_model}）" if from_model else ""
                excluded.append({"position": rec, "reason": f"归属{owner}{tag}，不在当前模型（模型 {_model_no(model)}）的默认范围内"})
        return included, excluded

    @staticmethod
    def _check_scope(scope: Any) -> str:
        if scope not in SCOPES:
            raise invalid("范围必须是 model（当前模型归属）或 all_managed（全部托管持仓）")
        return scope

    async def preview(self, scope: str) -> dict[str, Any]:
        self._check_scope(scope)
        state = self._deps.modelstate.current()
        require_allowed(state, "close_all")
        included, excluded = self._classify(scope, state)
        return {"scope": scope, "model_version": state.version, "included": included, "excluded": excluded,
                "mixed_included": [rec for rec in included if rec.is_mixed]}

    # ================================================================ 启动

    async def start(self, caller: Caller, *, op_id: str, scope: str, model_version: int, password: str | None = None,
                    verify: Verifier | None = None, parent_op_id: str | None = None) -> tuple[OperationRecord, bool]:
        return await run_protected(self._start(caller, op_id, scope, model_version, password, verify, parent_op_id))

    async def _start(self, caller: Caller, op_id: str, scope: str, model_version: int, password: str | None,
                     verify: Verifier | None, parent_op_id: str | None) -> tuple[OperationRecord, bool]:
        deps = self._deps
        self._check_scope(scope)
        params: dict[str, Any] = {"scope": scope}
        if parent_op_id:
            params["parent_op_id"] = parent_op_id
        admission = await admit(deps, caller, op_id=op_id, op_type=OperationType.CLOSE_ALL, allowed_key="close_all",
                                model_version=model_version, reauth=True, password=password, verify=verify,
                                scope=scope, params=params)
        if not admission.is_new:
            return admission.record, False
        try:
            await deps.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner=op_id)
            await deps.modelstate.set_gate(Gate.STOPPED, reason=f"一键全平（操作 {op_id}，{caller.operator}）")
        except OpLockConflict as exc:
            await finish_op(deps, op_id, OperationStatus.REJECTED, {"message": str(exc)})
            raise not_allowed(str(exc)) from None
        except BaseException as exc:
            await self._release(op_id)
            await self._fail(op_id, exc)
            raise
        run = _Run(op_id=op_id, scope=scope, caller=caller, started_ms=deps.clock.now_ms(), abort=asyncio.Event(),
                   done=asyncio.Event())
        self._runs[op_id] = run
        self._tasks[op_id] = asyncio.ensure_future(self._main(run))
        return admission.record, True

    async def wait(self, op_id: str) -> None:
        run = self._runs.get(op_id)
        if run is not None:
            await run.done.wait()

    # ================================================================ 中止

    async def abort(self, caller: Caller | None = None, *, op_id: str, target_op_id: str, operator: str | None = None,
                    session_id: int | None = None) -> dict[str, Any]:
        """``caller`` 缺省时用 ``operator`` / ``session_id`` 现造一个（已通过席位检查的主体，内部与测试用）。"""
        if caller is None:
            caller = Caller(operator=operator or "cli", session_id=session_id, is_session=session_id is not None,
                            holds_seat=True)
        return await run_protected(self._abort(caller, op_id, target_op_id))

    async def _abort(self, caller: Caller, op_id: str, target_op_id: str) -> dict[str, Any]:
        deps = self._deps

        async def target_must_run(state: Any, position: Any) -> None:
            run = self._runs.get(target_op_id)
            if run is None or run.done.is_set():
                raise not_found(f"没有正在进行的批量平仓（操作号 {target_op_id}）")

        admission = await admit(deps, caller, op_id=op_id, op_type=OperationType.ABORT, allowed_key="abort",
                                scope=target_op_id, params={"target_op_id": target_op_id}, precheck=target_must_run)
        if not admission.is_new:
            return {"aborted": True}
        run = self._runs[target_op_id]
        run.abort.set()
        await run.done.wait()
        await finish_op(deps, op_id, OperationStatus.SUCCEEDED, {"aborted": True})
        return {"aborted": True}

    # ================================================================ 后台执行

    async def _main(self, run: _Run) -> None:
        deps = self._deps
        try:
            await self._execute(run)
            if run.dead:
                return  # 中断点 6：进程「被杀」，没有收尾
            await self._conclude(run)
        except CrashSignal as signal:
            self.crashed = signal
            run.dead = True
        except Exception as exc:
            log.exception("一键全平 %s 出现内部错误", run.op_id)
            await self._release(run.op_id)
            await self._fail(run.op_id, exc)
        finally:
            if not run.dead:
                run.done.set()
                self._tasks.pop(run.op_id, None)

    def _remaining_ms(self, run: _Run) -> int:
        return run.started_ms + int(self._deps.config_get("close_all_timeout_s")) * 1000 - self._deps.clock.now_ms()

    async def _sleep(self) -> None:
        await asyncio.sleep(self._poll_s)

    async def _wait_open_intents(self, run: _Run) -> list[dict[str, Any]]:
        deps = self._deps
        deadline = deps.clock.now_ms() + int(deps.config_get("close_all_wait_open_s")) * 1000
        while True:
            opens = await self._intents.non_terminal(action=IntentAction.OPEN)
            if not opens or run.abort.is_set() or deps.clock.now_ms() >= deadline:
                break
            await self._sleep()
        return [{"intent_id": i.id, "symbol": i.symbol, "side": i.side.value, "state": i.state.value} for i in opens]

    async def _execute(self, run: _Run) -> None:
        deps = self._deps
        run.unknown_open_orders = await self._wait_open_intents(run)
        included, _ = self._classify(run.scope, deps.modelstate.current())
        pending = [rec.id for rec in included]
        for rec in included:
            run.rows[rec.id] = {"position_id": rec.id, "symbol": rec.symbol, "side": rec.side.value,
                                "result": R.UNKNOWN.value, "detail": "还没有处理"}
        by_id = {rec.id: rec for rec in included}
        for round_no in range(1, MAX_RECHECK_ROUNDS + 2):
            if not pending or run.abort.is_set() or run.dead:
                break
            if self._remaining_ms(run) <= 0:
                run.timed_out = True
                break
            run.rounds = round_no
            await asyncio.gather(*(self._close_one(run, by_id[pid], round_no) for pid in pending))
            if run.dead:
                return
            pending = [pid for pid in pending if R(run.rows[pid]["result"]) in _RETRY]
        if not run.abort.is_set() and not run.dead:
            await self._final_read(run, by_id)

    async def _final_read(self, run: _Run, by_id: dict[int, PositionRecord]) -> None:
        """状态未知的持仓不重发，只在最后再读一次即时持仓。"""
        for pid, row in run.rows.items():
            if R(row["result"]) is not R.UNKNOWN or pid not in run.submitted:
                continue
            rec = by_id[pid]
            read = await self._deps.coordinator.live_position(rec.symbol, rec.side)
            if isinstance(read, Known) and read.value == 0:
                self._set(run, rec, R.CLOSED, "收尾复查：即时持仓已为零")

    def _set(self, run: _Run, rec: PositionRecord, result: PerPositionResult, detail: str) -> None:
        row = run.rows[rec.id]
        row.update(result=result.value, detail=detail)
        if self._bus is not None:
            self._bus.publish(OperationProgress(
                op_id=run.op_id, op_type=OperationType.CLOSE_ALL.value, status=OperationStatus.RUNNING.value,
                detail=dict(row), at_ms=self._deps.clock.now_ms()))

    async def _close_one(self, run: _Run, rec: PositionRecord, round_no: int) -> None:
        deps = self._deps
        try:
            await self._close_one_inner(run, rec, round_no)
        except CrashSignal as signal:
            self.crashed = signal
            run.dead = True
        except Exception as exc:
            log.exception("一键全平 %s：%s %s 出错", run.op_id, rec.symbol, rec.side.value)
            self._set(run, rec, R.UNKNOWN, f"处理出错（{type(exc).__name__}），详情见内核日志，请核对持仓")

    async def _close_one_inner(self, run: _Run, rec: PositionRecord, round_no: int) -> None:
        deps = self._deps
        clock = deps.clock
        if run.dead:
            return
        if rec.status is PositionStatus.MANAGED and rec.id not in run.paused:
            await deps.registry.set_closing(rec.id, True, cause="close_all")
            run.paused[rec.id] = clock.now_ms()
        one_s = intent_wait_s(deps)
        # 同一个持仓上有别的意图在途（例如部分平仓）：先等，最多一个单仓时限
        busy_deadline = clock.now_ms() + int(one_s * 1000)
        while deps.coordinator.key_busy(rec.symbol, rec.side):
            if run.abort.is_set() or run.dead:
                self._set(run, rec, R.UNKNOWN, "已中止，平仓单未发出（该持仓上有别的意图在途）")
                return
            if clock.now_ms() >= busy_deadline or self._remaining_ms(run) <= 0:
                break
            await self._sleep()
        if run.abort.is_set() or run.dead:
            self._set(run, rec, R.UNKNOWN, "已中止，平仓单未发出")
            return
        if self._remaining_ms(run) <= 0:
            run.timed_out = True
            self._set(run, rec, R.UNKNOWN, "整体时限已到，平仓单未发出")
            return
        spec = IntentSpec(
            action=IntentAction.CLOSE, model="op",
            source=IntentSource.CLI if run.caller.operator == "cli" and not run.caller.is_session else IntentSource.OPERATOR,
            symbol=rec.symbol, side=rec.side,
            idem_key=intent_keys.key_op(run.op_id, rec.symbol, rec.side, f"r{round_no}"),
            params={"resend_on_unknown": True}, reason=f"一键全平（操作 {run.op_id}）", operator=run.caller.operator,
            op_id=run.op_id, position_id=rec.id, position_generation=rec.generation)
        submitted = await deps.coordinator.submit(spec)
        run.submitted.add(rec.id)
        self._crash_hit()
        intent_id = submitted.intent_id
        remaining = max(self._remaining_ms(run), 0)
        deadline = clock.now_ms() + min(int(one_s * 1000), remaining)
        cut_by_overall = remaining < int(one_s * 1000)
        intent = None
        while True:
            intent = await deps.coordinator.wait_terminal(intent_id, self._poll_s)
            if intent.state in IntentState.terminal() or run.abort.is_set() or run.dead:
                break
            if clock.now_ms() >= deadline:
                if cut_by_overall:
                    run.timed_out = True
                break
        await self._decide(run, rec, intent)

    def _crash_hit(self) -> None:
        if self._crash is not None:
            self._crash.hit("close_all_midway")

    async def _decide(self, run: _Run, rec: PositionRecord, intent: Any) -> None:
        deps = self._deps
        read = await deps.coordinator.live_position(rec.symbol, rec.side)
        reason = (getattr(intent, "state_reason", "") or "").strip()
        flat = isinstance(read, Known) and read.value == 0
        left = isinstance(read, Known) and read.value > 0
        if intent.state is IntentState.COMPLETED:
            if flat:
                fetched = await deps.registry.fetch(rec.id)
                exit_reason = getattr(fetched, "exit_reason", None)
                if exit_reason in _EXTERNAL_EXITS or "其它途径" in reason:
                    self._set(run, rec, R.CLOSED_EXTERNALLY, reason or "全平过程中该持仓已由外部（止损等）平掉")
                else:
                    self._set(run, rec, R.CLOSED, reason or "已平")
            elif left:
                self._set(run, rec, R.RESIDUAL, f"平仓单已完成，但交易所上仍有 {format(read.value, 'f')} 持仓")
            else:
                self._set(run, rec, R.UNKNOWN, f"平仓单已完成，但即时持仓读数失败（{read.reason}），暂记未知")
        elif intent.state in (IntentState.REJECTED, IntentState.EXPIRED, IntentState.CANCELLED):
            if flat:
                self._set(run, rec, R.CLOSED_EXTERNALLY, f"平仓单未成功（{reason or intent.state.value}），但即时持仓已为零")
            else:
                self._set(run, rec, R.FAILED, reason or f"平仓单未成功（{intent.state.value}）")
        else:
            if flat:
                self._set(run, rec, R.CLOSED, "订单回报未到，但即时持仓已为零")
            elif run.abort.is_set():
                self._set(run, rec, R.UNKNOWN, "已中止，订单还没到终态，转入后台继续回查")
            else:
                suffix = f"（即时持仓读数失败：{read.reason}）" if not isinstance(read, Known) else ""
                self._set(run, rec, R.UNKNOWN, "订单还没到终态，转入后台继续回查" + suffix)

    # ================================================================ 收尾

    async def _conclude(self, run: _Run) -> None:
        deps = self._deps
        rows = [run.rows[pid] for pid in sorted(run.rows)]
        aborted = run.abort.is_set()
        if aborted:
            status = OperationStatus.ABORTED
        elif all(R(r["result"]) in _GOOD for r in rows):
            status = OperationStatus.SUCCEEDED
        elif not any(R(r["result"]) in _GOOD for r in rows):
            status = OperationStatus.FAILED
        else:
            status = OperationStatus.PARTIAL
        now = deps.clock.now_ms()
        result = {"per_position": rows, "unknown_open_orders": run.unknown_open_orders,
                  "duration_ms": now - run.started_ms, "timed_out": run.timed_out, "aborted": aborted,
                  "rounds": run.rounds}
        await self._release(run.op_id)  # 锁立即释放、闸保持已停止
        for pid, row in run.rows.items():
            if pid not in run.paused or R(row["result"]) in _GOOD:
                continue  # 已平的持仓没有可恢复的
            if R(row["result"]) is R.UNKNOWN and pid in run.submitted:
                self._paused_after[pid] = now + int(deps.config_get("partial_close_pause_s")) * 1000
            else:
                await self._resume(pid, "close_all_resume")
        await finish_op(deps, run.op_id, status, result, per_position=rows)

    async def _release(self, op_id: str) -> None:
        deps = self._deps
        try:
            await deps.modelstate.release_op_lock(OpLock.BULK_CLOSING, owner=op_id)
        except OpLockConflict:
            await deps.modelstate.release_op_lock(OpLock.BULK_CLOSING)
        if deps.modelstate.current().gate is not Gate.STOPPED:
            await deps.modelstate.set_gate(Gate.STOPPED, reason=f"一键全平收尾（操作 {op_id}）")

    async def _resume(self, position_id: int, cause: str) -> None:
        rec = self._deps.registry.by_id(position_id)
        if rec is None or rec.status is not PositionStatus.CLOSING:
            return
        try:
            await self._deps.registry.set_closing(position_id, False, cause=cause)
        except Exception:  # 恢复不了（例如刚好已平）不能影响收尾
            log.exception("恢复持仓 %s 的托管失败", position_id)

    async def tick(self) -> int:
        """恢复到期的暂停持仓（状态未知的订单不能让止损更新无限期停着）。返回恢复了几个。"""
        now = self._deps.clock.now_ms()
        due = [pid for pid, at in self._paused_after.items() if now >= at]
        for pid in due:
            del self._paused_after[pid]
            await self._resume(pid, "close_all_pause_expired")
        return len(due)

    async def _fail(self, op_id: str, exc: BaseException) -> None:
        try:
            await finish_op(self._deps, op_id, OperationStatus.FAILED,
                            {"message": f"内部错误（{type(exc).__name__}），详情见内核日志"})
        except Exception:
            log.exception("操作 %s 无法记为失败", op_id)


# ---------------------------------------------------------------- 给 M2A 的入口

_installed: CloseAll | None = None


def install(close_all: CloseAll | None) -> None:
    global _installed
    _installed = close_all


async def start_close_all(deps: OpsDeps, caller: Caller, *, op_id: str, model_version: int, scope: str,
                          parent_op_id: str | None = None) -> OperationRecord:
    """``/ops/stop`` 带 close_all 时调用（口令已在上一步校验：调用方给的 caller 带新的校验时间）。"""
    if _installed is None:
        raise not_implemented("一键全平没有装配")
    record, _ = await _installed.start(caller, op_id=op_id, scope=scope, model_version=model_version,
                                       parent_op_id=parent_op_id)
    return record
