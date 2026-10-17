"""模型状态（第 3 层）：生效模型、开仓闸、抑制原因、操作锁、运行方式与模型状态版本（报告 4.6、F6、F7、F9）。

- ``ModelStateSnapshot``：某一时刻的状态快照（不可变），带两个判定：``can_open``（能不能新开仓）、``can_auto_takeover``（能不能自动接管）。
- ``ModelStateService``：唯一能改状态的入口。状态存在账本的 ``model_state`` 表（单行），每次变化在同一个事务里
  版本 +1、追加 ``audit_log``，提交之后才更新内存快照并发布 ``ModelStateChanged``。「读当前行 → 判定 → 写回」整个在
  写线程的一个事务里完成，所以 ``expected_version`` 以库里的版本为准：同一个版本上的并发请求只有一个成功，其余 ``StaleVersion``。
  没有实际变化的调用（加已有的抑制原因、关已经关着的闸……）不增版本、不审计、不发事件。
- ``allowed_operations``：逐格实现报告 4.6「状态与可执行操作」表；内核强制，界面置灰只是提示。

操作锁与版本的约定（文字与 docs/plans/M1-波次2-接口与测试.md 的 W2c 一节一致）：
- 切换预览中只能在没有别的锁时加；切换中要求没有批量平仓中（切换预览中不挡切换中）；批量平仓中要求没有切换中，
  拿到批量平仓中的同时取消切换预览。同一把锁不能重复拿。
- ``op_lock_owner``/``op_lock_expires_ms`` 记最近一次拿锁的持有者与到期时间；确认切换（拿切换中）时有效期清空，不再计时。
  没有任何锁时两者都清空。到期的预览由上层（ops/scheduler）发现后调 ``release_op_lock`` 解除，这里只存。
- ``set_active_model`` 是 F6 第 6 步的原子提交：新生效模型、闸已停止、解除切换中与切换预览中、写 ``model_switch_log``，要么全成要么全不成。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import EventBus, ModelStateChanged
from dmr_kernel.core.types import Gate, Model, OpLock, RunMode, Suppression
from dmr_kernel.ledger import repo_modelstate
from dmr_kernel.ledger.records import ModelStateRecord
from dmr_kernel.ledger.repo_modelstate import Change, SwitchLogEntry
from dmr_kernel.ledger.writer import Ledger

log = logging.getLogger(__name__)

__all__ = [
    "OP_LOCK_LABELS", "OPERATION_KEYS", "SUPPRESSION_LABELS", "ModelStateService", "ModelStateSnapshot", "OpLockConflict",
    "Permission", "StaleVersion", "allowed_operations",
]

SUPPRESSION_LABELS: dict[Suppression, str] = {
    Suppression.STREAM_SWITCHING: "数据流切换中",
    Suppression.RECONCILE_UNKNOWN: "对账未知",
    Suppression.RECONCILE_MISMATCH: "对账不一致",
    Suppression.EXCHANGE_RESTRICTED: "交易所限制",
    Suppression.RISK_TRIPPED: "风控触发（账户级）",
    Suppression.UNPROTECTED: "存在无保护持仓",
    Suppression.RECOVERING: "恢复中",
    Suppression.MUTEX_ANOMALY: "互斥异常",
    Suppression.CLOCK_SKEW: "时钟偏差",
}
OP_LOCK_LABELS: dict[OpLock, str] = {
    OpLock.SWITCH_PREVIEW: "切换预览中",
    OpLock.SWITCHING: "切换中",
    OpLock.BULK_CLOSING: "批量平仓中",
}
OPERATION_KEYS = ("switch", "switch_confirm", "switch_cancel", "start", "stop", "partial_close", "close_one",
                  "close_all", "abort", "takeover", "ignore", "restore", "cancel_takeover", "hand_stop_to_system")

_M3_RESERVED = "模型 3 本期只预留"
_ACTION_PREFIX = "model_state."


# ---------------------------------------------------------------- 异常


class StaleVersion(Exception):
    """请求基于的模型状态版本已过期（库里的版本已经变了）。"""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"模型状态版本已过期：请求基于版本 {expected}，当前是版本 {actual}")
        self.expected = expected
        self.actual = actual


class OpLockConflict(Exception):
    """操作锁规则不允许：别的锁挡着，或锁由别人持有。``held`` 是当时持有的锁。"""

    def __init__(self, message: str, *, held: Iterable[OpLock] = ()) -> None:
        super().__init__(message)
        self.held = frozenset(held)


# ---------------------------------------------------------------- 快照


def _labels(items: Iterable[Any], table: Mapping[Any, str]) -> str:
    present = set(items)
    return "、".join(label for key, label in table.items() if key in present)


def _model_name(model: Model) -> str:
    return f"模型 {model.value[1:]}"


@dataclass(frozen=True)
class ModelStateSnapshot:
    """模型状态的一份快照。字段顺序与默认值（默认行：模型 1、已停止、无抑制无锁、正常、版本 1）便于测试直接构造。"""

    active_model: Model = Model.M1
    gate: Gate = Gate.STOPPED
    suppressions: frozenset[Suppression] = frozenset()
    op_locks: frozenset[OpLock] = frozenset()
    op_lock_owner: str | None = None
    op_lock_expires_ms: int | None = None
    run_mode: RunMode = RunMode.NORMAL
    version: int = 1
    m2_started_ms: int | None = None
    mismatch_since_ms: int | None = None
    risk_detail: dict[str, Any] = field(default_factory=dict, hash=False)
    updated_ms: int = 0

    def can_open(self, model: Model) -> tuple[bool, str]:
        """新开仓的前提：该模型是生效模型、闸开着、没有抑制原因、没有操作锁。降级运行不挡开仓。"""
        if model is Model.M3:
            return False, f"{_M3_RESERVED}，不开仓"
        if model is not self.active_model:
            return False, f"{_model_name(model)}不是生效模型（当前生效：{_model_name(self.active_model)}）"
        if self.gate is not Gate.RUNNING:
            return False, "开仓闸已停止"
        if self.suppressions:
            return False, f"有抑制原因：{_labels(self.suppressions, SUPPRESSION_LABELS)}"
        if self.op_locks:
            return False, f"有操作锁：{_labels(self.op_locks, OP_LOCK_LABELS)}"
        return True, ""

    def can_auto_takeover(self) -> tuple[bool, str]:
        """自动接管的前提：模型 2 生效且运行中、没有操作锁。抑制原因不挡接管（接管是加保护）。"""
        if self.active_model is not Model.M2:
            return False, "模型 2 不是生效模型"
        if self.gate is not Gate.RUNNING:
            return False, "模型 2 未在运行"
        if self.op_locks:
            return False, f"有操作锁：{_labels(self.op_locks, OP_LOCK_LABELS)}"
        return True, ""


def snapshot_from_record(record: ModelStateRecord) -> ModelStateSnapshot:
    return ModelStateSnapshot(
        active_model=record.active_model,
        gate=record.gate,
        suppressions=frozenset(Suppression(x) for x in record.suppressions),
        op_locks=frozenset(OpLock(x) for x in record.op_locks),
        op_lock_owner=record.op_lock_owner,
        op_lock_expires_ms=record.op_lock_expires_ms,
        run_mode=record.run_mode,
        version=record.version,
        m2_started_ms=record.m2_started_ms,
        mismatch_since_ms=record.mismatch_since_ms,
        risk_detail=dict(record.risk_detail or {}),
        updated_ms=record.updated_ms,
    )


# ---------------------------------------------------------------- 状态与可执行操作（报告 4.6）


@dataclass(frozen=True)
class Permission:
    """一个操作现在能不能做。不能做时 ``reason`` 写明原因（中文）；``needs_live_read`` 表示须先取到即时持仓读数；
    ``note`` 是可以做时的附加说明（比如「运行中会自动先停」「只有席位持有者可以确认」）。"""

    allowed: bool
    reason: str = ""
    needs_live_read: bool = False
    note: str = ""


def _yes(note: str = "", live: bool = False) -> Permission:
    return Permission(True, "", live, note)


def _no(reason: str) -> Permission:
    return Permission(False, reason)


def allowed_operations(state: ModelStateSnapshot) -> dict[str, Permission]:
    """逐格实现报告 4.6「状态与可执行操作」表。优先级：切换中 > 批量平仓中 > 切换预览中 > 有抑制原因 > 运行中/已停止。

    补充（F9 末段）：恢复中只允许停止、平掉此仓、一键全平（须先取即时持仓读数），不允许部分平仓、启动、切换；
    模型 3 生效时启动一律不允许，停止、部分平仓、一键全平照常；运行中再启动、已停止再停止都不允许并写明原因。
    有抑制原因时，部分平仓、平掉此仓、一键全平都要先取即时持仓读数（不论是否还有更高优先级的锁）。
    """
    return {
        "switch": _switch(state),
        "switch_confirm": _switch_confirm(state),
        "switch_cancel": _switch_cancel(state),
        "start": _start(state),
        "stop": _stop(state),
        "partial_close": _partial_close(state),
        "close_one": _close_one(state),
        "close_all": _close_all(state),
        "abort": _abort(state),
        "takeover": _takeover(state),
        "ignore": _takeover_family(state, "忽略"),
        "restore": _takeover_family(state, "恢复托管"),
        "cancel_takeover": _takeover_family(state, "取消托管"),
        "hand_stop_to_system": _takeover_family(state, "交给系统管理止损"),
    }


def _suppressed(state: ModelStateSnapshot) -> str:
    return f"有抑制原因（{_labels(state.suppressions, SUPPRESSION_LABELS)}）"


def _switch(state: ModelStateSnapshot) -> Permission:
    if OpLock.SWITCHING in state.op_locks:
        return _no("切换进行中，不能再发起切换")
    if OpLock.BULK_CLOSING in state.op_locks:
        return _no("批量平仓进行中，不能切换模型")
    if OpLock.SWITCH_PREVIEW in state.op_locks:
        return _no("已有切换预览：请由席位持有者确认或取消")
    if state.suppressions:
        return _no(f"{_suppressed(state)}：不能切换模型")
    return _yes("运行中切换会自动先停" if state.gate is Gate.RUNNING else "")


def _switch_confirm(state: ModelStateSnapshot) -> Permission:
    if OpLock.SWITCHING in state.op_locks:
        return _no("切换已在进行中")
    if OpLock.BULK_CLOSING in state.op_locks:
        return _no("批量平仓进行中，不能切换模型")
    if OpLock.SWITCH_PREVIEW not in state.op_locks:
        return _no("没有进行中的切换预览")
    if state.suppressions:  # F6 第 3 步：有抑制原因时拒绝切换，预览期间新出现的也一样，只能取消
        return _no(f"{_suppressed(state)}：不能确认切换，请取消预览")
    return _yes("只有操作席位持有者可以确认")


def _switch_cancel(state: ModelStateSnapshot) -> Permission:
    if OpLock.SWITCHING in state.op_locks:
        return _no("切换已在进行中，不能取消")
    if OpLock.BULK_CLOSING in state.op_locks:
        return _no("批量平仓进行中，没有可取消的切换预览")
    if OpLock.SWITCH_PREVIEW not in state.op_locks:
        return _no("没有进行中的切换预览")
    return _yes("只有操作席位持有者可以取消")


def _start(state: ModelStateSnapshot) -> Permission:
    if state.active_model is Model.M3:
        return _no(f"{_M3_RESERVED}，不能启动")
    if OpLock.SWITCHING in state.op_locks:
        return _no("切换进行中，不能启动")
    if OpLock.BULK_CLOSING in state.op_locks:
        return _no("批量平仓进行中，不能启动")
    if OpLock.SWITCH_PREVIEW in state.op_locks:
        return _no("切换预览中：请先确认或取消切换，不能启动")
    if state.suppressions:
        return _no(f"{_suppressed(state)}：不能启动")
    if state.gate is Gate.RUNNING:
        return _no("已在运行中，不能再启动")
    return _yes()


def _stop(state: ModelStateSnapshot) -> Permission:
    if OpLock.SWITCHING in state.op_locks:
        return _no("切换进行中，不能停止")
    if state.op_locks or state.suppressions:  # 批量平仓中、切换预览中、有抑制原因（含恢复中）：停止都可
        return _yes()
    if state.active_model is Model.M3:
        return _yes("模型 3 本期没有可停的东西")
    if state.gate is Gate.RUNNING:
        return _yes()
    return _no("已停止，无需再停止")


def _partial_close(state: ModelStateSnapshot) -> Permission:
    if Suppression.RECOVERING in state.suppressions:
        return _no("恢复中：不允许部分平仓，请用「平掉此仓」或「一键全平」")
    return _close_cell(state)


def _close_one(state: ModelStateSnapshot) -> Permission:
    return _close_cell(state)


def _close_cell(state: ModelStateSnapshot) -> Permission:
    """表的「部分平仓、平掉此仓」列。"""
    live = bool(state.suppressions)  # 有抑制原因：须先取到即时持仓读数
    if OpLock.SWITCHING not in state.op_locks and OpLock.BULK_CLOSING in state.op_locks:
        return _yes("只有未在批量平仓处理中的持仓可以操作", live)
    return _yes("", live)


def _close_all(state: ModelStateSnapshot) -> Permission:
    if OpLock.SWITCHING in state.op_locks:
        return _no("切换进行中，不能一键全平")
    if OpLock.BULK_CLOSING in state.op_locks:
        return _no("批量平仓进行中，不能再发起一键全平；可以中止当前批量平仓")
    note = "切换预览随之取消" if OpLock.SWITCH_PREVIEW in state.op_locks else ""
    return _yes(note, bool(state.suppressions))


def _abort(state: ModelStateSnapshot) -> Permission:
    if OpLock.SWITCHING in state.op_locks:
        return _no("切换进行中，没有可中止的批量平仓")
    if OpLock.BULK_CLOSING in state.op_locks:
        return _yes()
    return _no("没有进行中的批量平仓")


def _takeover_locked(state: ModelStateSnapshot, what: str) -> str:
    if OpLock.SWITCHING in state.op_locks:
        return f"切换进行中，不能{what}"
    if OpLock.BULK_CLOSING in state.op_locks:
        return f"批量平仓进行中，不能{what}"
    return ""


def _takeover(state: ModelStateSnapshot) -> Permission:
    """手动接管（F13）：只在模型 2 生效时可用，与开仓闸、抑制原因无关；切换中与批量平仓中不可。"""
    if state.active_model is not Model.M2:
        return _no("模型 2 不是生效模型：手动接管只在模型 2 生效时可用")
    if reason := _takeover_locked(state, "手动接管"):
        return _no(reason)
    return _yes()


def _takeover_family(state: ModelStateSnapshot, what: str) -> Permission:
    """忽略、恢复托管、取消托管、交给系统管理止损（F13）：任何生效模型下都可用；切换中与批量平仓中不可。"""
    if reason := _takeover_locked(state, what):
        return _no(reason)
    return _yes()


# ---------------------------------------------------------------- 服务


def _check_version(record: ModelStateRecord, expected: int | None) -> None:
    if expected is not None and record.version != expected:
        raise StaleVersion(expected, record.version)


def _suppressions_of(record: ModelStateRecord) -> frozenset[Suppression]:
    return frozenset(Suppression(x) for x in record.suppressions)


def _locks_of(record: ModelStateRecord) -> frozenset[OpLock]:
    return frozenset(OpLock(x) for x in record.op_locks)


def _encode(values: Iterable[Any]) -> list[str]:
    """集合 → 落盘的取值列表（排序，输出稳定）。"""
    return sorted(v.value for v in values)


def _check_ms(value: Any, name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{name} 必须是整数毫秒或 None：{value!r}")


def _plan_acquire(held: frozenset[OpLock], lock: OpLock) -> tuple[frozenset[OpLock], bool]:
    """按操作锁规则算出拿到 ``lock`` 之后持有的锁，以及切换预览是否被取消；规则不允许就抛 OpLockConflict。"""
    if lock in held:
        raise OpLockConflict(f"{OP_LOCK_LABELS[lock]}已经在进行，不能重复获取", held=held)
    if lock is OpLock.SWITCH_PREVIEW:
        if held:
            raise OpLockConflict(f"已有操作锁（{_labels(held, OP_LOCK_LABELS)}）：切换预览只能在没有别的操作锁时开始",
                                 held=held)
        return held | {lock}, False
    if lock is OpLock.SWITCHING:
        if OpLock.BULK_CLOSING in held:
            raise OpLockConflict("批量平仓进行中，不能开始切换", held=held)
        return held | {lock}, False  # 切换预览中本身不挡切换中
    if OpLock.SWITCHING in held:
        raise OpLockConflict("切换进行中，不能开始批量平仓", held=held)
    cancelled = OpLock.SWITCH_PREVIEW in held
    return (held - {OpLock.SWITCH_PREVIEW}) | {lock}, cancelled


class ModelStateService:
    def __init__(self, ledger: Ledger, bus: EventBus, clock: Clock) -> None:
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._lock = asyncio.Lock()  # 同一进程内的变更排队，快照与事件的顺序与提交顺序一致
        self._snapshot: ModelStateSnapshot | None = None

    # ------------------------------------------------------------ 加载与读取

    async def load(self) -> ModelStateSnapshot:
        """内核启动时调用一次。首次建默认行；之后每次：闸一律已停止、操作锁清除（原先是切换中就在
        ``model_switch_log`` 与 ``audit_log`` 记「切换中断」，生效模型以已提交的为准）、版本 +1。
        行损坏（JSON 或枚举值非法）：回退到已停止，生效模型读得出就保留、读不出用模型 1，``risk_detail`` 里记
        ``corrupt_recovered`` 与 ``corrupt_reason``。"""
        async with self._lock:
            record, info = await self._ledger.write(repo_modelstate.startup, now_ms=self._clock.now_ms())
            snapshot = snapshot_from_record(record)
            self._snapshot = snapshot
            self._bus.publish(ModelStateChanged(version=snapshot.version, at_ms=snapshot.updated_ms))
        if info["problems"]:
            log.error("模型状态行损坏，已回退到已停止（生效模型 %s）：%s", snapshot.active_model.value,
                      "；".join(info["problems"]))
        if info["interrupted_switch"]:
            log.warning("上次的模型切换被中断：生效模型仍为 %s，操作锁已清除，开仓闸已停止", snapshot.active_model.value)
        log.info("模型状态已加载：生效模型=%s 闸=%s 抑制=%s 版本=%s", snapshot.active_model.value, snapshot.gate.value,
                 sorted(s.value for s in snapshot.suppressions), snapshot.version)
        return snapshot

    def current(self) -> ModelStateSnapshot:
        if self._snapshot is None:
            raise RuntimeError("模型状态还没有加载：先调用 load()")
        return self._snapshot

    # ------------------------------------------------------------ 变更

    async def set_gate(self, gate: Gate, *, reason: str, expected_version: int | None = None) -> ModelStateSnapshot:
        gate = Gate(gate)

        def decide(record: ModelStateRecord) -> Change | None:
            _check_version(record, expected_version)
            if gate is Gate.RUNNING and record.active_model is Model.M3:
                raise ValueError(f"{_M3_RESERVED}，不能打开开仓闸")
            if record.gate is gate:
                return None
            return Change({"gate": gate}, action=_ACTION_PREFIX + "gate", reason=reason)

        return await self._apply(decide)

    async def add_suppression(self, s: Suppression, *, reason: str) -> ModelStateSnapshot:
        s = Suppression(s)

        def decide(record: ModelStateRecord) -> Change | None:
            current = _suppressions_of(record)
            if s in current:
                return None  # 已存在则不变、不增版本
            return Change({"suppressions": _encode(current | {s})}, action=_ACTION_PREFIX + "suppression_add",
                          reason=reason)

        return await self._apply(decide)

    async def remove_suppression(self, s: Suppression, *, reason: str) -> ModelStateSnapshot:
        s = Suppression(s)

        def decide(record: ModelStateRecord) -> Change | None:
            current = _suppressions_of(record)
            if s not in current:
                return None
            return Change({"suppressions": _encode(current - {s})}, action=_ACTION_PREFIX + "suppression_remove",
                          reason=reason)

        return await self._apply(decide)

    async def acquire_op_lock(self, lock: OpLock, *, owner: str, expires_ms: int | None = None,
                              expected_version: int | None = None) -> ModelStateSnapshot:
        lock = OpLock(lock)
        if not isinstance(owner, str) or not owner:
            raise ValueError("owner 必须是非空文本")
        _check_ms(expires_ms, "expires_ms")

        def decide(record: ModelStateRecord) -> Change | None:
            _check_version(record, expected_version)
            held, cancelled_preview = _plan_acquire(_locks_of(record), lock)
            reason = f"获取操作锁：{OP_LOCK_LABELS[lock]}（持有者 {owner}）"
            if cancelled_preview:
                reason += "；切换预览随之取消"
            return Change({"op_locks": _encode(held), "op_lock_owner": owner, "op_lock_expires_ms": expires_ms},
                          action=_ACTION_PREFIX + "op_lock_acquire", reason=reason)

        return await self._apply(decide)

    async def release_op_lock(self, lock: OpLock, *, owner: str | None = None) -> ModelStateSnapshot:
        """没持有这把锁就是空操作（清理路径可以放心多调）。给了 ``owner`` 而锁由别人持有时拒绝；不给就是强制释放。"""
        lock = OpLock(lock)

        def decide(record: ModelStateRecord) -> Change | None:
            held = _locks_of(record)
            if lock not in held:
                return None
            if owner is not None and record.op_lock_owner is not None and record.op_lock_owner != owner:
                raise OpLockConflict(f"操作锁由 {record.op_lock_owner} 持有，{owner} 不能释放", held=held)
            remaining = held - {lock}
            updates: dict[str, Any] = {"op_locks": _encode(remaining)}
            if not remaining:
                updates.update(op_lock_owner=None, op_lock_expires_ms=None)
            return Change(updates, action=_ACTION_PREFIX + "op_lock_release", reason=f"释放操作锁：{OP_LOCK_LABELS[lock]}")

        return await self._apply(decide)

    async def set_run_mode(self, mode: RunMode, *, reason: str) -> ModelStateSnapshot:
        mode = RunMode(mode)
        return await self._apply(lambda record: Change({"run_mode": mode}, action=_ACTION_PREFIX + "run_mode",
                                                       reason=reason))

    async def set_active_model(self, model: Model, *, expected_version: int, operator: str, op_id: str,
                               mode: str | None = None) -> ModelStateSnapshot:
        """F6 第 6 步：同一个事务里写新生效模型、闸已停止、解除切换中与切换预览中、写 ``model_switch_log``。
        ``mode`` 是用户选的方式（保留原规则 / 先全部平仓再切换），只用于留痕。批量平仓中不能提交切换。"""
        model = Model(model)

        def decide(record: ModelStateRecord) -> Change | None:
            _check_version(record, expected_version)
            held = _locks_of(record)
            if OpLock.BULK_CLOSING in held:
                raise OpLockConflict("批量平仓进行中，不能提交模型切换", held=held)
            detail = (f"生效模型由 {record.active_model.value} 切换为 {model.value}；开仓闸已停止，"
                      f"切换中与切换预览中已解除")
            updates = {"active_model": model, "gate": Gate.STOPPED, "op_locks": [], "op_lock_owner": None,
                       "op_lock_expires_ms": None}
            entry = SwitchLogEntry(from_model=record.active_model.value, to_model=model.value, mode=mode,
                                   result="成功", op_id=op_id, operator=operator, detail=detail)
            return Change(updates, action=_ACTION_PREFIX + "switch", reason=detail, operator=operator,
                          request_id=op_id, switch_log=entry)

        return await self._apply(decide)

    async def open_gate_recording_m2_start(self, ms: int, *, reason: str,
                                           expected_version: int | None = None) -> ModelStateSnapshot:
        """打开开仓闸；若生效模型是模型 2 且闸原先是停止的，同一事务里写 ``m2_started_ms``（一个版本、一条审计）。

        闸已在运行则什么都不改（保留第一次的启动时刻）；生效模型不是模型 2 时只开闸，不动 ``m2_started_ms``。"""
        _check_ms(ms, "m2_started_ms")
        if ms is None:
            raise TypeError("m2_started_ms 不能为空")

        def decide(record: ModelStateRecord) -> Change | None:
            _check_version(record, expected_version)
            if record.active_model is Model.M3:
                raise ValueError(f"{_M3_RESERVED}，不能打开开仓闸")
            if record.gate is Gate.RUNNING:
                return None
            updates: dict[str, Any] = {"gate": Gate.RUNNING}
            if record.active_model is Model.M2:
                updates["m2_started_ms"] = ms
            return Change(updates, action=_ACTION_PREFIX + "open_gate_m2", reason=reason)

        return await self._apply(decide)

    async def set_m2_started(self, ms: int | None) -> ModelStateSnapshot:
        _check_ms(ms, "m2_started_ms")
        return await self._apply(lambda record: Change({"m2_started_ms": ms}, action=_ACTION_PREFIX + "m2_started"))

    async def set_mismatch_since(self, ms: int | None) -> ModelStateSnapshot:
        _check_ms(ms, "mismatch_since_ms")
        return await self._apply(
            lambda record: Change({"mismatch_since_ms": ms}, action=_ACTION_PREFIX + "mismatch_since"))

    async def set_risk_detail(self, d: dict[str, Any]) -> ModelStateSnapshot:
        """整体替换 ``risk_detail``（要保留旧内容就先合并）。"""
        if not isinstance(d, Mapping):
            raise TypeError(f"risk_detail 必须是字典：{type(d).__name__}")
        detail = dict(d)
        return await self._apply(lambda record: Change({"risk_detail": detail}, action=_ACTION_PREFIX + "risk_detail"))

    # ------------------------------------------------------------ 内部

    async def _apply(self, decide: Callable[[ModelStateRecord], Change | None]) -> ModelStateSnapshot:
        """排队 → 在写线程的事务里读-判-写 → 提交后更新内存快照 → 有变化才发布事件。"""
        if self._snapshot is None:
            raise RuntimeError("模型状态还没有加载：先调用 load()")
        async with self._lock:
            record, changed = await self._ledger.write(repo_modelstate.apply, decide, now_ms=self._clock.now_ms())
            snapshot = snapshot_from_record(record)
            self._snapshot = snapshot  # 与库一致（包括别的实例推高了版本之后追上来）
            if changed:
                self._bus.publish(ModelStateChanged(version=snapshot.version, at_ms=snapshot.updated_ms))
        return snapshot
