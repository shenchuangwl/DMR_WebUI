"""重启恢复第 9 步：中断的批量操作与切换不自动续做——清操作锁；operation 表里非终态的操作记为「中断」并给出逐仓结果；
这些操作名下还没发出订单的意图取消，免得它们一直占着「合约 + 方向」。"""
from __future__ import annotations

from dmr_kernel.core.events import OperationProgress
from dmr_kernel.core.types import (
    ExitReason,
    IntentAction,
    IntentSource,
    IntentState,
    OpLock,
    OperationStatus,
    OperationType,
    OrderPurpose,
    Suppression,
)
from recovery_kit import D, LONG, SYM2, pos


async def begin(env, op_id="op-1", op_type=OperationType.CLOSE_ALL, status=OperationStatus.RUNNING):
    record, is_new = await env.operations.begin(op_id, op_type, operator="alice", session_id=None, scope="managed",
                                                params={"scope": "managed"}, status=status)
    assert is_new
    return record


async def test_a_running_operation_is_recorded_as_interrupted_with_the_reason(make_env):
    env = await make_env().boot()
    await begin(env)
    result = await env.run()
    op = await env.operations.get("op-1")
    assert op.status is OperationStatus.ABORTED and op.ended_ms is not None
    assert op.result["interrupted"] is True and "不自动续做" in op.result["reason"]
    assert op.result["op_type"] == "close_all" and op.result["status_before"] == "running"
    assert result.steps["operations"]["interrupted"] == [{"op_id": "op-1", "op_type": "close_all", "positions": 0}]
    assert result.steps["summary"]["operations_interrupted"] == 1
    assert env.alerts("operation_interrupted")


async def test_an_accepted_operation_that_never_started_is_interrupted_too(make_env):
    env = await make_env().boot()
    await begin(env, "op-2", OperationType.SWITCH, OperationStatus.ACCEPTED)
    await env.run()
    assert (await env.operations.get("op-2")).status is OperationStatus.ABORTED


async def test_finished_operations_are_not_touched(make_env):
    env = await make_env().boot()
    await begin(env, "done-1")
    await env.operations.finish("done-1", OperationStatus.SUCCEEDED, result={"ok": True})
    result = await env.run()
    op = await env.operations.get("done-1")
    assert op.status is OperationStatus.SUCCEEDED and op.result == {"ok": True}
    assert result.steps["operations"]["interrupted"] == []


async def test_the_operation_lock_is_cleared(make_env):
    env = await make_env().boot()
    await env.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner="op-1")
    await begin(env)
    result = await env.run()
    assert env.modelstate.current().op_locks == frozenset()
    assert result.steps["operations"]["locks_released"] == ["bulk_closing"]
    assert Suppression.RECOVERING not in env.suppressions()


async def test_there_is_nothing_to_release_when_no_lock_is_held(make_env):
    env = await make_env().boot()
    result = await env.run()
    assert result.steps["operations"]["locks_released"] == []


async def test_a_switch_lock_is_cleared_as_well(make_env):
    env = await make_env().boot()
    await env.modelstate.acquire_op_lock(OpLock.SWITCHING, owner="op-sw")
    await env.run()
    assert env.modelstate.current().op_locks == frozenset()


async def test_per_position_results_say_closed_residual_and_unknown(make_env):
    env = await make_env().boot()
    await begin(env)
    done = await env.add_position(symbol="BTCUSDT")
    left = await env.add_position(symbol="ETHUSDT")
    for rec, state in ((done, IntentState.COMPLETED), (left, IntentState.COMPLETED)):
        await env.new_intent(action=IntentAction.CLOSE, symbol=rec.symbol, model="op", source=IntentSource.OPERATOR,
                             state=state, op_id="op-1", position_id=rec.id)
    unsure = await env.add_position(symbol="SOLUSDT")
    await env.new_intent(action=IntentAction.CLOSE, symbol="SOLUSDT", model="op", source=IntentSource.OPERATOR,
                         state=IntentState.RECONCILING, op_id="op-1", position_id=unsure.id)
    await env.registry.mark_closed(done.id, exit_reason=ExitReason.MANUAL, closed_ms=env.clock.now_ms())
    env.positions = [pos("ETHUSDT", qty="2")]
    await env.run()
    rows = {row["symbol"]: row for row in (await env.operations.get("op-1")).per_position}
    assert rows["BTCUSDT"]["result"] == "closed" and rows["BTCUSDT"]["position_id"] == done.id
    assert rows["ETHUSDT"]["result"] == "residual" and "2" in rows["ETHUSDT"]["detail"]
    assert rows["SOLUSDT"]["result"] == "unknown" and rows["SOLUSDT"]["intent_id"]


async def test_a_position_that_is_gone_from_the_exchange_without_a_record_is_closed_externally(make_env):
    env = await make_env().boot()
    await begin(env)
    rec = await env.add_position()
    await env.new_intent(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR,
                         state=IntentState.COMPLETED, op_id="op-1", position_id=rec.id)
    env.positions = []
    await env.run()
    (row,) = (await env.operations.get("op-1")).per_position
    assert row["result"] == "closed_externally"


