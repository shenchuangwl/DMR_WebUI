"""ModelStateService 的状态变更：版本 +1、与 audit_log 同一事务、发布 ModelStateChanged、无变化不增版本、版本校验。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.events import EventBus
from dmr_kernel.core.modelstate import ModelStateService, StaleVersion
from dmr_kernel.core.types import Gate, Model, OpLock, RunMode, Suppression
from dmr_kernel.ledger import repo_audit, repo_modelstate

START_PLUS = 1_760_000_000_000


@pytest.fixture
def svc(ledger, clock, bus) -> ModelStateService:
    """未加载的服务：协程测试里第一行 ``await svc.load()``（不用异步夹具）。"""
    return ModelStateService(ledger, bus, clock)


async def audit_rows(ledger, **filters):
    return list(reversed(await ledger.read(repo_audit.recent, limit=1000, **filters)))


async def audit_count(ledger) -> int:
    return len(await audit_rows(ledger))


async def stored(ledger):
    return await ledger.read(repo_modelstate.read_state)


# ---------------------------------------------------------------- 每种变更：版本 +1、一条审计、一个事件、落盘


def _gate(s):
    return s.set_gate(Gate.RUNNING, reason="启动")


def _add(s):
    return s.add_suppression(Suppression.CLOCK_SKEW, reason="偏差超限")


async def _remove(s):
    await s.add_suppression(Suppression.CLOCK_SKEW, reason="偏差超限")
    return await s.remove_suppression(Suppression.CLOCK_SKEW, reason="偏差回落")


def _acquire(s):
    return s.acquire_op_lock(OpLock.BULK_CLOSING, owner="alice")


async def _release(s):
    await s.acquire_op_lock(OpLock.BULK_CLOSING, owner="alice")
    return await s.release_op_lock(OpLock.BULK_CLOSING)


def _mode(s):
    return s.set_run_mode(RunMode.POLLING_DEGRADED, reason="数据流断")


def _m2(s):
    return s.set_m2_started(1234)


def _mismatch(s):
    return s.set_mismatch_since(5678)


def _risk(s):
    return s.set_risk_detail({"reasons": ["回撤"]})


def _switch(s):
    return s.set_active_model(Model.M2, expected_version=s.current().version, operator="alice", op_id="op-1")


# 每个用例：(调用, 该调用内部的变更次数, 审计动作)
MUTATIONS = {
    "set_gate": (_gate, 1, "model_state.gate"),
    "add_suppression": (_add, 1, "model_state.suppression_add"),
    "remove_suppression": (_remove, 2, "model_state.suppression_remove"),
    "acquire_op_lock": (_acquire, 1, "model_state.op_lock_acquire"),
    "release_op_lock": (_release, 2, "model_state.op_lock_release"),
    "set_run_mode": (_mode, 1, "model_state.run_mode"),
    "set_m2_started": (_m2, 1, "model_state.m2_started"),
    "set_mismatch_since": (_mismatch, 1, "model_state.mismatch_since"),
    "set_risk_detail": (_risk, 1, "model_state.risk_detail"),
    "set_active_model": (_switch, 1, "model_state.switch"),
}


@pytest.mark.parametrize("call,changes,action", MUTATIONS.values(), ids=MUTATIONS.keys())
async def test_every_change_bumps_version_audits_and_publishes(svc, ledger, clock, events, call, changes, action):
    await svc.load()
    await events.flush()  # 先放掉 load 的事件
    start_version, start_audits = svc.current().version, await audit_count(ledger)
    clock.advance(1_000)

    result = await call(svc)

    assert result == svc.current()
    assert result.version == start_version + changes
    assert result.updated_ms == clock.now_ms()
    assert (await stored(ledger)).version == result.version  # 已落盘
    audits = await audit_rows(ledger)
    assert len(audits) == start_audits + changes
    last = audits[-1]
    assert last.action == action and last.target == "model_state" and last.result == "ok"
    assert last.at_ms == clock.now_ms()
    assert last.before["version"] == result.version - 1 and last.after["version"] == result.version
    got = await events.flush()
    assert [e.version for e in got][-changes:] == list(range(start_version + 1, result.version + 1))
    assert got[-1].at_ms == clock.now_ms()


async def test_versions_are_consecutive_across_mixed_changes(svc, events):
    await svc.load()
    v0 = svc.current().version
    await svc.set_gate(Gate.RUNNING, reason="a")
    await svc.add_suppression(Suppression.UNPROTECTED, reason="b")
    await svc.set_run_mode(RunMode.POLLING_DEGRADED, reason="c")
    await svc.acquire_op_lock(OpLock.SWITCH_PREVIEW, owner="s1")
    await svc.release_op_lock(OpLock.SWITCH_PREVIEW)
    await svc.remove_suppression(Suppression.UNPROTECTED, reason="d")
    await svc.set_gate(Gate.STOPPED, reason="e")
    assert svc.current().version == v0 + 7
    await events.flush()
    assert events.versions[-7:] == list(range(v0 + 1, v0 + 8))


async def test_audit_trail_records_what_changed_and_why(svc, ledger):
    await svc.load()
    await svc.set_gate(Gate.RUNNING, reason="操作员启动")
    row = (await audit_rows(ledger, action="model_state.gate"))[-1]
    assert row.operator == "system"
    assert row.before == {"version": 1, "gate": "stopped"}
    assert row.after == {"version": 2, "gate": "running", "reason": "操作员启动"}
    await svc.add_suppression(Suppression.RECONCILE_UNKNOWN, reason="连续 3 次失败")
    row = (await audit_rows(ledger, action="model_state.suppression_add"))[-1]
    assert row.before["suppressions"] == [] and row.after["suppressions"] == ["reconcile_unknown"]
    assert row.after["reason"] == "连续 3 次失败"


# ---------------------------------------------------------------- 状态落盘，重启后读得回来


async def test_changes_survive_a_restart(svc, ledger, clock):
    await svc.load()
    await svc.set_active_model(Model.M2, expected_version=svc.current().version, operator="alice", op_id="op-1")
    await svc.add_suppression(Suppression.RISK_TRIPPED, reason="回撤")
    await svc.set_run_mode(RunMode.POLLING_DEGRADED, reason="数据流断")
    await svc.set_m2_started(11)
    await svc.set_mismatch_since(22)
    await svc.set_risk_detail({"limit": Decimal("12.50"), "reasons": ["回撤"]})
    before = svc.current()
    after = await ModelStateService(ledger, EventBus(), clock).load()
    assert after.active_model is Model.M2
    assert after.suppressions == before.suppressions == {Suppression.RISK_TRIPPED}
    assert after.run_mode is RunMode.POLLING_DEGRADED
    assert (after.m2_started_ms, after.mismatch_since_ms) == (11, 22)
    assert after.risk_detail == {"limit": "12.50", "reasons": ["回撤"]}  # Decimal 一律存成字符串


# ---------------------------------------------------------------- 无变化：不增版本、不审计、不发事件


async def test_noop_changes_do_not_bump_the_version(svc, ledger, events):
    await svc.load()
    await svc.add_suppression(Suppression.CLOCK_SKEW, reason="偏差")
    await svc.set_m2_started(5)
    await svc.set_mismatch_since(6)
    await svc.set_risk_detail({"a": 1})
    await svc.set_run_mode(RunMode.POLLING_DEGRADED, reason="断流")
    await svc.set_gate(Gate.RUNNING, reason="启动")
    snap, audits = svc.current(), await audit_count(ledger)
    await events.flush()
    published = len(events.events)

    again = [
        await svc.add_suppression(Suppression.CLOCK_SKEW, reason="偏差"),  # 已存在则不变
        await svc.remove_suppression(Suppression.EXCHANGE_RESTRICTED, reason="本来就没有"),
        await svc.set_gate(Gate.RUNNING, reason="已经在运行"),
        await svc.set_run_mode(RunMode.POLLING_DEGRADED, reason="已经降级"),
        await svc.set_m2_started(5),
        await svc.set_mismatch_since(6),
        await svc.set_risk_detail({"a": 1}),
        await svc.release_op_lock(OpLock.SWITCHING),  # 没持有的锁：释放是空操作
    ]

    assert all(s == snap for s in again)
    assert svc.current() == snap
    assert await audit_count(ledger) == audits
    await events.flush()
    assert len(events.events) == published


async def test_clearing_values_is_a_change(svc):
    await svc.load()
    await svc.set_m2_started(5)
    await svc.set_mismatch_since(6)
    await svc.set_risk_detail({"a": 1})
    v = svc.current().version
    await svc.set_m2_started(None)
    await svc.set_mismatch_since(None)
    await svc.set_risk_detail({})
    snap = svc.current()
    assert (snap.m2_started_ms, snap.mismatch_since_ms, snap.risk_detail) == (None, None, {})
    assert snap.version == v + 3


# ---------------------------------------------------------------- 抑制原因：集合语义


@pytest.mark.parametrize("suppression", list(Suppression), ids=lambda s: s.value)
async def test_add_and_remove_each_suppression(svc, suppression):
    await svc.load()
    snap = await svc.add_suppression(suppression, reason="测试")
    assert snap.suppressions == {suppression}
    assert not snap.can_open(Model.M1)[0]
    snap = await svc.remove_suppression(suppression, reason="解除")
    assert snap.suppressions == frozenset()


async def test_suppressions_accumulate_and_are_removed_independently(svc):
    await svc.load()
    await svc.add_suppression(Suppression.CLOCK_SKEW, reason="a")
    await svc.add_suppression(Suppression.UNPROTECTED, reason="b")
    snap = await svc.remove_suppression(Suppression.CLOCK_SKEW, reason="c")
    assert snap.suppressions == {Suppression.UNPROTECTED}


async def test_suppression_accepts_the_string_value(svc):
    await svc.load()
    snap = await svc.add_suppression("clock_skew", reason="x")  # type: ignore[arg-type]
    assert snap.suppressions == {Suppression.CLOCK_SKEW}
    with pytest.raises(ValueError):
        await svc.add_suppression("no_such_reason", reason="x")  # type: ignore[arg-type]


# ---------------------------------------------------------------- 版本校验


async def test_stale_expected_version_is_rejected_without_side_effects(svc, ledger, events):
    await svc.load()
    await svc.set_gate(Gate.RUNNING, reason="先改一次")  # 版本变成 2
    snap, audits = svc.current(), await audit_count(ledger)
    await events.flush()
    published = len(events.events)

    with pytest.raises(StaleVersion) as err:
        await svc.set_gate(Gate.STOPPED, reason="过期的请求", expected_version=1)
    assert (err.value.expected, err.value.actual) == (1, snap.version)

    assert svc.current() == snap and (await stored(ledger)).gate is Gate.RUNNING
    assert await audit_count(ledger) == audits
    await events.flush()
    assert len(events.events) == published


async def test_matching_expected_version_succeeds(svc):
    await svc.load()
    v = svc.current().version
    snap = await svc.set_gate(Gate.RUNNING, reason="启动", expected_version=v)
    assert snap.version == v + 1 and snap.gate is Gate.RUNNING


async def test_expected_version_is_checked_even_when_the_change_would_be_a_noop(svc):
    await svc.load()
    with pytest.raises(StaleVersion):
        await svc.set_gate(Gate.STOPPED, reason="已经是停止", expected_version=99)


async def test_expected_version_none_skips_the_check(svc):
    await svc.load()
    await svc.set_gate(Gate.RUNNING, reason="a")
    await svc.set_gate(Gate.STOPPED, reason="b", expected_version=None)
    assert svc.current().gate is Gate.STOPPED


# ---------------------------------------------------------------- 模型 3 的闸

async def test_gate_cannot_be_opened_while_model3_is_active(svc, ledger):
    await svc.load()
    await svc.set_active_model(Model.M3, expected_version=svc.current().version, operator="alice", op_id="op-3")
    before = svc.current()
    with pytest.raises(ValueError, match="模型 3 本期只预留"):
        await svc.set_gate(Gate.RUNNING, reason="想启动模型 3")
    assert svc.current() == before and (await stored(ledger)).gate is Gate.STOPPED
    # 关闸始终允许
    assert (await svc.set_gate(Gate.STOPPED, reason="停止")).gate is Gate.STOPPED


# ---------------------------------------------------------------- 参数校验


async def test_bad_arguments_are_program_errors(svc):
    await svc.load()
    with pytest.raises(TypeError):
        await svc.set_risk_detail("not a dict")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await svc.set_m2_started("later")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await svc.set_mismatch_since(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await svc.set_run_mode("turbo", reason="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await svc.set_gate("sideways", reason="x")  # type: ignore[arg-type]


# ---------------------------------------------------------------- 同一事务：审计写不进去，状态也不变


async def test_state_and_audit_share_one_transaction(svc, ledger, raw, events):
    await svc.load()
    snap, audits = svc.current(), await audit_count(ledger)
    await events.flush()
    published = len(events.events)
    raw.exec("CREATE TRIGGER audit_boom BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'audit boom'); END")

    with pytest.raises(Exception, match="audit boom"):
        await svc.set_gate(Gate.RUNNING, reason="审计会失败")

    assert svc.current() == snap
    stored_row = await stored(ledger)
    assert stored_row.gate is Gate.STOPPED and stored_row.version == snap.version
    await events.flush()
    assert len(events.events) == published  # 没提交就不发事件
    raw.exec("DROP TRIGGER audit_boom")
    assert await audit_count(ledger) == audits
    # 写线程没有被卡住：去掉故障后同一个调用成功
    assert (await svc.set_gate(Gate.RUNNING, reason="重试")).version == snap.version + 1
