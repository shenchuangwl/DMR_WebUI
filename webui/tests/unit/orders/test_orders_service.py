"""OrderService：先落盘、写结果、撤单结果、回查、查询、关联。（订单状态的单调合并在 test_orders_merge.py。）"""
from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from dmr_kernel.core.events import OrderStateChanged
from dmr_kernel.core.orders import DuplicateOrder, OrderNotFound, OrderService, model_code
from dmr_kernel.core.types import (
    CancelOutcome,
    CancelResult,
    IntentAction,
    Model,
    OrderOrigin,
    OrderPurpose,
    OrderSide,
    OrderState,
    PositionSide,
    RejectKind,
    Unknown,
    WorkingType,
    WriteAccepted,
    WriteRejected,
    WriteUnknown,
)
from dmr_kernel.exchange import ids
from dmr_kernel.ledger import repo_audit
from dmr_kernel.ledger.records import OrderRecord

O = OrderState


# ---------------------------------------------------------------- 模型代码

@pytest.mark.parametrize("model,code", [("m1", "1"), ("m2", "2"), ("m3", "3"), ("op", "0"), ("protection", "0"),
                                        ("cli", "0"), ("", "0"), ("m4", "0"), ("M1", "0")])
def test_model_code(model, code):
    assert model_code(model) == code


def test_model_code_accepts_the_model_enum():
    assert [model_code(m) for m in Model] == ["1", "2", "3"]


# ---------------------------------------------------------------- 创建（先落盘）

async def test_create_pending_persists_a_pending_send_order_before_any_exchange_call(new_order, orders, adapter,
                                                                                     instance):
    order = await new_order(qty="0.5")
    assert isinstance(order, OrderRecord)
    assert order.state is O.PENDING_SEND
    assert (order.kind, order.order_type, order.leg, order.purpose) == ("market", "MARKET", "o", OrderPurpose.OPEN)
    assert order.qty == Decimal("0.5")
    assert order.filled_qty == Decimal("0") and order.avg_price is None
    assert (order.exchange_order_id, order.algo_id, order.actual_order_id, order.raw_status) == (None, None, None, None)
    assert (order.sent_ms, order.acked_ms, order.last_query_ms, order.absent_count) == (None, None, None, 0)
    assert (order.error_code, order.error_msg, order.resend_of) == (None, None, None)
    assert await orders.get(order.client_order_id) == order  # 已经在账本里
    assert adapter.writes == [] and adapter.read_calls == []  # 落盘不碰交易所


async def test_client_order_id_is_built_from_instance_model_intent_and_leg(new_order, make_intent):
    intent = await make_intent(model="m2")
    order = await new_order(intent=intent, leg="c")
    parsed = ids.parse_client_id(order.client_order_id)
    assert parsed.origin is OrderOrigin.SYSTEM
    assert (parsed.model_code, parsed.intent_id, parsed.leg) == ("2", intent.id, "c")
    assert order.client_order_id.startswith("DKT")  # 前缀 + 设置里的实例号（T）+ 账本随机串
    assert len(order.client_order_id) <= 36


@pytest.mark.parametrize("model,code", [("m1", "1"), ("m2", "2"), ("m3", "3"), ("op", "0")])
async def test_model_code_of_the_intent_goes_into_the_id(new_order, make_intent, model, code):
    order = await new_order(intent=await make_intent(model=model))
    assert ids.parse_client_id(order.client_order_id).model_code == code


async def test_order_copies_symbol_and_links_to_the_intent(new_order, make_intent):
    intent = await make_intent(symbol="ETHUSDT", side=PositionSide.SHORT)
    order = await new_order(intent=intent, position_side=PositionSide.SHORT)
    assert (order.symbol, order.intent_id, order.position_side, order.side) == (
        "ETHUSDT", intent.id, PositionSide.SHORT, OrderSide.SELL)


async def test_create_pending_algo_order_keeps_trigger_data_and_has_no_quantity(new_stop):
    stop = await new_stop(trigger_price="95.5")
    assert (stop.kind, stop.order_type, stop.leg, stop.purpose) == ("algo", "STOP_MARKET", "s", OrderPurpose.STOP)
    assert stop.qty is None and stop.close_position is True
    assert stop.trigger_price == Decimal("95.5") and stop.working_type is WorkingType.CONTRACT_PRICE
    assert stop.state is O.PENDING_SEND


async def test_resend_uses_a_new_leg_and_remembers_the_original(new_order, make_intent):
    intent = await make_intent(action=IntentAction.CLOSE)
    first = await new_order(intent=intent, leg="c", purpose=OrderPurpose.CLOSE)
    again = await new_order(intent=intent, leg=ids.resend_leg("c", 1), purpose=OrderPurpose.CLOSE,
                            resend_of=first.client_order_id)
    assert again.client_order_id != first.client_order_id
    assert again.client_order_id.endswith("-c1")
    assert again.resend_of == first.client_order_id and first.resend_of is None


async def test_creating_the_same_leg_twice_is_refused_and_keeps_one_row(new_order, make_intent, orders):
    intent = await make_intent()
    first = await new_order(intent=intent, leg="o")
    with pytest.raises(DuplicateOrder):
        await new_order(intent=intent, leg="o")
    assert await orders.for_intent(intent.id) == [first]


