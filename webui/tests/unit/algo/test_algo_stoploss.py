"""止损算法 dmr_kernel.algo.stoploss 的单元测试：ATR、6 根 K 线窗口、止损价、初始止损（新增）、只朝有利方向。
与原实现的对拍在 tests/parity/test_parity_stoploss.py。"""
from __future__ import annotations

import dataclasses
import math
from decimal import Decimal

import pandas as pd
import pytest

from dmr_kernel.algo import stoploss as sl
from dmr_kernel.core.types import PositionSide

LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
D = Decimal
T0 = 1_789_000_200_000  # 一个 15m 边界（UTC 毫秒）
STEP = 900_000


def _bars(rows: list[tuple[float, float, float]], start_ms: int = T0) -> pd.DataFrame:
    """rows: (high, low, close)；open 取 close。"""
    return pd.DataFrame({
        "open_time_ms": [start_ms + i * STEP for i in range(len(rows))],
        "open": [r[2] for r in rows], "high": [r[0] for r in rows], "low": [r[1] for r in rows],
        "close": [r[2] for r in rows], "volume": 1.0,
    })


def _ladder(count: int = 20) -> pd.DataFrame:
    """收盘价每根升 0.125、高低各差 0.5：真实波幅恒为 1.0，ATR 精确等于 1.0（数值都可用二进制精确表示）。"""
    return _bars([(100 + 0.125 * i + 0.5, 100 + 0.125 * i - 0.5, 100 + 0.125 * i) for i in range(count)])


# ---------------------------------------------------------------- ATR


ATR_ROWS = [(10.0, 9.0, 9.5), (10.5, 9.4, 10.2), (10.1, 9.2, 9.4), (11.0, 9.8, 10.9), (10.7, 10.1, 10.3)]


def test_atr_is_the_simple_rolling_mean_of_true_range():
    bars = _bars(ATR_ROWS)
    out = sl.atr(bars["high"], bars["low"], bars["close"], 3)
    # 真实波幅：1.0（首根只有高减低）、1.1、1.0、1.6、0.8
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx((1.0 + 1.1 + 1.0) / 3, abs=1e-12)
    assert out.iloc[3] == pytest.approx((1.1 + 1.0 + 1.6) / 3, abs=1e-12)
    assert out.iloc[4] == pytest.approx((1.0 + 1.6 + 0.8) / 3, abs=1e-12)
    wilder = ((1.0 + 1.1 + 1.0) / 3 * 2 + 1.6) / 3  # Wilder 平滑会得到的值：不是它
    assert out.iloc[3] != pytest.approx(wilder, abs=1e-6)


def test_atr_is_none_with_fewer_bars_than_period():
    bars = _bars(ATR_ROWS)
    assert sl.atr(bars["high"], bars["low"], bars["close"], 6) is None
    assert sl.atr(bars["high"], bars["low"], bars["close"], 5) is not None


@pytest.mark.parametrize("period", [0, -3, 2.5, True])
def test_atr_rejects_invalid_period(period):
    bars = _bars(ATR_ROWS)
    with pytest.raises(ValueError):
        sl.atr(bars["high"], bars["low"], bars["close"], period)


# ---------------------------------------------------------------- 6 根 K 线窗口


def test_window6_takes_bars_strictly_after_the_anchor_in_groups_of_six():
    bars = _ladder(20)
    t = bars["open_time_ms"]
    for anchor in (int(t.iloc[3]), int(t.iloc[3]) + 1, int(t.iloc[4]) - 1):  # 正好等于第 3 根的开盘时间：第 3 根不算
        assert list(sl.window6(bars, anchor, 0)["open_time_ms"]) == list(t.iloc[4:10])
        assert list(sl.window6(bars, anchor, 1)["open_time_ms"]) == list(t.iloc[10:16])
        assert sl.window6(bars, anchor, 2) is None  # 只剩 4 根
    assert list(sl.window6(bars, int(t.iloc[0]) - 1, 0)["open_time_ms"]) == list(t.iloc[0:6])
    assert sl.window6(bars, int(t.iloc[-1]), 0) is None
    assert sl.window6(bars.iloc[:0], 0, 0) is None


