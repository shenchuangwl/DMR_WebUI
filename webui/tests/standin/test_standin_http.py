"""分发器的 HTTP 协议（docs/设计约定.md 7.2、7.4）：鉴权、权重头、报文格式、错误码、开发控制。

直接调 Dispatcher.handle（与传输无关），不开任何端口。
"""
from __future__ import annotations

import pytest

from standin_kit import D, KERNEL_TOKEN, KH, MANUAL_TOKEN, MH, T0, call, make_standin

PRIVATE_GETS = ["/api/private/account", "/api/private/position_mode", "/api/private/positions",
                "/api/private/open_orders", "/api/private/open_algo_orders", "/api/private/trading_status"]


@pytest.mark.parametrize("path", PRIVATE_GETS)
@pytest.mark.parametrize("headers", [{}, {"X-Standin-Key": "wrong"}, MH])
async def test_private_endpoints_need_the_kernel_token(path, headers):
    disp, _, _ = make_standin()
    status, body, _ = await call(disp, "GET", path, headers=headers)
    assert status == 401 and isinstance(body["code"], int)
    status, _, _ = await call(disp, "GET", path, headers=KH)
    assert status == 200


@pytest.mark.parametrize("headers", [{}, KH, {"X-Standin-Key": "wrong"}])
async def test_dev_endpoints_only_accept_the_manual_token(headers):
    disp, _, _ = make_standin()
    status, _, _ = await call(disp, "GET", "/dev/state", headers=headers)
    assert status == 401
    status, _, _ = await call(disp, "GET", "/dev/state", headers=MH)
    assert status == 200


async def test_header_lookup_is_case_insensitive():
    disp, _, _ = make_standin()
    status, _, _ = await call(disp, "GET", "/api/private/account", headers={"x-standin-key": KERNEL_TOKEN})
    assert status == 200
    status, _, _ = await call(disp, "GET", "/dev/state", headers={"X-STANDIN-KEY": MANUAL_TOKEN})
    assert status == 200


@pytest.mark.parametrize("path", ["/api/public/time", "/api/public/instruments", "/api/public/tickers",
                                  "/api/public/feed_status"])
async def test_public_endpoints_need_no_token(path):
    disp, _, _ = make_standin()
    status, _, _ = await call(disp, "GET", path, headers={})
    assert status == 200


async def test_used_weight_header_accumulates_and_rolls_out_after_a_minute():
    disp, _, clock = make_standin()
    _, _, h1 = await call(disp, "GET", "/api/private/positions")  # 5
    _, _, h2 = await call(disp, "GET", "/api/public/time")  # 1
    _, _, h3 = await call(disp, "GET", "/api/private/open_orders")  # 不带合约 40
    assert [int(h["X-MBX-USED-WEIGHT-1M"]) for h in (h1, h2, h3)] == [5, 6, 46]
    clock.advance(30_000)
    _, _, h4 = await call(disp, "GET", "/api/private/open_orders", params={"symbol": "BTCUSDT"})  # 带合约 1
    assert int(h4["X-MBX-USED-WEIGHT-1M"]) == 47
    clock.advance(30_000)  # 前三个滚出
    _, _, h5 = await call(disp, "GET", "/api/public/time")
    assert int(h5["X-MBX-USED-WEIGHT-1M"]) == 2
    _, _, h6 = await call(disp, "GET", "/dev/state", headers=MH)
    assert "X-MBX-USED-WEIGHT-1M" in h6  # 每个响应都带


async def test_time_instruments_and_position_mode_formats():
    disp, _, _ = make_standin()
    _, body, _ = await call(disp, "GET", "/api/public/time")
    assert body == {"server_time_ms": T0}
    _, insts, _ = await call(disp, "GET", "/api/public/instruments")
    btc = [i for i in insts if i["symbol"] == "BTCUSDT"][0]
    assert set(btc) == {"symbol", "base", "quote", "contract_type", "status", "tick_size", "step_size", "min_qty",
                        "min_notional", "market_step_size", "market_max_qty", "onboard_ms"}
    assert btc["tick_size"] == "0.1" and btc["min_notional"] == "100" and isinstance(btc["onboard_ms"], int)
    _, mode, _ = await call(disp, "GET", "/api/private/position_mode")
    assert mode == {"dual_side": True}
    _, status_body, _ = await call(disp, "GET", "/api/private/trading_status")
    assert status_body == {"restricted": False, "detail": {}}