@pytest.mark.parametrize("overrides", [
    {"kind": "limit"},
    {"qty": Decimal("0")},
    {"qty": Decimal("-1")},
    {"qty": None},  # 市价单必须有数量
    {"close_position": True},  # 只有条件单能是全平型
    {"leg": "O"},  # 分腿必须是小写字母加 0–2 位数字
])
async def test_create_pending_rejects_malformed_market_orders(new_order, overrides):
    with pytest.raises(ValueError):
        await new_order(**overrides)


@pytest.mark.parametrize("overrides", [
    {"qty": Decimal("1")},  # 全平型条件单不得带数量
    {"trigger_price": None},
    {"trigger_price": Decimal("0")},
    {"working_type": None},
])
async def test_create_pending_rejects_malformed_stop_orders(new_order, overrides):
    base = dict(purpose=OrderPurpose.STOP, kind="algo", order_type="STOP_MARKET", qty=None,
                trigger_price=Decimal("90"), working_type=WorkingType.CONTRACT_PRICE, close_position=True, leg="s")
    with pytest.raises(ValueError):
        await new_order(**{**base, **overrides})


async def test_create_pending_publishes_a_pending_send_event(new_order, bus, collector, clock):
    await bus.start()
    order = await new_order(symbol="SOLUSDT", position_side=PositionSide.SHORT)
    await bus.drain()
    [event] = collector.of(OrderStateChanged)
    assert (event.client_order_id, event.state, event.intent_id, event.symbol, event.position_side, event.at_ms) == (
        order.client_order_id, "PENDING_SEND", order.intent_id, "SOLUSDT", PositionSide.SHORT, clock.now_ms())


def test_service_refuses_an_invalid_instance(ledger, bus, clock, adapter):
    with pytest.raises(ValueError):
        OrderService(ledger, bus, clock, adapter, instance="TOOLONG")
    with pytest.raises(ValueError):
        OrderService(ledger, bus, clock, adapter, instance="a-b")


# ---------------------------------------------------------------- record_sent

async def test_record_sent_stamps_the_first_send_time_and_keeps_the_state(new_order, orders, clock):
    order = await new_order()
    clock.advance(1_000)
    sent = await orders.record_sent(order.client_order_id)
    assert sent.state is O.PENDING_SEND  # 发出去之后、回执之前仍是 PENDING_SEND
    assert sent.sent_ms == clock.now_ms()
    clock.advance(500)
    assert (await orders.record_sent(order.client_order_id)).sent_ms == sent.sent_ms  # 第二次不改首次发出时间


async def test_record_sent_does_not_touch_an_order_that_already_moved_on(new_order, orders, order_info):
    order = await new_order()
    await orders.apply_exchange_view(order.client_order_id, order_info(order, status="FILLED", filled="1"),
                                     source="stream")
    after = await orders.record_sent(order.client_order_id)
    assert after.state is O.FILLED and after.sent_ms is None


async def test_record_sent_of_unknown_order(orders):
    with pytest.raises(OrderNotFound):
        await orders.record_sent("DKnope-1-1-o")


# ---------------------------------------------------------------- 写结果

async def _sent(orders, order):
    await orders.record_sent(order.client_order_id)
    return order


async def test_accepted_new_order_is_acknowledged(new_order, orders, order_info, clock):
    order = await _sent(orders, await new_order())
    clock.advance(120)
    done = await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order, status="NEW")))
    assert done.state is O.ACKNOWLEDGED
    assert (done.exchange_order_id, done.raw_status, done.acked_ms) == ("9001", "NEW", clock.now_ms())
    assert done.filled_qty == Decimal("0")


async def test_accepted_partially_filled_order(new_order, orders, order_info):
    order = await _sent(orders, await new_order(qty="1"))
    done = await orders.apply_write_result(order.client_order_id, WriteAccepted(
        order=order_info(order, status="PARTIALLY_FILLED", filled="0.4", avg="100.5")))
    assert done.state is O.PARTIALLY_FILLED
    assert (done.filled_qty, done.avg_price, done.raw_status) == (Decimal("0.4"), Decimal("100.5"), "PARTIALLY_FILLED")


async def test_accepted_filled_order(new_order, orders, order_info):
    order = await _sent(orders, await new_order(qty="1"))
    done = await orders.apply_write_result(order.client_order_id, WriteAccepted(
        order=order_info(order, status="FILLED", filled="1", avg="100")))
    assert done.state is O.FILLED and done.filled_qty == Decimal("1") and done.avg_price == Decimal("100")


@pytest.mark.parametrize("raw", ["EXPIRED", "EXPIRED_IN_MATCH", "CANCELED"])
async def test_accepted_but_ended_without_fill_is_cancelled(new_order, orders, order_info, raw):
    order = await _sent(orders, await new_order())
    status = "EXPIRED" if raw.startswith("EXPIRED") else raw
    done = await orders.apply_write_result(order.client_order_id, WriteAccepted(
        order=order_info(order, status=status, raw=raw)))
    assert done.state is O.CANCELLED and done.raw_status == raw and done.filled_qty == Decimal("0")


