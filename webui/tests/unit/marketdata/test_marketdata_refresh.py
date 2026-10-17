"""marketdata.candles：refresh（增量刷新、缓存连续、落盘、并发与权重预算）与 closed/frame（T0.7）。

世界里的时间：服务器在整点后 5 秒（BOUNDARY_MS 是 15 分钟与 1 小时的边界），交易所有 5000 根历史 K 线。
首次刷新取 99 根，其中最后一根还在走，缓存里是 98 根已收盘的；``World.latest_closed`` 给出「此刻应有的最新 n 根」。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pandas as pd
import pytest

from marketdata_world import BOUNDARY_MS, P1H, P15, GatedLedger, SlowAdapter, SpyLedger, truth

SYM = "AAAUSDT"
CANDLES_LOGGER = "dmr_kernel.marketdata.candles"


def row(bar) -> tuple:
    return bar.open_time_ms, bar.open, bar.high, bar.low, bar.close, bar.volume


# ------------------------------------------------------------------ 首次与增量


async def test_the_first_refresh_asks_for_99_bars_and_caches_the_closed_ones(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    assert await service.refresh([SYM], "15m") == {SYM: True}
    assert world.requests == [{"symbol": SYM, "interval": "15m", "limit": 99}]
    assert service.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 98)  # 99 根里最后一根在走，不收


async def test_refresh_answers_for_each_distinct_symbol_in_input_order_and_asks_once_each(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    result = await service.refresh(["BBBUSDT", "AAAUSDT", "BBBUSDT"], "15m")
    assert list(result) == ["BBBUSDT", "AAAUSDT"] and all(result.values())
    assert sorted(r["symbol"] for r in world.requests) == ["AAAUSDT", "BBBUSDT"]


@pytest.mark.parametrize(("missing", "limit", "cached_after"), [
    (1, 3, 99),  # 新收盘 1 根：缺 1 根，多取 2 根（重叠 1 根 + 在走的 1 根）
    (2, 4, 100),
    (5, 7, 103),
    (50, 52, 148),
    (96, 98, 194),
    (97, 99, 195),  # 99 是上限；这时还重叠 1 根
    (98, 99, 196),  # 不再重叠，但和缓存紧挨着，仍然连续
    (99, 99, 98),  # 中间缺 1 根：连续性断了，丢掉缺口之前的旧段，只留新取到的 98 根
    (150, 99, 98),
])
async def test_the_incremental_limit_is_the_missing_bars_plus_two_capped_at_99(make_world, make_service, missing, limit,
                                                                              cached_after):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    world.advance(missing * P15)
    assert await service.refresh([SYM], "15m") == {SYM: True}
    assert world.requests[-1] == {"symbol": SYM, "interval": "15m", "limit": limit}
    assert service.closed(SYM, "15m") == world.latest_closed(SYM, "15m", cached_after)  # 连续，且到最新一根为止


async def test_a_cache_that_is_already_current_is_not_requested_again(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    assert await service.refresh([SYM], "15m") == {SYM: True}
    world.advance(P15 - 5_001)  # 距下一个边界还差 1 毫秒
    assert await service.refresh([SYM], "15m") == {SYM: True}
    assert len(world.requests) == 1


async def test_a_bar_closing_makes_the_next_refresh_ask_again(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    world.advance(P15 - 5_000)  # 正好到边界
    assert await service.refresh([SYM], "15m") == {SYM: True}
    assert len(world.requests) == 2 and world.requests[-1]["limit"] == 3
    assert service.closed(SYM, "15m")[-1].open_time_ms == BOUNDARY_MS  # 刚收盘的那根


async def test_the_forming_bar_never_reaches_the_cache(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    assert all(bar.open_time_ms + P15 <= world.server_ms for bar in service.closed(SYM, "15m"))
    assert service.closed(SYM, "15m")[-1].open_time_ms == BOUNDARY_MS - P15


async def test_refresh_decides_currency_on_the_server_time_after_calibrating(make_world, make_service):
    # 服务器：边界前 5 秒（最新已收盘的是上上根）；本机快 10 秒：若按本机时间规划，会以为缺了一根而报「不是最新」
    world = make_world(server_ms=BOUNDARY_MS - 5_000, local_offset_ms=10_000)
    service, _ = make_service(world)
    assert await service.refresh([SYM], "15m") == {SYM: True}
    assert service.closed(SYM, "15m")[-1].open_time_ms == BOUNDARY_MS - 2 * P15


async def test_timeframes_are_cached_separately(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    assert service.closed(SYM, "1h") == []
    await service.refresh([SYM], "1h")
    assert service.closed(SYM, "1h") == world.latest_closed(SYM, "1h", 98)
    assert service.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 98)
    assert {(r["interval"], r["limit"]) for r in world.requests} == {("15m", 99), ("1h", 99)}
    world.advance(P1H)  # 一小时后：15 分钟线缺 4 根，1 小时线缺 1 根
    await service.refresh([SYM], "1h")
    assert world.requests[-1]["limit"] == 3
    assert service.closed(SYM, "15m")[-1].open_time_ms == BOUNDARY_MS - P15  # 15 分钟线没动


# ------------------------------------------------------------------ 失败与落后


async def test_a_lagging_exchange_leaves_the_cache_behind_and_the_next_refresh_catches_up(make_world, make_service):
    world = make_world()
    world.lagging.add("LAGUSDT")
    service, _ = make_service(world)
    assert await service.refresh(["LAGUSDT", SYM], "15m") == {"LAGUSDT": False, SYM: True}
    behind = service.closed("LAGUSDT", "15m")
    assert behind == world.latest_closed("LAGUSDT", "15m", 98)[:-1]  # 缺最新已收盘的一根，其余照收
    world.lagging.clear()
    assert await service.refresh(["LAGUSDT"], "15m") == {"LAGUSDT": True}
    assert world.requests[-1]["limit"] == 3  # 缺 1 根，按落后的缓存算
    assert service.closed("LAGUSDT", "15m") == world.latest_closed("LAGUSDT", "15m", 98)


async def test_a_failed_read_reports_false_keeps_the_cache_and_recovers_with_the_right_limit(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    kept = service.closed(SYM, "15m")
    world.advance(2 * P15)
    world.failing.add(SYM)
    assert await service.refresh([SYM], "15m") == {SYM: False}
    assert service.closed(SYM, "15m") == kept
    world.failing.clear()
    assert await service.refresh([SYM], "15m") == {SYM: True}
    assert world.requests[-1]["limit"] == 4  # 缺 2 根
    assert service.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 100)


async def test_an_empty_answer_leaves_an_empty_cache_not_current_and_asks_for_99_again(make_world, make_service):
    world = make_world()
    world.adapter.set_read("fetch_candles", lambda symbol, interval, limit: [])
    service, _ = make_service(world)
    assert await service.refresh([SYM], "15m") == {SYM: False}
    assert service.closed(SYM, "15m") == []
    assert await service.refresh([SYM], "15m") == {SYM: False}
    assert [call["limit"] for call in world.adapter.calls("fetch_candles")] == [99, 99]  # 自己换了读取，World 不记请求


async def test_a_hole_in_the_answer_drops_everything_before_the_hole(make_world, make_service):
    world = make_world()
    exchange = world._fetch_candles
    hole = BOUNDARY_MS - 10 * P15
    world.adapter.set_read("fetch_candles", lambda symbol, interval, limit: [
        bar for bar in exchange(symbol, interval, limit) if bar.open_time_ms != hole])
    service, _ = make_service(world)
    assert await service.refresh([SYM], "15m") == {SYM: True}
    assert service.closed(SYM, "15m") == truth(SYM, P15, hole + P15, BOUNDARY_MS - P15)  # 缓存里永远没有洞


async def test_an_adapter_exception_for_one_symbol_does_not_stop_the_others(make_world, make_service):
    world = make_world()
    world.raising.add("BADUSDT")
    world.failing.add("DOWNUSDT")
    service, _ = make_service(world)
    assert await service.refresh(["BADUSDT", SYM, "DOWNUSDT", "CCCUSDT"], "15m") == {
        "BADUSDT": False, SYM: True, "DOWNUSDT": False, "CCCUSDT": True}
    assert service.closed("BADUSDT", "15m") == [] and service.closed("DOWNUSDT", "15m") == []


async def test_without_a_usable_server_time_everything_is_false_and_nothing_is_requested(make_world, make_service):
    world = make_world()
    world.server_time_ok = False
    service, _ = make_service(world)
    assert await service.refresh([SYM, "BBBUSDT"], "15m") == {SYM: False, "BBBUSDT": False}
    assert world.requests == [] and world.server_time_calls == 1  # 整轮只试一次校准，不是每个合约一次


async def test_refresh_of_no_symbols_is_empty_and_needs_no_calibration(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    assert await service.refresh([], "15m") == {}
    assert world.server_time_calls == 0 and world.requests == []


async def test_refresh_publishes_no_events(make_world, make_service, event_log):
    world = make_world()
    service, _ = make_service(world)
    seen = event_log()
    await service.refresh([SYM], "15m")
    assert await seen.settle() == []


# ------------------------------------------------------------------ 参数


async def test_symbols_must_be_a_collection_not_a_single_string(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    with pytest.raises(TypeError):
        await service.refresh("AAAUSDT", "15m")  # type: ignore[arg-type]  逐字符迭代会悄悄刷新 7 个不存在的合约
    assert world.requests == []


@pytest.mark.parametrize("bad", ["", None, 7])
async def test_a_blank_symbol_in_the_list_is_refused_before_anything_is_sent(make_world, make_service, bad):
    world = make_world()
    service, _ = make_service(world)
    with pytest.raises((TypeError, ValueError)):
        await service.refresh([SYM, bad], "15m")
    assert world.requests == []


async def test_an_unknown_timeframe_is_refused(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    with pytest.raises(ValueError):
        await service.refresh([SYM], "1x")
    assert world.requests == [] and world.server_time_calls == 0


@pytest.mark.parametrize("kwargs", [{"max_concurrency": 0}, {"max_concurrency": True}, {"cache_bars": 0},
                                      {"cache_bars": -3}, {"cache_bars": 1.5}])
def test_constructor_arguments_are_checked(make_world, make_service, kwargs):
    with pytest.raises((TypeError, ValueError)):
        make_service(make_world(), **kwargs)


# ------------------------------------------------------------------ 并发与权重预算


async def test_requests_are_made_concurrently_but_never_above_the_cap(make_world, make_service):
    adapter = SlowAdapter(delay_s=0.02)
    world = make_world(adapter=adapter)
    service, _ = make_service(world)
    symbols = [f"S{i:02d}USDT" for i in range(40)]
    result = await service.refresh(symbols, "15m")
    assert all(result.values()) and len(result) == 40
    assert adapter.max_in_flight == 10  # 用满默认的 10 个，但不超过


async def test_the_cap_is_configurable(make_world, make_service):
    adapter = SlowAdapter(delay_s=0.01)
    world = make_world(adapter=adapter)
    service, _ = make_service(world, max_concurrency=3)
    await service.refresh([f"S{i:02d}USDT" for i in range(12)], "15m")
    assert adapter.max_in_flight == 3


async def test_the_cap_is_shared_by_refreshes_running_at_the_same_time(make_world, make_service):
    adapter = SlowAdapter(delay_s=0.02)
    world = make_world(adapter=adapter)
    service, _ = make_service(world)
    symbols = [f"S{i:02d}USDT" for i in range(30)]
    first, second = await asyncio.gather(service.refresh(symbols, "15m"), service.refresh(symbols, "1h"))
    assert all(first.values()) and all(second.values())
    assert adapter.max_in_flight == 10  # 不是每次刷新各 10 个


async def test_one_request_per_symbol_per_refresh_each_within_the_weight_budget(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    symbols = [f"S{i:02d}USDT" for i in range(25)]
    await service.refresh(symbols, "15m")
    assert len(world.requests) == 25 and {r["symbol"] for r in world.requests} == set(symbols)
    world.advance(3 * P15)
    await service.refresh(symbols, "15m")
    assert len(world.requests) == 50
    assert all(1 <= r["limit"] <= 99 for r in world.requests)


# ------------------------------------------------------------------ 落盘


async def test_closed_bars_are_persisted_with_their_values_and_the_forming_bar_is_not(make_world, make_service,
                                                                                      raw_db):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    expected = world.latest_closed(SYM, "15m", 98)
    assert raw_db.candle_rows(SYM, "15m") == [row(bar) for bar in expected]
    assert max(raw_db.candle_times(SYM, "15m")) == BOUNDARY_MS - P15
    assert raw_db.candle_rows(SYM, "1h") == []


async def test_contract_names_are_persisted_as_given(make_world, make_service, raw_db):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh(["币安人生USDT", "1000SHIBUSDT"], "15m")
    assert len(raw_db.candle_times("币安人生USDT", "15m")) == 98
    assert len(raw_db.candle_times("1000SHIBUSDT", "15m")) == 98


async def test_one_refresh_round_is_one_transaction(make_world, make_service, ledger):
    spy = SpyLedger(ledger)
    world = make_world()
    service, _ = make_service(world, with_ledger=spy)
    symbols = [f"S{i:02d}USDT" for i in range(20)]
    await service.refresh(symbols, "15m")
    assert [name for name, _, _ in spy.writes] == ["upsert_bars"]
    _, args, kwargs = spy.writes[0]
    assert args[0] == "15m" and set(args[1]) == set(symbols) and kwargs == {"keep": 500}


async def test_only_new_bars_are_written_and_nothing_is_written_when_nothing_is_new(make_world, make_service, ledger):
    spy = SpyLedger(ledger)
    world = make_world()
    service, _ = make_service(world, with_ledger=spy)
    await service.refresh([SYM], "15m")
    world.advance(P15)
    await service.refresh([SYM], "15m")
    assert len(spy.writes) == 2
    assert spy.writes[1][1][1] == {SYM: [world.latest_closed(SYM, "15m", 1)[0]]}  # 只有新收盘的那一根，不重写重叠的
    await service.refresh([SYM], "15m")
    assert len(spy.writes) == 2


async def test_a_bar_the_exchange_revised_is_rewritten(make_world, make_service, raw_db):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    world.advance(P15)
    original = {bar.open_time_ms: bar for bar in service.closed(SYM, "15m")}[BOUNDARY_MS - P15]
    revised = replace(original, close=original.close + 5.0)
    exchange = world._fetch_candles

    def with_a_correction(symbol, interval, limit):
        answer = exchange(symbol, interval, limit)
        return [revised if bar.open_time_ms == revised.open_time_ms else bar for bar in answer]

    world.adapter.set_read("fetch_candles", with_a_correction)
    await service.refresh([SYM], "15m")
    kept = {bar.open_time_ms: bar for bar in service.closed(SYM, "15m")}
    assert kept[revised.open_time_ms] == revised
    assert {r[0]: r for r in raw_db.candle_rows(SYM, "15m")}[revised.open_time_ms] == row(revised)


async def test_the_cache_and_the_table_are_both_kept_to_cache_bars(make_world, make_service, raw_db):
    world = make_world()
    service, _ = make_service(world, cache_bars=50)
    await service.refresh([SYM], "15m")
    assert service.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 50)
    assert raw_db.candle_times(SYM, "15m") == [bar.open_time_ms for bar in world.latest_closed(SYM, "15m", 50)]
    world.advance(3 * P15)
    await service.refresh([SYM], "15m")
    assert service.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 50)
    assert raw_db.candle_times(SYM, "15m") == [bar.open_time_ms for bar in world.latest_closed(SYM, "15m", 50)]


async def test_a_failed_write_is_logged_but_does_not_fail_the_refresh(make_world, make_service, ledger, caplog):
    spy = SpyLedger(ledger)
    spy.fail_writes = True
    world = make_world()
    service, _ = make_service(world, with_ledger=spy)
    with caplog.at_level(logging.ERROR, logger=CANDLES_LOGGER):
        assert await service.refresh([SYM], "15m") == {SYM: True}
    assert service.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 98)  # 数据在内存里，照常可用
    assert any("落盘" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


async def test_bars_that_could_not_be_saved_are_written_by_a_later_round(make_world, make_service, ledger, raw_db):
    spy = SpyLedger(ledger)
    spy.fail_writes = True
    world = make_world()
    service, _ = make_service(world, with_ledger=spy)
    assert await service.refresh([SYM], "15m") == {SYM: True}
    assert raw_db.candle_count() == 0 and len(spy.writes) == 1
    spy.fail_writes = False
    world.advance(P15)
    assert await service.refresh([SYM], "15m") == {SYM: True}
    expected = world.latest_closed(SYM, "15m", 99)
    assert raw_db.candle_times(SYM, "15m") == [bar.open_time_ms for bar in expected]  # 上一轮没存上的也一并补上
    assert len(spy.writes) == 2
    await service.refresh([SYM], "15m")
    assert len(spy.writes) == 2  # 存上之后不再重写


async def test_overlapping_refreshes_of_one_series_leave_one_consistent_cache(make_world, make_service, raw_db):
    world = make_world(adapter=SlowAdapter(delay_s=0.02))
    service, _ = make_service(world)
    first, second = await asyncio.gather(service.refresh([SYM], "15m"), service.refresh([SYM], "15m"))
    assert first == second == {SYM: True}
    expected = world.latest_closed(SYM, "15m", 98)
    assert service.closed(SYM, "15m") == expected
    assert raw_db.candle_times(SYM, "15m") == [bar.open_time_ms for bar in expected]


async def test_a_bar_revised_while_its_old_version_was_being_saved_is_saved_again(make_world, make_service, ledger,
                                                                                  raw_db):
    gated = GatedLedger(ledger)
    world = make_world()
    service, _ = make_service(world, with_ledger=gated)
    first = asyncio.ensure_future(service.refresh([SYM], "15m"))
    await gated.first_write_done.wait()  # 旧版本已经落盘，第一轮还没醒来
    bar_open = BOUNDARY_MS - P15
    original = {bar.open_time_ms: bar for bar in service.closed(SYM, "15m")}[bar_open]
    revised = replace(original, close=original.close + 5.0)
    exchange = world._fetch_candles

    def with_a_correction(symbol, interval, limit):
        gated.release.set()  # 第二轮取回修正版的同时，第一轮醒来并确认「已落盘」
        answer = exchange(symbol, interval, limit)
        return [revised if bar.open_time_ms == bar_open else bar for bar in answer]

    world.adapter.set_read("fetch_candles", with_a_correction)
    world.advance(P15)
    assert await service.refresh([SYM], "15m") == {SYM: True}
    assert await first == {SYM: True}
    assert {r[0]: r for r in raw_db.candle_rows(SYM, "15m")}[bar_open] == row(revised)  # 存的是修正版，不是第一轮写的旧版


# ------------------------------------------------------------------ closed / frame


async def test_closed_is_empty_for_unknown_series_and_returns_a_copy(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    assert service.closed("NOPEUSDT", "15m") == []
    await service.refresh([SYM], "15m")
    handed_out = service.closed(SYM, "15m")
    handed_out.clear()
    assert service.closed(SYM, "15m") == world.latest_closed(SYM, "15m", 98)


async def test_frame_has_the_documented_columns_ascending_with_numeric_dtypes(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    frame = service.frame(SYM, "15m")
    expected = world.latest_closed(SYM, "15m", 98)
    assert list(frame.columns) == ["open_time_ms", "open", "high", "low", "close", "volume"]
    assert len(frame) == 98 and frame["open_time_ms"].is_monotonic_increasing
    assert frame["open_time_ms"].tolist() == [bar.open_time_ms for bar in expected]
    for column in ("open", "high", "low", "close", "volume"):
        assert frame[column].tolist() == [getattr(bar, column) for bar in expected]
    assert str(frame["open_time_ms"].dtype) == "int64"
    assert all(str(frame[c].dtype) == "float64" for c in ("open", "high", "low", "close", "volume"))
    assert isinstance(frame, pd.DataFrame) and frame.index.tolist() == list(range(98))


async def test_frame_is_a_fresh_copy_and_follows_new_bars(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    frame = service.frame(SYM, "15m")
    frame.loc[:, "close"] = 0.0  # 改返回的表格不影响缓存
    assert service.frame(SYM, "15m")["close"].tolist() == [b.close for b in world.latest_closed(SYM, "15m", 98)]
    world.advance(P15)
    await service.refresh([SYM], "15m")
    assert len(service.frame(SYM, "15m")) == 99


def test_frame_of_an_unknown_series_is_empty_with_the_same_columns_and_dtypes(make_world, make_service):
    service, _ = make_service(make_world())
    frame = service.frame("NOPEUSDT", "15m")
    assert list(frame.columns) == ["open_time_ms", "open", "high", "low", "close", "volume"] and len(frame) == 0
    assert str(frame["open_time_ms"].dtype) == "int64" and str(frame["close"].dtype) == "float64"

