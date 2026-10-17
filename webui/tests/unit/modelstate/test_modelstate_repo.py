"""ledger/repo_modelstate：仓储函数本身（事务内读-判-写、只追加审计、切换日志、版本列只由仓储维护）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import Gate, Model, OpLock, RunMode, Suppression
from dmr_kernel.ledger import repo_audit, repo_modelstate
from dmr_kernel.ledger.errors import LedgerError
from dmr_kernel.ledger.records import ModelStateRecord, ModelSwitchLogRecord
from dmr_kernel.ledger.repo_modelstate import Change, SwitchLogEntry

T0 = 1_760_000_000_000


async def init(ledger, now_ms=T0):
    return await ledger.write(repo_modelstate.startup, now_ms=now_ms)


async def apply(ledger, decide, now_ms=T0 + 1):
    return await ledger.write(repo_modelstate.apply, decide, now_ms=now_ms)


async def audit_rows(ledger, **filters):
    return list(reversed(await ledger.read(repo_audit.recent, limit=1000, **filters)))


# ---------------------------------------------------------------- startup / read_state


async def test_read_state_is_none_before_the_first_startup(ledger):
    assert await ledger.read(repo_modelstate.read_state) is None


async def test_first_startup_inserts_the_default_row(ledger):
    record, info = await init(ledger)
    assert isinstance(record, ModelStateRecord)
    assert (record.id, record.active_model, record.gate, record.run_mode, record.version) == (
        1, Model.M1, Gate.STOPPED, RunMode.NORMAL, 1)
    assert record.suppressions == [] and record.op_locks == [] and record.risk_detail == {}
    assert record.updated_ms == T0
    assert info == {"created": True, "problems": [], "interrupted_switch": False, "previous_version": None}
    assert await ledger.read(repo_modelstate.read_state) == record


async def test_second_startup_is_a_restart(ledger):
    first, _ = await init(ledger)
    second, info = await init(ledger, T0 + 5)
    assert second.version == first.version + 1 and second.updated_ms == T0 + 5
    assert info == {"created": False, "problems": [], "interrupted_switch": False, "previous_version": 1}


async def test_startup_reports_problems_in_chinese(ledger, raw):
    await init(ledger)
    raw.exec("UPDATE model_state SET gate = 'sideways', suppressions = '{x'")
    record, info = await init(ledger, T0 + 5)
    assert record.gate is Gate.STOPPED
    assert len(info["problems"]) == 2
    assert any("gate" in p for p in info["problems"]) and any("suppressions" in p for p in info["problems"])
    assert record.risk_detail["corrupt_recovered"] is True
    assert record.risk_detail["corrupt_at_ms"] == T0 + 5


async def test_startup_reports_an_interrupted_switch(ledger):
    await init(ledger)
    await apply(ledger, lambda rec: Change(updates={"op_locks": ["switching"], "op_lock_owner": "alice"},
                                           action="t.lock"))
    _, info = await init(ledger, T0 + 5)
    assert info["interrupted_switch"] is True


async def test_startup_writes_the_audit_row_in_the_same_transaction(ledger, raw):
    await init(ledger)
    raw.exec("CREATE TRIGGER boom BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(Exception, match="boom"):
        await init(ledger, T0 + 5)
    raw.exec("DROP TRIGGER boom")
    assert (await ledger.read(repo_modelstate.read_state)).version == 1  # 启动重置没有半截落盘


# ---------------------------------------------------------------- apply


async def test_apply_updates_columns_bumps_version_and_appends_audit(ledger):
    await init(ledger)
    record, changed = await apply(ledger, lambda rec: Change(
        updates={"gate": Gate.RUNNING, "suppressions": ["clock_skew"]}, action="t.change", reason="测试",
        operator="alice", request_id="req-1"))
    assert changed is True
    assert (record.gate, record.suppressions, record.version, record.updated_ms) == (
        Gate.RUNNING, ["clock_skew"], 2, T0 + 1)
    (row,) = await audit_rows(ledger, action="t.change")
    assert (row.operator, row.target, row.request_id, row.result, row.at_ms) == (
        "alice", "model_state", "req-1", "ok", T0 + 1)
    assert row.before == {"version": 1, "gate": "stopped", "suppressions": []}
    assert row.after == {"version": 2, "gate": "running", "suppressions": ["clock_skew"], "reason": "测试"}


async def test_apply_receives_the_current_record_inside_the_transaction(ledger):
    await init(ledger)
    seen = []

    def decide(rec):
        seen.append(rec)
        return None

    record, changed = await apply(ledger, decide)
    assert changed is False and record == seen[0] and isinstance(seen[0], ModelStateRecord)
    assert record.version == 1 and await audit_rows(ledger, action="t.change") == []


async def test_apply_with_unchanged_values_is_a_noop(ledger):
    await init(ledger)
    record, changed = await apply(ledger, lambda rec: Change(
        updates={"gate": Gate.STOPPED, "suppressions": [], "risk_detail": {}}, action="t.change"))
    assert changed is False and record.version == 1
    assert await audit_rows(ledger, action="t.change") == []


async def test_apply_only_audits_the_columns_that_really_changed(ledger):
    await init(ledger)
    _, changed = await apply(ledger, lambda rec: Change(
        updates={"gate": Gate.STOPPED, "run_mode": RunMode.POLLING_DEGRADED}, action="t.change"))
    assert changed is True
    (row,) = await audit_rows(ledger, action="t.change")
    assert row.before == {"version": 1, "run_mode": "normal"} and row.after == {"version": 2, "run_mode": "polling_degraded"}


async def test_apply_refuses_to_write_the_version_or_unknown_columns(ledger):
    await init(ledger)
    for bad in ({"version": 99}, {"id": 2}, {"updated_ms": 1}, {"no_such_column": 1}):
        with pytest.raises(ValueError):
            await apply(ledger, lambda rec, bad=bad: Change(updates=bad, action="t.change"))
    assert (await ledger.read(repo_modelstate.read_state)).version == 1


async def test_apply_rolls_back_everything_when_decide_raises(ledger):
    await init(ledger)

    def decide(rec):
        raise RuntimeError("decide failed")

    with pytest.raises(RuntimeError, match="decide failed"):
        await apply(ledger, decide)
    assert (await ledger.read(repo_modelstate.read_state)).version == 1


async def test_apply_needs_the_row_to_exist(ledger):
    with pytest.raises(LedgerError, match="load"):
        await apply(ledger, lambda rec: None)


async def test_apply_writes_the_switch_log_with_the_state_and_forces_a_commit(ledger):
    await init(ledger)
    entry = SwitchLogEntry(from_model="m1", to_model="m1", mode="keep", result="成功", op_id="op-1",
                           operator="alice", detail="同一个模型")
    record, changed = await apply(ledger, lambda rec: Change(updates={"gate": Gate.STOPPED}, action="t.switch",
                                                             switch_log=entry))
    assert changed is True and record.version == 2  # 有切换日志就一定提交，哪怕列没变
    (log,) = await ledger.read(repo_modelstate.recent_switch_log)
    assert isinstance(log, ModelSwitchLogRecord)
    assert (log.from_model, log.to_model, log.mode, log.result, log.op_id, log.operator, log.detail, log.at_ms) == (
        "m1", "m1", "keep", "成功", "op-1", "alice", "同一个模型", T0 + 1)


async def test_apply_normalizes_json_columns_like_the_ledger_does(ledger):
    from decimal import Decimal

    await init(ledger)
    record, _ = await apply(ledger, lambda rec: Change(updates={"risk_detail": {"limit": Decimal("1.50")}},
                                                       action="t.change"))
    assert record.risk_detail == {"limit": "1.50"}
    # 再写一次同样的值：规范化之后相等，不算变化
    _, changed = await apply(ledger, lambda rec: Change(updates={"risk_detail": {"limit": Decimal("1.50")}},
                                                        action="t.change"))
    assert changed is False


# ---------------------------------------------------------------- recent_switch_log


async def test_recent_switch_log_is_newest_first_and_limited(ledger):
    await init(ledger)
    for i in range(5):
        entry = SwitchLogEntry("m1", "m2", None, "成功", f"op-{i}", "alice")
        await apply(ledger, lambda rec, e=entry: Change(updates={}, action="t.switch", switch_log=e), now_ms=T0 + i)
    logs = await ledger.read(repo_modelstate.recent_switch_log, limit=3)
    assert [r.op_id for r in logs] == ["op-4", "op-3", "op-2"]
    assert len(await ledger.read(repo_modelstate.recent_switch_log)) == 5
    for bad in (0, -1, 10_000, True):
        with pytest.raises(ValueError):
            await ledger.read(repo_modelstate.recent_switch_log, limit=bad)


async def test_enum_columns_accept_only_their_own_enum(ledger):
    await init(ledger)
    with pytest.raises(Exception):
        await apply(ledger, lambda rec: Change(updates={"gate": Model.M2}, action="t.change"))
    assert (await ledger.read(repo_modelstate.read_state)).gate is Gate.STOPPED
    # 列表里存的是取值字符串
    record, _ = await apply(ledger, lambda rec: Change(
        updates={"op_locks": [OpLock.BULK_CLOSING.value], "suppressions": [Suppression.UNPROTECTED.value]},
        action="t.change"))
    assert record.op_locks == ["bulk_closing"] and record.suppressions == ["unprotected"]
