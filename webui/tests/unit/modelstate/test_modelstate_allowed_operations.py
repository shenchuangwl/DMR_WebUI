"""allowed_operations：逐格对照报告 4.6「状态与可执行操作」表（6 行 × 5 列），加恢复中、模型 3 与优先级。

表的行：已停止无抑制、运行中无抑制、切换预览中、有抑制原因、切换中、批量平仓中。
列：切换模型、启动、停止、部分平仓与平掉此仓、一键全平。优先级：切换中 > 批量平仓中 > 切换预览中 > 有抑制原因 > 运行中/已停止。
"""
from __future__ import annotations

import dataclasses

import pytest

from dmr_kernel.core.modelstate import ModelStateSnapshot, Permission, allowed_operations
from dmr_kernel.core.types import Gate, Model, OpLock, Suppression

TABLE_KEYS = ("switch", "switch_confirm", "switch_cancel", "start", "stop", "partial_close", "close_one", "close_all",
              "abort")
# F13 的接管类操作（M4 收尾加入）：接管只在模型 2 生效时可用，其余四个任何模型可用；切换中、批量平仓中都不可
TAKEOVER_KEYS = ("takeover", "ignore", "restore", "cancel_takeover", "hand_stop_to_system")
KEYS = TABLE_KEYS + TAKEOVER_KEYS

# 抑制原因在报告 4.6 里的叫法
SUPPRESSION_NAMES = {
    Suppression.STREAM_SWITCHING: "数据流切换中",
    Suppression.RECONCILE_UNKNOWN: "对账未知",
    Suppression.RECONCILE_MISMATCH: "对账不一致",
    Suppression.EXCHANGE_RESTRICTED: "交易所限制",
    Suppression.RISK_TRIPPED: "风控触发",
    Suppression.UNPROTECTED: "无保护持仓",
    Suppression.RECOVERING: "恢复中",
    Suppression.MUTEX_ANOMALY: "互斥异常",
    Suppression.CLOCK_SKEW: "时钟偏差",
}


def state(*, model=Model.M1, gate=Gate.STOPPED, supp=(), locks=()) -> ModelStateSnapshot:
    return ModelStateSnapshot(active_model=model, gate=gate, suppressions=frozenset(supp), op_locks=frozenset(locks))


def summary(ops: dict[str, Permission]) -> dict[str, tuple[bool, bool]]:
    return {key: (perm.allowed, perm.needs_live_read) for key, perm in ops.items()}


def expect(*, model: Model = Model.M1, op_blocked: bool = False,
           **cells: tuple[bool, bool]) -> dict[str, tuple[bool, bool]]:
    """表里未列出的格子默认不可（False, False）。

    接管类五格按 F13 推出：``op_blocked``（切换中或批量平仓中）时全不可；否则忽略、恢复托管、取消托管、
    交给系统管理止损都可，手动接管只在 ``model`` 是模型 2 时可。都不需要即时读数。"""
    out = {key: (False, False) for key in KEYS}
    if not op_blocked:
        for key in TAKEOVER_KEYS[1:]:
            out[key] = (True, False)
        if model is Model.M2:
            out["takeover"] = (True, False)
    out.update(cells)
    return out


YES = (True, False)
YES_LIVE = (True, True)


# ---------------------------------------------------------------- 返回结构


def test_returns_every_key_with_permission_objects():
    ops = allowed_operations(state())
    assert tuple(ops) == KEYS
    assert all(isinstance(p, Permission) for p in ops.values())


def test_permission_is_frozen_with_defaults():
    p = Permission(allowed=True)
    assert (p.allowed, p.reason, p.needs_live_read, p.note) == (True, "", False, "")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.allowed = False  # type: ignore[misc]


def test_every_denial_states_a_reason_and_every_grant_has_none():
    scenarios = [state(), state(gate=Gate.RUNNING), state(locks=[OpLock.SWITCH_PREVIEW]),
                 state(supp=[Suppression.CLOCK_SKEW]), state(locks=[OpLock.SWITCHING]),
                 state(locks=[OpLock.BULK_CLOSING]), state(model=Model.M3)]
    for snap in scenarios:
        for key, perm in allowed_operations(snap).items():
            if perm.allowed:
                assert perm.reason == "", (key, snap)
            else:
                assert perm.reason.strip() != "", (key, snap)


