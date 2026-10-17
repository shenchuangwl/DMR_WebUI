"""撮合规则（docs/设计约定.md 7.6）：市价单、对冲两侧、均价、实现盈亏、手续费、最小名义金额、保证金。"""
from __future__ import annotations

import pytest

from dmr_standin.engine import EngineError
from standin_kit import D, make_engine, open_long, open_short, position


def _code(exc_info) -> int:
    return exc_info.value.code


def test_market_open_long_fills_at_last_price_with_commission():
    engine, feed, _, clock = make_engine()
    order = open_long(engine, qty="0.01")
    assert order["status"] == "FILLED" and order["raw_status"] == "FILLED"
    assert order["filled_qty"] == "0.01" and order["avg_price"] == "50000"
    assert order["client_order_id"] == "DKA1-1-1-o" and order["type"] == "MARKET"
    assert order["side"] == "BUY" and order["position_side"] == "LONG"
    assert order["close_position"] is False and order["reduce_only"] is False
    assert order["update_time_ms"] == clock()
    pos = position(engine, "BTCUSDT", "LONG")
    assert pos["qty"] == "0.01" and pos["entry_price"] == "50000"
    assert pos["leverage"] == 20 and pos["margin_mode"] == "cross"  # Binance 新合约默认
    acct = engine.account()
    assert D(acct["wallet_balance"]) == D("1000") - D("0.25")  # 500 × 0.05%
    trades = engine.trades(symbol="BTCUSDT")
    assert len(trades) == 1
    t = trades[0]
    assert t["order_id"] == order["order_id"] and D(t["commission"]) == D("0.25")
    assert t["commission_asset"] == "USDT" and D(t["realized_pnl"]) == 0 and t["maker"] is False


def test_add_to_long_uses_weighted_average_price():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    feed.set_price("BTCUSDT", "52000")
    open_long(engine, qty="0.01", cid="DKA1-1-3-o")
    pos = position(engine, "BTCUSDT", "LONG")
    assert pos["qty"] == "0.02" and D(pos["entry_price"]) == D("51000")


def test_reduce_long_realizes_pnl_and_pays_fee_into_wallet():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    feed.set_price("BTCUSDT", "52000")
    open_long(engine, qty="0.01", cid="DKA1-1-3-o")
    feed.set_price("BTCUSDT", "53000")
    order = engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                                      client_order_id="DKA1-0-4-p")
    assert order["status"] == "FILLED"
    pos = position(engine, "BTCUSDT", "LONG")
    assert pos["qty"] == "0.01" and D(pos["entry_price"]) == D("51000")  # 减仓不改均价
    trade = engine.trades(symbol="BTCUSDT")[-1]
    assert D(trade["realized_pnl"]) == D("20")  # (53000 − 51000) × 0.01
    assert D(trade["commission"]) == D("0.265")
    wallet = D(engine.account()["wallet_balance"])
    assert wallet == D("1000") - D("0.25") - D("0.26") + D("20") - D("0.265")
    kinds = [(i["type"], D(i["amount"])) for i in engine.income()]
    assert ("REALIZED_PNL", D("20")) in kinds
    assert ("COMMISSION", D("-0.265")) in kinds


def test_close_short_realizes_reversed_pnl_and_flattens():
    engine, feed, _, _ = make_engine()
    open_short(engine, qty="0.02")
    feed.set_price("BTCUSDT", "49000")
    order = engine.place_market_order(symbol="BTCUSDT", side="BUY", position_side="SHORT", qty=D("0.02"),
                                      client_order_id="DKA1-0-5-c")
    assert order["status"] == "FILLED"
    assert position(engine, "BTCUSDT", "SHORT") is None  # 只列数量非零的
    trade = engine.trades(symbol="BTCUSDT")[-1]
    assert D(trade["realized_pnl"]) == D("20")  # (50000 − 49000) × 0.02
    assert D(trade["commission"]) == D("0.49")


