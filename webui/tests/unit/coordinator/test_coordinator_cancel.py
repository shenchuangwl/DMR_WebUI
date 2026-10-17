"""撤单类：ORDER_CANCEL、停止与切换时撤掉未成交的开仓单、等开仓意图全部到终态。"""
from __future__ import annotations

import asyncio

import pytest

from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.core.types import (
    CancelOutcome,
    CancelResult,
    IntentState,
    Known,
    OrderState,
    Unknown,
)

from coordinator_kit import (
    D, LONG, SYM, ack_ok, close_spec, fill_ok, market_view_for, order_cancel_spec, partial_ok, position, spec, tick,
    unknown_write,
)
from dmr_kernel.core.coordinator import CoordinatorStopped

S = IntentState


async def working_open(rig, **spec_kw):
    """发出一张开仓单，交易所受理但还没成交。"""
    rig.adapter.push_write_result("create_market_order", ack_ok())
    rec = await rig.go(spec(**spec_kw))
    assert rec.state is S.ACTIVE
    return rec, await rig.only_order(rec.id)


# ================================================================ ORDER_CANCEL


async def test_order_cancel_cancels_an_unfilled_open_order_and_the_open_intent_is_rejected(rig):
    await rig.start()
    opened, order = await working_open(rig)
    rec = await rig.go(order_cancel_spec(order.client_order_id))
    assert rec.state is S.COMPLETED and "已撤销" in rec.state_reason
    assert rig.sent("cancel_order") == [{"symbol": SYM, "client_order_id": order.client_order_id,
                                         "exchange_order_id": None}]
    assert (await rig.orders.get(order.client_order_id)).state is OrderState.CANCELLED
    done = await rig.intents.get(opened.id)
    assert done.state is S.REJECTED and "没有成交" in done.state_reason  # 作废且无成交
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 0
    assert not rig.coordinator.key_busy(SYM, LONG)


