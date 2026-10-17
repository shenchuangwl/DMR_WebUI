"""补拉：每 60 秒按合约水位补拉成交，每小时拉收益流水（资金费等）。数据流与补拉两条路径到达的同一笔成交只入账一次。"""
from __future__ import annotations

from decimal import Decimal

from dmr_kernel.core.events import FillIngested, IncomeIngested
from dmr_kernel.core.types import OrderOrigin, OrderState, Unknown
from tests.support.ledger import START_MS

D = Decimal
O = OrderState
HOUR = 3_600_000
MINUTE = 60_000


def serve_trades(adapter, trades):
    """假适配层的 fetch_trades：按合约与起点过滤，并遵守 limit（和真实接口一样从最早的开始给）。"""
    calls = []

    def answer(*, symbol, start_ms, end_ms, limit):
        calls.append((symbol, start_ms, limit))
        found = sorted((t for t in trades if t.symbol == symbol and t.time_ms >= start_ms),
                       key=lambda t: (t.time_ms, t.trade_id))
        return found[:limit]

    adapter.set_read("fetch_trades", answer)
    return calls


# ---------------------------------------------------------------- 补拉成交

async def test_backfill_ingests_new_trades_announces_them_and_counts_only_new_ones(
        acked_order, ingest, adapter, trade, fills, bus, collector, orders):
    await bus.start()
    order = await acked_order(qty="1")
    trades = [trade(order, trade_id="B1", qty="0.4", price="100", time_ms=START_MS + 1_000),
              trade(order, trade_id="B2", qty="0.6", price="101", time_ms=START_MS + 2_000)]
    serve_trades(adapter, trades)

    assert await ingest.backfill_trades(["BTCUSDT"]) == 2
    rows = await fills()
    assert [(r.trade_id, r.path, r.origin, r.client_order_id) for r in rows] == [
        ("B1", "rest", OrderOrigin.SYSTEM, order.client_order_id), ("B2", "rest", OrderOrigin.SYSTEM, order.client_order_id)]
    assert (await orders.get(order.client_order_id)).state is O.FILLED
    await bus.drain()
    assert [(e.trade.trade_id, e.path) for e in collector.of(FillIngested)] == [("B1", "rest"), ("B2", "rest")]

    assert await ingest.backfill_trades(["BTCUSDT"]) == 0  # 再补一次：全是重复
    assert len(await fills()) == 2


async def test_trade_seen_on_the_stream_is_not_ingested_again_by_the_backfill(acked_order, ingest, adapter, trade,
                                                                              fills, bus, collector):
    await bus.start()
    order = await acked_order(qty="1")
    t = trade(order, trade_id="S1", qty="0.4", time_ms=START_MS + 500)
    await ingest.ingest_trade(t, client_order_id=order.client_order_id, path="stream")
    serve_trades(adapter, [t])
    assert await ingest.backfill_trades(["BTCUSDT"]) == 0
    [row] = await fills()
    assert row.path == "stream"
    await bus.drain()
    assert len(collector.of(FillIngested)) == 1


async def _events(bus, collector, kind):
    await bus.drain()
    return collector.of(kind)


async def test_backfill_recovers_a_trade_the_stream_missed(acked_order, ingest, adapter, trade, fills, bus, collector,
                                                           orders):
    await bus.start()
    order = await acked_order(qty="1")
    seen = trade(order, trade_id="S2", qty="0.3", time_ms=START_MS + 2_000)
    missed = trade(order, trade_id="S1", qty="0.2", time_ms=START_MS + 1_000)  # 数据流断了，这笔没收到
    await ingest.ingest_trade(seen, client_order_id=order.client_order_id, path="stream")
    serve_trades(adapter, [seen, missed])
    assert await ingest.backfill_trades(["BTCUSDT"]) == 1
    assert sorted(r.trade_id for r in await fills()) == ["S1", "S2"]
    [event] = [e for e in (await _events(bus, collector, FillIngested)) if e.path == "rest"]
    assert event.trade == missed
    assert (await orders.get(order.client_order_id)).filled_qty == D("0.5")  # 补上的成交并进了订单


