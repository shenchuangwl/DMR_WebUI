"""ModelStateService.load：首次建默认行、每次内核启动的重置规则（F6 第 8 步、F9）、行损坏时的回退。"""
from __future__ import annotations

import json

import pytest

from dmr_kernel.core.events import EventBus
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.types import Gate, Model, OpLock, RunMode, Suppression
from dmr_kernel.ledger import repo_audit, repo_modelstate


def new_service(ledger, clock, bus=None) -> ModelStateService:
    return ModelStateService(ledger, bus or EventBus(), clock)


async def audit_rows(ledger, **filters):
    return list(reversed(await ledger.read(repo_audit.recent, limit=1000, **filters)))


async def audit_actions(ledger) -> list[str]:
    return [r.action for r in await audit_rows(ledger)]


async def switch_log(ledger):
    return list(reversed(await ledger.read(repo_modelstate.recent_switch_log, limit=100)))


# ---------------------------------------------------------------- 首次建默认行


async def test_current_before_load_is_a_program_error(ledger, clock):
    with pytest.raises(RuntimeError, match="load"):
        new_service(ledger, clock).current()


async def test_first_load_creates_the_default_row(ledger, clock, raw):
    svc = new_service(ledger, clock)
    snap = await svc.load()
    assert snap == svc.current()
    assert snap.active_model is Model.M1 and snap.gate is Gate.STOPPED
    assert snap.suppressions == frozenset() and snap.op_locks == frozenset()
    assert snap.op_lock_owner is None and snap.op_lock_expires_ms is None
    assert snap.run_mode is RunMode.NORMAL and snap.version == 1
    assert snap.m2_started_ms is None and snap.mismatch_since_ms is None
    assert snap.risk_detail == {} and snap.updated_ms == clock.now_ms()
    assert raw.query("SELECT id, active_model, gate, suppressions, op_locks, run_mode, version FROM model_state") == [
        (1, "m1", "stopped", "[]", "[]", "normal", 1)]
    assert await audit_actions(ledger) == ["model_state.init"]
    assert await switch_log(ledger) == []


async def test_first_load_publishes_the_change(ledger, clock, bus, events):
    await new_service(ledger, clock, bus).load()
    got = await events.flush()
    assert [(e.version, e.at_ms) for e in got] == [(1, clock.now_ms())]


async def test_deleted_row_is_recreated_as_a_fresh_default(ledger, clock, raw):
    svc = new_service(ledger, clock)
    await svc.load()
    await svc.set_gate(Gate.RUNNING, reason="x")
    raw.exec("DELETE FROM model_state")
    snap = await new_service(ledger, clock).load()
    assert (snap.active_model, snap.gate, snap.version) == (Model.M1, Gate.STOPPED, 1)


# ---------------------------------------------------------------- 每次启动：闸已停止、锁清除、版本 +1


async def dirty_state(ledger, clock):
    a = new_service(ledger, clock)
    await a.load()
    await a.set_active_model(Model.M2, expected_version=a.current().version, operator="alice", op_id="op-1")
    await a.set_gate(Gate.RUNNING, reason="启动")
    await a.add_suppression(Suppression.RECONCILE_MISMATCH, reason="差异")
    await a.set_run_mode(RunMode.POLLING_DEGRADED, reason="数据流断")
    await a.acquire_op_lock(OpLock.SWITCH_PREVIEW, owner="seat-1", expires_ms=clock.now_ms() + 60_000)
    await a.set_m2_started(1_700_000_000_123)
    await a.set_mismatch_since(1_700_000_000_456)
    await a.set_risk_detail({"reasons": ["回撤"]})
    return a


