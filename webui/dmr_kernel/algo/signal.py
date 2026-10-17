"""信号判定：DMR12 过零、长周期趋势、双周期协调，以及在协调之上的开仓判定（报告 F1 第 2 步）与出场判定（F11 第 2 步）。

来源（只读引用 DMR 源码 bit/strategy/bit_short_term/strategy_engine.py；sha256 是该行段原文 UTF-8 的哈希，
与 tests/parity/source_hashes.json 一致）：

- bit/strategy/bit_short_term/strategy_engine.py:282-293  detect_signal 里的过零语句段（誊写为 crossing）
  sha256 cb7e43a23d36ceeef2b9d5add479f614386072c4586cebbf5815e22bcbeb270b
- bit/strategy/bit_short_term/strategy_engine.py:129-146  get_long_term_trend 里的语句段（誊写为 long_trend）
  sha256 0317e3c0b306fb567c3825ab36dfbf01953a4c12177ea79dab8cd18bf3e1ce82
- bit/strategy/bit_short_term/strategy_engine.py:326-377  ShortTermDMRStrategy._coordinate_dual_timeframe_signal
  （誊写为 coordinate）
  sha256 afaf0a59eabb9c117360f816e74bb23d47eb48f012ebcb763b6eb026e34f72e3
- bit/strategy/bit_short_term/strategy_engine.py:266-269  detect_signal 取当前值与落盘前值（5.4 第 1、2 项的对照，不誊写）
  sha256 dbf0f5bf49c428627e9e1480185ce6851fdaa118d2b8bc54427ee069a2801b56
- bit/strategy/bit_short_term/strategy_engine.py:296-302  协调结果为空时改用纠错信号（5.4 第 4 项的对照，不誊写）
  sha256 ae4af5ddf69f8606639d1937814a34ef4f5ff14cbb5ec5072d993556f32a0d8f
- bit/strategy/bit_short_term/strategy_engine.py:379-415  ShortTermDMRStrategy._detect_position_correction_signal
  （持仓纠错信号；5.4 第 4 项的对照，不誊写）
  sha256 f05b9f83251d4a3b4504f9d4f02b42a841688764059e19544a6caeb3704f5557

返回值是 StrEnum 成员（PositionSide、Trend、Coordinated、ExitAction），与原实现的字符串相等（'LONG' 等）；
持仓方向用 PositionSide（原实现是小写的 'long'/'short'）。去掉了日志、对象状态与持仓查询：持仓方向由调用方传入。

有意差异（报告 5.4）：

- 第 1 项：当前值只取已收盘 K 线（原实现取含未收盘那根的序列末行）。由调用方只传已收盘 K 线的 DMR 值。
- 第 2 项：过零比较相邻两根已收盘 K 线（prev、curr），不比较「上次运行落盘的值」（原实现漏跑一根时会迟一根才报）；
  按开盘时间去重与缺根补判由调用方负责。
- 第 3 项：长周期取不到（None）时不出信号——协调返回 None、开仓判定返回 None、出场判定返回 PENDING（待补判，
  补判时只平不反）；原实现跳过过滤、直接执行短周期信号。
- 第 4 项：首期不实现持仓纠错信号（原实现默认开启，没有过零也会出信号、可反手）：没有过零就没有任何信号。
- 无效值（None、NaN、无穷）不参与判断：过零返回 None；长周期 NaN 是「取不到」而不是中性（原实现 NaN 落进中性分支；
  与第 12 项配套：DMR 序列开头的无效值是 NaN）。

版权与许可：本文件誊写自 DMR（DMR_binance_Version_V16_A1）的源码，保留其版权声明。
    Copyright 2026 shenchuangwl
    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    许可证全文见 DMR_WebUI/LICENSE，声明见 DMR_WebUI/NOTICE。
"""
from __future__ import annotations

import math
from enum import StrEnum

import numpy as np
import pandas as pd

from dmr_kernel.core.types import PositionSide


class Trend(StrEnum):
    """长周期趋势（DMR6 的符号）。"""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Coordinated(StrEnum):
    """双周期协调的结果：开多、开空、平多、平空。"""

    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"


