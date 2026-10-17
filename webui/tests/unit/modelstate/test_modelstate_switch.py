"""set_active_model（F6 第 6 步：同一个事务里换模型、闸已停止、解除切换中与切换预览中、写 model_switch_log）与并发。"""
from __future__ import annotations

import asyncio

import pytest

from dmr_kernel.core.events import EventBus
from dmr_kernel.core.modelstate import ModelStateService, ModelStateSnapshot, OpLockConflict, StaleVersion
from dmr_kernel.core.types import Gate, Model, OpLock, Suppression
from dmr_kernel.ledger import repo_audit, repo_modelstate

PREVIEW, SWITCHING, BULK = OpLock.SWITCH_PREVIEW, OpLock.SWITCHING, OpLock.BULK_CLOSING


@pytest.fixture
def svc(ledger, clock, bus) -> ModelStateService:
    """未加载的服务：协程测试里第一行 ``await svc.load()``。"""
    return ModelStateService(ledger, bus, clock)


async def audit_rows(ledger, **filters):
    return list(reversed(await ledger.read(repo_audit.recent, limit=1000, **filters)))


async def switch_log(ledger):
    return list(reversed(await ledger.read(repo_modelstate.recent_switch_log, limit=100)))


async def stored(ledger):
    return await ledger.read(repo_modelstate.read_state)


# ---------------------------------------------------------------- 成功路径


async def test_switch_commits_everything_together(svc, ledger, clock, events):
    await svc.load()
    await svc.set_gate(Gate.RUNNING, reason="先运行")
    await svc.acquire_op_lock(PREVIEW, owner="seat-1", expires_ms=clock.now_ms() + 60_000)
    await svc.acquire_op_lock(SWITCHING, owner="seat-1")
    before = svc.current()
    clock.advance(2_000)
    await events.flush()

    snap = await svc.set_active_model(Model.M2, expected_version=before.version, operator="alice", op_id="op-42")

    assert snap == svc.current()
    assert snap.active_model is Model.M2
    assert snap.gate is Gate.STOPPED  # 新模型处于已停止，需要手动启动
    assert snap.op_locks == frozenset()  # 切换中与切换预览中一并解除
    assert (snap.op_lock_owner, snap.op_lock_expires_ms) == (None, None)
    assert snap.version == before.version + 1 and snap.updated_ms == clock.now_ms()
    row = await stored(ledger)
    assert (row.active_model, row.gate, row.op_locks, row.version) == (Model.M2, Gate.STOPPED, [], snap.version)

    (entry,) = await switch_log(ledger)
    assert (entry.from_model, entry.to_model, entry.result) == ("m1", "m2", "成功")
    assert (entry.op_id, entry.operator, entry.at_ms) == ("op-42", "alice", clock.now_ms())
    assert entry.mode is None and "m1" in entry.detail and "m2" in entry.detail

    audit = (await audit_rows(ledger, action="model_state.switch"))[-1]
    assert (audit.operator, audit.request_id, audit.result) == ("alice", "op-42", "ok")
    assert audit.before["active_model"] == "m1" and audit.after["active_model"] == "m2"
    assert audit.after["gate"] == "stopped" and audit.after["op_locks"] == []

    got = await events.flush()
    assert got[-1].version == snap.version


async def test_switch_records_the_chosen_mode(svc, ledger):
    await svc.load()
    await svc.set_active_model(Model.M3, expected_version=svc.current().version, operator="alice", op_id="op-1",
                               mode="close_all_first")
    (entry,) = await switch_log(ledger)
    assert entry.mode == "close_all_first" and entry.to_model == "m3"


async def test_switch_keeps_suppressions_run_mode_and_other_fields(svc):
    await svc.load()
    await svc.add_suppression(Suppression.EXCHANGE_RESTRICTED, reason="限制")
    await svc.set_m2_started(77)
    snap = await svc.set_active_model(Model.M2, expected_version=svc.current().version, operator="a", op_id="o")
    assert snap.suppressions == {Suppression.EXCHANGE_RESTRICTED} and snap.m2_started_ms == 77