async def test_restart_forces_gate_stopped_clears_locks_and_bumps_the_version(ledger, clock):
    a = await dirty_state(ledger, clock)
    before = a.current()
    assert before.gate is Gate.RUNNING and before.op_locks == {OpLock.SWITCH_PREVIEW}
    clock.advance(5_000)

    b = new_service(ledger, clock)
    after = await b.load()

    assert b.current() == after
    assert after.gate is Gate.STOPPED
    assert after.op_locks == frozenset() and after.op_lock_owner is None and after.op_lock_expires_ms is None
    assert after.version == before.version + 1
    assert after.updated_ms == clock.now_ms()
    # 其余状态原样保留
    assert after.active_model is Model.M2
    assert after.suppressions == {Suppression.RECONCILE_MISMATCH}
    assert after.run_mode is RunMode.POLLING_DEGRADED
    assert after.m2_started_ms == 1_700_000_000_123 and after.mismatch_since_ms == 1_700_000_000_456
    assert after.risk_detail == {"reasons": ["回撤"]}
    assert not after.can_open(Model.M2)[0]


async def test_restart_leaves_an_audit_trail_of_what_was_reset(ledger, clock):
    a = await dirty_state(ledger, clock)
    await new_service(ledger, clock).load()
    startup = (await audit_rows(ledger, action="model_state.startup"))[-1]
    assert startup.operator == "system" and startup.result == "ok"
    assert startup.before["gate"] == "running" and startup.before["op_locks"] == ["switch_preview"]
    assert startup.after["gate"] == "stopped" and startup.after["op_locks"] == []
    assert startup.after["version"] == a.current().version + 1


async def test_restart_publishes_the_new_version(ledger, clock, bus, events):
    a = await dirty_state(ledger, clock)
    await new_service(ledger, clock, bus).load()
    got = await events.flush()
    assert [e.version for e in got] == [a.current().version + 1]


async def test_every_restart_bumps_the_version_again(ledger, clock):
    versions = []
    for _ in range(3):
        versions.append((await new_service(ledger, clock).load()).version)
    assert versions == [1, 2, 3]


# ---------------------------------------------------------------- 切换中途重启（F6 第 8 步）


async def test_restart_during_switching_records_the_interruption_and_keeps_the_committed_model(ledger, clock):
    a = new_service(ledger, clock)
    await a.load()
    await a.acquire_op_lock(OpLock.SWITCH_PREVIEW, owner="alice")
    await a.acquire_op_lock(OpLock.SWITCHING, owner="alice")
    assert a.current().active_model is Model.M1  # 切换还没提交就“崩溃”

    clock.advance(1_000)
    snap = await new_service(ledger, clock).load()

    assert snap.active_model is Model.M1 and snap.gate is Gate.STOPPED and snap.op_locks == frozenset()
    logs = await switch_log(ledger)
    assert len(logs) == 1
    assert (logs[0].result, logs[0].from_model, logs[0].operator, logs[0].at_ms) == ("切换中断", "m1", "alice",
                                                                                clock.now_ms())
    assert "切换中断" in logs[0].detail
    interrupted = await audit_rows(ledger, action="model_state.switch_interrupted")
    assert len(interrupted) == 1 and interrupted[0].result == "切换中断"
    assert interrupted[0].after["active_model"] == "m1"


@pytest.mark.parametrize("lock", [OpLock.SWITCH_PREVIEW, OpLock.BULK_CLOSING], ids=lambda s: s.value)
async def test_restart_with_only_preview_or_bulk_lock_is_not_a_switch_interruption(ledger, clock, lock):
    a = new_service(ledger, clock)
    await a.load()
    await a.acquire_op_lock(lock, owner="alice")
    snap = await new_service(ledger, clock).load()
    assert snap.op_locks == frozenset()
    assert await switch_log(ledger) == []
    assert "model_state.switch_interrupted" not in await audit_actions(ledger)


async def test_restart_after_a_committed_switch_has_no_interruption(ledger, clock):
    a = new_service(ledger, clock)
    await a.load()
    await a.acquire_op_lock(OpLock.SWITCHING, owner="alice")
    await a.set_active_model(Model.M2, expected_version=a.current().version, operator="alice", op_id="op-7")
    snap = await new_service(ledger, clock).load()
    assert snap.active_model is Model.M2 and snap.op_locks == frozenset()
    logs = await switch_log(ledger)
    assert [(r.result, r.to_model) for r in logs] == [("成功", "m2")]  # 只有那条成功的记录