@pytest.mark.parametrize("raw", ["EXPIRED", "EXPIRED_IN_MATCH", "CANCELED"])
async def test_accepted_but_ended_with_partial_fill_is_cancelled_and_keeps_the_fill(new_order, orders, order_info, raw):
    order = await _sent(orders, await new_order(qty="1"))
    status = "EXPIRED" if raw.startswith("EXPIRED") else raw
    done = await orders.apply_write_result(order.client_order_id, WriteAccepted(
        order=order_info(order, status=status, raw=raw, filled="0.6", avg="99")))
    assert done.state is O.CANCELLED
    assert (done.filled_qty, done.avg_price) == (Decimal("0.6"), Decimal("99"))  # 已成交部分照记


async def test_accepted_stop_order_is_acknowledged_and_remembers_the_algo_id(new_stop, orders, algo_info):
    stop = await new_stop()
    done = await orders.apply_write_result(stop.client_order_id, WriteAccepted(algo=algo_info(stop, algo_id="5001")))
    assert done.state is O.ACKNOWLEDGED
    assert (done.algo_id, done.raw_status, done.exchange_order_id) == ("5001", "NEW", None)
    assert (await orders.by_algo_id("5001")).client_order_id == stop.client_order_id


async def test_accepted_without_any_view_still_counts_as_acknowledged(new_order, orders, clock):
    order = await _sent(orders, await new_order())
    clock.advance(10)
    done = await orders.apply_write_result(order.client_order_id, WriteAccepted())
    assert done.state is O.ACKNOWLEDGED and done.acked_ms == clock.now_ms()
    assert done.raw_status is None  # 没有交易所的原始状态就不编造


async def test_rejected_records_error_code_and_reason(new_order, orders):
    order = await _sent(orders, await new_order())
    done = await orders.apply_write_result(order.client_order_id, WriteRejected(
        RejectKind.INSUFFICIENT_MARGIN, -2019, "Margin is insufficient.", 400))
    assert done.state is O.REJECTED
    assert (done.error_code, done.error_msg) == (-2019, "Margin is insufficient.")
    assert done.acked_ms is None


async def test_duplicate_client_id_rejection_means_the_order_may_exist(new_order, orders):
    """-4116 编号重复说明先前那次请求可能已经到了交易所：不能当成拒绝，要回查。"""
    order = await _sent(orders, await new_order())
    done = await orders.apply_write_result(order.client_order_id, WriteRejected(
        RejectKind.DUPLICATE_CLIENT_ID, -4116, "ClientOrderId is duplicated.", 400))
    assert done.state is O.SENT_UNKNOWN
    assert done.error_code == -4116


async def test_unknown_result_marks_the_order_sent_unknown(new_order, orders):
    order = await _sent(orders, await new_order())
    done = await orders.apply_write_result(order.client_order_id, WriteUnknown("请求超时", code=-1007, http_status=503))
    assert done.state is O.SENT_UNKNOWN
    assert (done.error_code, done.error_msg) == (-1007, "请求超时")
    assert done.sent_ms is not None


async def test_unknown_result_never_demotes_an_order_the_stream_already_advanced(new_order, orders, order_info):
    order = await _sent(orders, await new_order(qty="1"))
    await orders.apply_exchange_view(order.client_order_id, order_info(order, status="FILLED", filled="1"),
                                     source="stream")
    done = await orders.apply_write_result(order.client_order_id, WriteUnknown("超时"))
    assert done.state is O.FILLED


async def test_rejection_after_a_stream_fill_does_not_override_the_fill(new_order, orders, order_info):
    order = await _sent(orders, await new_order(qty="1"))
    await orders.apply_exchange_view(order.client_order_id,
                                     order_info(order, status="PARTIALLY_FILLED", filled="0.5"), source="stream")
    done = await orders.apply_write_result(order.client_order_id, WriteRejected(RejectKind.OTHER, -1, "x"))
    assert done.state is O.PARTIALLY_FILLED  # 已有成交的订单不会被晚到的拒绝结果改成 REJECTED


async def test_write_result_for_unknown_order(orders):
    with pytest.raises(OrderNotFound):
        await orders.apply_write_result("DKnope-1-1-o", WriteAccepted())


async def test_state_events_are_published_only_when_the_state_changes(new_order, orders, order_info, bus, collector):
    await bus.start()
    order = await _sent(orders, await new_order(qty="1"))
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order, status="NEW")))
    await orders.apply_exchange_view(order.client_order_id, order_info(order, status="NEW"), source="query")  # 没变化
    await orders.apply_exchange_view(order.client_order_id, order_info(order, status="PARTIALLY_FILLED", filled="0.5"),
                                     source="stream")
    await orders.apply_exchange_view(order.client_order_id, order_info(order, status="PARTIALLY_FILLED", filled="0.7"),
                                     source="stream")  # 只是成交量增加，状态没变
    await bus.drain()
    assert [e.state for e in collector.of(OrderStateChanged)] == ["PENDING_SEND", "ACKNOWLEDGED", "PARTIALLY_FILLED"]


# ---------------------------------------------------------------- 撤单结果

async def test_cancelled_outcome_cancels_the_order(new_order, orders, order_info):
    order = await new_order()
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order)))
    done = await orders.apply_cancel_result(order.client_order_id, CancelResult(
        CancelOutcome.CANCELLED, order=order_info(order, status="CANCELED")))
    assert done.state is O.CANCELLED and done.raw_status == "CANCELED"


async def test_cancelled_outcome_keeps_a_partial_fill(new_order, orders, order_info):
    order = await new_order(qty="1")
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order)))
    done = await orders.apply_cancel_result(order.client_order_id, CancelResult(
        CancelOutcome.CANCELLED, order=order_info(order, status="CANCELED", filled="0.3", avg="100")))
    assert done.state is O.CANCELLED and done.filled_qty == Decimal("0.3")