def test_hedge_sides_are_independent():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    feed.set_price("BTCUSDT", "51000")
    open_short(engine, qty="0.03")
    engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                              client_order_id="DKA1-0-6-c")
    assert position(engine, "BTCUSDT", "LONG") is None
    short = position(engine, "BTCUSDT", "SHORT")
    assert short["qty"] == "0.03" and D(short["entry_price"]) == D("51000")


def test_unrealized_pnl_uses_mark_and_equity_is_wallet_plus_upnl():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    open_short(engine, symbol="ETHUSDT", qty="0.1", cid="DKA1-1-7-o")
    feed.set_price("BTCUSDT", "50500", mark="51000")
    feed.set_price("ETHUSDT", "3100", mark="3050")
    long_ = position(engine, "BTCUSDT", "LONG")
    short = position(engine, "ETHUSDT", "SHORT")
    assert D(long_["unrealized_pnl"]) == D("10")  # (51000 − 50000) × 0.01
    assert D(short["unrealized_pnl"]) == D("-5")  # (3000 − 3050) × 0.1
    assert long_["mark_price"] == "51000"
    acct = engine.account()
    assert D(acct["unrealized_pnl"]) == D("5")
    assert D(acct["equity"]) == D(acct["wallet_balance"]) + D("5")
    assert D(acct["available_balance"]) < D(acct["equity"])


def test_liquidation_price_simplified_formula():
    engine, _, _, _ = make_engine()
    open_long(engine, qty="0.01")
    open_short(engine, qty="0.01")
    long_ = position(engine, "BTCUSDT", "LONG")
    short = position(engine, "BTCUSDT", "SHORT")
    # 多：均价 × (1 − 1/20 + 0.5%)；空：均价 × (1 + 1/20 − 0.5%)
    assert D(long_["liquidation_price"]) == D("47750")
    assert D(short["liquidation_price"]) == D("52250")


def test_reduce_without_position_is_2022():
    engine, _, _, _ = make_engine()
    with pytest.raises(EngineError) as e:
        engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                                  client_order_id="DKA1-0-8-c")
    assert _code(e) == -2022
    assert engine.trades(symbol="BTCUSDT") == []


def test_close_qty_exceeds_reject_by_default():
    engine, _, _, _ = make_engine()
    open_long(engine, qty="0.01")
    with pytest.raises(EngineError) as e:
        engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.02"),
                                  client_order_id="DKA1-0-9-c")
    assert _code(e) == -2022
    assert position(engine, "BTCUSDT", "LONG")["qty"] == "0.01"


def test_close_qty_exceeds_truncate_closes_only_the_position():
    engine, _, _, _ = make_engine(close_qty_exceeds="truncate")
    open_long(engine, qty="0.01")
    order = engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.02"),
                                      client_order_id="DKA1-0-9-c")
    assert order["status"] == "FILLED" and order["filled_qty"] == "0.01"
    assert position(engine, "BTCUSDT", "LONG") is None
    assert position(engine, "BTCUSDT", "SHORT") is None  # 不会开出反向仓


def test_open_below_min_notional_is_4164():
    engine, _, _, _ = make_engine()
    with pytest.raises(EngineError) as e:
        open_long(engine, qty="0.001")  # 50 USDT < 100
    assert _code(e) == -4164
    assert engine.positions() == []


def test_dust_close_below_min_notional_rejected_when_not_exempt():
    engine, _, _, _ = make_engine()
    open_long(engine, qty="0.002")  # 100 USDT
    with pytest.raises(EngineError) as e:
        engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.001"),
                                  client_order_id="DKA1-0-10-p")
    assert _code(e) == -4164
    assert position(engine, "BTCUSDT", "LONG")["qty"] == "0.002"


def test_dust_close_accepted_when_exempt():
    engine, _, _, _ = make_engine(close_min_notional_exempt=True)
    open_long(engine, qty="0.002")
    order = engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.001"),
                                      client_order_id="DKA1-0-10-p")
    assert order["status"] == "FILLED"
    assert position(engine, "BTCUSDT", "LONG")["qty"] == "0.001"


