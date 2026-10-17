"""条件单（docs/设计约定.md 7.6）：触发方向、触发价类型、全平型、建单越价、同向两张、数量上限、
持仓变小、仓位归零后自动撤销、触发后订单号关联。测试直接调 check_triggers()，不等真实时间。"""
from __future__ import annotations

import pytest

from dmr_standin.engine import EngineError
from standin_kit import D, make_engine, open_long, open_short, position, stop


def test_long_stop_triggers_only_when_price_at_or_below_trigger():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.02")
    algo = stop(engine, trigger="49000")
    assert algo["status"] == "NEW" and algo["qty"] is None and algo["close_position"] is True
    feed.set_price("BTCUSDT", "49000.1")
    assert engine.check_triggers() == []
    assert position(engine, "BTCUSDT", "LONG")["qty"] == "0.02"
    feed.set_price("BTCUSDT", "49000")
    assert engine.check_triggers() == [algo["algo_id"]]
    assert position(engine, "BTCUSDT", "LONG") is None
    done = engine.get_algo_order(symbol="BTCUSDT", algo_id=algo["algo_id"])
    assert done["status"] == "FINISHED"
    assert engine.open_algo_orders() == []


def test_short_stop_triggers_only_when_price_at_or_above_trigger():
    engine, feed, _, _ = make_engine()
    open_short(engine, qty="0.02")
    algo = stop(engine, side="BUY", position_side="SHORT", trigger="51000", cid="DKA1-0-2-s")
    feed.set_price("BTCUSDT", "50999.9")
    assert engine.check_triggers() == []
    feed.set_price("BTCUSDT", "51500")
    assert engine.check_triggers() == [algo["algo_id"]]
    assert position(engine, "BTCUSDT", "SHORT") is None
    fill = engine.trades(symbol="BTCUSDT")[-1]
    assert fill["side"] == "BUY" and fill["position_side"] == "SHORT" and fill["price"] == "51500"


def test_take_profit_triggers_in_the_opposite_direction():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    tp = stop(engine, trigger="52000", algo_type="TAKE_PROFIT_MARKET", cid="DKA1-0-3-t")
    feed.set_price("BTCUSDT", "40000")
    assert engine.check_triggers() == []
    feed.set_price("BTCUSDT", "52000")
    assert engine.check_triggers() == [tp["algo_id"]]


def test_mark_price_stop_watches_mark_not_last():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    algo = stop(engine, trigger="49000", working_type="MARK_PRICE")
    assert algo["working_type"] == "MARK_PRICE"
    feed.set_price("BTCUSDT", "48000", mark="49500")
    assert engine.check_triggers() == []
    feed.set_price("BTCUSDT", "49800", mark="48900")
    assert engine.check_triggers() == [algo["algo_id"]]
    # 触发后按最新价成交
    assert engine.trades(symbol="BTCUSDT")[-1]["price"] == "49800"


def test_contract_price_stop_ignores_mark():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    stop(engine, trigger="49000")
    feed.set_price("BTCUSDT", "49500", mark="48000")
    assert engine.check_triggers() == []


def test_close_position_stop_closes_entire_side_only():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    open_long(engine, qty="0.02", cid="DKA1-1-4-o")
    open_short(engine, qty="0.01")
    stop(engine, trigger="49000")
    feed.set_price("BTCUSDT", "48000")
    engine.check_triggers()
    assert position(engine, "BTCUSDT", "LONG") is None
    assert position(engine, "BTCUSDT", "SHORT")["qty"] == "0.01"
    trade = engine.trades(symbol="BTCUSDT")[-1]
    assert trade["qty"] == "0.03" and trade["side"] == "SELL"


def test_stop_crossed_on_create_rejected_with_2021_by_default():
    engine, _, _, _ = make_engine()
    open_long(engine, qty="0.01")
    with pytest.raises(EngineError) as e:
        stop(engine, trigger="50000")  # 现价 50000，多仓止损 ≥ 现价即已越过
    assert e.value.code == -2021
    assert engine.open_algo_orders() == []
    assert position(engine, "BTCUSDT", "LONG")["qty"] == "0.01"


def test_stop_crossed_on_create_triggers_immediately_when_switched():
    engine, _, _, _ = make_engine(stop_crossed_on_create="trigger_immediately")
    open_long(engine, qty="0.01")
    algo = stop(engine, trigger="50500")
    assert algo["status"] == "NEW"  # 回执是受理时的快照
    assert position(engine, "BTCUSDT", "LONG") is None  # 但已立即触发并平仓
    assert engine.get_algo_order(symbol="BTCUSDT", algo_id=algo["algo_id"])["status"] == "FINISHED"


