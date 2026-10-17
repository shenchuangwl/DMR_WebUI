"""信号判定 dmr_kernel.algo.signal 的单元测试：过零、长周期趋势、双周期协调、开仓判定（F1 第 2 步）、
出场判定（F11 第 2 步）。与原实现的逐例对拍在 tests/parity/test_parity_signal.py。"""
from __future__ import annotations

import math

import numpy as np
import pytest

from dmr_kernel.algo import signal as sg
from dmr_kernel.core.types import PositionSide

LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
NAN, INF = math.nan, math.inf


# ---------------------------------------------------------------- 过零


@pytest.mark.parametrize(("prev", "curr", "expected"), [
    (-0.001, 0.001, LONG), (0.0, 1e-9, LONG), (np.float64(-0.5), np.float64(0.25), LONG),
    (0.001, -0.001, SHORT), (0.0, -1e-9, SHORT),
])
def test_crossing_detects_zero_crossings(prev, curr, expected):
    got = sg.crossing(prev, curr)
    assert got is expected
    assert got == expected.value  # 与原实现的字符串相等


@pytest.mark.parametrize(("prev", "curr"), [
    (-0.001, 0.0), (0.001, 0.0), (0.0, 0.0), (0.001, 0.002), (-0.001, -0.002),
    (None, 0.001), (0.001, None), (None, None), (NAN, 0.001), (-0.001, NAN), (-INF, 0.001), (0.001, -INF),
])
def test_crossing_returns_none_without_a_valid_crossing(prev, curr):
    """当前值为 None 是原实现遇不到的输入（它总从 DataFrame 取当前值）；无效值（None、NaN、无穷）一律不算过零。"""
    assert sg.crossing(prev, curr) is None


# ---------------------------------------------------------------- 长周期趋势


@pytest.mark.parametrize(("dmr6", "expected"), [
    (0.002, sg.Trend.BULLISH), (np.float64(1e-12), sg.Trend.BULLISH), (-0.002, sg.Trend.BEARISH),
    (0.0, sg.Trend.NEUTRAL), (-0.0, sg.Trend.NEUTRAL), (None, None), (NAN, None), (INF, None),
])
def test_long_trend(dmr6, expected):
    assert sg.long_trend(dmr6) is expected


# ---------------------------------------------------------------- 双周期协调


def test_coordinate_returns_enum_members_equal_to_original_strings():
    out = sg.coordinate(LONG, sg.Trend.BULLISH, None)
    assert out is sg.Coordinated.LONG and out == "LONG"
    assert sg.coordinate("SHORT", "BULLISH", "LONG") is sg.Coordinated.CLOSE_LONG
    assert sg.coordinate(SHORT, sg.Trend.BEARISH, None) is sg.Coordinated.SHORT
    assert sg.coordinate(LONG, sg.Trend.BEARISH, SHORT) is sg.Coordinated.CLOSE_SHORT


@pytest.mark.parametrize("trend", ["BULLISH", "BEARISH", "NEUTRAL", None])
@pytest.mark.parametrize("side", [LONG, SHORT, None])
def test_coordinate_without_short_signal_is_none(trend, side):
    assert sg.coordinate(None, trend, side) is None


@pytest.mark.parametrize("side", [LONG, SHORT, None])
def test_coordinate_neutral_or_missing_trend_gives_nothing(side):
    """长周期中性：暂停交易（与原实现相同）；长周期取不到：不出信号（5.4 第 3 项，原实现直接执行短信号）。"""
    for short_signal in (LONG, SHORT):
        assert sg.coordinate(short_signal, sg.Trend.NEUTRAL, side) is None
        assert sg.coordinate(short_signal, None, side) is None


def test_coordinate_counter_trend_signal_only_closes_an_existing_position():
    assert sg.coordinate(SHORT, sg.Trend.BULLISH, LONG) is sg.Coordinated.CLOSE_LONG
    assert sg.coordinate(SHORT, sg.Trend.BULLISH, SHORT) is None
    assert sg.coordinate(SHORT, sg.Trend.BULLISH, None) is None
    assert sg.coordinate(LONG, sg.Trend.BEARISH, SHORT) is sg.Coordinated.CLOSE_SHORT
    assert sg.coordinate(LONG, sg.Trend.BEARISH, LONG) is None


@pytest.mark.parametrize(("short_signal", "trend", "side"), [
    ("BUY", "BULLISH", None), ("LONG", "UP", None), ("LONG", "BULLISH", "long"), ("LONG", "BULLISH", "BOTH"),
])
def test_coordinate_rejects_unknown_values(short_signal, trend, side):
    with pytest.raises(ValueError):
        sg.coordinate(short_signal, trend, side)


# ---------------------------------------------------------------- 开仓判定（F1 第 2 步）


@pytest.mark.parametrize(("prev12", "curr12", "dmr6", "expected"), [
    (-0.001, 0.002, 0.003, LONG),  # 上穿配 DMR6 > 0：开多
    (0.001, -0.002, -0.003, SHORT),  # 下穿配 DMR6 < 0：开空
    (-0.001, 0.002, -0.003, None),  # 上穿但长周期空头
    (0.001, -0.002, 0.003, None),  # 下穿但长周期多头
    (-0.001, 0.002, 0.0, None),  # DMR6 等于 0
    (-0.001, 0.002, None, None),  # 长周期取不到
    (-0.001, 0.002, NAN, None),
    (0.001, 0.002, 0.003, None),  # 没有过零
    (NAN, 0.002, 0.003, None),  # 前值无效（序列开头）
])
def test_open_signal(prev12, curr12, dmr6, expected):
    assert sg.open_signal(prev12, curr12, dmr6) is expected


# ---------------------------------------------------------------- 出场判定（F11 第 2 步）


@pytest.mark.parametrize(("side", "prev12", "curr12", "dmr6", "expected"), [
    # 多仓看下穿
    (LONG, 0.001, -0.002, 0.003, sg.ExitAction.CLOSE),  # DMR6 与持仓同向：只平
    (LONG, 0.001, -0.002, -0.003, sg.ExitAction.REVERSE),  # 反向：反手
    (LONG, 0.001, -0.002, 0.0, None),  # 等于 0：不动作，由止损保护
    (LONG, 0.001, -0.002, None, sg.ExitAction.PENDING),  # 取不到：待补判
    (LONG, 0.001, -0.002, NAN, sg.ExitAction.PENDING),
    (LONG, -0.001, 0.002, -0.003, None),  # 同向过零不是出场信号
    (LONG, -0.001, -0.002, -0.003, None),  # 没有过零
    # 空仓看上穿
    (SHORT, -0.001, 0.002, -0.003, sg.ExitAction.CLOSE),
    (SHORT, -0.001, 0.002, 0.003, sg.ExitAction.REVERSE),
    (SHORT, -0.001, 0.002, 0.0, None),
    (SHORT, -0.001, 0.002, None, sg.ExitAction.PENDING),
    (SHORT, 0.001, -0.002, 0.003, None),
    (SHORT, 0.001, 0.002, 0.003, None),
])
def test_exit_signal(side, prev12, curr12, dmr6, expected):
    assert sg.exit_signal(prev12, curr12, dmr6, side) is expected


def test_exit_signal_requires_a_position_side():
    with pytest.raises(ValueError):
        sg.exit_signal(0.001, -0.002, 0.003, None)
    with pytest.raises(ValueError):
        sg.exit_signal(0.001, -0.002, 0.003, "long")


def test_exit_signal_accepts_side_as_string():
    assert sg.exit_signal(0.001, -0.002, 0.003, "LONG") is sg.ExitAction.CLOSE
