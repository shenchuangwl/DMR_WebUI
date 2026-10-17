"""marketdata.candles.on_bar_close / missing_periods：一根 K 线收盘后的单轮刷新、就绪名单与 CandlesReady（T0.7）。

约定（见 candles.py 的说明）：就绪 = 刷新后缓存里已有这根 K 线；时限 ``kline_ready_deadline_s`` 从调用起算，
到时没回来的合约不在名单里（它们的请求被取消），不重试——要重试由调用方再调一次；
``missing_periods`` 数的是「连续几根 K 线没就绪」，同一根重试只计一次。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from dmr_kernel.core.events import CandlesReady
from marketdata_world import BOUNDARY_MS, P1H, P15, SlowAdapter, SlowClockAdapter, SpyLedger

BAR = BOUNDARY_MS - P15  # 默认世界里（边界后 5 秒）最新一根已收盘的 15 分钟线
NEXT_BAR = BOUNDARY_MS  # 下一根，要等 15 分钟后才收盘
SYM = "AAAUSDT"


# ------------------------------------------------------------------ 就绪名单与事件


async def test_ready_symbols_are_announced_in_a_candles_ready_event(make_world, make_service, event_log):
    world = make_world()
    service, _ = make_service(world)
    seen = event_log(CandlesReady)
    event = await service.on_bar_close("15m", BAR, ["BBBUSDT", SYM, "CCCUSDT"])
    assert isinstance(event, CandlesReady)
    assert (event.timeframe, event.bar_open_ms, event.symbols, event.at_ms) == (
        "15m", BAR, ("BBBUSDT", SYM, "CCCUSDT"), world.clock.now_ms())
    assert await seen.settle() == [event]
    for symbol in event.symbols:
        assert service.closed(symbol, "15m")[-1].open_time_ms == BAR


async def test_symbols_whose_read_failed_or_lags_are_left_out(make_world, make_service):
    world = make_world()
    world.failing.add("DOWNUSDT")
    world.raising.add("BADUSDT")
    world.lagging.add("LAGUSDT")
    service, _ = make_service(world)
    event = await service.on_bar_close("15m", BAR, ["DOWNUSDT", SYM, "BADUSDT", "LAGUSDT", "CCCUSDT"])
    assert event.symbols == (SYM, "CCCUSDT")
    assert service.closed("LAGUSDT", "15m")[-1].open_time_ms == BAR - P15  # 落后的那个缓存里只有更早的


async def test_ready_symbols_keep_the_order_given_not_the_order_of_completion(make_world, make_service):
    adapter = SlowAdapter(slow_symbols={"S1USDT": 0.08, "S2USDT": 0.0, "S3USDT": 0.04})
    service, _ = make_service(make_world(adapter=adapter))
    event = await service.on_bar_close("15m", BAR, ["S1USDT", "S2USDT", "S3USDT"])
    assert event.symbols == ("S1USDT", "S2USDT", "S3USDT")


async def test_duplicate_symbols_are_listed_and_requested_once(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    event = await service.on_bar_close("15m", BAR, [SYM, "BBBUSDT", SYM])
    assert event.symbols == (SYM, "BBBUSDT") and len(world.requests) == 2


async def test_the_event_is_published_even_when_nobody_is_ready(make_world, make_service, event_log):
    world = make_world()
    world.failing.update({SYM, "BBBUSDT"})
    service, _ = make_service(world)
    seen = event_log(CandlesReady)
    event = await service.on_bar_close("15m", BAR, [SYM, "BBBUSDT"])
    assert event.symbols == () and await seen.settle() == [event]


async def test_no_symbols_publishes_an_empty_event_and_asks_nothing(make_world, make_service, event_log):
    world = make_world()
    service, _ = make_service(world)
    seen = event_log(CandlesReady)
    event = await service.on_bar_close("1h", BOUNDARY_MS - P1H, [])
    assert (event.timeframe, event.bar_open_ms, event.symbols) == ("1h", BOUNDARY_MS - P1H, ())
    assert await seen.settle() == [event] and world.requests == []


async def test_the_bars_are_persisted_by_the_time_the_round_returns(make_world, make_service, raw_db):
    world = make_world()
    service, _ = make_service(world)
    await service.on_bar_close("15m", BAR, [SYM])
    assert raw_db.candle_times(SYM, "15m")[-1] == BAR and len(raw_db.candle_times(SYM, "15m")) == 98


async def test_a_failed_save_does_not_stop_the_announcement(make_world, make_service, ledger):
    spy = SpyLedger(ledger)
    spy.fail_writes = True
    service, _ = make_service(make_world(), with_ledger=spy)
    event = await service.on_bar_close("15m", BAR, [SYM])
    assert event.symbols == (SYM,) and len(spy.writes) == 1


async def test_a_cache_that_already_has_the_bar_is_ready_without_another_request(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM, "BBBUSDT"], "15m")
    requests = len(world.requests)
    event = await service.on_bar_close("15m", BAR, [SYM, "BBBUSDT"])
    assert event.symbols == (SYM, "BBBUSDT") and len(world.requests) == requests


async def test_ready_means_the_cache_holds_that_very_bar(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.refresh([SYM], "15m")
    assert (await service.on_bar_close("15m", BAR - P15, [SYM])).symbols == (SYM,)  # 前一根也在缓存里
    assert (await service.on_bar_close("15m", BAR - 200 * P15, [SYM])).symbols == ()  # 早已滚出缓存


async def test_a_bar_that_has_not_closed_yet_is_never_ready_and_is_not_counted_as_missing(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    event = await service.on_bar_close("15m", NEXT_BAR, [SYM])  # 服务器在 B+5s，[B, B+15m) 这根还在走
    assert event.symbols == () and service.missing_periods(SYM, "15m") == 0
    world.advance(P15)
    assert (await service.on_bar_close("15m", NEXT_BAR, [SYM])).symbols == (SYM,)  # 收盘之后同一根就能就绪


async def test_without_a_usable_server_time_nobody_is_ready(make_world, make_service):
    world = make_world()
    world.server_time_ok = False
    service, _ = make_service(world)
    event = await service.on_bar_close("15m", BAR, [SYM, "BBBUSDT"])
    assert event.symbols == () and world.requests == []
    assert service.missing_periods(SYM, "15m") == 1


# ------------------------------------------------------------------ missing_periods


async def test_missing_periods_count_the_consecutive_bars_a_symbol_was_not_ready(make_world, make_service):
    world = make_world()
    world.failing.add("DOWNUSDT")
    service, _ = make_service(world)
    assert service.missing_periods("DOWNUSDT", "15m") == 0
    await service.on_bar_close("15m", BAR, ["DOWNUSDT", SYM])
    assert (service.missing_periods("DOWNUSDT", "15m"), service.missing_periods(SYM, "15m")) == (1, 0)
    world.advance(P15)
    await service.on_bar_close("15m", BAR + P15, ["DOWNUSDT", SYM])
    world.advance(P15)
    await service.on_bar_close("15m", BAR + 2 * P15, ["DOWNUSDT", SYM])
    assert (service.missing_periods("DOWNUSDT", "15m"), service.missing_periods(SYM, "15m")) == (3, 0)


async def test_a_retry_for_the_same_bar_counts_only_once(make_world, make_service):
    world = make_world()
    world.failing.add("DOWNUSDT")
    service, _ = make_service(world)
    for _ in range(4):
        await service.on_bar_close("15m", BAR, ["DOWNUSDT"])
    assert service.missing_periods("DOWNUSDT", "15m") == 1


async def test_a_symbol_that_becomes_ready_starts_counting_from_zero_again(make_world, make_service):
    world = make_world()
    world.failing.add("DOWNUSDT")
    service, _ = make_service(world)
    await service.on_bar_close("15m", BAR, ["DOWNUSDT"])
    world.advance(P15)
    await service.on_bar_close("15m", BAR + P15, ["DOWNUSDT"])
    assert service.missing_periods("DOWNUSDT", "15m") == 2
    world.failing.clear()
    world.advance(P15)
    event = await service.on_bar_close("15m", BAR + 2 * P15, ["DOWNUSDT"])
    assert event.symbols == ("DOWNUSDT",) and service.missing_periods("DOWNUSDT", "15m") == 0
    world.failing.add("DOWNUSDT")
    world.advance(P15)
    await service.on_bar_close("15m", BAR + 3 * P15, ["DOWNUSDT"])
    assert service.missing_periods("DOWNUSDT", "15m") == 1  # 重新从 1 数起


async def test_a_late_retry_for_an_older_bar_does_not_count(make_world, make_service):
    world = make_world()
    world.failing.add("DOWNUSDT")
    service, _ = make_service(world)
    await service.on_bar_close("15m", BAR, ["DOWNUSDT"])
    world.advance(P15)
    await service.on_bar_close("15m", BAR + P15, ["DOWNUSDT"])
    await service.on_bar_close("15m", BAR, ["DOWNUSDT"])  # 迟到的重试：前一根
    assert service.missing_periods("DOWNUSDT", "15m") == 2


async def test_after_a_recovery_the_bar_that_was_missed_is_ready_on_a_late_retry_and_the_count_stays_zero(
        make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.on_bar_close("15m", BAR, [SYM])
    world.advance(P15)
    await service.on_bar_close("15m", BAR + P15, [SYM])
    world.failing.add(SYM)
    world.advance(P15)
    await service.on_bar_close("15m", BAR + 2 * P15, [SYM])
    assert service.missing_periods(SYM, "15m") == 1
    world.advance(P15)
    world.failing.clear()
    event = await service.on_bar_close("15m", BAR + 3 * P15, [SYM])
    assert event.symbols == (SYM,) and service.missing_periods(SYM, "15m") == 0
    assert world.requests[-1]["limit"] == 4  # 缓存里缺 2 根：这次一并补齐了中间那根
    late = await service.on_bar_close("15m", BAR + 2 * P15, [SYM])  # 迟到的重试：补齐之后缓存里有它，就绪
    assert late.symbols == (SYM,) and service.missing_periods(SYM, "15m") == 0


async def test_a_late_retry_for_a_bar_that_rolled_out_of_the_cache_is_not_ready_and_does_not_count(make_world,
                                                                                                    make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.on_bar_close("15m", BAR, [SYM])
    world.advance(200 * P15)  # 停了 200 根：缓存断档，只留新取到的一段
    assert (await service.on_bar_close("15m", BAR + 200 * P15, [SYM])).symbols == (SYM,)
    late = await service.on_bar_close("15m", BAR + 50 * P15, [SYM])  # 那一根早已不在缓存里
    assert late.symbols == () and service.missing_periods(SYM, "15m") == 0


async def test_counts_are_kept_per_symbol_and_per_timeframe(make_world, make_service):
    world = make_world()
    world.failing.add("DOWNUSDT")
    service, _ = make_service(world)
    await service.on_bar_close("15m", BAR, ["DOWNUSDT"])
    await service.on_bar_close("15m", BAR, ["DOWNUSDT"])
    assert service.missing_periods("DOWNUSDT", "15m") == 1
    assert service.missing_periods("DOWNUSDT", "1h") == 0
    await service.on_bar_close("1h", BOUNDARY_MS - P1H, ["DOWNUSDT"])
    assert (service.missing_periods("DOWNUSDT", "15m"), service.missing_periods("DOWNUSDT", "1h")) == (1, 1)
    assert service.missing_periods(SYM, "15m") == 0


# ------------------------------------------------------------------ 时限与规模


async def test_symbols_not_back_by_the_deadline_are_left_out_and_their_requests_are_cancelled(make_world,
                                                                                                make_service):
    adapter = SlowAdapter(slow_symbols={"SLOWUSDT": 30.0})
    world = make_world(adapter=adapter)
    service, _ = make_service(world, deadline=0.3)
    started = time.monotonic()
    event = await service.on_bar_close("15m", BAR, ["FASTUSDT", "SLOWUSDT", "QUICKUSDT"])
    elapsed = time.monotonic() - started
    assert event.symbols == ("FASTUSDT", "QUICKUSDT")
    assert 0.25 <= elapsed < 3.0  # 等满时限，不会一直等下去
    assert adapter.in_flight == 0  # 超时的请求已取消
    assert service.closed("SLOWUSDT", "15m") == [] and service.missing_periods("SLOWUSDT", "15m") == 1


async def test_what_came_back_before_the_deadline_is_kept_and_saved(make_world, make_service, raw_db):
    adapter = SlowAdapter(slow_symbols={"SLOWUSDT": 30.0})
    service, _ = make_service(make_world(adapter=adapter), deadline=0.2)
    event = await service.on_bar_close("15m", BAR, ["FASTUSDT", "SLOWUSDT"])
    assert event.symbols == ("FASTUSDT",)
    assert len(raw_db.candle_times("FASTUSDT", "15m")) == 98 and raw_db.candle_times("SLOWUSDT", "15m") == []
    assert len(service.closed("FASTUSDT", "15m")) == 98


async def test_the_deadline_counts_from_the_call_including_the_time_calibrating(make_world, make_service):
    adapter = SlowClockAdapter(0.25, delay_s=0.2)  # 校准 0.25 秒 + 读 K 线 0.2 秒 > 时限 0.3 秒
    service, _ = make_service(make_world(adapter=adapter), deadline=0.3)
    started = time.monotonic()
    event = await service.on_bar_close("15m", BAR, [SYM])
    assert event.symbols == () and time.monotonic() - started < 0.5  # 在时限处就返回，没有再多等一个时限


async def test_a_deadline_that_expires_during_calibration_leaves_nobody_ready_but_the_calibration_goes_on(
        make_world, make_service):
    world = make_world(adapter=SlowClockAdapter(0.3))
    service, server_clock = make_service(world, deadline=0.1)
    started = time.monotonic()
    event = await service.on_bar_close("15m", BAR, [SYM, "BBBUSDT"])
    assert event.symbols == () and time.monotonic() - started < 0.25
    assert world.requests == [] and service.missing_periods(SYM, "15m") == 1
    assert server_clock.skew_ms is None  # 还在校准途中
    while server_clock.skew_ms is None:  # 大家共用的那次校准没被连带取消，自己做完
        await asyncio.sleep(0.01)
    assert world.server_time_calls == 1


async def test_the_deadline_is_read_from_the_config_on_every_call(make_world, ledger, bus):
    from dmr_kernel.marketdata.candles import CandleService
    from dmr_kernel.marketdata.clock import ServerClock

    adapter = SlowAdapter(slow_symbols={"SLOWUSDT": 0.5})
    world = make_world(adapter=adapter)
    config = {"kline_ready_deadline_s": 0.15}
    service = CandleService(adapter, ledger, bus, world.clock, ServerClock(world.clock), config_get=config.__getitem__)
    assert (await service.on_bar_close("15m", BAR, ["SLOWUSDT"])).symbols == ()
    config["kline_ready_deadline_s"] = 5
    assert (await service.on_bar_close("15m", BAR, ["SLOWUSDT"])).symbols == ("SLOWUSDT",)  # 同一根重试，这回来得及
    assert service.missing_periods("SLOWUSDT", "15m") == 0


async def test_70_symbols_in_two_timeframes_are_ready_within_the_deadline(make_world, make_service, raw_db):
    adapter = SlowAdapter(delay_s=0.05)  # 每次请求 50 毫秒；串行要 140 × 0.05 = 7 秒，并发 10 约 0.7 秒
    world = make_world(adapter=adapter)
    service, _ = make_service(world, deadline=3)
    symbols = [f"S{i:02d}USDT" for i in range(70)]
    started = time.monotonic()
    quarter, hour = await asyncio.gather(service.on_bar_close("15m", BAR, symbols),
                                         service.on_bar_close("1h", BOUNDARY_MS - P1H, symbols))
    assert time.monotonic() - started < 3
    assert quarter.symbols == tuple(symbols) and hour.symbols == tuple(symbols)
    assert adapter.max_in_flight == 10
    assert len(world.requests) == 140 and all(1 <= r["limit"] <= 99 for r in world.requests)  # 权重预算
    assert raw_db.candle_count() == 140 * 98


async def test_cancelling_on_bar_close_cancels_the_requests_in_flight(make_world, make_service):
    adapter = SlowAdapter(delay_s=30)
    service, _ = make_service(make_world(adapter=adapter))
    task = asyncio.ensure_future(service.on_bar_close("15m", BAR, [f"S{i:02d}USDT" for i in range(15)]))
    while adapter.in_flight < 10:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.in_flight == 0
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()] == []


# ------------------------------------------------------------------ 参数


@pytest.mark.parametrize(("timeframe", "bar_open_ms", "error"), [
    ("15m", BAR + 1, ValueError),  # 没对齐周期边界
    ("1h", BOUNDARY_MS - P15, ValueError),  # 15 分钟线的开盘时间不是整点
    ("1x", BAR, ValueError),
    ("15m", True, TypeError),
    ("15m", float(BAR), TypeError),
    ("15m", str(BAR), TypeError),
])
async def test_bad_arguments_are_refused_before_anything_happens(make_world, make_service, event_log, timeframe,
                                                                  bar_open_ms, error):
    world = make_world()
    service, _ = make_service(world)
    seen = event_log(CandlesReady)
    with pytest.raises(error):
        await service.on_bar_close(timeframe, bar_open_ms, [SYM])
    assert world.requests == [] and world.server_time_calls == 0 and await seen.settle() == []


async def test_symbols_must_be_a_collection_not_a_single_string(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    with pytest.raises(TypeError):
        await service.on_bar_close("15m", BAR, "AAAUSDT")  # type: ignore[arg-type]
    assert world.requests == []


@pytest.mark.parametrize("bad", [0, -5, None, "20", True, float("nan"), float("inf")])
async def test_a_bad_deadline_in_the_config_is_refused_before_anything_is_sent(make_world, make_service, bad):
    world = make_world()
    service, _ = make_service(world, deadline=bad)
    with pytest.raises(ValueError, match="kline_ready_deadline_s"):
        await service.on_bar_close("15m", BAR, [SYM])
    assert world.requests == [] and world.server_time_calls == 0
