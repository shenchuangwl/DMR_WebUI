"""allowed_operations 的接管类五个键，以及「打开闸并记录模型 2 启动时刻」的组合写法。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.modelstate import ModelStateService, OPERATION_KEYS, StaleVersion, allowed_operations
from dmr_kernel.core.types import Gate, Model, OpLock, Suppression
from dmr_kernel.ledger import repo_audit
from test_modelstate_allowed_operations import state

TAKEOVER_KEYS = ("takeover", "ignore", "restore", "cancel_takeover", "hand_stop_to_system")
OTHERS = TAKEOVER_KEYS[1:]


def test_keys_are_appended_after_the_existing_nine():
    assert OPERATION_KEYS[-5:] == TAKEOVER_KEYS
    assert list(allowed_operations(state())) == list(OPERATION_KEYS)


@pytest.mark.parametrize("model", [Model.M1, Model.M3])
def test_takeover_needs_model_2_but_the_rest_work_under_any_model(model):
    ops = allowed_operations(state(model=model))
    assert not ops["takeover"].allowed and "模型 2" in ops["takeover"].reason
    assert all(ops[k].allowed for k in OTHERS)


@pytest.mark.parametrize("gate", [Gate.STOPPED, Gate.RUNNING])
def test_model_2_allows_everything_regardless_of_gate(gate):
    ops = allowed_operations(state(model=Model.M2, gate=gate))
    assert all(ops[k].allowed for k in TAKEOVER_KEYS)


def test_suppressions_and_a_switch_preview_do_not_block():
    ops = allowed_operations(state(model=Model.M2, supp=[Suppression.RECOVERING], locks=[OpLock.SWITCH_PREVIEW]))
    assert all(ops[k].allowed for k in TAKEOVER_KEYS)


@pytest.mark.parametrize("lock", [OpLock.SWITCHING, OpLock.BULK_CLOSING])
def test_switching_and_bulk_closing_block_all_five_with_a_reason(lock):
    ops = allowed_operations(state(model=Model.M2, locks=[lock]))
    for k in TAKEOVER_KEYS:
        assert not ops[k].allowed and ops[k].reason


STARTED = 1_760_000_123_000


async def _svc(ledger, clock, bus, model=Model.M2):
    svc = ModelStateService(ledger, bus, clock)
    await svc.load()
    if model is not svc.current().active_model:
        await svc.set_active_model(model, expected_version=svc.current().version, operator="t", op_id="sw1")
    return svc


async def test_open_gate_and_record_m2_start_is_one_version_and_one_audit(ledger, clock, bus):
    svc = await _svc(ledger, clock, bus)
    before = svc.current()
    snap = await svc.open_gate_recording_m2_start(STARTED, reason="启动")
    assert snap.gate is Gate.RUNNING and snap.m2_started_ms == STARTED and snap.version == before.version + 1
    rows = await ledger.read(repo_audit.recent, limit=1000)
    assert [r.action for r in rows].count("model_state.open_gate_m2") == 1
    assert svc.current() == snap


async def test_open_gate_when_already_running_keeps_the_first_start_time(ledger, clock, bus):
    svc = await _svc(ledger, clock, bus)
    first = await svc.open_gate_recording_m2_start(STARTED, reason="启动")
    again = await svc.open_gate_recording_m2_start(STARTED + 5_000, reason="启动")
    assert again.m2_started_ms == STARTED and again.version == first.version


async def test_open_gate_under_another_model_does_not_touch_the_m2_start_time(ledger, clock, bus):
    svc = await _svc(ledger, clock, bus, Model.M1)
    snap = await svc.open_gate_recording_m2_start(STARTED, reason="启动")
    assert snap.gate is Gate.RUNNING and snap.m2_started_ms is None


async def test_open_gate_checks_the_version_and_the_clock_value(ledger, clock, bus):
    svc = await _svc(ledger, clock, bus)
    with pytest.raises(StaleVersion):
        await svc.open_gate_recording_m2_start(STARTED, reason="启动", expected_version=svc.current().version + 9)
    with pytest.raises(TypeError):
        await svc.open_gate_recording_m2_start("1", reason="启动")
    assert svc.current().gate is Gate.STOPPED


async def test_open_gate_is_refused_under_model_3(ledger, clock, bus):
    svc = await _svc(ledger, clock, bus, Model.M3)
    with pytest.raises(ValueError):
        await svc.open_gate_recording_m2_start(STARTED, reason="启动")
