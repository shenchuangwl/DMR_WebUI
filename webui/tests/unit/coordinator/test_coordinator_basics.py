"""协调者的生命周期、提交与幂等、先落盘后下单、同键串行与异键并发、崩溃点。"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from dmr_kernel.core.coordinator import Coordinator, CoordinatorStopped, SubmitResult
from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.core.events import IntentStateChanged
from dmr_kernel.core.types import IntentState, Known, OrderState, Unknown
from dmr_kernel.ledger import repo_orders

from coordinator_kit import (
    D, LONG, SHORT, SYM, close_spec, fill_ok, market_view_for, position, spec, tick, unknown_write,
    wait_until,
)


# ================================================================ 提交与先落盘


async def test_submit_before_start_is_refused(rig):
    with pytest.raises(CoordinatorStopped):
        await rig.coordinator.submit(spec())


async def test_start_twice_is_harmless(rig):
    await rig.start()
    await rig.coordinator.start()
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE
    assert len(rig.sent("create_market_order")) == 1


async def test_submit_persists_the_intent_first_and_returns_without_waiting_for_the_exchange(rig):
    await rig.start()
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hook(name, kw):
        entered.set()
        await gate.wait()

    rig.adapter.hook = hook
    result = await rig.coordinator.submit(spec())
    assert isinstance(result, SubmitResult) and result.created is True and result.state is IntentState.CREATED
    stored = await rig.intents.get(result.intent_id)
    assert stored is not None  # 先落盘
    await asyncio.wait_for(entered.wait(), 2)  # submit 早已返回，派发在后台做到了交易所调用
    assert (await rig.intents.get(result.intent_id)).state is IntentState.DISPATCH_READY
    gate.set()
    await rig.settle()
    assert (await rig.intents.get(result.intent_id)).state is IntentState.ACTIVE


async def test_the_pending_send_order_row_is_on_disk_when_the_request_is_made(rig):
    await rig.start()
    seen = []

    async def hook(name, kw):
        row = await rig.ledger.read(repo_orders.get, kw["client_order_id"])
        intent = await rig.intents.get(row.intent_id)
        seen.append((name, row.state, row.sent_ms, row.qty, intent.state))

    rig.adapter.hook = hook
    await rig.go(spec())
    assert seen == [("create_market_order", OrderState.PENDING_SEND, rig.clock.now_ms(), D("0.002"),
                     IntentState.DISPATCH_READY)]


async def test_the_intent_walks_the_state_machine_in_order(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", fill_ok())
    rec = await rig.go(spec())
    states = [e.state for e in rig.events if isinstance(e, IntentStateChanged) and e.intent_id == rec.id]
    assert states == ["CREATED", "VALIDATED", "RESERVED", "DISPATCH_READY", "ACTIVE", "COMPLETED"]


# ================================================================ 幂等：不产生第二笔订单


async def test_the_same_idempotency_key_never_produces_a_second_order(rig):
    await rig.start()
    s = spec(idem_key="open:m1:BTCUSDT:LONG:1")
    first = await rig.coordinator.submit(s)
    second = await rig.coordinator.submit(s)  # 任务还在跑时重复提交
    await rig.settle()
    third = await rig.coordinator.submit(s)  # 已经结束之后重复提交
    await rig.settle()
    assert (first.created, second.created, third.created) == (True, False, False)
    assert first.intent_id == second.intent_id == third.intent_id
    assert len(rig.sent("create_market_order")) == 1
    assert len(await rig.order_rows(first.intent_id)) == 1


async def test_replay_after_a_restart_never_creates_a_second_order(make_rig):
    first = make_rig()
    await first.start()
    first.crash.arm("open_sent")
    s = spec(idem_key="open:m1:BTCUSDT:LONG:2")
    result = await first.coordinator.submit(s)
    with pytest.raises(CrashSignal):
        await first.coordinator.wait_terminal(result.intent_id, 2.0)
    assert len(first.sent("create_market_order")) == 1

    # 重启：同一个账本，全新的装配（新总线、新服务、新适配层）
    second = make_rig()
    await second.start()
    again = await second.coordinator.submit(s)  # 重放同一个意图
    await second.settle()
    assert again.created is False and again.intent_id == result.intent_id
    assert second.sent("create_market_order") == []  # 没有第二次发单
    rows = await second.order_rows(result.intent_id)
    assert len(rows) == 1 and rows[0].state is OrderState.PENDING_SEND
    # 订单命运未定：意图转入回查，按编号查清之前不重发
    assert (await second.intents.get(result.intent_id)).state is IntentState.RECONCILING
    assert second.coordinator.key_busy(SYM, LONG)


async def _crash_after_sending(make_rig, key: str):
    """第一个进程在发单之后、记结果之前被杀：返回（意图号，账本里的订单行）。"""
    first = make_rig()
    await first.start()
    first.crash.arm("open_sent")
    result = await first.coordinator.submit(spec(idem_key=key))
    with pytest.raises(CrashSignal):
        await first.coordinator.wait_terminal(result.intent_id, 2.0)
    return result.intent_id, await first.only_order(result.intent_id)


async def test_an_intent_orphaned_by_a_restart_is_adopted_and_looked_up_never_resent(make_rig):
    """重启后没有人重新提交：回查轮次接手「有订单行、没有派发任务」的意图，按编号回查。"""
    intent_id, row = await _crash_after_sending(make_rig, "open:m1:BTCUSDT:LONG:orphan-1")
    second = make_rig()
    await second.start()  # 全新的装配，没有任何人重新提交这个意图
    assert second.coordinator.key_busy(SYM, LONG)  # 账本里未到终态的意图算在途
    second.adapter.set_read("fetch_order", lambda **kw: Known(market_view_for(row)))
    await tick(second)  # 订单发出已超过 5 秒：回查一轮
    done = await second.intents.get(intent_id)
    assert done.state is IntentState.COMPLETED and "已全部成交" in done.state_reason
    assert (await second.only_order(intent_id)).state is OrderState.FILLED
    assert second.sent("create_market_order") == []  # 没有第二次发单
    assert not second.coordinator.key_busy(SYM, LONG)


async def test_an_orphan_the_exchange_never_saw_is_not_resent_and_is_rejected_after_three_absences(make_rig):
    intent_id, row = await _crash_after_sending(make_rig, "open:m1:BTCUSDT:LONG:orphan-2")
    second = make_rig()
    await second.start()
    second.adapter.set_read("fetch_order", Known(None))  # 交易所明确答复：没有这张订单
    for _ in range(4):
        await tick(second)
    assert (await second.intents.get(intent_id)).state is IntentState.REJECTED
    assert second.sent("create_market_order") == []  # 开仓单绝不重发
    assert not second.coordinator.key_busy(SYM, LONG)


async def test_an_orphan_without_an_order_row_is_left_alone(make_rig):
    """还没有建订单的意图重启后不会被自动派发：谁也没发过东西，由上层重新提交或让它过期。"""
    first = make_rig()
    await first.start()
    created, _ = await first.intents.create(spec(idem_key="open:m1:BTCUSDT:LONG:orphan-3", ttl_s=None))
    second = make_rig()
    await second.start()
    await tick(second)
    assert (await second.intents.get(created.id)).state is IntentState.CREATED
    assert second.sent("create_market_order") == [] and await second.order_rows(created.id) == []


async def test_a_stopped_coordinator_no_longer_looks_anything_up(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(spec())
    assert rec.state is IntentState.RECONCILING
    await rig.coordinator.stop(0.1)
    rig.adapter.set_read("fetch_order", Known(None))
    rig.clock.advance(60_000)
    await rig.coordinator.resolve_unknown_orders()
    assert rig.adapter.calls("fetch_order") == []  # 停止之后不再回查


async def test_a_terminal_intent_submitted_again_is_returned_as_is(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", fill_ok())
    s = spec(idem_key="open:m1:BTCUSDT:LONG:3")
    rec = await rig.go(s)
    assert rec.state is IntentState.COMPLETED
    again = await rig.coordinator.submit(s)
    await rig.settle()
    assert again.created is False and again.state is IntentState.COMPLETED
    assert len(rig.sent("create_market_order")) == 1


# ================================================================ 同键串行、异键并发


async def test_same_key_dispatches_one_intent_at_a_time(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    log, waiting = [], []

    async def hook(name, kw):
        release = asyncio.Event()
        waiting.append(release)
        log.append("enter")
        await release.wait()
        log.append("exit")

    rig.adapter.hook = hook
    await rig.coordinator.submit(close_spec())
    await rig.coordinator.submit(close_spec())
    await wait_until(lambda: len(waiting) == 1)
    await asyncio.sleep(0.05)
    assert log == ["enter"]  # 第二个在排队，没有进入交易所调用
    waiting[0].set()
    await wait_until(lambda: len(waiting) == 2)
    assert log[:3] == ["enter", "exit", "enter"]  # 第一个做完，第二个才开始
    waiting[1].set()
    await rig.settle()
    assert log == ["enter", "exit", "enter", "exit"]


async def test_different_keys_dispatch_concurrently(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01"), position("ETHUSDT", LONG, "0.5", "3000"),
                     position(SYM, SHORT, "0.01")]
    log, waiting = [], []

    async def hook(name, kw):
        release = asyncio.Event()
        waiting.append(release)
        log.append("enter")
        await release.wait()
        log.append("exit")

    rig.adapter.hook = hook
    await rig.coordinator.submit(close_spec(symbol=SYM, side=LONG))
    await rig.coordinator.submit(close_spec(symbol="ETHUSDT", side=LONG))
    await rig.coordinator.submit(close_spec(symbol=SYM, side=SHORT))
    await wait_until(lambda: len(waiting) == 3)  # 三个键同时在交易所调用里；若串行，这里会超时
    assert log == ["enter", "enter", "enter"]
    for release in waiting:
        release.set()
    await rig.settle()
    assert log.count("exit") == 3


async def test_leverage_check_and_set_is_locked_per_symbol(rig):
    """同一合约的两个方向并发开仓：杠杆与保证金模式的「检查 + 设置」按合约加锁，只设置一次。"""
    await rig.start()
    rig.leverage[SYM] = rig_leverage(20, "cross")

    async def hook(name, kw):
        if name == "set_margin_mode":
            await asyncio.sleep(0.05)  # 让另一个方向的意图有机会在设置过程中检查
            rig.leverage[SYM] = rig_leverage(rig.leverage[SYM].leverage, kw["margin_mode"])
        elif name == "set_leverage":
            rig.leverage[SYM] = rig_leverage(kw["leverage"], rig.leverage[SYM].margin_mode)

    rig.adapter.hook = hook
    long_rec, short_rec = await rig.go_all(spec(side=LONG), spec(side=SHORT))
    assert len(rig.sent("set_margin_mode")) == 1 and len(rig.sent("set_leverage")) == 1
    assert long_rec.state is IntentState.ACTIVE and short_rec.state is IntentState.ACTIVE


def rig_leverage(leverage: int, margin_mode: str):
    from dmr_kernel.core.types import LeverageInfo

    return LeverageInfo(SYM, leverage, margin_mode)


# ================================================================ 等待终态

async def test_wait_terminal_returns_the_current_record_when_the_timeout_passes(rig):
    await rig.start()
    gate = asyncio.Event()

    async def hook(name, kw):
        await gate.wait()

    rig.adapter.hook = hook
    result = await rig.coordinator.submit(spec())
    rec = await rig.coordinator.wait_terminal(result.intent_id, 0.05)
    # 时限到点时返回当刻的记录：意图还没到终态即可（在高负载下可能仍停在 RESERVED，不要求恰好是 DISPATCH_READY）
    assert rec.id == result.intent_id and rec.state not in IntentState.terminal()
    gate.set()
    await rig.settle()


async def test_wait_terminal_wakes_up_as_soon_as_the_intent_ends(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", fill_ok())
    result = await rig.coordinator.submit(spec())
    rec = await rig.coordinator.wait_terminal(result.intent_id, 5.0)
    assert rec.state is IntentState.COMPLETED


async def test_wait_terminal_returns_at_once_for_an_already_terminal_intent(rig):
    await rig.start()
    result = (await rig.go(close_spec())).id  # 没有持仓：直接完成
    rec = await rig.coordinator.wait_terminal(result, 0.01)
    assert rec.state is IntentState.COMPLETED


async def test_wait_terminal_of_an_unknown_intent_raises_key_error(rig):
    await rig.start()
    with pytest.raises(KeyError):
        await rig.coordinator.wait_terminal(987654, 0.01)


# ================================================================ 键是否忙

async def test_key_busy_covers_in_flight_and_unknown_but_not_other_keys(rig):
    await rig.start()
    assert not rig.coordinator.key_busy(SYM, LONG)
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(spec())
    assert rec.state is IntentState.RECONCILING  # 结果未知：回查期间该键算有在途订单
    assert rig.coordinator.key_busy(SYM, LONG)
    assert not rig.coordinator.key_busy(SYM, SHORT)
    assert not rig.coordinator.key_busy("ETHUSDT", LONG)


async def test_key_busy_turns_false_once_the_intent_is_terminal(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", fill_ok())
    rec = await rig.go(spec())
    assert rec.state is IntentState.COMPLETED
    assert not rig.coordinator.key_busy(SYM, LONG)


async def test_key_busy_is_true_while_an_intent_is_queued_behind_a_blocked_one(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    gate = asyncio.Event()

    async def hook(name, kw):
        await gate.wait()

    rig.adapter.hook = hook
    await rig.coordinator.submit(close_spec())
    assert rig.coordinator.key_busy(SYM, LONG)  # submit 一返回，这个键就已经忙了
    gate.set()
    await rig.settle()


async def test_a_resting_stop_order_does_not_make_the_key_busy(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    from coordinator_kit import algo_ok, stop_place_spec

    rig.adapter.push_write_result("create_stop_order", algo_ok())
    rec = await rig.go(stop_place_spec())
    assert rec.state is IntentState.COMPLETED
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.ACKNOWLEDGED  # 止损单挂在交易所，未到终态
    assert not rig.coordinator.key_busy(SYM, LONG)


# ================================================================ 即时持仓读数


async def test_live_position_reads_the_exchange_and_reports_zero_for_an_empty_side(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    assert await rig.coordinator.live_position(SYM, LONG) == Known(D("0.01"))
    assert await rig.coordinator.live_position(SYM, SHORT) == Known(D("0"))
    assert await rig.coordinator.live_position("ETHUSDT", LONG) == Known(D("0"))


async def test_live_position_is_unknown_not_zero_when_the_read_fails(rig):
    await rig.start()
    rig.adapter.set_read("fetch_positions", Unknown("超时"))
    result = await rig.coordinator.live_position(SYM, LONG)
    assert isinstance(result, Unknown) and "超时" in result.reason


# ================================================================ 停止与排空


async def test_stop_refuses_new_intents_and_waits_for_the_dispatch_in_flight(rig):
    await rig.start()
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hook(name, kw):
        entered.set()
        await gate.wait()

    rig.adapter.hook = hook
    result = await rig.coordinator.submit(spec())
    await asyncio.wait_for(entered.wait(), 2)
    stopper = asyncio.create_task(rig.coordinator.stop(5.0))
    await asyncio.sleep(0.05)
    assert not stopper.done()  # 还在等在途的派发步骤
    with pytest.raises(CoordinatorStopped):
        await rig.coordinator.submit(spec())
    gate.set()
    await asyncio.wait_for(stopper, 2)
    assert (await rig.intents.get(result.intent_id)).state is IntentState.ACTIVE  # 在途的这一步做完了


async def test_stop_gives_up_after_the_drain_time_and_leaves_the_order_for_recovery(rig):
    await rig.start()
    entered = asyncio.Event()

    async def hook(name, kw):
        entered.set()
        await asyncio.Event().wait()  # 永远不返回

    rig.adapter.hook = hook
    result = await rig.coordinator.submit(spec())
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(rig.coordinator.stop(0.05), 2)  # 到点就放手
    assert (await rig.intents.get(result.intent_id)).state is IntentState.DISPATCH_READY
    row = await rig.only_order(result.intent_id)
    assert row.state is OrderState.PENDING_SEND  # 订单行已落盘，重启恢复时按编号回查


async def test_stop_is_harmless_when_idle_and_when_repeated(rig):
    await rig.start()
    await rig.coordinator.stop(0.1)
    await rig.coordinator.stop(0.1)
    with pytest.raises(CoordinatorStopped):
        await rig.coordinator.submit(spec())


# ================================================================ 崩溃点

async def test_open_sent_crash_leaves_the_order_row_on_disk(rig):
    await rig.start()
    rig.crash.arm("open_sent")
    result = await rig.coordinator.submit(spec())
    with pytest.raises(CrashSignal) as raised:
        await rig.coordinator.wait_terminal(result.intent_id, 2.0)
    assert raised.value.name == "open_sent"
    assert rig.coordinator.crashed is not None
    row = await rig.only_order(result.intent_id)
    assert row.state is OrderState.PENDING_SEND and row.sent_ms is not None  # 订单行已落盘
    assert len(rig.sent("create_market_order")) == 1  # 请求确实发出去了
    assert (await rig.intents.get(result.intent_id)).state is IntentState.DISPATCH_READY  # 结果没有记下
    assert rig.crash.armed == frozenset()


async def test_after_a_crash_the_dead_coordinator_sends_nothing_more(rig):
    await rig.start()
    rig.crash.arm("open_sent")
    first = await rig.coordinator.submit(spec())
    with pytest.raises(CrashSignal):
        await rig.coordinator.wait_terminal(first.intent_id, 2.0)
    with pytest.raises(CoordinatorStopped):
        await rig.coordinator.submit(spec(symbol="ETHUSDT"))
    await rig.settle()
    assert len(rig.sent("create_market_order")) == 1


# ================================================================ 结构约定


def test_public_method_names_never_reuse_the_adapter_write_method_names():
    write_methods = {"create_market_order", "create_stop_order", "cancel_order", "cancel_algo_order",
                     "set_leverage", "set_margin_mode"}
    public = {name for name, _ in inspect.getmembers(Coordinator) if not name.startswith("_")}
    assert not (public & write_methods)
    for name in ("start", "stop", "submit", "wait_terminal", "key_busy", "open_intents_settled",
                 "cancel_unfilled_opens", "resolve_unknown_orders", "live_position"):
        assert name in public


def test_the_spec_signatures_are_kept():
    assert list(inspect.signature(Coordinator.__init__).parameters) == [
        "self", "adapter", "capability", "ledger", "bus", "clock", "config_get", "modelstate", "registry",
        "intents", "orders", "riskgate", "limits", "rules", "notifier", "crash", "list_check"]
    assert inspect.iscoroutinefunction(Coordinator.submit)
    assert inspect.iscoroutinefunction(Coordinator.wait_terminal)
    assert not inspect.iscoroutinefunction(Coordinator.key_busy)
    assert inspect.iscoroutinefunction(Coordinator.live_position)
    assert inspect.iscoroutinefunction(Coordinator.resolve_unknown_orders)
    assert inspect.iscoroutinefunction(Coordinator.cancel_unfilled_opens)
    assert inspect.iscoroutinefunction(Coordinator.open_intents_settled)
