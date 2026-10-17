"""ledger.repo_candles：candle 表的仓储函数（只存已收盘的 K 线；按合约 + 周期 + 开盘时间唯一）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import Candle
from dmr_kernel.ledger import repo_candles
from dmr_kernel.ledger.records import CandleRecord
from marketdata_world import BASE_MS, P1H, P15, make_bar, truth

SYMBOL = "AAAUSDT"
OTHER = "BBBUSDT"


def bars(symbol: str, count: int, *, first: int = BASE_MS, period: int = P15) -> list[Candle]:
    return truth(symbol, period, first, first + (count - 1) * period)


def as_tuple(record: CandleRecord) -> tuple:
    return (record.symbol, record.timeframe, record.open_time_ms, record.open, record.high, record.low,
            record.close, record.volume)


def expected_tuple(symbol: str, timeframe: str, bar: Candle) -> tuple:
    return symbol, timeframe, bar.open_time_ms, bar.open, bar.high, bar.low, bar.close, bar.volume


async def load(ledger, symbol: str = SYMBOL, timeframe: str = "15m", limit: int = 1000) -> list[CandleRecord]:
    return (await ledger.read(repo_candles.load_recent, timeframe, [symbol], limit))[symbol]


async def test_upsert_stores_the_bars_and_load_recent_reads_them_back_ascending(ledger, raw_db):
    stored = bars(SYMBOL, 5)
    written = await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: stored})
    assert written == 5
    rows = await load(ledger)
    assert all(isinstance(r, CandleRecord) for r in rows)
    assert [as_tuple(r) for r in rows] == [expected_tuple(SYMBOL, "15m", b) for b in stored]
    assert raw_db.candle_times(SYMBOL, "15m") == [b.open_time_ms for b in stored]


async def test_upsert_reports_the_number_of_bars_across_symbols(ledger):
    written = await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 3), OTHER: bars(OTHER, 4)})
    assert written == 7
    assert len(await load(ledger, SYMBOL)) == 3 and len(await load(ledger, OTHER)) == 4


async def test_upserting_an_existing_bar_replaces_it_instead_of_adding_a_row(ledger, raw_db):
    first = bars(SYMBOL, 3)
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: first})
    revised = Candle(open_time_ms=first[1].open_time_ms, open=1.5, high=9.25, low=0.125, close=7.75, volume=42.5,
                     close_time_ms=first[1].close_time_ms)
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: [revised]})
    rows = await load(ledger)
    assert [r.open_time_ms for r in rows] == [b.open_time_ms for b in first]  # 仍是三根
    assert as_tuple(rows[1]) == expected_tuple(SYMBOL, "15m", revised)
    assert as_tuple(rows[0]) == expected_tuple(SYMBOL, "15m", first[0])  # 其余不动
    assert as_tuple(rows[2]) == expected_tuple(SYMBOL, "15m", first[2])
    assert raw_db.candle_count() == 3


async def test_the_same_open_time_in_another_timeframe_or_symbol_is_a_different_bar(ledger, raw_db):
    same_open = BASE_MS
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: [make_bar(SYMBOL, same_open, P15)]})
    await ledger.write(repo_candles.upsert_bars, "1h", {SYMBOL: [make_bar(SYMBOL, same_open, P1H)]})
    await ledger.write(repo_candles.upsert_bars, "15m", {OTHER: [make_bar(OTHER, same_open, P15)]})
    assert raw_db.candle_count() == 3


async def test_load_recent_returns_only_the_newest_limit_bars_still_ascending(ledger):
    stored = bars(SYMBOL, 10)
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: stored})
    rows = await load(ledger, limit=4)
    assert [r.open_time_ms for r in rows] == [b.open_time_ms for b in stored[-4:]]
    assert [r.open_time_ms for r in await load(ledger, limit=1)] == [stored[-1].open_time_ms]
    assert len(await load(ledger, limit=100)) == 10  # 比现有的多就是全部


async def test_load_recent_separates_symbols_and_timeframes(ledger):
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 3), OTHER: bars(OTHER, 2)})
    await ledger.write(repo_candles.upsert_bars, "1h", {SYMBOL: bars(SYMBOL, 6, period=P1H)})
    assert len(await load(ledger, SYMBOL, "15m")) == 3
    assert len(await load(ledger, SYMBOL, "1h")) == 6
    assert len(await load(ledger, OTHER, "15m")) == 2
    assert await load(ledger, OTHER, "1h") == []


async def test_load_recent_answers_for_every_requested_symbol_and_the_newest_limit_applies_to_each(ledger):
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 6), OTHER: bars(OTHER, 2)})
    loaded = await ledger.read(repo_candles.load_recent, "15m", [SYMBOL, OTHER, "CCCUSDT"], 3)
    assert list(loaded) == [SYMBOL, OTHER, "CCCUSDT"]
    assert [len(loaded[s]) for s in (SYMBOL, OTHER, "CCCUSDT")] == [3, 2, 0]
    assert loaded[SYMBOL][-1].open_time_ms == BASE_MS + 5 * P15


async def test_load_recent_with_no_symbols_is_empty(ledger):
    assert await ledger.read(repo_candles.load_recent, "15m", [], 10) == {}


async def test_contract_names_are_stored_exactly_as_given(ledger):
    chinese = "币安人生USDT"
    mixed = "1000SHIBUSDT"
    await ledger.write(repo_candles.upsert_bars, "15m", {chinese: bars(chinese, 2), mixed: bars(mixed, 2)})
    assert len(await load(ledger, chinese)) == 2
    assert len(await load(ledger, mixed)) == 2
    assert await load(ledger, "币安人生usdt") == []  # 不做大小写归一化


async def test_float_values_come_back_exactly(ledger):
    awkward = Candle(open_time_ms=BASE_MS, open=0.1 + 0.2, high=123456.789012345, low=1e-9, close=0.30000000000000004,
                     volume=3.141592653589793e7, close_time_ms=BASE_MS + P15 - 1)
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: [awkward]})
    (record,) = await load(ledger)
    assert (record.open, record.high, record.low, record.close, record.volume) == (
        awkward.open, awkward.high, awkward.low, awkward.close, awkward.volume)


async def test_keep_trims_the_symbols_it_touched_to_their_newest_rows(ledger):
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 6), OTHER: bars(OTHER, 6)})
    await ledger.write(repo_candles.upsert_bars, "1h", {SYMBOL: bars(SYMBOL, 6, period=P1H)})
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 2, first=BASE_MS + 6 * P15)}, keep=3)
    rows = await load(ledger)
    assert [r.open_time_ms for r in rows] == [BASE_MS + i * P15 for i in (5, 6, 7)]  # 总共 8 根，留最新 3 根
    assert len(await load(ledger, OTHER)) == 6  # 没写到的合约不动
    assert len(await load(ledger, SYMBOL, "1h")) == 6  # 别的周期不动


async def test_keep_counts_the_rows_already_in_the_table(ledger):
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 3)}, keep=4)
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 3, first=BASE_MS + 3 * P15)}, keep=4)
    assert [r.open_time_ms for r in await load(ledger)] == [BASE_MS + i * P15 for i in (2, 3, 4, 5)]


async def test_keep_exactly_the_number_of_rows_deletes_nothing(ledger):
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 4)}, keep=4)
    assert len(await load(ledger)) == 4


async def test_without_keep_nothing_is_ever_deleted(ledger):
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 30)})
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 30, first=BASE_MS + 30 * P15)})
    assert len(await load(ledger)) == 60


async def test_a_late_write_of_an_old_bar_is_the_one_trimmed_not_the_newer_rows(ledger):
    # 补写一根比表里最新 keep 根都旧的 K 线：先写后修剪，被修剪掉的是它（表里只留最新的 keep 根），不会反过来挤掉新的
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 3, first=BASE_MS + 10 * P15)}, keep=3)
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: [make_bar(SYMBOL, BASE_MS, P15)]}, keep=3)
    assert [r.open_time_ms for r in await load(ledger)] == [BASE_MS + i * P15 for i in (10, 11, 12)]


@pytest.mark.parametrize("keep", [0, -1])
async def test_keep_must_be_positive(ledger, raw_db, keep):
    with pytest.raises(ValueError, match="keep"):
        await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 2)}, keep=keep)
    assert raw_db.candle_count() == 0


@pytest.mark.parametrize("keep", [True, 1.5, "3"])
async def test_keep_must_be_an_integer(ledger, raw_db, keep):
    with pytest.raises((TypeError, ValueError)):
        await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 2)}, keep=keep)
    assert raw_db.candle_count() == 0


@pytest.mark.parametrize("limit", [0, -1, repo_candles.MAX_LIMIT + 1])
async def test_load_recent_limit_must_be_within_bounds(ledger, limit):
    with pytest.raises(ValueError, match="limit"):
        await ledger.read(repo_candles.load_recent, "15m", [SYMBOL], limit)


@pytest.mark.parametrize("limit", [True, 2.5, "5"])
async def test_load_recent_limit_must_be_an_integer(ledger, limit):
    with pytest.raises((TypeError, ValueError)):
        await ledger.read(repo_candles.load_recent, "15m", [SYMBOL], limit)


async def test_load_recent_accepts_the_largest_allowed_limit(ledger):
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 2)})
    assert len(await load(ledger, limit=repo_candles.MAX_LIMIT)) == 2


@pytest.mark.parametrize(("timeframe", "symbol"), [("", SYMBOL), (None, SYMBOL), ("15m", ""), ("15m", None)])
async def test_blank_timeframe_or_symbol_is_refused(ledger, raw_db, timeframe, symbol):
    with pytest.raises((TypeError, ValueError)):
        await ledger.write(repo_candles.upsert_bars, timeframe, {symbol: bars(SYMBOL, 1)})
    assert raw_db.candle_count() == 0


async def test_a_bar_the_table_rejects_leaves_the_whole_batch_unwritten(ledger, raw_db):
    good = bars(SYMBOL, 3)
    broken = Candle(open_time_ms=BASE_MS + 9 * P15, open=float("nan"), high=1.0, low=1.0, close=1.0, volume=1.0,
                    close_time_ms=BASE_MS + 10 * P15 - 1)
    with pytest.raises(Exception):
        await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: good, OTHER: [broken]}, keep=2)
    assert raw_db.candle_count() == 0  # 整个批次同一个事务：好的那几根也没写进去


async def test_a_failed_batch_does_not_trim_what_was_already_stored(ledger):
    await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: bars(SYMBOL, 5)})
    broken = Candle(open_time_ms=BASE_MS + 99 * P15, open=float("nan"), high=1.0, low=1.0, close=1.0, volume=1.0,
                    close_time_ms=BASE_MS + 100 * P15 - 1)
    with pytest.raises(Exception):
        await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: [broken]}, keep=1)
    assert len(await load(ledger)) == 5


async def test_an_empty_batch_writes_nothing(ledger, raw_db):
    assert await ledger.write(repo_candles.upsert_bars, "15m", {}) == 0
    assert await ledger.write(repo_candles.upsert_bars, "15m", {SYMBOL: []}) == 0
    assert raw_db.candle_count() == 0
