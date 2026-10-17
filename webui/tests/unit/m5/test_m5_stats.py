"""统计（ledger/stats.py）：按书面定义手算样例逐项比对。盈亏比 = 盈利交易平均净盈亏 ÷ |亏损交易平均净盈亏|；
盈利率 = 净盈亏合计 ÷ 开仓名义金额合计；胜率 = 净盈亏 > 0 的笔数 ÷ 总笔数。"""
from __future__ import annotations

from decimal import Decimal

from dmr_kernel.core.types import ExitReason, Ownership, PositionSide
from dmr_kernel.ledger import stats
from dmr_kernel.ledger.records import TradeRecord

D = Decimal
_ids = iter(range(1, 1000))


def trade(net, notional, duration_ms, *, symbol="BTCUSDT", side=PositionSide.LONG, ownership=Ownership.STRATEGY,
          model="m1", mixed=False, fees="1", funding="0.5", open_ms=1_000) -> TradeRecord:
    net, fees, funding = D(str(net)), D(str(fees)), D(str(funding))
    n = next(_ids)
    return TradeRecord(
        id=n, position_id=n, symbol=symbol, side=side, ownership=ownership, model=model, is_mixed=mixed,
        open_ms=open_ms, close_ms=open_ms + duration_ms, open_time_estimated=False, max_qty=D(1),
        open_avg=None if notional is None else D(str(notional)), close_avg=None, open_notional=None if notional is None else D(str(notional)),
        gross_pnl=net + fees - funding, fees=fees, funding=funding, net_pnl=net, entry_reason="", exit_reason=ExitReason.SIGNAL,
        list_snapshot_id=None, list_direction=None, list_score=None)


SAMPLE = [
    trade(30, 1000, 60_000),
    trade(10, 500, 120_000),
    trade(-20, 800, 30_000),
    trade(-40, 700, 90_000),
    trade(0, 1000, 0),
]


def test_hand_computed_sample_item_by_item():
    s = stats.compute(SAMPLE)
    assert s["count"] == 5 and s["wins"] == 2 and s["losses"] == 2
    assert s["net_pnl"] == D(-20)
    assert s["fees"] == D(5) and s["funding"] == D("2.5")
    assert s["gross_pnl"] == D("-17.5")  # 净 = 毛 − 手续费 + 资金费 → 毛 = −20 + 5 − 2.5
    assert s["open_notional"] == D(4000)
    assert s["win_rate"] == D("0.4")
    assert s["profit_rate"] == D("-0.005")  # −20 / 4000
    assert abs(s["profit_loss_ratio"] - D(2) / D(3)) < D("1e-20")  # (40/2) / |(-60)/2|
    assert s["avg_duration_ms"] == 60_000
    assert s["max_win"] == D(30) and s["max_loss"] == D(-40)


def test_profit_loss_ratio_edges():
    only_wins = stats.compute([trade(5, 100, 1), trade(7, 100, 1)])
    assert only_wins["profit_loss_ratio"] is None  # 没有亏损交易
    only_losses = stats.compute([trade(-5, 100, 1)])
    assert only_losses["profit_loss_ratio"] == D(0)  # 没有盈利交易
    assert only_losses["win_rate"] == D(0)


def test_empty_set():
    s = stats.compute([])
    assert s["count"] == 0 and s["net_pnl"] == D(0)
    assert s["win_rate"] is None and s["profit_rate"] is None and s["profit_loss_ratio"] is None
    assert s["avg_duration_ms"] is None and s["max_win"] is None and s["max_loss"] is None


def test_profit_rate_ignores_trades_without_notional_and_zero_denominator():
    assert stats.compute([trade(5, None, 1)])["profit_rate"] is None
    s = stats.compute([trade(5, 100, 1), trade(-1, None, 1)])
    assert s["profit_rate"] == D("0.04")  # 分子是全部净盈亏合计 4，分母只累加有名义金额的 100


def test_grouping_keeps_mixed_apart_and_overall_covers_everything():
    rows = [
        trade(10, 100, 1, model="m1", side=PositionSide.LONG, symbol="BTCUSDT"),
        trade(-4, 100, 1, model="m1", side=PositionSide.SHORT, symbol="ETHUSDT"),
        trade(6, 200, 1, model="m2", ownership=Ownership.TAKEOVER, side=PositionSide.LONG, symbol="BTCUSDT"),
        trade(100, 300, 1, model="m1", mixed=True),
    ]
    by_model = stats.grouped(rows, "model")
    assert by_model["overall"]["count"] == 4 and by_model["overall"]["net_pnl"] == D(112)
    assert by_model["mixed"]["count"] == 1 and by_model["mixed"]["net_pnl"] == D(100)
    keys = {g["key"]: g["stats"] for g in by_model["groups"]}
    assert set(keys) == {"m1", "m2"}
    assert keys["m1"]["count"] == 2 and keys["m1"]["net_pnl"] == D(6)  # 混合仓不进分组
    assert keys["m2"]["net_pnl"] == D(6)
    assert [g["key"] for g in stats.grouped(rows, "side")["groups"]] == ["LONG", "SHORT"]
    assert [g["key"] for g in stats.grouped(rows, "symbol")["groups"]] == ["BTCUSDT", "ETHUSDT"]
    assert {g["key"] for g in stats.grouped(rows, "ownership")["groups"]} == {"strategy", "takeover"}


def test_grouping_rejects_unknown_dimension():
    import pytest
    with pytest.raises(ValueError):
        stats.grouped([], "colour")


def test_empty_grouping_has_overall_and_mixed():
    out = stats.grouped([], "model")
    assert out["groups"] == [] and out["overall"]["count"] == 0 and out["mixed"]["count"] == 0
