"""界面格式化：金额/数量/时间/相对时间。内核的数值一律是字符串，格式化不得经过 float，也不得抛异常。"""
from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from dmr_console.fmt import (DASH, fmt_age, fmt_clock, fmt_money, fmt_number, fmt_pct, fmt_remaining, fmt_signed_money,
                             fmt_span, fmt_time, sign_of)

UTC7 = timezone(timedelta(hours=7))


@pytest.mark.parametrize("value,expected", [
    ("1000", "1,000.00"), ("900.5", "900.50"), ("0", "0.00"), ("-12.345", "-12.35"), ("0.005", "0.01"),
    ("1234567.891", "1,234,567.89"), (5, "5.00"),
])
def test_money_two_places_half_up_with_thousands_separator(value, expected):
    assert fmt_money(value) == expected


@pytest.mark.parametrize("value", [None, ""])
def test_missing_values_show_a_dash(value):
    assert fmt_money(value) == DASH and fmt_number(value) == DASH and fmt_pct(value) == DASH
    assert fmt_signed_money(value) == DASH


@pytest.mark.parametrize("value", ["abc", "NaN", "Infinity", "1,5", "--1"])
def test_unparsable_numbers_pass_through_instead_of_crashing(value):
    assert fmt_money(value) == value
    assert fmt_number(value) == value


def test_number_keeps_the_kernel_digits_and_never_uses_exponent_form():
    assert fmt_number("0.010") == "0.010"
    assert fmt_number("60000") == "60000"
    assert fmt_number("1E+3") == "1000"
    assert fmt_number("0.00000123") == "0.00000123"
    assert fmt_number("1.23E-7") == "0.000000123"
    assert fmt_number(0.5) == "0.5"


def test_big_decimal_precision_is_not_lost_through_float():
    assert fmt_number("12345678901234567890.123456789") == "12345678901234567890.123456789"
    assert fmt_money("12345678901234567890.125") == "12,345,678,901,234,567,890.13"


def test_signed_money_shows_plus_for_gains():
    assert fmt_signed_money("1") == "+1.00"
    assert fmt_signed_money("-0.5") == "-0.50"
    assert fmt_signed_money("0") == "0.00"


def test_sign_of_drives_red_green_colouring():
    assert (sign_of("1.5"), sign_of("-0.01"), sign_of("0"), sign_of(None), sign_of("x")) == (1, -1, 0, 0, 0)


def test_percent():
    assert fmt_pct("10") == "10.00%" and fmt_pct("0.333") == "0.33%"


def test_time_is_rendered_in_the_given_timezone():
    ms = 1_760_000_000_000  # 2025-10-09T08:53:20Z
    assert fmt_time(ms, UTC7) == "2025-10-09 15:53:20"
    assert fmt_time(ms, timezone.utc) == "2025-10-09 08:53:20"
    assert fmt_clock(ms, UTC7) == "15:53:20"
    assert fmt_time(None, UTC7) == DASH and fmt_clock(None, UTC7) == DASH


def test_time_defaults_to_the_local_zone_and_bad_values_do_not_crash():
    assert fmt_time(1_760_000_000_000)  # 不指定时区：用本机时区
    assert fmt_time("x", UTC7) == DASH
    assert fmt_time(10 ** 20, UTC7) == DASH
    assert fmt_time(True, UTC7) == DASH


@pytest.mark.parametrize("delta,expected", [
    (-5_000, "刚刚"), (0, "刚刚"), (999, "刚刚"), (1_000, "1 秒前"), (59_000, "59 秒前"), (60_000, "1 分钟前"),
    (3_599_000, "59 分钟前"), (3_600_000, "1 小时前"), (86_399_000, "23 小时前"), (86_400_000, "1 天前"),
])
def test_age(delta, expected):
    assert fmt_age(delta) == expected


def test_age_of_none():
    assert fmt_age(None) == DASH


@pytest.mark.parametrize("delta,expected", [
    (None, DASH), (-1, "已到期"), (0, "已到期"), (1_000, "1 秒"), (59_000, "59 秒"), (60_000, "1 分钟"),
    (125_000, "2 分 5 秒"), (3_600_000, "1 小时"), (3_725_000, "1 小时 2 分"),
])
def test_remaining(delta, expected):
    assert fmt_remaining(delta) == expected


@pytest.mark.parametrize("delta,expected", [
    (None, DASH), (-5, "0 秒"), (0, "0 秒"), (20_000, "20 秒"), (60_000, "1 分钟"), (125_000, "2 分 5 秒"), (3_725_000, "1 小时 2 分"),
])
def test_span_is_a_duration_never_says_expired(delta, expected):
    assert fmt_span(delta) == expected
