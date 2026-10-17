"""取整 dmr_kernel.algo.rounding 的单元测试：步长向下取整、止损价按方向取整到价格步长（新增）。"""
from __future__ import annotations

import math
from decimal import Decimal

import pytest

from dmr_kernel.algo.rounding import floor_to_step, round_stop_price, to_decimal
from dmr_kernel.core.types import PositionSide

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT


# ---------------------------------------------------------------- 步长向下取整


@pytest.mark.parametrize(("value", "step", "expected"), [
    (D("100.33"), D("0.05"), D("100.30")),
    (D("1.13"), D("0.01"), D("1.13")),  # 原实现注释里 math.floor(1.13 × 100) 的浮点陷阱：Decimal 下不会变成 1.12
    (D("0.123456"), D("0.001"), D("0.123")),
    (D("68.63417982155113"), D("1"), D("68")),
    (D("12.5"), D("2.5"), D("12.5")),  # 正好是整数倍：不变
    (D("0.04"), D("0.05"), D("0")),  # 不足一个步长
    (D("0"), D("0.001"), D("0")),
    (7, D("5"), D("5")),  # 整数也可以
])
def test_floor_to_step(value, step, expected):
    got = floor_to_step(value, step)
    assert got == expected and isinstance(got, Decimal)


def test_floor_to_step_converts_floats_through_their_shortest_repr():
    """在边界处把 float 转成 Decimal(str(x))，与原实现一致：1.13 不会因为二进制表示变成 1.12999…"""
    assert floor_to_step(1.13, D("0.01")) == D("1.13")
    assert floor_to_step(100.33, 0.05) == D("100.30")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, D("NaN"), D("Infinity"), None, True, "1.5"])
def test_floor_to_step_rejects_non_finite_or_non_numeric_values(value):
    with pytest.raises((ValueError, TypeError)):
        floor_to_step(value, D("0.01"))


def test_floor_to_step_rejects_negative_values():
    with pytest.raises(ValueError):
        floor_to_step(D("-1.5"), D("0.1"))


@pytest.mark.parametrize("step", [D("0"), D("-0.01"), D("NaN"), math.inf, None])
def test_floor_to_step_rejects_invalid_step(step):
    with pytest.raises((ValueError, TypeError)):
        floor_to_step(D("1.5"), step)


def test_to_decimal_rejects_bool_and_keeps_decimals():
    assert to_decimal(D("0.1")) == D("0.1") and to_decimal(3) == D(3) and to_decimal(0.1) == D("0.1")
    with pytest.raises(TypeError):
        to_decimal(False)


# ---------------------------------------------------------------- 止损价取整（新增）


@pytest.mark.parametrize(("price", "tick", "side", "expected"), [
    (D("100.37"), D("0.05"), LONG, D("100.35")),  # 多仓止损向下取整：离现价更远
    (D("100.37"), D("0.05"), SHORT, D("100.40")),  # 空仓止损向上取整：离现价更远
    (D("100.35"), D("0.05"), LONG, D("100.35")),  # 已在步长上：不变
    (D("100.35"), D("0.05"), SHORT, D("100.35")),
    (D("0.1442999"), D("0.0001"), LONG, D("0.1442")),
    (D("0.1442001"), D("0.0001"), SHORT, D("0.1443")),
    (97.82142857142857, D("0.01"), LONG, D("97.82")),  # 算法给出的 float：在边界处转 Decimal(str(x))
    (97.82142857142857, D("0.01"), SHORT, D("97.83")),
])
def test_round_stop_price_moves_away_from_the_market(price, tick, side, expected):
    got = round_stop_price(price, tick, side)
    assert got == expected and isinstance(got, Decimal)


@pytest.mark.parametrize("price", [D("0"), D("-1"), D("NaN"), math.inf, D("0.004")])
def test_round_stop_price_rejects_prices_that_are_not_positive_after_rounding(price):
    with pytest.raises(ValueError):
        round_stop_price(price, D("0.01"), LONG)


def test_round_stop_price_rejects_unknown_side_and_bad_tick():
    with pytest.raises(ValueError):
        round_stop_price(D("100"), D("0.01"), "long")
    with pytest.raises(ValueError):
        round_stop_price(D("100"), D("0"), LONG)
