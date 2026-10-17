"""保护单管理的收尾改进（FU5）：重试节奏。

重试间隔 = ``protect_retry_window_s`` / (``protect_retries`` + 1)（默认 30 秒、3 次 -> 7.5 / 15 / 22.5 秒）：第 k 次重试在保护周期
开始后 k × 间隔；最后一次重试失败即下「仍失败」的结论。所以接管仓转「无保护」、策略仓发起保护性平仓都落在重试窗口之内
（4.11），而不是恰好在窗口末尾（再加调度器的 tick 间隔与交易所往返）之后。结论之后的持续重试用同一个间隔。

时间一律用 ManualClock 推进；「像调度器那样每 2 秒 tick 一次」用 ``_tick_until`` 模拟。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import AlertLevel, ProtectionState
from protection_kit import BAR0, SYM, T0, TF_MS, history_then

WINDOW_MS = 30_000
RETRY_SLOTS_MS = (7_500, 15_000, 22_500)  # 默认配置：30 秒 / (3 + 1) 的 1、2、3 倍
SCHEDULER_TICK_MS = 2_000  # 调度器每隔几秒调一次 tick()，设计上不超过 2 秒
RISING = [100.5, 101.0, 101.5, 102.0, 102.5, 103.0]


def _fail_stops(rig, n):
    for _ in range(n):
        rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())


def _state(rig, rec):
    return rig.registry.by_id(rec.id).protection_state


async def _tick_until(rig, done, *, step_ms=SCHEDULER_TICK_MS, limit_ms=120_000):
    """像调度器那样每隔 step_ms 驱动一次 tick，直到 done() 为真；返回从调用时起用了多少毫秒。"""
    started = rig.clock.now_ms()
    while not done():
        assert rig.clock.now_ms() - started < limit_ms, "等不到结论"
        await rig.tick(step_ms)
    return rig.clock.now_ms() - started


async def _tick_to_each_slot(rig, slots_ms=RETRY_SLOTS_MS):
    """依次走到每个重试时刻（距保护周期开始的毫秒数）：时刻前 1 毫秒不该重试，到点正好重试一次。"""
    attempts = len(rig.writes("create_stop_order"))
    for due in slots_ms:
        await rig.tick(due - (rig.clock.now_ms() - T0) - 1)
        assert len(rig.writes("create_stop_order")) == attempts, f"重试不该早于 {due} 毫秒"
        await rig.tick(1)
        attempts += 1
        assert len(rig.writes("create_stop_order")) == attempts, f"到了 {due} 毫秒该重试"


# ---------------------------------------------------------------- 节奏：窗口 / (次数 + 1)


async def test_retries_come_at_one_two_and_three_times_window_over_retries_plus_one(rig):
    await rig.start()
    _fail_stops(rig, 4)
    rec = await rig.open_takeover()
    assert (await rig.protection.ensure_protected(rec.id, reason="takeover")) == "rebuilding"
    assert len(rig.writes("create_stop_order")) == 1 and _state(rig, rec) is ProtectionState.REBUILDING

    await _tick_to_each_slot(rig)  # 7.5、15、22.5 秒各重试一次（旧节奏是 10、20、30 秒）

    assert len(rig.writes("create_stop_order")) == 4  # 第 1 次尝试 + 3 次重试，次数没变
    assert rig.clock.now_ms() - T0 == 22_500


async def test_the_last_retry_failing_is_the_verdict_at_once_inside_the_window(rig):
    await rig.start()
    _fail_stops(rig, 4)
    rec = await rig.open_takeover()
    await rig.protection.ensure_protected(rec.id, reason="takeover")
    await _tick_to_each_slot(rig)
    # 第 3 次（最后一次）重试在 22.5 秒失败：当场转「无保护」，不再等到窗口末尾
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.NONE and "无保护" in row.stop_note
    assert rig.clock.now_ms() - T0 < WINDOW_MS and rig.suppressed()
    (alert,) = rig.notifier.of("protect_unprotected")
    assert alert["level"] is AlertLevel.CRITICAL
    assert rig.writes("create_market_order") == []  # 接管仓不自动平


async def test_the_retries_before_the_last_one_do_not_reach_a_verdict(rig):
    """反例：第 1、2 次重试失败时仍是「重建中」，没有无保护、没有抑制、没有最高级告警。"""
    await rig.start()
    _fail_stops(rig, 4)
    rec = await rig.open_takeover()
    await rig.protection.ensure_protected(rec.id, reason="takeover")
    await _tick_to_each_slot(rig, RETRY_SLOTS_MS[:2])
    assert _state(rig, rec) is ProtectionState.REBUILDING
    assert not rig.suppressed() and rig.notifier.of("protect_unprotected") == []


async def test_with_a_two_second_scheduler_a_takeover_position_goes_unprotected_inside_the_window(rig):
    """调度器每 2 秒 tick 一次：22.5 秒的重试在 24 秒的那次 tick 发生，离 30 秒的窗口还留着一个 tick 以上的余量给交易所往返
    （旧节奏的最后一次重试排在 30 秒整，没有任何余量）。"""
    await rig.start()
    _fail_stops(rig, 8)
    rec = await rig.open_takeover()
    await rig.protection.ensure_protected(rec.id, reason="takeover")
    assert _state(rig, rec) is ProtectionState.REBUILDING

    elapsed = await _tick_until(rig, lambda: _state(rig, rec) is ProtectionState.NONE)

    assert elapsed == 24_000
    assert elapsed <= WINDOW_MS - SCHEDULER_TICK_MS  # 4.11：protect_retry_window_s 秒内转无保护，且留有余量
    assert len(rig.writes("create_stop_order")) == 4 and rig.suppressed()
    assert len(rig.notifier.of("protect_unprotected")) == 1 and rig.writes("create_market_order") == []


async def test_with_a_two_second_scheduler_a_strategy_position_is_closed_inside_window_plus_close_limit(rig):
    await rig.start()
    _fail_stops(rig, 4)
    rec = await rig.open_strategy(qty="2")
    await rig.protection.ensure_protected(rec.id, reason="registered")

    elapsed = await _tick_until(rig, lambda: bool(rig.writes("create_market_order")))

    close_limit_ms = int(rig.config["close_one_timeout_s"]) * 1000
    assert elapsed == 24_000
    assert elapsed <= WINDOW_MS - SCHEDULER_TICK_MS  # 保护性平仓在窗口之内就发起了
    assert elapsed + close_limit_ms <= WINDOW_MS + close_limit_ms  # 平仓再用满单仓时限，也在 窗口 + close_one_timeout_s 之内
    (close,) = rig.writes("create_market_order")
    assert close["qty"] == 2 and "protect_failed_closing" in rig.notifier.kinds()


async def test_a_stop_that_goes_on_at_the_last_retry_slot_ends_the_cycle_inside_the_window(rig):
    await rig.start()
    _fail_stops(rig, 3)  # 第 1 次尝试、第 1、2 次重试失败；第 3 次重试（22.5 秒）挂上了
    rec = await rig.open_strategy()
    await rig.protection.ensure_protected(rec.id, reason="registered")

    elapsed = await _tick_until(rig, lambda: _state(rig, rec) is ProtectionState.SYSTEM)

    assert elapsed == 24_000 and elapsed <= WINDOW_MS - SCHEDULER_TICK_MS
    assert len(rig.exchange.stops()) == 1 and rig.writes("create_market_order") == [] and not rig.suppressed()


# ---------------------------------------------------------------- 其它窗口 / 次数的组合


@pytest.mark.parametrize("window_s, retries", [(30, 3), (60, 3), (30, 1), (10, 5), (5, 20), (300, 20)])
async def test_each_retry_is_due_at_k_times_window_over_retries_plus_one_and_the_last_ends_inside_the_window(
        make_rig, window_s, retries):
    rig = make_rig(protect_retry_window_s=window_s, protect_retries=retries)
    await rig.start()
    _fail_stops(rig, retries + 1)
    rec = await rig.open_takeover()
    await rig.protection.ensure_protected(rec.id, reason="takeover")
    window_ms = window_s * 1000

    slots = [k * window_ms // (retries + 1) for k in range(1, retries + 1)]
    await _tick_to_each_slot(rig, slots)

    assert len(rig.writes("create_stop_order")) == retries + 1
    assert rig.clock.now_ms() - T0 == slots[-1] < window_ms  # 最后一次重试排在窗口之内
    assert _state(rig, rec) is ProtectionState.NONE  # 最后一次重试失败就是结论


# ---------------------------------------------------------------- 结论之后的持续重试用同一个间隔


async def test_after_the_verdict_the_persistent_retries_keep_the_same_interval(rig):
    await rig.start()
    _fail_stops(rig, 8)
    rec = await rig.open_takeover()
    await rig.protection.ensure_protected(rec.id, reason="takeover")
    await _tick_to_each_slot(rig)
    assert _state(rig, rec) is ProtectionState.NONE and len(rig.writes("create_stop_order")) == 4

    for expected in (5, 6, 7):  # 结论之后每 7.5 秒再试一次
        await rig.tick(7_499)
        assert len(rig.writes("create_stop_order")) == expected - 1
        await rig.tick(1)
        assert len(rig.writes("create_stop_order")) == expected
    assert _state(rig, rec) is ProtectionState.NONE and rig.suppressed()


async def test_a_persistent_retry_that_succeeds_restores_the_protection(rig):
    await rig.start()
    _fail_stops(rig, 4)  # 窗口内全失败；之后第一次持续重试挂上了
    rec = await rig.open_takeover()
    await rig.protection.ensure_protected(rec.id, reason="takeover")
    await _tick_to_each_slot(rig)
    assert _state(rig, rec) is ProtectionState.NONE and rig.suppressed()

    await rig.tick(7_500)

    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and not rig.suppressed()
    assert "protect_restored" in rig.notifier.kinds()


async def test_a_failed_protective_close_is_retried_at_the_same_interval(rig):
    """保护性平仓被拒：保持「无保护」，之后同样的间隔再试（先再挂止损，挂不上再平仓）。"""
    await rig.start()
    _fail_stops(rig, 8)
    rig.exchange.push_write_result("create_market_order", rig.exchange.reject())
    rec = await rig.open_strategy()
    await rig.protection.ensure_protected(rec.id, reason="registered")
    await _tick_to_each_slot(rig)
    assert len(rig.writes("create_market_order")) == 1 and _state(rig, rec) is ProtectionState.NONE

    await rig.tick(7_499)
    assert len(rig.writes("create_stop_order")) == 4  # 间隔没到
    await rig.tick(1)
    assert len(rig.writes("create_stop_order")) == 5 and len(rig.writes("create_market_order")) == 2


async def test_with_zero_retries_the_persistent_interval_is_the_whole_window(make_rig):
    """没有重试时 窗口 / (0 + 1) 就是整个窗口：第一次失败即结论，之后每 30 秒再试一次（不会除零、也不会更密）。"""
    rig = make_rig(protect_retries=0)
    await rig.start()
    _fail_stops(rig, 3)
    rec = await rig.open_takeover()
    assert (await rig.protection.ensure_protected(rec.id, reason="takeover")) == "unprotected"
    assert len(rig.writes("create_stop_order")) == 1

    await rig.tick(29_999)
    assert len(rig.writes("create_stop_order")) == 1
    await rig.tick(1)
    assert len(rig.writes("create_stop_order")) == 2


# ---------------------------------------------------------------- 与 gap 模式的空窗上限一起用


async def test_the_gap_limit_still_caps_a_retry_that_would_come_later_than_it(make_rig):
    """gap 模式新旧止损都没挂上：重试排在 min(窗口节奏, 撤旧单起 stop_gap_max_s)，到了上限仍失败就直接下结论。
    窗口 60 秒 / 3 次 -> 自然节奏 15、30、45 秒；上限 18 秒：第 1 次重试照自然节奏在 15 秒，第 2 次被上限提前到 18 秒，
    18 秒的那次失败就是结论（旧节奏 20 秒的第 1 次重试会被上限提前到 18 秒，看不出自然节奏）。"""
    rig = make_rig(stop_gap_max_s=18, protect_retry_window_s=60, protect_retries=3)
    await rig.start()
    rec = await rig.open_takeover()
    rec = await rig.registry.mark_managed(rec.id)
    rec, _ = await rig.with_system_stop(rec, "97")
    rig.candles.set(SYM, history_then(RISING))
    rig.exchange.set_price(SYM, "105")
    await rig.settle()
    _fail_stops(rig, 4)  # 新单、按旧价重挂、15 秒的重试、18 秒的重试
    await rig.protection.trailing(BAR0 + 6 * TF_MS)
    await rig.settle()
    (intent,) = await rig.intents_of("stop_replace")
    assert intent.params["result"]["outcome"] == "unprotected"
    assert len(rig.writes("create_stop_order")) == 2 and _state(rig, rec) is ProtectionState.REBUILDING

    await rig.tick(14_999)
    assert len(rig.writes("create_stop_order")) == 2
    await rig.tick(1)  # 15 秒：自然节奏的第 1 次重试
    assert len(rig.writes("create_stop_order")) == 3 and _state(rig, rec) is ProtectionState.REBUILDING
    await rig.tick(2_999)
    assert len(rig.writes("create_stop_order")) == 3
    await rig.tick(1)  # 18 秒：上限；这次也失败 -> 「仍失败」
    assert len(rig.writes("create_stop_order")) == 4
    assert _state(rig, rec) is ProtectionState.NONE and rig.suppressed()
    assert rig.notifier.of("protect_unprotected") and rig.writes("create_market_order") == []