async def test_events_follow_trade_time_not_the_order_the_exchange_listed_them(ingest, adapter, trade, bus,
                                                                               collector):
    await bus.start()
    later = trade(trade_id="X2", time_ms=START_MS + 2_000, exchange_order_id="1")
    earlier = trade(trade_id="X1", time_ms=START_MS + 1_000, exchange_order_id="2")
    adapter.set_read("fetch_trades", lambda **kw: [later, earlier])
    adapter.set_read("fetch_order", Unknown("x"))
    await ingest.backfill_trades(["BTCUSDT"])
    assert [e.trade.trade_id for e in await _events(bus, collector, FillIngested)] == ["X1", "X2"]


async def test_the_first_backfill_of_a_symbol_looks_back_24_hours(ingest, adapter, clock):
    calls = serve_trades(adapter, [])
    assert await ingest.backfill_trades(["BTCUSDT"]) == 0
    assert calls == [("BTCUSDT", clock.now_ms() - 24 * HOUR, 500)]


async def test_the_next_backfill_starts_60_seconds_before_the_last_trade_seen(ingest, adapter, trade, clock):
    calls = serve_trades(adapter, [trade(trade_id="W1", time_ms=START_MS - 5 * MINUTE, exchange_order_id="1")])
    adapter.set_read("fetch_order", Unknown("x"))
    await ingest.backfill_trades(["BTCUSDT"])
    clock.advance(MINUTE)
    await ingest.backfill_trades(["BTCUSDT"])
    assert calls[1] == ("BTCUSDT", START_MS - 5 * MINUTE - MINUTE, 500)  # 水位 − 60 秒重叠


async def test_the_watermark_survives_a_restart(ingest, make_ingest, adapter, trade, clock):
    calls = serve_trades(adapter, [trade(trade_id="W1", time_ms=START_MS - 10 * MINUTE, exchange_order_id="1")])
    adapter.set_read("fetch_order", Unknown("x"))
    await ingest.backfill_trades(["BTCUSDT"])
    clock.advance(HOUR)
    await make_ingest().backfill_trades(["BTCUSDT"])  # 重启后内存是空的，水位在账本里
    assert calls[1][1] == START_MS - 10 * MINUTE - MINUTE


async def test_the_watermark_is_kept_per_symbol(ingest, adapter, trade):
    calls = serve_trades(adapter, [trade(trade_id="W1", symbol="BTCUSDT", time_ms=START_MS - MINUTE, exchange_order_id="1")])
    adapter.set_read("fetch_order", Unknown("x"))
    await ingest.backfill_trades(["BTCUSDT", "ETHUSDT"])
    await ingest.backfill_trades(["BTCUSDT", "ETHUSDT"])
    by_symbol = {}
    for symbol, start, _ in calls:
        by_symbol.setdefault(symbol, []).append(start)
    assert by_symbol["BTCUSDT"][1] == START_MS - 2 * MINUTE
    assert by_symbol["ETHUSDT"][1] == by_symbol["ETHUSDT"][0]  # 没见过成交的合约水位不动


async def test_fills_recorded_by_the_stream_do_not_move_the_backfill_watermark(ingest, adapter, trade, clock, fills):
    """数据流入账了更晚的成交，不能把补拉没处理的更早的成交挡在窗口外：水位只认补拉自己完整处理过的。"""
    await ingest.ingest_trade(trade(trade_id="E1", time_ms=START_MS - MINUTE, exchange_order_id="1"),
                              client_order_id="web_e", path="stream")  # 数据流先入账，补拉还没跑过
    missed = trade(trade_id="E0", time_ms=START_MS - 2 * HOUR, exchange_order_id="2")  # 更早的、数据流没收到的成交
    calls = serve_trades(adapter, [missed])
    adapter.set_read("fetch_order", Unknown("x"))
    assert await ingest.backfill_trades(["BTCUSDT"]) == 1
    assert calls == [("BTCUSDT", clock.now_ms() - 24 * HOUR, 500)]
    assert sorted(r.trade_id for r in await fills()) == ["E0", "E1"]