class ExitAction(StrEnum):
    """出场判定：只平、反手（先平后开反向）、待补判（长周期取不到，补判时只平不反）。"""

    CLOSE = "CLOSE"
    REVERSE = "REVERSE"
    PENDING = "PENDING"


def _value(x: object) -> float | None:
    """DMR 值换成 float；None、NaN、无穷是无效值，返回 None。不是数值是程序错误。"""
    if x is None or x is pd.NA:
        return None
    if isinstance(x, bool) or not isinstance(x, (int, float, np.integer, np.floating)):
        raise TypeError(f"DMR 值必须是数值：{x!r}")
    f = float(x)
    return f if math.isfinite(f) else None


def _side(side: PositionSide | str | None) -> PositionSide | None:
    return None if side is None else PositionSide(side)


def crossing(prev: float | None, curr: float | None) -> PositionSide | None:
    """相邻两根已收盘 K 线的 DMR12 过零：prev ≤ 0 且 curr > 0 是上穿（LONG）；prev ≥ 0 且 curr < 0 是下穿（SHORT）。"""
    p, c = _value(prev), _value(curr)
    if p is None or c is None:
        return None
    if p <= 0 and c > 0:
        return PositionSide.LONG
    if p >= 0 and c < 0:
        return PositionSide.SHORT
    return None


def long_trend(dmr6: float | None) -> Trend | None:
    """最后一根已收盘长周期 K 线的 DMR6 > 0 多头、< 0 空头、= 0 中性；None、NaN、无穷表示取不到，返回 None。"""
    v = _value(dmr6)
    if v is None:
        return None
    if v > 0:
        return Trend.BULLISH
    if v < 0:
        return Trend.BEARISH
    return Trend.NEUTRAL


def coordinate(short_signal: PositionSide | str | None, long_trend: Trend | str | None,
               position_side: PositionSide | str | None) -> Coordinated | None:
    """双周期协调：顺势的过零开仓；逆势的过零只平掉已有的同向持仓（不反向开仓）；中性或取不到不出信号。"""
    signal = _side(short_signal)
    trend = None if long_trend is None else Trend(long_trend)
    side = _side(position_side)
    if signal is None or trend is None or trend is Trend.NEUTRAL:
        return None
    if trend is Trend.BULLISH:
        if signal is PositionSide.LONG:
            return Coordinated.LONG
        return Coordinated.CLOSE_LONG if side is PositionSide.LONG else None
    if signal is PositionSide.SHORT:
        return Coordinated.SHORT
    return Coordinated.CLOSE_SHORT if side is PositionSide.SHORT else None


def open_signal(prev12: float | None, curr12: float | None, dmr6: float | None) -> PositionSide | None:
    """开仓判定（无持仓时，F1 第 2 步）：上穿配 DMR6 > 0 开多，下穿配 DMR6 < 0 开空；DMR6 取不到或等于 0 不出信号。"""
    out = coordinate(crossing(prev12, curr12), long_trend(dmr6), None)
    return None if out is None else PositionSide(out.value)


def exit_signal(prev12: float | None, curr12: float | None, dmr6: float | None,
                side: PositionSide | str) -> ExitAction | None:
    """出场判定（持仓的信号出场，F11 第 2 步）：只看与持仓反向的过零。

    DMR6 与持仓同向只平（CLOSE）；反向反手（REVERSE）；等于 0 不动作（None，由止损保护）；取不到待补判（PENDING）。
    """
    if side is None:
        raise ValueError("出场判定需要持仓方向")
    held = PositionSide(side)
    cross = crossing(prev12, curr12)
    if cross is None or cross is held:
        return None
    trend = long_trend(dmr6)
    if trend is None:
        return ExitAction.PENDING
    out = coordinate(cross, trend, held)
    if out is None:
        return None
    if out in (Coordinated.CLOSE_LONG, Coordinated.CLOSE_SHORT):
        return ExitAction.CLOSE
    return ExitAction.REVERSE
