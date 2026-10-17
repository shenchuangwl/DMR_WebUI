"""补拉成交的失败要报出来（FU5）：读成交返回「未知」不是「没有新成交」。

``Ingest.backfill_trades`` 返回 ``BackfillResult``：它本身就是新入账的笔数（``int`` 的子类，``== 2``、``+=`` 等现有用法照旧），
另带 ``failed``（``frozenset[str]``）：这一次没能补拉完整的合约——读成交返回未知、补拉某个合约时出错、翻页达到上限。
失败只属于这一次调用，不是调用方要清的状态；读到了而没有新成交的合约不在其中。
"""
from __future__ import annotations

from dmr_kernel.core.ingest import BackfillResult
from dmr_kernel.core.types import Unknown
from tests.support.ledger import START_MS

MINUTE = 60_000
BTC, ETH = "BTCUSDT", "ETHUSDT"


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


# ---------------------------------------------------------------- 结果对象本身


def test_the_result_is_a_plain_int_count_that_also_carries_the_failed_symbols():
    result = BackfillResult(3)
    assert result == 3 and isinstance(result, int) and result.failed == frozenset()
    assert result + 1 == 4 and sum([result, BackfillResult(2)]) == 5  # 现有调用里的算术照旧
    assert not BackfillResult(0) and bool(BackfillResult(2))
    failed = BackfillResult(1, [BTC, BTC, ETH]).failed
    assert failed == frozenset({BTC, ETH}) and isinstance(failed, frozenset)  # 去重、不可变


# ---------------------------------------------------------------- 读失败 vs 读到了但没有新成交


async def test_a_successful_backfill_reports_no_failure_with_or_without_new_trades(ingest, adapter, trade):
    adapter.set_read("fetch_order", Unknown("x"))
    serve_trades(adapter, [trade(trade_id="B1", time_ms=START_MS - MINUTE, exchange_order_id="1")])
    first = await ingest.backfill_trades([BTC, ETH])
    assert isinstance(first, BackfillResult) and first == 1 and first.failed == frozenset()
    again = await ingest.backfill_trades([BTC, ETH])
    assert again == 0 and again.failed == frozenset()  # 读到了、没有新成交：不是失败
    nothing = await ingest.backfill_trades([])
    assert nothing == 0 and nothing.failed == frozenset()


async def test_a_symbol_whose_trades_cannot_be_read_is_reported_as_failed(ingest, adapter, fills):
    adapter.set_read("fetch_trades", lambda **kw: Unknown("限频"))
    result = await ingest.backfill_trades([BTC])
    assert result == 0 and result.failed == frozenset({BTC})
    assert await fills() == []


async def test_a_read_that_was_never_prepared_is_a_failed_read_too(ingest):
    result = await ingest.backfill_trades([BTC])  # 假适配层没预设 -> Unknown
    assert result == 0 and result.failed == frozenset({BTC})


async def test_only_the_failing_symbols_are_listed_and_the_others_still_count(ingest, adapter, trade, fills):
    eth = trade(trade_id="E1", symbol=ETH, time_ms=START_MS - MINUTE, exchange_order_id="2")
    adapter.set_read("fetch_order", Unknown("x"))
    adapter.set_read("fetch_trades", lambda **kw: Unknown("限频") if kw["symbol"] == BTC else [eth])
    result = await ingest.backfill_trades([BTC, ETH])
    assert result == 1 and result.failed == frozenset({BTC})
    assert [r.symbol for r in await fills()] == [ETH]


async def test_a_symbol_given_twice_is_listed_once(ingest, adapter):
    adapter.set_read("fetch_trades", lambda **kw: Unknown("限频"))
    assert (await ingest.backfill_trades([BTC, BTC])).failed == frozenset({BTC})


async def test_the_failure_belongs_to_that_call_a_later_good_call_reports_none(ingest, adapter, trade):
    btc = trade(trade_id="B1", time_ms=START_MS - MINUTE, exchange_order_id="1")
    adapter.set_read("fetch_order", Unknown("x"))
    adapter.set_read("fetch_trades", lambda **kw: Unknown("限频"))
    bad = await ingest.backfill_trades([BTC])
    assert bad.failed == frozenset({BTC})
    serve_trades(adapter, [btc])
    good = await ingest.backfill_trades([BTC])
    assert good == 1 and good.failed == frozenset()
    assert bad.failed == frozenset({BTC})  # 先前返回的结果不受影响


# ---------------------------------------------------------------- 翻页途中、入账出错、翻页用完