# ---------------------------------------------------------------- 表的第 1 行：已停止，无抑制


@pytest.mark.parametrize("model", [Model.M1, Model.M2])
def test_row1_stopped_without_suppression(model):
    ops = allowed_operations(state(model=model, gate=Gate.STOPPED))
    assert summary(ops) == expect(model=model, switch=YES, start=YES, partial_close=YES, close_one=YES, close_all=YES)
    assert "已停止" in ops["stop"].reason  # 「—」：已停止再停止不允许并写明原因
    assert "预览" in ops["switch_confirm"].reason and "预览" in ops["switch_cancel"].reason
    assert "批量平仓" in ops["abort"].reason
    assert "自动先停" not in ops["switch"].note  # 只有运行中切换才先停


# ---------------------------------------------------------------- 表的第 2 行：运行中，无抑制


@pytest.mark.parametrize("model", [Model.M1, Model.M2])
def test_row2_running_without_suppression(model):
    ops = allowed_operations(state(model=model, gate=Gate.RUNNING))
    assert summary(ops) == expect(model=model, switch=YES, stop=YES, partial_close=YES, close_one=YES, close_all=YES)
    assert "运行中" in ops["start"].reason  # 「—」：运行中再启动不允许并写明原因
    assert "自动先停" in ops["switch"].note


def test_degraded_run_mode_is_not_a_suppression_for_permissions():
    from dmr_kernel.core.types import RunMode

    snap = dataclasses.replace(state(gate=Gate.RUNNING), run_mode=RunMode.POLLING_DEGRADED)
    assert summary(allowed_operations(snap)) == summary(allowed_operations(state(gate=Gate.RUNNING)))


# ---------------------------------------------------------------- 表的第 3 行：切换预览中


@pytest.mark.parametrize("gate", [Gate.STOPPED, Gate.RUNNING])
def test_row3_switch_preview(gate):
    ops = allowed_operations(state(gate=gate, locks=[OpLock.SWITCH_PREVIEW]))
    assert summary(ops) == expect(switch_confirm=YES, switch_cancel=YES, stop=YES, partial_close=YES,
                                  close_one=YES, close_all=YES)
    assert "席位持有者" in ops["switch_confirm"].note
    assert "席位持有者" in ops["switch_cancel"].note
    assert "取消" in ops["close_all"].note  # 一键全平：预览随之取消
    assert "预览" in ops["switch"].reason  # 已有预览，不能再开
    assert "预览" in ops["start"].reason


# ---------------------------------------------------------------- 表的第 4 行：有抑制原因


@pytest.mark.parametrize("gate", [Gate.STOPPED, Gate.RUNNING])
@pytest.mark.parametrize("suppression", [s for s in Suppression if s is not Suppression.RECOVERING],
                         ids=lambda s: s.value)
def test_row4_suppression(suppression, gate):
    ops = allowed_operations(state(gate=gate, supp=[suppression]))
    assert summary(ops) == expect(stop=YES, partial_close=YES_LIVE, close_one=YES_LIVE, close_all=YES_LIVE)
    name = SUPPRESSION_NAMES[suppression]
    assert name in ops["switch"].reason
    assert name in ops["start"].reason


def test_row4_all_suppressions_together_name_each_of_them():
    ops = allowed_operations(state(gate=Gate.RUNNING, supp=list(Suppression)))
    for name in SUPPRESSION_NAMES.values():
        assert name in ops["start"].reason
    assert ops["stop"].allowed and ops["close_all"].needs_live_read


# ---------------------------------------------------------------- 恢复中（F9 末段）：只允许停止、平掉此仓、一键全平


@pytest.mark.parametrize("gate", [Gate.STOPPED, Gate.RUNNING])
def test_recovering_allows_only_stop_close_one_and_close_all(gate):
    ops = allowed_operations(state(gate=gate, supp=[Suppression.RECOVERING]))
    assert summary(ops) == expect(stop=YES, close_one=YES_LIVE, close_all=YES_LIVE)
    for key in ("partial_close", "start", "switch"):
        assert "恢复中" in ops[key].reason, key