async def test_a_failed_read_keeps_the_watermark_and_does_not_stop_the_other_symbols(ingest, adapter, trade, fills):
    btc = trade(trade_id="B1", symbol="BTCUSDT", time_ms=START_MS - MINUTE, exchange_order_id="1")
    eth = trade(trade_id="E1", symbol="ETHUSDT", time_ms=START_MS - MINUTE, exchange_order_id="2")
    adapter.set_read("fetch_order", Unknown("x"))
    adapter.set_read("fetch_trades", lambda **kw: Unknown("限频") if kw["symbol"] == "BTCUSDT" else [eth])
    assert await ingest.backfill_trades(["BTCUSDT", "ETHUSDT"]) == 1
    assert [r.symbol for r in await fills()] == ["ETHUSDT"]

    calls = serve_trades(adapter, [btc, eth])
    assert await ingest.backfill_trades(["BTCUSDT", "ETHUSDT"]) == 1  # BTC 这次补上了
    btc_start = [start for symbol, start, _ in calls if symbol == "BTCUSDT"][0]
    assert btc_start == (START_MS) - 24 * HOUR  # 失败的那次没有推进水位：仍从首次回看开始


async def test_an_invalid_trade_is_skipped_without_blocking_the_rest_or_the_watermark(ingest, adapter, trade, fills):
    good = trade(trade_id="G1", time_ms=START_MS - MINUTE, exchange_order_id="1")
    bad = trade(trade_id="G2", qty="0", time_ms=START_MS - 30_000, exchange_order_id="2")  # 数量为 0：入账拒绝
    after = trade(trade_id="G3", time_ms=START_MS - 10_000, exchange_order_id="3")
    adapter.set_read("fetch_order", Unknown("x"))
    calls = serve_trades(adapter, [good, bad, after])
    assert await ingest.backfill_trades(["BTCUSDT"]) == 2  # 坏的丢弃（记日志），好的照常入账，不抛出
    assert [r.trade_id for r in await fills()] == ["G1", "G3"]
    await ingest.backfill_trades(["BTCUSDT"])
    assert calls[1][1] == START_MS - 10_000 - MINUTE  # 坏成交是永久性的问题：不挡水位


async def test_a_transient_failure_aborts_that_symbol_keeps_its_watermark_and_spares_the_others(
        ingest, adapter, trade, fills):
    btc = [trade(trade_id="G1", symbol="BTCUSDT", time_ms=START_MS - MINUTE, exchange_order_id="1"),
           trade(trade_id="G2", symbol="BTCUSDT", time_ms=START_MS - 30_000, exchange_order_id="2")]
    eth = [trade(trade_id="E1", symbol="ETHUSDT", time_ms=START_MS - MINUTE, exchange_order_id="3")]
    adapter.set_read("fetch_order", Unknown("x"))
    calls = serve_trades(adapter, btc + eth)
    real = ingest.ingest_trade
    broken = {"on": True}

    async def flaky(t, **kwargs):
        if broken["on"] and t.trade_id == "G2":
            raise RuntimeError("账本忙")
        return await real(t, **kwargs)

    ingest.ingest_trade = flaky
    assert await ingest.backfill_trades(["BTCUSDT", "ETHUSDT"]) == 2  # G1 与 E1；G2 失败不挡 ETH
    assert sorted(r.trade_id for r in await fills()) == ["E1", "G1"]

    broken["on"] = False
    assert await ingest.backfill_trades(["BTCUSDT", "ETHUSDT"]) == 1  # 水位没动，G2 下一轮补上
    btc_starts = [start for symbol, start, _ in calls if symbol == "BTCUSDT"]
    assert btc_starts[1] == btc_starts[0]
    assert sorted(r.trade_id for r in await fills()) == ["E1", "G1", "G2"]


async def test_backfill_pages_until_a_short_page(ingest, adapter, trade, fills):
    ingest.PAGE_LIMIT = 3
    trades = [trade(trade_id=f"P{i}", time_ms=START_MS - 100_000 + i * 1_000, exchange_order_id=str(i))
              for i in range(7)]
    adapter.set_read("fetch_order", Unknown("x"))
    calls = serve_trades(adapter, trades)
    assert await ingest.backfill_trades(["BTCUSDT"]) == 7
    assert len(await fills()) == 7
    # 下一页从上一页最晚的成交时间开始（同一毫秒的成交可能被页尾截断，所以重叠一笔）；不满一页就停
    assert [limit for _, _, limit in calls] == [3, 3, 3, 3]
    assert [start for _, start, _ in calls][1:] == [trades[2].time_ms, trades[4].time_ms, trades[6].time_ms]


