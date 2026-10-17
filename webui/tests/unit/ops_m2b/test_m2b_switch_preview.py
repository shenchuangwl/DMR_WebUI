"""模型切换的预览（F6 第 1 步）：切换预览中的锁、有效期、取消、到期、席位变化、一键全平随之取消。

席位本身由路由层检查（见 test_m2b_routes.py）；这里验证内核侧：预览属于发起它的会话，本机令牌（无会话）不能切换。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.api.common import SeatChanged
from dmr_kernel.core.types import (
    Gate,
    Model,
    OperationStatus,
    OperationType,
    OpLock,
    OrderPurpose,
    OrderState,
    Ownership,
    PositionStatus,
    Suppression,
)
from dmr_kernel.ops.common import OpRejected
from tests.unit.ops_m2b.m2b_kit import LONG, SHORT, FakeOrder, build_world, caller
from tests.unit.ops_m2b.test_m2b_close_all_scope import finished, launch

D = Decimal


async def preview(w, op_id="pv-1", target="m2", session_id=7, operator="alice"):
    return await w.switch.preview(caller(w, operator=operator, session_id=session_id), op_id=op_id, target=target)


async def sw_confirm(w, *, op_id="cf-1", model_version, mode="keep", session_id=7, operator="alice", checked=True):
    return await w.switch.confirm(caller(w, operator=operator, session_id=session_id, checked=checked), op_id=op_id,
                                  model_version=model_version, mode=mode)


async def sw_cancel(w, op_id="cx-1", session_id=7, operator="alice"):
    return await w.switch.cancel(caller(w, operator=operator, session_id=session_id), op_id=op_id)


async def refused(awaitable, code: str) -> OpRejected:
    with pytest.raises(OpRejected) as caught:
        await awaitable
    assert caught.value.code == code, (caught.value.code, caught.value.message)
    return caught.value


# ====================================================================== 预览的内容


async def test_preview_returns_the_contract_fields_and_takes_the_preview_lock_with_an_expiry(ledger, clock, bus):
    w = await build_world(ledger, clock, bus, switch_preview_ttl_s=60)
    w.registry.add("BTCUSDT", LONG, ownership=Ownership.STRATEGY)
    w.registry.add("ETHUSDT", SHORT, ownership=Ownership.STRATEGY)
    w.registry.add("SOLUSDT", LONG, ownership=Ownership.TAKEOVER)
    w.registry.add("DOGEUSDT", LONG, ownership=Ownership.NONE, status=PositionStatus.PENDING)
    w.orders.rows.append(FakeOrder("DK1-1-5-o", OrderState.ACKNOWLEDGED, "BTCUSDT", LONG, OrderPurpose.OPEN, D("2")))
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    result = await preview(w)
    state = w.modelstate.current()
    assert result["target"] == "m2"
    assert result["model_version"] == state.version  # 拿预览锁之后的版本：确认时拿它来核对
    assert result["current"]["active_model"] == "m1" and result["current"]["gate"] == "running"
    assert result["current"]["suppressions"] == [] and result["current"]["op_locks"] == ["switch_preview"]
    assert result["counts_by_ownership"] == {"strategy": 2, "takeover": 1, "ai": 0, "none": 1}
    assert result["expires_ms"] == clock.now_ms() + 60_000
    (order,) = result["open_orders"]
    assert order["client_order_id"] == "DK1-1-5-o" and order["symbol"] == "BTCUSDT" and order["state"] == "ACKNOWLEDGED"
    assert OpLock.SWITCH_PREVIEW in state.op_locks and state.op_lock_expires_ms == result["expires_ms"]
    record = await w.operations.get("pv-1")
    assert record.op_type is OperationType.SWITCH and record.status is OperationStatus.SUCCEEDED
    assert record.operator == "alice" and record.session_id == 7 and record.scope == "m2"


async def test_preview_notes_say_that_existing_positions_keep_their_rules_and_model_3_opens_and_takes_over_nothing(
        ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    to_m2 = await preview(w, "pv-1", "m2")
    assert any("归属" in note and "不变" in note for note in to_m2["notes"])
    assert not any("不开仓，也不接管新仓" in note for note in to_m2["notes"])
    await sw_cancel(w)
    to_m3 = await preview(w, "pv-2", "m3")
    assert "切到模型 3：不开仓，也不接管新仓" in to_m3["notes"]


async def test_the_preview_blocks_new_opens_while_it_lasts(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    assert w.modelstate.current().can_open(Model.M1)[0] is True
    await preview(w)
    allowed, why = w.modelstate.current().can_open(Model.M1)
    assert allowed is False and "切换预览中" in why  # 协调者据此跳过开仓并写进意图原因（留痕）


# ====================================================================== 预览被拒


@pytest.mark.parametrize("target", ["m1", "m4", "", None, 2])
async def test_the_target_must_be_another_known_model(ledger, clock, bus, target):
    w = await build_world(ledger, clock, bus)
    err = await refused(preview(w, target=target), "invalid")
    assert err.message
    assert w.modelstate.current().op_locks == frozenset()


async def test_the_command_line_principal_has_no_seat_so_it_cannot_switch(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await refused(preview(w, session_id=None, operator="cli"), "seat_required")
    assert w.modelstate.current().op_locks == frozenset()


@pytest.mark.parametrize("lock", [OpLock.SWITCHING, OpLock.BULK_CLOSING, OpLock.SWITCH_PREVIEW])
async def test_preview_is_refused_while_another_lock_is_held(ledger, clock, bus, lock):
    w = await build_world(ledger, clock, bus)
    await w.modelstate.acquire_op_lock(lock, owner="other")
    await refused(preview(w), "not_allowed")


async def test_preview_is_refused_with_a_suppression_reason(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await w.modelstate.add_suppression(Suppression.RECONCILE_MISMATCH, reason="测试")
    err = await refused(preview(w), "not_allowed")
    assert "对账不一致" in err.message


async def test_the_same_preview_operation_id_returns_the_first_answer_without_taking_the_lock_again(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    first = await preview(w)
    version = w.modelstate.current().version
    again = await preview(w)
    assert again == first and w.modelstate.current().version == version


# ====================================================================== 有效期、取消、席位、一键全平

async def test_the_preview_expires_after_the_ttl_and_the_lock_is_released(ledger, clock, bus):
    w = await build_world(ledger, clock, bus, switch_preview_ttl_s=60)
    await preview(w)
    clock.advance(59_000)
    assert await w.switch.expire_due() is False
    assert OpLock.SWITCH_PREVIEW in w.modelstate.current().op_locks
    clock.advance(1_000)  # 恰好到点
    assert await w.switch.expire_due() is True
    assert w.modelstate.current().op_locks == frozenset()
    assert await w.switch.expire_due() is False  # 没有可清的了
    await refused(sw_confirm(w, model_version=w.modelstate.current().version), "not_allowed")


async def test_confirming_an_expired_preview_is_refused_even_before_the_sweeper_ran(ledger, clock, bus):
    w = await build_world(ledger, clock, bus, switch_preview_ttl_s=60)
    result = await preview(w)
    clock.advance(61_000)
    err = await refused(sw_confirm(w, model_version=result["model_version"]), "not_allowed")
    assert "预览" in err.message
    assert w.modelstate.current().active_model is Model.M1 and w.modelstate.current().op_locks == frozenset()


async def test_cancel_releases_the_preview_and_records_the_operation(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await preview(w)
    assert await sw_cancel(w) == {"cancelled": True}
    assert w.modelstate.current().op_locks == frozenset()
    record = await w.operations.get("cx-1")
    assert record.op_type is OperationType.SWITCH and record.status is OperationStatus.SUCCEEDED
    # 取消之后可以重新预览
    await preview(w, "pv-2", "m3")


async def test_cancel_without_a_preview_or_by_another_session_is_refused(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await refused(sw_cancel(w, "cx-0"), "not_allowed")
    await preview(w, session_id=7)
    await refused(sw_cancel(w, "cx-1", session_id=8, operator="bob"), "not_allowed")
    assert OpLock.SWITCH_PREVIEW in w.modelstate.current().op_locks  # 别人取消不了


async def test_releasing_the_seat_cancels_the_preview(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await preview(w)
    await w.switch.on_seat_changed(None)
    assert w.modelstate.current().op_locks == frozenset()


async def test_somebody_else_taking_the_seat_cancels_the_preview_but_the_owner_renewing_does_not(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await preview(w, session_id=7)
    await w.switch.on_seat_changed(7)  # 持有者自己续租、重复申请：预览还在
    assert OpLock.SWITCH_PREVIEW in w.modelstate.current().op_locks
    await w.switch.on_seat_changed(8)  # 强制接管：预览取消
    assert w.modelstate.current().op_locks == frozenset()


async def test_a_seat_changed_event_on_the_bus_cancels_the_preview(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await bus.start()
    await w.switch.start()
    await preview(w, session_id=7)
    bus.publish(SeatChanged(holder_session_id=None, cause="logout", at_ms=clock.now_ms()))
    await bus.drain()
    assert w.modelstate.current().op_locks == frozenset()
    await w.switch.stop()
    await bus.stop()


async def test_seat_events_do_nothing_when_there_is_no_preview(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await w.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner="ca-x")
    await w.switch.on_seat_changed(None)
    assert w.modelstate.current().op_locks == frozenset({OpLock.BULK_CLOSING})  # 别人的锁不碰


async def test_starting_a_close_all_cancels_the_preview_and_the_confirm_that_follows_is_refused(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    w.add_position("BTCUSDT", LONG)
    result = await preview(w)
    await launch(w, model_version=w.modelstate.current().version)
    await finished(w)
    assert w.modelstate.current().op_locks == frozenset()
    await refused(sw_confirm(w, model_version=result["model_version"]), "not_allowed")
    await preview(w, "pv-2")  # 一键全平结束之后可以重新预览


async def test_a_kernel_restart_clears_the_preview(ledger, clock, bus):
    from dmr_kernel.core.modelstate import ModelStateService

    w = await build_world(ledger, clock, bus)
    await preview(w)
    reloaded = ModelStateService(ledger, bus, clock)
    snapshot = await reloaded.load()
    assert snapshot.op_locks == frozenset() and snapshot.gate is Gate.STOPPED