def test_window6_keeps_the_rows_of_the_input():
    bars = _ladder(12)
    win = sl.window6(bars, int(bars["open_time_ms"].iloc[1]), 0)
    pd.testing.assert_frame_equal(win, bars.iloc[2:8])


@pytest.mark.parametrize("cycle", [-1, 1.0, True])
def test_window6_rejects_invalid_cycle(cycle):
    with pytest.raises(ValueError):
        sl.window6(_ladder(12), T0, cycle)


def test_window6_rejects_unsorted_duplicate_or_incomplete_bars():
    bars = _ladder(12)
    with pytest.raises(ValueError):
        sl.window6(bars.iloc[::-1], T0, 0)
    with pytest.raises(ValueError):
        sl.window6(pd.concat([bars, bars.iloc[-1:]]).reset_index(drop=True), T0, 0)
    with pytest.raises(ValueError):
        sl.window6(bars.drop(columns=["low"]), T0, 0)


# ---------------------------------------------------------------- 止损价


def test_stop_price_long_uses_the_lowest_low_and_the_atr_of_that_bar():
    bars = _ladder(20)
    bars.loc[16, "low"] = 99.0
    bars.loc[18, "low"] = 99.0  # 并列最低：取第一根
    atr = sl.atr(bars["high"], bars["low"], bars["close"], 14)
    calc = sl.stop_price(bars, int(bars["open_time_ms"].iloc[13]), LONG, 0)
    assert calc.extreme == 99.0
    assert calc.bar_open_ms == int(bars["open_time_ms"].iloc[16])
    assert calc.atr == atr.iloc[16] and calc.atr != atr.iloc[18]
    assert calc.stop == D(str(99.0 - atr.iloc[16]))
    assert isinstance(calc.stop, Decimal) and isinstance(calc.extreme, float) and isinstance(calc.bar_open_ms, int)


def test_stop_price_short_uses_the_highest_high_plus_atr():
    bars = _ladder(20)
    calc = sl.stop_price(bars, int(bars["open_time_ms"].iloc[13]), SHORT, 0)  # 窗口第 14–19 根
    assert calc.extreme == 100 + 0.125 * 19 + 0.5
    assert calc.atr == 1.0
    assert calc.stop == D("103.875")
    assert calc.bar_open_ms == int(bars["open_time_ms"].iloc[19])


def test_stop_price_is_none_when_window_or_atr_is_unavailable():
    bars = _ladder(20)
    assert sl.stop_price(bars, int(bars["open_time_ms"].iloc[15]), LONG, 0) is None  # 窗口不足
    assert sl.stop_price(bars, T0 - 1, LONG, 0) is None  # 窗口在 ATR 预热期（前 13 根 ATR 无效）
    assert sl.stop_price(bars.iloc[:10], T0 - 1, LONG, 0) is None  # K 线少于 ATR 周期