async def test_cancelled_outcome_without_a_view_still_cancels(new_order, orders, order_info):
    order = await new_order()
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order)))
    done = await orders.apply_cancel_result(order.client_order_id, CancelResult(CancelOutcome.CANCELLED))
    assert done.state is O.CANCELLED


async def test_cancelled_algo_order(new_stop, orders, algo_info):
    stop = await new_stop()
    await orders.apply_write_result(stop.client_order_id, WriteAccepted(algo=algo_info(stop)))
    done = await orders.apply_cancel_result(stop.client_order_id, CancelResult(
        CancelOutcome.CANCELLED, algo=algo_info(stop, raw="CANCELED")))
    assert done.state is O.CANCELLED and done.raw_status == "CANCELED"


async def test_cancel_not_found_triggers_a_requery_and_follows_its_answer(new_order, orders, adapter, order_info):
    order = await new_order(qty="1")
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order)))
    # 撤单说「订单不存在」，其实是已经成交了
    adapter.set_read("fetch_order", lambda **kw: order_info(order, status="FILLED", filled="1", avg="100"))
    done = await orders.apply_cancel_result(order.client_order_id, CancelResult(CancelOutcome.NOT_FOUND, code=-2011))
    assert done.state is O.FILLED
    [call] = adapter.calls("fetch_order")
    assert call["client_order_id"] == order.client_order_id and call["symbol"] == "BTCUSDT"


async def test_cancel_not_found_with_unknown_requery_changes_no_state(new_order, orders, adapter, order_info):
    order = await new_order()
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order)))
    adapter.set_read("fetch_order", Unknown("网络错误"))
    done = await orders.apply_cancel_result(order.client_order_id, CancelResult(CancelOutcome.NOT_FOUND))
    assert done.state is O.ACKNOWLEDGED  # 回查不到结果不能当成「已撤」


async def test_cancel_not_found_with_explicit_absence_counts_it_but_keeps_the_state(new_order, orders, adapter,
                                                                                    order_info):
    order = await new_order()
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order)))
    adapter.set_read("fetch_order", lambda **kw: None)
    done = await orders.apply_cancel_result(order.client_order_id, CancelResult(CancelOutcome.NOT_FOUND, code=-2011))
    assert done.state is O.ACKNOWLEDGED and done.absent_count == 1  # 不会据此直接改成已撤


async def test_cancel_unknown_outcome_becomes_cancel_pending(new_order, orders, order_info):
    order = await new_order()
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order)))
    done = await orders.apply_cancel_result(order.client_order_id, CancelResult(
        CancelOutcome.UNKNOWN, message="撤单超时"))
    assert done.state is O.CANCEL_PENDING
    assert done.error_msg == "撤单超时"


async def test_cancel_unknown_outcome_on_a_partly_filled_order(new_order, orders, order_info):
    order = await new_order(qty="1")
    await orders.apply_write_result(order.client_order_id, WriteAccepted(
        order=order_info(order, status="PARTIALLY_FILLED", filled="0.2")))
    done = await orders.apply_cancel_result(order.client_order_id, CancelResult(CancelOutcome.UNKNOWN))
    assert done.state is O.CANCEL_PENDING and done.filled_qty == Decimal("0.2")


async def test_cancel_unknown_outcome_does_not_touch_terminal_or_unconfirmed_orders(new_order, orders, order_info):
    filled = await new_order(qty="1")
    await orders.apply_exchange_view(filled.client_order_id, order_info(filled, status="FILLED", filled="1"),
                                     source="stream")
    assert (await orders.apply_cancel_result(filled.client_order_id, CancelResult(CancelOutcome.UNKNOWN))).state is O.FILLED
    unconfirmed = await new_order(leg="c", purpose=OrderPurpose.CLOSE)
    await orders.apply_write_result(unconfirmed.client_order_id, WriteUnknown("超时"))
    assert (await orders.apply_cancel_result(unconfirmed.client_order_id,
                                             CancelResult(CancelOutcome.UNKNOWN))).state is O.SENT_UNKNOWN


async def test_cancel_pending_resolves_through_requery(new_order, orders, adapter, order_info):
    order = await new_order()
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order)))
    await orders.apply_cancel_result(order.client_order_id, CancelResult(CancelOutcome.UNKNOWN))
    adapter.set_read("fetch_order", lambda **kw: order_info(order, status="CANCELED"))
    [resolved] = await orders.requery_unknown()
    assert resolved.state is O.CANCELLED


# ---------------------------------------------------------------- 交易所视图与回查

async def test_missing_view_counts_absence_but_never_changes_state(new_order, orders, clock):
    order = await _sent(orders, await new_order())
    await orders.apply_write_result(order.client_order_id, WriteUnknown("超时"))
    clock.advance(1_000)
    first = await orders.apply_exchange_view(order.client_order_id, None, source="query")
    second = await orders.apply_exchange_view(order.client_order_id, None, source="query")
    assert (first.absent_count, second.absent_count) == (1, 2)
    assert second.state is O.SENT_UNKNOWN
    assert second.last_query_ms == clock.now_ms()


