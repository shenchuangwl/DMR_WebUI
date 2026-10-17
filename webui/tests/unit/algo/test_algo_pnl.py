"""线性毛盈亏 dmr_kernel.algo.pnl.linear_pnl 的单元测试。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.algo.pnl import linear_pnl
from dmr_kernel.core.types import PositionSide

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT


@pytest.mark.parametrize(("side", "entry", "exit_", "qty", "expected"), [
    (LONG, D("100"), D("90"), D("2"), D("-20")),
    (LONG, D("90"), D("100"), D("2"), D("20")),
    (SHORT, D("100"), D("90"), D("2"), D("20")),
    (SHORT, D("90"), D("100"), D("2"), D("-20")),
    (LONG, D("100"), D("100"), D("3"), D("0")),
    (LONG, D("0.1"), D("0.3"), D("3"), D("0.6")),  # Decimal 精确：不会出现 0.6000000000000001
    ("SHORT", D("0.1457"), D("0.1401"), D("68"), D("0.3808")),
])
def test_linear_pnl(side, entry, exit_, qty, expected):
    got = linear_pnl(side, entry, exit_, qty)
    assert got == expected and isinstance(got, Decimal)


def test_linear_pnl_accepts_keyword_arguments_of_the_plan_signature():
    assert linear_pnl(side=LONG, entry=D("1"), exit=D("2"), qty=D("3")) == D("3")


@pytest.mark.parametrize(("entry", "exit_", "qty"), [
    (D("0"), D("90"), D("2")), (D("100"), D("0"), D("2")), (D("100"), D("90"), D("0")),
    (D("-1"), D("90"), D("2")), (D("NaN"), D("90"), D("2")), (None, D("90"), D("2")),
])
def test_linear_pnl_rejects_missing_or_non_positive_inputs(entry, exit_, qty):
    """原实现在数据缺失时返回 None；新实现抛错，调用方必须先拿到成交均价与数量（见对拍测试的说明）。"""
    with pytest.raises((ValueError, TypeError)):
        linear_pnl(LONG, entry, exit_, qty)


@pytest.mark.parametrize("side", ["long", "buy", "BOTH", None])
def test_linear_pnl_rejects_unknown_side(side):
    """有意差异：原实现把 'long'/'buy' 以外的任何值都当成空仓；新实现只认 PositionSide。"""
    with pytest.raises(ValueError):
        linear_pnl(side, D("100"), D("90"), D("2"))
