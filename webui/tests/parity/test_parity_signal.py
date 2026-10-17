"""过零、长周期趋势、双周期协调、开仓与出场判定的对拍。

原实现：bit/strategy/bit_short_term/strategy_engine.py。过零与长周期趋势是函数里的语句段，按行段取出包成函数；
双周期协调与纠错信号是方法，按名字取出；都在隔离命名空间里执行，桩 self 提供 logger 与 get_strategy_position。
原实现的持仓方向是小写 'long'/'short'，新实现用 PositionSide。
"""
from __future__ import annotations

import itertools

import pandas as pd
import pytest

import dmr_originals as orig
from dmr_kernel.algo import signal as sg
from dmr_kernel.core.types import PositionSide

NAN = float("nan")
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
OLD_SIDE = {LONG: "long", SHORT: "short", None: None}


# ---------------------------------------------------------------- 过零


CROSSING_CASES = [
    (-0.002, 0.003, "LONG"),  # 由负转正
    (0.0, 0.001, "LONG"),  # 0 边界：prev ≤ 0 且 curr > 0 才是上穿
    (0.002, -0.003, "SHORT"),  # 由正转负
    (0.0, -0.001, "SHORT"),  # 0 边界：prev ≥ 0 且 curr < 0 才是下穿
    (-0.001, 0.0, None),  # 到 0 不算上穿
    (0.001, 0.0, None),  # 到 0 不算下穿
    (0.0, 0.0, None),
    (0.001, 0.002, None),  # 同号不穿
    (-0.001, -0.002, None),
    (None, 0.001, None),  # 没有前值
    (NAN, 0.001, None),  # 无效值
    (-0.001, NAN, None),
]


@pytest.mark.parametrize(("prev", "curr", "expected"), CROSSING_CASES)
def test_crossing_matches_original_segment(prev, curr, expected):
    assert orig.crossing()(prev, curr) == expected
    assert sg.crossing(prev, curr) == expected


# ---------------------------------------------------------------- 长周期趋势


@pytest.mark.parametrize(("dmr6", "expected"), [(0.003, "BULLISH"), (-0.002, "BEARISH"), (0.0, "NEUTRAL"),
                                                (None, None)])
def test_long_trend_matches_original_segment(dmr6, expected):
    frame = None if dmr6 is None else pd.DataFrame({"dmr_6": [dmr6]})
    assert orig.long_trend()(frame) == expected
    assert sg.long_trend(dmr6) == expected


def test_long_trend_of_an_invalid_value_is_unavailable_not_neutral():
    """与 5.4 第 12 项配套的有意差异：新实现里 DMR 序列开头的无效值是 NaN（原实现补 0）。NaN 在原行段里既不大于 0
    也不小于 0，会落进「中性」分支；新实现把它当作「取不到」（None），由出场判定记为待补判，而不是当成中性不动作。"""
    assert orig.long_trend()(pd.DataFrame({"dmr_6": [NAN]})) == "NEUTRAL"
    assert orig.long_trend()(pd.DataFrame({"dmr_6": []})) is None  # 原实现：取不到数据
    assert sg.long_trend(NAN) is None


# ---------------------------------------------------------------- 双周期协调（3×4×3 = 36 种组合）


COMBOS = list(itertools.product(["LONG", "SHORT", None], ["BULLISH", "BEARISH", "NEUTRAL", None], [LONG, SHORT, None]))


@pytest.mark.parametrize(("short_signal", "trend", "side"), COMBOS)
def test_coordinate_matches_original_except_when_long_trend_is_missing(short_signal, trend, side):
    """长周期取不到（None）时，原实现跳过过滤、直接返回短周期信号（第 337–338 行）；新实现不出信号
    （5.4 第 3 项，有意差异）。其余 27 种组合与原实现逐一相同。"""
    old = orig.coordinate()(short_signal, trend, OLD_SIDE[side])
    new = sg.coordinate(short_signal, trend, side)
    if trend is None:
        assert old == short_signal  # 原实现：过滤被跳过
        assert new is None  # 新实现：不出信号
    else:
        assert new == old


def test_coordinate_examples():
    assert sg.coordinate("LONG", "BULLISH", None) == "LONG"
    assert sg.coordinate("SHORT", "BULLISH", LONG) == "CLOSE_LONG"
    assert sg.coordinate("SHORT", "BULLISH", SHORT) is None
    assert sg.coordinate("LONG", "BEARISH", SHORT) == "CLOSE_SHORT"
    assert sg.coordinate("SHORT", "BEARISH", LONG) == "SHORT"
    assert sg.coordinate("LONG", "NEUTRAL", SHORT) is None


# ---------------------------------------------------------------- 开仓判定（F1 第 2 步）与出场判定（F11 第 2 步）


UP, DOWN, FLAT = (-0.001, 0.002), (0.001, -0.002), (0.001, 0.002)
DMR6_VALUES = [0.003, -0.003, 0.0, None]