async def test_missing_view_never_touches_a_terminal_order_state(new_order, orders, order_info, clock):
    order = await new_order(qty="1")
    await orders.apply_exchange_view(order.client_order_id, order_info(order, status="FILLED", filled="1"),
                                     source="stream")
    clock.advance(700)
    after = await orders.apply_exchange_view(order.client_order_id, None, source="query")
    assert after.state is O.FILLED and after.filled_qty == Decimal("1")
    assert after.absent_count == 0  # 已经确认存在过的订单，晚到的「查不到」不计数
    assert after.last_query_ms == clock.now_ms()


async def test_a_real_view_resets_the_absence_streak(new_order, orders, order_info):
    order = await _sent(orders, await new_order())
    await orders.apply_write_result(order.client_order_id, WriteUnknown("超时"))
    for _ in range(2):
        await orders.apply_exchange_view(order.client_order_id, None, source="query")
    seen = await orders.apply_exchange_view(order.client_order_id, order_info(order), source="query")
    assert seen.absent_count == 0 and seen.state is O.ACKNOWLEDGED


@pytest.mark.parametrize("source", ["query", "reconcile"])
async def test_last_query_time_is_stamped_by_active_reads_not_by_the_stream(new_order, orders, order_info, clock,
                                                                            source):
    order = await new_order(qty="1")
    clock.advance(500)
    streamed = await orders.apply_exchange_view(order.client_order_id, order_info(order), source="stream")
    assert streamed.last_query_ms is None
    clock.advance(500)
    queried = await orders.apply_exchange_view(order.client_order_id, order_info(order), source=source)
    assert queried.last_query_ms == clock.now_ms()


async def test_exchange_view_for_unknown_order(orders, order_info, new_order):
    stranger = await new_order()
    with pytest.raises(OrderNotFound):
        await orders.apply_exchange_view("DKnope-1-1-o", order_info(stranger), source="query")


async def test_requery_applies_a_found_order(new_order, orders, adapter, order_info, clock):
    order = await _sent(orders, await new_order(qty="1"))
    await orders.apply_write_result(order.client_order_id, WriteUnknown("超时"))
    adapter.set_read("fetch_order", lambda **kw: order_info(order, status="FILLED", filled="1", avg="101"))
    clock.advance(2_000)
    done = await orders.requery(order.client_order_id)
    assert done.state is O.FILLED and done.avg_price == Decimal("101")
    assert done.last_query_ms == clock.now_ms() and done.absent_count == 0
    assert adapter.calls("fetch_order") == [{"symbol": "BTCUSDT", "client_order_id": order.client_order_id,
                                             "exchange_order_id": None}]
    assert adapter.writes == []  # 回查只用读方法


async def test_requery_counts_an_explicit_absence(new_order, orders, adapter, clock):
    order = await _sent(orders, await new_order())
    await orders.apply_write_result(order.client_order_id, WriteUnknown("超时"))
    adapter.set_read("fetch_order", lambda **kw: None)  # Known(None)：交易所明确答复「不存在」
    clock.advance(1_000)
    done = await orders.requery(order.client_order_id)
    assert done.state is O.SENT_UNKNOWN and done.absent_count == 1 and done.last_query_ms == clock.now_ms()


async def test_requery_unknown_result_only_updates_the_query_time(new_order, orders, adapter, clock):
    order = await _sent(orders, await new_order())
    await orders.apply_write_result(order.client_order_id, WriteUnknown("超时"))
    adapter.set_read("fetch_order", Unknown("连接中断"))
    clock.advance(3_000)
    done = await orders.requery(order.client_order_id)
    assert done.state is O.SENT_UNKNOWN
    assert done.absent_count == 0  # 「未知」绝不能当成「不存在」
    assert done.last_query_ms == clock.now_ms()
    # 夹在两次明确答复之间的未知，既不增加也不清零
    adapter.set_read("fetch_order", lambda **kw: None)
    await orders.requery(order.client_order_id)
    adapter.set_read("fetch_order", Unknown("再次失败"))
    await orders.requery(order.client_order_id)
    assert (await orders.get(order.client_order_id)).absent_count == 1


async def test_requery_without_any_prepared_read_is_unknown_not_absent(new_order, orders):
    order = await _sent(orders, await new_order())
    await orders.apply_write_result(order.client_order_id, WriteUnknown("超时"))
    done = await orders.requery(order.client_order_id)  # 假适配层没预设 → Unknown
    assert done.absent_count == 0 and done.state is O.SENT_UNKNOWN


async def test_requery_algo_order_prefers_the_algo_id_once_known(new_stop, orders, adapter, algo_info):
    stop = await new_stop()
    adapter.set_read("fetch_algo_order", lambda **kw: algo_info(stop, algo_id="5001"))
    first = await orders.requery(stop.client_order_id)
    assert first.state is O.ACKNOWLEDGED and first.algo_id == "5001"
    adapter.set_read("fetch_algo_order", lambda **kw: algo_info(stop, algo_id="5001", raw="CANCELED"))
    second = await orders.requery(stop.client_order_id)
    assert second.state is O.CANCELLED
    calls = adapter.calls("fetch_algo_order")
    assert calls[0] == {"symbol": "BTCUSDT", "client_algo_id": stop.client_order_id, "algo_id": None}
    assert calls[1] == {"symbol": "BTCUSDT", "client_algo_id": None, "algo_id": "5001"}  # 已结束的条件单只能按 algo_id 查
    assert adapter.calls("fetch_order") == []