async def test_account_and_position_formats_are_strings():
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body={"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
                                                          "type": "MARKET", "qty": "0.01",
                                                          "client_order_id": "DKA1-1-1-o"})
    _, acct, _ = await call(disp, "GET", "/api/private/account")
    assert set(acct) == {"equity", "wallet_balance", "available_balance", "unrealized_pnl", "update_time_ms"}
    assert all(isinstance(acct[k], str) for k in ("equity", "wallet_balance", "available_balance", "unrealized_pnl"))
    _, positions, _ = await call(disp, "GET", "/api/private/positions")
    assert len(positions) == 1
    p = positions[0]
    assert set(p) == {"symbol", "position_side", "qty", "entry_price", "mark_price", "liquidation_price",
                      "unrealized_pnl", "leverage", "margin_mode", "update_time_ms"}
    assert isinstance(p["qty"], str) and isinstance(p["update_time_ms"], int)


async def test_order_round_trip_query_and_errors():
    disp, _, _ = make_standin()
    body = {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "type": "MARKET", "qty": "0.01",
            "client_order_id": "DKA1-1-1-o"}
    status, order, _ = await call(disp, "POST", "/api/private/order", body=body)
    assert status == 200
    assert set(order) == {"symbol", "client_order_id", "order_id", "side", "position_side", "type", "qty",
                          "filled_qty", "avg_price", "status", "raw_status", "close_position", "reduce_only",
                          "update_time_ms"}
    status, got, _ = await call(disp, "GET", "/api/private/order",
                                params={"symbol": "BTCUSDT", "client_order_id": "DKA1-1-1-o"})
    assert status == 200 and got["order_id"] == order["order_id"]
    status, got2, _ = await call(disp, "GET", "/api/private/order",
                                 params={"symbol": "BTCUSDT", "order_id": order["order_id"]})
    assert got2 == got
    status, missing, _ = await call(disp, "GET", "/api/private/order",
                                    params={"symbol": "BTCUSDT", "client_order_id": "nope"})
    assert status == 400 and missing["code"] == -2013 and isinstance(missing["msg"], str)
    status, err, _ = await call(disp, "DELETE", "/api/private/order",
                                params={"symbol": "BTCUSDT", "client_order_id": "DKA1-1-1-o"})
    assert status == 400 and err["code"] == -2011
    status, trades, _ = await call(disp, "GET", "/api/private/trades", params={"symbol": "BTCUSDT"})
    assert status == 200 and len(trades) == 1 and "client_order_id" not in trades[0]


@pytest.mark.parametrize("patch, code", [
    ({"symbol": "NOPEUSDT"}, -1121),
    ({"side": "HOLD"}, -1102),
    ({"position_side": "BOTH"}, -1102),
    ({"type": "LIMIT"}, -1102),
    ({"qty": "abc"}, -1102),
    ({"qty": None}, -1102),
    ({"qty": "0.0105"}, -1111),
    ({"client_order_id": "has space"}, -4015),
])
async def test_order_parameter_errors(patch, code):
    disp, _, _ = make_standin()
    body = {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "type": "MARKET", "qty": "0.01",
            "client_order_id": "DKA1-1-1-o", **patch}
    status, err, _ = await call(disp, "POST", "/api/private/order", body=body)
    assert status == 400 and err["code"] == code