def test_second_close_position_stop_same_side_rejected_4130_by_default():
    engine, _, _, _ = make_engine()
    open_long(engine, qty="0.01")
    stop(engine, trigger="49000", cid="DKA1-0-1-s")
    with pytest.raises(EngineError) as e:
        stop(engine, trigger="48000", cid="DKA1-0-1-s1")
    assert e.value.code == -4130
    # 另一侧、另一种类型（止盈）不受影响
    open_short(engine, qty="0.01")
    stop(engine, side="BUY", position_side="SHORT", trigger="51000", cid="DKA1-0-2-s")
    stop(engine, trigger="52000", algo_type="TAKE_PROFIT_MARKET", cid="DKA1-0-3-t")
    assert len(engine.open_algo_orders(symbol="BTCUSDT")) == 3


def test_second_close_position_stop_allowed_when_switched():
    engine, _, _, _ = make_engine(dual_close_position_stop="allow")
    open_long(engine, qty="0.01")
    stop(engine, trigger="49000", cid="DKA1-0-1-s")
    stop(engine, trigger="48000", cid="DKA1-0-1-s1")
    assert len(engine.open_algo_orders(symbol="BTCUSDT")) == 2


def _qty_stops(engine, symbol: str, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        stop(engine, symbol=symbol, trigger="1000" if symbol == "ETHUSDT" else "40000", close_position=False,
             qty="0.001", cid=f"DKA1-0-{i:x}-s")


def test_conditional_limit_per_symbol_10_by_default():
    engine, _, _, _ = make_engine()
    _qty_stops(engine, "BTCUSDT", 10)
    with pytest.raises(EngineError) as e:
        _qty_stops(engine, "BTCUSDT", 1, start=10)
    assert e.value.code == -4045
    _qty_stops(engine, "ETHUSDT", 1, start=20)  # 别的合约不受影响


def test_conditional_limit_account_200_when_switched():
    engine, _, _, _ = make_engine(conditional_limit="account_200")
    _qty_stops(engine, "BTCUSDT", 150)
    _qty_stops(engine, "ETHUSDT", 50, start=150)
    with pytest.raises(EngineError) as e:
        _qty_stops(engine, "ETHUSDT", 1, start=200)
    assert e.value.code == -4045


def test_qty_stop_after_shrink_rejected_by_default():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.03")
    algo = stop(engine, trigger="49000", close_position=False, qty="0.03")
    engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                              client_order_id="DKA1-0-5-p")
    feed.set_price("BTCUSDT", "48000")
    assert engine.check_triggers() == [algo["algo_id"]]
    assert position(engine, "BTCUSDT", "LONG")["qty"] == "0.02"  # 被拒，持仓不变
    assert engine.get_algo_order(symbol="BTCUSDT", algo_id=algo["algo_id"])["status"] == "REJECTED"


def test_qty_stop_after_shrink_closes_remaining_when_switched():
    engine, feed, _, _ = make_engine(qty_stop_after_shrink="close_remaining")
    open_long(engine, qty="0.03")
    algo = stop(engine, trigger="49000", close_position=False, qty="0.03")
    engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                              client_order_id="DKA1-0-5-p")
    feed.set_price("BTCUSDT", "48000")
    engine.check_triggers()
    assert position(engine, "BTCUSDT", "LONG") is None
    assert position(engine, "BTCUSDT", "SHORT") is None  # 不会开出反向仓
    assert engine.get_algo_order(symbol="BTCUSDT", algo_id=algo["algo_id"])["status"] == "FINISHED"


def test_qty_stop_smaller_than_position_closes_its_qty():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.03")
    stop(engine, trigger="49000", close_position=False, qty="0.01")
    feed.set_price("BTCUSDT", "48000")
    engine.check_triggers()
    assert position(engine, "BTCUSDT", "LONG")["qty"] == "0.02"


def test_close_position_stop_survives_flat_by_default_and_expires_on_trigger():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    algo = stop(engine, trigger="49000")
    engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                              client_order_id="DKA1-0-6-c")
    assert [a["algo_id"] for a in engine.open_algo_orders()] == [algo["algo_id"]]  # 仍挂着
    feed.set_price("BTCUSDT", "48000")
    assert engine.check_triggers() == [algo["algo_id"]]
    assert engine.get_algo_order(symbol="BTCUSDT", algo_id=algo["algo_id"])["status"] == "EXPIRED"  # 该侧已无仓
    assert engine.positions() == []


