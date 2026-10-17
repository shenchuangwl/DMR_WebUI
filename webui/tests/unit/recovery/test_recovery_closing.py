"""重启恢复第 6 步：「平仓中」的仓。订单回查到终态后，用户发起的平仓/部分平仓回到托管中并提示结果；
信号平仓与保护性平仓的意图重新生效继续执行；还没查清的保持平仓中（部分平仓「暂停到期恢复」的记录只在协调者内存里，重启后由这里处理：
还没查清的那些，等协调者后来把意图走到终态时由恢复放回托管中——否则它们会永远停在平仓中）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import AlertLevel, ExitReason, IntentAction, IntentSource, IntentState, PositionStatus
from recovery_kit import LONG, SHORT, pos


async def closing_position(env, **kw):
    rec = await env.add_position(status="closing", **kw)
    env.positions = [pos()]
    return rec


async def test_a_position_stuck_in_closing_with_a_finished_partial_close_goes_back_to_managed(make_env):
    env = await make_env().boot()
    rec = await closing_position(env)
    await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                         state=IntentState.COMPLETED, reason="手动部分平仓 50%")
    result = await env.run()
    assert env.registry.by_id(rec.id).status is PositionStatus.MANAGED
    assert result.steps["closing"]["resumed"] == [rec.id]
    (alert,) = env.alerts("recovery_closing_resumed")
    assert alert.level is AlertLevel.INFO and "部分平仓" in alert.message and "COMPLETED" in alert.message


async def test_the_result_message_covers_a_rejected_close_too(make_env):
    env = await make_env().boot()
    await closing_position(env)
    await env.new_intent(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR,
                         state=IntentState.REJECTED)
    await env.run()
    assert "REJECTED" in env.alerts("recovery_closing_resumed")[0].message


async def test_closing_without_any_close_intent_is_resumed_with_a_note(make_env):
    env = await make_env().boot()
    rec = await closing_position(env)
    await env.run()
    assert env.registry.by_id(rec.id).status is PositionStatus.MANAGED
    assert "没有查到相关的平仓意图" in env.alerts("recovery_closing_resumed")[0].message


async def test_a_close_that_is_still_in_flight_keeps_the_position_in_closing(make_env):
    env = await make_env().boot()
    rec = await closing_position(env)
    intent = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                                  state=IntentState.RECONCILING)
    result = await env.run()
    assert env.registry.by_id(rec.id).status is PositionStatus.CLOSING
    assert result.steps["closing"]["kept"] == [rec.id] and result.steps["closing"]["resumed"] == []
    assert env.coordinator.submitted == []  # 部分平仓永不自动重发
    assert str(intent.id) in env.alerts("recovery_closing_unresolved")[0].message


async def test_a_protective_close_that_was_never_sent_takes_effect_again(make_env):
    """保护性平仓（带 retry_until_done）落了盘、还没发出：同一幂等键重新交给协调者，继续执行。"""
    env = await make_env().boot()
    rec = await closing_position(env)
    intent = await env.new_intent(action=IntentAction.CLOSE, model="op", source=IntentSource.PROTECTION,
                                  state=IntentState.CREATED, params={"retry_until_done": True},
                                  key="protect:test:close", reason="protective:止损挂不上")
    result = await env.run()
    (spec,) = env.coordinator.submitted
    assert spec.idem_key == "protect:test:close" and spec.action is IntentAction.CLOSE
    assert spec.params == {"retry_until_done": True} and spec.reason == "protective:止损挂不上"
    assert result.steps["closing"]["resubmitted"] == [intent.id]
    assert env.registry.by_id(rec.id).status is PositionStatus.CLOSING  # 平仓还要继续，仓位保持平仓中


async def test_a_protective_close_that_cannot_be_resubmitted_is_a_problem(make_env):
    """保护性平仓没能重新生效：仓位停在平仓中、平仓不会继续，必须算问题（保持「恢复中」并告警），不能只记日志。"""
    env = await make_env().boot()
    rec = await closing_position(env)
    await env.new_intent(action=IntentAction.CLOSE, model="op", source=IntentSource.PROTECTION,
                         state=IntentState.CREATED, params={"retry_until_done": True},
                         key="protect:test:close", reason="protective:止损挂不上")

    async def submit(spec):
        raise RuntimeError("协调者忙")

    env.coordinator.submit = submit
    result = await env.run()
    assert result.tradable is False and any("协调者忙" in p for p in result.problems)
    assert result.steps["closing"]["resubmitted"] == []
    assert result.steps["closing"]["kept"] == [rec.id]  # 照样保持平仓中、继续观察
    assert env.registry.by_id(rec.id).status is PositionStatus.CLOSING


async def test_a_signal_close_in_reconciling_is_left_to_the_coordinator(make_env):
    """已经有订单、在回查中的信号平仓：协调者的回查接着做，恢复不重复提交。"""
    env = await make_env().boot()
    await closing_position(env)
    await env.new_intent(action=IntentAction.CLOSE, model="m1", source=IntentSource.SIGNAL,
                         state=IntentState.RECONCILING, params={"retry_until_done": True})
    await env.run()
    assert env.coordinator.submitted == []


async def test_a_close_without_retry_that_was_never_sent_is_not_resubmitted(make_env):
    """用户发起的平仓（没有 retry_until_done）落了盘没发出：不替用户重发。"""
    env = await make_env().boot()
    await closing_position(env)
    await env.new_intent(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR,
                         state=IntentState.CREATED)
    await env.run()
    assert env.coordinator.submitted == []


async def test_positions_that_are_not_closing_are_left_alone(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = [pos()]
    result = await env.run()
    assert env.registry.by_id(rec.id).status is PositionStatus.MANAGED
    assert result.steps["closing"] == {"resumed": [], "kept": [], "resubmitted": []}
    assert env.alerts("recovery_closing_resumed") == []


async def test_only_close_intents_of_the_same_key_count(make_env):
    env = await make_env().boot()
    rec = await closing_position(env)
    await env.new_intent(action=IntentAction.CLOSE, symbol="ETHUSDT", model="op", state=IntentState.RECONCILING)
    await env.new_intent(action=IntentAction.CLOSE, side=LONG.opposite(), model="op", state=IntentState.RECONCILING)
    await env.new_intent(action=IntentAction.STOP_PLACE, model="op", state=IntentState.RECONCILING)
    await env.run()
    assert env.registry.by_id(rec.id).status is PositionStatus.MANAGED


# ================================================================ 还没查清的平仓后来查清了：协调者的「暂停到期恢复」记录重启时丢了


async def in_flight_partial_close(env, **kw):
    """一个「平仓中」的仓，名下有一个还在回查的部分平仓；恢复跑完，仓位仍在平仓中。返回（持仓, 意图）。"""
    rec = await closing_position(env)
    intent = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                                  state=IntentState.RECONCILING, **kw)
    await env.run()
    assert env.registry.by_id(rec.id).status is PositionStatus.CLOSING  # 前提
    assert env.alerts("recovery_closing_resumed") == []
    return rec, intent


async def settle_intent(env, intent, state, reason="协调者回查的结论"):
    await env.intents.transition(intent.id, state, reason=reason)
    await env.settle()


@pytest.mark.parametrize("final", [IntentState.COMPLETED, IntentState.REJECTED])
async def test_a_close_settled_after_the_recovery_puts_the_position_back_to_managed(make_env, final):
    env = await make_env().boot()
    rec, intent = await in_flight_partial_close(env)
    await settle_intent(env, intent, final, reason="回查三次都查不到这张订单")
    assert env.registry.by_id(rec.id).status is PositionStatus.MANAGED
    (alert,) = env.alerts("recovery_closing_resumed")
    assert alert.level is AlertLevel.INFO and final.value in alert.message and "回查三次都查不到" in alert.message
    assert "部分平仓" in alert.message and str(intent.id) in alert.message


async def test_the_position_waits_for_every_in_flight_close_of_the_key(make_env):
    env = await make_env().boot()
    rec = await closing_position(env)
    first = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                                 state=IntentState.RECONCILING)
    second = await env.new_intent(action=IntentAction.CLOSE, model="op", source=IntentSource.PROTECTION,
                                  state=IntentState.RECONCILING, params={"retry_until_done": True})
    await env.run()
    await settle_intent(env, first, IntentState.COMPLETED)
    assert env.registry.by_id(rec.id).status is PositionStatus.CLOSING and env.alerts("recovery_closing_resumed") == []
    await settle_intent(env, second, IntentState.REJECTED)
    assert env.registry.by_id(rec.id).status is PositionStatus.MANAGED
    assert len(env.alerts("recovery_closing_resumed")) == 1


async def test_a_state_change_that_is_not_final_changes_nothing(make_env):
    env = await make_env().boot()
    rec, intent = await in_flight_partial_close(env)
    await settle_intent(env, intent, IntentState.ACTIVE, reason="回查到了订单，还在成交")
    assert env.registry.by_id(rec.id).status is PositionStatus.CLOSING
    assert env.alerts("recovery_closing_resumed") == []
    await settle_intent(env, intent, IntentState.COMPLETED)
    assert env.registry.by_id(rec.id).status is PositionStatus.MANAGED


async def test_an_unrelated_intent_ending_does_not_resume_the_position(make_env):
    env = await make_env().boot()
    rec, _ = await in_flight_partial_close(env)
    other = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, symbol="ETHUSDT", model="op",
                                 state=IntentState.RECONCILING)
    await settle_intent(env, other, IntentState.COMPLETED)
    assert env.registry.by_id(rec.id).status is PositionStatus.CLOSING


async def test_a_position_closed_meanwhile_is_left_alone(make_env):
    env = await make_env().boot()
    rec, intent = await in_flight_partial_close(env)
    await env.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=env.clock.now_ms())
    await settle_intent(env, intent, IntentState.COMPLETED)
    assert env.registry.by_id(rec.id) is None
    assert env.alerts("recovery_closing_resumed") == []


async def test_a_new_close_started_after_the_recovery_keeps_the_position_in_closing(make_env):
    """用户在恢复之后又发起了平仓：这条新意图的暂停由协调者自己记着，恢复不抢先放回。"""
    env = await make_env().boot()
    rec, intent = await in_flight_partial_close(env)
    newer = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                                 state=IntentState.ACTIVE)
    await settle_intent(env, intent, IntentState.COMPLETED)
    assert env.registry.by_id(rec.id).status is PositionStatus.CLOSING
    assert env.alerts("recovery_closing_resumed") == []
    await settle_intent(env, newer, IntentState.COMPLETED)  # 新的也结束了：这时才放回
    assert env.registry.by_id(rec.id).status is PositionStatus.MANAGED
    (alert,) = env.alerts("recovery_closing_resumed")
    assert str(newer.id) in alert.message


async def test_a_close_that_ended_between_the_listing_and_the_watch_is_not_waited_for_forever(make_env, monkeypatch):
    """读「未到终态的意图」和开始观察之间意图到了终态：之后不会再有事件，所以开始观察时要把现状再核对一遍。"""
    env = await make_env().boot()
    rec = await closing_position(env)
    intent = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                                  state=IntentState.RECONCILING)
    stale = await env.intents.non_terminal()
    assert [i.id for i in stale] == [intent.id]
    await env.intents.transition(intent.id, IntentState.COMPLETED, reason="刚刚结束")

    async def non_terminal(**kw):  # 这一轮恢复里读到的清单都是结束之前的
        return stale

    monkeypatch.setattr(env.intents, "non_terminal", non_terminal)
    await env.run()
    await env.settle()
    assert env.registry.by_id(rec.id).status is PositionStatus.MANAGED
    assert len(env.alerts("recovery_closing_resumed")) == 1


async def test_running_the_same_recovery_twice_watches_only_once(make_env):
    env = await make_env().boot()
    rec = await closing_position(env)
    intent = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                                  state=IntentState.RECONCILING)
    recovery = env.recovery()
    await recovery.run()
    await recovery.run()
    assert len([s for s in env.bus._subs if s.name == "recovery.closing"]) == 1
    await settle_intent(env, intent, IntentState.COMPLETED)
    assert env.registry.by_id(rec.id).status is PositionStatus.MANAGED
    assert len(env.alerts("recovery_closing_resumed")) == 1  # 只放回、只提示一次


async def test_both_sides_of_a_symbol_are_watched_independently(make_env):
    env = await make_env().boot()
    long_ = await closing_position(env)
    short = await env.add_position(side=SHORT, status="closing")
    env.positions = [pos(), pos(side=SHORT)]
    a = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op", state=IntentState.RECONCILING)
    b = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, side=SHORT, model="op", state=IntentState.RECONCILING)
    await env.run()
    await settle_intent(env, b, IntentState.COMPLETED)
    assert env.registry.by_id(short.id).status is PositionStatus.MANAGED
    assert env.registry.by_id(long_.id).status is PositionStatus.CLOSING
    await settle_intent(env, a, IntentState.COMPLETED)
    assert env.registry.by_id(long_.id).status is PositionStatus.MANAGED


async def test_the_summary_still_lists_the_position_as_kept_at_the_time_of_the_run(make_env):
    env = await make_env().boot()
    rec = await closing_position(env)
    intent = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                                  state=IntentState.RECONCILING)
    result = await env.run()
    await settle_intent(env, intent, IntentState.COMPLETED)
    assert result.steps["closing"]["kept"] == [rec.id]  # 结果是恢复结束时的快照，后来的变化不回头改它


async def test_one_position_that_cannot_be_resumed_does_not_stop_the_others(make_env, monkeypatch):
    env = await make_env().boot()
    first = await closing_position(env)
    second = await env.add_position(symbol="ETHUSDT", status="closing")
    env.positions = [pos(), pos(symbol="ETHUSDT")]
    real = env.registry.set_closing

    async def flaky(position_id, closing, *, cause):
        if position_id == first.id:
            raise RuntimeError("账本写失败")
        return await real(position_id, closing, cause=cause)

    monkeypatch.setattr(env.registry, "set_closing", flaky)
    result = await env.run()
    assert env.registry.by_id(second.id).status is PositionStatus.MANAGED  # 另一个仓照常放回
    assert env.registry.by_id(first.id).status is PositionStatus.CLOSING
    assert result.steps["closing"]["resumed"] == [second.id] and result.steps["closing"]["errors"][0]["position_id"] == first.id
    assert result.tradable is False and any("账本写失败" in p for p in result.problems)
