"""marketdata.candles：校准服务器时间（sync_server_time）与只返回已收盘的 K 线（fetch_closed）（T0.7）。

核心约束：**未收盘的 K 线永远不会被返回**，是否收盘只按服务器时间判断（open_time + 周期 <= 服务器时间 才算收盘）。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from dmr_kernel.core.types import Candle, Known, Unknown
from dmr_kernel.marketdata.clock import MAX_PLAUSIBLE_SKEW_MS, MAX_ROUND_TRIP_MS
from marketdata_world import BOUNDARY_MS, P1H, P15, SlowAdapter, SlowClockAdapter, World, make_bar

CANDLES_LOGGER = "dmr_kernel.marketdata.candles"
PERIODS = {"15m": P15, "1h": P1H}


def opens(result) -> list[int]:
    assert isinstance(result, Known), result
    return [bar.open_time_ms for bar in result.value]


# ------------------------------------------------------------------ sync_server_time


async def test_sync_calibrates_the_server_clock_from_the_exchange_reading(make_world, make_service):
    world = make_world(local_offset_ms=10_000)  # 本机快 10 秒
    service, server_clock = make_service(world)
    assert await service.sync_server_time() is True
    assert world.server_time_calls == 1
    assert server_clock.skew_ms == -10_000
    assert server_clock.now_server_ms() == world.server_ms
    assert server_clock.updated_ms == world.clock.now_ms()


async def test_the_round_trip_time_does_not_bias_the_offset(make_world, make_service):
    world = make_world(local_offset_ms=-3_000)
    world.rtt_ms = 400  # 服务器在往返中点读数
    service, server_clock = make_service(world)
    assert await service.sync_server_time() is True
    assert server_clock.skew_ms == 3_000
    assert server_clock.now_server_ms() == world.server_ms


async def test_a_later_sync_replaces_the_offset(make_world, make_service):
    world = make_world(local_offset_ms=2_000)
    service, server_clock = make_service(world)
    await service.sync_server_time()
    assert server_clock.skew_ms == -2_000
    world.clock.advance(1_500)  # 本机时钟漂了 1.5 秒
    assert await service.sync_server_time() is True
    assert server_clock.skew_ms == -3_500


async def test_a_round_trip_of_exactly_the_limit_is_accepted_and_one_millisecond_more_is_not(make_world, make_service):
    world = make_world(local_offset_ms=500)
    service, server_clock = make_service(world)
    world.rtt_ms = MAX_ROUND_TRIP_MS
    assert await service.sync_server_time() is True
    assert server_clock.skew_ms == -500
    world.clock.advance(1_000)  # 若下面的样本被采用，偏移会变成 -1500
    world.rtt_ms = MAX_ROUND_TRIP_MS + 1
    assert await service.sync_server_time() is False
    assert server_clock.skew_ms == -500


def _unknown_reading(world: World) -> None:
    world.server_time_ok = False


def _backwards_clock(world: World) -> None:
    world.rtt_ms = -500  # 本机时钟在往返途中被往回拨


def _slow_round_trip(world: World) -> None:
    world.rtt_ms = MAX_ROUND_TRIP_MS + 500


def _zero_answer(world: World) -> None:
    world.server_time_answer = 0


def _negative_answer(world: World) -> None:
    world.server_time_answer = -world.server_ms


def _seconds_instead_of_milliseconds(world: World) -> None:
    world.server_time_answer = world.server_ms // 1000


def _far_future_answer(world: World) -> None:
    world.server_time_answer = world.clock.now_ms() + MAX_PLAUSIBLE_SKEW_MS + 1


def _text_answer(world: World) -> None:
    world.server_time_answer = str(world.server_ms)


def _float_answer(world: World) -> None:
    world.server_time_answer = float(world.server_ms)


def _boolean_answer(world: World) -> None:
    world.server_time_answer = True


def _raising_adapter(world: World) -> None:
    def boom():
        raise RuntimeError("连接被重置")

    world.adapter.set_read("server_time", boom)


BAD_SAMPLES = [_unknown_reading, _backwards_clock, _slow_round_trip, _zero_answer, _negative_answer,
               _seconds_instead_of_milliseconds, _far_future_answer, _text_answer, _float_answer, _boolean_answer,
               _raising_adapter]


@pytest.mark.parametrize("spoil", BAD_SAMPLES, ids=lambda f: f.__name__.strip("_"))
async def test_an_unusable_sample_fails_the_sync_and_keeps_the_previous_offset(make_world, make_service, spoil, caplog):
    world = make_world(local_offset_ms=1_000)
    service, server_clock = make_service(world)
    assert await service.sync_server_time() is True
    before = (server_clock.skew_ms, server_clock.updated_ms)
    world.clock.advance(5_000)  # 若坏样本被采用，偏移一定会变
    spoil(world)
    with caplog.at_level(logging.WARNING, logger=CANDLES_LOGGER):
        assert await service.sync_server_time() is False
    assert (server_clock.skew_ms, server_clock.updated_ms) == before
    assert any(r.levelno >= logging.WARNING and "服务器时间" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("spoil", BAD_SAMPLES, ids=lambda f: f.__name__.strip("_"))
async def test_an_unusable_first_sample_leaves_the_clock_uncalibrated(make_world, make_service, spoil):
    world = make_world(local_offset_ms=1_000)
    service, server_clock = make_service(world)
    spoil(world)
    assert await service.sync_server_time() is False
    assert server_clock.skew_ms is None and server_clock.updated_ms is None


# ------------------------------------------------------------------ fetch_closed：按服务器时间剔除未收盘


@pytest.mark.parametrize("timeframe", ["15m", "1h"])
@pytest.mark.parametrize(("delta_ms", "last_closed_back"), [(-1, 2), (0, 1), (1, 1)])
async def test_a_bar_is_closed_only_once_the_server_time_reaches_its_end(make_world, make_service, timeframe, delta_ms,
                                                                         last_closed_back):
    period = PERIODS[timeframe]
    world = make_world(server_ms=BOUNDARY_MS + delta_ms)  # 边界前 1 毫秒 / 正好 / 后 1 毫秒
    service, _ = make_service(world)
    result = await service.fetch_closed("AAAUSDT", timeframe, 5)
    expected = world.latest_closed("AAAUSDT", timeframe, 4)
    assert opens(result)[-1] == BOUNDARY_MS - last_closed_back * period
    assert result.value == expected
    assert all(bar.open_time_ms + period <= world.server_ms for bar in result.value)


@pytest.mark.parametrize(("server_delta_ms", "local_offset_ms", "last_closed_back"), [
    (-5_000, 10_000, 2),  # 服务器：边界前 5 秒，那根还在走；本机快 10 秒，按本机会以为已收盘
    (5_000, -10_000, 1),  # 服务器：边界后 5 秒，那根已收盘；本机慢 10 秒，按本机会以为还没收盘
])
async def test_closure_follows_the_server_time_not_the_local_clock(make_world, make_service, server_delta_ms,
                                                                   local_offset_ms, last_closed_back):
    world = make_world(server_ms=BOUNDARY_MS + server_delta_ms, local_offset_ms=local_offset_ms)
    service, _ = make_service(world)
    result = await service.fetch_closed("AAAUSDT", "15m", 6)
    assert opens(result)[-1] == BOUNDARY_MS - last_closed_back * P15
    assert result.value == world.latest_closed("AAAUSDT", "15m", 5)


async def test_a_bar_that_closes_while_the_request_is_in_flight_is_not_trusted(make_world, make_service):
    world = make_world(server_ms=BOUNDARY_MS - 100)
    service, _ = make_service(world)
    assert await service.sync_server_time() is True
    exchange = world._fetch_candles

    def answers_after_the_boundary(symbol, interval, limit):
        world.advance(250)  # 请求在途中，服务器时间越过了边界
        return exchange(symbol, interval, limit)

    world.adapter.set_read("fetch_candles", answers_after_the_boundary)
    result = await service.fetch_closed("AAAUSDT", "15m", 5)
    # 发请求时那根还没收盘：即使答复里它已经完整，也按发请求时的服务器时间判断，不采信
    assert opens(result)[-1] == BOUNDARY_MS - 2 * P15
    world.adapter.set_read("fetch_candles", world._fetch_candles)
    assert opens(await service.fetch_closed("AAAUSDT", "15m", 5))[-1] == BOUNDARY_MS - P15  # 下一次就能拿到


async def test_every_unclosed_bar_is_dropped_not_only_the_last_one(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    closed = world.latest_closed("AAAUSDT", "15m", 3)
    forming = [make_bar("AAAUSDT", BOUNDARY_MS + i * P15, P15) for i in range(3)]  # 在走的一根和两根「未来」的
    world.adapter.set_read("fetch_candles", lambda symbol, interval, limit: closed + forming)
    assert (await service.fetch_closed("AAAUSDT", "15m", 6)).value == closed


async def test_when_every_bar_is_unclosed_the_answer_is_an_empty_known_list_not_unknown(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    world.adapter.set_read("fetch_candles", lambda symbol, interval, limit: [make_bar(symbol, BOUNDARY_MS, P15)])
    result = await service.fetch_closed("AAAUSDT", "15m", 2)
    assert result == Known([])


async def test_bars_come_back_ascending_and_without_duplicates(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    c1, c2, c3 = world.latest_closed("AAAUSDT", "15m", 3)
    revised_c2 = Candle(open_time_ms=c2.open_time_ms, open=1.0, high=2.0, low=0.5, close=1.5, volume=9.0,
                          close_time_ms=c2.close_time_ms)
    world.adapter.set_read("fetch_candles", lambda symbol, interval, limit: [c3, c1, c2, revised_c2, c1])
    result = await service.fetch_closed("AAAUSDT", "15m", 6)
    assert result.value == [c1, revised_c2, c3]  # 同一根出现多次，以最后出现的为准


async def test_the_requested_limit_is_sent_as_given_and_the_default_is_99(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.fetch_closed("AAAUSDT", "15m", 1)
    await service.fetch_closed("AAAUSDT", "15m", 99)
    await service.fetch_closed("AAAUSDT", "15m")
    assert [r["limit"] for r in world.requests] == [1, 99, 99]
    assert {(r["symbol"], r["interval"]) for r in world.requests} == {("AAAUSDT", "15m")}


async def test_limit_one_returns_nothing_when_the_only_bar_is_still_forming(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    assert await service.fetch_closed("AAAUSDT", "15m", 1) == Known([])


# ------------------------------------------------------------------ fetch_closed：自动校准


async def test_the_first_fetch_calibrates_the_clock_once(make_world, make_service):
    world = make_world(local_offset_ms=7_000)
    service, server_clock = make_service(world)
    assert server_clock.skew_ms is None
    await service.fetch_closed("AAAUSDT", "15m", 5)
    await service.fetch_closed("BBBUSDT", "15m", 5)
    assert world.server_time_calls == 1
    assert server_clock.skew_ms == -7_000
    assert [r["symbol"] for r in world.requests] == ["AAAUSDT", "BBBUSDT"]


async def test_an_already_calibrated_clock_is_not_calibrated_again(make_world, make_service):
    world = make_world()
    service, _ = make_service(world)
    await service.sync_server_time()
    await service.fetch_closed("AAAUSDT", "15m", 5)
    assert world.server_time_calls == 1


async def test_without_a_usable_server_time_nothing_is_requested_and_the_answer_is_unknown(make_world, make_service):
    world = make_world()
    world.server_time_ok = False
    service, server_clock = make_service(world)
    result = await service.fetch_closed("AAAUSDT", "15m", 5)
    assert isinstance(result, Unknown) and "服务器时间" in result.reason
    assert world.requests == [] and server_clock.skew_ms is None
    world.server_time_ok = True  # 下一次还会再试一次校准
    assert isinstance(await service.fetch_closed("AAAUSDT", "15m", 5), Known)
    assert world.server_time_calls == 2


async def test_concurrent_first_fetches_share_one_calibration(make_world, make_service):
    # 校准要等一会儿：并发的第一批请求都会撞上「还没校准」
    world = make_world(adapter=SlowClockAdapter(0.01))
    service, _ = make_service(world)
    results = await asyncio.gather(*(service.fetch_closed(f"S{i}USDT", "15m", 5) for i in range(12)))
    assert all(isinstance(r, Known) for r in results)
    assert world.server_time_calls == 1


async def test_concurrent_explicit_syncs_share_one_reading_and_a_later_one_reads_again(make_world, make_service):
    world = make_world(adapter=SlowClockAdapter(0.01))
    service, _ = make_service(world)
    assert await asyncio.gather(service.sync_server_time(), service.sync_server_time()) == [True, True]
    assert world.server_time_calls == 1
    assert await service.sync_server_time() is True
    assert world.server_time_calls == 2


async def test_a_waiter_that_gives_up_does_not_cancel_the_calibration_the_others_wait_for(make_world, make_service):
    world = make_world(adapter=SlowClockAdapter(0.3))
    service, server_clock = make_service(world)
    patient = asyncio.ensure_future(service.fetch_closed("AAAUSDT", "15m", 5))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(service.fetch_closed("BBBUSDT", "15m", 5), 0.05)  # 等不及的那个先走
    result = await patient
    assert isinstance(result, Known) and server_clock.skew_ms == 0
    assert world.server_time_calls == 1
    assert [r["symbol"] for r in world.requests] == ["AAAUSDT"]  # 走掉的那个没有留下请求


async def test_direct_fetches_share_the_concurrency_cap_with_refreshes(make_world, make_service):
    adapter = SlowAdapter(delay_s=0.02)
    service, _ = make_service(make_world(adapter=adapter))
    await service.sync_server_time()
    refreshing = service.refresh([f"R{i:02d}USDT" for i in range(15)], "15m")
    fetching = [service.fetch_closed(f"F{i:02d}USDT", "15m", 5) for i in range(15)]
    refreshed, *fetched = await asyncio.gather(refreshing, *fetching)
    assert all(refreshed.values()) and all(isinstance(r, Known) for r in fetched)
    assert adapter.max_in_flight == 10


# ------------------------------------------------------------------ fetch_closed：读失败与参数


async def test_a_read_failure_is_passed_on_as_unknown_with_its_reason(make_world, make_service):
    world = make_world()
    world.failing.add("AAAUSDT")
    service, _ = make_service(world)
    result = await service.fetch_closed("AAAUSDT", "15m", 5)
    assert isinstance(result, Unknown) and result.reason == "模拟读失败"
    assert isinstance(await service.fetch_closed("BBBUSDT", "15m", 5), Known)


async def test_an_adapter_exception_becomes_unknown_instead_of_escaping(make_world, make_service, caplog):
    world = make_world()
    world.raising.add("AAAUSDT")
    service, _ = make_service(world)
    with caplog.at_level(logging.WARNING, logger=CANDLES_LOGGER):
        result = await service.fetch_closed("AAAUSDT", "15m", 5)
    assert isinstance(result, Unknown) and "AAAUSDT" in result.reason and "模拟适配层异常" in result.reason
    assert any("AAAUSDT" in r.getMessage() for r in caplog.records)
    assert isinstance(await service.fetch_closed("BBBUSDT", "15m", 5), Known)


async def test_cancelling_a_fetch_is_not_swallowed(make_world, make_service):
    adapter = SlowAdapter(slow_symbols={"SLOWUSDT": 30.0})
    world = make_world(adapter=adapter)
    service, _ = make_service(world)
    await service.sync_server_time()
    task = asyncio.ensure_future(service.fetch_closed("SLOWUSDT", "15m", 5))
    while adapter.in_flight == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.in_flight == 0


@pytest.mark.parametrize("limit", [0, -1, 100, 500])
async def test_limit_outside_1_to_99_is_refused_before_anything_is_sent(make_world, make_service, limit):
    world = make_world()
    service, _ = make_service(world)
    with pytest.raises(ValueError, match="limit"):
        await service.fetch_closed("AAAUSDT", "15m", limit)
    assert world.requests == [] and world.server_time_calls == 0


@pytest.mark.parametrize("limit", [True, 5.0, "5", None])
async def test_limit_must_be_an_integer(make_world, make_service, limit):
    world = make_world()
    service, _ = make_service(world)
    with pytest.raises((TypeError, ValueError)):
        await service.fetch_closed("AAAUSDT", "15m", limit)
    assert world.requests == []


@pytest.mark.parametrize("timeframe", ["", "15", "1x", "m", "0m", "1w"])
async def test_an_unknown_timeframe_is_refused(make_world, make_service, timeframe):
    world = make_world()
    service, _ = make_service(world)
    with pytest.raises(ValueError):
        await service.fetch_closed("AAAUSDT", timeframe, 5)
    assert world.requests == []


@pytest.mark.parametrize("symbol", ["", None, 5])
async def test_a_blank_symbol_is_refused(make_world, make_service, symbol):
    world = make_world()
    service, _ = make_service(world)
    with pytest.raises((TypeError, ValueError)):
        await service.fetch_closed(symbol, "15m", 5)
    assert world.requests == []
