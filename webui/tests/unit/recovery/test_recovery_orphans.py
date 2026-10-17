"""重启恢复第 8 步：孤儿条件单清理（F8 的五条，经对账）。恢复自己不判孤儿、不撤单——只调对账、等对账提交的撤单意图做完。"""
from __future__ import annotations

from dmr_kernel.core.types import AlertLevel, IntentAction, IntentSource, IntentState
from recovery_kit import pos


async def test_orders_are_reconciled_after_the_positions_so_the_zero_readings_are_fresh(make_env):
    env = await make_env().boot()
    result = await env.run()
    assert env.index_of("reconcile_positions") < env.index_of("reconcile_orders")
    assert result.steps["orphans"]["orders_reconcile"] == "ok"
    assert env.alerts("recovery_done")  # 一切顺利：最后一条提示


async def test_the_reconcile_runs_even_without_a_snapshot_but_then_no_orphan_is_judged_by_recovery(make_env):
    """孤儿的判定在对账里（它自己要新鲜的持仓快照，没有就只告警不撤）；恢复照样调用。"""
    env = await make_env().boot()
    env.fail("fetch_positions")
    result = await env.run()
    assert result.steps["orphans"]["orders_reconcile"] == "ok"
    assert env.adapter.writes == []


async def test_recovery_waits_for_the_cancel_intents_the_reconcile_submitted_and_counts_them(make_env):
    env = await make_env().boot()

    async def reconcile_orders():
        env.calls.append("reconcile_orders")
        done = await env.new_intent(action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.RECONCILE,
                                    state=IntentState.COMPLETED, key="orphan:9001")
        await env.new_intent(action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.RECONCILE,
                             state=IntentState.REJECTED, key="orphan:9002")
        await env.new_intent(action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.PROTECTION,
                             state=IntentState.COMPLETED, key="protect:other")  # 不是对账提交的
        assert done
        return "ok"

    env.reconciler.reconcile_orders = reconcile_orders
    result = await env.run()
    assert env.calls_named("coordinator.idle")
    assert env.index_of("reconcile_orders") < env.index_of("coordinator.idle")
    assert result.steps["orphans"]["cancel_intents"] == 2 and result.steps["orphans"]["cancelled"] == 1
    assert result.steps["summary"]["orphans_cleaned"] == 1


async def test_cancel_intents_from_before_the_run_are_not_counted(make_env):
    env = await make_env().boot()
    await env.new_intent(action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.RECONCILE,
                         state=IntentState.COMPLETED, key="orphan:old")
    env.clock.advance(60_000)
    result = await env.run()
    assert result.steps["orphans"]["cancel_intents"] == 0


async def test_an_unknown_order_reconcile_is_reported_but_is_not_a_recovery_failure(make_env):
    """挂单对账这一轮读不到：不下任何结论，定时对账稍后接着做；不是恢复的问题。"""
    env = await make_env().boot()
    env.reconciler.orders_result = "unknown"
    result = await env.run()
    assert result.steps["orphans"]["orders_reconcile"] == "unknown" and result.tradable is True


async def test_a_reconcile_that_raises_is_a_step_error(make_env):
    env = await make_env().boot()
    env.reconciler.orders_result = RuntimeError("对账炸了")
    env.positions = [pos()]
    result = await env.run()
    assert "对账炸了" in result.steps["orphans"]["error"] and result.tradable is False
    assert env.alerts("recovery_step_failed")[0].level is AlertLevel.CRITICAL
