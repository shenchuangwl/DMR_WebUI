"""步长取整、金额换数量、线性毛盈亏的对拍。

- 取整：原实现 common/precision_helper.py 的 _round_down_decimal / _decimal_to_str；用例数值从
  tests/offline/test_business_regressions.py 第 99–108 行的文本里解析（只读文本，不运行该文件）。
- 数量：原实现 bit/strategy/bit_short_term/position_manager.py 的 calculate_position_size（不取整）。
- 盈亏：原实现 common/pnl.py 的 realized_pnl；基准值从 tests/test_security_fixes.py 第 103–107 行的文本里解析
  （只读文本，绝不运行这个文件：它会改写真实的 model_state.json）。
"""
from __future__ import annotations

import itertools
from decimal import Decimal

import numpy as np
import pytest

import dmr_originals as orig
from dmr_kernel.algo.pnl import linear_pnl
from dmr_kernel.algo.rounding import floor_to_step
from dmr_kernel.algo.sizing import qty_raw, size_order
from dmr_kernel.core.types import PositionSide

D = Decimal


# ---------------------------------------------------------------- 步长取整


def test_floor_to_step_matches_original_regression_cases():
    cases = orig.precision_regression_cases()
    old = orig.round_down_decimal()
    for kind, step in (("price", D(cases["tick"])), ("quantity", D(cases["step"]))):
        for value, expected in cases[kind]:
            new = floor_to_step(D(str(value)), step)
            assert new == old(value, 0, step)
            assert format(new, "f") == expected  # '100.30'、'0.0002345'
    for value in cases["nonfinite"]:
        with pytest.raises(ValueError):
            old(value, 2, D(cases["tick"]))
        with pytest.raises(ValueError):
            floor_to_step(value, D(cases["tick"]))


@pytest.mark.parametrize("step", ["0.05", "0.1", "0.001", "1", "5", "0.0000001", "0.00001000"])
def test_floor_to_step_matches_original_on_a_grid(step):
    old = orig.round_down_decimal()
    rng = np.random.default_rng(len(step))
    values = [D("0"), D(step), D(step) * 3, D("100.33"), D("0.0002345678")] + [
        D(str(round(float(v), 9))) for v in rng.uniform(0, 1000, 50)]
    for value in values:
        assert floor_to_step(value, D(step)) == old(value, 0, D(step))


# ---------------------------------------------------------------- 金额换数量


QTY_CASES = [(D("10"), D("0.1457")), (D("10"), D("0.02729")), (D("25"), D("63000.1")), (D("7.5"), D("3"))]


@pytest.mark.parametrize(("notional", "price"), QTY_CASES)
def test_qty_raw_matches_original_without_rounding(notional, price):
    old = orig.calculate_position_size()
    assert qty_raw(notional, price) == old(notional, price)  # 同为 Decimal 时逐位相同
    float_old = old(float(notional), float(price))  # 原实现实际吃的是 float
    assert abs(float(qty_raw(notional, price)) - float_old) <= 1e-12 * abs(float_old)


def test_size_order_rounds_down_and_checks_minimums_unlike_original():
    """5.4 第 5 项（有意差异）：原实现的数量是金额除以价格，本地按未取整的值记账，提交时由 ccxt 按精度截断；
    新实现按数量步长向下取整，并校验最小数量与最小名义金额，不满足就不下单（ok=False，写明原因）。"""
    old = orig.calculate_position_size()
    unrounded = old(D("10"), D("0.1457"))
    res = size_order(D("10"), D("0.1457"), D("1"), D("1"), D("5"))
    assert unrounded == D("10") / D("0.1457") and unrounded != unrounded.to_integral_value()
    assert res.ok and res.qty == D("68") and res.qty < unrounded
    below = size_order(D("4"), D("0.1457"), D("1"), D("1"), D("5"))  # 27 × 0.1457 = 3.9339 < 5
    assert not below.ok and below.qty == D("27") and "最小名义金额" in below.reason
    assert old(D("4"), D("0.1457")) > 0  # 原实现照样给出一个数量


# ---------------------------------------------------------------- 线性毛盈亏


def test_linear_pnl_matches_original_base_values():
    """基准值来自 tests/test_security_fixes.py。有意差异：数据缺失（价格或数量不为正）时原实现返回 None，
    新实现抛 ValueError——调用方必须拿到成交均价与数量才计算，不能把「算不出」当成 0 或悄悄跳过。"""
    old = orig.realized_pnl()
    cases = orig.pnl_regression_cases()
    assert {c[-1] for c in cases} >= {-20.0, 20.0, None}
    for side, entry, exit_, amount, expected in cases:
        new_side = PositionSide.LONG if side == "long" else PositionSide.SHORT
        if expected is None:
            assert old(side, entry, exit_, amount) is None
            with pytest.raises(ValueError):
                linear_pnl(new_side, D(entry), D(exit_), D(amount))
        else:
            assert abs(old(side, entry, exit_, amount) - expected) < 1e-9
            assert linear_pnl(new_side, D(entry), D(exit_), D(amount)) == D(str(expected))


@pytest.mark.parametrize(("side", "entry", "exit_", "qty"), list(itertools.product(
    [PositionSide.LONG, PositionSide.SHORT], ["100", "0.1457", "63000.5"], ["90", "0.1501", "61234.25"],
    ["2", "68", "0.003"])))
def test_linear_pnl_matches_original_on_a_grid(side, entry, exit_, qty):
    old = orig.realized_pnl()(side.value.lower(), float(entry), float(exit_), float(qty))
    new = linear_pnl(side, D(entry), D(exit_), D(qty))
    assert new == (D(exit_) - D(entry)) * D(qty) * (1 if side is PositionSide.LONG else -1)
    assert abs(float(new) - old) <= 1e-9 * max(1.0, abs(old))
