"""ModelStateSnapshot：can_open、can_auto_takeover，以及快照本身的性质。"""
from __future__ import annotations

import dataclasses

import pytest

from dmr_kernel.core.modelstate import ModelStateSnapshot, OpLockConflict, StaleVersion
from dmr_kernel.core.types import Gate, Model, OpLock, RunMode, Suppression


def snap(**kw) -> ModelStateSnapshot:
    kw.setdefault("gate", Gate.RUNNING)
    for key in ("suppressions", "op_locks"):
        if key in kw:
            kw[key] = frozenset(kw[key])
    return ModelStateSnapshot(**kw)


# ---------------------------------------------------------------- 快照本身


def test_snapshot_field_order_and_defaults_match_the_interface():
    names = [f.name for f in dataclasses.fields(ModelStateSnapshot)]
    assert names == ["active_model", "gate", "suppressions", "op_locks", "op_lock_owner", "op_lock_expires_ms",
                     "run_mode", "version", "m2_started_ms", "mismatch_since_ms", "risk_detail", "updated_ms"]
    s = ModelStateSnapshot()
    assert (s.active_model, s.gate, s.run_mode, s.version) == (Model.M1, Gate.STOPPED, RunMode.NORMAL, 1)
    assert s.suppressions == frozenset() and s.op_locks == frozenset() and s.risk_detail == {}


def test_snapshot_is_frozen_and_hashable_despite_risk_detail():
    s = snap(risk_detail={"a": 1})
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.gate = Gate.STOPPED  # type: ignore[misc]
    assert isinstance(hash(s), int)
    assert snap(risk_detail={"a": 1}) == s
    assert snap(risk_detail={"a": 2}) != s


def test_default_risk_detail_is_not_shared_between_snapshots():
    a, b = ModelStateSnapshot(), ModelStateSnapshot()
    a.risk_detail["x"] = 1
    assert b.risk_detail == {}


def test_exceptions_are_plain_exceptions_with_context():
    err = StaleVersion(3, 5)
    assert isinstance(err, Exception) and (err.expected, err.actual) == (3, 5)
    assert "3" in str(err) and "5" in str(err)
    held = frozenset({OpLock.SWITCHING})
    conflict = OpLockConflict("切换中", held=held)
    assert isinstance(conflict, Exception) and conflict.held == held and "切换中" in str(conflict)


# ---------------------------------------------------------------- can_open：生效模型、闸开、无抑制、无操作锁


def test_can_open_when_active_running_clean():
    assert snap(active_model=Model.M1).can_open(Model.M1) == (True, "")
    assert snap(active_model=Model.M2).can_open(Model.M2) == (True, "")


def test_can_open_rejects_a_model_that_is_not_active():
    ok, reason = snap(active_model=Model.M2).can_open(Model.M1)
    assert not ok and "生效" in reason


def test_can_open_rejects_when_gate_stopped():
    ok, reason = snap(gate=Gate.STOPPED).can_open(Model.M1)
    assert not ok and "已停止" in reason


@pytest.mark.parametrize("suppression", list(Suppression), ids=lambda s: s.value)
def test_can_open_rejects_each_suppression(suppression):
    ok, reason = snap(suppressions=[suppression]).can_open(Model.M1)
    assert not ok and "抑制" in reason


@pytest.mark.parametrize("lock", list(OpLock), ids=lambda s: s.value)
def test_can_open_rejects_each_op_lock(lock):
    ok, reason = snap(op_locks=[lock]).can_open(Model.M1)
    assert not ok and "操作锁" in reason


def test_can_open_allows_polling_degraded_run_mode():
    # 降级不是抑制原因：期间可以开仓（上限收紧由 limits 处理）
    assert snap(run_mode=RunMode.POLLING_DEGRADED).can_open(Model.M1)[0]


def test_model3_can_never_open_even_if_the_row_says_running():
    ok, reason = snap(active_model=Model.M3, gate=Gate.RUNNING).can_open(Model.M3)
    assert not ok and "模型 3 本期只预留" in reason


def test_can_open_reports_every_failure_kind_with_distinct_reasons():
    reasons = {
        snap(active_model=Model.M2).can_open(Model.M1)[1],
        snap(gate=Gate.STOPPED).can_open(Model.M1)[1],
        snap(suppressions=[Suppression.CLOCK_SKEW]).can_open(Model.M1)[1],
        snap(op_locks=[OpLock.SWITCHING]).can_open(Model.M1)[1],
    }
    assert len(reasons) == 4


# ---------------------------------------------------------------- can_auto_takeover：接管是加保护，抑制原因不挡


def test_can_auto_takeover_when_model2_active_and_running():
    assert snap(active_model=Model.M2).can_auto_takeover() == (True, "")


@pytest.mark.parametrize("suppression", list(Suppression), ids=lambda s: s.value)
def test_suppressions_do_not_block_auto_takeover(suppression):
    assert snap(active_model=Model.M2, suppressions=[suppression]).can_auto_takeover()[0]


@pytest.mark.parametrize("model", [Model.M1, Model.M3])
def test_auto_takeover_needs_model2_active(model):
    ok, reason = snap(active_model=model).can_auto_takeover()
    assert not ok and "模型 2" in reason


def test_auto_takeover_needs_the_gate_open():
    ok, reason = snap(active_model=Model.M2, gate=Gate.STOPPED).can_auto_takeover()
    assert not ok and "未在运行" in reason


@pytest.mark.parametrize("lock", list(OpLock), ids=lambda s: s.value)
def test_any_op_lock_blocks_auto_takeover(lock):
    ok, reason = snap(active_model=Model.M2, op_locks=[lock]).can_auto_takeover()
    assert not ok and "操作锁" in reason