def test_residual_close_position_stop_acts_on_a_later_position():
    """不自动撤时，残留的全平型止损会作用到之后同方向的新仓上（实测第 15 项要防的情形）。"""
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    algo = stop(engine, trigger="49000")
    engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                              client_order_id="DKA1-0-6-c")
    open_long(engine, qty="0.02", cid="DKA1-1-7-o")
    feed.set_price("BTCUSDT", "48000")
    assert engine.check_triggers() == [algo["algo_id"]]
    assert position(engine, "BTCUSDT", "LONG") is None


def test_close_position_stop_autocancelled_on_flat_when_switched():
    engine, _, _, _ = make_engine(close_position_stop_autocancel_on_flat=True)
    open_long(engine, qty="0.01")
    algo = stop(engine, trigger="49000")
    qty_algo = stop(engine, trigger="48000", close_position=False, qty="0.01", cid="DKA1-0-9-s")
    engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                              client_order_id="DKA1-0-6-c")
    assert engine.get_algo_order(symbol="BTCUSDT", algo_id=algo["algo_id"])["status"] == "CANCELED"
    # 只撤全平型；数量型不动
    assert [a["algo_id"] for a in engine.open_algo_orders()] == [qty_algo["algo_id"]]


def test_triggered_order_link_unavailable_by_default():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    algo = stop(engine, trigger="49000", cid="DKA1-0-1-s")
    feed.set_price("BTCUSDT", "48000")
    engine.check_triggers()
    done = engine.get_algo_order(symbol="BTCUSDT", algo_id=algo["algo_id"])
    assert done["actual_order_id"] is None
    trade = engine.trades(symbol="BTCUSDT")[-1]
    order = engine.get_order(symbol="BTCUSDT", order_id=trade["order_id"])
    assert order["client_order_id"] != "DKA1-0-1-s"  # 交易所随机生成
    assert not order["client_order_id"].startswith("DK")
    assert order["close_position"] is True and order["status"] == "FILLED"


def test_triggered_order_link_available_when_switched():
    engine, feed, _, _ = make_engine(triggered_order_link="available")
    open_long(engine, qty="0.01")
    algo = stop(engine, trigger="49000", cid="DKA1-0-1-s")
    feed.set_price("BTCUSDT", "48000")
    engine.check_triggers()
    done = engine.get_algo_order(symbol="BTCUSDT", algo_id=algo["algo_id"])
    trade = engine.trades(symbol="BTCUSDT")[-1]
    assert done["actual_order_id"] == trade["order_id"]
    order = engine.get_order(symbol="BTCUSDT", order_id=trade["order_id"])
    assert order["client_order_id"] == "DKA1-0-1-s"


@pytest.mark.parametrize("kwargs, code", [
    ({"close_position": True, "qty": "0.01"}, -1102),  # 全平型不得带数量
    ({"close_position": False, "qty": None}, -1102),  # 数量型必须带数量
    ({"trigger": "49000.05"}, -1111),  # 不是价格步长的整数倍
    ({"working_type": "INDEX_PRICE"}, -1102),
    ({"algo_type": "LIMIT"}, -1102),
    ({"side": "BUY"}, -1102),  # 多仓的全平型条件单必须是 SELL
])
def test_algo_parameter_validation(kwargs, code):
    engine, _, _, _ = make_engine()
    with pytest.raises(EngineError) as e:
        stop(engine, **kwargs)
    assert e.value.code == code


def test_duplicate_open_client_algo_id_is_4116_but_reusable_after_finish():
    engine, feed, _, _ = make_engine(dual_close_position_stop="allow")
    open_long(engine, qty="0.01")
    first = stop(engine, trigger="49000", cid="DKA1-0-1-s")
    with pytest.raises(EngineError) as e:
        stop(engine, trigger="48000", cid="DKA1-0-1-s")
    assert e.value.code == -4116
    engine.cancel_algo_order(symbol="BTCUSDT", algo_id=first["algo_id"])
    again = stop(engine, trigger="48000", cid="DKA1-0-1-s")
    assert again["algo_id"] != first["algo_id"]


def test_check_triggers_returns_nothing_without_algos():
    engine, _, _, _ = make_engine()
    assert engine.check_triggers() == []