def test_recovering_with_bulk_closing_still_forbids_partial_close():
    ops = allowed_operations(state(supp=[Suppression.RECOVERING], locks=[OpLock.BULK_CLOSING]))
    assert not ops["partial_close"].allowed
    assert ops["close_one"].allowed and ops["abort"].allowed


# ---------------------------------------------------------------- 表的第 5 行：切换中


@pytest.mark.parametrize("gate", [Gate.STOPPED, Gate.RUNNING])
def test_row5_switching(gate):
    ops = allowed_operations(state(gate=gate, locks=[OpLock.SWITCHING]))
    assert summary(ops) == expect(op_blocked=True, partial_close=YES, close_one=YES)
    for key in ("switch", "switch_confirm", "switch_cancel", "start", "stop", "close_all", "abort"):
        assert "切换" in ops[key].reason, key


# ---------------------------------------------------------------- 表的第 6 行：批量平仓中


@pytest.mark.parametrize("gate", [Gate.STOPPED, Gate.RUNNING])
def test_row6_bulk_closing(gate):
    ops = allowed_operations(state(gate=gate, locks=[OpLock.BULK_CLOSING]))
    assert summary(ops) == expect(op_blocked=True, stop=YES, partial_close=YES, close_one=YES, abort=YES)
    assert "未在" in ops["partial_close"].note  # 未在处理的持仓可
    assert "批量平仓" in ops["close_all"].reason and "中止" in ops["close_all"].reason
    assert "批量平仓" in ops["switch"].reason and "批量平仓" in ops["start"].reason


# ---------------------------------------------------------------- 模型 3：启动一律拒绝，其余照常


M3_STATES = {
    "stopped": dict(gate=Gate.STOPPED),
    "running(异常数据)": dict(gate=Gate.RUNNING),
    "preview": dict(locks=[OpLock.SWITCH_PREVIEW]),
    "switching": dict(locks=[OpLock.SWITCHING]),
    "bulk": dict(locks=[OpLock.BULK_CLOSING]),
    "suppressed": dict(supp=[Suppression.RISK_TRIPPED]),
    "recovering": dict(supp=[Suppression.RECOVERING]),
}


@pytest.mark.parametrize("kw", M3_STATES.values(), ids=M3_STATES.keys())
def test_model3_start_is_always_refused_with_reason(kw):
    perm = allowed_operations(state(model=Model.M3, **kw))["start"]
    assert not perm.allowed
    assert "模型 3 本期只预留" in perm.reason


def test_model3_stop_partial_close_and_close_all_work_as_usual():
    ops = allowed_operations(state(model=Model.M3))
    assert summary(ops) == expect(model=Model.M3, switch=YES, stop=YES, partial_close=YES, close_one=YES,
                                  close_all=YES, start=(False, False))
    assert ops["stop"].note  # 提示：本期没有可停的东西
    assert "没有可停" in ops["stop"].note


def test_model3_under_suppression_keeps_live_read_requirement():
    ops = allowed_operations(state(model=Model.M3, supp=[Suppression.UNPROTECTED]))
    assert ops["close_all"].needs_live_read and ops["partial_close"].needs_live_read


# ---------------------------------------------------------------- 优先级与组合


def test_switching_beats_bulk_closing():
    ops = allowed_operations(state(gate=Gate.RUNNING, locks=[OpLock.SWITCHING, OpLock.BULK_CLOSING]))
    assert not ops["stop"].allowed and not ops["close_all"].allowed and not ops["abort"].allowed
    assert ops["partial_close"].allowed and ops["close_one"].allowed


def test_switching_beats_suppression():
    ops = allowed_operations(state(gate=Gate.RUNNING, locks=[OpLock.SWITCHING], supp=[Suppression.CLOCK_SKEW]))
    assert not ops["stop"].allowed and not ops["close_all"].allowed
    assert ops["partial_close"].allowed and ops["partial_close"].needs_live_read