async def test_interruption_is_recorded_once_even_if_the_kernel_restarts_again(ledger, clock):
    a = new_service(ledger, clock)
    await a.load()
    await a.acquire_op_lock(OpLock.SWITCHING, owner="alice")
    await new_service(ledger, clock).load()
    await new_service(ledger, clock).load()
    assert len(await switch_log(ledger)) == 1


# ---------------------------------------------------------------- 行损坏：回退到已停止


async def healthy_m2_running(ledger, clock):
    a = new_service(ledger, clock)
    await a.load()
    await a.set_active_model(Model.M2, expected_version=a.current().version, operator="alice", op_id="op-1")
    await a.set_gate(Gate.RUNNING, reason="启动")
    await a.add_suppression(Suppression.CLOCK_SKEW, reason="偏差")
    return a


CORRUPTIONS = {
    "suppressions 不是合法 JSON": ("UPDATE model_state SET suppressions = '{not json'", "suppressions"),
    "suppressions 含非法取值": ("UPDATE model_state SET suppressions = '[\"no_such_reason\"]'", "suppressions"),
    "suppressions 不是列表": ("UPDATE model_state SET suppressions = '{\"a\":1}'", "suppressions"),
    "gate 取值非法": ("UPDATE model_state SET gate = 'sideways'", "gate"),
    "op_locks 不是合法 JSON": ("UPDATE model_state SET op_locks = '[1,'", "op_locks"),
    "op_locks 含非法取值": ("UPDATE model_state SET op_locks = '[\"frozen\"]'", "op_locks"),
    "run_mode 取值非法": ("UPDATE model_state SET run_mode = 'turbo'", "run_mode"),
    "risk_detail 不是合法 JSON": ("UPDATE model_state SET risk_detail = 'oops'", "risk_detail"),
    "risk_detail 不是对象": ("UPDATE model_state SET risk_detail = '[1,2]'", "risk_detail"),
    "version 不是整数": ("UPDATE model_state SET version = 'abc'", "version"),
    "version 小于 1": ("UPDATE model_state SET version = 0", "version"),
    "m2_started_ms 不是整数": ("UPDATE model_state SET m2_started_ms = 'later'", "m2_started_ms"),
    "mismatch_since_ms 不是整数": ("UPDATE model_state SET mismatch_since_ms = 'soon'", "mismatch_since_ms"),
    "op_lock_expires_ms 不是整数": ("UPDATE model_state SET op_lock_expires_ms = 'never'", "op_lock_expires_ms"),
    "updated_ms 不是整数": ("UPDATE model_state SET updated_ms = 'yesterday'", "updated_ms"),
}


@pytest.mark.parametrize("sql,column", CORRUPTIONS.values(), ids=CORRUPTIONS.keys())
async def test_corrupt_row_falls_back_to_stopped_and_keeps_a_readable_model(ledger, clock, raw, sql, column):
    await healthy_m2_running(ledger, clock)
    raw.exec(sql)

    svc = new_service(ledger, clock)
    snap = await svc.load()

    assert svc.current() == snap
    assert snap.gate is Gate.STOPPED  # 禁止新开仓
    assert snap.op_locks == frozenset()
    assert snap.active_model is Model.M2  # 生效模型读得出就保留
    assert not snap.can_open(Model.M2)[0]
    assert isinstance(snap.version, int) and snap.version >= 1
    assert snap.risk_detail["corrupt_recovered"] is True
    assert column in snap.risk_detail["corrupt_reason"]
    recovered = await audit_rows(ledger, action="model_state.corrupt_recovered")
    assert len(recovered) == 1 and column in json.dumps(recovered[0].after, ensure_ascii=False)
    # 修复后的行是健康的：再重启一次不会重复报损坏，仍然是已停止
    again = await new_service(ledger, clock).load()
    assert again.gate is Gate.STOPPED and again.active_model is Model.M2
    assert len(await audit_rows(ledger, action="model_state.corrupt_recovered")) == 1