async def test_algo_round_trip_and_cancel_by_client_id():
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body={"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
                                                          "type": "MARKET", "qty": "0.01",
                                                          "client_order_id": "DKA1-1-1-o"})
    body = {"symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG", "type": "STOP_MARKET",
            "trigger_price": "49000", "working_type": "MARK_PRICE", "close_position": True, "qty": None,
            "client_algo_id": "DKA1-0-1-s"}
    status, algo, _ = await call(disp, "POST", "/api/private/algo_order", body=body)
    assert status == 200
    assert set(algo) == {"symbol", "client_algo_id", "algo_id", "side", "position_side", "type", "trigger_price",
                         "working_type", "close_position", "qty", "status", "actual_order_id", "create_time_ms",
                         "update_time_ms"}
    assert algo["trigger_price"] == "49000" and algo["qty"] is None and algo["status"] == "NEW"
    status, listed, _ = await call(disp, "GET", "/api/private/open_algo_orders", params={"symbol": "BTCUSDT"})
    assert [a["algo_id"] for a in listed] == [algo["algo_id"]]
    status, got, _ = await call(disp, "GET", "/api/private/algo_order",
                                params={"symbol": "BTCUSDT", "client_algo_id": "DKA1-0-1-s"})
    assert got["algo_id"] == algo["algo_id"]
    status, cancelled, _ = await call(disp, "DELETE", "/api/private/algo_order",
                                      params={"symbol": "BTCUSDT", "client_algo_id": "DKA1-0-1-s"})
    assert status == 200 and cancelled["status"] == "CANCELED"
    status, err, _ = await call(disp, "DELETE", "/api/private/algo_order",
                                params={"symbol": "BTCUSDT", "algo_id": algo["algo_id"]})
    assert status == 400 and err["code"] == -2011
    status, err2, _ = await call(disp, "GET", "/api/private/algo_order",
                                 params={"symbol": "BTCUSDT", "algo_id": "42"})
    assert status == 400 and err2["code"] == -2013


async def test_stop_crossed_on_create_is_2021_over_http():
    disp, _, _ = make_standin()
    body = {"symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG", "type": "STOP_MARKET",
            "trigger_price": "50000", "working_type": "CONTRACT_PRICE", "close_position": True, "qty": None,
            "client_algo_id": "DKA1-0-1-s"}
    status, err, _ = await call(disp, "POST", "/api/private/algo_order", body=body)
    assert status == 400 and err["code"] == -2021


async def test_klines_format_and_limit():
    disp, feed, _ = make_standin()
    candles = [[T0 - 900_000 * (3 - i), "1", "2", "0.5", "1.5", "10", T0 - 900_000 * (2 - i) - 1] for i in range(3)]
    feed.set_candles("BTCUSDT", "15m", candles)
    status, rows, _ = await call(disp, "GET", "/api/public/klines",
                                 params={"symbol": "BTCUSDT", "interval": "15m", "limit": "2"})
    assert status == 200 and rows == candles[-2:]
    status, err, _ = await call(disp, "GET", "/api/public/klines",
                                params={"symbol": "BTCUSDT", "interval": "15m", "limit": "100"})
    assert status == 400 and err["code"] == -1102
    status, err2, _ = await call(disp, "GET", "/api/public/klines",
                                 params={"symbol": "NOPEUSDT", "interval": "15m", "limit": "10"})
    assert status == 400 and err2["code"] == -1121


async def test_tickers_all_and_filtered():
    disp, feed, _ = make_standin()
    feed.set_price("ETHUSDT", "3000", mark="2999.5")
    status, all_, _ = await call(disp, "GET", "/api/public/tickers")
    assert set(all_) == {"BTCUSDT", "ETHUSDT", "DOGEUSDT"}
    status, some, _ = await call(disp, "GET", "/api/public/tickers", params={"symbols": "ETHUSDT,BTCUSDT"})
    assert set(some) == {"ETHUSDT", "BTCUSDT"}
    assert some["ETHUSDT"] == {"symbol": "ETHUSDT", "last": "3000", "mark": "2999.5", "time_ms": T0}


async def test_feed_status_reports_source():
    disp, _, _ = make_standin()
    status, body, _ = await call(disp, "GET", "/api/public/feed_status")
    assert body["source"] == "static" and body["healthy"] is True and "last_ok_ms" in body
    assert isinstance(body["errors"], list)


async def test_leverage_and_margin_mode_over_http():
    disp, _, _ = make_standin()
    status, body, _ = await call(disp, "POST", "/api/private/leverage", body={"symbol": "BTCUSDT", "leverage": 10})
    assert status == 200 and body["leverage"] == 10
    status, body, _ = await call(disp, "GET", "/api/private/leverage", params={"symbol": "BTCUSDT"})
    assert body == {"symbol": "BTCUSDT", "leverage": 10, "margin_mode": "cross"}
    await call(disp, "POST", "/api/private/order", body={"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
                                                          "type": "MARKET", "qty": "0.01",
                                                          "client_order_id": "DKA1-1-1-o"})
    status, err, _ = await call(disp, "POST", "/api/private/margin_mode",
                                body={"symbol": "BTCUSDT", "margin_mode": "isolated"})
    assert status == 400 and err["code"] == -4048