async def test_requery_of_unknown_order(orders):
    with pytest.raises(OrderNotFound):
        await orders.requery("DKnope-1-1-o")


async def test_requery_unknown_selects_orders_whose_fate_is_open(new_order, orders, adapter, order_info, clock):
    # t = 0：三张「命运未定」的单 —— 超时的、撤单结果未知的、落盘后一直没有结果的
    sent_unknown = await new_order(leg="o")
    await orders.apply_write_result(sent_unknown.client_order_id, WriteUnknown("超时"))
    cancel_pending = await new_order(leg="c", purpose=OrderPurpose.CLOSE)
    await orders.apply_write_result(cancel_pending.client_order_id, WriteAccepted(order=order_info(cancel_pending)))
    await orders.apply_cancel_result(cancel_pending.client_order_id, CancelResult(CancelOutcome.UNKNOWN))
    stale_pending = await new_order(leg="p", purpose=OrderPurpose.PARTIAL_CLOSE)
    clock.advance(5_001)
    boundary = await new_order(leg="d", purpose=OrderPurpose.DUST_CLOSE)  # t = 5001 建的
    clock.advance(5_000)  # t = 10001：boundary 恰好 5 秒；stale_pending 已 10.001 秒
    fresh = await new_order(leg="s", purpose=OrderPurpose.STOP, kind="algo", order_type="STOP_MARKET", qty=None,
                            trigger_price=Decimal("90"), working_type=WorkingType.CONTRACT_PRICE, close_position=True)
    acknowledged = await new_order(leg="o1")
    await orders.apply_write_result(acknowledged.client_order_id, WriteAccepted(order=order_info(acknowledged)))
    done = await new_order(leg="o2")
    await orders.apply_exchange_view(done.client_order_id, order_info(done, status="FILLED", filled="1"),
                                     source="stream")

    queried: list[str] = []

    def answer(**kw):
        queried.append(kw["client_order_id"])
        return Unknown("查不到")

    adapter.set_read("fetch_order", answer)
    adapter.set_read("fetch_algo_order", Unknown("查不到"))
    results = await orders.requery_unknown()

    expected = [sent_unknown.client_order_id, cancel_pending.client_order_id, stale_pending.client_order_id]
    assert sorted(queried) == sorted(expected)
    assert sorted(r.client_order_id for r in results) == sorted(expected)
    for skipped in (boundary, fresh, acknowledged, done):  # 恰好 5 秒不算「超过」；刚建的、已确认的、已结束的都不查
        assert skipped.client_order_id not in queried
    assert adapter.calls("fetch_algo_order") == []
    assert adapter.writes == []


async def test_requery_unknown_asks_about_stop_orders_through_the_algo_endpoint(new_stop, orders, adapter, algo_info):
    stop = await new_stop()
    await orders.apply_write_result(stop.client_order_id, WriteUnknown("超时"))
    adapter.set_read("fetch_algo_order", lambda **kw: algo_info(stop, algo_id="5001"))
    [resolved] = await orders.requery_unknown()
    assert resolved.state is O.ACKNOWLEDGED and resolved.algo_id == "5001"
    assert adapter.calls("fetch_order") == []


async def test_requery_unknown_measures_pending_age_from_the_send_time_when_known(new_order, orders, adapter, clock):
    order = await new_order()
    clock.advance(60_000)
    await orders.record_sent(order.client_order_id)  # 建了一分钟才发出
    clock.advance(4_000)
    assert await orders.requery_unknown() == []  # 发出才 4 秒
    clock.advance(1_001)
    adapter.set_read("fetch_order", Unknown("x"))
    [only] = await orders.requery_unknown()
    assert only.client_order_id == order.client_order_id


async def test_requery_unknown_with_nothing_to_do(orders, adapter):
    assert await orders.requery_unknown() == []
    assert adapter.read_calls == []


# ---------------------------------------------------------------- 判定「不存在」

async def _unknown_open_order(new_order, orders):
    """一张发出后超时、已连续 3 次查到不存在的开仓单。"""
    order = await new_order()
    await orders.record_sent(order.client_order_id)
    order = await orders.apply_write_result(order.client_order_id, WriteUnknown("超时"))
    return dataclasses.replace(order, absent_count=3)


async def test_judge_absent_true_only_when_all_three_hold(new_order, orders):
    order = await _unknown_open_order(new_order, orders)
    late = order.sent_ms + 5_000 + 15_000 + 1
    assert orders.judge_absent(order, now_ms=late, grace_s=15, position_unchanged=True) is True


async def test_judge_absent_false_inside_the_signing_window_plus_grace(new_order, orders):
    order = await _unknown_open_order(new_order, orders)
    for now_ms in (order.sent_ms, order.sent_ms + 5_000, order.sent_ms + 20_000):  # 20 000 = 窗口 + 15 秒，恰好不算「超过」
        assert orders.judge_absent(order, now_ms=now_ms, grace_s=15, position_unchanged=True) is False
    assert orders.judge_absent(order, now_ms=order.sent_ms + 20_001, grace_s=15, position_unchanged=True) is True