async def test_paging_always_makes_progress_even_when_a_whole_page_shares_one_millisecond(ingest, adapter, trade,
                                                                                          fills):
    ingest.PAGE_LIMIT = 2
    same_ms = START_MS - 50_000
    trades = [trade(trade_id=f"M{i}", time_ms=same_ms, exchange_order_id=str(i)) for i in range(5)]
    adapter.set_read("fetch_order", Unknown("x"))
    calls = serve_trades(adapter, trades)
    await ingest.backfill_trades(["BTCUSDT"])
    assert len(calls) <= ingest.MAX_PAGES  # 不会死循环
    starts = [start for _, start, _ in calls]
    assert starts == sorted(set(starts))  # 起点严格前进


async def test_backfill_with_no_symbols_or_duplicate_symbols(ingest, adapter):
    calls = serve_trades(adapter, [])
    assert await ingest.backfill_trades([]) == 0
    assert calls == []
    await ingest.backfill_trades(["BTCUSDT", "BTCUSDT", "ETHUSDT"])
    assert [symbol for symbol, _, _ in calls] == ["BTCUSDT", "ETHUSDT"]


async def test_backfill_without_a_prepared_read_is_unknown_not_empty(ingest, adapter, fills):
    assert await ingest.backfill_trades(["BTCUSDT"]) == 0  # 假适配层没预设 → Unknown，不当成「没有成交」
    assert await fills() == []


async def test_backfill_resolves_unlabelled_rest_trades_by_exchange_order_id(acked_order, ingest, adapter, trade,
                                                                              fills):
    order = await acked_order(qty="1", exchange_order_id="9001")
    serve_trades(adapter, [trade(order, trade_id="R1", exchange_order_id="9001", time_ms=START_MS - 1_000)])
    await ingest.backfill_trades(["BTCUSDT"])
    [row] = await fills()
    assert (row.origin, row.client_order_id, row.intent_id) == (OrderOrigin.SYSTEM, order.client_order_id,
                                                               order.intent_id)
    assert adapter.calls("fetch_order") == []


async def test_backfill_never_calls_a_write_method(acked_order, ingest, adapter, trade):
    order = await acked_order(qty="1")
    serve_trades(adapter, [trade(order, time_ms=START_MS - 1_000)])
    await ingest.backfill_trades(["BTCUSDT"])
    await ingest.backfill_income()
    assert adapter.writes == []


# ---------------------------------------------------------------- 收益流水

def serve_income(adapter, items):
    calls = []

    def answer(*, start_ms, end_ms, income_type, limit):
        calls.append((start_ms, end_ms, income_type, limit))
        return sorted((i for i in items if i.time_ms >= start_ms), key=lambda i: (i.time_ms, i.income_id))[:limit]

    adapter.set_read("fetch_income", answer)
    return calls


async def test_income_is_recorded_announced_and_deduplicated_by_income_id(ingest, adapter, income, incomes, bus,
                                                                         collector, clock):
    await bus.start()
    funding = income(income_id="F1", income_type="FUNDING_FEE", symbol="BTCUSDT", amount="-0.1234",
                     time_ms=START_MS - HOUR)
    transfer = income(income_id="X1", income_type="TRANSFER", symbol=None, amount="100", time_ms=START_MS - 30 * MINUTE,
                      info="划转")
    serve_income(adapter, [funding, transfer])
    clock.advance(1_000)

    assert await ingest.backfill_income() == 2
    first, second = await incomes()
    assert (first.income_id, first.income_type, first.symbol, first.amount, first.asset, first.time_ms,
            first.position_id, first.info) == ("F1", "FUNDING_FEE", "BTCUSDT", D("-0.1234"), "USDT",
                                               START_MS - HOUR, None, "")
    assert (second.symbol, second.income_type, second.amount, second.info) == (None, "TRANSFER", D("100"), "划转")
    await bus.drain()
    events = collector.of(IncomeIngested)
    assert [(e.income_id, e.income_type, e.symbol, e.amount, e.at_ms) for e in events] == [
        ("F1", "FUNDING_FEE", "BTCUSDT", D("-0.1234"), clock.now_ms()),
        ("X1", "TRANSFER", None, D("100"), clock.now_ms())]

    assert await ingest.backfill_income() == 0  # 再拉一次：全是重复
    assert len(await incomes()) == 2
    await bus.drain()
    assert len(collector.of(IncomeIngested)) == 2