async def test_a_failure_on_a_later_page_keeps_the_trades_already_taken_and_reports_the_symbol(
        ingest, adapter, trade, fills):
    ingest.PAGE_LIMIT = 2
    page = [trade(trade_id=f"P{i}", time_ms=START_MS - 10 * MINUTE + i * 1_000, exchange_order_id=str(i))
            for i in range(2)]
    adapter.set_read("fetch_order", Unknown("x"))
    asked = []

    def answer(**kw):
        asked.append(kw["start_ms"])
        return page if len(asked) == 1 else Unknown("超时")

    adapter.set_read("fetch_trades", answer)
    result = await ingest.backfill_trades([BTC])
    assert result == 2 and result.failed == frozenset({BTC})
    assert len(asked) == 2 and sorted(r.trade_id for r in await fills()) == ["P0", "P1"]


async def test_an_error_while_ingesting_one_symbol_is_reported_as_failed_and_spares_the_others(
        ingest, adapter, trade, fills):
    btc = [trade(trade_id="G1", symbol=BTC, time_ms=START_MS - MINUTE, exchange_order_id="1"),
           trade(trade_id="G2", symbol=BTC, time_ms=START_MS - 30_000, exchange_order_id="2")]
    eth = [trade(trade_id="E1", symbol=ETH, time_ms=START_MS - MINUTE, exchange_order_id="3")]
    adapter.set_read("fetch_order", Unknown("x"))
    serve_trades(adapter, btc + eth)
    real = ingest.ingest_trade

    async def flaky(t, **kwargs):
        if t.trade_id == "G2":
            raise RuntimeError("账本忙")
        return await real(t, **kwargs)

    ingest.ingest_trade = flaky
    result = await ingest.backfill_trades([BTC, ETH])
    assert result == 2 and result.failed == frozenset({BTC})  # G1 与 E1 入了账；G2 没入成
    assert sorted(r.trade_id for r in await fills()) == ["E1", "G1"]

    ingest.ingest_trade = real
    healed = await ingest.backfill_trades([BTC, ETH])
    assert healed == 1 and healed.failed == frozenset()  # 水位没动，G2 下一轮补上
    assert sorted(r.trade_id for r in await fills()) == ["E1", "G1", "G2"]


async def test_an_invalid_trade_is_dropped_and_is_not_a_failed_read(ingest, adapter, trade, fills):
    good = trade(trade_id="G1", time_ms=START_MS - MINUTE, exchange_order_id="1")
    bad = trade(trade_id="G2", qty="0", time_ms=START_MS - 30_000, exchange_order_id="2")  # 数量为 0：入账拒绝
    adapter.set_read("fetch_order", Unknown("x"))
    serve_trades(adapter, [good, bad])
    result = await ingest.backfill_trades([BTC])
    assert result == 1 and result.failed == frozenset()  # 永久性的坏数据不能让这个合约永远「未判定」
    assert [r.trade_id for r in await fills()] == ["G1"]


async def test_running_out_of_pages_means_the_symbol_was_not_fully_backfilled(ingest, adapter, trade, fills):
    ingest.PAGE_LIMIT = 2
    ingest.MAX_PAGES = 2
    spaced = [trade(trade_id=f"C{i}", time_ms=START_MS - 20 * MINUTE + i * 2 * MINUTE, exchange_order_id=str(i))
              for i in range(6)]
    adapter.set_read("fetch_order", Unknown("x"))
    serve_trades(adapter, spaced)

    first = await ingest.backfill_trades([BTC])
    assert first.failed == frozenset({BTC}) and 0 < first < 6  # 翻页用完了，还有成交没取到

    result = first
    for _ in range(5):  # 水位在前进：再补几轮就补完
        result = await ingest.backfill_trades([BTC])
        if not result.failed:
            break
    assert result.failed == frozenset()
    assert sorted(r.trade_id for r in await fills()) == [f"C{i}" for i in range(6)]


async def test_a_backfill_that_uses_every_allowed_page_but_ends_on_a_short_page_is_complete(ingest, adapter, trade):
    """最后一页不满页就是补完了，哪怕正好用完了允许的翻页数（反例：不能把所有「翻了很多页」都当失败）。"""
    ingest.PAGE_LIMIT = 2
    ingest.MAX_PAGES = 3
    three = [trade(trade_id=f"D{i}", time_ms=START_MS - 20 * MINUTE + i * 2 * MINUTE, exchange_order_id=str(i))
             for i in range(3)]
    adapter.set_read("fetch_order", Unknown("x"))
    serve_trades(adapter, three)
    result = await ingest.backfill_trades([BTC])
    assert result == 3 and result.failed == frozenset()
