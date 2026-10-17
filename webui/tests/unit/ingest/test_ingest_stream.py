"""Ingest 对账户数据流事件的订阅：订单事件（有成交的入账、无成交的合并）、条件单事件（转发与关联触发后订单号）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.events import AlgoOrderObserved, FillIngested, OrderStateChanged, StreamOrderReceived
from dmr_kernel.core.types import OrderOrigin, OrderState, StreamOrder

D = Decimal
O = OrderState


async def start(ingest, bus):
    await bus.start()
    await ingest.start()


def without_stream_events(collector):
    return [k for k in collector.kinds() if not k.startswith("Stream")]


# ---------------------------------------------------------------- 生命周期

async def test_start_is_idempotent_and_stop_unsubscribes(ingest, bus, acked_order, stream_order, trade, fills):
    await start(ingest, bus)
    await ingest.start()  # 重复启动不会重复订阅
    order = await acked_order(qty="1")
    bus.publish(stream_order(order, execution="TRADE", status="PARTIALLY_FILLED", filled="0.4", avg="100",
                             fill=trade(order, trade_id="T1", qty="0.4")))
    await bus.drain()
    assert len(await fills()) == 1

    await ingest.stop()
    await ingest.stop()  # 重复停止无害
    bus.publish(stream_order(order, execution="TRADE", status="FILLED", filled="1", avg="100",
                             fill=trade(order, trade_id="T2", qty="0.6")))
    await bus.drain()
    assert len(await fills()) == 1  # 停止之后不再处理


async def test_nothing_is_ingested_before_start(ingest, bus, acked_order, stream_order, trade, fills):
    await bus.start()
    order = await acked_order(qty="1")
    bus.publish(stream_order(order, execution="TRADE", status="PARTIALLY_FILLED", filled="0.4",
                             fill=trade(order, trade_id="T1", qty="0.4")))
    await bus.drain()
    assert await fills() == []


# ---------------------------------------------------------------- 有成交的订单事件

async def test_trade_event_is_ingested_on_the_stream_path_and_merges_the_order_view(
        ingest, bus, collector, acked_order, stream_order, trade, fills, orders):
    await start(ingest, bus)
    order = await acked_order(qty="1")
    await bus.drain()
    collector.events.clear()

    bus.publish(stream_order(order, execution="TRADE", status="PARTIALLY_FILLED", filled="0.4", avg="100.5",
                             fill=trade(order, trade_id="T1", qty="0.4", price="100.5")))
    await bus.drain()

    [row] = await fills()
    assert (row.trade_id, row.path, row.origin, row.client_order_id) == ("T1", "stream", OrderOrigin.SYSTEM,
                                                                          order.client_order_id)
    merged = await orders.get(order.client_order_id)
    assert (merged.state, merged.raw_status, merged.filled_qty, merged.avg_price) == (
        O.PARTIALLY_FILLED, "PARTIALLY_FILLED", D("0.4"), D("100.5"))
    assert without_stream_events(collector) == ["FillIngested", "OrderStateChanged"]
    assert collector.of(FillIngested)[0].path == "stream"


async def test_final_trade_event_fills_the_order(ingest, bus, acked_order, stream_order, trade, orders):
    await start(ingest, bus)
    order = await acked_order(qty="1")
    bus.publish(stream_order(order, execution="TRADE", status="PARTIALLY_FILLED", filled="0.4", avg="100",
                             fill=trade(order, trade_id="T1", qty="0.4")))
    bus.publish(stream_order(order, execution="TRADE", status="FILLED", filled="1", avg="100.2",
                             fill=trade(order, trade_id="T2", qty="0.6", price="100.3")))
    await bus.drain()
    done = await orders.get(order.client_order_id)
    assert (done.state, done.raw_status, done.filled_qty) == (O.FILLED, "FILLED", D("1.0"))


async def test_a_replayed_trade_event_is_ingested_once(ingest, bus, collector, acked_order, stream_order, trade, fills,
                                                       orders):
    await start(ingest, bus)
    order = await acked_order(qty="1")
    event = stream_order(order, execution="TRADE", status="PARTIALLY_FILLED", filled="0.4", avg="100",
                         fill=trade(order, trade_id="T1", qty="0.4"))
    for _ in range(3):  # 重连后交易所重放
        bus.publish(event)
    await bus.drain()
    assert len(await fills()) == 1 and len(collector.of(FillIngested)) == 1
    assert (await orders.get(order.client_order_id)).filled_qty == D("0.4")


async def test_a_trade_event_the_backfill_already_ingested_still_brings_its_order_view(
        ingest, bus, collector, acked_order, stream_order, trade, fills, orders):
    await start(ingest, bus)
    order = await acked_order(qty="1")
    t = trade(order, trade_id="T1", qty="1", price="100")
    assert await ingest.ingest_trade(t, client_order_id=None, path="rest") is not None  # 补拉先到：没有订单视图
    before = await orders.get(order.client_order_id)
    assert (before.state, before.raw_status) == (O.FILLED, "NEW")
    await bus.drain()
    collector.events.clear()

    bus.publish(stream_order(order, execution="TRADE", status="FILLED", filled="1", avg="100", fill=t))
    await bus.drain()

    assert len(await fills()) == 1 and collector.of(FillIngested) == []  # 成交不重复入账
    assert (await orders.get(order.client_order_id)).raw_status == "FILLED"  # 但交易所的最新状态要记下来


async def test_trade_event_of_an_external_order_is_recorded_without_touching_orders(
        ingest, bus, collector, trade, fills, acked_order, stranger, order_info):
    await start(ingest, bus)
    mine = await acked_order(qty="1", exchange_order_id="9001")
    await bus.drain()
    collector.events.clear()
    outsider = stranger("web_ext", qty="2")
    view = order_info(outsider, status="FILLED", filled="2", avg="50", exchange_order_id="123")
    bus.publish(StreamOrderReceived(StreamOrder(order=view, execution="TRADE", event_time_ms=1,
                                                fill=trade(exchange_order_id="123", qty="2", price="50"))))
    await bus.drain()
    [row] = await fills()
    assert (row.origin, row.client_order_id) == (OrderOrigin.EXTERNAL, "web_ext")
    assert without_stream_events(collector) == ["FillIngested"]  # 我们自己的订单没有任何变化
    assert mine.state is O.ACKNOWLEDGED


# ---------------------------------------------------------------- 无成交的订单事件

async def test_new_event_acknowledges_a_system_order(ingest, bus, new_order, stream_order, orders, fills, collector):
    await start(ingest, bus)
    order = await new_order(qty="1")  # 数据流先于下单回执
    bus.publish(stream_order(order, execution="NEW", status="NEW", exchange_order_id="9001"))
    await bus.drain()
    merged = await orders.get(order.client_order_id)
    assert (merged.state, merged.exchange_order_id, merged.raw_status, merged.last_query_ms) == (
        O.ACKNOWLEDGED, "9001", "NEW", None)  # 数据流事件不算回查
    assert await fills() == []
    assert [e.state for e in collector.of(OrderStateChanged)] == ["PENDING_SEND", "ACKNOWLEDGED"]


@pytest.mark.parametrize("execution,status,raw,expected", [
    ("CANCELED", "CANCELED", "CANCELED", O.CANCELLED),
    ("EXPIRED", "EXPIRED", "EXPIRED", O.CANCELLED),
    ("EXPIRED", "EXPIRED", "EXPIRED_IN_MATCH", O.CANCELLED),
    ("REJECTED", "REJECTED", "REJECTED", O.REJECTED),
])
async def test_terminal_events_without_fills_end_a_system_order(ingest, bus, acked_order, stream_order, orders, fills,
                                                                execution, status, raw, expected):
    await start(ingest, bus)
    order = await acked_order(qty="1")
    bus.publish(stream_order(order, execution=execution, status=status, raw=raw))
    await bus.drain()
    merged = await orders.get(order.client_order_id)
    assert (merged.state, merged.raw_status, merged.filled_qty) == (expected, raw, D("0"))
    assert await fills() == []


async def test_events_of_orders_that_are_not_ours_are_ignored(ingest, bus, order_info, stranger, fills, collector):
    await start(ingest, bus)
    outsider = stranger("web_ext")
    for execution, status in (("NEW", "NEW"), ("CANCELED", "CANCELED")):
        view = order_info(outsider, status=status, exchange_order_id="123")
        bus.publish(StreamOrderReceived(StreamOrder(order=view, execution=execution, fill=None, event_time_ms=1)))
    await bus.drain()
    assert await fills() == []
    assert collector.of(OrderStateChanged) == []


@pytest.mark.parametrize("order_of_arrival", ["expired_then_trade", "trade_then_expired"])
async def test_expired_and_trade_events_converge_in_any_arrival_order(ingest, bus, acked_order, stream_order, trade,
                                                                      orders, fills, order_of_arrival):
    """市价单部分成交后余量被交易所作废：不管先收到作废事件还是先收到成交回报，结果都一样。"""
    await start(ingest, bus)
    order = await acked_order(qty="1")
    expired = stream_order(order, execution="EXPIRED", status="EXPIRED", raw="EXPIRED", filled="0.6", avg="99")
    traded = stream_order(order, execution="TRADE", status="PARTIALLY_FILLED", filled="0.6", avg="99",
                          fill=trade(order, trade_id="T1", qty="0.6", price="99"))
    for event in ((expired, traded) if order_of_arrival == "expired_then_trade" else (traded, expired)):
        bus.publish(event)
    await bus.drain()
    final = await orders.get(order.client_order_id)
    assert (final.state, final.raw_status, final.filled_qty, final.avg_price) == (
        O.CANCELLED, "EXPIRED", D("0.6"), D("99"))
    assert len(await fills()) == 1


async def test_expired_event_that_does_not_carry_the_fill_still_converges(ingest, bus, acked_order, stream_order,
                                                                          trade, orders):
    await start(ingest, bus)
    order = await acked_order(qty="1")
    bus.publish(stream_order(order, execution="EXPIRED", status="EXPIRED", raw="EXPIRED", filled="0"))
    bus.publish(stream_order(order, execution="TRADE", status="PARTIALLY_FILLED", filled="0.6", avg="99",
                             fill=trade(order, trade_id="T1", qty="0.6", price="99")))
    await bus.drain()
    final = await orders.get(order.client_order_id)
    assert (final.state, final.raw_status, final.filled_qty) == (O.CANCELLED, "EXPIRED", D("0.6"))


# ---------------------------------------------------------------- 条件单事件

async def test_algo_event_is_republished_as_observed_from_the_stream(ingest, bus, collector, acked_stop, stream_algo,
                                                                     clock):
    await start(ingest, bus)
    stop = await acked_stop()
    await bus.drain()
    collector.events.clear()
    clock.advance(40)
    event = stream_algo(stop, raw="NEW")
    bus.publish(event)
    await bus.drain()
    [observed] = collector.of(AlgoOrderObserved)
    assert (observed.algo, observed.source, observed.at_ms) == (event.payload.algo, "stream", clock.now_ms())


async def test_algo_event_with_the_triggered_order_id_links_it_to_our_stop(ingest, bus, collector, acked_stop,
                                                                           stream_algo, orders):
    await start(ingest, bus)
    stop = await acked_stop(algo_id="5001")
    await bus.drain()
    collector.events.clear()
    bus.publish(stream_algo(stop, raw="TRIGGERED", algo_id="5001", actual_order_id="7001"))
    await bus.drain()
    linked = await orders.by_actual_order_id("7001")
    assert linked.client_order_id == stop.client_order_id
    assert (linked.state, linked.raw_status) == (O.ACKNOWLEDGED, "TRIGGERED")  # 已触发但触发后的订单还没成交
    assert collector.of(OrderStateChanged) == []  # 状态没变
    assert len(collector.of(AlgoOrderObserved)) == 1


async def test_algo_event_without_a_triggered_order_id_links_nothing(ingest, bus, acked_stop, stream_algo, orders):
    await start(ingest, bus)
    stop = await acked_stop(algo_id="5001")
    bus.publish(stream_algo(stop, raw="NEW", algo_id="5001"))
    await bus.drain()
    assert (await orders.get(stop.client_order_id)).actual_order_id is None


async def test_algo_event_of_a_stop_that_is_not_ours_is_only_republished(ingest, bus, collector, acked_stop,
                                                                          stream_algo, orders):
    await start(ingest, bus)
    mine = await acked_stop(algo_id="5001")
    foreign = stream_algo(mine, raw="TRIGGERED", algo_id="6666", actual_order_id="7777",
                          client_algo_id="user_stop_1")
    await bus.drain()
    collector.events.clear()
    bus.publish(foreign)
    await bus.drain()
    assert await orders.by_actual_order_id("7777") is None
    assert await orders.by_algo_id("6666") is None
    assert len(collector.of(AlgoOrderObserved)) == 1 and collector.of(OrderStateChanged) == []


async def test_algo_events_move_our_stop_forward_and_never_back(ingest, bus, collector, acked_stop, stream_algo,
                                                                orders):
    await start(ingest, bus)
    stop = await acked_stop(algo_id="5001")
    await bus.drain()
    collector.events.clear()
    bus.publish(stream_algo(stop, raw="CANCELED", algo_id="5001"))  # 用户在交易所把止损撤了
    bus.publish(stream_algo(stop, raw="NEW", algo_id="5001"))  # 迟到的旧状态
    await bus.drain()
    final = await orders.get(stop.client_order_id)
    assert (final.state, final.raw_status) == (O.CANCELLED, "CANCELED")
    assert [e.state for e in collector.of(OrderStateChanged)] == ["CANCELLED"]
    assert len(collector.of(AlgoOrderObserved)) == 2  # 两个事件都转发出去


async def test_trade_of_the_triggered_stop_order_is_the_stops_own_fill(ingest, bus, collector, acked_stop,
                                                                       stream_algo, stream_order, trade, orders,
                                                                       fills):
    await start(ingest, bus)
    stop = await acked_stop(algo_id="5001")
    bus.publish(stream_algo(stop, raw="TRIGGERED", algo_id="5001", actual_order_id="7001"))
    # 触发后生成的订单：自定义编号是交易所生成的，订单号是 7001
    bus.publish(stream_order(stop, execution="TRADE", status="FILLED", filled="0.8", avg="89.5",
                             exchange_order_id="7001", client_order_id="ex-generated-1",
                             fill=trade(stop, trade_id="S1", exchange_order_id="7001", qty="0.8", price="89.5")))
    bus.publish(stream_algo(stop, raw="FINISHED", algo_id="5001", actual_order_id="7001"))
    await bus.drain()

    [row] = await fills()
    assert (row.client_order_id, row.origin, row.intent_id) == (stop.client_order_id, OrderOrigin.SYSTEM,
                                                               stop.intent_id)
    [event] = collector.of(FillIngested)
    assert (event.order_purpose, event.intent_id) == ("stop", stop.intent_id)
    final = await orders.get(stop.client_order_id)
    assert (final.state, final.filled_qty, final.avg_price, final.actual_order_id) == (O.FILLED, D("0.8"), D("89.5"),
                                                                                       "7001")


# ---------------------------------------------------------------- 稳健性

async def test_one_bad_event_does_not_stop_the_subscription(ingest, bus, acked_order, stream_order, trade, fills):
    await start(ingest, bus)
    order = await acked_order(qty="1")
    bad = stream_order(order, execution="TRADE", status="PARTIALLY_FILLED", filled="0.4",
                       fill=trade(order, trade_id="BAD", qty="0"))  # 数量为 0：入账拒绝，总线记日志
    good = stream_order(order, execution="TRADE", status="PARTIALLY_FILLED", filled="0.4",
                        fill=trade(order, trade_id="OK", qty="0.4"))
    bus.publish(bad)
    bus.publish(good)
    await bus.drain()
    assert [r.trade_id for r in await fills()] == ["OK"]
