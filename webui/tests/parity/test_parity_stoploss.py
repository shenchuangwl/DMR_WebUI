"""ATR、6 根 K 线窗口、止损价、只朝有利方向的对拍。

原实现：common/trailing_stop_manager.py 的方法（按名字取出源码，挂在桩 self 上；不构造 TrailingStopManager，
它的构造函数会建目录、读写持仓文件），连同 common/timeutil.py 的 to_naive_utc / index_to_naive_utc（同样按名字取出）。
起算时间传给原实现时一律带 UTC 时区；进程时区由 tests/parity/conftest.py 固定为 UTC。
"""
from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

import dmr_originals as orig
from dmr_kernel.algo import stoploss as sl
from dmr_kernel.core.types import PositionSide

GOLD = orig.gold_files()
GOLD_IDS = [rel.rsplit("/", 1)[-1] for rel, _, _ in GOLD]
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
OLD_SIDE = {LONG: "long", SHORT: "short"}
TOL = 1e-12


def _anchors(bars: pd.DataFrame) -> list[int]:
    """起算时间：最早一根之前、正好等于某根的开盘时间（边界：该根不算）、两根之间、接近末尾（窗口不足）。"""
    t = bars["open_time_ms"]
    step = int(t.iloc[1] - t.iloc[0])
    return [int(t.iloc[0]) - 1, int(t.iloc[7]), int(t.iloc[100]), int(t.iloc[100]) + 1,
            int(t.iloc[len(t) // 2]) + step // 2, int(t.iloc[-20])]


def test_process_timezone_is_fixed_to_utc():
    assert time.timezone == 0
    assert datetime.now().astimezone().utcoffset().total_seconds() == 0
    # 原实现把不带时区的时间按本机时区换算；时区固定为 UTC 后，换算结果与 UTC 一致
    assert orig.to_naive_utc()("2026-09-17 12:00") == pd.Timestamp("2026-09-17 12:00")
    assert orig.to_naive_utc()("2026-09-17T20:00:00+08:00") == pd.Timestamp("2026-09-17 12:00")


# ---------------------------------------------------------------- ATR


@pytest.mark.parametrize(("rel", "n", "col"), GOLD, ids=GOLD_IDS)
@pytest.mark.parametrize("period", [14, 7])
def test_atr_matches_original_on_gold_bars(rel, n, col, period):
    bars = orig.gold_closed(rel)
    old = orig.trailing_stop_manager().calculate_atr(orig.original_frame(bars), period=period)
    new = sl.atr(bars["high"], bars["low"], bars["close"], period)
    np.testing.assert_allclose(new.to_numpy(), old.to_numpy(), rtol=0, atol=TOL)  # NaN 位置也一致
    assert new.iloc[: period - 1].isna().all() and not new.iloc[period - 1:].isna().any()


def test_atr_returns_none_when_fewer_bars_than_period_like_original():
    bars = orig.gold_closed(GOLD[0][0]).iloc[:13]
    assert orig.trailing_stop_manager().calculate_atr(orig.original_frame(bars), period=14) is None
    assert sl.atr(bars["high"], bars["low"], bars["close"], 14) is None


# ---------------------------------------------------------------- 6 根 K 线窗口


@pytest.mark.parametrize(("rel", "n", "col"), GOLD, ids=GOLD_IDS)
def test_window6_matches_original_on_gold_bars(rel, n, col):
    bars = orig.gold_closed(rel)
    frame = orig.original_frame(bars)
    tsm = orig.trailing_stop_manager()
    compared = missing = 0
    for anchor in _anchors(bars):
        for cycle in range(5):
            old = tsm.get_6kline_range(frame, orig.utc(anchor), cycle)
            new = sl.window6(bars, anchor, cycle)
            if old is None:
                assert new is None
                missing += 1
                continue
            assert list(new["open_time_ms"]) == orig.index_ms(old)
            assert all(t > anchor for t in new["open_time_ms"])
            for c in ("open", "high", "low", "close"):
                assert list(new[c]) == list(old[c])
            compared += 1
    assert compared >= 20 and missing >= 1


def test_window6_anchor_equal_to_an_open_time_excludes_that_bar():
    bars = orig.gold_closed(GOLD[0][0])
    anchor = int(bars["open_time_ms"].iloc[50])
    old = orig.trailing_stop_manager().get_6kline_range(orig.original_frame(bars), orig.utc(anchor), 0)
    new = sl.window6(bars, anchor, 0)
    assert orig.index_ms(old) == list(new["open_time_ms"]) == list(bars["open_time_ms"].iloc[51:57])


# ---------------------------------------------------------------- 止损价


@pytest.mark.parametrize(("rel", "n", "col"), GOLD, ids=GOLD_IDS)
@pytest.mark.parametrize("side", [LONG, SHORT])
def test_stop_price_matches_original_on_gold_bars(rel, n, col, side):
    bars = orig.gold_closed(rel)
    frame = orig.original_frame(bars)
    tsm = orig.trailing_stop_manager()
    compared = 0
    for anchor in _anchors(bars):
        for cycle in range(4):
            old = tsm.calculate_stop_loss_price(frame, orig.utc(anchor), OLD_SIDE[side], cycle, 14)
            new = sl.stop_price(bars, anchor, side, cycle, 14)
            if old is None or np.isnan(old["atr_value"]):  # 后者：ATR 预热期，见下一条测试
                assert new is None
                continue
            column = "low" if side is LONG else "high"
            label = old["kline_data"][column].idxmin() if side is LONG else old["kline_data"][column].idxmax()
            assert float(new.stop) == old["stop_loss"]  # stop 是 Decimal(str(float))：换回 float 逐位一致
            assert abs(float(new.stop) - old["stop_loss"]) <= TOL
            assert new.extreme == old["extreme_price"]
            assert abs(new.atr - old["atr_value"]) <= TOL
            assert new.bar_open_ms == orig.index_ms(frame.loc[[label]])[0]
            compared += 1
    assert compared >= 10


@pytest.mark.parametrize("side", [LONG, SHORT])
def test_stop_price_is_none_while_atr_is_still_warming_up(side):
    """有意差异：窗口落在 ATR 预热期（前 period−1 根）时，原实现照样返回止损价 NaN（再交给下单与比较）；
    新实现返回 None（算不出），由调用方保留旧止损。"""
    bars = orig.gold_closed(GOLD[0][0])
    anchor = int(bars["open_time_ms"].iloc[0]) - 1
    old = orig.trailing_stop_manager().calculate_stop_loss_price(orig.original_frame(bars), orig.utc(anchor),
                                                                 OLD_SIDE[side], 0, 14)
    assert np.isnan(old["stop_loss"]) and np.isnan(old["atr_value"])
    assert sl.stop_price(bars, anchor, side, 0, 14) is None


# ---------------------------------------------------------------- 5.4 第 8 项


def _bars(rows: list[tuple[float, float, float, float]], start_ms: int = 1_789_000_200_000,
          step_ms: int = 900_000) -> pd.DataFrame:
    return pd.DataFrame({"open_time_ms": [start_ms + i * step_ms for i in range(len(rows))],
                         "open": [r[0] for r in rows], "high": [r[1] for r in rows],
                         "low": [r[2] for r in rows], "close": [r[3] for r in rows], "volume": 1.0})


def test_six_bar_count_includes_only_closed_bars():
    """5.4 第 8 项（有意差异）：原实现拿到的 K 线含未收盘那根（行情接口返回的最后一根）。get_6kline_range 把它
    算作窗口的第 6 根，update_trailing_stop 数完整周期（第 366–367 行 len(after_entry) // 6）也把它算进去，
    第 6 根还没收盘就按它的半根数据算出新止损。新实现只接受已收盘 K 线：此刻只有 5 根已收盘，窗口不足，返回 None；
    第 6 根收盘后两边取到同样的 6 根。"""
    rng = np.random.default_rng(8)
    rows = [(1.0 + 0.01 * i, 1.02 + 0.01 * i + rng.uniform(0, 0.01), 0.98 + 0.01 * i - rng.uniform(0, 0.01),
             1.0 + 0.01 * i) for i in range(26)]
    full = _bars(rows)  # 第 26 根（最后一根）此刻还没收盘
    anchor = int(full["open_time_ms"].iloc[19])  # 起算之后：第 21–25 根已收盘，第 26 根未收盘
    tsm = orig.trailing_stop_manager()
    frame = orig.original_frame(full)
    old = tsm.get_6kline_range(frame, orig.utc(anchor), 0)
    assert orig.index_ms(old)[-1] == int(full["open_time_ms"].iloc[-1])  # 原实现：未收盘那根也算
    after_entry = frame[frame.index > orig.utc(anchor).tz_localize(None)]
    assert orig.cycle_count()(after_entry) == 1  # 原实现：已经数满一个周期
    closed = full.iloc[:-1]
    assert sl.window6(closed, anchor, 0) is None
    assert sl.stop_price(closed, anchor, LONG, 0) is None
    assert list(sl.window6(full, anchor, 0)["open_time_ms"]) == orig.index_ms(old)  # 第 26 根收盘之后


# ---------------------------------------------------------------- 只朝有利方向


IS_BETTER_CASES = [
    (LONG, None, 100.0, True),  # 还没有止损：采纳
    (LONG, 99.0, 100.0, True),  # 多仓上移
    (LONG, 100.0, 100.0, False),
    (LONG, 101.0, 100.0, False),
    (SHORT, None, 100.0, True),
    (SHORT, 101.0, 100.0, True),  # 空仓下移
    (SHORT, 100.0, 100.0, False),
    (SHORT, 99.0, 100.0, False),
]


@pytest.mark.parametrize(("side", "current", "new", "expected"), IS_BETTER_CASES)
def test_is_better_matches_original_segment(side, current, new, expected):
    assert orig.is_better()(OLD_SIDE[side], current, new) is expected
    assert sl.is_better(side, current, new) is expected
    as_dec = None if current is None else Decimal(str(current))
    assert sl.is_better(side, as_dec, Decimal(str(new))) is expected