def test_bulk_closing_beats_preview_and_suppression():
    ops = allowed_operations(state(locks=[OpLock.BULK_CLOSING, OpLock.SWITCH_PREVIEW],
                                   supp=[Suppression.EXCHANGE_RESTRICTED]))
    assert not ops["switch_confirm"].allowed and not ops["switch_cancel"].allowed
    assert not ops["close_all"].allowed and ops["abort"].allowed and ops["stop"].allowed
    assert ops["partial_close"].allowed and "未在" in ops["partial_close"].note


def test_preview_beats_suppression_for_cancel_but_confirm_rechecks_suppression():
    # F6 第 3 步：有抑制原因时拒绝切换，所以预览期间出现抑制原因后只能取消，不能确认
    ops = allowed_operations(state(gate=Gate.RUNNING, locks=[OpLock.SWITCH_PREVIEW],
                                   supp=[Suppression.STREAM_SWITCHING]))
    assert ops["switch_cancel"].allowed
    assert not ops["switch_confirm"].allowed and "数据流切换中" in ops["switch_confirm"].reason
    assert ops["stop"].allowed and not ops["start"].allowed
    assert ops["close_all"].allowed and ops["close_all"].needs_live_read  # 抑制原因下的一键全平仍要即时读数
    assert ops["partial_close"].needs_live_read


def test_suppression_beats_running_state():
    ops = allowed_operations(state(gate=Gate.RUNNING, supp=[Suppression.UNPROTECTED]))
    assert not ops["switch"].allowed
    assert ops["stop"].allowed


def test_stopped_with_suppression_still_allows_stop():
    # 表的「有抑制原因」行：停止「可」；恢复中（重启后闸一定是已停止）也明确允许停止
    assert allowed_operations(state(gate=Gate.STOPPED, supp=[Suppression.RECONCILE_MISMATCH]))["stop"].allowed


def test_snapshot_does_not_change_when_asking_permissions():
    snap = state(gate=Gate.RUNNING, supp=[Suppression.CLOCK_SKEW], locks=[OpLock.SWITCH_PREVIEW])
    before = dataclasses.replace(snap)
    allowed_operations(snap)
    assert snap == before


# ---------------------------------------------------------------- 接管类五格（F13）在表的每一行


TAKEOVER_ROWS = {
    "已停止": dict(gate=Gate.STOPPED),
    "运行中": dict(gate=Gate.RUNNING),
    "切换预览中": dict(locks=[OpLock.SWITCH_PREVIEW]),
    "有抑制原因": dict(supp=[Suppression.RISK_TRIPPED]),
    "恢复中": dict(supp=[Suppression.RECOVERING]),
}


@pytest.mark.parametrize("model", list(Model), ids=lambda m: m.value)
@pytest.mark.parametrize("kw", TAKEOVER_ROWS.values(), ids=TAKEOVER_ROWS.keys())
def test_takeover_family_outside_switching_and_bulk_closing(kw, model):
    ops = allowed_operations(state(model=model, **kw))
    for key in TAKEOVER_KEYS[1:]:  # 忽略、恢复托管、取消托管、交给系统管理止损：任何模型、与闸和抑制原因无关
        assert (ops[key].allowed, ops[key].needs_live_read) == YES, key
    if model is Model.M2:
        assert ops["takeover"].allowed
    else:  # 手动接管只在模型 2 生效时可用，原因写明
        assert not ops["takeover"].allowed and "模型 2" in ops["takeover"].reason


@pytest.mark.parametrize("model", list(Model), ids=lambda m: m.value)
@pytest.mark.parametrize("lock,word", [(OpLock.SWITCHING, "切换"), (OpLock.BULK_CLOSING, "批量平仓")])
def test_takeover_family_refused_while_switching_or_bulk_closing(lock, word, model):
    ops = allowed_operations(state(model=model, locks=[lock], supp=[Suppression.RECOVERING]))
    for key in TAKEOVER_KEYS[1:]:
        assert not ops[key].allowed and word in ops[key].reason, key
    assert not ops["takeover"].allowed
    if model is Model.M2:  # 模型 2 下拒绝的原因是锁，不是模型
        assert word in ops["takeover"].reason
