"""DMR 指标：中价比率的 n 期简单均值减 1。

来源（只读引用 DMR 源码；sha256 是该行段原文 UTF-8 的哈希，与 tests/parity/source_hashes.json 一致）：

- bit/strategy/bit_short_term/data_fetcher.py:124-152  ShortTermDataFetcher.calculate_dmr（短周期，金标列 dmr_12）
  sha256 7ae0be9da2910d6460fb110013cbc28c47d5c86edbb2aae53fd1704b2ecc879f
- bit/strategy/bit_long_term/data_fetcher.py:124-152  LongTermDataFetcher.calculate_dmr（长周期，金标列 dmr_6；
  与短周期版只差注释与日志文字）
  sha256 ee0ad4e7e02f26dc2cc037c41adc24f9e703d4b3824a466aed98f516c471a88b

誊写：中价 = (高 + 低) / 2；比率 = 中价 / 前一根中价，首值（没有前一根）按原实现取 1.0；DMR = 比率的 n 期简单滚动均值 − 1。
运算次序与原实现相同，对金标逐项误差 ≤ 1e-12（tests/parity/test_parity_dmr.py）。
去掉了原实现的对象状态、日志、log_dmr 列（读运行日志文件）与整表就地改写：输入不被修改，只返回一列。

有意差异（报告 5.4）：

- 第 12 项：前 n−1 个值是无效值 NaN（原实现 ``ffill().fillna(0)`` 补 0）。补的 0 与第一个有效值之间会被当成一次过零。
- 缺价（高或低为 NaN、无穷）的那根：依赖它的比率与 DMR 都是 NaN，窗口不再含它时恢复有效
  （原实现对比率整列补 1.0、对 DMR 向前填充，缺价的那根被当成「价格没变」）。
- 第 1 项：「当前值」只取已收盘 K 线。本函数不区分是否收盘，由调用方只传已收盘 K 线（原实现的序列含未收盘那根）。

版权与许可：本文件誊写自 DMR（DMR_binance_Version_V16_A1）的源码，保留其版权声明。
    Copyright 2026 shenchuangwl
    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    许可证全文见 DMR_WebUI/LICENSE，声明见 DMR_WebUI/NOTICE。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from dmr_kernel.algo.rounding import is_int


def check_period(n: int, what: str = "周期") -> int:
    """周期必须是正整数（bool 不算整数）。"""
    if not is_int(n) or n < 1:
        raise ValueError(f"{what}必须是正整数：{n!r}")
    return int(n)


def check_aligned(*series: pd.Series) -> None:
    """几列必须都是 pandas Series，且长度与索引一致。"""
    for s in series:
        if not isinstance(s, pd.Series):
            raise TypeError(f"需要 pandas Series，得到 {type(s).__name__}")
    first = series[0]
    for s in series[1:]:
        if len(s) != len(first) or not s.index.equals(first.index):
            raise ValueError("各列的长度与索引必须一致")


def _finite_or_nan(s: pd.Series) -> pd.Series:
    return s.where(np.isfinite(s))


def dmr(high: pd.Series, low: pd.Series, n: int) -> pd.Series:
    """DMR(n) 序列，与输入同索引，名为 ``dmr_<n>``；前 n−1 个值与依赖缺价 K 线的值为 NaN。"""
    period = check_period(n)
    check_aligned(high, low)
    mid = _finite_or_nan((high.astype("float64") + low.astype("float64")) / 2)
    ratio = _finite_or_nan(mid / mid.shift(1))
    if len(ratio):
        ratio.iloc[0] = 1.0 if np.isfinite(mid.iloc[0]) else np.nan  # 原实现：首值取 1.0
    out = ratio.rolling(window=period, min_periods=period).mean() - 1
    out.name = f"dmr_{period}"
    return out
