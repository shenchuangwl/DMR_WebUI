"""查询、撤单、杠杆与保证金模式、资金费（docs/设计约定.md 7.2、7.6）。"""
from __future__ import annotations

import pytest

from dmr_standin.engine import EngineError, PartialFill
from standin_kit import D, T0, make_engine, open_long, open_short, position, stop


def test_get_order_by_client_id_and_by_order_id():
    engine, _, _, _ = make_engine()
    order = open_long(engine, qty="0.01", cid="DKA1-1-1-o")
    by_cid = engine.get_order(symbol="BTCUSDT", client_order_id="DKA1-1-1-o")
    by_id = engine.get_order(symbol="BTCUSDT", order_id=order["order_id"])
    assert by_cid == by_id == order


@pytest.mark.parametrize("kwargs", [
    {"symbol": "BTCUSDT", "client_order_id": "DKA1-1-99-o"},
    {"symbol": "BTCUSDT", "order_id": "123"},
    {"symbol": "ETHUSDT", "client_order_id": "DKA1-1-1-o"},  # 合约不符也是不存在
])
def test_get_missing_order_is_2013(kwargs):
    engine, _, _, _ = make_engine()
    open_long(engine, qty="0.01", cid="DKA1-1-1-o")
    with pytest.raises(EngineError) as e:
        engine.get_order(**kwargs)
    assert e.value.code == -2013


def test_get_algo_by_client_id_only_while_open_then_by_algo_id():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    algo = stop(engine, trigger="49000", cid="DKA1-0-1-s")
    assert engine.get_algo_order(symbol="BTCUSDT", client_algo_id="DKA1-0-1-s")["algo_id"] == algo["algo_id"]
    feed.set_price("BTCUSDT", "48000")
    engine.check_triggers()
    # 条件单编号只在未结条件单内唯一：已结束的按 algo_id 查
    with pytest.raises(EngineError) as e:
        engine.get_algo_order(symbol="BTCUSDT", client_algo_id="DKA1-0-1-s")
    assert e.value.code == -2013
    assert engine.get_algo_order(symbol="BTCUSDT", algo_id=algo["algo_id"])["status"] == "FINISHED"
    with pytest.raises(EngineError) as e2:
        engine.get_algo_order(symbol="BTCUSDT", algo_id="1")
    assert e2.value.code == -2013


def test_cancel_open_order_then_not_found():
    engine, _, _, clock = make_engine()
    order = engine.place_market_order(symbol="BTCUSDT", side="BUY", position_side="LONG", qty=D("0.02"),
                                      client_order_id="DKA1-1-1-o",
                                      partial=PartialFill(ratio=D("0.5"), remainder="later", delay_ms=5000))
    assert order["status"] == "PARTIALLY_FILLED" and order["filled_qty"] == "0.01"
    assert [o["order_id"] for o in engine.open_orders(symbol="BTCUSDT")] == [order["order_id"]]
    cancelled = engine.cancel_order(symbol="BTCUSDT", client_order_id="DKA1-1-1-o")
    assert cancelled["status"] == "CANCELED" and cancelled["filled_qty"] == "0.01"
    assert engine.open_orders() == []
    clock.advance(10_000)
    engine.check_triggers()
    assert position(engine, "BTCUSDT", "LONG")["qty"] == "0.01"  # 撤掉的余量不再成交
    with pytest.raises(EngineError) as e:
        engine.cancel_order(symbol="BTCUSDT", client_order_id="DKA1-1-1-o")
    assert e.value.code == -2011


def test_cancel_filled_order_and_unknown_order_are_2011():
    engine, _, _, _ = make_engine()
    order = open_long(engine, qty="0.01")
    for kwargs in ({"order_id": order["order_id"]}, {"client_order_id": "nope"}):
        with pytest.raises(EngineError) as e:
            engine.cancel_order(symbol="BTCUSDT", **kwargs)
        assert e.value.code == -2011


def test_cancel_algo_three_ways():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    a = stop(engine, trigger="49000", cid="DKA1-0-1-s")
    cancelled = engine.cancel_algo_order(symbol="BTCUSDT", client_algo_id="DKA1-0-1-s")
    assert cancelled["status"] == "CANCELED" and cancelled["algo_id"] == a["algo_id"]
    with pytest.raises(EngineError) as e:
        engine.cancel_algo_order(symbol="BTCUSDT", algo_id=a["algo_id"])  # 已撤
    assert e.value.code == -2011
    b = stop(engine, trigger="49000", cid="DKA1-0-2-s")
    feed.set_price("BTCUSDT", "48000")
    engine.check_triggers()
    with pytest.raises(EngineError) as e2:
        engine.cancel_algo_order(symbol="BTCUSDT", algo_id=b["algo_id"])  # 已触发
    assert e2.value.code == -2011


def test_open_orders_and_algos_filter_by_symbol():
    engine, _, _, _ = make_engine()
    open_long(engine, qty="0.01")
    open_long(engine, symbol="ETHUSDT", qty="0.1", cid="DKA1-1-2-o")
    stop(engine, trigger="49000", cid="DKA1-0-1-s")
    stop(engine, symbol="ETHUSDT", trigger="2900", cid="DKA1-0-2-s")
    assert {a["symbol"] for a in engine.open_algo_orders()} == {"BTCUSDT", "ETHUSDT"}
    assert [a["symbol"] for a in engine.open_algo_orders(symbol="ETHUSDT")] == ["ETHUSDT"]