def _old_trend(dmr6):
    return orig.long_trend()(None if dmr6 is None else pd.DataFrame({"dmr_6": [dmr6]}))


@pytest.mark.parametrize(("move", "dmr6"), list(itertools.product([UP, DOWN, FLAT], DMR6_VALUES)))
def test_open_signal_matches_original_pipeline_without_position(move, dmr6):
    """原实现的流水线：过零语句段 → 长周期语句段 → 双周期协调（无持仓）。长周期取不到时原实现照样开仓，
    新实现不出信号（5.4 第 3 项，有意差异）。"""
    prev, curr = move
    old = orig.coordinate()(orig.crossing()(prev, curr), _old_trend(dmr6), None)
    new = sg.open_signal(prev, curr, dmr6)
    if dmr6 is None:
        assert old == orig.crossing()(prev, curr)
        assert new is None
    else:
        assert new == old


def _old_action(signal, side):
    """原实现 execute_signal（第 430–532 行）对协调结果的处置：CLOSE_* 只平；与持仓相反的开仓信号先平后开（反手）；
    与持仓同向的开仓信号「已有同向仓，无需操作」。"""
    if signal in ("CLOSE_LONG", "CLOSE_SHORT"):
        return "CLOSE"
    if signal == ("SHORT" if side is LONG else "LONG"):
        return "REVERSE"
    return None


@pytest.mark.parametrize(("move", "dmr6", "side"),
                         list(itertools.product([UP, DOWN, FLAT], DMR6_VALUES, [LONG, SHORT])))
def test_exit_signal_matches_original_handling_of_coordinated_signal(move, dmr6, side):
    """反向过零时：DMR6 与持仓同向只平、反向反手、等于 0 不动作，与原实现一致。长周期取不到时原实现直接反手；
    新实现记为待补判（PENDING，补判时只平不反），这是 5.4 第 3 项的有意差异。"""
    prev, curr = move
    cross = orig.crossing()(prev, curr)
    old = _old_action(orig.coordinate()(cross, _old_trend(dmr6), OLD_SIDE[side]), side)
    new = sg.exit_signal(prev, curr, dmr6, side)
    against = "SHORT" if side is LONG else "LONG"
    if dmr6 is None and cross == against:
        assert old == "REVERSE"
        assert new == "PENDING"
    else:
        assert new == old


# ---------------------------------------------------------------- 5.4 第 2、4 项


def test_crossing_compares_adjacent_closed_bars_not_the_persisted_value():
    """5.4 第 2 项（有意差异）：原实现拿「当前值」与「上次运行落盘的值」比较（第 266–269 行：
    previous_dmr = self.last_dmr_value）。中间漏跑一根（或重启）时，比较的是相隔两根的值：上穿实际发生在第 k−1 根，
    原实现到第 k 根才报。新实现只比较相邻两根已收盘 K 线：上穿在第 k−1 根报一次，第 k 根不再报
    （按开盘时间去重、缺根补判由调用方负责，F11 第 2 步）。"""
    closed = [-0.0010, 0.0005, 0.0012]  # 第 k−2、k−1、k 根已收盘 K 线的 DMR12
    frame = pd.DataFrame({"dmr_12": closed})
    prev_old, curr_old = orig.current_and_previous()(frame, 12, closed[0])  # 落盘值停在第 k−2 根
    assert (prev_old, curr_old) == (closed[0], closed[2])
    assert orig.crossing()(prev_old, curr_old) == "LONG"  # 迟一根才报
    assert sg.crossing(closed[0], closed[1]) == "LONG"  # 新：第 k−1 根
    assert sg.crossing(closed[1], closed[2]) is None  # 新：第 k 根不再报


def test_no_correction_signal_in_the_first_phase():
    """5.4 第 4 项（有意差异）：原实现的持仓纠错信号在 DMR 配置里默认开启（bit/config/bit_short_term_config.py
    第 151 行 'position_correction_enabled': True）；没有过零也会出信号，并且优先级排在协调结果之后、不经长周期过滤
    （第 302 行 signal = final_signal or correction_signal），持空仓时给出 LONG 即反手。新实现首期不实现纠错信号
    （配置项 correction_signal 只能为关）：没有过零就没有任何开仓或出场信号。"""
    # 持空仓；DMR12 为正、超过纠错阈值 0.0005，但相邻两根之间没有过零；长周期为空头
    assert orig.correction()(0.0012, "short") == "LONG"
    assert orig.correction()(-0.0012, "long") == "SHORT"
    assert orig.signal_priority()(None, "BEARISH", 0.0012, "short") == "LONG"
    # 关掉纠错后原实现也不出信号：差异只来自纠错信号
    assert orig.signal_priority()(None, "BEARISH", 0.0012, "short", correction_enabled=False) is None
    assert sg.exit_signal(0.0008, 0.0012, -0.003, SHORT) is None
    assert sg.exit_signal(-0.0008, -0.0012, 0.003, LONG) is None
    assert sg.open_signal(0.0008, 0.0012, -0.003) is None
