"""marketdata.candles.warm_up：重启后从账本 candle 表把缓存预热回来（T0.7）。

预热只读账本，不碰交易所；读回来的 K 线同样要经过「是否收盘」与「连续」两道关。
"""
from __future__ import annotations

import logging

import pytest

from dmr_kernel.ledger import repo_candles
from marketdata_world import BOUNDARY_MS, P15, SpyLedger, make_bar, truth

SYM = "AAAUSDT"
CANDLES_LOGGER = "dmr_kernel.marketdata.candles"


async def persisted(make_world, make_service, *, symbols=(SYM,), timeframe="15m", **world_kwargs):
    """先用一个服务刷新一遍（落盘），返回 (世界, 那个服务)。之后再造一个全新的服务来预热。"""
    world = make_world(**world_kwargs)
    first, _ = make_service(world)
    await first.refresh(list(symbols), timeframe)
    return world, first


async def test_warm_up_reads_back_what_a_refresh_persisted_without_touching_the_exchange(make_world, make_service):
    world, first = await persisted(make_world, make_service)
    requests_before, time_calls_before = len(world.requests), world.server_time_calls
    second, _ = make_service(world)
    assert second.closed(SYM, "15m") == []
    await second.warm_up([SYM], "15m")
    assert second.closed(SYM, "15m") == first.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 98)
    assert second.frame(SYM, "15m").equals(first.frame(SYM, "15m"))
    assert len(world.requests) == requests_before and world.server_time_calls == time_calls_before


async def test_a_warmed_cache_drives_the_next_incremental_request(make_world, make_service):
    world, _ = await persisted(make_world, make_service)
    second, _ = make_service(world)
    await second.warm_up([SYM], "15m")
    world.advance(3 * P15)
    assert await second.refresh([SYM], "15m") == {SYM: True}
    assert world.requests[-1]["limit"] == 5  # 缺 3 根，按预热出来的缓存算，不是从头取 99 根
    assert second.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 101)


async def test_warm_up_loads_only_the_newest_limit_bars(make_world, make_service):
    world, _ = await persisted(make_world, make_service)
    second, _ = make_service(world)
    await second.warm_up([SYM], "15m", limit=10)
    assert second.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 10)


async def test_the_default_limit_is_500_and_the_cache_never_exceeds_cache_bars(make_world, make_service, ledger):
    world = make_world()
    bars = truth(SYM, P15, BOUNDARY_MS - 600 * P15, BOUNDARY_MS - P15)  # 600 根已收盘的
    await ledger.write(repo_candles.upsert_bars, "15m", {SYM: bars})
    default, _ = make_service(world)
    await default.warm_up([SYM], "15m")
    assert default.closed(SYM, "15m") == bars[-500:]
    small, _ = make_service(world, cache_bars=20)
    await small.warm_up([SYM], "15m", limit=500)
    assert small.closed(SYM, "15m") == bars[-20:]


async def test_rows_the_server_clock_says_are_not_closed_are_dropped(make_world, make_service, ledger):
    # 服务器：边界前 5 秒；本机快 10 秒，按本机时间会以为刚收盘的那根（B-P15）已经收盘
    world = make_world(server_ms=BOUNDARY_MS - 5_000, local_offset_ms=10_000)
    service, _ = make_service(world)
    assert await service.sync_server_time() is True
    closed_bars = world.latest_closed(SYM, "15m", 5)  # 到 B-2P15 为止
    still_forming = make_bar(SYM, BOUNDARY_MS - P15, P15)
    future = make_bar(SYM, BOUNDARY_MS, P15)
    await ledger.write(repo_candles.upsert_bars, "15m", {SYM: closed_bars + [still_forming, future]})
    await service.warm_up([SYM], "15m")
    assert service.closed(SYM, "15m") == closed_bars


async def test_warm_up_keeps_only_the_newest_unbroken_run(make_world, make_service, ledger):
    world = make_world()
    first_open = BOUNDARY_MS - 100 * P15
    older = truth(SYM, P15, first_open, first_open + 9 * P15)
    newer = truth(SYM, P15, first_open + 12 * P15, first_open + 20 * P15)  # 中间缺 2 根
    await ledger.write(repo_candles.upsert_bars, "15m", {SYM: older + newer})
    service, _ = make_service(world)
    await service.warm_up([SYM], "15m")
    assert service.closed(SYM, "15m") == newer


