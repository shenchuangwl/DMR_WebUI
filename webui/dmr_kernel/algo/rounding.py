"""取整：按步长向下取整（数量、价格）、止损价按方向取整到价格步长（新增）、边界处换成 Decimal。

来源（只读引用 DMR 源码；sha256 是该行段原文 UTF-8 的哈希，与 tests/parity/source_hashes.json 一致）：

- common/precision_helper.py:276-293  PrecisionHelper._round_down_decimal（按步长向下取整）
  sha256 3d7b0d31c1234dbfb600d8ffeec2f5b69633db7cc5d26a874a2beab1a997516e
- common/precision_helper.py:295-297  PrecisionHelper._decimal_to_str（定点字符串）
  sha256 f612e7803171b4f589264ed6ba9928c48757f68a22c00d3018667d89edeea79f
- tests/offline/test_business_regressions.py:99-108  取整用例的数值（对拍只解析文本，不运行该文件）
  sha256 03cc6e98b12d7c0f65bd045c4a3fe05d0a5f115f6ce9a4d44018cf49b7db80a0

誊写与差异：

- ``floor_to_step`` 对应 ``_round_down_decimal`` 给了步长的分支：(值 / 步长) 向下取整 × 步长。float 先经最短表示转成
  Decimal（与原实现的 ``Decimal(str(value))`` 一致，1.13 不会变成 1.12999…）；NaN、无穷报 ValueError（与原实现一致）。
  有意差异：商用 Decimal 整除（``//``，结果精确）而不是「先除、再取整」，商不会因有效位数的舍入越过整数；
  负值、步长不为正一律报错（原实现在步长为空或不为正时改按精度位数截断）。
- 结果的指数与步长相同，``format(x, "f")`` 就是交易所要的定点字符串（对应 ``_decimal_to_str``，例如 '100.30'）。
- ``round_stop_price``（新增，报告 F2）：多仓止损向下、空仓止损向上取整到价格步长，即离现价更远，避免一挂就触发；
  取整后不为正报 ValueError。
- 只做纯计算：不读交易所规则、不读配置；步长由调用方从合约规则取得后传入。

版权与许可：本文件誊写自 DMR（DMR_binance_Version_V16_A1）的源码，保留其版权声明。
    Copyright 2026 shenchuangwl
    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    许可证全文见 DMR_WebUI/LICENSE，声明见 DMR_WebUI/NOTICE。
"""
from __future__ import annotations

from decimal import Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext

import numpy as np

from dmr_kernel.core.types import PositionSide

#: 算法里 Decimal 运算统一用的上下文（经 ``localcontext`` 复制后使用，不受调用方全局上下文影响）：
#: 50 位有效数字（价格、数量、金额远用不到），这个范围内加减乘与整除都是精确的；非法运算、除零、溢出直接报错。
EXACT = Context(prec=50, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])


def is_int(value: object) -> bool:
    """是整数（Python int 或 numpy 整数）；bool 不算。"""
    return isinstance(value, (int, np.integer)) and not isinstance(value, bool)


def to_decimal(value: Decimal | int | float) -> Decimal:
    """边界处换成 Decimal：Decimal 原样返回；整数精确换算；float 经最短表示（``repr``）换算，即 Decimal(str(x))。

    bool、字符串、None 等不是数值的输入是程序错误，报 TypeError。非有限值（NaN、无穷）照样换算，由调用方检查。
    """
    if isinstance(value, bool):
        raise TypeError(f"不接受布尔值作为数值：{value!r}")
    if isinstance(value, Decimal):
        return value
    if is_int(value):
        return Decimal(int(value))
    if isinstance(value, float):
        return Decimal(repr(float(value)))
    raise TypeError(f"不是数值：{value!r}（{type(value).__name__}）")


def finite_decimal(value: Decimal | int | float, what: str) -> Decimal:
    """换成 Decimal 并要求是有限值；NaN、无穷报 ValueError（what 写进错误信息）。"""
    out = to_decimal(value)
    if not out.is_finite():
        raise ValueError(f"{what}不是有限数值：{value!r}")
    return out


def positive_decimal(value: Decimal | int | float, what: str) -> Decimal:
    """换成 Decimal 并要求是有限的正数。"""
    out = finite_decimal(value, what)
    if out <= 0:
        raise ValueError(f"{what}必须为正：{value!r}")
    return out


def floor_to_step(value: Decimal | int | float, step: Decimal | int | float) -> Decimal:
    """把非负的 value 向下取整到 step 的整数倍（结果的指数与 step 相同）。

    >>> floor_to_step(Decimal("100.33"), Decimal("0.05"))
    Decimal('100.30')
    """
    v = finite_decimal(value, "取整的数值")
    s = positive_decimal(step, "步长")
    if v < 0:
        raise ValueError(f"取整的数值不能为负：{value!r}")
    if v.is_zero():
        v = v.copy_abs()  # -0 按 0 处理
    with localcontext(EXACT):
        return (v // s) * s


def round_stop_price(price: Decimal | int | float, tick: Decimal | int | float, side: PositionSide | str) -> Decimal:
    """止损价按方向取整到价格步长：多仓向下、空仓向上（都是离现价更远的方向）。取整后必须为正（结果的指数与 tick 相同）。

    price 通常是 ``StopCalc.stop``（Decimal）；给 float 时在这里经最短表示换成 Decimal。
    """
    ps = PositionSide(side)
    p = positive_decimal(price, "止损价")
    t = positive_decimal(tick, "价格步长")
    with localcontext(EXACT):
        q, r = divmod(p, t)
        if r and ps is PositionSide.SHORT:
            q += 1
        out = q * t
    if out <= 0:
        raise ValueError(f"止损价 {p} 按价格步长 {t} 取整后不为正")
    return out