async def test_judge_absent_grace_period_is_added_to_the_window(new_order, orders):
    order = await _unknown_open_order(new_order, orders)
    assert orders.judge_absent(order, now_ms=order.sent_ms + 5_001, grace_s=0, position_unchanged=True) is True
    assert orders.judge_absent(order, now_ms=order.sent_ms + 5_001, grace_s=1, position_unchanged=True) is False
    assert orders.judge_absent(order, now_ms=order.sent_ms + 6_001, grace_s=1, position_unchanged=True) is True


@pytest.mark.parametrize("count,expected", [(0, False), (1, False), (2, False), (3, True), (4, True)])
async def test_judge_absent_needs_three_consecutive_absences(new_order, orders, count, expected):
    order = dataclasses.replace(await _unknown_open_order(new_order, orders), absent_count=count)
    assert orders.judge_absent(order, now_ms=order.sent_ms + 60_000, grace_s=15, position_unchanged=True) is expected


async def test_judge_absent_false_when_the_position_changed(new_order, orders):
    order = await _unknown_open_order(new_order, orders)
    assert orders.judge_absent(order, now_ms=order.sent_ms + 60_000, grace_s=15, position_unchanged=False) is False


@pytest.mark.parametrize("state", [O.ACKNOWLEDGED, O.PARTIALLY_FILLED, O.CANCEL_PENDING, O.FILLED, O.CANCELLED,
                                   O.REJECTED])
async def test_judge_absent_only_applies_to_orders_the_exchange_never_confirmed(new_order, orders, state):
    """交易所确认过存在的订单（或已有结论的订单），连续「不存在」的计数不能当成「根本没到交易所」的依据。"""
    order = dataclasses.replace(await _unknown_open_order(new_order, orders), state=state)
    assert orders.judge_absent(order, now_ms=order.sent_ms + 60_000, grace_s=15, position_unchanged=True) is False


async def test_judge_absent_uses_the_creation_time_when_the_order_was_never_marked_sent(new_order, orders):
    order = dataclasses.replace(await new_order(), absent_count=3)
    assert order.sent_ms is None
    assert orders.judge_absent(order, now_ms=order.created_ms + 20_000, grace_s=15, position_unchanged=True) is False
    assert orders.judge_absent(order, now_ms=order.created_ms + 20_001, grace_s=15, position_unchanged=True) is True


# ---------------------------------------------------------------- 标记拒绝

async def test_mark_rejected_records_the_reason_audits_and_publishes(new_order, orders, ledger, bus, collector):
    await bus.start()
    order = await _sent(orders, await new_order())
    await orders.apply_write_result(order.client_order_id, WriteUnknown("超时"))
    done = await orders.mark_rejected(order.client_order_id, reason="连续 3 次查到不存在，且该侧持仓没有变化")
    assert done.state is O.REJECTED
    assert done.error_msg == "连续 3 次查到不存在，且该侧持仓没有变化"
    await bus.drain()
    assert collector.of(OrderStateChanged)[-1].state == "REJECTED"
    [audit] = await ledger.read(repo_audit.recent, action="order.mark_rejected")
    assert audit.target == order.client_order_id
    assert audit.before["state"] == "SENT_UNKNOWN" and audit.after["state"] == "REJECTED"


@pytest.mark.parametrize("status,filled", [("FILLED", "1"), ("CANCELED", "0")])
async def test_mark_rejected_never_overrides_a_terminal_order(new_order, orders, order_info, status, filled):
    order = await new_order(qty="1")
    await orders.apply_exchange_view(order.client_order_id, order_info(order, status=status, filled=filled),
                                     source="stream")
    before = await orders.get(order.client_order_id)
    after = await orders.mark_rejected(order.client_order_id, reason="不该生效")
    assert after.state is before.state and after.error_msg == before.error_msg


async def test_mark_rejected_refuses_an_order_that_already_has_fills(new_order, orders, order_info):
    order = await new_order(qty="1")
    await orders.apply_exchange_view(order.client_order_id,
                                     order_info(order, status="PARTIALLY_FILLED", filled="0.5"), source="stream")
    after = await orders.mark_rejected(order.client_order_id, reason="不该生效")
    assert after.state is O.PARTIALLY_FILLED and after.filled_qty == Decimal("0.5")  # 有成交就证明订单存在


async def test_mark_rejected_of_unknown_order(orders):
    with pytest.raises(OrderNotFound):
        await orders.mark_rejected("DKnope-1-1-o", reason="x")


# ---------------------------------------------------------------- 查询

async def test_lookups_by_every_identifier(new_order, new_stop, orders, order_info, algo_info):
    order = await new_order()
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order, exchange_order_id="777")))
    stop = await new_stop()
    await orders.apply_write_result(stop.client_order_id, WriteAccepted(algo=algo_info(stop, algo_id="555")))
    await orders.link_actual_order("555", "888")

    assert (await orders.get(order.client_order_id)).exchange_order_id == "777"
    assert (await orders.by_exchange_order_id("777")).client_order_id == order.client_order_id
    assert (await orders.by_algo_id("555")).client_order_id == stop.client_order_id
    assert (await orders.by_actual_order_id("888")).client_order_id == stop.client_order_id
    for missing in (await orders.get("DKnope-1-1-o"), await orders.by_exchange_order_id("1"),
                    await orders.by_algo_id("1"), await orders.by_actual_order_id("1")):
        assert missing is None