def test_trades_have_no_client_ids_and_respect_time_window_and_limit():
    engine, _, _, clock = make_engine()
    o1 = open_long(engine, qty="0.01", cid="DKA1-1-1-o")
    clock.advance(1000)
    open_long(engine, qty="0.01", cid="DKA1-1-2-o")
    clock.advance(1000)
    open_long(engine, qty="0.01", cid="DKA1-1-3-o")
    trades = engine.trades(symbol="BTCUSDT")
    assert len(trades) == 3
    assert set(trades[0]) == {"trade_id", "order_id", "symbol", "side", "position_side", "qty", "price",
                              "commission", "commission_asset", "realized_pnl", "time_ms", "maker"}
    assert trades[0]["order_id"] == o1["order_id"]
    assert [t["time_ms"] for t in engine.trades(symbol="BTCUSDT", start_ms=T0 + 500)] == [T0 + 1000, T0 + 2000]
    assert [t["time_ms"] for t in engine.trades(symbol="BTCUSDT", end_ms=T0 + 1000)] == [T0, T0 + 1000]
    assert len(engine.trades(symbol="BTCUSDT", limit=2)) == 2
    assert engine.trades(symbol="ETHUSDT") == []


def test_income_types_commission_realized_pnl_and_funding():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.02")
    feed.set_price("BTCUSDT", "51000")
    engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                              client_order_id="DKA1-0-2-p")
    engine.settle_funding()
    rows = engine.income()
    assert {r["type"] for r in rows} == {"COMMISSION", "REALIZED_PNL", "FUNDING_FEE"}
    assert set(rows[0]) == {"tran_id", "type", "symbol", "amount", "asset", "time_ms", "info"}
    assert all(r["asset"] == "USDT" for r in rows)
    assert {r["type"] for r in engine.income(income_type="FUNDING_FEE")} == {"FUNDING_FEE"}
    assert len({r["tran_id"] for r in rows}) == len(rows)


def test_funding_long_pays_short_receives_at_positive_rate():
    engine, feed, _, _ = make_engine()
    open_long(engine, qty="0.01")
    open_short(engine, symbol="ETHUSDT", qty="0.1", cid="DKA1-1-2-o")
    feed.set_funding_rate("BTCUSDT", "0.0001")
    feed.set_funding_rate("ETHUSDT", "0.0001")
    feed.set_price("BTCUSDT", "50000", mark="50000")
    wallet0 = D(engine.account()["wallet_balance"])
    rows = engine.settle_funding()
    amounts = {r["symbol"]: D(r["amount"]) for r in rows}
    assert amounts == {"BTCUSDT": D("-0.05"), "ETHUSDT": D("0.03")}  # 500 × 0.01%；300 × 0.01%
    assert D(engine.account()["wallet_balance"]) == wallet0 - D("0.05") + D("0.03")
    only_eth = engine.settle_funding(symbol="ETHUSDT")
    assert [r["symbol"] for r in only_eth] == ["ETHUSDT"]


def test_funding_settles_on_utc_8h_boundaries_once():
    engine, _, _, clock = make_engine()
    open_long(engine, qty="0.01")
    assert engine.maybe_settle_funding() == []  # T0 = 02:00，不在结算时刻
    clock.set(T0 + 6 * 3600_000 - 1)  # 07:59:59.999
    assert engine.maybe_settle_funding() == []
    clock.set(T0 + 6 * 3600_000)  # 08:00
    assert len(engine.maybe_settle_funding()) == 1
    clock.advance(5000)
    assert engine.maybe_settle_funding() == []  # 同一时刻只结一次
    clock.set(T0 + 14 * 3600_000 + 1)  # 16:00 之后
    assert len(engine.maybe_settle_funding()) == 1


def test_set_and_get_leverage():
    engine, _, _, _ = make_engine()
    assert engine.leverage_info("ETHUSDT") == {"symbol": "ETHUSDT", "leverage": 20, "margin_mode": "cross"}
    assert engine.set_leverage("ETHUSDT", 5)["leverage"] == 5
    open_long(engine, symbol="ETHUSDT", qty="0.1")
    assert position(engine, "ETHUSDT", "LONG")["leverage"] == 5
    for bad in (0, 126):
        with pytest.raises(EngineError):
            engine.set_leverage("ETHUSDT", bad)


def test_margin_mode_blocked_by_position_4048_and_by_open_orders_4047():
    engine, _, _, _ = make_engine()
    assert engine.set_margin_mode("BTCUSDT", "isolated")["margin_mode"] == "isolated"
    open_long(engine, qty="0.01")
    with pytest.raises(EngineError) as e:
        engine.set_margin_mode("BTCUSDT", "cross")
    assert e.value.code == -4048
    engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                              client_order_id="DKA1-0-2-c")
    stop(engine, trigger="49000")
    with pytest.raises(EngineError) as e2:
        engine.set_margin_mode("BTCUSDT", "cross")
    assert e2.value.code == -4047
    assert engine.set_margin_mode("ETHUSDT", "isolated")["margin_mode"] == "isolated"  # 别的合约不受影响
    assert engine.set_margin_mode("ETHUSDT", "isolated")["margin_mode"] == "isolated"  # 重复设置同值也成功
    with pytest.raises(EngineError) as e3:
        engine.set_margin_mode("ETHUSDT", "crossed")
    assert e3.value.code == -1102


def test_reset_clears_account():
    engine, _, _, _ = make_engine()
    open_long(engine, qty="0.01")
    stop(engine, trigger="49000")
    engine.reset(D("2000"))
    assert engine.positions() == [] and engine.open_algo_orders() == [] and engine.trades(symbol="BTCUSDT") == []
    assert D(engine.account()["wallet_balance"]) == D("2000")
