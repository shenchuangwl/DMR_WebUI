"""模型 1 的纯计算部分：DMR 取值表、长周期对齐、开仓信号、出场逐根补判（F1 第 2 步、F11 第 2 步）。"""
from __future__ import annotations

import math

import pytest

from dmr_kernel.algo.signal import ExitAction
from dmr_kernel.core.types import Candle, PositionSide
from dmr_kernel.models import m1_strategy as m1

SHORT, LONG = 900_000, 3_600_000
T0 = 1_791_943_200_000  # 整点
L, S = PositionSide.LONG, PositionSide.SHORT


def bars(mids, start=T0, period=SHORT):
    out = []
    for i, mid in enumerate(mids):
        out.append(Candle(open_time_ms=start + i * period, open=mid, high=mid, low=mid, close=mid, volume=1.0,
                          close_time_ms=start + (i + 1) * period - 1))
    return out


def test_dmr_values_map_open_time_to_value_with_nan_as_none():
    table = m1.dmr_values(bars([100.0] * 12 + [101.0]), 12)
    assert table[T0] is None and table[T0 + 10 * SHORT] is None  # 前 11 个无效
    assert table[T0 + 11 * SHORT] == pytest.approx(0.0) and table[T0 + 12 * SHORT] > 0


def test_dmr_values_of_nothing_is_empty():
    assert m1.dmr_values([], 12) == {}


@pytest.mark.parametrize("short_open,expected", [
    (T0, T0 - LONG), (T0 + SHORT, T0 - LONG), (T0 + 3 * SHORT, T0), (T0 + 4 * SHORT, T0)])
def test_the_long_bar_for_a_short_bar_is_the_last_one_closed_when_it_closes(short_open, expected):
    assert m1.long_open_for(short_open, SHORT, LONG) == expected


def table(mapping):
    return dict(mapping)


def test_open_side_needs_the_cross_and_a_same_direction_long_trend():
    latest = T0 + 4 * SHORT
    s12 = {latest - SHORT: 0.0, latest: 0.1}
    assert m1.open_side(latest_ms=latest, short_ms=SHORT, long_ms=LONG, s12=s12, l6={T0: 0.2}) is L
    assert m1.open_side(latest_ms=latest, short_ms=SHORT, long_ms=LONG, s12=s12, l6={T0: -0.2}) is None
    assert m1.open_side(latest_ms=latest, short_ms=SHORT, long_ms=LONG, s12=s12, l6={}) is None  # 长周期缺失不出信号
    assert m1.open_side(latest_ms=latest, short_ms=SHORT, long_ms=LONG, s12={latest: 0.1}, l6={T0: 0.2}) is None


def scan(side=L, judged=T0, latest=T0 + 3 * SHORT, s12=None, l6=None, pending=()):
    return m1.scan_exit(side=side, judged_ms=judged, latest_ms=latest, short_ms=SHORT, long_ms=LONG, s12=s12 or {},
                        l6=l6 or {}, pending=pending)


def test_no_cross_means_no_hit_and_judged_moves_to_the_latest_bar():
    s12 = {T0 + i * SHORT: 0.1 for i in range(4)}
    result = scan(s12=s12, l6={T0 - LONG: 0.1})
    assert result.hit is None and result.judged_ms == T0 + 3 * SHORT and result.pending == ()


def test_same_direction_long_trend_closes_only_and_opposite_trend_reverses():
    latest = T0 + 2 * SHORT
    s12 = {latest - SHORT: 0.1, latest: -0.1}  # 对多头是反向过零
    closed = scan(judged=latest - SHORT, latest=latest, s12=s12, l6={T0 - LONG: 0.3})
    assert closed.hit.action is ExitAction.CLOSE and closed.hit.bar_open_ms == latest and not closed.hit.late
    reverse = scan(judged=latest - SHORT, latest=latest, s12=s12, l6={T0 - LONG: -0.3})
    assert reverse.hit.action is ExitAction.REVERSE and reverse.hit.bar_open_ms == latest


def test_a_cross_found_on_an_older_bar_is_closed_only_never_reversed():
    s12 = {T0 + SHORT: 0.1, T0 + 2 * SHORT: -0.1, T0 + 3 * SHORT: -0.2}
    result = scan(judged=T0 + SHORT, latest=T0 + 3 * SHORT, s12=s12, l6={T0 - LONG: -0.3})
    assert result.hit.bar_open_ms == T0 + 2 * SHORT and result.hit.action is ExitAction.CLOSE and result.hit.late
    assert result.judged_ms == T0 + 2 * SHORT


def test_a_missing_short_bar_that_arrives_later_is_judged_bar_by_bar():
    # 上次判到 T0；T0+1、T0+2 两根现在才一起补到：在 T0+1 上有反向过零
    s12 = {T0: 0.2, T0 + SHORT: -0.1, T0 + 2 * SHORT: -0.3}
    result = scan(judged=T0, latest=T0 + 2 * SHORT, s12=s12, l6={T0 - LONG: 0.3})
    assert result.hit.bar_open_ms == T0 + SHORT and result.hit.action is ExitAction.CLOSE


def test_missing_long_trend_records_a_pending_bar_and_does_not_act():
    latest = T0 + 2 * SHORT
    s12 = {latest - SHORT: 0.1, latest: -0.1}
    result = scan(judged=latest - SHORT, latest=latest, s12=s12, l6={})
    assert result.hit is None and result.pending == (latest,) and result.judged_ms == latest


def test_a_pending_bar_is_made_up_once_the_long_trend_is_back_and_only_closes():
    pend = T0 + 2 * SHORT
    s12 = {pend - SHORT: 0.1, pend: -0.1, pend + SHORT: -0.2}
    # 趋势对多头是空头：正常是反手，补判只平
    result = scan(judged=pend, latest=pend + SHORT, s12=s12, l6={T0: -0.3}, pending=(pend,))
    assert result.hit.bar_open_ms == pend and result.hit.action is ExitAction.CLOSE and result.hit.late
    assert result.pending == ()


def test_a_pending_bar_stays_while_the_long_trend_is_still_missing():
    pend = T0 + 2 * SHORT
    s12 = {pend - SHORT: 0.1, pend: -0.1, pend + SHORT: -0.2}
    result = scan(judged=pend, latest=pend + SHORT, s12=s12, l6={}, pending=(pend,))
    assert result.hit is None and result.pending == (pend,)


def test_a_pending_bar_that_turns_out_neutral_is_dropped():
    pend = T0 + 2 * SHORT
    s12 = {pend - SHORT: 0.1, pend: -0.1, pend + SHORT: -0.2}
    result = scan(judged=pend, latest=pend + SHORT, s12=s12, l6={T0: 0.0}, pending=(pend,))
    assert result.hit is None and result.pending == ()


def test_nan_values_are_not_a_signal():
    s12 = {T0 + SHORT: math.nan, T0 + 2 * SHORT: -0.1}
    assert scan(judged=T0, latest=T0 + 2 * SHORT, s12=s12, l6={T0 - LONG: 0.3}).hit is None