async def test_after_a_switch_nothing_can_open_until_the_gate_is_opened(svc):
    await svc.load()
    await svc.set_gate(Gate.RUNNING, reason="x")
    assert svc.current().can_open(Model.M1)[0]
    snap = await svc.set_active_model(Model.M2, expected_version=svc.current().version, operator="a", op_id="o")
    assert not snap.can_open(Model.M1)[0] and not snap.can_open(Model.M2)[0]
    snap = await svc.set_gate(Gate.RUNNING, reason="启动模型 2")
    assert snap.can_open(Model.M2)[0] and snap.can_auto_takeover()[0]


async def test_switch_without_the_switching_lock_is_allowed_at_this_layer(svc):
    # 是否持有切换中的锁由 ops 保证；状态层只保证提交是原子的
    await svc.load()
    snap = await svc.set_active_model(Model.M2, expected_version=svc.current().version, operator="a", op_id="o")
    assert snap.active_model is Model.M2


async def test_switch_to_the_same_model_still_commits_and_logs(svc, ledger):
    await svc.load()
    await svc.acquire_op_lock(SWITCHING, owner="alice")
    snap = await svc.set_active_model(Model.M1, expected_version=svc.current().version, operator="a", op_id="o")
    assert snap.active_model is Model.M1 and snap.op_locks == frozenset()
    assert [(e.from_model, e.to_model) for e in await switch_log(ledger)] == [("m1", "m1")]


# ---------------------------------------------------------------- 拒绝路径


async def test_switch_with_a_stale_version_changes_nothing(svc, ledger, events):
    await svc.load()
    await svc.acquire_op_lock(SWITCHING, owner="alice")
    snap = svc.current()
    audits = len(await audit_rows(ledger))
    with pytest.raises(StaleVersion) as err:
        await svc.set_active_model(Model.M2, expected_version=snap.version - 1, operator="a", op_id="o")
    assert (err.value.expected, err.value.actual) == (snap.version - 1, snap.version)
    assert svc.current() == snap
    assert (await stored(ledger)).active_model is Model.M1
    assert await switch_log(ledger) == [] and len(await audit_rows(ledger)) == audits


async def test_switch_during_bulk_closing_is_refused(svc, ledger):
    await svc.load()
    await svc.acquire_op_lock(BULK, owner="alice")
    snap = svc.current()
    with pytest.raises(OpLockConflict, match="批量平仓"):
        await svc.set_active_model(Model.M2, expected_version=snap.version, operator="a", op_id="o")
    assert svc.current() == snap and await switch_log(ledger) == []


async def test_switch_to_an_unknown_model_is_a_program_error(svc):
    await svc.load()
    with pytest.raises(ValueError):
        await svc.set_active_model("m9", expected_version=svc.current().version, operator="a", op_id="o")  # type: ignore[arg-type]


# ---------------------------------------------------------------- 原子性：事务中途失败，旧模型不变


@pytest.mark.parametrize("table", ["model_switch_log", "audit_log"])
async def test_switch_is_atomic_when_a_write_in_the_transaction_fails(svc, ledger, raw, clock, events, table):
    await svc.load()
    await svc.set_gate(Gate.RUNNING, reason="先运行")
    await svc.acquire_op_lock(PREVIEW, owner="alice", expires_ms=clock.now_ms() + 60_000)
    await svc.acquire_op_lock(SWITCHING, owner="alice")
    before = svc.current()
    audits = len(await audit_rows(ledger))
    await events.flush()
    published = len(events.events)
    # 状态行已经更新之后，再往 model_switch_log / audit_log 插入时失败（事务中途出错）
    raw.exec(f"CREATE TRIGGER boom BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'boom'); END")

    with pytest.raises(Exception, match="boom"):
        await svc.set_active_model(Model.M2, expected_version=before.version, operator="alice", op_id="op-9")

    # 内存与落盘都还是旧状态：旧模型、闸、两把锁、版本、审计、切换日志一样都没变
    assert svc.current() == before
    row = await stored(ledger)
    assert (row.active_model, row.gate, sorted(row.op_locks), row.version) == (
        Model.M1, Gate.RUNNING, ["switch_preview", "switching"], before.version)
    assert raw.query("SELECT count(*) FROM model_switch_log") == [(0,)]
    assert len(await audit_rows(ledger)) == audits
    await events.flush()
    assert len(events.events) == published

    # 故障排除后，同一次切换可以成功：没有留下半截状态，写线程也没被卡住
    raw.exec("DROP TRIGGER boom")
    snap = await svc.set_active_model(Model.M2, expected_version=before.version, operator="alice", op_id="op-9")
    assert snap.active_model is Model.M2 and snap.op_locks == frozenset() and snap.gate is Gate.STOPPED
    assert [e.result for e in await switch_log(ledger)] == ["成功"]