async def test_unreadable_active_model_falls_back_to_m1(ledger, clock, raw):
    await healthy_m2_running(ledger, clock)
    raw.exec("UPDATE model_state SET active_model = 'm9'")
    snap = await new_service(ledger, clock).load()
    assert snap.active_model is Model.M1 and snap.gate is Gate.STOPPED
    assert "active_model" in snap.risk_detail["corrupt_reason"]
    assert raw.query("SELECT active_model, gate FROM model_state") == [("m1", "stopped")]


async def test_unreadable_version_restarts_the_count_at_one(ledger, clock, raw):
    await healthy_m2_running(ledger, clock)
    raw.exec("UPDATE model_state SET version = 'abc'")
    assert (await new_service(ledger, clock).load()).version == 1


async def test_partially_valid_lists_keep_the_valid_items(ledger, clock, raw):
    await healthy_m2_running(ledger, clock)
    raw.exec("UPDATE model_state SET suppressions = ?", (json.dumps(["reconcile_mismatch", "no_such_reason"]),))
    snap = await new_service(ledger, clock).load()
    assert snap.suppressions == {Suppression.RECONCILE_MISMATCH}
    assert "suppressions" in snap.risk_detail["corrupt_reason"]


async def test_corrupt_recovery_keeps_valid_risk_detail_content(ledger, clock, raw):
    a = await healthy_m2_running(ledger, clock)
    await a.set_risk_detail({"reasons": ["回撤"]})
    raw.exec("UPDATE model_state SET gate = 'sideways'")
    snap = await new_service(ledger, clock).load()
    assert snap.risk_detail["reasons"] == ["回撤"] and snap.risk_detail["corrupt_recovered"] is True


async def test_everything_corrupt_at_once_still_loads_into_a_safe_state(ledger, clock, raw):
    await healthy_m2_running(ledger, clock)
    raw.exec("UPDATE model_state SET active_model='x', gate='x', suppressions='x', op_locks='x', run_mode='x', "
             "version='x', m2_started_ms='x', mismatch_since_ms='x', risk_detail='x', updated_ms='x', "
             "op_lock_expires_ms='x'")
    snap = await new_service(ledger, clock).load()
    assert (snap.active_model, snap.gate, snap.run_mode, snap.version) == (Model.M1, Gate.STOPPED, RunMode.NORMAL, 1)
    assert snap.suppressions == frozenset() and snap.op_locks == frozenset()
    assert snap.m2_started_ms is None and snap.mismatch_since_ms is None
    assert snap.updated_ms == clock.now_ms()
    assert snap.risk_detail["corrupt_recovered"] is True


async def test_unreadable_op_locks_cannot_claim_an_interrupted_switch(ledger, clock, raw):
    a = new_service(ledger, clock)
    await a.load()
    await a.acquire_op_lock(OpLock.SWITCHING, owner="alice")
    raw.exec("UPDATE model_state SET op_locks = '[\"switching\"'")  # JSON 坏了：不知道当时在不在切换
    snap = await new_service(ledger, clock).load()
    assert snap.op_locks == frozenset() and "op_locks" in snap.risk_detail["corrupt_reason"]
    assert await switch_log(ledger) == []


async def test_healthy_row_is_never_marked_corrupt(ledger, clock, raw):
    await healthy_m2_running(ledger, clock)
    raw.exec("UPDATE model_state SET risk_detail = NULL")  # 允许为空
    snap = await new_service(ledger, clock).load()
    assert "corrupt_recovered" not in snap.risk_detail
    assert "model_state.corrupt_recovered" not in await audit_actions(ledger)


async def test_corrupt_row_with_hostile_values_does_not_break_the_audit_write(ledger, clock, raw):
    await healthy_m2_running(ledger, clock)
    raw.exec("UPDATE model_state SET gate = ?, run_mode = 1e999", ("x" * 5000,))  # 超长文本与无穷大
    snap = await new_service(ledger, clock).load()
    assert snap.gate is Gate.STOPPED and snap.risk_detail["corrupt_recovered"] is True
    (row,) = await audit_rows(ledger, action="model_state.corrupt_recovered")
    assert len(json.dumps(row.before, ensure_ascii=False)) < 3000
