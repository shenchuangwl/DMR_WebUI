"""ops.common：统一步骤（允许表校验、席位、重新输入口令、状态版本、操作号去重）的公共函数，M2B 复用。

覆盖：错误类型的形状；``require_allowed`` 逐格（报告 4.6 与这几个操作有关的格）；席位；重新输入口令（窗口内、窗口外、当场校验、
校验失败、本机令牌免）；两种状态版本；``admit`` 的顺序（重复的操作号先于一切校验返回第一次的记录）；操作号被另一种操作占用；
意图结果到操作结果的映射。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.modelstate import ModelStateSnapshot, allowed_operations
from dmr_kernel.core.types import (
    Gate,
    IntentState,
    Model,
    OpLock,
    OperationStatus,
    OperationType,
    Suppression,
)
from dmr_kernel.ops import common
from dmr_kernel.ops.common import Caller, OpRejected
from tests.unit.ops_m2a.m2a_kit import make_deps, rig

D = Decimal
SESSION = Caller(operator="alice", session_id=7, is_session=True, holds_seat=True, reauth_ms=None)


def snap(**kw) -> ModelStateSnapshot:
    return ModelStateSnapshot(**kw)


# ---------------------------------------------------------------- 错误类型


def test_op_rejected_carries_status_code_message_and_extra():
    error = OpRejected(409, "not_allowed", "不行", checks=[1])
    assert (error.status, error.code, error.message) == (409, "not_allowed", "不行")
    assert error.body() == {"code": "not_allowed", "message": "不行", "checks": [1]}


@pytest.mark.parametrize("factory,status,code", [
    (common.not_allowed, 409, "not_allowed"), (common.stale_version, 409, "stale_version"),
    (common.seat_required, 409, "seat_required"), (common.reauth_required, 403, "reauth_required"),
    (common.invalid, 422, "invalid"), (common.not_found, 404, "not_found"),
    (common.unavailable, 503, "unavailable"), (common.not_implemented, 501, "not_implemented")])
def test_error_factories_use_the_documented_status_and_code(factory, status, code):
    error = factory("原因")
    assert (error.status, error.code, error.message) == (status, code, "原因")


def test_caller_helpers():
    assert common.Caller.cli().is_session is False and common.Caller.cli().operator == "cli"
    assert SESSION.is_session and SESSION.session_id == 7


# ---------------------------------------------------------------- 允许表（报告 4.6 逐格）

CELLS = {
    # 当前情况: (状态, {操作: 是否允许})
    "已停止，无抑制": (snap(), dict(start=True, stop=False, partial_close=True, close_one=True)),
    "运行中，无抑制": (snap(gate=Gate.RUNNING), dict(start=False, stop=True, partial_close=True, close_one=True)),
    "切换预览中": (snap(op_locks=frozenset({OpLock.SWITCH_PREVIEW})),
                dict(start=False, stop=True, partial_close=True, close_one=True)),
    "有抑制原因": (snap(suppressions=frozenset({Suppression.RECONCILE_UNKNOWN})),
               dict(start=False, stop=True, partial_close=True, close_one=True)),
    "恢复中": (snap(suppressions=frozenset({Suppression.RECOVERING})),
            dict(start=False, stop=True, partial_close=False, close_one=True)),
    "切换中": (snap(op_locks=frozenset({OpLock.SWITCHING})),
            dict(start=False, stop=False, partial_close=True, close_one=True)),
    "批量平仓中": (snap(op_locks=frozenset({OpLock.BULK_CLOSING})),
               dict(start=False, stop=True, partial_close=True, close_one=True)),
    "模型 3 生效": (snap(active_model=Model.M3), dict(start=False, stop=True, partial_close=True, close_one=True)),
}


@pytest.mark.parametrize("name", list(CELLS))
@pytest.mark.parametrize("op", ["start", "stop", "partial_close", "close_one"])
def test_require_allowed_matches_the_status_table_cell_by_cell(name, op):
    state, expected = CELLS[name]
    permission = allowed_operations(state)[op]
    assert permission.allowed is expected[op]
    if expected[op]:
        assert common.require_allowed(state, op) == permission
    else:
        with pytest.raises(OpRejected) as caught:
            common.require_allowed(state, op)
        assert (caught.value.status, caught.value.code) == (409, "not_allowed")
        assert caught.value.message == permission.reason and permission.reason  # 原因原文


def test_require_allowed_rejects_unknown_keys_as_a_program_error():
    with pytest.raises(KeyError):
        common.require_allowed(snap(), "no_such_operation")


# ---------------------------------------------------------------- 席位


def test_a_session_without_the_seat_is_rejected_and_the_cli_never_needs_one():
    common.require_seat(SESSION)
    common.require_seat(Caller.cli())
    with pytest.raises(OpRejected) as caught:
        common.require_seat(Caller(operator="bob", session_id=8, is_session=True, holds_seat=False))
    assert (caught.value.status, caught.value.code) == (409, "seat_required")


# ---------------------------------------------------------------- 重新输入口令


class Verifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.calls = ok, []

    async def __call__(self, password: str) -> None:
        self.calls.append(password)
        if not self.ok:
            raise OpRejected(401, "bad_credentials", "口令错误")


NOW, WINDOW = 1_000_000, 300


async def test_a_recent_check_inside_the_window_needs_no_password():
    caller = Caller("alice", 7, True, True, reauth_ms=NOW - 299_999)
    verify = Verifier()
    await common.require_reauth(caller, now_ms=NOW, window_s=WINDOW, password=None, verify=verify)
    assert verify.calls == []


async def test_a_check_exactly_at_the_window_edge_has_expired():
    caller = Caller("alice", 7, True, True, reauth_ms=NOW - 300_000)
    with pytest.raises(OpRejected) as caught:
        await common.require_reauth(caller, now_ms=NOW, window_s=WINDOW, password=None, verify=Verifier())
    assert (caught.value.status, caught.value.code) == (403, "reauth_required")


async def test_no_check_at_all_and_no_password_is_reauth_required():
    with pytest.raises(OpRejected) as caught:
        await common.require_reauth(SESSION, now_ms=NOW, window_s=WINDOW, password=None, verify=Verifier())
    assert caught.value.code == "reauth_required"


async def test_a_password_in_the_request_is_checked_on_the_spot():
    verify = Verifier()
    await common.require_reauth(SESSION, now_ms=NOW, window_s=WINDOW, password="secret", verify=verify)
    assert verify.calls == ["secret"]


async def test_a_wrong_password_propagates_the_verifiers_error():
    with pytest.raises(OpRejected) as caught:
        await common.require_reauth(SESSION, now_ms=NOW, window_s=WINDOW, password="nope", verify=Verifier(False))
    assert (caught.value.status, caught.value.code) == (401, "bad_credentials")


async def test_the_local_token_needs_no_password_at_all():
    verify = Verifier(False)
    await common.require_reauth(Caller.cli(), now_ms=NOW, window_s=WINDOW, password=None, verify=verify)
    assert verify.calls == []


# ---------------------------------------------------------------- 两种状态版本


def test_the_model_version_must_match_exactly():
    state = snap(version=12)
    common.require_model_version(state, 12)
    for stale in (11, 13):
        with pytest.raises(OpRejected) as caught:
            common.require_model_version(state, stale)
        assert (caught.value.status, caught.value.code) == (409, "stale_version")


async def test_the_position_version_must_match_exactly(tmp_path):
    async with rig(tmp_path) as r:
        position = await r.open_position()
        common.require_position_version(position, position.version)
        for stale in (position.version - 1, position.version + 1):
            with pytest.raises(OpRejected) as caught:
                common.require_position_version(position, stale)
            assert (caught.value.status, caught.value.code) == (409, "stale_version")


# ---------------------------------------------------------------- admit：顺序与去重


async def admit_start(deps, caller, op_id, *, version=None, **kw):
    state = deps.modelstate.current()
    return await common.admit(deps, caller, op_id=op_id, op_type=OperationType.START, allowed_key="start",
                              model_version=state.version if version is None else version, **kw)


async def test_admit_runs_the_steps_and_registers_the_operation(tmp_path):
    async with rig(tmp_path) as r:
        deps = make_deps(r.kernel)
        admission = await admit_start(deps, SESSION, "adm-1", scope="m1", params={"a": 1})
        assert admission.is_new and admission.record.status is OperationStatus.RUNNING
        assert admission.record.operator == "alice" and admission.record.session_id == 7
        assert admission.record.scope == "m1" and admission.record.state_version == admission.state.version
        assert admission.record.params == {"a": 1}


async def test_admit_checks_allowed_before_seat_before_reauth_before_version(tmp_path):
    async with rig(tmp_path) as r:
        deps = make_deps(r.kernel)
        no_seat = Caller("bob", 8, True, False, None)
        # 全都不满足：先报「不允许」
        await r.kernel.modelstate.set_gate(Gate.RUNNING, reason="t")
        with pytest.raises(OpRejected) as caught:
            await admit_start(deps, no_seat, "adm-2", version=1, reauth=True)
        assert caught.value.code == "not_allowed"
        await r.kernel.modelstate.set_gate(Gate.STOPPED, reason="t")
        # 允许了，没席位
        with pytest.raises(OpRejected) as caught:
            await admit_start(deps, no_seat, "adm-2", version=1, reauth=True)
        assert caught.value.code == "seat_required"
        # 有席位，没口令
        with pytest.raises(OpRejected) as caught:
            await admit_start(deps, SESSION, "adm-2", version=1, reauth=True)
        assert caught.value.code == "reauth_required"
        # 口令过了，版本过期
        with pytest.raises(OpRejected) as caught:
            await admit_start(deps, SESSION, "adm-2", version=1, reauth=True, password="p", verify=Verifier())
        assert caught.value.code == "stale_version"
        # 被拒绝的请求没有占用操作号
        assert await deps.operations.get("adm-2") is None


async def test_a_repeated_operation_id_returns_the_first_record_before_any_validation(tmp_path):
    async with rig(tmp_path) as r:
        deps = make_deps(r.kernel)
        first = await admit_start(deps, SESSION, "dup-1")
        await deps.operations.finish("dup-1", OperationStatus.SUCCEEDED, result={"x": 1})
        await r.kernel.modelstate.set_gate(Gate.RUNNING, reason="t")  # 现在启动已不被允许、版本也变了
        again = await common.admit(deps, SESSION, op_id="dup-1", op_type=OperationType.START, allowed_key="start",
                                   model_version=1)
        assert not again.is_new and again.record.op_id == first.record.op_id
        assert again.record.status is OperationStatus.SUCCEEDED and again.record.result == {"x": 1}


async def test_two_concurrent_submissions_of_one_operation_id_yield_one_new_record(tmp_path):
    import asyncio

    async with rig(tmp_path) as r:
        deps = make_deps(r.kernel)
        version = deps.modelstate.current().version
        results = await asyncio.gather(*[
            common.admit(deps, SESSION, op_id="race-1", op_type=OperationType.START, allowed_key="start",
                         model_version=version) for _ in range(5)])
        assert sorted(a.is_new for a in results) == [False, False, False, False, True]


async def test_an_operation_id_used_by_another_kind_of_operation_is_invalid(tmp_path):
    async with rig(tmp_path) as r:
        deps = make_deps(r.kernel)
        await admit_start(deps, SESSION, "kind-1")
        with pytest.raises(OpRejected) as caught:
            await common.admit(deps, SESSION, op_id="kind-1", op_type=OperationType.STOP, allowed_key="stop",
                               model_version=1)
        assert (caught.value.status, caught.value.code) == (422, "invalid")


async def test_a_bad_operation_id_is_invalid(tmp_path):
    async with rig(tmp_path) as r:
        deps = make_deps(r.kernel)
        with pytest.raises(OpRejected) as caught:
            await admit_start(deps, SESSION, "bad id!")
        assert caught.value.code == "invalid"


async def test_admit_with_a_position_checks_existence_and_the_position_version(tmp_path):
    async with rig(tmp_path) as r:
        deps = make_deps(r.kernel)
        position = await r.open_position()

        async def go(op_id, position_id, version):
            return await common.admit(deps, SESSION, op_id=op_id, op_type=OperationType.CLOSE_ONE,
                                      allowed_key="close_one", position_id=position_id, position_version=version)

        with pytest.raises(OpRejected) as caught:
            await go("pos-1", 9999, 1)
        assert (caught.value.status, caught.value.code) == (404, "not_found")
        with pytest.raises(OpRejected) as caught:
            await go("pos-2", position.id, position.version - 1)
        assert caught.value.code == "stale_version"
        admission = await go("pos-3", position.id, position.version)
        assert admission.position.id == position.id and admission.is_new


async def test_a_precheck_runs_after_the_versions_and_before_the_operation_is_registered(tmp_path):
    async with rig(tmp_path) as r:
        deps = make_deps(r.kernel)

        async def refuse(state, position):
            raise common.not_allowed("预检不过")

        with pytest.raises(OpRejected) as caught:
            await common.admit(deps, SESSION, op_id="pre-1", op_type=OperationType.START, allowed_key="start",
                               precheck=refuse)
        assert caught.value.message == "预检不过"
        assert await deps.operations.get("pre-1") is None


# ---------------------------------------------------------------- 响应与结果映射


async def test_response_of_has_the_documented_shape(tmp_path):
    async with rig(tmp_path) as r:
        deps = make_deps(r.kernel)
        admission = await admit_start(deps, SESSION, "resp-1")
        assert common.response_of(admission.record) == {"op_id": "resp-1", "status": "running", "result": {}}
        record = await deps.operations.finish("resp-1", OperationStatus.SUCCEEDED, result={"gate": "running"})
        assert common.response_of(record) == {"op_id": "resp-1", "status": "succeeded", "result": {"gate": "running"}}


@pytest.mark.parametrize("state,filled,outcome,status", [
    (IntentState.COMPLETED, D("0.5"), "closed", OperationStatus.SUCCEEDED),
    (IntentState.COMPLETED, D("0"), "closed_externally", OperationStatus.SUCCEEDED),
    (IntentState.REJECTED, D("0"), "failed", OperationStatus.FAILED),
    (IntentState.EXPIRED, D("0"), "failed", OperationStatus.FAILED),
    (IntentState.CANCELLED, D("0"), "failed", OperationStatus.FAILED),
    (IntentState.RECONCILING, D("0"), "unknown", OperationStatus.UNKNOWN),
    (IntentState.ACTIVE, D("0"), "unknown", OperationStatus.UNKNOWN)])
def test_close_outcome_maps_the_intent_state_to_an_outcome_and_an_operation_status(state, filled, outcome, status):
    assert common.close_outcome(state, filled) == (outcome, status)


def test_decimals_in_results_are_rendered_as_strings():
    assert common.jsonable_result({"a": D("1.50"), "b": [D("2")], "c": {"d": D("0.001")}}) == {
        "a": "1.50", "b": ["2"], "c": {"d": "0.001"}}