async def test_crash_during_a_failed_switch_restarts_on_the_old_model_with_an_interruption(svc, ledger, raw, clock):
    await svc.load()
    await svc.acquire_op_lock(SWITCHING, owner="alice")
    raw.exec("CREATE TRIGGER boom BEFORE INSERT ON model_switch_log BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(Exception, match="boom"):
        await svc.set_active_model(Model.M2, expected_version=svc.current().version, operator="alice", op_id="op-9")
    raw.exec("DROP TRIGGER boom")

    again = await ModelStateService(ledger, EventBus(), clock).load()  # 进程在这里被杀，重启

    assert again.active_model is Model.M1 and again.gate is Gate.STOPPED and again.op_locks == frozenset()
    assert [e.result for e in await switch_log(ledger)] == ["切换中断"]


# ---------------------------------------------------------------- 并发：同版本只有一个成功


async def test_concurrent_requests_on_the_same_version_only_one_succeeds(svc, ledger):
    await svc.load()
    v = svc.current().version
    results = await asyncio.gather(
        *[svc.acquire_op_lock(PREVIEW, owner=f"seat-{i}", expected_version=v) for i in range(8)],
        return_exceptions=True)
    winners = [r for r in results if isinstance(r, ModelStateSnapshot)]
    losers = [r for r in results if isinstance(r, StaleVersion)]
    assert len(winners) == 1 and len(losers) == 7, results
    assert svc.current().version == v + 1
    assert svc.current().op_lock_owner == winners[0].op_lock_owner
    assert len(await audit_rows(ledger, action="model_state.op_lock_acquire")) == 1


async def test_concurrent_requests_of_different_kinds_on_the_same_version_only_one_succeeds(svc):
    await svc.load()
    v = svc.current().version
    results = await asyncio.gather(
        svc.set_gate(Gate.RUNNING, reason="a", expected_version=v),
        svc.acquire_op_lock(BULK, owner="alice", expected_version=v),
        svc.set_gate(Gate.RUNNING, reason="c", expected_version=v),
        return_exceptions=True)
    assert sum(isinstance(r, ModelStateSnapshot) for r in results) == 1
    assert sum(isinstance(r, StaleVersion) for r in results) == 2
    assert svc.current().version == v + 1


async def test_concurrent_switches_on_the_same_version_only_one_commits(svc, ledger):
    await svc.load()
    v = svc.current().version
    results = await asyncio.gather(
        svc.set_active_model(Model.M2, expected_version=v, operator="alice", op_id="op-a"),
        svc.set_active_model(Model.M3, expected_version=v, operator="bob", op_id="op-b"),
        return_exceptions=True)
    assert sum(isinstance(r, ModelStateSnapshot) for r in results) == 1
    assert sum(isinstance(r, StaleVersion) for r in results) == 1
    logs = await switch_log(ledger)
    assert len(logs) == 1 and svc.current().active_model.value == logs[0].to_model


async def test_concurrent_requests_without_a_version_conflict_on_the_lock_rules_instead(svc):
    await svc.load()
    results = await asyncio.gather(
        *[svc.acquire_op_lock(PREVIEW, owner=f"seat-{i}") for i in range(5)], return_exceptions=True)
    assert sum(isinstance(r, ModelStateSnapshot) for r in results) == 1
    assert sum(isinstance(r, OpLockConflict) for r in results) == 4


async def test_version_check_is_decided_by_the_database_not_by_memory(ledger, clock):
    a = ModelStateService(ledger, EventBus(), clock)
    await a.load()
    b = ModelStateService(ledger, EventBus(), clock)
    await b.load()  # 另一个实例（比如重启）把库里的版本推高了；a 的内存已经过期
    stale = a.current().version
    assert b.current().version == stale + 1
    with pytest.raises(StaleVersion) as err:
        await a.set_gate(Gate.RUNNING, reason="x", expected_version=stale)
    assert err.value.actual == stale + 1
    # 没带版本的调用基于库里的最新状态，成功后内存也追上了
    snap = await a.set_gate(Gate.RUNNING, reason="x")
    assert snap.version == stale + 2 and a.current() == snap
