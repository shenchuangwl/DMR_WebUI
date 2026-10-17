"""线性毛盈亏（USDT 本位永续合约）：不含手续费与资金费。

来源（只读引用 DMR 源码；sha256 是该行段原文 UTF-8 的哈希，与 tests/parity/source_hashes.json 一致）：

- common/pnl.py:67-83  realized_pnl
  sha256 627541a117285d70bd58ff888f8d5cb5f95804e589a99d89e581de6a8ffafeac
- tests/test_security_fixes.py:103-107  盈亏基准值（对拍只解析文本，绝不运行这个文件：它会改写真实的 model_state.json）
  sha256 c885788aef288bfa950369c3f05e9774b1683440f5945eec38d139f01cc4f2b6

誊写：多 = (平仓价 − 开仓价) × 数量；空 = (开仓价 − 平仓价) × 数量。

有意差异：

- 用 Decimal 精确计算（原实现换成 float：0.1 → 0.3 的 3 张得到 0.6000000000000001）。
- 数据缺失（价格或数量不为正、NaN、None）报错而不是返回 None：调用方必须拿到成交均价与数量才计算，
  不能把「算不出」当成 0 或悄悄跳过。
- 方向只认 PositionSide（原实现把 'long'/'buy' 以外的任何值都当成空仓）。

版权与许可：本文件誊写自 DMR（DMR_binance_Version_V16_A1）的源码，保留其版权声明。
    Copyright 2026 shenchuangwl
    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    许可证全文见 DMR_WebUI/LICENSE，声明见 DMR_WebUI/NOTICE。
"""
from __future__ import annotations

from decimal import Decimal, localcontext

from dmr_kernel.algo.rounding import EXACT, positive_decimal
from dmr_kernel.core.types import PositionSide


def linear_pnl(side: PositionSide | str, entry: Decimal, exit: Decimal, qty: Decimal) -> Decimal:  # noqa: A002
    """线性毛盈亏（USDT）。entry、exit 是成交均价，qty 是成交数量，都必须是有限的正数。"""
    held = PositionSide(side)
    e = positive_decimal(entry, "开仓均价")
    x = positive_decimal(exit, "平仓均价")
    q = positive_decimal(qty, "数量")
    with localcontext(EXACT):
        return (x - e) * q if held is PositionSide.LONG else (e - x) * q