async def test_cancelling_a_partly_filled_open_completes_the_open_intent_with_the_fill(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", partial_ok("0.5"))
    opened = await rig.go(spec())
    assert opened.state is S.ACTIVE
    order = await rig.only_order(opened.id)
    assert order.state is OrderState.PARTIALLY_FILLED and order.filled_qty == D("0.001")
    cancelled = market_view_for(order, status="CANCELED", filled=D("0.001"))
    rig.adapter.push_write_result("cancel_order", CancelResult(CancelOutcome.CANCELLED, order=cancelled))
    await rig.go(order_cancel_spec(order.client_order_id))
    done = await rig.intents.get(opened.id)
    assert done.state is S.COMPLETED and "部分成交" in done.state_reason and "0.001/0.002" in done.state_reason


async def test_order_cancel_of_an_order_that_is_already_gone_is_completed(rig):
    await rig.start()
    opened, order = await working_open(rig)
    rig.adapter.push_write_result("cancel_order", CancelResult(CancelOutcome.NOT_FOUND))
    rec = await rig.go(order_cancel_spec(order.client_order_id))
    assert rec.state is S.COMPLETED and "已不在挂单中" in rec.state_reason


async def test_order_cancel_without_an_order_id_is_rejected(rig):
    await rig.start()
    rec = await rig.go(order_cancel_spec(None))
    assert rec.state is S.REJECTED and "订单编号" in rec.state_reason and rig.names() == []


async def test_an_unknown_cancel_is_settled_by_looking_the_order_up(rig):
    await rig.start()
    opened, order = await working_open(rig)
    rig.adapter.push_write_result("cancel_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rec = await rig.go(order_cancel_spec(order.client_order_id))
    assert rec.state is S.RECONCILING
    assert (await rig.orders.get(order.client_order_id)).state is OrderState.CANCEL_PENDING
    rig.adapter.set_read("fetch_order", Known(market_view_for(order, status="CANCELED", filled=D("0"), avg=None)))
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED
    assert (await rig.intents.get(opened.id)).state is S.REJECTED  # 撤销确认后，开仓意图作废且无成交


async def test_an_order_that_is_still_open_after_the_signature_window_means_the_cancel_failed(rig):
    await rig.start()
    opened, order = await working_open(rig)
    rig.adapter.push_write_result("cancel_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rec = await rig.go(order_cancel_spec(order.client_order_id))
    rig.adapter.set_read("fetch_order", Known(market_view_for(order, status="NEW", filled=D("0"), avg=None)))
    await tick(rig, 5_000)  # 恰好 5 秒：撤单请求还可能生效
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING
    await tick(rig, 1_000)
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "仍然存在" in done.state_reason
    assert (await rig.intents.get(opened.id)).state is S.ACTIVE  # 订单还在，开仓意图不受影响


async def test_an_unreadable_lookup_leaves_an_unknown_cancel_alone(rig):
    await rig.start()
    opened, order = await working_open(rig)
    rig.adapter.push_write_result("cancel_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rec = await rig.go(order_cancel_spec(order.client_order_id))
    rig.adapter.set_read("fetch_order", Unknown("限频"))
    for _ in range(3):
        await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_cancelling_an_order_the_ledger_does_not_know_is_rejected_once_it_cannot_be_settled(rig):
    await rig.start()
    rig.adapter.push_write_result("cancel_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rec = await rig.go(order_cancel_spec("DKTxx-1-zz-o"))
    assert rec.state is S.RECONCILING
    await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "不在账本里" in done.state_reason


# ================================================================ cancel_unfilled_opens


async def test_cancel_unfilled_opens_cancels_the_working_open_order(rig):
    await rig.start()
    opened, order = await working_open(rig)
    count = await rig.coordinator.cancel_unfilled_opens(model=None, reason="用户停止")
    await rig.settle()
    assert count == 1
    assert rig.sent("cancel_order") == [{"symbol": SYM, "client_order_id": order.client_order_id,
                                         "exchange_order_id": None}]
    done = await rig.intents.get(opened.id)
    assert done.state is S.REJECTED and "用户停止" in done.state_reason and "没有成交" in done.state_reason
    assert (await rig.orders.get(order.client_order_id)).state is OrderState.CANCELLED
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 0


async def test_cancel_unfilled_opens_keeps_what_was_already_filled(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", partial_ok("0.5"))
    opened = await rig.go(spec())
    order = await rig.only_order(opened.id)
    cancelled = market_view_for(order, status="CANCELED", filled=D("0.001"))
    rig.adapter.push_write_result("cancel_order", CancelResult(CancelOutcome.CANCELLED, order=cancelled))
    assert await rig.coordinator.cancel_unfilled_opens(model=None, reason="切换") == 1
    await rig.settle()
    done = await rig.intents.get(opened.id)
    assert done.state is S.COMPLETED and "部分成交" in done.state_reason


async def test_the_model_filter_only_cancels_that_models_opens(rig):
    await rig.start()
    opened, order = await working_open(rig)  # 模型 m1
    assert await rig.coordinator.cancel_unfilled_opens(model="m2", reason="x") == 0
    assert rig.sent("cancel_order") == []
    assert await rig.coordinator.cancel_unfilled_opens(model="m1", reason="x") == 1
    await rig.settle()
    assert (await rig.intents.get(opened.id)).state is S.REJECTED


async def test_cancel_unfilled_opens_leaves_closes_and_filled_opens_alone(rig):
    await rig.start()
    rig.positions = [position("ETHUSDT", LONG, "0.5", "3000")]
    rig.adapter.push_write_result("create_market_order", ack_ok())
    close = await rig.go(close_spec(symbol="ETHUSDT"))  # 平仓单挂着没成交
    rig.adapter.push_write_result("create_market_order", fill_ok())
    filled = await rig.go(spec())  # 开仓单已成交
    assert close.state is S.ACTIVE and filled.state is S.COMPLETED
    assert await rig.coordinator.cancel_unfilled_opens(model=None, reason="停止") == 0
    assert rig.sent("cancel_order") == []


async def test_orders_with_an_unknown_fate_are_not_cancelled_they_are_left_to_the_lookup(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(spec())
    assert rec.state is S.RECONCILING
    assert await rig.coordinator.cancel_unfilled_opens(model=None, reason="停止") == 0
    assert rig.sent("cancel_order") == []
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_an_intent_that_was_persisted_but_never_dispatched_is_cancelled(rig):
    await rig.start()
    created, _ = await rig.intents.create(spec())
    assert await rig.coordinator.cancel_unfilled_opens(model=None, reason="切换模型") == 1
    done = await rig.intents.get(created.id)
    assert done.state is S.CANCELLED and "切换模型" in done.state_reason
    assert rig.names() == []


async def test_an_open_queued_behind_another_intent_is_pulled_back_once_it_has_sent_its_order(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hook(name, kw):
        if not entered.is_set():
            entered.set()
            await gate.wait()

    rig.adapter.hook = hook
    close = await rig.coordinator.submit(close_spec())  # 占着 BTCUSDT 多头这个键
    await asyncio.wait_for(entered.wait(), 2)
    queued = await rig.coordinator.submit(spec())  # 同一个键，排在后面
    await asyncio.sleep(0.05)
    canceller = asyncio.create_task(rig.coordinator.cancel_unfilled_opens(model=None, reason="停止"))
    await asyncio.sleep(0.05)
    rig.positions = []  # 平仓之后仓位没了，排队的开仓可以发出
    gate.set()
    count = await asyncio.wait_for(canceller, 5)
    await rig.settle()
    assert count == 1 and len(rig.sent("cancel_order")) == 1  # 排队的开仓发出后，在键锁上排在它后面的撤单把它撤了
    done = await rig.intents.get(queued.intent_id)
    assert done.state is S.REJECTED and "停止" in done.state_reason
    assert (await rig.intents.get(close.intent_id)).state is S.ACTIVE  # 平仓单不动


async def test_a_failing_cancel_does_not_stop_the_other_intents_from_being_cancelled(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", ack_ok())
    rig.adapter.push_write_result("create_market_order", ack_ok())
    first = await rig.go(spec(symbol="BTCUSDT"))
    second = await rig.go(spec(symbol="ETHUSDT"))
    calls = []

    async def hook(name, kw):
        if name == "cancel_order":
            calls.append(kw["symbol"])
            if len(calls) == 1:
                raise RuntimeError("撤单出错")

    rig.adapter.hook = hook
    count = await rig.coordinator.cancel_unfilled_opens(model=None, reason="停止")
    await rig.settle()
    assert count == 1 and sorted(calls) == ["BTCUSDT", "ETHUSDT"]
    states = {(await rig.intents.get(i.id)).symbol: (await rig.intents.get(i.id)).state for i in (first, second)}
    assert sorted(s.value for s in states.values()) == ["ACTIVE", "REJECTED"]


async def test_cancel_unfilled_opens_is_refused_after_a_crash(rig):
    await rig.start()
    rig.crash.arm("open_sent")
    result = await rig.coordinator.submit(spec())
    with pytest.raises(CrashSignal):
        await rig.coordinator.wait_terminal(result.intent_id, 2.0)
    with pytest.raises(CoordinatorStopped):
        await rig.coordinator.cancel_unfilled_opens(model=None, reason="x")


# ================================================================ open_intents_settled


async def test_open_intents_settled_is_true_at_once_when_there_is_no_open_intent(rig):
    await rig.start()
    assert await rig.coordinator.open_intents_settled(0.5) is True


async def test_only_open_intents_count_not_closes_or_stops(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", ack_ok())
    close = await rig.go(close_spec())
    assert close.state is S.ACTIVE
    assert await rig.coordinator.open_intents_settled(0.5) is True


async def test_open_intents_settled_waits_for_the_fill_and_wakes_up_when_it_arrives(rig):
    await rig.start()
    opened, order = await working_open(rig)
    waiter = asyncio.create_task(rig.coordinator.open_intents_settled(5.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await rig.orders.apply_exchange_view(order.client_order_id, market_view_for(order), source="stream")
    assert await asyncio.wait_for(waiter, 5) is True
    assert (await rig.intents.get(opened.id)).state is S.COMPLETED


async def test_open_intents_settled_gives_up_at_the_timeout(rig):
    await rig.start()
    await working_open(rig)
    assert await rig.coordinator.open_intents_settled(0.05) is False


async def test_an_open_with_an_unknown_order_is_not_settled(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(spec())
    assert rec.state is S.RECONCILING
    assert await rig.coordinator.open_intents_settled(0.05) is False
    rig.adapter.set_read("fetch_order", Known(None))  # 之后查清了：不存在
    for _ in range(4):
        await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.REJECTED
    assert await rig.coordinator.open_intents_settled(0.5) is True


async def test_an_open_waiting_in_the_queue_is_not_settled_until_it_ends(rig):
    await rig.start()
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hook(name, kw):
        entered.set()
        await gate.wait()

    rig.adapter.hook = hook
    await rig.coordinator.submit(spec())
    await asyncio.wait_for(entered.wait(), 2)
    assert await rig.coordinator.open_intents_settled(0.05) is False
    gate.set()
    await rig.settle()