async def test_non_terminal_filters(new_order, new_stop, orders, order_info, make_intent):
    long_open = await new_order(symbol="BTCUSDT", position_side=PositionSide.LONG)
    short_close = await new_order(symbol="BTCUSDT", position_side=PositionSide.SHORT, leg="c",
                                  purpose=OrderPurpose.CLOSE)
    eth_stop = await new_stop(symbol="ETHUSDT")
    done = await new_order(symbol="BTCUSDT", position_side=PositionSide.LONG, leg="c")
    await orders.apply_exchange_view(done.client_order_id, order_info(done, status="FILLED", filled="1"), source="stream")

    def ids_of(rows):
        return [r.client_order_id for r in rows]

    assert ids_of(await orders.non_terminal()) == [long_open.client_order_id, short_close.client_order_id,
                                                   eth_stop.client_order_id]
    assert ids_of(await orders.non_terminal(symbol="BTCUSDT")) == [long_open.client_order_id, short_close.client_order_id]
    assert ids_of(await orders.non_terminal(side=PositionSide.LONG)) == [long_open.client_order_id,
                                                                         eth_stop.client_order_id]
    # 买卖方向也能筛：平空仓是买入，所以空仓的平仓单与多仓的开仓单同属 BUY；多仓的止损是卖出
    assert ids_of(await orders.non_terminal(side=OrderSide.BUY)) == [long_open.client_order_id,
                                                                     short_close.client_order_id]
    assert ids_of(await orders.non_terminal(side=OrderSide.SELL)) == [eth_stop.client_order_id]
    assert ids_of(await orders.non_terminal(purpose=OrderPurpose.STOP)) == [eth_stop.client_order_id]
    assert ids_of(await orders.non_terminal(symbol="BTCUSDT", side=PositionSide.SHORT,
                                            purpose=OrderPurpose.CLOSE)) == [short_close.client_order_id]
    assert await orders.non_terminal(symbol="NOPEUSDT") == []


async def test_non_terminal_includes_every_open_state(new_order, orders, order_info):
    pending = await new_order(leg="o")
    unknown = await new_order(leg="c")
    await orders.apply_write_result(unknown.client_order_id, WriteUnknown("x"))
    acked = await new_order(leg="p")
    await orders.apply_write_result(acked.client_order_id, WriteAccepted(order=order_info(acked)))
    partial = await new_order(leg="d")
    await orders.apply_exchange_view(partial.client_order_id, order_info(partial, status="PARTIALLY_FILLED", filled="0.5"),
                                     source="stream")
    pending_cancel = await new_order(leg="o1")
    await orders.apply_write_result(pending_cancel.client_order_id, WriteAccepted(order=order_info(pending_cancel)))
    await orders.apply_cancel_result(pending_cancel.client_order_id, CancelResult(CancelOutcome.UNKNOWN))
    rejected = await new_order(leg="o2")
    await orders.mark_rejected(rejected.client_order_id, reason="x")

    open_ids = {r.client_order_id: r.state for r in await orders.non_terminal()}
    assert open_ids == {pending.client_order_id: O.PENDING_SEND, unknown.client_order_id: O.SENT_UNKNOWN,
                        acked.client_order_id: O.ACKNOWLEDGED, partial.client_order_id: O.PARTIALLY_FILLED,
                        pending_cancel.client_order_id: O.CANCEL_PENDING}


async def test_for_intent_lists_the_orders_of_one_intent(new_order, make_intent, orders):
    intent = await make_intent()
    other = await make_intent()
    a = await new_order(intent=intent, leg="o")
    b = await new_order(intent=intent, leg="s", purpose=OrderPurpose.STOP, kind="algo", order_type="STOP_MARKET",
                        qty=None, trigger_price=Decimal("90"), working_type=WorkingType.MARK_PRICE, close_position=True)
    await new_order(intent=other, leg="o")
    assert [r.client_order_id for r in await orders.for_intent(intent.id)] == [a.client_order_id, b.client_order_id]
    assert await orders.for_intent(999_999) == []


# ---------------------------------------------------------------- 条件单触发后订单号

async def test_link_actual_order_records_the_triggered_order_id(new_stop, orders, algo_info, bus, collector):
    await bus.start()
    stop = await new_stop()
    await orders.apply_write_result(stop.client_order_id, WriteAccepted(algo=algo_info(stop, algo_id="5001")))
    await bus.drain()
    collector.events.clear()
    assert await orders.link_actual_order("5001", "9100") is None
    linked = await orders.by_algo_id("5001")
    assert linked.actual_order_id == "9100"
    assert (await orders.by_actual_order_id("9100")).client_order_id == stop.client_order_id
    await bus.drain()
    assert collector.of(OrderStateChanged) == []  # 只是多了一个编号，状态没变


async def test_link_actual_order_is_idempotent_and_never_overwrites(new_stop, orders, algo_info):
    stop = await new_stop()
    await orders.apply_write_result(stop.client_order_id, WriteAccepted(algo=algo_info(stop, algo_id="5001")))
    await orders.link_actual_order("5001", "9100")
    await orders.link_actual_order("5001", "9100")
    await orders.link_actual_order("5001", "9200")  # 已经关联过，不改
    assert (await orders.by_algo_id("5001")).actual_order_id == "9100"


async def test_link_actual_order_ignores_algo_ids_that_are_not_ours(orders):
    assert await orders.link_actual_order("424242", "9100") is None
    assert await orders.by_actual_order_id("9100") is None
