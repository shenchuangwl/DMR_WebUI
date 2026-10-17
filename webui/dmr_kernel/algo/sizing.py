"""金额换数量：原实现的未取整数量（对拍用），以及按数量步长向下取整、校验最小数量与最小名义金额的下单数量（报告 F1 第 5 步）。

来源（只读引用 DMR 源码；sha256 是该行段原文 UTF-8 的哈希，与 tests/parity/source_hashes.json 一致）：

- bit/strategy/bit_short_term/position_manager.py:19-23  ShortTermPositionManager.calculate_position_size
  sha256 a90f0a760b41a77a900807b8122f1ed133db3ea40257d9987b8c2b52efc0cc14

誊写：``qty_raw`` = 金额 / 价格，不取整；输入是 Decimal 时与原实现逐位相同（按调用方当前的 Decimal 上下文相除，与原实现一样）。
去掉了打印与对象状态：金额由调用方传入。

有意差异：

- 报告 5.4 第 5 项：原实现按未取整的数量本地记账，提交时由 ccxt 按精度截断；``size_order`` 按数量步长向下取整
  （精确整除：⌊金额 / (价格 × 步长)⌋ × 步长），再校验最小数量与最小名义金额（取整后的数量 × 价格），
  不满足就不下单（``ok=False``，``reason`` 写明原因）。
- 金额、价格不为正（含 NaN、无穷）报 ValueError；原实现照样相除。

版权与许可：本文件誊写自 DMR（DMR_binance_Version_V16_A1）的源码，保留其版权声明。
    Copyright 2026 shenchuangwl
    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    许可证全文见 DMR_WebUI/LICENSE，声明见 DMR_WebUI/NOTICE。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from dmr_kernel.algo.rounding import EXACT, finite_decimal, positive_decimal


@dataclass(frozen=True)
class SizeResult:
    """下单数量：qty 是按步长向下取整后的数量；ok=False 时不下单，reason 写明原因。"""

    qty: Decimal
    ok: bool
    reason: str = ""


def _non_negative(value: Decimal | int | float, what: str) -> Decimal:
    out = finite_decimal(value, what)
    if out < 0:
        raise ValueError(f"{what}不能为负：{value!r}")
    return out


def _s(value: Decimal) -> str:
    return format(value, "f")


def qty_raw(notional: Decimal, price: Decimal) -> Decimal:
    """原实现的数量：金额 / 价格，不取整。"""
    return positive_decimal(notional, "金额") / positive_decimal(price, "价格")


def size_order(notional: Decimal, price: Decimal, step: Decimal, min_qty: Decimal,
               min_notional: Decimal) -> SizeResult:
    """按数量步长向下取整；取整后为 0、小于最小数量、或名义金额小于最小名义金额时 ok=False。"""
    n = positive_decimal(notional, "金额")
    p = positive_decimal(price, "价格")
    s = positive_decimal(step, "数量步长")
    mq = _non_negative(min_qty, "最小数量")
    mn = _non_negative(min_notional, "最小名义金额")
    with localcontext(EXACT):
        qty = (n // (p * s)) * s
        value = qty * p
    if qty.is_zero():
        return SizeResult(qty, False, f"取整后数量为 0（金额 {_s(n)} ÷ 价格 {_s(p)}，数量步长 {_s(s)}）")
    if qty < mq:
        return SizeResult(qty, False, f"数量 {_s(qty)} 小于最小数量 {_s(mq)}")
    if value < mn:
        return SizeResult(qty, False, f"名义金额 {_s(value)}（数量 {_s(qty)} × 价格 {_s(p)}）小于最小名义金额 {_s(mn)}")
    return SizeResult(qty, True, "")