async def test_income_endpoint_filters_type():
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body={"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
                                                          "type": "MARKET", "qty": "0.01",
                                                          "client_order_id": "DKA1-1-1-o"})
    status, rows, _ = await call(disp, "GET", "/api/private/income", params={"type": "COMMISSION"})
    assert status == 200 and [r["type"] for r in rows] == ["COMMISSION"]


async def test_unknown_route_and_bad_json():
    disp, _, _ = make_standin()
    status, body, _ = await call(disp, "GET", "/api/private/nothing")
    assert status == 404 and isinstance(body["code"], int)
    status, body, _ = await disp.handle("POST", "/api/private/order", {}, b"{not json", KH)
    assert status == 400 and body["code"] == -1102


# ------------------------------------------------------------------ 开发控制（7.4）


async def test_manual_order_uses_web_ids_and_is_logged():
    disp, _, _ = make_standin()
    status, order, _ = await call(disp, "POST", "/dev/manual/order",
                                  body={"symbol": "ETHUSDT", "side": "SELL", "position_side": "SHORT", "qty": "0.1"},
                                  headers=MH)
    assert status == 200 and order["client_order_id"].startswith("web_") and order["status"] == "FILLED"
    status, log, _ = await call(disp, "GET", "/dev/ops_log", params={"limit": "5"}, headers=MH)
    assert log[-1]["op"] == "manual_order" and log[-1]["result"] == "ok"


async def test_manual_algo_and_manual_cancel_of_system_stop():
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body={"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
                                                          "type": "MARKET", "qty": "0.01",
                                                          "client_order_id": "DKA1-1-1-o"})
    status, sys_stop, _ = await call(disp, "POST", "/api/private/algo_order", body={
        "symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG", "type": "STOP_MARKET",
        "trigger_price": "49000", "working_type": "CONTRACT_PRICE", "close_position": True, "qty": None,
        "client_algo_id": "DKA1-0-1-s"})
    assert status == 200
    status, tp, _ = await call(disp, "POST", "/dev/manual/algo_order", body={
        "symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG", "type": "TAKE_PROFIT_MARKET",
        "trigger_price": "55000", "working_type": "MARK_PRICE", "close_position": False, "qty": "0.01"},
        headers=MH)
    assert status == 200 and tp["client_algo_id"].startswith("web_")
    # 故障第 9 种：用户手动撤掉系统的止损
    status, cancelled, _ = await call(disp, "POST", "/dev/manual/cancel",
                                      body={"symbol": "BTCUSDT", "client_algo_id": "DKA1-0-1-s"}, headers=MH)
    assert status == 200 and cancelled["status"] == "CANCELED" and cancelled["algo_id"] == sys_stop["algo_id"]
    status, err, _ = await call(disp, "POST", "/dev/manual/cancel",
                                body={"symbol": "BTCUSDT", "algo_id": sys_stop["algo_id"]}, headers=MH)
    assert status == 400 and err["code"] == -2011
    status, err2, _ = await call(disp, "POST", "/dev/manual/cancel", body={"symbol": "BTCUSDT"}, headers=MH)
    assert status == 400 and err2["code"] == -1102


async def test_price_override_absolute_relative_and_clear():
    disp, feed, _ = make_standin()
    status, body, _ = await call(disp, "POST", "/dev/price_override", body={"symbol": "BTCUSDT", "price": "45000"},
                                 headers=MH)
    assert status == 200
    _, t, _ = await call(disp, "GET", "/api/public/tickers", params={"symbols": "BTCUSDT"})
    assert t["BTCUSDT"]["last"] == "45000" and t["BTCUSDT"]["mark"] == "45000"
    await call(disp, "POST", "/dev/price_override", body={"symbol": "BTCUSDT", "pct": "-10"}, headers=MH)
    _, t, _ = await call(disp, "GET", "/api/public/tickers", params={"symbols": "BTCUSDT"})
    assert D(t["BTCUSDT"]["last"]) == D("40500")
    feed.set_price("BTCUSDT", "51000")
    _, t, _ = await call(disp, "GET", "/api/public/tickers", params={"symbols": "BTCUSDT"})
    assert t["BTCUSDT"]["last"] == "40500"  # 覆盖优先于行情源
    await call(disp, "POST", "/dev/price_override", body={"symbol": "BTCUSDT", "clear": True}, headers=MH)
    _, t, _ = await call(disp, "GET", "/api/public/tickers", params={"symbols": "BTCUSDT"})
    assert t["BTCUSDT"]["last"] == "51000"
    status, err, _ = await call(disp, "POST", "/dev/price_override", body={"symbol": "BTCUSDT"}, headers=MH)
    assert status == 400