async def test_without_a_snapshot_the_per_position_result_is_unknown_not_residual_or_closed(make_env):
    env = await make_env().boot()
    await begin(env)
    rec = await env.add_position()
    await env.new_intent(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR,
                         state=IntentState.COMPLETED, op_id="op-1", position_id=rec.id)
    env.fail("fetch_positions")
    await env.run()
    (row,) = (await env.operations.get("op-1")).per_position
    assert row["result"] == "unknown" and "没有交易所快照" in row["detail"]


async def test_intents_of_the_interrupted_operation_that_never_sent_anything_are_cancelled(make_env):
    env = await make_env().boot()
    await begin(env)
    created = await env.new_intent(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR,
                                   state=IntentState.CREATED, op_id="op-1")
    ready = await env.new_intent(action=IntentAction.CLOSE, symbol=SYM2, model="op", source=IntentSource.OPERATOR,
                                 state=IntentState.DISPATCH_READY, op_id="op-1")
    other = await env.new_intent(action=IntentAction.CLOSE, symbol="SOLUSDT", model="op",
                                 source=IntentSource.OPERATOR, state=IntentState.CREATED)  # 不属于这个操作
    result = await env.run()
    assert (await env.intents.get(created.id)).state is IntentState.CANCELLED
    assert (await env.intents.get(ready.id)).state is IntentState.REJECTED  # DISPATCH_READY 的迁移表里没有 CANCELLED
    assert (await env.intents.get(other.id)).state is IntentState.CREATED
    assert sorted(result.steps["operations"]["intents_cancelled"]) == sorted([created.id, ready.id])


async def test_an_intent_that_already_has_an_order_is_left_for_the_coordinator_to_recheck(make_env):
    """已经发出过订单的意图不能一撤了之：订单可能已成交，协调者的回查会把它走完。"""
    env = await make_env().boot()
    await begin(env)
    intent = await env.new_intent(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR,
                                  state=IntentState.DISPATCH_READY, op_id="op-1")
    await env.orders.create_pending(intent=intent, leg="c", purpose=OrderPurpose.CLOSE, kind="market",
                                    order_type="MARKET", side=LONG.close_side(), position_side=LONG, qty=D("1"))
    await env.run()
    assert (await env.intents.get(intent.id)).state is IntentState.DISPATCH_READY


async def test_the_finish_goes_through_the_operations_service_so_it_is_audited_and_announced(make_env):
    env = await make_env().boot()
    await begin(env)
    await env.run()
    await env.settle()
    progress = [e for e in env.events if isinstance(e, OperationProgress) and e.op_id == "op-1"]
    assert [e.status for e in progress] == ["running", "aborted"]


async def test_one_operation_that_cannot_be_recorded_does_not_stop_the_others(make_env, monkeypatch):
    env = await make_env().boot()
    await begin(env, "op-a")
    await begin(env, "op-b", OperationType.SWITCH)
    real = env.operations.finish

    async def flaky(op_id, status, *, result, per_position=None):
        if op_id == "op-a":
            raise RuntimeError("账本写失败")
        return await real(op_id, status, result=result, per_position=per_position)

    monkeypatch.setattr(env.operations, "finish", flaky)
    outcome = await env.run()
    assert (await env.operations.get("op-b")).status is OperationStatus.ABORTED  # 另一个照常记为中断
    assert (await env.operations.get("op-a")).status is OperationStatus.RUNNING
    assert [i["op_id"] for i in outcome.steps["operations"]["interrupted"]] == ["op-b"]
    assert outcome.steps["operations"]["errors"][0]["op_id"] == "op-a"
    assert outcome.tradable is False and any("op-a" in p and "账本写失败" in p for p in outcome.problems)
    assert env.registry.holding is False  # 照样释放


async def test_the_locks_are_cleared_even_when_an_operation_cannot_be_recorded(make_env, monkeypatch):
    env = await make_env().boot()
    await env.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner="op-a")
    await begin(env, "op-a")

    async def broken(*args, **kwargs):
        raise RuntimeError("账本写失败")

    monkeypatch.setattr(env.operations, "finish", broken)
    await env.run()
    assert env.modelstate.current().op_locks == frozenset()  # 锁是先清的：持有者进程已经不在了


async def test_a_retrying_close_of_an_interrupted_operation_is_never_resubmitted(make_env):
    """批量操作名下的平仓即使带 retry_until_done，也不能被「平仓中」那一步重新提交——那等于自动续做中断的一键全平。"""
    env = await make_env().boot()
    await begin(env)
    rec = await env.add_position(status="closing")
    env.positions = [pos()]
    intent = await env.new_intent(action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR,
                                  state=IntentState.CREATED, params={"retry_until_done": True}, op_id="op-1",
                                  position_id=rec.id)
    result = await env.run()
    assert env.coordinator.submitted == [] and result.steps["closing"]["resubmitted"] == []
    assert (await env.intents.get(intent.id)).state is IntentState.CANCELLED
    assert result.steps["operations"]["intents_cancelled"] == [intent.id]
