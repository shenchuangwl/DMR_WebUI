"""金额换数量 dmr_kernel.algo.sizing 的单元测试（F1 第 5 步：按数量步长向下取整，不足最小数量或最小名义金额就放弃）。"""
from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from dmr_kernel.algo.sizing import SizeResult, qty_raw, size_order

D = Decimal


def test_qty_raw_is_notional_over_price_without_rounding():
    assert qty_raw(D("10"), D("4")) == D("2.5")
    assert qty_raw(D("10"), D("3")) == D("10") / D("3")


@pytest.mark.parametrize(("notional", "price"), [(D("10"), D("0")), (D("10"), D("-1")), (D("0"), D("1")),
                                                 (D("-5"), D("1")), (D("NaN"), D("1"))])
def test_qty_raw_rejects_non_positive_inputs(notional, price):
    with pytest.raises(ValueError):
        qty_raw(notional, price)


def test_size_order_rounds_down_to_the_quantity_step():
    res = size_order(D("10"), D("0.1457"), D("1"), D("1"), D("5"))
    assert res == SizeResult(qty=D("68"), ok=True, reason="")
    res = size_order(D("100"), D("63000.1"), D("0.001"), D("0.001"), D("100"))
    assert res.qty == D("0.001") and not res.ok  # 0.001 × 63000.1 = 63.0001 < 100
    res = size_order(D("100"), D("31000"), D("0.001"), D("0.001"), D("50"))
    assert res.qty == D("0.003") and res.ok  # 100 / 31000 = 0.003225… → 0.003；名义 93 ≥ 50


def test_size_order_rejects_quantity_that_rounds_to_zero():
    res = size_order(D("5"), D("63000"), D("0.001"), D("0.001"), D("5"))
    assert res.qty == D("0") and not res.ok and "取整后数量为 0" in res.reason


def test_size_order_rejects_quantity_below_min_qty():
    res = size_order(D("10"), D("2.5"), D("0.1"), D("5"), D("1"))
    assert res.qty == D("4.0") and not res.ok and "最小数量" in res.reason


def test_size_order_rejects_notional_below_min_notional():
    res = size_order(D("5.04"), D("0.1"), D("1"), D("1"), D("5.04"))
    assert res.qty == D("50") and not res.ok and "最小名义金额" in res.reason  # 取整后名义金额 5.00 < 5.04
    assert size_order(D("5.04"), D("0.1"), D("0.01"), D("1"), D("5.04")).ok  # 步长更细时 50.4 × 0.1 = 5.04


def test_size_order_accepts_exact_minimums():
    res = size_order(D("5"), D("1"), D("1"), D("5"), D("5"))
    assert res.ok and res.qty == D("5")


@pytest.mark.parametrize("args", [
    (D("10"), D("1"), D("0"), D("1"), D("5")),  # 步长为 0
    (D("10"), D("1"), D("1"), D("-1"), D("5")),  # 最小数量为负
    (D("10"), D("1"), D("1"), D("1"), D("-5")),  # 最小名义金额为负
])
def test_size_order_rejects_invalid_rules(args):
    with pytest.raises(ValueError):
        size_order(*args)


def test_size_result_is_frozen():
    res = size_order(D("10"), D("0.1457"), D("1"), D("1"), D("5"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.ok = False  # type: ignore[misc]