def test_stop_calc_is_frozen():
    calc = sl.stop_price(_ladder(20), int(T0 + 13 * STEP), LONG, 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        calc.stop = D("1")  # type: ignore[misc]


# ---------------------------------------------------------------- 初始止损（报告 F2 第 1 步，新增）


@pytest.mark.parametrize(("side", "base", "expected"), [
    (LONG, D("102"), D("100.25")),  # 未夹：距基准价约 1.72%，在 1%–5% 之间
    (LONG, D("100.5"), D("99.495")),  # 夹到 A：原止损只差约 0.25%，推远到 1%
    (LONG, D("110"), D("104.50")),  # 夹到 B：原止损差约 8.9%，拉近到 5%
    (LONG, D("100"), D("99.00")),  # 原止损在基准价之上（行情已跌过）：按 A 夹到基准价之下
    (SHORT, D("102.5"), D("103.875")),  # 未夹：约 1.34%
    (SHORT, D("103.5"), D("104.535")),  # 夹到 A
    (SHORT, D("95"), D("99.75")),  # 夹到 B
    (SHORT, D("104"), D("105.04")),  # 原止损在基准价之下：按 A 夹到基准价之上
])
def test_initial_stop_clamps_between_a_and_b(side, base, expected):
    calc = sl.initial_stop(_ladder(20), side, base, D("1"), D("5"))
    assert calc.stop == expected
    assert calc.atr == 1.0
    raw_bar = 14 if side is LONG else 19  # 最近 6 根（第 14–19 根）里的最低价、最高价所在的那根
    assert calc.bar_open_ms == T0 + raw_bar * STEP


def test_initial_stop_is_symmetric_for_long_and_short():
    """多空对称：把 K 线关于基准价镜像（价 → 2×基准价 − 价），空仓止损与多仓止损关于基准价对称（未夹、夹到 A、夹到 B 都成立）。"""
    up = _ladder(20)
    for base in (D("100"), D("100.5"), D("102"), D("110")):
        twice = float(2 * base)
        mirror = pd.DataFrame({"open_time_ms": up["open_time_ms"], "open": twice - up["open"],
                               "high": twice - up["low"], "low": twice - up["high"], "close": twice - up["close"],
                               "volume": 1.0})
        long_ = sl.initial_stop(up, LONG, base, D("1"), D("5"))
        short = sl.initial_stop(mirror, SHORT, base, D("1"), D("5"))
        assert long_.stop + short.stop == 2 * base
        assert long_.atr == short.atr == 1.0
        assert long_.bar_open_ms == short.bar_open_ms


def test_initial_stop_is_none_without_enough_bars_or_atr():
    assert sl.initial_stop(_ladder(5), LONG, D("102"), D("1"), D("5")) is None  # 不足 6 根
    assert sl.initial_stop(_ladder(10), LONG, D("102"), D("1"), D("5")) is None  # 少于 ATR 周期
    bars = _ladder(16)
    bars.loc[11, "low"] = 90.0  # 最近 6 根（10–15）的最低价落在 ATR 还无效的第 11 根
    assert sl.initial_stop(bars, LONG, D("102"), D("1"), D("5")) is None
    assert sl.initial_stop(bars, SHORT, D("100"), D("1"), D("5")) is not None  # 最高价在第 15 根，ATR 有效


@pytest.mark.parametrize(("a", "b"), [(D("0"), D("5")), (D("-1"), D("5")), (D("6"), D("5")), (D("1"), D("100"))])
def test_initial_stop_rejects_invalid_band(a, b):
    with pytest.raises(ValueError):
        sl.initial_stop(_ladder(20), LONG, D("102"), a, b)


@pytest.mark.parametrize("base", [D("0"), D("-1"), D("NaN")])
def test_initial_stop_rejects_invalid_base_price(base):
    with pytest.raises(ValueError):
        sl.initial_stop(_ladder(20), LONG, base, D("1"), D("5"))


# ---------------------------------------------------------------- 只朝有利方向


@pytest.mark.parametrize(("side", "current", "new", "expected"), [
    (LONG, None, D("100"), True), (LONG, D("99.95"), D("100"), True), (LONG, D("100"), D("100"), False),
    (LONG, D("100.05"), D("100"), False), (SHORT, None, D("100"), True), (SHORT, D("100.05"), D("100"), True),
    (SHORT, D("100"), D("100"), False), (SHORT, D("99.95"), D("100"), False),
])
def test_is_better_only_accepts_moves_in_the_favourable_direction(side, current, new, expected):
    assert sl.is_better(side, current, new) is expected


@pytest.mark.parametrize("new", [None, math.nan, D("NaN"), math.inf])
def test_is_better_rejects_invalid_new_stop(new):
    with pytest.raises(ValueError):
        sl.is_better(LONG, D("100"), new)


def test_is_better_rejects_unknown_side():
    with pytest.raises(ValueError):
        sl.is_better("long", D("100"), D("101"))
