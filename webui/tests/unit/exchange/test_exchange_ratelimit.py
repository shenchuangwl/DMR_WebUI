"""exchange.ratelimit：已用权重的软上限暂停（保护类不暂停）、429/418 按 Retry-After 退避、过了这一分钟自动恢复。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.exchange.ratelimit import WEIGHT_HEADER, RateLimiter

MINUTE = 60_000
# 某一分钟的开头（整分钟）
M0 = 1_791_943_200_000
assert M0 % MINUTE == 0


def make(soft=1500, start=M0 + 20_000):
    clock = ManualClock(start)
    box = {"soft": soft}
    return RateLimiter(clock, lambda: box["soft"]), clock, box


def test_fresh_limiter_knows_nothing_and_pauses_nothing():
    rl, _, _ = make()
    assert rl.used_weight is None
    assert rl.pause_s(False) == 0 and rl.pause_s(True) == 0


def test_observe_records_the_used_weight_header():
    rl, _, _ = make()
    rl.observe({WEIGHT_HEADER: "37"}, 200)
    assert rl.used_weight == 37
    rl.observe({WEIGHT_HEADER: "52"}, 200)
    assert rl.used_weight == 52


def test_header_name_is_case_insensitive():
    rl, _, _ = make()
    rl.observe({"x-mbx-used-weight-1m": "12"}, 200)
    assert rl.used_weight == 12


def test_missing_or_garbage_header_keeps_the_previous_value():
    rl, _, _ = make()
    rl.observe({WEIGHT_HEADER: "40"}, 200)
    rl.observe({}, 200)
    rl.observe({WEIGHT_HEADER: "abc"}, 200)
    rl.observe({WEIGHT_HEADER: ""}, 200)
    rl.observe({WEIGHT_HEADER: "-5"}, 200)
    assert rl.used_weight == 40


# ---------------------------------------------------------------- 软上限


def test_weight_at_the_soft_limit_does_not_pause_but_above_it_does():
    rl, _, _ = make(soft=1500)
    rl.observe({WEIGHT_HEADER: "1500"}, 200)
    assert rl.pause_s(False) == 0
    rl.observe({WEIGHT_HEADER: "1501"}, 200)
    assert rl.pause_s(False) > 0


def test_soft_limit_pause_lasts_until_the_next_minute_boundary():
    rl, clock, _ = make(start=M0 + 20_000)
    rl.observe({WEIGHT_HEADER: "1800"}, 200)
    assert rl.pause_s(False) == pytest.approx(40.0)
    clock.advance(15_000)
    assert rl.pause_s(False) == pytest.approx(25.0)


def test_protective_requests_are_never_paused_by_the_soft_limit():
    rl, _, _ = make()
    rl.observe({WEIGHT_HEADER: "2400"}, 200)
    assert rl.pause_s(True) == 0
    assert rl.pause_s(False) > 0


def test_pause_rolls_out_at_the_next_minute():
    rl, clock, _ = make(start=M0 + 59_000)
    rl.observe({WEIGHT_HEADER: "1800"}, 200)
    assert rl.pause_s(False) == pytest.approx(1.0)
    clock.advance(1_000)  # 整分钟
    assert rl.pause_s(False) == 0
    # 这个读数只是「最近一次」，没有新响应前仍保留
    assert rl.used_weight == 1800


def test_a_lower_reading_in_the_same_minute_lifts_the_pause():
    rl, clock, _ = make()
    rl.observe({WEIGHT_HEADER: "1800"}, 200)
    assert rl.pause_s(False) > 0
    clock.advance(1_000)
    rl.observe({WEIGHT_HEADER: "900"}, 200)
    assert rl.pause_s(False) == 0


def test_soft_limit_is_read_every_time_so_config_changes_apply_at_once():
    rl, _, box = make(soft=1500)
    rl.observe({WEIGHT_HEADER: "1200"}, 200)
    assert rl.pause_s(False) == 0
    box["soft"] = 1000  # 操作员在界面调低
    assert rl.pause_s(False) > 0
    box["soft"] = 2400
    assert rl.pause_s(False) == 0


# ---------------------------------------------------------------- 429 / 418


@pytest.mark.parametrize("status", [429, 418])
def test_rate_limit_responses_back_off_non_protective_reads_by_retry_after(status):
    rl, clock, _ = make()
    rl.observe({"Retry-After": "7", WEIGHT_HEADER: "10"}, status)
    assert rl.pause_s(False) == pytest.approx(7.0)
    assert rl.pause_s(True) == 0  # 保护类不受影响
    clock.advance(6_000)
    assert rl.pause_s(False) == pytest.approx(1.0)
    clock.advance(1_000)
    assert rl.pause_s(False) == 0


def test_retry_after_may_be_fractional_and_header_case_does_not_matter():
    rl, _, _ = make()
    rl.observe({"retry-after": "2.5"}, 429)
    assert rl.pause_s(False) == pytest.approx(2.5)


def test_429_without_a_usable_retry_after_waits_for_the_next_minute():
    rl, _, _ = make(start=M0 + 50_000)
    rl.observe({}, 429)
    assert rl.pause_s(False) == pytest.approx(10.0)
    rl2, _, _ = make(start=M0 + 50_000)
    rl2.observe({"Retry-After": "soon"}, 429)
    assert rl2.pause_s(False) == pytest.approx(10.0)


def test_418_without_retry_after_assumes_a_long_ban():
    rl, _, _ = make()
    rl.observe({}, 418)
    assert rl.pause_s(False) >= 60


def test_retry_after_is_at_least_one_second():
    rl, _, _ = make()
    rl.observe({"Retry-After": "0"}, 429)
    assert rl.pause_s(False) == pytest.approx(1.0)


def test_a_later_longer_backoff_extends_but_a_shorter_one_does_not_shorten():
    rl, _, _ = make()
    rl.observe({"Retry-After": "30"}, 429)
    rl.observe({"Retry-After": "5"}, 429)
    assert rl.pause_s(False) == pytest.approx(30.0)
    rl.observe({"Retry-After": "120"}, 418)
    assert rl.pause_s(False) == pytest.approx(120.0)


def test_ordinary_responses_do_not_cancel_a_backoff():
    rl, _, _ = make()
    rl.observe({"Retry-After": "30"}, 429)
    rl.observe({WEIGHT_HEADER: "5"}, 200)  # 保护类请求照常通过，权重读数很低
    assert rl.pause_s(False) == pytest.approx(30.0)


def test_backoff_and_soft_limit_combine_to_the_longer_wait():
    rl, _, _ = make(start=M0 + 50_000)  # 距下一分钟 10 秒
    rl.observe({WEIGHT_HEADER: "1800"}, 200)
    rl.observe({"Retry-After": "25"}, 429)
    assert rl.pause_s(False) == pytest.approx(25.0)


def test_error_statuses_other_than_rate_limits_do_not_back_off():
    rl, _, _ = make()
    for status in (400, 401, 500, 503):
        rl.observe({"Retry-After": "30"}, status)
    assert rl.pause_s(False) == 0
