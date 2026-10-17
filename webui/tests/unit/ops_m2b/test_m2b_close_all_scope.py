"""一键全平（F5 第 1、2 步）的范围与预览、入口校验（计划第 1 节的统一步骤）。

覆盖：两档范围（当前模型归属 / 全部托管持仓）；外部与待确认永不包含；模型 1 默认范围含混合仓并单独标出；模型 3 默认范围为空；
预览列出将平掉的与不在范围内的（带原因）；入口校验（不允许、口令、版本、操作号重复、范围取值）；开始时关闸、加批量平仓中锁并取消切换预览。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import (
    Gate,
    Model,
    OperationStatus,
    OperationType,
    OpLock,
    Ownership,
    PositionStatus,
    Suppression,
)
from dmr_kernel.ops.common import OpRejected
from tests.unit.ops_m2b.m2b_kit import LONG, SHORT, build_world, caller, drive


async def mixed_world(ledger, clock, bus):
    """模型 1 生效；两个策略仓（一个是混合仓）、一个接管仓、一个 AI 仓、一个待确认仓、一个外部（已忽略）仓。"""
    w = await build_world(ledger, clock, bus)
    ids = {
        "strategy": w.registry.add("BTCUSDT", LONG, ownership=Ownership.STRATEGY).id,
        "mixed": w.registry.add("ETHUSDT", LONG, ownership=Ownership.STRATEGY, is_mixed=True).id,
        "takeover": w.registry.add("SOLUSDT", SHORT, ownership=Ownership.TAKEOVER).id,
        "ai": w.registry.add("XRPUSDT", LONG, ownership=Ownership.AI).id,
        "pending": w.registry.add("DOGEUSDT", LONG, ownership=Ownership.NONE, status=PositionStatus.PENDING).id,
        "external": w.registry.add("ADAUSDT", SHORT, ownership=Ownership.NONE, status=PositionStatus.IGNORED).id,
    }
    return w, ids


def included_ids(preview: dict) -> set[int]:
    return {rec.id for rec in preview["included"]}


def excluded_reasons(preview: dict) -> dict[int, str]:
    return {item["position"].id: item["reason"] for item in preview["excluded"]}


async def launch(w, op_id="ca-1", scope="all_managed", *, who=None, **overrides):
    args = dict(op_id=op_id, scope=scope, model_version=w.modelstate.current().version)
    args.update(overrides)
    return await w.close_all.start(who if who is not None else caller(w), **args)


async def finished(w, op_id="ca-1"):
    await drive(w.clock, w.close_all.wait(op_id))
    return await w.operations.get(op_id)


# ====================================================================== 两档范围


async def test_model_scope_of_model_1_is_the_strategy_positions_and_the_mixed_one_is_listed_separately(ledger, clock, bus):
    w, ids = await mixed_world(ledger, clock, bus)
    preview = await w.close_all.preview("model")
    assert included_ids(preview) == {ids["strategy"], ids["mixed"]}
    assert [rec.id for rec in preview["mixed_included"]] == [ids["mixed"]]  # 混合仓在默认范围里，并单独标出
    assert preview["scope"] == "model" and preview["model_version"] == w.modelstate.current().version


async def test_model_scope_of_model_2_is_the_takeover_positions_only(ledger, clock, bus):
    w, ids = await mixed_world(ledger, clock, bus)
    await w.modelstate.set_active_model(Model.M2, expected_version=w.modelstate.current().version, operator="t",
                                        op_id="sw")
    preview = await w.close_all.preview("model")
    assert included_ids(preview) == {ids["takeover"]}
    assert preview["mixed_included"] == []
    assert "模型 1" in excluded_reasons(preview)[ids["strategy"]] or "归属" in excluded_reasons(preview)[ids["strategy"]]


async def test_model_scope_of_model_3_is_empty_but_all_managed_is_still_offered(ledger, clock, bus):
    w, ids = await mixed_world(ledger, clock, bus)
    await w.modelstate.set_active_model(Model.M3, expected_version=w.modelstate.current().version, operator="t",
                                        op_id="sw")
    default = await w.close_all.preview("model")
    assert default["included"] == [] and default["mixed_included"] == []
    assert all("模型 3" in reason for pid, reason in excluded_reasons(default).items()
               if pid in (ids["strategy"], ids["mixed"], ids["takeover"], ids["ai"]))
    wide = await w.close_all.preview("all_managed")
    assert included_ids(wide) == {ids["strategy"], ids["mixed"], ids["takeover"], ids["ai"]}


async def test_all_managed_scope_includes_every_registered_position_of_any_ownership(ledger, clock, bus):
    w, ids = await mixed_world(ledger, clock, bus)
    closing = w.registry.add("LTCUSDT", LONG, status=PositionStatus.CLOSING).id  # 部分平仓在途：仍是托管仓
    taking = w.registry.add("DOTUSDT", SHORT, status=PositionStatus.TAKING_OVER, ownership=Ownership.TAKEOVER).id
    preview = await w.close_all.preview("all_managed")
    assert included_ids(preview) == {ids["strategy"], ids["mixed"], ids["takeover"], ids["ai"], closing, taking}
    assert [rec.id for rec in preview["mixed_included"]] == [ids["mixed"]]


@pytest.mark.parametrize("scope", ["model", "all_managed"])
async def test_external_and_pending_positions_are_never_included_whatever_the_scope(ledger, clock, bus, scope):
    w, ids = await mixed_world(ledger, clock, bus)
    preview = await w.close_all.preview(scope)
    assert ids["pending"] not in included_ids(preview) and ids["external"] not in included_ids(preview)
    reasons = excluded_reasons(preview)
    assert "待确认" in reasons[ids["pending"]] and "永不" in reasons[ids["pending"]]
    assert "外部" in reasons[ids["external"]] and "永不" in reasons[ids["external"]]


async def test_preview_lists_what_is_in_and_what_is_out_with_a_reason_for_each_exclusion(ledger, clock, bus):
    w, ids = await mixed_world(ledger, clock, bus)
    preview = await w.close_all.preview("model")
    every = included_ids(preview) | set(excluded_reasons(preview))
    assert every == set(ids.values())  # 一个不漏，也不重复
    assert included_ids(preview).isdisjoint(excluded_reasons(preview))
    assert all(reason for reason in excluded_reasons(preview).values())


async def test_closed_positions_are_not_listed_at_all(ledger, clock, bus):
    w, ids = await mixed_world(ledger, clock, bus)
    w.registry.mark_flat(ids["strategy"])
    preview = await w.close_all.preview("all_managed")
    assert ids["strategy"] not in included_ids(preview) | set(excluded_reasons(preview))


async def test_an_unknown_scope_is_refused_as_invalid(ledger, clock, bus):
    w, _ = await mixed_world(ledger, clock, bus)
    with pytest.raises(OpRejected) as caught:
        await w.close_all.preview("everything")
    assert caught.value.code == "invalid"
    with pytest.raises(OpRejected) as caught:
        await launch(w, scope="everything")
    assert caught.value.code == "invalid"
    assert await w.operations.get("ca-1") is None  # 校验不过不登记操作


# ====================================================================== 入口校验（统一步骤）


@pytest.mark.parametrize("lock, expected", [
    (OpLock.SWITCHING, "切换"),
    (OpLock.BULK_CLOSING, "批量平仓"),
])
async def test_close_all_is_refused_with_the_table_reason_while_switching_or_already_closing(ledger, clock, bus, lock,
                                                                                            expected):
    w, _ = await mixed_world(ledger, clock, bus)
    await w.modelstate.acquire_op_lock(lock, owner="someone")
    with pytest.raises(OpRejected) as caught:
        await launch(w)
    assert caught.value.code == "not_allowed" and expected in caught.value.message
    with pytest.raises(OpRejected) as caught:
        await w.close_all.preview("model")
    assert caught.value.code == "not_allowed"
    assert w.coordinator.submitted == []


@pytest.mark.parametrize("suppression", [Suppression.RECONCILE_UNKNOWN, Suppression.RECOVERING])
async def test_close_all_is_still_allowed_with_a_suppression_reason(ledger, clock, bus, suppression):
    """表：有抑制原因时一键全平「可，须先取到即时持仓读数」——读数由逐仓的即时读取保证（读不到记「未知」，不当零）。"""
    w, ids = await mixed_world(ledger, clock, bus)
    await w.modelstate.add_suppression(suppression, reason="测试")
    await launch(w)
    record = await finished(w)
    assert record.status in (OperationStatus.SUCCEEDED, OperationStatus.PARTIAL)


async def test_a_password_that_was_not_checked_is_refused_before_anything_changes(ledger, clock, bus):
    w, _ = await mixed_world(ledger, clock, bus)
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    before = w.modelstate.current()
    with pytest.raises(OpRejected) as caught:
        await launch(w, model_version=before.version, who=caller(w, checked=False))
    assert caught.value.code == "reauth_required"
    after = w.modelstate.current()
    assert after.gate is Gate.RUNNING and after.version == before.version and not after.op_locks
    assert await w.operations.get("ca-1") is None


async def test_a_stale_model_version_is_refused_and_nothing_changes(ledger, clock, bus):
    w, _ = await mixed_world(ledger, clock, bus)
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    stale = w.modelstate.current().version
    await w.modelstate.add_suppression(Suppression.CLOCK_SKEW, reason="别处改了状态")
    await w.modelstate.remove_suppression(Suppression.CLOCK_SKEW, reason="恢复")
    with pytest.raises(OpRejected) as caught:
        await launch(w, model_version=stale)
    assert caught.value.code == "stale_version"
    state = w.modelstate.current()
    assert state.gate is Gate.RUNNING and not state.op_locks
    assert w.coordinator.submitted == []


async def test_an_operation_id_already_used_by_another_kind_of_operation_is_refused(ledger, clock, bus):
    w, _ = await mixed_world(ledger, clock, bus)
    await w.operations.begin("shared-id", OperationType.CONFIG, operator="alice", session_id=7)
    with pytest.raises(OpRejected) as caught:
        await launch(w, op_id="shared-id")
    assert caught.value.code == "invalid"
    assert w.modelstate.current().op_locks == frozenset()


async def test_a_malformed_operation_id_is_refused_as_invalid(ledger, clock, bus):
    w, _ = await mixed_world(ledger, clock, bus)
    with pytest.raises(OpRejected) as caught:
        await launch(w, op_id="带空格 的")
    assert caught.value.code == "invalid"


# ====================================================================== 开始时的状态变化


async def test_start_closes_the_gate_and_takes_the_bulk_lock_before_any_order_goes_out(ledger, clock, bus):
    w, ids = await mixed_world(ledger, clock, bus)
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    w.coordinator.behave("BTCUSDT", LONG, "stuck")
    w.coordinator.add_position("BTCUSDT", LONG)
    record, is_new = await launch(w, scope="model")
    assert is_new and record.status is OperationStatus.RUNNING and record.op_type is OperationType.CLOSE_ALL
    state = w.modelstate.current()
    assert state.gate is Gate.STOPPED  # F5 第 3 步：关上开仓闸
    assert state.op_locks == frozenset({OpLock.BULK_CLOSING})  # 加「批量平仓中」操作锁
    assert record.scope == "model" and record.operator == "alice" and record.session_id == 7
    await w.close_all.abort(op_id="ab-1", target_op_id="ca-1", operator="alice", session_id=7)


async def test_start_cancels_a_switch_preview_in_progress(ledger, clock, bus):
    w, _ = await mixed_world(ledger, clock, bus)
    await w.modelstate.acquire_op_lock(OpLock.SWITCH_PREVIEW, owner="pv", expires_ms=clock.now_ms() + 60_000)
    await launch(w)
    await finished(w)
    # 拿批量平仓中锁时预览随之取消（表：切换预览中 → 一键全平「可，预览随之取消」）
    assert OpLock.SWITCH_PREVIEW not in w.modelstate.current().op_locks


async def test_the_gate_stays_stopped_after_it_finishes_and_the_lock_is_gone(ledger, clock, bus):
    w, _ = await mixed_world(ledger, clock, bus)
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    await launch(w)
    record = await finished(w)
    state = w.modelstate.current()
    assert state.gate is Gate.STOPPED and state.op_locks == frozenset()  # F5 第 12 步：结束后保持已停止
    assert record.status is not OperationStatus.RUNNING


async def test_a_second_submission_of_the_same_operation_id_returns_the_first_record_and_runs_nothing_again(
        ledger, clock, bus):
    w, _ = await mixed_world(ledger, clock, bus)
    for rec in w.registry.registered():
        w.coordinator.add_position(rec.symbol, rec.side)
    first, is_new = await launch(w)
    again, is_new_again = await launch(w, model_version=0)  # 版本、口令都不再看：直接返回第一次的记录
    assert is_new and not is_new_again and again.id == first.id
    await finished(w)
    sent = len(w.coordinator.submitted)
    third, is_new_third = await launch(w)
    assert not is_new_third and third.status is not OperationStatus.RUNNING  # 结束之后重复提交：返回第一次的结果
    assert len(w.coordinator.submitted) == sent


async def test_an_empty_scope_finishes_at_once_as_succeeded_with_the_gate_closed(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    await launch(w)
    record = await finished(w)
    assert record.status is OperationStatus.SUCCEEDED and record.result["per_position"] == []
    state = w.modelstate.current()
    assert state.gate is Gate.STOPPED and not state.op_locks


async def test_a_session_without_the_seat_is_refused_and_the_command_line_needs_none(ledger, clock, bus):
    w, _ = await mixed_world(ledger, clock, bus)
    with pytest.raises(OpRejected) as caught:
        await launch(w, who=caller(w, seat=False))
    assert caught.value.code == "seat_required"
    assert await w.operations.get("ca-1") is None and w.modelstate.current().op_locks == frozenset()
    record, is_new = await launch(w, who=caller(w, session_id=None))  # 本机令牌：不占席位、免口令
    assert is_new and record.operator == "cli" and record.session_id is None


async def test_a_password_in_the_request_is_verified_on_the_spot_and_a_wrong_one_changes_nothing(ledger, clock, bus):
    w, _ = await mixed_world(ledger, clock, bus)
    checked: list[str] = []

    async def verify(password: str) -> None:
        checked.append(password)
        if password != "right":
            raise OpRejected(401, "bad_credentials", "口令错误")

    stale_session = caller(w, checked=False)  # 会话上最近没有校验过口令
    with pytest.raises(OpRejected) as caught:
        await launch(w, who=stale_session, password="wrong", verify=verify)
    assert caught.value.code == "bad_credentials" and checked == ["wrong"]
    assert await w.operations.get("ca-1") is None and w.modelstate.current().op_locks == frozenset()
    record, is_new = await launch(w, who=stale_session, password="right", verify=verify)
    assert is_new and checked == ["wrong", "right"]
    assert "right" not in repr(record)  # 口令不进操作记录
