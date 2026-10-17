"""ops.operations 与 ledger.repo_operations：通用的操作记录（同一操作号只执行一次）。

覆盖：begin 的字段与初始状态、重复操作号返回第一次的记录且不产生任何副作用（含并发）、finish 只能结束一次、
get、每次 begin/finish 写审计（重复提交不写）、事件、口令与令牌不进操作记录、参数校验。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from dmr_kernel.core.events import OperationProgress
from dmr_kernel.core.types import OperationStatus, OperationType
from dmr_kernel.ledger import repo_audit
from dmr_kernel.ledger.records import OperationRecord
from dmr_kernel.ops.operations import OP_ID_PATTERN, OperationFinished, OperationNotFound, Operations
from tests.support.ledger import START_MS

D = Decimal
CONFIG = OperationType.CONFIG
TERMINAL = [s for s in OperationStatus if s not in (OperationStatus.ACCEPTED, OperationStatus.RUNNING)]


def make_ops(ledger, clock, bus=None) -> Operations:
    return Operations(ledger, clock, bus)


async def begin(ops: Operations, op_id: str = "op-1", op_type=CONFIG, **overrides):
    fields = dict(operator="alice", session_id=7, scope="leverage", state_version=None,
                  params={"key": "leverage", "value": 3})
    fields.update(overrides)
    return await ops.begin(op_id, op_type, **fields)


async def audit_rows(ledger):
    """从旧到新。"""
    return list(reversed(await ledger.read(repo_audit.recent)))


# ====================================================================== begin


async def test_begin_creates_a_running_record_with_everything_the_caller_gave(ledger, clock, raw):
    ops = make_ops(ledger, clock)
    record, is_new = await begin(ops, state_version=12)
    assert is_new is True
    assert isinstance(record, OperationRecord)
    assert record.op_id == "op-1" and record.op_type is CONFIG
    assert record.operator == "alice" and record.session_id == 7
    assert record.scope == "leverage" and record.state_version == 12
    assert record.params == {"key": "leverage", "value": 3}
    assert record.status is OperationStatus.RUNNING
    assert record.started_ms == START_MS and record.ended_ms is None
    assert record.result is None and record.per_position is None
    # 落盘的就是这一条
    assert raw.query("SELECT op_id, op_type, status, operator, session_id, scope, state_version FROM operation") == [
        ("op-1", "config", "running", "alice", 7, "leverage", 12)]


async def test_begin_defaults_scope_state_version_and_params_to_nothing(ledger, clock):
    record, _ = await make_ops(ledger, clock).begin("op-1", OperationType.STOP, operator="cli", session_id=None)
    assert record.scope is None and record.state_version is None and record.params == {}
    assert record.session_id is None  # 本机令牌没有会话


async def test_begin_takes_the_start_time_from_the_clock(ledger, clock):
    clock.advance(5_000)
    record, _ = await begin(make_ops(ledger, clock))
    assert record.started_ms == START_MS + 5_000


async def test_begin_accepts_the_operation_type_as_its_text_value(ledger, clock):
    record, _ = await begin(make_ops(ledger, clock), op_type="close_all")
    assert record.op_type is OperationType.CLOSE_ALL


async def test_begin_stores_decimals_in_params_as_strings(ledger, clock):
    record, _ = await begin(make_ops(ledger, clock), params={"qty": D("0.50"), "note": "平掉此仓"})
    assert record.params == {"qty": "0.50", "note": "平掉此仓"}


# ---------------------------------------------------------------- 重复操作号


async def test_a_repeated_op_id_returns_the_first_record_and_changes_nothing(ledger, clock, raw):
    ops = make_ops(ledger, clock)
    first, _ = await begin(ops)
    audit_before = await audit_rows(ledger)
    clock.advance(10_000)
    again, is_new = await begin(ops, op_type=OperationType.STOP, operator="mallory", session_id=99, scope="x",
                                state_version=5, params={"key": "other"})
    assert is_new is False
    assert again == first  # 第一次的记录，一个字段都不是第二次提交的
    assert raw.query("SELECT COUNT(*) FROM operation") == [(1,)]
    assert await audit_rows(ledger) == audit_before  # 重复提交不写审计


async def test_a_repeated_op_id_after_finish_returns_the_finished_record(ledger, clock):
    ops = make_ops(ledger, clock)
    await begin(ops)
    clock.advance(1_000)
    done = await ops.finish("op-1", OperationStatus.SUCCEEDED, result={"old": 2, "new": 3})
    again, is_new = await begin(ops)
    assert is_new is False
    assert again == done and again.status is OperationStatus.SUCCEEDED and again.result == {"old": 2, "new": 3}


async def test_different_op_ids_are_independent_operations(ledger, clock, raw):
    ops = make_ops(ledger, clock)
    (a, new_a), (b, new_b) = await begin(ops, "op-a"), await begin(ops, "op-b", scope="margin_mode")
    assert new_a and new_b and a.id != b.id
    assert raw.query("SELECT op_id FROM operation ORDER BY id") == [("op-a",), ("op-b",)]


async def test_concurrent_submissions_of_one_op_id_create_exactly_one_operation(ledger, clock, raw):
    ops = make_ops(ledger, clock)
    outcomes = await asyncio.gather(*(begin(ops, "same", operator=f"user{i}", session_id=i) for i in range(12)))
    assert sorted(is_new for _, is_new in outcomes) == [False] * 11 + [True]
    assert len({record.id for record, _ in outcomes}) == 1  # 全部拿到同一条
    winner = next(record for record, is_new in outcomes if is_new)
    assert all(record == winner for record, _ in outcomes)
    assert raw.query("SELECT COUNT(*) FROM operation") == [(1,)]
    assert len([row for row in await audit_rows(ledger) if row.action == "operation.begin"]) == 1


# ====================================================================== get


async def test_get_returns_none_for_an_unknown_op_id(ledger, clock):
    assert await make_ops(ledger, clock).get("nope") is None


async def test_get_returns_the_current_record(ledger, clock):
    ops = make_ops(ledger, clock)
    created, _ = await begin(ops)
    assert await ops.get("op-1") == created
    done = await ops.finish("op-1", OperationStatus.SUCCEEDED, result={"ok": True})
    assert await ops.get("op-1") == done


# ====================================================================== finish


@pytest.mark.parametrize("status", TERMINAL)
async def test_finish_records_status_result_and_end_time(ledger, clock, raw, status):
    ops = make_ops(ledger, clock)
    await begin(ops)
    clock.advance(2_500)
    record = await ops.finish("op-1", status, result={"message": "做完了", "n": 3})
    assert record.status is status and record.result == {"message": "做完了", "n": 3}
    assert record.started_ms == START_MS and record.ended_ms == START_MS + 2_500
    assert record.per_position is None
    assert raw.query("SELECT status, ended_ms FROM operation") == [(status.value, START_MS + 2_500)]


async def test_finish_stores_the_per_position_results(ledger, clock):
    ops = make_ops(ledger, clock)
    await begin(ops, op_type=OperationType.CLOSE_ALL, scope="managed")
    per_position = [{"position_id": 1, "symbol": "BTCUSDT", "side": "LONG", "result": "closed"},
                    {"position_id": 2, "symbol": "ETHUSDT", "side": "SHORT", "result": "failed", "reason": "超时"}]
    record = await ops.finish("op-1", OperationStatus.PARTIAL, result={"closed": 1, "failed": 1},
                              per_position=per_position)
    assert record.per_position == per_position and record.status is OperationStatus.PARTIAL
    assert (await ops.get("op-1")).per_position == per_position


async def test_an_operation_may_begin_as_accepted_and_still_be_finished(ledger, clock):
    ops = make_ops(ledger, clock)
    record, _ = await begin(ops, status=OperationStatus.ACCEPTED)
    assert record.status is OperationStatus.ACCEPTED
    assert (await audit_rows(ledger))[0].result == "accepted"
    finished = await ops.finish("op-1", OperationStatus.ABORTED, result={"message": "用户中止"})
    assert finished.status is OperationStatus.ABORTED


@pytest.mark.parametrize("status", [OperationStatus.SUCCEEDED, OperationStatus.FAILED, "nonsense"])
async def test_an_operation_cannot_begin_in_a_final_state(ledger, clock, raw, status):
    with pytest.raises(ValueError, match="进行中"):
        await begin(make_ops(ledger, clock), status=status)
    assert raw.query("SELECT COUNT(*) FROM operation") == [(0,)]


async def test_finish_of_an_unknown_operation_is_an_error_and_writes_nothing(ledger, clock):
    ops = make_ops(ledger, clock)
    with pytest.raises(OperationNotFound):
        await ops.finish("ghost", OperationStatus.SUCCEEDED, result={})
    assert await audit_rows(ledger) == []


async def test_an_operation_can_only_be_finished_once(ledger, clock):
    ops = make_ops(ledger, clock)
    await begin(ops)
    clock.advance(1_000)
    first = await ops.finish("op-1", OperationStatus.SUCCEEDED, result={"n": 1})
    audit_before = await audit_rows(ledger)
    clock.advance(1_000)
    for status in (OperationStatus.SUCCEEDED, OperationStatus.FAILED):
        with pytest.raises(OperationFinished) as caught:
            await ops.finish("op-1", status, result={"n": 2}, per_position=[{"x": 1}])
        assert caught.value.record == first
    assert await ops.get("op-1") == first  # 一个字段都没被改
    assert await audit_rows(ledger) == audit_before


@pytest.mark.parametrize("status", [OperationStatus.ACCEPTED, OperationStatus.RUNNING])
async def test_finish_needs_a_final_status(ledger, clock, status):
    ops = make_ops(ledger, clock)
    await begin(ops)
    with pytest.raises(ValueError, match="终态"):
        await ops.finish("op-1", status, result={})
    assert (await ops.get("op-1")).status is OperationStatus.RUNNING


async def test_finish_accepts_the_status_as_its_text_value(ledger, clock):
    ops = make_ops(ledger, clock)
    await begin(ops)
    assert (await ops.finish("op-1", "rejected", result={"message": "不行"})).status is OperationStatus.REJECTED


# ====================================================================== 审计


async def test_begin_and_finish_each_write_one_audit_row(ledger, clock):
    ops = make_ops(ledger, clock)
    await begin(ops, state_version=4)
    clock.advance(3_000)
    await ops.finish("op-1", OperationStatus.SUCCEEDED, result={"old": 2, "new": 3})
    rows = await audit_rows(ledger)
    assert [r.action for r in rows] == ["operation.begin", "operation.finish"]
    started, ended = rows
    assert (started.operator, started.target, started.at_ms) == ("alice", "op-1", START_MS)
    assert started.before is None
    assert started.after == {"op_type": "config", "scope": "leverage", "state_version": 4,
                             "params": {"key": "leverage", "value": 3}}
    assert started.result == "running"
    assert (ended.operator, ended.target, ended.at_ms) == ("alice", "op-1", START_MS + 3_000)
    assert ended.before == {"status": "running"}
    assert ended.after == {"status": "succeeded", "result": {"old": 2, "new": 3}}
    assert ended.result == "succeeded"


async def test_the_finish_audit_carries_the_per_position_results(ledger, clock):
    ops = make_ops(ledger, clock)
    await begin(ops, op_type=OperationType.CLOSE_ALL)
    per_position = [{"position_id": 1, "result": "closed"}]
    await ops.finish("op-1", OperationStatus.SUCCEEDED, result={}, per_position=per_position)
    ended = (await audit_rows(ledger))[-1]
    assert ended.after == {"status": "succeeded", "result": {}, "per_position": per_position}


async def test_the_audit_names_the_operator_even_without_a_session(ledger, clock):
    ops = make_ops(ledger, clock)
    await begin(ops, operator="cli", session_id=None)
    await ops.finish("op-1", OperationStatus.FAILED, result={"message": "超时"})
    assert {r.operator for r in await audit_rows(ledger)} == {"cli"}


async def test_a_failed_begin_or_finish_leaves_neither_a_row_nor_an_audit_entry(ledger, clock, raw):
    ops = make_ops(ledger, clock)
    with pytest.raises(Exception, match="JSON"):  # 集合不是 JSON：写事务整体回滚
        await begin(ops, params={"bad": {1, 2}})
    assert raw.query("SELECT COUNT(*) FROM operation") == [(0,)]
    assert await audit_rows(ledger) == []
    await begin(ops)
    with pytest.raises(Exception, match="JSON"):
        await ops.finish("op-1", OperationStatus.SUCCEEDED, result={"bad": object()})
    assert (await ops.get("op-1")).status is OperationStatus.RUNNING  # 结束没成功：仍是进行中
    assert [r.action for r in await audit_rows(ledger)] == ["operation.begin"]


# ====================================================================== 口令与令牌不进记录


async def test_passwords_and_tokens_never_reach_the_operation_row_or_the_audit_log(ledger, clock, raw):
    ops = make_ops(ledger, clock)
    record, _ = await begin(ops, op_type=OperationType.CLOSE_ALL, params={
        "scope": "managed", "password": "hunter2-secret", "nested": {"Token": "tok-123", "ok": 1},
        "list": [{"new_password": "pw-xyz", "keep": "yes"}], "api_secret": "s3cr3t-api-value"})
    assert record.params == {"scope": "managed", "password": "***", "nested": {"Token": "***", "ok": 1},
                             "list": [{"new_password": "***", "keep": "yes"}], "api_secret": "***"}
    await ops.finish("op-1", OperationStatus.SUCCEEDED, result={"password": "again-secret", "closed": 2})
    dump = repr(raw.query("SELECT * FROM operation")) + repr(raw.query("SELECT * FROM audit_log"))
    for secret in ("hunter2-secret", "tok-123", "pw-xyz", "s3cr3t-api-value", "again-secret"):
        assert secret not in dump
    assert (await ops.get("op-1")).result == {"password": "***", "closed": 2}


# ====================================================================== 事件


async def test_progress_events_are_published_for_a_new_operation_and_its_end_only(ledger, clock, bus, event_log):
    log = event_log(OperationProgress)
    ops = make_ops(ledger, clock, bus)
    await begin(ops)
    await begin(ops)  # 重复提交：不发
    clock.advance(1_000)
    await ops.finish("op-1", OperationStatus.REJECTED, result={"message": "不行"})
    events = await log.settle()
    assert [(e.op_id, e.op_type, e.status, e.at_ms) for e in events] == [
        ("op-1", "config", "running", START_MS), ("op-1", "config", "rejected", START_MS + 1_000)]


async def test_operations_work_without_an_event_bus(ledger, clock):
    ops = make_ops(ledger, clock)  # bus=None
    await begin(ops)
    assert (await ops.finish("op-1", OperationStatus.SUCCEEDED, result={})).status is OperationStatus.SUCCEEDED


# ====================================================================== 参数校验


@pytest.mark.parametrize("op_id", ["", " ", "a b", "x" * 65, "带中文", "line\nbreak", "semi;colon", None, 5])
async def test_a_malformed_op_id_is_refused_and_nothing_is_written(ledger, clock, raw, op_id):
    with pytest.raises(ValueError, match="操作号"):
        await begin(make_ops(ledger, clock), op_id)
    assert raw.query("SELECT COUNT(*) FROM operation") == [(0,)]
    assert await audit_rows(ledger) == []


@pytest.mark.parametrize("op_id", ["a", "op-1", "7f3c9a1e-5b2d-4e8a-9c1f-0d2b3a4c5e6f", "cli:close_all.1", "X" * 64])
async def test_ordinary_client_generated_op_ids_are_accepted(ledger, clock, op_id):
    assert OP_ID_PATTERN.fullmatch(op_id)
    _, is_new = await begin(make_ops(ledger, clock), op_id)
    assert is_new


@pytest.mark.parametrize("operator", ["", "  ", None, 3])
async def test_an_operation_must_name_who_did_it(ledger, clock, operator):
    with pytest.raises(ValueError, match="操作人"):
        await begin(make_ops(ledger, clock), operator=operator)


@pytest.mark.parametrize("session_id", [True, "7", 1.5])
async def test_the_session_id_is_an_int_or_none(ledger, clock, session_id):
    with pytest.raises(ValueError, match="会话"):
        await begin(make_ops(ledger, clock), session_id=session_id)


async def test_an_unknown_operation_type_is_refused(ledger, clock):
    with pytest.raises(ValueError):
        await begin(make_ops(ledger, clock), op_type="launch_missiles")


async def test_params_must_be_a_mapping(ledger, clock):
    with pytest.raises(ValueError, match="params"):
        await begin(make_ops(ledger, clock), params=[1, 2])
