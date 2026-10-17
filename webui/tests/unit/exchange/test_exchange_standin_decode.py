"""exchange.standin 的解码函数：把替身协议（设计约定 7.2）里的 JSON 解析成 core.types 数据类。

字符串数值 -> Decimal；缺字段、坏数值、未知枚举统一抛 DecodeError（上层把它变成 Unknown / WriteUnknown，绝不当成空）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core import types as t
from dmr_kernel.exchange import standin as sd

D = Decimal

ORDER = {"symbol": "BTCUSDT", "client_order_id": "DKA1-1-1-o", "order_id": "9001", "side": "BUY",
         "position_side": "LONG", "type": "MARKET", "qty": "0.010", "filled_qty": "0.010", "avg_price": "82874.6",
         "status": "FILLED", "raw_status": "FILLED", "close_position": False, "reduce_only": False,
         "update_time_ms": 1_791_943_200_123}
ALGO = {"symbol": "BTCUSDT", "client_algo_id": "DKA1-0-1-s", "algo_id": "7001", "side": "SELL", "position_side": "LONG",
        "type": "STOP_MARKET", "trigger_price": "80000.0", "working_type": "MARK_PRICE", "close_position": True,
        "qty": None, "status": "NEW", "actual_order_id": None, "create_time_ms": 10, "update_time_ms": 20}
TRADE = {"trade_id": "5001", "order_id": "9001", "symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
         "qty": "0.010", "price": "82874.6", "commission": "0.41437", "commission_asset": "USDT",
         "realized_pnl": "0", "time_ms": 1_791_943_200_123, "maker": False}
POSITION = {"symbol": "BTCUSDT", "position_side": "LONG", "qty": "0.010", "entry_price": "82874.6",
            "mark_price": "82900", "liquidation_price": "78800.5", "unrealized_pnl": "0.254", "leverage": 20,
            "margin_mode": "cross", "update_time_ms": 77}
INSTRUMENT = {"symbol": "哈基米USDT", "base": "哈基米", "quote": "USDT", "contract_type": "PERPETUAL",
              "status": "TRADING", "tick_size": "0.00001", "step_size": "1", "min_qty": "1", "min_notional": "5",
              "market_step_size": "1", "market_max_qty": "2000000", "onboard_ms": 1_700_000_000_000}


def test_decode_order_parses_every_field():
    o = sd.decode_order(ORDER)
    assert o == t.OrderInfo(symbol="BTCUSDT", client_order_id="DKA1-1-1-o", exchange_order_id="9001",
                            side=t.OrderSide.BUY, position_side=t.PositionSide.LONG, order_type="MARKET",
                            qty=D("0.010"), filled_qty=D("0.010"), avg_price=D("82874.6"), status="FILLED",
                            raw_status="FILLED", close_position=False, reduce_only=False,
                            update_time_ms=1_791_943_200_123)
    assert isinstance(o.qty, Decimal) and isinstance(o.exchange_order_id, str)


def test_decode_order_unfilled_has_no_average_price():
    o = sd.decode_order({**ORDER, "filled_qty": "0", "avg_price": "0", "status": "NEW", "raw_status": "NEW"})
    assert o.avg_price is None and o.filled_qty == D("0")


def test_decode_order_keeps_the_raw_status_apart_from_the_normalised_one():
    o = sd.decode_order({**ORDER, "status": "EXPIRED", "raw_status": "EXPIRED_IN_MATCH"})
    assert (o.status, o.raw_status) == ("EXPIRED", "EXPIRED_IN_MATCH")


def test_decode_order_without_a_client_id_is_allowed_but_ids_are_strings():
    o = sd.decode_order({**ORDER, "client_order_id": None, "order_id": 9002})
    assert o.client_order_id is None and o.exchange_order_id == "9002"


def test_decode_algo():
    a = sd.decode_algo(ALGO)
    assert a == t.AlgoOrderInfo(symbol="BTCUSDT", client_algo_id="DKA1-0-1-s", algo_id="7001", side=t.OrderSide.SELL,
                                position_side=t.PositionSide.LONG, algo_type=t.AlgoType.STOP_MARKET,
                                trigger_price=D("80000.0"), working_type=t.WorkingType.MARK_PRICE,
                                close_position=True, qty=None, raw_status="NEW", actual_order_id=None,
                                create_time_ms=10, update_time_ms=20)
    assert a.is_open


def test_decode_algo_with_quantity_and_actual_order_id():
    a = sd.decode_algo({**ALGO, "close_position": False, "qty": "0.5", "status": "FINISHED",
                        "actual_order_id": 8123})
    assert a.qty == D("0.5") and a.actual_order_id == "8123" and not a.is_open


def test_decode_trade_carries_no_client_id():
    tr = sd.decode_trade(TRADE)
    assert tr == t.TradeInfo(trade_id="5001", exchange_order_id="9001", symbol="BTCUSDT", side=t.OrderSide.BUY,
                             position_side=t.PositionSide.LONG, qty=D("0.010"), price=D("82874.6"),
                             commission=D("0.41437"), commission_asset="USDT", realized_pnl=D("0"),
                             time_ms=1_791_943_200_123, maker=False)
    assert not hasattr(tr, "client_order_id")


def test_decode_position():
    p = sd.decode_position(POSITION)
    assert p == t.PositionInfo(symbol="BTCUSDT", side=t.PositionSide.LONG, qty=D("0.010"), entry_price=D("82874.6"),
                               mark_price=D("82900"), liquidation_price=D("78800.5"), unrealized_pnl=D("0.254"),
                               leverage=20, margin_mode="cross", update_time_ms=77)


def test_decode_position_zero_liquidation_price_means_none_and_qty_is_absolute():
    p = sd.decode_position({**POSITION, "liquidation_price": "0", "qty": "-0.5"})
    assert p.liquidation_price is None and p.qty == D("0.5")
    flat = sd.decode_position({**POSITION, "qty": "0", "liquidation_price": "0"})
    assert flat.qty == 0


def test_decode_account_and_income_and_leverage():
    acct = sd.decode_account({"equity": "1000.5", "wallet_balance": "1000", "available_balance": "900",
                              "unrealized_pnl": "0.5", "update_time_ms": 5})
    assert acct == t.AccountInfo(equity=D("1000.5"), wallet_balance=D("1000"), available_balance=D("900"),
                                 unrealized_pnl=D("0.5"), update_time_ms=5)
    inc = sd.decode_income({"tran_id": "31", "type": "FUNDING_FEE", "symbol": "BTCUSDT", "amount": "-0.04",
                            "asset": "USDT", "time_ms": 9, "info": "funding"})
    assert inc == t.IncomeInfo(income_id="31", income_type="FUNDING_FEE", symbol="BTCUSDT", amount=D("-0.04"),
                               asset="USDT", time_ms=9, info="funding")
    assert sd.decode_income({"tran_id": 1, "type": "COMMISSION", "symbol": None, "amount": "0", "asset": "USDT",
                             "time_ms": 1}).symbol is None
    assert sd.decode_leverage({"symbol": "ETHUSDT", "leverage": 20, "margin_mode": "isolated"}) == \
        t.LeverageInfo("ETHUSDT", 20, "isolated")


def test_decode_instrument_keeps_chinese_names_and_optional_fields():
    i = sd.decode_instrument(INSTRUMENT)
    assert i.symbol == "哈基米USDT" and i.base == "哈基米" and i.tradable
    assert i.tick_size == D("0.00001") and i.market_max_qty == D("2000000") and i.onboard_ms == 1_700_000_000_000
    j = sd.decode_instrument({**INSTRUMENT, "market_step_size": None, "market_max_qty": None, "onboard_ms": None})
    assert (j.market_step_size, j.market_max_qty, j.onboard_ms) == (None, None, None)


def test_decode_ticker_and_candle():
    tk = sd.decode_ticker({"symbol": "BTCUSDT", "last": "82874.6", "mark": "82870.1", "time_ms": 3})
    assert tk == t.Ticker("BTCUSDT", D("82874.6"), D("82870.1"), 3)
    c = sd.decode_candle([1_000, "1.5", "2.5", "1.0", "2.0", "300", 1_899])
    assert c == t.Candle(1_000, 1.5, 2.5, 1.0, 2.0, 300.0, 1_899)
    assert isinstance(c.open, float)


def test_decode_restrictions():
    r = sd.decode_restrictions({"restricted": True, "detail": {"rule": "量化规则", "level": 2}})
    assert r.restricted is True and dict(r.detail) == {"rule": "量化规则", "level": "2"}
    assert sd.decode_restrictions({"restricted": False, "detail": {}}) == t.RestrictionInfo(False, ())


@pytest.mark.parametrize("decode,sample,required", [
    (sd.decode_order, ORDER, ("symbol", "qty", "filled_qty", "side", "position_side", "order_id", "status", "raw_status")),
    (sd.decode_algo, ALGO, ("symbol", "side", "trigger_price", "working_type", "close_position", "algo_id", "status")),
    (sd.decode_trade, TRADE, ("symbol", "qty", "price", "side", "order_id", "trade_id", "commission", "time_ms")),
    (sd.decode_position, POSITION, ("symbol", "qty", "entry_price", "position_side")),
    (sd.decode_instrument, INSTRUMENT, ("symbol", "tick_size", "step_size", "min_qty", "min_notional", "status")),
])
def test_missing_required_fields_raise_decode_error(decode, sample, required):
    assert decode(sample)  # 完整的样例本身可以解析
    for key in required:
        broken = {k: v for k, v in sample.items() if k != key}
        with pytest.raises(sd.DecodeError):
            decode(broken)
        with pytest.raises(sd.DecodeError):
            decode({**sample, key: None})


def test_garbage_values_raise_decode_error():
    with pytest.raises(sd.DecodeError):
        sd.decode_order({**ORDER, "qty": "abc"})
    with pytest.raises(sd.DecodeError):
        sd.decode_order({**ORDER, "qty": True})
    with pytest.raises(sd.DecodeError):
        sd.decode_order({**ORDER, "qty": "NaN"})
    with pytest.raises(sd.DecodeError):
        sd.decode_order({**ORDER, "qty": "Infinity"})
    with pytest.raises(sd.DecodeError):
        sd.decode_order({**ORDER, "side": "HOLD"})
    with pytest.raises(sd.DecodeError):
        sd.decode_order({**ORDER, "update_time_ms": "yesterday"})
    with pytest.raises(sd.DecodeError):
        sd.decode_algo({**ALGO, "working_type": "LAST"})
    with pytest.raises(sd.DecodeError):
        sd.decode_position({**POSITION, "position_side": "BOTH"})
    with pytest.raises(sd.DecodeError):
        sd.decode_order("not a dict")
    with pytest.raises(sd.DecodeError):
        sd.decode_order(None)
    with pytest.raises(sd.DecodeError):
        sd.decode_candle([1, 2])
    with pytest.raises(sd.DecodeError):
        sd.decode_candle("nope")


def test_decode_error_is_a_value_error():
    assert issubclass(sd.DecodeError, ValueError)


# ================================================================ 数据流帧（设计约定 7.3）

ORDER_UPDATE = {"type": "order_update", "event_time_ms": 1_791_943_200_200, "execution": "TRADE", "order": ORDER,
                "fill": TRADE}
ALGO_UPDATE = {"type": "algo_update", "event_time_ms": 1_791_943_200_300, "algo": ALGO}
ACCOUNT_UPDATE = {"type": "account_update", "event_time_ms": 1_791_943_200_400, "reason": "ORDER",
                  "positions": [POSITION, {**POSITION, "symbol": "ETHUSDT", "position_side": "SHORT", "qty": "0",
                                           "entry_price": "0", "liquidation_price": "0"}],
                  "balances": [{"asset": "USDT", "wallet_balance": "1000.50"}]}


def test_decode_order_update_with_fill():
    u = sd.decode_order_update(ORDER_UPDATE)
    assert u == t.StreamOrder(order=sd.decode_order(ORDER), execution="TRADE", fill=sd.decode_trade(TRADE),
                              event_time_ms=1_791_943_200_200)


def test_decode_order_update_without_fill_keeps_none():
    u = sd.decode_order_update({**ORDER_UPDATE, "execution": "NEW", "fill": None})
    assert u.fill is None and u.execution == "NEW"
    u = sd.decode_order_update({k: v for k, v in ORDER_UPDATE.items() if k != "fill"})
    assert u.fill is None


def test_decode_algo_update():
    u = sd.decode_algo_update(ALGO_UPDATE)
    assert u == t.StreamAlgo(algo=sd.decode_algo(ALGO), event_time_ms=1_791_943_200_300)


def test_decode_account_update_keeps_flat_positions_and_balances_as_decimals():
    u = sd.decode_account_update(ACCOUNT_UPDATE)
    assert u.reason == "ORDER" and u.event_time_ms == 1_791_943_200_400
    assert [(p.symbol, p.side, p.qty) for p in u.positions] == [
        ("BTCUSDT", t.PositionSide.LONG, D("0.010")), ("ETHUSDT", t.PositionSide.SHORT, D("0"))]
    assert u.positions[1].liquidation_price is None
    assert u.balances == (("USDT", D("1000.50")),)
    assert isinstance(u.positions, tuple) and isinstance(u.balances, tuple)


def test_decode_account_update_allows_no_positions_or_balances():
    u = sd.decode_account_update({"type": "account_update", "event_time_ms": 5, "reason": "FUNDING_FEE",
                                  "positions": [], "balances": []})
    assert u.positions == () and u.balances == ()
    u = sd.decode_account_update({"type": "account_update", "event_time_ms": 5, "reason": "FUNDING_FEE",
                                  "positions": []})
    assert u.balances == ()


@pytest.mark.parametrize("decode, sample, required", [
    (sd.decode_order_update, ORDER_UPDATE, ("execution", "order", "event_time_ms")),
    (sd.decode_algo_update, ALGO_UPDATE, ("algo", "event_time_ms")),
    (sd.decode_account_update, ACCOUNT_UPDATE, ("reason", "positions", "event_time_ms")),
])
def test_frame_missing_required_fields_raise_decode_error(decode, sample, required):
    assert decode(sample)
    for key in required:
        with pytest.raises(sd.DecodeError):
            decode({k: v for k, v in sample.items() if k != key})
        with pytest.raises(sd.DecodeError):
            decode({**sample, key: None})


def test_frame_with_a_broken_inner_record_raises_decode_error():
    with pytest.raises(sd.DecodeError):
        sd.decode_order_update({**ORDER_UPDATE, "order": {**ORDER, "qty": "abc"}})
    with pytest.raises(sd.DecodeError):
        sd.decode_order_update({**ORDER_UPDATE, "fill": {**TRADE, "price": None}})
    with pytest.raises(sd.DecodeError):
        sd.decode_algo_update({**ALGO_UPDATE, "algo": {**ALGO, "type": "TRAILING"}})
    with pytest.raises(sd.DecodeError):
        sd.decode_account_update({**ACCOUNT_UPDATE, "positions": [POSITION, "x"]})
    with pytest.raises(sd.DecodeError):
        sd.decode_account_update({**ACCOUNT_UPDATE, "positions": {"a": 1}})
    with pytest.raises(sd.DecodeError):
        sd.decode_account_update({**ACCOUNT_UPDATE, "balances": [{"asset": "USDT"}]})
    with pytest.raises(sd.DecodeError):
        sd.decode_account_update({**ACCOUNT_UPDATE, "balances": [{"asset": "USDT", "wallet_balance": "NaN"}]})
    with pytest.raises(sd.DecodeError):
        sd.decode_account_update({**ACCOUNT_UPDATE, "balances": "USDT"})
    for decode in (sd.decode_order_update, sd.decode_algo_update, sd.decode_account_update):
        with pytest.raises(sd.DecodeError):
            decode(["not", "an", "object"])
