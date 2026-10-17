"""Ingest.ingest_trade：去重、归类、认领、同事务合并订单、发布事件。"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from dmr_kernel.core.events import FillIngested, OrderStateChanged
from dmr_kernel.core.types import (
    FillSource,
    IntentState,
    Known,
    OrderOrigin,
    OrderPurpose,
    OrderSide,
    OrderState,
    PositionSide,
    Unknown,
    WriteUnknown,
)
from dmr_kernel.exchange import ids

D = Decimal
O = OrderState


# ---------------------------------------------------------------- 系统成交：落账、认领、发事件

async def test_a_system_trade_is_recorded_claimed_and_announced(acked_order, ingest, trade, fills, clock, bus,
                                                                collector):
    await bus.start()
    order = await acked_order(qty="1")
    clock.advance(250)
    t = trade(order, trade_id="T1", qty="0.4", price="100.5", commission="0.04", realized_pnl="1.5", maker=True)

    event = await ingest.ingest_trade(t, client_order_id=order.client_order_id, path="stream")

    [row] = await fills()
    assert (row.trade_id, row.exchange_order_id, row.client_order_id, row.symbol, row.side, row.position_side) == (
        "T1", "9001", order.client_order_id, "BTCUSDT", OrderSide.BUY, PositionSide.LONG)
    assert (row.qty, row.price, row.commission, row.commission_asset, row.commission_usdt, row.realized_pnl) == (
        D("0.4"), D("100.5"), D("0.04"), "USDT", D("0.04"), D("1.5"))
    assert (row.time_ms, row.source, row.origin, row.position_id, row.intent_id, row.path, row.created_ms) == (
        t.time_ms, FillSource.SYSTEM, OrderOrigin.SYSTEM, None, order.intent_id, "stream", clock.now_ms())
    assert isinstance(event, FillIngested)
    assert (event.fill_id, event.trade, event.client_order_id, event.origin, event.intent_id, event.order_purpose,
            event.path, event.at_ms) == (row.id, t, order.client_order_id, OrderOrigin.SYSTEM, order.intent_id,
                                         "open", "stream", clock.now_ms())
    await bus.drain()
    assert collector.of(FillIngested) == [event]  # 返回的就是发布的那个事件


async def test_order_purpose_is_reported_for_every_purpose(new_order, ingest, trade, make_intent):
    intent = await make_intent()
    for leg, purpose in (("o", OrderPurpose.OPEN), ("c", OrderPurpose.CLOSE), ("p", OrderPurpose.PARTIAL_CLOSE),
                         ("d", OrderPurpose.DUST_CLOSE)):
        order = await new_order(intent=intent, leg=leg, purpose=purpose)
        event = await ingest.ingest_trade(trade(order), client_order_id=order.client_order_id, path="stream")
        assert event.order_purpose == purpose.value


# ---------------------------------------------------------------- 去重

async def test_a_trade_with_a_known_id_is_ingested_only_once(acked_order, ingest, trade, fills, orders, bus,
                                                             collector):
    await bus.start()
    order = await acked_order(qty="1")
    t = trade(order, trade_id="T1", qty="0.4")
    assert await ingest.ingest_trade(t, client_order_id=order.client_order_id, path="stream") is not None
    after_first = await orders.get(order.client_order_id)

    assert await ingest.ingest_trade(t, client_order_id=order.client_order_id, path="stream") is None
    assert await ingest.ingest_trade(t, client_order_id=None, path="rest") is None
    assert len(await fills()) == 1
    assert await orders.get(order.client_order_id) == after_first  # 重复的成交不会再加一遍成交量
    await bus.drain()
    assert len(collector.of(FillIngested)) == 1


async def test_a_duplicate_does_not_cost_an_exchange_lookup(ingest, adapter, trade, fills):
    t = trade(trade_id="D1", exchange_order_id="123")
    adapter.set_read("fetch_order", Unknown("x"))
    assert await ingest.ingest_trade(t, client_order_id=None, path="rest") is not None
    assert len(adapter.calls("fetch_order")) == 1
    assert await ingest.ingest_trade(t, client_order_id=None, path="rest") is None
    assert len(adapter.calls("fetch_order")) == 1  # 重复的成交先去重，不再为它回查订单
    assert len(await fills()) == 1


@pytest.mark.parametrize("first,second", [("stream", "rest"), ("rest", "stream")])
async def test_the_same_trade_arriving_on_both_paths_is_ingested_once(acked_order, ingest, trade, fills, first,
                                                                      second):
    order = await acked_order(qty="1")
    t = trade(order, trade_id="T1", qty="0.4")

    def cid(path):  # 数据流带编号，REST 成交没有编号
        return order.client_order_id if path == "stream" else None

    assert await ingest.ingest_trade(t, client_order_id=cid(first), path=first) is not None
    assert await ingest.ingest_trade(t, client_order_id=cid(second), path=second) is None
    [row] = await fills()
    assert row.path == first  # 先到的那条路径留在账上


async def test_concurrent_ingestion_of_the_same_trade_yields_exactly_one_row(acked_order, ingest, trade, fills,
                                                                            orders):
    order = await acked_order(qty="1")
    t = trade(order, trade_id="T1", qty="0.4")
    results = await asyncio.gather(*[ingest.ingest_trade(t, client_order_id=order.client_order_id,
                                                         path="stream" if i % 2 else "rest") for i in range(6)])
    assert len([r for r in results if r is not None]) == 1
    assert len(await fills()) == 1
    assert (await orders.get(order.client_order_id)).filled_qty == D("0.4")


async def test_trade_ids_that_collide_across_symbols_are_both_kept(ingest, trade, fills):
    a = trade(trade_id="777", symbol="BTCUSDT", exchange_order_id="1")
    b = trade(trade_id="777", symbol="ETHUSDT", exchange_order_id="2")
    assert await ingest.ingest_trade(a, client_order_id="web_a", path="rest") is not None
    assert await ingest.ingest_trade(b, client_order_id="web_b", path="rest") is not None
    first, second = await fills()
    assert (first.symbol, first.trade_id) == ("BTCUSDT", "777")
    assert second.symbol == "ETHUSDT" and "777" in second.trade_id and second.trade_id != "777"  # 库里的成交号唯一
    # 两笔都能被识别为重复
    assert await ingest.ingest_trade(a, client_order_id="web_a", path="stream") is None
    assert await ingest.ingest_trade(b, client_order_id="web_b", path="stream") is None
    assert len(await fills()) == 2


# ---------------------------------------------------------------- 认领：订单表里的三种编号

async def test_rest_trade_without_a_client_id_is_claimed_by_the_exchange_order_id(acked_order, ingest, trade, fills,
                                                                                  adapter):
    order = await acked_order(qty="1", exchange_order_id="9001")
    event = await ingest.ingest_trade(trade(order, exchange_order_id="9001"), client_order_id=None, path="rest")
    assert (event.origin, event.client_order_id, event.intent_id, event.order_purpose) == (
        OrderOrigin.SYSTEM, order.client_order_id, order.intent_id, "open")
    [row] = await fills()
    assert (row.client_order_id, row.source, row.intent_id, row.path) == (order.client_order_id, FillSource.SYSTEM,
                                                                           order.intent_id, "rest")
    assert adapter.calls("fetch_order") == []  # 订单表里认得出来，不用问交易所


async def test_trade_of_a_triggered_stop_is_claimed_through_the_actual_order_id(acked_stop, orders, ingest, trade,
                                                                                fills, adapter, bus, collector):
    await bus.start()
    stop = await acked_stop(algo_id="5001")
    await orders.link_actual_order("5001", "7001")  # 条件单触发后生成的订单号
    t = trade(stop, exchange_order_id="7001", qty="0.8", price="89.5", trade_id="S1")

    # 数据流里触发后订单的自定义编号是交易所生成的，不带系统前缀；认领靠触发后订单号
    event = await ingest.ingest_trade(t, client_order_id="triggered-by-exchange", path="stream")

    assert (event.origin, event.client_order_id, event.intent_id, event.order_purpose) == (
        OrderOrigin.SYSTEM, stop.client_order_id, stop.intent_id, "stop")
    [row] = await fills()
    assert (row.client_order_id, row.source, row.intent_id) == (stop.client_order_id, FillSource.SYSTEM,
                                                               stop.intent_id)
    merged = await orders.get(stop.client_order_id)
    assert (merged.state, merged.filled_qty, merged.avg_price) == (O.PARTIALLY_FILLED, D("0.8"), D("89.5"))
    assert adapter.calls("fetch_order") == []
    await bus.drain()
    assert [e.state for e in collector.of(OrderStateChanged)][-1] == "PARTIALLY_FILLED"


async def test_claiming_by_exchange_order_id_is_limited_to_the_same_symbol(acked_order, ingest, trade, adapter):
    order = await acked_order(qty="1", exchange_order_id="9001", symbol="BTCUSDT")
    other = trade(symbol="ETHUSDT", exchange_order_id="9001")  # 不同合约上碰巧相同的订单号
    adapter.set_read("fetch_order", Unknown("查不到"))
    event = await ingest.ingest_trade(other, client_order_id=None, path="rest")
    assert event.origin is OrderOrigin.UNKNOWN and event.intent_id is None


async def test_client_id_in_the_order_table_wins_over_the_exchange_order_id(acked_order, ingest, trade):
    first = await acked_order(qty="1", exchange_order_id="9001", leg="o")
    second = await acked_order(qty="1", exchange_order_id="9002", leg="o1")
    event = await ingest.ingest_trade(trade(second, exchange_order_id="9001"), client_order_id=second.client_order_id,
                                      path="stream")
    assert event.client_order_id == second.client_order_id != first.client_order_id


# ---------------------------------------------------------------- 认领不到：向交易所回查订单号

async def test_rest_trade_of_our_order_without_a_recorded_exchange_id_is_resolved_by_asking_the_exchange(
        new_order, orders, ingest, adapter, order_info, trade, fills):
    order = await new_order(qty="1")
    await orders.apply_write_result(order.client_order_id, WriteUnknown("超时"))  # 受理回执丢了：订单号没记下来
    adapter.set_read("fetch_order", lambda **kw: order_info(order, status="PARTIALLY_FILLED", filled="0.5",
                                                            avg="100", exchange_order_id="9001"))
    event = await ingest.ingest_trade(trade(order, qty="0.5", exchange_order_id="9001"), client_order_id=None,
                                      path="rest")
    assert (event.origin, event.client_order_id, event.intent_id) == (OrderOrigin.SYSTEM, order.client_order_id,
                                                                      order.intent_id)
    assert adapter.calls("fetch_order") == [{"symbol": "BTCUSDT", "client_order_id": None,
                                             "exchange_order_id": "9001"}]
    [row] = await fills()
    assert (row.client_order_id, row.source) == (order.client_order_id, FillSource.SYSTEM)
    merged = await orders.get(order.client_order_id)
    assert (merged.state, merged.exchange_order_id, merged.filled_qty) == (O.PARTIALLY_FILLED, "9001", D("0.5"))


@pytest.mark.parametrize("client_id,origin,source", [
    ("web_8Zx", OrderOrigin.EXTERNAL, FillSource.EXTERNAL),
    ("autoclose-1791949893", OrderOrigin.LIQUIDATION, FillSource.LIQUIDATION),
    ("adl_autoclose-1791949893", OrderOrigin.ADL, FillSource.ADL),
    ("DMR12_SHORT_sell_SHORT_1726640000000", OrderOrigin.LEGACY, FillSource.LEGACY),
    ("DKE-lq3x9z4-1", OrderOrigin.EMERGENCY, FillSource.EMERGENCY),
    ("x-cvBPrNm9abcdef0123456789", OrderOrigin.CCXT_DEFAULT, FillSource.EXTERNAL),  # 系统漏传编号：当作异常，来源仍是外部
])
async def test_rest_trade_is_classified_by_the_client_id_the_exchange_reports(ingest, adapter, order_info, trade,
                                                                              fills, stranger, client_id, origin,
                                                                              source):
    t = trade(exchange_order_id="123")
    adapter.set_read("fetch_order", lambda **kw: order_info(stranger(client_id), status="FILLED", filled="0.5",
                                                            exchange_order_id="123"))
    event = await ingest.ingest_trade(t, client_order_id=None, path="rest")
    assert (event.origin, event.client_order_id, event.intent_id, event.order_purpose) == (origin, client_id, None,
                                                                                           None)
    [row] = await fills()
    assert (row.origin, row.source, row.client_order_id, row.intent_id) == (origin, source, client_id, None)


@pytest.mark.parametrize("client_id,origin,source", [
    ("web_8Zx", OrderOrigin.EXTERNAL, FillSource.EXTERNAL),
    ("autoclose-1791949893", OrderOrigin.LIQUIDATION, FillSource.LIQUIDATION),
    ("adl_autoclose-1791949893", OrderOrigin.ADL, FillSource.ADL),
    ("DMR6_LONG_buy_LONG_1726640000000", OrderOrigin.LEGACY, FillSource.LEGACY),
    ("DKE-lq3x9z4-1", OrderOrigin.EMERGENCY, FillSource.EMERGENCY),
    ("x-cvBPrNm9abcdef0123456789", OrderOrigin.CCXT_DEFAULT, FillSource.EXTERNAL),
])
async def test_stream_trade_is_classified_by_its_own_client_id(ingest, adapter, trade, fills, client_id, origin,
                                                               source):
    event = await ingest.ingest_trade(trade(), client_order_id=client_id, path="stream")
    assert (event.origin, event.client_order_id) == (origin, client_id)
    [row] = await fills()
    assert (row.origin, row.source) == (origin, source)
    assert adapter.calls("fetch_order") == []  # 数据流成交自带编号，不需要回查


@pytest.mark.parametrize("answer", [Unknown("连接中断"), Known(None), None])
async def test_rest_trade_whose_order_cannot_be_looked_up_is_unknown_origin(ingest, adapter, trade, fills, answer):
    if answer is not None:
        adapter.set_read("fetch_order", answer)  # None：假适配层没预设 → Unknown
    event = await ingest.ingest_trade(trade(exchange_order_id="123"), client_order_id=None, path="rest")
    assert event.origin is OrderOrigin.UNKNOWN and event.client_order_id is None
    [row] = await fills()
    assert (row.origin, row.source, row.client_order_id) == (OrderOrigin.UNKNOWN, FillSource.EXTERNAL, None)


async def test_a_second_fill_of_the_same_unlabelled_order_reuses_the_recorded_answer(ingest, adapter, order_info,
                                                                                     trade, fills, stranger):
    adapter.set_read("fetch_order", lambda **kw: order_info(stranger("web_Q1"), status="FILLED", filled="1",
                                                            exchange_order_id="123"))
    await ingest.ingest_trade(trade(exchange_order_id="123", qty="0.5"), client_order_id=None, path="rest")
    second = await ingest.ingest_trade(trade(exchange_order_id="123", qty="0.5"), client_order_id=None, path="rest")
    assert second.client_order_id == "web_Q1" and second.origin is OrderOrigin.EXTERNAL
    assert len(adapter.calls("fetch_order")) == 1  # 账本里已有这张订单的成交，编号直接沿用
    assert [r.client_order_id for r in await fills()] == ["web_Q1", "web_Q1"]


async def test_a_failed_lookup_is_asked_again_for_the_next_fill_of_that_order(ingest, adapter, order_info, trade,
                                                                              stranger):
    adapter.set_read("fetch_order", Unknown("暂时失败"))
    first = await ingest.ingest_trade(trade(exchange_order_id="123"), client_order_id=None, path="rest")
    assert first.origin is OrderOrigin.UNKNOWN
    adapter.set_read("fetch_order", lambda **kw: order_info(stranger("web_Q2"), status="FILLED",
                                                            exchange_order_id="123"))
    second = await ingest.ingest_trade(trade(exchange_order_id="123"), client_order_id=None, path="rest")
    assert second.origin is OrderOrigin.EXTERNAL  # 没有把失败的答复当成结论记下来


# ---------------------------------------------------------------- 系统前缀的边界

async def test_system_prefix_of_another_instance_is_not_ours(ingest, trade, instance):
    foreign = ids.make_client_id("Zq9z", "1", 5, "o")
    assert ids.parse_client_id(foreign).instance != instance
    event = await ingest.ingest_trade(trade(), client_order_id=foreign, path="stream")
    assert event.origin is OrderOrigin.EXTERNAL and event.intent_id is None


async def test_our_prefix_without_an_order_row_is_still_a_system_fill_but_has_no_intent(ingest, trade, instance,
                                                                                        fills):
    orphan = ids.make_client_id(instance, "1", 999, "o")  # 账本里没有这张订单（比如从旧备份恢复）
    event = await ingest.ingest_trade(trade(), client_order_id=orphan, path="stream")
    assert (event.origin, event.client_order_id, event.intent_id, event.order_purpose) == (
        OrderOrigin.SYSTEM, orphan, None, None)
    [row] = await fills()
    assert (row.origin, row.source, row.intent_id, row.client_order_id) == (OrderOrigin.SYSTEM, FillSource.SYSTEM,
                                                                           None, orphan)


# ---------------------------------------------------------------- 手续费折合

@pytest.mark.parametrize("asset,commission,expected", [
    ("USDT", "0.04", D("0.04")),
    ("BNB", "0.001", None),  # 折算需要行情，入账时不编造
    ("BNB", "0", D("0")),
])
async def test_commission_in_usdt_only_when_it_is_known(ingest, trade, fills, asset, commission, expected):
    await ingest.ingest_trade(trade(commission=commission, commission_asset=asset), client_order_id="web_c",
                              path="stream")
    [row] = await fills()
    assert (row.commission, row.commission_asset, row.commission_usdt) == (D(commission), asset, expected)


# ---------------------------------------------------------------- 同一事务里合并订单

async def test_fills_drive_the_order_state_and_events_are_ordered(acked_order, orders, ingest, trade, bus,
                                                                  collector):
    await bus.start()
    order = await acked_order(qty="1")
    await bus.drain()
    collector.events.clear()

    await ingest.ingest_trade(trade(order, trade_id="T1", qty="0.4", price="100.5"),
                              client_order_id=order.client_order_id, path="stream")
    partial = await orders.get(order.client_order_id)
    assert (partial.state, partial.filled_qty, partial.avg_price) == (O.PARTIALLY_FILLED, D("0.4"), D("100.5"))

    await ingest.ingest_trade(trade(order, trade_id="T2", qty="0.1", price="100.5"),
                              client_order_id=order.client_order_id, path="stream")  # 状态没变，只是成交量增加
    await ingest.ingest_trade(trade(order, trade_id="T3", qty="0.5", price="101"),
                              client_order_id=order.client_order_id, path="stream")
    done = await orders.get(order.client_order_id)
    assert (done.state, done.filled_qty) == (O.FILLED, D("1.0"))
    assert done.avg_price == (D("0.4") * D("100.5") + D("0.1") * D("100.5") + D("0.5") * D("101")) / D("1.0")

    await bus.drain()
    assert collector.kinds() == ["FillIngested", "OrderStateChanged", "FillIngested", "FillIngested",
                                 "OrderStateChanged"]  # 先成交后订单；状态没变的不发
    assert [e.state for e in collector.of(OrderStateChanged)] == ["PARTIALLY_FILLED", "FILLED"]


async def test_a_trade_for_an_order_that_never_got_a_receipt_still_moves_it_forward(new_order, orders, ingest, trade):
    order = await new_order(qty="1")  # 数据流先于写结果到达：订单还是 PENDING_SEND
    await ingest.ingest_trade(trade(order, qty="1", price="100"), client_order_id=order.client_order_id,
                              path="stream")
    merged = await orders.get(order.client_order_id)
    assert merged.state is O.FILLED and merged.acked_ms is not None


async def test_the_order_view_that_came_with_the_trade_is_merged_in_the_same_transaction(acked_order, orders, ingest,
                                                                                         trade, order_info):
    order = await acked_order(qty="1")
    view = order_info(order, status="PARTIALLY_FILLED", filled="0.4", avg="100.5")
    await ingest.ingest_trade(trade(order, qty="0.4", price="100.5"), client_order_id=order.client_order_id,
                              path="stream", view=view)
    merged = await orders.get(order.client_order_id)
    assert (merged.state, merged.raw_status, merged.filled_qty) == (O.PARTIALLY_FILLED, "PARTIALLY_FILLED", D("0.4"))


async def test_external_trades_do_not_touch_any_order(acked_order, orders, ingest, trade, bus, collector):
    await bus.start()
    mine = await acked_order(qty="1", exchange_order_id="9001")
    await bus.drain()
    collector.events.clear()
    await ingest.ingest_trade(trade(exchange_order_id="555", qty="3"), client_order_id="web_ext", path="stream")
    assert await orders.get(mine.client_order_id) == mine
    await bus.drain()
    assert collector.kinds() == ["FillIngested"]


async def test_late_fill_on_an_order_that_was_given_up_is_recorded_and_revives_it(new_order, orders, ingest, trade,
                                                                                  fills, bus, collector):
    await bus.start()
    order = await new_order(qty="1")
    await orders.mark_rejected(order.client_order_id, reason="连续 3 次查到不存在")
    event = await ingest.ingest_trade(trade(order, qty="0.5"), client_order_id=order.client_order_id, path="rest")
    assert event.origin is OrderOrigin.SYSTEM and event.intent_id == order.intent_id  # 系统前缀的成交永远不算外部
    revived = await orders.get(order.client_order_id)
    assert (revived.state, revived.filled_qty) == (O.CANCELLED, D("0.5"))  # 成交证明订单存在过
    assert len(await fills()) == 1
    await bus.drain()
    assert [e.state for e in collector.of(OrderStateChanged)][-1] == "CANCELLED"


async def test_late_fill_reopens_a_finished_intent_and_raises_an_alert(new_order, intents, orders, ingest, trade,
                                                                       bus, notifier):
    """真正的晚到成交：意图已完成（例如重发的那张成交了），但成交落在一张我们判为不存在（拒绝）的订单上，带来新的成交量。"""
    await bus.start()
    await intents.start()
    order = await new_order(qty="1")
    for step in (IntentState.VALIDATED, IntentState.DISPATCH_READY, IntentState.ACTIVE, IntentState.COMPLETED):
        await intents.transition(order.intent_id, step)
    await orders.mark_rejected(order.client_order_id, reason="连续 3 次查到不存在")
    event = await ingest.ingest_trade(trade(order, trade_id="L1", qty="0.3"), client_order_id=order.client_order_id,
                                      path="stream")
    assert event.on_dead_order is True
    await bus.drain()

    assert (await intents.get(order.intent_id)).state is IntentState.ACTIVE
    [alert] = notifier.alerts
    assert alert["kind"] == "intent_late_fill" and "L1" in alert["message"]
    await intents.stop()


async def test_the_expected_fill_of_a_completed_intents_order_is_not_a_late_fill(acked_order, intents, ingest, trade,
                                                                                bus, notifier):
    """REST 应答里就是 FILLED、意图先完成，成交回报随后入账：这是常态，不重开、不告警。"""
    await bus.start()
    await intents.start()
    order = await acked_order(qty="1")
    for step in (IntentState.VALIDATED, IntentState.DISPATCH_READY, IntentState.ACTIVE, IntentState.COMPLETED):
        await intents.transition(order.intent_id, step)
    event = await ingest.ingest_trade(trade(order, trade_id="N1", qty="1"), client_order_id=order.client_order_id,
                                      path="stream")
    assert event.on_dead_order is False
    await bus.drain()
    assert (await intents.get(order.intent_id)).state is IntentState.COMPLETED
    assert notifier.alerts == []
    await intents.stop()


async def test_a_fill_already_known_from_the_exchange_view_of_a_cancelled_order_is_not_on_a_dead_order(
        acked_order, orders, order_info, ingest, trade):
    """市价单部分成交、余量被作废：视图里已经知道成交了 0.5；这 0.5 的成交随后到达不是新的成交量。"""
    order = await acked_order(qty="1")
    await orders.apply_exchange_view(order.client_order_id, order_info(order, status="EXPIRED", raw="EXPIRED_IN_MATCH",
                                                                       filled="0.5", avg="100"), source="query")
    assert (await orders.get(order.client_order_id)).state is O.CANCELLED
    event = await ingest.ingest_trade(trade(order, trade_id="P1", qty="0.5"), client_order_id=order.client_order_id,
                                      path="stream")
    assert event.on_dead_order is False


# ---------------------------------------------------------------- 编号漏传：当作异常

async def test_a_ccxt_generated_client_id_is_logged_as_an_error(ingest, trade, caplog):
    with caplog.at_level("ERROR", logger="dmr_kernel.core.ingest"):
        event = await ingest.ingest_trade(trade(trade_id="C1"), client_order_id="x-cvBPrNm9abcdef", path="stream")
    assert event.origin is OrderOrigin.CCXT_DEFAULT  # 事件带着来源，下游自己判断
    assert any("ccxt" in record.getMessage() and "C1" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------- 输入检查

def test_the_instance_must_be_a_valid_order_id_instance(ledger, bus, clock, adapter):
    from dmr_kernel.core.ingest import Ingest

    for bad in ("TOOLONG", "a-b", ""):
        with pytest.raises(ValueError):
            Ingest(ledger, bus, clock, adapter, instance=bad, config_get={}.__getitem__)


@pytest.mark.parametrize("overrides", [{"qty": "0"}, {"qty": "-1"}, {"price": "0"}, {"price": "-5"}])
async def test_malformed_trades_are_refused_and_leave_no_trace(ingest, trade, fills, bus, collector, overrides):
    await bus.start()
    with pytest.raises(ValueError):
        await ingest.ingest_trade(trade(**overrides), client_order_id="web_x", path="stream")
    assert await fills() == []
    await bus.drain()
    assert collector.events == []


async def test_path_must_be_stream_or_rest(ingest, trade, fills):
    with pytest.raises(ValueError):
        await ingest.ingest_trade(trade(), client_order_id="web_x", path="carrier-pigeon")
    assert await fills() == []