def test_insufficient_margin_is_2019():
    engine, _, _, _ = make_engine(equity="100")
    with pytest.raises(EngineError) as e:
        open_long(engine, qty="0.05")  # 2500 USDT ÷ 20 = 125 > 100
    assert _code(e) == -2019
    open_long(engine, qty="0.03")  # 1500 ÷ 20 = 75：够
    with pytest.raises(EngineError) as e2:
        open_short(engine, qty="0.01")  # 剩余可用不足 25 + 手续费
    assert _code(e2) == -2019


def test_leverage_changes_required_margin():
    engine, _, _, _ = make_engine(equity="100")
    engine.set_leverage("BTCUSDT", 1)
    with pytest.raises(EngineError) as e:
        open_long(engine, qty="0.002")  # 100 USDT ÷ 1 + 手续费 > 100
    assert _code(e) == -2019
    assert engine.leverage_info("BTCUSDT") == {"symbol": "BTCUSDT", "leverage": 1, "margin_mode": "cross"}


@pytest.mark.parametrize("qty, code", [("0.0105", -1111), ("0", -1102), ("-0.01", -1102)])
def test_quantity_must_be_positive_step_multiple(qty, code):
    engine, _, _, _ = make_engine()
    with pytest.raises(EngineError) as e:
        open_long(engine, qty=qty)
    assert _code(e) == code


def test_unknown_symbol_and_bad_client_id():
    engine, _, _, _ = make_engine()
    with pytest.raises(EngineError) as e:
        open_long(engine, symbol="NOPEUSDT")
    assert _code(e) == -1121
    with pytest.raises(EngineError) as e2:
        open_long(engine, cid="bad id with spaces")
    assert _code(e2) == -4015
    with pytest.raises(EngineError) as e3:
        open_long(engine, cid="x" * 37)
    assert _code(e3) == -4015


def test_missing_client_id_gets_exchange_generated_one():
    engine, _, _, _ = make_engine()
    order = engine.place_market_order(symbol="BTCUSDT", side="BUY", position_side="LONG", qty=D("0.01"),
                                      client_order_id=None)
    assert order["client_order_id"] and len(order["client_order_id"]) <= 36


def test_duplicate_client_id_after_fill_accepted_by_default():
    engine, _, _, _ = make_engine()
    first = open_long(engine, qty="0.01", cid="DKA1-1-11-o")
    second = open_long(engine, qty="0.01", cid="DKA1-1-11-o")
    assert second["order_id"] != first["order_id"]
    assert position(engine, "BTCUSDT", "LONG")["qty"] == "0.02"


def test_duplicate_client_id_after_fill_rejected_when_switched():
    engine, _, _, _ = make_engine(duplicate_client_id_after_fill="reject")
    open_long(engine, qty="0.01", cid="DKA1-1-11-o")
    with pytest.raises(EngineError) as e:
        open_long(engine, qty="0.01", cid="DKA1-1-11-o")
    assert _code(e) == -4116
    assert position(engine, "BTCUSDT", "LONG")["qty"] == "0.01"


def test_order_and_trade_ids_are_distinct_strings():
    engine, _, _, _ = make_engine()
    a = open_long(engine, qty="0.01")
    b = open_short(engine, qty="0.01")
    assert isinstance(a["order_id"], str) and a["order_id"] != b["order_id"]
    trades = engine.trades(symbol="BTCUSDT")
    assert len({t["trade_id"] for t in trades}) == 2
    assert all(isinstance(t["trade_id"], str) for t in trades)


def test_no_price_means_order_rejected_not_unknown():
    engine, feed, _, _ = make_engine(prices={"BTCUSDT": "50000"})
    with pytest.raises(EngineError) as e:
        open_long(engine, symbol="ETHUSDT", qty="0.1")
    assert e.value.http_status == 400