async def test_after_a_long_downtime_the_old_run_is_dropped_by_the_first_refresh(make_world, make_service, ledger):
    world = make_world()
    ten_days = 10 * 96 * P15
    stale = truth(SYM, P15, BOUNDARY_MS - ten_days - 97 * P15, BOUNDARY_MS - ten_days)
    await ledger.write(repo_candles.upsert_bars, "15m", {SYM: stale})
    service, _ = make_service(world)
    await service.warm_up([SYM], "15m")
    assert service.closed(SYM, "15m") == stale  # 它们确实是收盘的，先留着
    assert await service.refresh([SYM], "15m") == {SYM: True}
    assert world.requests[-1]["limit"] == 99
    assert service.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 98)  # 与新数据不连续的旧段丢掉


async def test_warm_up_with_nothing_stored_leaves_the_cache_empty(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.warm_up([SYM, "BBBUSDT"], "15m")
    assert service.closed(SYM, "15m") == [] and len(service.frame(SYM, "15m")) == 0


async def test_warm_up_only_fills_the_series_that_have_rows(make_world, make_service):
    world, _ = await persisted(make_world, make_service, symbols=(SYM,))
    second, _ = make_service(world)
    await second.warm_up([SYM, "BBBUSDT"], "15m")
    assert len(second.closed(SYM, "15m")) == 98 and second.closed("BBBUSDT", "15m") == []
    await second.warm_up([SYM], "1h")  # 1 小时线没有存过
    assert second.closed(SYM, "1h") == []


async def test_warm_up_merges_with_what_is_already_cached(make_world, make_service):
    world, first = await persisted(make_world, make_service)
    await first.warm_up([SYM], "15m")
    assert first.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 98)  # 没有重复


async def test_warm_up_reads_all_symbols_in_one_query_and_ignores_duplicates(make_world, make_service, ledger):
    world, _ = await persisted(make_world, make_service, symbols=("S1USDT", "S2USDT", "S3USDT"))
    spy = SpyLedger(ledger)
    second, _ = make_service(world, with_ledger=spy)
    await second.warm_up(["S1USDT", "S2USDT", "S1USDT", "S3USDT"], "15m")
    assert len(spy.reads) == 1 and spy.writes == []
    assert spy.reads[0][1] == ("15m", ["S1USDT", "S2USDT", "S3USDT"], 500)
    assert all(len(second.closed(s, "15m")) == 98 for s in ("S1USDT", "S2USDT", "S3USDT"))


async def test_a_ledger_read_failure_leaves_the_cache_as_it_was_and_the_first_refresh_rebuilds_it(
        make_world, make_service, ledger, caplog):
    world, _ = await persisted(make_world, make_service)
    spy = SpyLedger(ledger)
    spy.fail_reads = True
    second, _ = make_service(world, with_ledger=spy)
    with caplog.at_level(logging.ERROR, logger=CANDLES_LOGGER):
        await second.warm_up([SYM], "15m")
    assert second.closed(SYM, "15m") == []
    assert any("预热" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert await second.refresh([SYM], "15m") == {SYM: True}
    assert world.requests[-1]["limit"] == 99 and second.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 98)


async def test_contract_names_survive_the_round_trip(make_world, make_service):
    chinese = "币安人生USDT"
    world, first = await persisted(make_world, make_service, symbols=(chinese,))
    second, _ = make_service(world)
    await second.warm_up([chinese], "15m")
    assert second.closed(chinese, "15m") == first.closed(chinese, "15m") != []


async def test_warm_up_publishes_nothing_and_counts_no_missing_period(make_world, make_service, event_log):
    world = make_world()
    service, _ = make_service(world)
    seen = event_log()
    await service.warm_up([SYM], "15m")
    assert await seen.settle() == [] and service.missing_periods(SYM, "15m") == 0


@pytest.mark.parametrize("limit", [0, -1, repo_candles.MAX_LIMIT + 1])
async def test_limit_out_of_range_is_refused(make_world, make_service, limit):
    service, _ = make_service(make_world())
    with pytest.raises(ValueError, match="limit"):
        await service.warm_up([SYM], "15m", limit=limit)


@pytest.mark.parametrize("limit", [True, 2.5, "5", None])
async def test_limit_must_be_an_integer(make_world, make_service, limit):
    service, _ = make_service(make_world())
    with pytest.raises((TypeError, ValueError)):
        await service.warm_up([SYM], "15m", limit=limit)


async def test_symbols_must_be_a_collection_and_the_timeframe_a_known_one(make_world, make_service):
    service, _ = make_service(make_world())
    with pytest.raises(TypeError):
        await service.warm_up("AAAUSDT", "15m")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await service.warm_up([SYM], "1x")
