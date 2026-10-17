"""模型切换的确认与执行（F6 第 2–8 步）：口令与版本、保留原规则、先平后切、停稳的四条判据与 10 秒时限、
切换后一律已停止、新模型在同一事务里生效并解锁、中断点 8（switch_midway）。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.types import (
    Gate,
    IntentAction,
    IntentState,
    Model,
    OperationStatus,
    OperationType,
    OpLock,
    Ownership,
    PositionStatus,
    ProtectionState,
    RunMode,
    Suppression,
)
from dmr_kernel.core.types import OrderState
from dmr_kernel.ledger import repo_modelstate
from tests.unit.ops_m2b.m2b_kit import (
    LONG,
    SHORT,
    Behavior,
    FakeIntent,
    FakeOrder,
    build_world,
    caller,
    drive,
    settle_tasks,
)
from tests.unit.ops_m2b.test_m2b_switch_preview import preview, refused, sw_confirm

D = Decimal


async def confirm(w, preview_result, *, op_id="cf-1", mode="keep", session_id=7, operator="alice",
                  password_checked=True, model_version=None):
    return await sw_confirm(
        w, op_id=op_id, mode=mode, operator=operator, session_id=session_id, checked=password_checked,
        model_version=preview_result["model_version"] if model_version is None else model_version)


async def run_to_end(w, preview_result, **kw):
    """确认并把时钟往前推直到结束；返回（记录, 报文）。"""
    return await drive(w.clock, confirm(w, preview_result, **kw))


async def switched(w, op_id="cf-1"):
    await drive(w.clock, w.switch.wait(op_id))
    return await w.operations.get(op_id)


# ====================================================================== 保留原规则


async def test_keep_switches_the_model_stops_the_gate_and_clears_the_locks_in_one_commit(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    pos = w.add_position("BTCUSDT", LONG, ownership=Ownership.STRATEGY)
    seen_gate = []
    real_cancel = w.coordinator.cancel_unfilled_opens

    async def cancel_spy(**kw):
        seen_gate.append(w.modelstate.current().gate)
        return await real_cancel(**kw)

    w.coordinator.cancel_unfilled_opens = cancel_spy
    pv = await preview(w, target="m2")
    record, payload = await run_to_end(w, pv)
    assert record.status is OperationStatus.SUCCEEDED
    assert payload["status"] == "succeeded" and payload["active_model"] == "m2" and payload["gate"] == "stopped"
    assert payload["detail"]
    state = w.modelstate.current()
    assert state.active_model is Model.M2 and state.gate is Gate.STOPPED  # 切换后一律已停止
    assert state.op_locks == frozenset() and state.op_lock_owner is None  # 同一事务里解锁
    assert seen_gate == [Gate.STOPPED]  # 先关闸，再撤未成交的开仓单
    assert w.coordinator.cancel_calls and "切换" in w.coordinator.cancel_calls[0]["reason"]
    # 已有持仓的归属、保护、状态都不变；没有发出任何交易意图
    assert w.registry.by_id(pos.id) == pos and w.coordinator.submitted == []
    row = (await ledger.read(repo_modelstate.recent_switch_log))[0]
    assert (row.from_model, row.to_model, row.op_id, row.operator) == ("m1", "m2", "cf-1", "alice")
    assert "保留" in (row.mode or "")


@pytest.mark.parametrize("initial_gate", [Gate.RUNNING, Gate.STOPPED])
@pytest.mark.parametrize("target", ["m2", "m3"])
async def test_after_any_switch_the_gate_is_stopped_whatever_it_was(ledger, clock, bus, initial_gate, target):
    w = await build_world(ledger, clock, bus)
    await w.modelstate.set_gate(initial_gate, reason="t")
    pv = await preview(w, target=target)
    await run_to_end(w, pv)
    state = w.modelstate.current()
    assert state.active_model is Model(target) and state.gate is Gate.STOPPED


async def test_switching_back_and_forth_works_and_each_switch_is_logged(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await run_to_end(w, await preview(w, "pv-1", "m3"), op_id="cf-1")
    await run_to_end(w, await preview(w, "pv-2", "m1"), op_id="cf-2")
    log = await ledger.read(repo_modelstate.recent_switch_log)
    assert [(r.from_model, r.to_model) for r in reversed(log)] == [("m1", "m3"), ("m3", "m1")]


# ====================================================================== 入口校验


async def test_an_unchecked_password_is_refused_and_the_preview_stays_alive(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    pv = await preview(w)
    await refused(confirm(w, pv, password_checked=False), "reauth_required")
    state = w.modelstate.current()
    assert OpLock.SWITCH_PREVIEW in state.op_locks and state.active_model is Model.M1
    assert await w.operations.get("cf-1") is None
    await run_to_end(w, pv, op_id="cf-2")  # 补上口令之后还能确认
    assert w.modelstate.current().active_model is Model.M2


async def test_a_stale_model_version_is_refused_and_the_preview_stays_alive(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    pv = await preview(w)
    await w.modelstate.set_run_mode(RunMode.POLLING_DEGRADED, reason="别处改了状态")
    await refused(confirm(w, pv), "stale_version")
    assert OpLock.SWITCH_PREVIEW in w.modelstate.current().op_locks and w.modelstate.current().active_model is Model.M1


async def test_confirm_without_a_preview_is_refused(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await refused(sw_confirm(w, model_version=w.modelstate.current().version), "not_allowed")


async def test_only_the_session_that_made_the_preview_can_confirm_it(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    pv = await preview(w, session_id=7)
    err = await refused(confirm(w, pv, session_id=8, operator="bob"), "not_allowed")
    assert "会话" in err.message
    await refused(confirm(w, pv, session_id=None, operator="cli"), "seat_required")
    assert w.modelstate.current().active_model is Model.M1


async def test_a_suppression_that_appears_during_the_preview_forbids_the_confirm_but_not_the_cancel(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await preview(w)
    await w.modelstate.add_suppression(Suppression.EXCHANGE_RESTRICTED, reason="测试")
    version = w.modelstate.current().version
    err = await refused(sw_confirm(w, model_version=version), "not_allowed")
    assert "交易所限制" in err.message
    assert await w.switch.cancel(caller(w), op_id="cx-1") == {"cancelled": True}


@pytest.mark.parametrize("mode", ["", "stop", None, "KEEP"])
async def test_an_unknown_mode_is_refused_as_invalid(ledger, clock, bus, mode):
    w = await build_world(ledger, clock, bus)
    pv = await preview(w)
    await refused(confirm(w, pv, mode=mode), "invalid")
    assert OpLock.SWITCH_PREVIEW in w.modelstate.current().op_locks


async def test_a_repeated_confirm_with_the_same_operation_id_switches_only_once(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    pv = await preview(w)
    first, _ = await run_to_end(w, pv)
    again, payload = await confirm(w, pv, model_version=0, password_checked=False)  # 重复提交不再校验、不再执行
    assert again.op_id == first.op_id and payload["status"] == "succeeded" and payload["active_model"] == "m2"
    assert len(await ledger.read(repo_modelstate.recent_switch_log)) == 1


# ====================================================================== 停稳的四条判据与 10 秒时限


def blockers():
    """四条判据各自的「停不稳」布置。"""
    return [
        ("open_intent", lambda w: w.intents.opens.append(
            FakeIntent(77, IntentState.ACTIVE, symbol="BTCUSDT", side=LONG, action=IntentAction.OPEN)), "开仓"),
        ("unknown_order", lambda w: w.orders.rows.append(FakeOrder("DK1-1-9-o", OrderState.SENT_UNKNOWN)), "未知"),
        ("pending_send_order", lambda w: w.orders.rows.append(FakeOrder("DK1-1-9-o", OrderState.PENDING_SEND)), "未知"),
        ("taking_over", lambda w: w.registry.add("SOLUSDT", LONG, status=PositionStatus.TAKING_OVER,
                                                  ownership=Ownership.TAKEOVER), "接管"),
        ("rebuilding", lambda w: w.registry.add("ETHUSDT", LONG, protection=ProtectionState.REBUILDING), "保护"),
    ]


@pytest.mark.parametrize("name, arrange, word", blockers(), ids=[b[0] for b in blockers()])
async def test_if_the_old_model_does_not_settle_within_the_limit_it_stays_stopped_in_the_old_model(
        ledger, clock, bus, name, arrange, word):
    w = await build_world(ledger, clock, bus, switch_settle_s=10)
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    arrange(w)
    pv = await preview(w)
    t0 = clock.now_ms()
    record, payload = await run_to_end(w, pv)
    waited = clock.now_ms() - t0
    assert 10_000 <= waited < 16_000  # switch_settle_s 秒内停不稳才放弃
    assert record.status is OperationStatus.FAILED
    assert payload["status"] == "failed" and payload["active_model"] == "m1" and word in payload["reason"]
    state = w.modelstate.current()
    assert state.active_model is Model.M1 and state.gate is Gate.STOPPED  # 留在旧模型的已停止
    assert state.op_locks == frozenset()  # 锁释放，不卡死
    assert await ledger.read(repo_modelstate.recent_switch_log) == []  # 没有切换成功的记录


async def test_it_settles_when_the_blocker_clears_inside_the_window(ledger, clock, bus):
    w = await build_world(ledger, clock, bus, switch_settle_s=10)
    opening = FakeIntent(77, IntentState.ACTIVE, symbol="BTCUSDT", side=LONG, action=IntentAction.OPEN)
    w.intents.opens.append(opening)
    pv = await preview(w)

    async def clear_later():
        while clock.now_ms() < pv_time + 3_000:
            await asyncio.sleep(0.002)
        w.intents.opens.clear()

    pv_time = clock.now_ms()
    clearer = asyncio.ensure_future(clear_later())
    record, payload = await run_to_end(w, pv)
    await clearer
    assert payload["status"] == "succeeded" and w.modelstate.current().active_model is Model.M2


async def test_a_cancel_pending_order_does_not_count_as_unknown(ledger, clock, bus):
    """撤单回报未到的订单（常见于仍挂着的旧止损）不是「状态未知的订单」：不挡切换。"""
    w = await build_world(ledger, clock, bus)
    w.orders.rows.append(FakeOrder("DK1-1-9-s", OrderState.CANCEL_PENDING))
    _, payload = await run_to_end(w, await preview(w))
    assert payload["status"] == "succeeded"


async def test_registered_positions_that_are_settled_do_not_block(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    w.registry.add("BTCUSDT", LONG, status=PositionStatus.MANAGED, protection=ProtectionState.SYSTEM)
    w.registry.add("ETHUSDT", LONG, status=PositionStatus.CLOSING, protection=ProtectionState.EXTERNAL)
    w.registry.add("DOGEUSDT", LONG, status=PositionStatus.PENDING, ownership=Ownership.NONE)
    _, payload = await run_to_end(w, await preview(w))
    assert payload["status"] == "succeeded"


async def test_a_failed_switch_can_be_previewed_again_straight_away(ledger, clock, bus):
    w = await build_world(ledger, clock, bus, switch_settle_s=10)
    w.intents.opens.append(FakeIntent(77, IntentState.ACTIVE, symbol="BTCUSDT", side=LONG, action=IntentAction.OPEN))
    await run_to_end(w, await preview(w))
    w.intents.opens.clear()
    _, payload = await run_to_end(w, await preview(w, "pv-2"), op_id="cf-2")
    assert payload["status"] == "succeeded"


# ====================================================================== 先全部平仓再切换


async def test_close_all_first_switches_only_after_everything_is_closed(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    w.add_position("BTCUSDT", LONG, ownership=Ownership.STRATEGY)
    w.add_position("SOLUSDT", SHORT, ownership=Ownership.TAKEOVER)  # 范围固定为全部托管持仓：模型 1 下接管仓也平
    w.add_position("DOGEUSDT", LONG, ownership=Ownership.NONE, status=PositionStatus.PENDING)  # 待确认仍不包含
    pv = await preview(w)
    record, payload = await confirm(w, pv, mode="close_all_first")
    assert payload["status"] == "running" and record.status is OperationStatus.RUNNING
    assert w.modelstate.current().active_model is Model.M1  # 平完之前还是旧模型
    await switched(w)
    final = await w.operations.get("cf-1")
    assert final.status is OperationStatus.SUCCEEDED
    assert final.result["status"] == "succeeded" and final.result["active_model"] == "m2"
    assert w.coordinator.exchange[("BTCUSDT", LONG)] == 0 and w.coordinator.exchange[("SOLUSDT", SHORT)] == 0
    assert w.coordinator.exchange[("DOGEUSDT", LONG)] == 1  # 待确认的没动
    assert {r["symbol"] for r in final.per_position} == {"BTCUSDT", "SOLUSDT"}
    state = w.modelstate.current()
    assert state.active_model is Model.M2 and state.gate is Gate.STOPPED and state.op_locks == frozenset()
    row = (await ledger.read(repo_modelstate.recent_switch_log))[0]
    assert "平仓" in (row.mode or "")


async def test_close_all_first_records_its_own_close_all_operation_and_covers_all_managed_positions(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    w.add_position("SOLUSDT", SHORT, ownership=Ownership.TAKEOVER)
    pv = await preview(w)
    await confirm(w, pv, mode="close_all_first")
    await switched(w)
    inner = await w.operations.get("cf-1:ca")
    assert inner is not None and inner.op_type is OperationType.CLOSE_ALL and inner.scope == "all_managed"
    assert inner.status is OperationStatus.SUCCEEDED and inner.operator == "alice"


@pytest.mark.parametrize("behavior, ok", [("ok", True), ("external", True), ("reject", False), ("stuck", False),
                                          ("residual", False)])
async def test_close_all_first_continues_only_if_every_result_is_closed_or_closed_externally(
        ledger, clock, bus, behavior, ok):
    w = await build_world(ledger, clock, bus, close_one_timeout_s=15)
    w.add_position("BTCUSDT", LONG)
    w.add_position("ETHUSDT", LONG)
    w.coordinator.behave("ETHUSDT", LONG, Behavior(behavior, reason="测试"))
    pv = await preview(w)
    await confirm(w, pv, mode="close_all_first")
    final = await switched(w)
    state = w.modelstate.current()
    if ok:
        assert final.status is OperationStatus.SUCCEEDED and state.active_model is Model.M2
    else:
        # 停在旧模型的已停止，并给出逐仓结果
        assert final.status is OperationStatus.FAILED
        assert final.result["status"] == "failed" and final.result["active_model"] == "m1"
        assert final.result["reason"] and {r["symbol"] for r in final.per_position} == {"BTCUSDT", "ETHUSDT"}
        assert state.active_model is Model.M1 and state.gate is Gate.STOPPED
        assert await ledger.read(repo_modelstate.recent_switch_log) == []
    assert state.op_locks == frozenset()


async def test_close_all_first_cancels_the_preview_through_the_close_all_lock_and_one_password_covers_both_steps(
        ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    w.add_position("BTCUSDT", LONG)
    pv = await preview(w)
    await confirm(w, pv, mode="close_all_first")  # password_checked=True 一次，内部一键全平不再要口令
    final = await switched(w)
    assert final.status is OperationStatus.SUCCEEDED


async def test_close_all_first_with_a_suppression_after_the_close_all_stays_in_the_old_model(ledger, clock, bus):
    """平完之后、拿切换中锁之前出现了抑制原因：按 F6 第 3 步拒绝，留在旧模型的已停止。"""
    w = await build_world(ledger, clock, bus)
    w.add_position("BTCUSDT", LONG)
    w.coordinator.behave("BTCUSDT", LONG, Behavior("ok", latency_ms=3_000))
    pv = await preview(w)
    await confirm(w, pv, mode="close_all_first")
    await settle_tasks()
    await w.modelstate.add_suppression(Suppression.CLOCK_SKEW, reason="测试")
    final = await switched(w)
    assert final.status is OperationStatus.FAILED and "抑制" in final.result["reason"]
    assert w.modelstate.current().active_model is Model.M1 and w.modelstate.current().op_locks == frozenset()


# ====================================================================== 中断点 8：switch_midway


async def test_crash_point_8_leaves_the_old_model_and_the_lock_until_the_restart_clears_it(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    w.add_position("BTCUSDT", LONG)
    pv = await preview(w)
    w.crash.arm("switch_midway")
    with pytest.raises(CrashSignal) as caught:
        await run_to_end(w, pv)
    assert caught.value.name == "switch_midway" and w.switch.crashed is caught.value
    # 进程「被杀」在第 4–6 步之间：旧模型还是生效模型，闸已关，切换中锁还在，操作还是进行中
    state = w.modelstate.current()
    assert state.active_model is Model.M1 and state.gate is Gate.STOPPED
    assert OpLock.SWITCHING in state.op_locks
    assert (await w.operations.get("cf-1")).status is OperationStatus.RUNNING
    assert await ledger.read(repo_modelstate.recent_switch_log) == []
    # 重启：以最后一次已提交的事务为准——仍是旧模型，操作锁清除，一律已停止
    reloaded = ModelStateService(ledger, bus, clock)
    restarted = await reloaded.load()
    assert restarted.active_model is Model.M1 and restarted.gate is Gate.STOPPED and restarted.op_locks == frozenset()
    assert w.coordinator.exchange[("BTCUSDT", LONG)] == 1  # 持仓没动


async def test_crash_point_8_is_not_hit_when_the_old_model_never_settled(ledger, clock, bus):
    w = await build_world(ledger, clock, bus, switch_settle_s=10)
    w.intents.opens.append(FakeIntent(77, IntentState.ACTIVE, symbol="BTCUSDT", side=LONG, action=IntentAction.OPEN))
    pv = await preview(w)
    w.crash.arm("switch_midway")
    _, payload = await run_to_end(w, pv)
    assert payload["status"] == "failed" and "switch_midway" in w.crash.armed  # 没走到第 5 步之后
