"""一键全平的执行（F5 第 4–12 步）：并发逐仓、时限、五种逐仓结果、收尾复查、中止、操作锁、中断点 6。

时间用 ManualClock 推进（``drive``）：「30 个仓 120 秒内出结果」量的是模拟时间。假协调者的剧本见 m2b_kit.py。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.core.events import OperationProgress
from dmr_kernel.core.types import (
    ExitReason,
    Gate,
    IntentAction,
    IntentSource,
    IntentState,
    OperationStatus,
    OpLock,
    Ownership,
    PerPositionResult,
    PositionStatus,
    RunMode,
)
from dmr_kernel.ops.close_all import MAX_RECHECK_ROUNDS
from dmr_kernel.ops.common import OpRejected
from tests.unit.ops_m2b.m2b_kit import (
    LONG,
    SHORT,
    Behavior,
    FakeIntent,
    build_world,
    caller,
    drive,
    settle_tasks,
)
from tests.unit.ops_m2b.test_m2b_close_all_scope import finished, launch

R = PerPositionResult
D = Decimal


def rows_by_symbol(record) -> dict[str, dict]:
    return {row["symbol"]: row for row in record.per_position}


async def many(ledger, clock, bus, symbols, **config):
    w = await build_world(ledger, clock, bus, **config)
    for symbol in symbols:
        w.add_position(symbol, LONG)
    return w


# ====================================================================== 30 个仓、并发、时限


async def test_thirty_positions_get_per_position_results_within_120_seconds_because_they_run_concurrently(
        ledger, clock, bus):
    symbols = [f"C{i:02d}USDT" for i in range(30)]
    w = await many(ledger, clock, bus, symbols)
    for symbol in symbols:
        w.coordinator.behave(symbol, LONG, Behavior("ok", latency_ms=8_000))  # 串行要 240 秒，并发只要 8 秒
    t0 = clock.now_ms()
    await launch(w)
    record = await finished(w)
    elapsed = clock.now_ms() - t0
    assert elapsed < 120_000 and elapsed <= 12_000  # 模拟时间：不是 30 个仓一个接一个
    assert record.status is OperationStatus.SUCCEEDED
    assert len(record.per_position) == 30 and {r["result"] for r in record.per_position} == {"closed"}
    assert {r["symbol"] for r in record.per_position} == set(symbols)
    assert 0 <= record.result["duration_ms"] <= 12_000
    assert all(v == 0 for v in w.coordinator.exchange.values())


async def test_one_stuck_position_does_not_hold_back_the_others_and_ends_unknown_at_its_own_deadline(
        ledger, clock, bus):
    symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
    w = await many(ledger, clock, bus, symbols, close_one_timeout_s=15)
    w.coordinator.behave("AAAUSDT", LONG, "stuck")
    for symbol in symbols[1:]:
        w.coordinator.behave(symbol, LONG, Behavior("ok", latency_ms=2_000))
    seen_at: dict[str, int] = {}
    bus.subscribe(OperationProgress, lambda e: seen_at.setdefault(e.detail.get("symbol"), e.at_ms),
                  name="progress-times")
    await bus.start()
    t0 = clock.now_ms()
    await launch(w)
    record = await finished(w)
    await bus.drain()
    rows = rows_by_symbol(record)
    assert rows["AAAUSDT"]["result"] == "unknown"
    assert all(rows[s]["result"] == "closed" for s in symbols[1:])
    # 其余三个在 2 秒左右就给出了结果，没有等卡住的那个（它到 15 秒才放弃）
    assert all(seen_at[s] - t0 <= 4_000 for s in symbols[1:])
    assert seen_at["AAAUSDT"] - t0 >= 15_000
    assert clock.now_ms() - t0 < 20_000  # 整体也没有拖到 120 秒
    assert record.status is OperationStatus.PARTIAL
    assert not w.modelstate.current().op_locks  # 结束了就没有锁
    await bus.stop()


async def test_the_single_position_deadline_doubles_when_only_polling_is_available(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_one_timeout_s=15)
    w.coordinator.behave("AAAUSDT", LONG, "stuck")
    await w.modelstate.set_run_mode(RunMode.POLLING_DEGRADED, reason="测试")
    t0 = clock.now_ms()
    await launch(w)
    record = await finished(w)
    waited = clock.now_ms() - t0
    assert 30_000 <= waited < 40_000  # 15 秒的两倍
    assert record.per_position[0]["result"] == "unknown"


async def test_the_single_position_deadline_is_the_plain_one_in_normal_running(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_one_timeout_s=15)
    w.coordinator.behave("AAAUSDT", LONG, "stuck")
    t0 = clock.now_ms()
    await launch(w)
    await finished(w)
    assert 15_000 <= clock.now_ms() - t0 < 25_000


# ====================================================================== 整体时限、中止、锁立即释放


async def test_the_overall_deadline_releases_the_lock_at_once_and_marks_unfinished_positions_unknown(
        ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT", "BBBUSDT"], close_one_timeout_s=120, close_all_timeout_s=30)
    w.coordinator.behave("AAAUSDT", LONG, "stuck")
    w.coordinator.behave("BBBUSDT", LONG, Behavior("ok", latency_ms=1_000))
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    t0 = clock.now_ms()
    await launch(w)
    record = await finished(w)
    assert 30_000 <= clock.now_ms() - t0 < 36_000  # 整体时限 30 秒，不是单仓的 120 秒
    rows = rows_by_symbol(record)
    assert rows["AAAUSDT"]["result"] == "unknown" and rows["BBBUSDT"]["result"] == "closed"
    assert record.result["timed_out"] is True and record.result["aborted"] is False
    assert w.modelstate.current().op_locks == frozenset()  # 超时之后锁立即释放
    assert w.modelstate.current().gate is Gate.STOPPED  # 模型回到已停止
    assert "后台" in rows["AAAUSDT"]["detail"]  # 还没到终态的订单转入后台继续回查


async def test_abort_releases_the_lock_at_once_sends_nothing_more_and_reports_every_position(ledger, clock, bus):
    symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    w = await many(ledger, clock, bus, symbols, close_one_timeout_s=120, close_all_timeout_s=600)
    for symbol in symbols:
        w.coordinator.behave(symbol, LONG, "stuck")
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    await launch(w)
    await settle_tasks()
    assert w.modelstate.current().op_locks == frozenset({OpLock.BULK_CLOSING})
    sent_before = len(w.coordinator.submitted)
    result = await w.close_all.abort(caller(w), op_id="ab-1", target_op_id="ca-1")
    assert result == {"aborted": True}
    # 返回的时候锁已经释放、闸已停止（不是等到整体时限）
    state = w.modelstate.current()
    assert state.op_locks == frozenset() and state.gate is Gate.STOPPED
    record = await w.operations.get("ca-1")
    assert record.status is OperationStatus.ABORTED and record.result["aborted"] is True
    assert {r["result"] for r in record.per_position} == {"unknown"}  # 没到终态的订单记「未知」
    assert len(w.coordinator.submitted) == sent_before  # 不再发新单
    abort_record = await w.operations.get("ab-1")
    assert abort_record.status is OperationStatus.SUCCEEDED


async def test_abort_before_a_position_was_submitted_never_submits_it(ledger, clock, bus):
    """卡在「等同一仓上在途的意图」时中止：这个仓一张单也不发。"""
    w = await many(ledger, clock, bus, ["AAAUSDT", "BBBUSDT"], close_one_timeout_s=120, close_all_timeout_s=600)
    w.coordinator.busy.add(("AAAUSDT", LONG))
    await launch(w)
    await settle_tasks()
    await w.close_all.abort(caller(w), op_id="ab-1", target_op_id="ca-1")
    assert w.coordinator.submits_for("AAAUSDT", LONG) == []
    assert len(w.coordinator.submits_for("BBBUSDT", LONG)) == 1
    rows = rows_by_symbol(await w.operations.get("ca-1"))
    assert rows["AAAUSDT"]["result"] == "unknown" and "未发出" in rows["AAAUSDT"]["detail"]
    assert rows["BBBUSDT"]["result"] == "closed"


async def test_abort_is_refused_when_nothing_is_closing_or_the_target_is_another_operation(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_one_timeout_s=120, close_all_timeout_s=600)
    with pytest.raises(OpRejected) as caught:
        await w.close_all.abort(caller(w), op_id="ab-0", target_op_id="ca-1")
    assert caught.value.code == "not_allowed" and "没有进行中的批量平仓" in caught.value.message
    w.coordinator.behave("AAAUSDT", LONG, "stuck")
    await launch(w)
    await settle_tasks()
    with pytest.raises(OpRejected) as caught:
        await w.close_all.abort(caller(w), op_id="ab-2", target_op_id="some-other-op")
    assert caught.value.code in ("not_found", "not_allowed")
    assert w.modelstate.current().op_locks == frozenset({OpLock.BULK_CLOSING})  # 目标不对，什么也没动
    await w.close_all.abort(caller(w), op_id="ab-3", target_op_id="ca-1")


async def test_aborting_twice_with_the_same_operation_id_returns_the_first_answer(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_one_timeout_s=120, close_all_timeout_s=600)
    w.coordinator.behave("AAAUSDT", LONG, "stuck")
    await launch(w)
    await settle_tasks()
    first = await w.close_all.abort(caller(w), op_id="ab-1", target_op_id="ca-1")
    second = await w.close_all.abort(caller(w), op_id="ab-1", target_op_id="ca-1")
    assert first == second == {"aborted": True}


async def test_after_a_timeout_a_new_close_all_can_start_straight_away(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_one_timeout_s=120, close_all_timeout_s=30)
    w.coordinator.behave("AAAUSDT", LONG, "stuck")
    await launch(w)
    await finished(w)
    w.coordinator.behave("AAAUSDT", LONG, "ok")
    await launch(w, op_id="ca-2")
    again = await finished(w, "ca-2")
    assert again.status is OperationStatus.SUCCEEDED


# ====================================================================== 五种逐仓结果


async def test_the_five_per_position_results(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["OKUSDT", "EXTUSDT", "FAILUSDT", "UNKUSDT", "RESUSDT"],
                   close_one_timeout_s=15)
    w.coordinator.behave("OKUSDT", LONG, "ok")
    w.coordinator.behave("EXTUSDT", LONG, "external")  # 全平过程中止损先被触发：持仓本来就没了
    w.coordinator.behave("FAILUSDT", LONG, Behavior("reject", reason="交易所拒绝：测试"))
    w.coordinator.behave("UNKUSDT", LONG, "stuck")
    w.coordinator.behave("RESUSDT", LONG, "residual")  # 平仓单完成了，持仓还在
    await launch(w)
    record = await finished(w)
    rows = rows_by_symbol(record)
    assert {s: r["result"] for s, r in rows.items()} == {
        "OKUSDT": "closed", "EXTUSDT": "closed_externally", "FAILUSDT": "failed", "UNKUSDT": "unknown",
        "RESUSDT": "residual"}
    assert "交易所拒绝：测试" in rows["FAILUSDT"]["detail"]
    assert rows["RESUSDT"]["detail"]  # 每一行都写明原因
    assert {"position_id", "symbol", "side", "result", "detail"} <= set(rows["OKUSDT"])
    assert record.status is OperationStatus.PARTIAL
    assert rows["OKUSDT"]["side"] == "LONG"


async def test_a_rejected_close_whose_position_turned_out_to_be_flat_counts_as_closed_externally(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"])
    w.coordinator.behave("AAAUSDT", LONG, Behavior("reject", reason="无仓可平"))
    w.coordinator.exchange[("AAAUSDT", LONG)] = D(0)  # 被拒之后即时读数为零
    await launch(w)
    row = rows_by_symbol(await finished(w))["AAAUSDT"]
    assert row["result"] == "closed_externally"


async def test_a_stop_that_fired_first_is_booked_as_closed_externally_even_if_the_intent_completed(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"])
    rec = w.registry.get_active("AAAUSDT", LONG)
    w.registry.exit_reasons[rec.id] = ExitReason.STOP  # 登记簿上这一笔的出场原因是止损：先于我们的平仓单触发
    await launch(w)
    row = rows_by_symbol(await finished(w))["AAAUSDT"]
    assert row["result"] == "closed_externally"


async def test_a_close_filled_by_our_order_is_closed_not_external(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"])
    rec = w.registry.get_active("AAAUSDT", LONG)
    w.registry.exit_reasons[rec.id] = ExitReason.CLOSE_ALL
    await launch(w)
    assert rows_by_symbol(await finished(w))["AAAUSDT"]["result"] == "closed"


async def test_an_unreadable_position_is_unknown_never_zero(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"])
    w.coordinator.unreadable.add(("AAAUSDT", LONG))
    await launch(w)
    row = rows_by_symbol(await finished(w))["AAAUSDT"]
    assert row["result"] == "unknown" and "读数" in row["detail"]


# ====================================================================== 收尾复查最多两轮


async def test_a_position_still_open_after_the_first_round_is_closed_again_in_the_recheck(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"])
    w.coordinator.behave("AAAUSDT", LONG, "residual", "ok")  # 第一轮完成了但仓还在，第二轮平掉
    await launch(w)
    record = await finished(w)
    assert rows_by_symbol(record)["AAAUSDT"]["result"] == "closed"
    sent = w.coordinator.submits_for("AAAUSDT", LONG)
    assert len(sent) == 2 and len({s.idem_key for s in sent}) == 2  # 每一轮一个新的幂等键
    assert record.result["rounds"] == 2


async def test_the_recheck_runs_at_most_two_more_rounds_and_then_leaves_a_residual(ledger, clock, bus):
    assert MAX_RECHECK_ROUNDS == 2
    w = await many(ledger, clock, bus, ["AAAUSDT", "BBBUSDT"])
    w.coordinator.behave("AAAUSDT", LONG, "residual")  # 永远平不掉
    await launch(w)
    record = await finished(w)
    assert len(w.coordinator.submits_for("AAAUSDT", LONG)) == 3  # 首轮 + 最多两轮复查
    assert len(w.coordinator.submits_for("BBBUSDT", LONG)) == 1  # 已平的不会被再平
    assert record.result["rounds"] == 3
    assert rows_by_symbol(record)["AAAUSDT"]["result"] == "residual"


async def test_a_failed_position_is_retried_in_the_recheck_too(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"])
    w.coordinator.behave("AAAUSDT", LONG, Behavior("reject", reason="暂时被拒"), "ok")
    await launch(w)
    record = await finished(w)
    assert rows_by_symbol(record)["AAAUSDT"]["result"] == "closed"
    assert len(w.coordinator.submits_for("AAAUSDT", LONG)) == 2


async def test_an_unknown_position_is_not_resent_but_is_read_once_more_at_the_end(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_one_timeout_s=15)
    w.coordinator.behave("AAAUSDT", LONG, "stuck")
    await launch(w)
    record = await finished(w)
    assert len(w.coordinator.submits_for("AAAUSDT", LONG)) == 1  # 状态未知不重发（重发由协调者按回查规则决定）
    assert rows_by_symbol(record)["AAAUSDT"]["result"] == "unknown"


async def test_an_unknown_position_found_flat_by_the_final_read_is_upgraded_to_closed(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"])
    w.coordinator.unreadable_counts[("AAAUSDT", LONG)] = 1  # 平仓单完成后的第一次读数失败，复查时读到零
    await launch(w)
    row = rows_by_symbol(await finished(w))["AAAUSDT"]
    assert row["result"] == "closed" and "复查" in row["detail"]


async def test_a_stuck_order_whose_position_is_flat_by_the_deadline_is_closed(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_one_timeout_s=15)
    w.coordinator.behave("AAAUSDT", LONG, "stuck")
    await launch(w)
    await settle_tasks()
    w.coordinator.exchange[("AAAUSDT", LONG)] = D(0)  # 回报没到，但交易所上其实已经平了
    row = rows_by_symbol(await finished(w))["AAAUSDT"]
    assert row["result"] == "closed" and "即时持仓已为零" in row["detail"]


# ====================================================================== 在途开仓意图与部分平仓在途


async def test_open_intents_are_waited_for_and_the_list_is_taken_after_the_wait(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"])
    opening = FakeIntent(900, IntentState.ACTIVE, symbol="NEWUSDT", side=LONG, action=IntentAction.OPEN)
    w.intents.opens.append(opening)
    await launch(w)
    await settle_tasks()
    assert w.coordinator.submitted == []  # 开仓意图还没到终态：先不取清单、不发平仓单

    # 开仓成交、登记：新持仓出现在清单里（清单在等待之后才取）
    w.add_position("NEWUSDT", LONG)
    w.intents.opens.clear()
    record = await finished(w)
    assert {r["symbol"] for r in record.per_position} == {"AAAUSDT", "NEWUSDT"}
    assert record.result["unknown_open_orders"] == []


async def test_the_wait_for_open_intents_is_bounded_and_the_leftovers_are_listed_separately(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_all_wait_open_s=10)
    w.intents.opens.append(FakeIntent(901, IntentState.RECONCILING, symbol="STUCKUSDT", side=SHORT,
                                      action=IntentAction.OPEN))
    t0 = clock.now_ms()
    await launch(w)
    record = await finished(w)
    assert 10_000 <= clock.now_ms() - t0 < 20_000  # 最多等 close_all_wait_open_s，然后照常平仓
    assert rows_by_symbol(record)["AAAUSDT"]["result"] == "closed"
    (leftover,) = record.result["unknown_open_orders"]  # 第 4 步之后仍状态未知的开仓单另列一行
    assert leftover["intent_id"] == 901 and leftover["symbol"] == "STUCKUSDT" and leftover["side"] == "SHORT"


async def test_a_position_with_a_partial_close_in_flight_is_waited_for_first(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_one_timeout_s=15)
    w.coordinator.busy.add(("AAAUSDT", LONG))
    await launch(w)
    await settle_tasks()
    assert w.coordinator.submitted == []  # 同一个持仓上有意图在途：先等
    w.coordinator.busy.clear()
    record = await finished(w)
    assert rows_by_symbol(record)["AAAUSDT"]["result"] == "closed"


async def test_the_wait_for_an_in_flight_intent_times_out_and_the_close_goes_out_anyway(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_one_timeout_s=15)
    w.coordinator.busy.add(("AAAUSDT", LONG))
    t0 = clock.now_ms()
    await launch(w)
    await finished(w)
    assert clock.now_ms() - t0 >= 15_000  # 等了 close_one_timeout_s 之后照常发出
    assert len(w.coordinator.submits_for("AAAUSDT", LONG)) == 1


# ====================================================================== 意图的写法与登记簿的暂停

async def test_each_close_is_a_close_intent_from_the_operator_carrying_the_operation_id(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT", "BBBUSDT"])
    pos = w.registry.get_active("AAAUSDT", LONG)
    await launch(w)
    await finished(w)
    spec = w.coordinator.submits_for("AAAUSDT", LONG)[0]
    assert spec.action is IntentAction.CLOSE and spec.model == "op"
    assert spec.source is IntentSource.OPERATOR and spec.operator == "alice"
    assert spec.op_id == "ca-1"  # 重启恢复靠它把中断的批量操作和意图对上
    assert spec.position_id == pos.id and spec.position_generation == pos.generation
    assert spec.params.get("resend_on_unknown") is True
    assert spec.idem_key.startswith("op:ca-1:AAAUSDT:LONG")
    keys = {s.idem_key for s in w.coordinator.submitted}
    assert len(keys) == 2


async def test_the_command_line_principal_closes_with_the_cli_source(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"])
    await launch(w, who=caller(w, session_id=None))
    await finished(w)
    assert w.coordinator.submitted[0].source is IntentSource.CLI
    record = await w.operations.get("ca-1")
    assert record.operator == "cli" and record.session_id is None


async def test_managed_positions_are_paused_while_closing_and_failures_go_back_to_managed(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["OKUSDT", "FAILUSDT"])
    w.coordinator.behave("FAILUSDT", LONG, Behavior("reject", reason="拒绝"))
    ok, fail = w.registry.get_active("OKUSDT", LONG), w.registry.get_active("FAILUSDT", LONG)
    await launch(w)
    await finished(w)
    calls = w.registry.closing_calls
    assert (ok.id, True, "close_all") in calls and (fail.id, True, "close_all") in calls
    assert w.registry.by_id(fail.id).status is PositionStatus.MANAGED  # 失败的保持托管（可单独重试）
    assert (fail.id, False, "close_all_resume") in calls


async def test_unknown_positions_stay_closing_and_resume_after_the_pause_limit(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT"], close_one_timeout_s=15, partial_close_pause_s=30)
    w.coordinator.behave("AAAUSDT", LONG, "stuck")
    pos = w.registry.get_active("AAAUSDT", LONG)
    await launch(w)
    await finished(w)
    assert w.registry.by_id(pos.id).status is PositionStatus.CLOSING  # 订单没到终态：保持平仓中
    await w.close_all.tick()
    assert w.registry.by_id(pos.id).status is PositionStatus.CLOSING  # 还没到 partial_close_pause_s
    clock.advance(31_000)
    await w.close_all.tick()
    assert w.registry.by_id(pos.id).status is PositionStatus.MANAGED  # 最长 partial_close_pause_s 秒后恢复止损更新


async def test_non_managed_taking_over_positions_are_closed_without_being_flagged_closing(ledger, clock, bus):
    w = await build_world(ledger, clock, bus)
    rec = w.add_position("AAAUSDT", LONG, status=PositionStatus.TAKING_OVER, ownership=Ownership.TAKEOVER)
    await launch(w)
    record = await finished(w)
    assert rows_by_symbol(record)["AAAUSDT"]["result"] == "closed"
    assert all(call[0] != rec.id for call in w.registry.closing_calls)


# ====================================================================== 进度事件


async def test_every_position_result_is_published_as_a_progress_frame(ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT", "BBBUSDT"])
    w.coordinator.behave("BBBUSDT", LONG, Behavior("reject", reason="拒绝"))
    await bus.start()
    await launch(w)
    await finished(w)
    await bus.drain()
    frames = [e for e in w.events if isinstance(e, OperationProgress) and e.op_id == "ca-1"]
    per_position = [e for e in frames if e.detail.get("symbol")]
    assert {e.detail["symbol"] for e in per_position} == {"AAAUSDT", "BBBUSDT"}
    assert all(e.op_type == "close_all" and e.status == "running" for e in per_position)
    assert all({"position_id", "side", "result", "detail"} <= set(e.detail) for e in per_position)
    assert frames[-1].status in ("succeeded", "partial", "failed")  # 最后一帧是终态
    await bus.stop()


# ====================================================================== 中断点 6：close_all_midway


async def test_crash_point_6_stops_everything_where_it_is_and_leaves_the_lock_for_the_recovery_to_clear(
        ledger, clock, bus):
    w = await many(ledger, clock, bus, ["AAAUSDT", "BBBUSDT", "CCCUSDT"], close_one_timeout_s=120)
    for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
        w.coordinator.behave(symbol, LONG, Behavior("ok", latency_ms=5_000))
    w.crash.arm("close_all_midway")
    await w.modelstate.set_gate(Gate.RUNNING, reason="t")
    await launch(w)
    for _ in range(200):
        if w.close_all.crashed is not None:
            break
        await asyncio.sleep(0.002)
    assert isinstance(w.close_all.crashed, CrashSignal) and w.close_all.crashed.name == "close_all_midway"
    # 进程「被杀」：没有收尾——锁还在、操作还是进行中、不再发新单
    sent = len(w.coordinator.submitted)
    clock.advance(10_000)
    await settle_tasks()
    assert len(w.coordinator.submitted) == sent
    assert w.modelstate.current().op_locks == frozenset({OpLock.BULK_CLOSING})
    assert (await w.operations.get("ca-1")).status is OperationStatus.RUNNING
    assert 1 <= sent <= 3
    assert "close_all_midway" not in w.crash.armed  # 点只触发一次
