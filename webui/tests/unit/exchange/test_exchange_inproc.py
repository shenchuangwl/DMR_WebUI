"""tests/support/inproc.py：进程内传输与 make_standin 工厂（之后所有集成测试都靠它，所以它自己要先可靠）。"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.exchange.transport import Response, Transport, TransportConnectionError, TransportTimeout
from dmr_standin.feed import StaticFeed, load_sample
from tests.support.inproc import T0_MS, InProcessTransport, make_standin

D = Decimal
ORDER = {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "type": "MARKET", "qty": "0.01",
         "client_order_id": "DKA1-1-1-o"}


def stack(**kwargs):
    clock = kwargs.pop("clock", None) or ManualClock(T0_MS)
    disp, engine, kernel_token, manual_token = make_standin(clock=clock, **kwargs)
    return disp, engine, InProcessTransport(disp, token=kernel_token), clock, manual_token


# ---------------------------------------------------------------- make_standin


def test_make_standin_returns_dispatcher_engine_and_two_distinct_tokens():
    disp, engine, kernel_token, manual_token = make_standin()
    assert engine is disp.engine
    assert kernel_token and manual_token and kernel_token != manual_token


def test_make_standin_default_feed_is_a_static_feed_priced_from_the_sample_ref_prices():
    disp, engine, *_ = make_standin(clock=ManualClock(T0_MS))
    assert isinstance(disp.feed, StaticFeed)
    sample = {s.symbol: s for s in load_sample()}
    assert set(disp.feed.symbols()) == set(sample)
    quote = disp.feed.quote("BTCUSDT")
    assert quote is not None and quote.last == sample["BTCUSDT"].ref_price


def test_make_standin_accepts_a_prepared_feed_and_prices_override():
    clock = ManualClock(T0_MS)
    feed = StaticFeed(load_sample()[:3], clock=clock.now_ms)
    disp, *_ = make_standin(feed=feed, clock=clock)
    assert disp.feed is feed and feed.quote(feed.symbols()[0]) is None  # 自备的行情源原样使用，不替它设价
    disp2, *_ = make_standin(clock=clock, prices={"BTCUSDT": "50000"})
    assert disp2.feed.quote("BTCUSDT").last == D("50000")


async def test_make_standin_equity_and_injected_clock():
    clock = ManualClock(T0_MS)
    disp, engine, kernel_token, manual_token = make_standin(equity="500", clock=clock)
    t = InProcessTransport(disp, token=kernel_token)
    r = await t.request("GET", "/api/private/account", timeout_s=1)
    assert r.body["equity"] == "500"
    assert (await t.request("GET", "/api/public/time", auth=False, timeout_s=1)).body == {"server_time_ms": T0_MS}
    clock.advance(5_000)
    assert (await t.request("GET", "/api/public/time", auth=False, timeout_s=1)).body == {"server_time_ms": T0_MS + 5_000}


# ---------------------------------------------------------------- 请求


async def test_transport_implements_the_protocol_and_returns_json_decoded_responses():
    disp, engine, t, clock, _ = stack()
    assert isinstance(t, Transport)
    r = await t.request("GET", "/api/private/account", timeout_s=1)
    assert isinstance(r, Response) and r.status == 200
    assert r.body["equity"] == "1000" and isinstance(r.body["update_time_ms"], int)
    assert "X-MBX-USED-WEIGHT-1M" in r.headers


async def test_public_routes_need_no_token_but_private_ones_refuse_without_auth():
    disp, engine, t, *_ = stack()
    assert (await t.request("GET", "/api/public/instruments", auth=False, timeout_s=1)).status == 200
    r = await t.request("GET", "/api/private/account", auth=False, timeout_s=1)
    assert r.status == 401 and r.body["code"] == -2015


async def test_wrong_token_is_refused_with_401():
    disp, engine, t, clock, manual_token = stack()
    wrong = InProcessTransport(disp, token=manual_token)  # 控制台的令牌不能访问内核接口
    assert (await wrong.request("GET", "/api/private/account", timeout_s=1)).status == 401


async def test_post_body_and_query_params_reach_the_dispatcher():
    disp, engine, t, *_ = stack()
    r = await t.request("POST", "/api/private/order", body=ORDER, timeout_s=1)
    assert r.status == 200 and r.body["client_order_id"] == "DKA1-1-1-o" and r.body["status"] == "FILLED"
    q = await t.request("GET", "/api/private/order", params={"symbol": "BTCUSDT", "client_order_id": "DKA1-1-1-o"},
                        timeout_s=1)
    assert q.status == 200 and q.body["order_id"] == r.body["order_id"]
    # 值为 None 的参数不发；整数转成字符串
    trades = await t.request("GET", "/api/private/trades", params={"symbol": "BTCUSDT", "limit": 5, "end_ms": None},
                             timeout_s=1)
    assert trades.status == 200 and len(trades.body) == 1


async def test_business_errors_are_returned_as_responses_not_raised():
    disp, engine, t, *_ = stack()
    r = await t.request("GET", "/api/private/order", params={"symbol": "BTCUSDT", "client_order_id": "nope"},
                        timeout_s=1)
    assert r.status == 400 and r.body["code"] == -2013


async def test_hang_fault_surfaces_as_transport_timeout_and_the_order_was_executed():
    disp, engine, t, *_ = stack()
    disp.switches.update({"order_timeout": {"enabled": True, "mode": "accept_then_hang", "hang_s": 30}})
    with pytest.raises(TransportTimeout):
        await t.request("POST", "/api/private/order", body=ORDER, timeout_s=0.05)
    assert [p["qty"] for p in engine.positions()] == ["0.01"]  # accept_then_hang：照常成交，只是响应没回来


async def test_drop_fault_surfaces_as_connection_error_and_nothing_executes():
    disp, engine, t, *_ = stack()
    disp.switches.update({"order_timeout": {"enabled": True, "mode": "drop", "hang_s": 0.01}})
    with pytest.raises(TransportConnectionError):
        await t.request("POST", "/api/private/order", body=ORDER, timeout_s=1)
    assert engine.positions() == []


async def test_drop_fault_with_a_long_hang_is_a_timeout_first():
    disp, engine, t, *_ = stack()
    disp.switches.update({"order_timeout": {"enabled": True, "mode": "drop", "hang_s": 30}})
    with pytest.raises(TransportTimeout):
        await t.request("POST", "/api/private/order", body=ORDER, timeout_s=0.05)
    assert engine.positions() == []


async def test_position_query_hang_is_a_timeout():
    disp, engine, t, *_ = stack()
    disp.switches.update({"position_query_fail": {"enabled": True, "mode": "hang"}})
    with pytest.raises(TransportTimeout):
        await t.request("GET", "/api/private/positions", timeout_s=0.05)


async def test_position_query_error_mode_is_a_500_response():
    disp, engine, t, *_ = stack()
    disp.switches.update({"position_query_fail": {"enabled": True, "mode": "error"}})
    r = await t.request("GET", "/api/private/positions", timeout_s=1)
    assert r.status == 500 and r.body["code"] == -1001


async def test_rate_limit_fault_is_a_429_response_with_retry_after():
    disp, engine, t, *_ = stack()
    disp.switches.update({"rate_limit": {"enabled": True, "http": 429, "scope": "reads"}})
    r = await t.request("GET", "/api/private/account", timeout_s=1)
    assert r.status == 429 and int(r.headers["Retry-After"]) >= 1


# ---------------------------------------------------------------- 数据流


async def open_stream(t, *, timeout_s=1.0):
    key = (await t.request("POST", "/api/private/listen_key", timeout_s=1)).body["listen_key"]
    return key, await t.ws_connect("/ws/stream", params={"listen_key": key}, timeout_s=timeout_s)


async def drain(conn):
    frames = []
    while True:
        try:
            text = await conn.receive(0.05)
        except TransportTimeout:
            return frames
        assert text is not None
        frames.append(json.loads(text))


async def test_ws_delivers_json_text_frames_for_trading_activity():
    disp, engine, t, *_ = stack()
    key, conn = await open_stream(t)
    await t.request("POST", "/api/private/order", body=ORDER, timeout_s=1)
    frames = await drain(conn)
    assert {f["type"] for f in frames} == {"order_update", "account_update"}
    orders = [f for f in frames if f["type"] == "order_update"]
    assert {f["execution"] for f in orders} == {"NEW", "TRADE"}
    assert all(f["order"]["client_order_id"] == "DKA1-1-1-o" for f in orders)


async def test_ws_receive_times_out_when_nothing_arrives():
    disp, engine, t, *_ = stack()
    _, conn = await open_stream(t)
    with pytest.raises(TransportTimeout):
        await conn.receive(0.05)
    # 超时之后连接仍可继续使用
    disp.hub.publish([{"type": "heartbeat", "server_time_ms": 1}])
    assert json.loads(await conn.receive(1.0))["type"] == "heartbeat"


async def test_ws_is_refused_for_bad_keys_and_during_fault_windows():
    disp, engine, t, *_ = stack()
    with pytest.raises(TransportConnectionError):
        await t.ws_connect("/ws/stream", params={"listen_key": "nope"}, timeout_s=1)
    key = (await t.request("POST", "/api/private/listen_key", timeout_s=1)).body["listen_key"]
    disp.switches.update({"behaviors": {"stream_available": False}})
    with pytest.raises(TransportConnectionError):
        await t.ws_connect("/ws/stream", params={"listen_key": key}, timeout_s=1)
    disp.switches.update({"behaviors": {"stream_available": True}})
    disp.switches.update({"stream_drop": {"enabled": True, "mode": "close", "duration_s": 60}})
    with pytest.raises(TransportConnectionError):
        await t.ws_connect("/ws/stream", params={"listen_key": key}, timeout_s=1)


async def test_ws_receive_returns_none_after_the_server_closes_and_frames_are_drained():
    disp, engine, t, *_ = stack()
    _, conn = await open_stream(t)
    disp.hub.publish([{"type": "heartbeat", "server_time_ms": 1}])
    disp.switches.update({"stream_drop": {"enabled": True, "mode": "close", "duration_s": 60}})  # 服务端断开全部连接
    assert json.loads(await conn.receive(1.0))["type"] == "heartbeat"  # 已排队的帧仍可取完
    assert await conn.receive(1.0) is None
    assert await conn.receive(1.0) is None


async def test_ws_close_and_transport_close_unsubscribe():
    disp, engine, t, *_ = stack()
    _, conn = await open_stream(t)
    _, conn2 = await open_stream(t)
    assert disp.hub.count() == 2
    await conn.close()
    assert disp.hub.count() == 1
    await conn.close()  # 重复关闭不出错
    await t.close()
    assert disp.hub.count() == 0
    assert await conn2.receive(1.0) is None


async def test_an_unknown_ws_path_is_a_connection_error_even_with_a_valid_listen_key():
    disp, engine, t, *_ = stack()
    key = (await t.request("POST", "/api/private/listen_key", timeout_s=1)).body["listen_key"]
    for path in ("/ws/other", "/ws/stream/", "/ws"):
        with pytest.raises(TransportConnectionError):
            await t.ws_connect(path, params={"listen_key": key}, timeout_s=1)
    assert disp.hub.count() == 0  # 路径不对就根本没有订阅
    conn = await t.ws_connect("/ws/stream", params={"listen_key": key}, timeout_s=1)
    assert disp.hub.count() == 1
    await conn.close()


async def test_payloads_come_back_as_plain_json_types_and_a_non_json_payload_is_a_dispatcher_bug():
    class Dispatcher:
        def __init__(self, payload):
            self.payload = payload

        async def handle(self, method, path, params, body, headers):
            return 200, self.payload, {"X-MBX-USED-WEIGHT-1M": "3"}

    t = InProcessTransport(Dispatcher({"rows": ({"a": 1}, (2, 3)), 5: "int key"}), token="k")
    response = await t.request("GET", "/x", timeout_s=1)
    assert response.body == {"rows": [{"a": 1}, [2, 3]], "5": "int key"}  # 与真实 HTTP 一样只剩 JSON 类型
    assert response.headers == {"X-MBX-USED-WEIGHT-1M": "3"}
    t = InProcessTransport(Dispatcher({"price": D("1.5")}), token="k")
    with pytest.raises(TypeError):
        await t.request("GET", "/x", timeout_s=1)


def test_default_config_covers_every_kernel_setting_and_rejects_unknown_overrides():
    from dmr_kernel.core.configdefs import DEFINITIONS
    from tests.support.inproc import default_config

    config = default_config(stream_silence_s=30)
    assert set(config) == set(DEFINITIONS) and config["stream_silence_s"] == 30
    assert default_config()["stream_silence_s"] == 90
    with pytest.raises(KeyError):
        default_config(not_a_setting=1)