async def test_price_override_crosses_stop_while_kernel_is_down():
    """故障第 6 种：内核停机期间价格越过止损，替身照常触发。"""
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body={"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
                                                          "type": "MARKET", "qty": "0.01",
                                                          "client_order_id": "DKA1-1-1-o"})
    await call(disp, "POST", "/api/private/algo_order", body={
        "symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG", "type": "STOP_MARKET",
        "trigger_price": "49000", "working_type": "CONTRACT_PRICE", "close_position": True, "qty": None,
        "client_algo_id": "DKA1-0-1-s"})
    await call(disp, "POST", "/dev/price_override", body={"symbol": "BTCUSDT", "pct": "-3"}, headers=MH)
    disp.tick()
    _, positions, _ = await call(disp, "GET", "/api/private/positions")
    assert positions == []


async def test_funding_settle_now_and_dev_state_and_reset():
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body={"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
                                                          "type": "MARKET", "qty": "0.01",
                                                          "client_order_id": "DKA1-1-1-o"})
    status, rows, _ = await call(disp, "POST", "/dev/funding/settle", body={"symbol": "BTCUSDT"}, headers=MH)
    assert status == 200 and rows[0]["type"] == "FUNDING_FEE" and D(rows[0]["amount"]) == D("-0.05")
    status, state, _ = await call(disp, "GET", "/dev/state", headers=MH)
    assert {"account", "positions", "open_orders", "open_algo_orders", "prices", "feed", "switches"} <= set(state)
    assert state["switches"]["behaviors"]["close_qty_exceeds"] == "reject"
    status, _, _ = await call(disp, "POST", "/dev/reset", body={"equity": "500"}, headers=MH)
    assert status == 200
    _, acct, _ = await call(disp, "GET", "/api/private/account")
    assert D(acct["wallet_balance"]) == D("500")
    _, positions, _ = await call(disp, "GET", "/api/private/positions")
    assert positions == []


async def test_dev_subscribe_adds_symbols_to_feed_interest():
    disp, feed, _ = make_standin()
    status, body, _ = await call(disp, "POST", "/dev/subscribe", body={"symbols": ["DOGEUSDT"]}, headers=MH)
    assert status == 200
    disp.tick()
    assert "DOGEUSDT" in feed.interest
    status, err, _ = await call(disp, "POST", "/dev/subscribe", body={"symbols": "DOGEUSDT"}, headers=MH)
    assert status == 400


async def test_faults_get_put_reset_over_http():
    disp, _, _ = make_standin()
    status, body, _ = await call(disp, "GET", "/dev/faults", headers=MH)
    assert status == 200 and set(body) == {"faults", "behaviors"}
    status, body, _ = await call(disp, "PUT", "/dev/faults",
                                 body={"faults": {"rate_limit": {"enabled": True, "http": 418}}}, headers=MH)
    assert status == 200 and body["faults"]["rate_limit"]["enabled"] is True
    status, err, _ = await call(disp, "PUT", "/dev/faults", body={"behaviors": {"nope": 1}}, headers=MH)
    assert status == 400 and isinstance(err["msg"], str)
    status, body, _ = await call(disp, "POST", "/dev/faults/reset", headers=MH)
    assert body["faults"]["rate_limit"]["enabled"] is False
    status, log, _ = await call(disp, "GET", "/dev/ops_log", headers=MH)
    assert [e["op"] for e in log][-3:] == ["faults_update", "faults_update", "faults_reset"]
    assert log[-2]["result"] != "ok"  # 失败的修改也留痕
