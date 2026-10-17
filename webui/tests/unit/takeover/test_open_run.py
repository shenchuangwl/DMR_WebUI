"""``open_run``：从成交记录里找「仓位从零变非零」的那一段（纯函数）。"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from dmr_kernel.core.types import OrderOrigin, OrderSide, PositionSide
from dmr_kernel.models.m2_takeover import open_run

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL


def fill(side, qty, t):
    return SimpleNamespace(side=side, qty=D(qty), time_ms=t, origin=OrderOrigin.EXTERNAL)


def test_no_fills_and_flat_positions_have_no_run():
    assert open_run([], PositionSide.LONG) is None
    assert open_run([fill(BUY, "1", 1), fill(SELL, "1", 2)], PositionSide.LONG) is None


def test_the_run_starts_at_the_fill_that_took_the_position_from_zero():
    fills = [fill(BUY, "1", 1), fill(SELL, "1", 2), fill(BUY, "2", 3), fill(BUY, "1", 4), fill(SELL, "1", 5)]
    run = open_run(fills, PositionSide.LONG)
    assert [f.time_ms for f in run] == [3, 4, 5]


def test_short_side_opens_with_sells():
    fills = [fill(SELL, "1", 1), fill(SELL, "1", 2), fill(BUY, "1", 3)]
    assert [f.time_ms for f in open_run(fills, PositionSide.SHORT)] == [1, 2, 3]
    assert [f.time_ms for f in open_run(fills, PositionSide.LONG)] == [3]  # 多头方向：先卖的按零处理，最后一笔买入才是起点