async def test_duplicates_inside_one_batch_are_recorded_once(ingest, adapter, income, incomes):
    twin = income(income_id="D1", time_ms=START_MS - MINUTE)
    adapter.set_read("fetch_income", lambda **kw: [twin, twin])
    assert await ingest.backfill_income() == 1
    assert len(await incomes()) == 1


async def test_first_income_pull_looks_back_24_hours_and_asks_for_every_type(ingest, adapter, clock):
    calls = serve_income(adapter, [])
    await ingest.backfill_income()
    assert calls == [(clock.now_ms() - 24 * HOUR, None, None, 1000)]


async def test_the_next_income_pull_starts_before_the_last_income_seen(ingest, make_ingest, adapter, income, clock):
    calls = serve_income(adapter, [income(income_id="F1", time_ms=START_MS - 2 * HOUR)])
    await ingest.backfill_income()
    clock.advance(HOUR)
    await ingest.backfill_income()
    await make_ingest().backfill_income()  # 重启后水位仍在
    assert calls[1][0] == calls[2][0] == START_MS - 2 * HOUR - 10 * MINUTE  # 水位 − 10 分钟重叠


async def test_income_arriving_late_inside_the_overlap_is_still_picked_up(ingest, adapter, income, incomes, clock):
    on_time = income(income_id="A", time_ms=START_MS - 30 * MINUTE)
    serve_income(adapter, [on_time])
    await ingest.backfill_income()
    late = income(income_id="B", time_ms=START_MS - 35 * MINUTE)  # 比水位早 5 分钟，但交易所晚到
    serve_income(adapter, [on_time, late])
    clock.advance(HOUR)
    assert await ingest.backfill_income() == 1
    assert sorted(i.income_id for i in await incomes()) == ["A", "B"]


async def test_unknown_income_read_changes_nothing(ingest, adapter, incomes, bus, collector):
    await bus.start()
    adapter.set_read("fetch_income", Unknown("限频"))
    assert await ingest.backfill_income() == 0
    assert await incomes() == []
    await bus.drain()
    assert collector.events == []


async def test_income_pages_until_a_short_page(ingest, adapter, income, incomes):
    ingest.INCOME_PAGE_LIMIT = 2
    items = [income(income_id=f"I{n}", time_ms=START_MS - 100_000 + n * 1_000) for n in range(5)]
    calls = serve_income(adapter, items)
    assert await ingest.backfill_income() == 5
    assert [limit for _, _, _, limit in calls] == [2, 2, 2, 2, 2]  # 每页重叠一笔（同一毫秒的流水可能被页尾截断）
    assert len(await incomes()) == 5


async def test_income_paging_makes_progress_when_a_page_shares_one_millisecond(ingest, adapter, income):
    ingest.INCOME_PAGE_LIMIT = 2
    same_ms = START_MS - 5_000
    items = [income(income_id=f"S{n}", time_ms=same_ms) for n in range(5)]
    calls = serve_income(adapter, items)
    await ingest.backfill_income()
    starts = [start for start, _, _, _ in calls]
    assert len(calls) <= ingest.MAX_PAGES and starts == sorted(set(starts))


async def test_funding_fee_credits_and_debits_are_both_kept_with_their_sign(ingest, adapter, income, incomes):
    serve_income(adapter, [income(income_id="C", amount="0.25", time_ms=START_MS - 2 * MINUTE),
                           income(income_id="D", amount="-0.31", time_ms=START_MS - MINUTE)])
    await ingest.backfill_income()
    assert [i.amount for i in await incomes()] == [D("0.25"), D("-0.31")]
    assert sum(i.amount for i in await incomes(income_type="FUNDING_FEE")) == D("-0.06")
    assert await incomes(income_type="COMMISSION") == []
    assert [i.income_id for i in await incomes(symbol="BTCUSDT")] == ["C", "D"]
    assert await incomes(symbol="ETHUSDT") == []
