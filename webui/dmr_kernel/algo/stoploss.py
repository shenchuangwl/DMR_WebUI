"""止损算法：ATR、6 根 K 线窗口、止损价、初始止损（新增，报告 F2 第 1 步）、只朝有利方向。

来源（只读引用 DMR 源码；sha256 是该行段原文 UTF-8 的哈希，与 tests/parity/source_hashes.json 一致）：

- common/trailing_stop_manager.py:101-130  TrailingStopManager.calculate_atr（誊写为 atr）
  sha256 fa03b8e58d6074e25391adab63a6d4c193f7d7574a7ec317bc8e8d7e7e2943e3
- common/trailing_stop_manager.py:132-174  TrailingStopManager.get_6kline_range（誊写为 window6）
  sha256 52a80d76f3300522227601858e9ff0a3ddad304f2d527cf509730bfcb7d7d6ba
- common/trailing_stop_manager.py:497-576  TrailingStopManager._ensure_datetime_column（get_6kline_range 依赖的时间列整理）
  sha256 24e25134ab788332118926e7981d9223dc2d6a5ad25884afa2ddbcbcec92dcca
- common/timeutil.py:33-53  to_naive_utc（get_6kline_range 依赖）
  sha256 cf10fcb8e8446c794ed196e8dd4fb8e2af989e5cde5a9f2e8982eacb6e482e69
- common/timeutil.py:56-64  index_to_naive_utc（_ensure_datetime_column 依赖）
  sha256 284256c44e21c2fd58e32fb3ce5fd0e90517ca193bbab572ad679c9250d92874
- common/trailing_stop_manager.py:176-230  TrailingStopManager.calculate_stop_loss_price（誊写为 stop_price）
  sha256 f711930cada23a13938c7ff89320df552e68594ca2b6b4e799a4e221d2e1c266
- common/trailing_stop_manager.py:383-403  update_trailing_stop 里只朝有利方向采纳的语句段（誊写为 is_better）
  sha256 9c08ea515e2903a7aa63b17a390b5cf42c3c1ce92f2ff1a34086851dd4dc8953
- common/trailing_stop_manager.py:366-367  update_trailing_stop 数完整周期（5.4 第 8 项的对照，不誊写）
  sha256 f9684ecd50c785aee786c826798b4a70af2cf2196738cef37312acb4db08fd7a

K 线 ``bars`` 是 DataFrame，至少有 ``open_time_ms``（UTC 毫秒，严格递增）、``high``、``low``、``close`` 列，**只含已收盘 K 线**。
誊写：ATR 是真实波幅的简单滚动均值（不是 Wilder 平滑；首根真实波幅只有高减低）；窗口是开盘时间**严格晚于**起算时间的
K 线里第 cycle 组的 6 根，凑不满返回 None；止损价：多 = 6 根内最低价 − 该根的 ATR，空 = 最高价 + 该根的 ATR，
并列时取第一根；ATR 在整段 K 线上计算，运算与原实现相同，对拍逐位一致（tests/parity/test_parity_stoploss.py）。
去掉了对象状态、持仓文件读写、打印与时区换算：起算时间直接是 UTC 毫秒整数（原实现经 to_naive_utc 把不带时区的
时间按本机时区换算，本机 +07 时会错位）。

有意差异：

- 报告 5.4 第 8 项：只接受已收盘 K 线。原实现拿到的 K 线含未收盘那根，会把它算作窗口的第 6 根、数进完整周期，
  第 6 根还没收盘就按它的半根数据算出新止损；这里第 cycle 组凑不满 6 根已收盘 K 线就返回 None。
- 窗口落在 ATR 预热期（前 period−1 根）或极值那根缺价时，止损价返回 None（算不出），由调用方保留旧止损；
  原实现照样返回 NaN 并交给下单与比较。
- ``StopCalc.stop`` 是 ``Decimal(str(float))``：换回 float 与原实现逐位相同；还没有按价格步长取整
  （取整用 rounding.round_stop_price，多仓向下、空仓向上）。
- ``initial_stop``（新增）：进入托管前最近 6 根已收盘 K 线的极值 ± 该根 ATR，再夹在距基准价 A%–B% 之间（A、B 是百分数），
  先限下限（至少 A% 远）、再限上限（至多 B% 远）；K 线不足或 ATR 无效返回 None，由调用方按 A% 设置并注明。
- ``is_better`` 拒绝无效的新止损价（None、NaN、无穷报 ValueError），持仓方向只认 PositionSide。

版权与许可：本文件誊写自 DMR（DMR_binance_Version_V16_A1）的源码，保留其版权声明。
    Copyright 2026 shenchuangwl
    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    许可证全文见 DMR_WebUI/LICENSE，声明见 DMR_WebUI/NOTICE。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext

import numpy as np
import pandas as pd

from dmr_kernel.algo.dmr import check_aligned, check_period
from dmr_kernel.algo.rounding import EXACT, finite_decimal, is_int, positive_decimal
from dmr_kernel.core.types import PositionSide

WINDOW = 6  # 每组 K 线根数
BAR_COLUMNS = ("open_time_ms", "high", "low", "close")


@dataclass(frozen=True)
class StopCalc:
    """止损价的计算结果。"""

    stop: Decimal  # 止损价（未按价格步长取整）
    extreme: float  # 窗口内的最低价（多）或最高价（空）
    atr: float  # 极值那根的 ATR
    bar_open_ms: int  # 极值那根的开盘时间（UTC 毫秒）


# ---------------------------------------------------------------- 输入检查


def _check_bars(bars: pd.DataFrame) -> None:
    if not isinstance(bars, pd.DataFrame):
        raise TypeError(f"K 线必须是 DataFrame，得到 {type(bars).__name__}")
    missing = [c for c in BAR_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"K 线缺少列：{missing}")
    t = bars["open_time_ms"]
    if len(t) and not pd.api.types.is_integer_dtype(t):
        raise ValueError("open_time_ms 必须是整数毫秒")
    if len(t) > 1 and not (t.is_monotonic_increasing and t.is_unique):
        raise ValueError("K 线必须按开盘时间严格递增（不重复）")


def _check_anchor(anchor_ms: int) -> int:
    if not is_int(anchor_ms):
        raise TypeError(f"起算时间必须是 UTC 毫秒整数：{anchor_ms!r}")
    return int(anchor_ms)


def _check_cycle(cycle: int) -> int:
    if not is_int(cycle) or cycle < 0:
        raise ValueError(f"周期序号必须是非负整数：{cycle!r}")
    return int(cycle)


def _window_slice(bars: pd.DataFrame, anchor_ms: int, cycle: int) -> slice | None:
    """第 cycle 组 6 根（开盘时间严格晚于起算时间）在 bars 里的位置范围；凑不满返回 None。"""
    t = bars["open_time_ms"].to_numpy()
    first = int(np.searchsorted(t, anchor_ms, side="right"))
    start = first + cycle * WINDOW
    end = start + WINDOW
    return slice(start, end) if end <= len(t) else None


# ---------------------------------------------------------------- ATR 与窗口


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series | None:
    """ATR：真实波幅的 period 期简单滚动均值，前 period−1 个值为 NaN；K 线少于 period 根返回 None。"""
    n = check_period(period, "ATR 周期")
    check_aligned(high, low, close)
    if len(high) < n:
        return None
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    out = tr.rolling(window=n).mean()
    out.name = f"atr_{n}"
    return out


def window6(bars: pd.DataFrame, anchor_ms: int, cycle: int) -> pd.DataFrame | None:
    """开盘时间严格晚于 anchor_ms 的已收盘 K 线里第 cycle 组（从 0 起）的 6 根，保留输入的行与列；凑不满返回 None。"""
    _check_bars(bars)
    span = _window_slice(bars, _check_anchor(anchor_ms), _check_cycle(cycle))
    return None if span is None else bars.iloc[span]


# ---------------------------------------------------------------- 止损价


def _extreme_stop(bars: pd.DataFrame, span: slice, side: PositionSide, atr_period: int) -> StopCalc | None:
    """span 范围内的极值 ∓ 该根的 ATR（ATR 在整段 bars 上计算）；算不出返回 None。"""
    atr_series = atr(bars["high"], bars["low"], bars["close"], atr_period)
    if atr_series is None:
        return None
    is_long = side is PositionSide.LONG
    prices = bars["low" if is_long else "high"].to_numpy(dtype="float64")[span]
    if np.isnan(prices).all():
        return None
    j = int(np.nanargmin(prices) if is_long else np.nanargmax(prices))  # 并列取第一根，与 idxmin/idxmax 相同
    pos = span.start + j
    extreme = float(prices[j])
    atr_value = float(atr_series.iloc[pos])
    stop = extreme - atr_value if is_long else extreme + atr_value
    if not (math.isfinite(atr_value) and math.isfinite(stop)):
        return None
    return StopCalc(stop=Decimal(repr(stop)), extreme=extreme, atr=atr_value,
                    bar_open_ms=int(bars["open_time_ms"].iloc[pos]))


def stop_price(bars: pd.DataFrame, anchor_ms: int, side: PositionSide | str, cycle: int,
               atr_period: int = 14) -> StopCalc | None:
    """第 cycle 组 6 根已收盘 K 线的止损价：多 = 最低价 − 该根 ATR；空 = 最高价 + 该根 ATR。算不出返回 None。"""
    held = PositionSide(side)
    _check_bars(bars)
    period = check_period(atr_period, "ATR 周期")
    span = _window_slice(bars, _check_anchor(anchor_ms), _check_cycle(cycle))
    return None if span is None else _extreme_stop(bars, span, held, period)


def initial_stop(bars_before: pd.DataFrame, side: PositionSide | str, base_price: Decimal, a_pct: Decimal,
                 b_pct: Decimal, atr_period: int = 14) -> StopCalc | None:
    """初始止损（F2 第 1 步）：最近 6 根已收盘 K 线的极值 ± 该根 ATR，再夹在距基准价 A%–B% 之间。

    先限下限（至少 A% 远），再限上限（至多 B% 远）；要求 0 < A ≤ B < 100。K 线不足 6 根、少于 ATR 周期、
    或极值那根的 ATR 无效时返回 None。返回值的 extreme、atr、bar_open_ms 是夹之前那根 K 线的数据。
    """
    held = PositionSide(side)
    base = positive_decimal(base_price, "基准价")
    a = finite_decimal(a_pct, "初始止损距离下限 A")
    b = finite_decimal(b_pct, "初始止损距离上限 B")
    if not (0 < a <= b < 100):
        raise ValueError(f"初始止损距离必须 0 < A ≤ B < 100（百分数）：A={a_pct!r}，B={b_pct!r}")
    _check_bars(bars_before)
    period = check_period(atr_period, "ATR 周期")
    count = len(bars_before)
    if count < WINDOW:
        return None
    raw = _extreme_stop(bars_before, slice(count - WINDOW, count), held, period)
    if raw is None:
        return None
    with localcontext(EXACT):
        hundred = Decimal(100)
        if held is PositionSide.LONG:
            stop = max(min(raw.stop, base * (1 - a / hundred)), base * (1 - b / hundred))
        else:
            stop = min(max(raw.stop, base * (1 + a / hundred)), base * (1 + b / hundred))
    return replace(raw, stop=stop)


# ---------------------------------------------------------------- 只朝有利方向


def is_better(side: PositionSide | str, current: Decimal | float | None, new: Decimal | float) -> bool:
    """新止损价是否更有利：还没有止损（current 为 None）时采纳；多仓只上移，空仓只下移；相等不采纳。"""
    held = PositionSide(side)
    if new is None:
        raise ValueError("新止损价不能为空")
    proposed = finite_decimal(new, "新止损价")
    if current is None:
        return True
    existing = finite_decimal(current, "当前止损价")
    return proposed > existing if held is PositionSide.LONG else proposed < existing
