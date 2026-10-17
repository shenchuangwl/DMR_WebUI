"""替身开发工具的客户端：报文、令牌读取（var/standin/manual.token）、地址（DMR_STANDIN_URL）、错误处理。"""
from __future__ import annotations

import json

import httpx
import pytest

from dmr_console.devtools import (DEFAULT_STANDIN_URL, FAULT_KEYS, StandinDev, StandinError, manual_algo_body,
                                  manual_order_body, standin_url_from_env)


def make_dev(tmp_path, handler, *, token="tok-manual"):
    path = tmp_path / "standin" / "manual.token"
    path.parent.mkdir(parents=True)
    path.write_text(token + "\n", encoding="utf-8")
    return StandinDev("http://127.0.0.1:18220", path, transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ 纯函数

@pytest.mark.parametrize("action,direction,side", [
    ("open", "LONG", "BUY"), ("add", "LONG", "BUY"), ("reduce", "LONG", "SELL"),
    ("open", "SHORT", "SELL"), ("add", "SHORT", "SELL"), ("reduce", "SHORT", "BUY"),
])
def test_manual_order_sides(action, direction, side):
    assert manual_order_body(action, "BTCUSDT", direction, "0.01") == {
        "symbol": "BTCUSDT", "side": side, "position_side": direction, "qty": "0.01"}


@pytest.mark.parametrize("kind,direction,type_,side", [
    ("stop", "LONG", "STOP_MARKET", "SELL"), ("take_profit", "LONG", "TAKE_PROFIT_MARKET", "SELL"),
    ("stop", "SHORT", "STOP_MARKET", "BUY"), ("take_profit", "SHORT", "TAKE_PROFIT_MARKET", "BUY"),
])
def test_manual_algo_orders_close_the_whole_position_on_the_closing_side(kind, direction, type_, side):
    assert manual_algo_body(kind, "BTCUSDT", direction, "59000") == {
        "symbol": "BTCUSDT", "side": side, "position_side": direction, "type": type_, "trigger_price": "59000",
        "close_position": True}


def test_bad_choices_are_rejected_before_any_request():
    with pytest.raises(ValueError):
        manual_order_body("buy", "BTCUSDT", "LONG", "1")
    with pytest.raises(ValueError):
        manual_order_body("open", "BTCUSDT", "UP", "1")
    with pytest.raises(ValueError):
        manual_algo_body("trail", "BTCUSDT", "LONG", "1")


def test_the_standin_address_comes_from_the_environment_and_must_be_loopback():
    assert standin_url_from_env({}) == DEFAULT_STANDIN_URL == "http://127.0.0.1:18220"
    assert standin_url_from_env({"DMR_STANDIN_URL": "http://127.0.0.1:19999/"}) == "http://127.0.0.1:19999"
    with pytest.raises(ValueError):
        standin_url_from_env({"DMR_STANDIN_URL": "http://example.org:18220"})
    with pytest.raises(ValueError):
        standin_url_from_env({"DMR_STANDIN_URL": "https://127.0.0.1:18220"})


def test_the_client_refuses_non_loopback_addresses(tmp_path):
    with pytest.raises(ValueError):
        StandinDev("http://10.1.2.3:18220", tmp_path / "t")


# ------------------------------------------------------------------ 请求

async def test_orders_are_sent_with_the_manual_token_header(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"orderId": 1, "status": "FILLED"})

    dev = make_dev(tmp_path, handler)
    result = await dev.manual_order("open", "BTCUSDT", "LONG", "0.01")
    assert result["status"] == "FILLED"
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/dev/manual/order"
    assert request.headers["x-standin-key"] == "tok-manual"
    assert json.loads(request.content) == {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "qty": "0.01"}
    await dev.aclose()


async def test_algo_faults_and_price_override_use_their_endpoints(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"faults": {}, "behaviors": {}})

    dev = make_dev(tmp_path, handler)
    await dev.manual_algo("stop", "BTCUSDT", "LONG", "59000")
    await dev.faults()
    await dev.set_fault("partial_fill", True)
    await dev.set_fault("partial_fill", False)
    await dev.reset_faults()
    await dev.price_override("BTCUSDT", price="61000")
    await dev.price_override("BTCUSDT", pct="-3")
    await dev.price_override("BTCUSDT", clear=True)
    assert seen == [
        ("POST", "/dev/manual/algo_order", {"symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG",
                                            "type": "STOP_MARKET", "trigger_price": "59000", "close_position": True}),
        ("GET", "/dev/faults", None),
        ("PUT", "/dev/faults", {"faults": {"partial_fill": {"enabled": True}}}),
        ("PUT", "/dev/faults", {"faults": {"partial_fill": {"enabled": False}}}),
        ("POST", "/dev/faults/reset", None),
        ("POST", "/dev/price_override", {"symbol": "BTCUSDT", "price": "61000"}),
        ("POST", "/dev/price_override", {"symbol": "BTCUSDT", "pct": "-3"}),
        ("POST", "/dev/price_override", {"symbol": "BTCUSDT", "clear": True}),
    ]
    await dev.aclose()


async def test_the_token_is_read_when_used_so_a_rotated_token_works(tmp_path):
    tokens = []

    def handler(request: httpx.Request):
        tokens.append(request.headers["x-standin-key"])
        return httpx.Response(200, json={})

    dev = make_dev(tmp_path, handler)
    await dev.faults()
    (tmp_path / "standin" / "manual.token").write_text("tok-2", encoding="utf-8")
    await dev.faults()
    assert tokens == ["tok-manual", "tok-2"]
    await dev.aclose()


async def test_a_missing_token_file_is_a_clear_error_and_nothing_is_sent(tmp_path):
    sent = []
    dev = StandinDev("http://127.0.0.1:18220", tmp_path / "nope" / "manual.token",
                     transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, json={})))
    with pytest.raises(StandinError) as info:
        await dev.faults()
    assert "manual.token" in str(info.value) and not sent
    await dev.aclose()


async def test_standin_errors_carry_the_standin_message_not_the_token(tmp_path):
    dev = make_dev(tmp_path, lambda r: httpx.Response(400, json={"code": -1102, "msg": "缺少参数 qty"}))
    with pytest.raises(StandinError) as info:
        await dev.manual_order("open", "BTCUSDT", "LONG", "")
    assert "缺少参数 qty" in str(info.value) and "tok-manual" not in str(info.value)
    await dev.aclose()


async def test_an_unreachable_standin_is_reported_as_an_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom")

    dev = make_dev(tmp_path, handler)
    with pytest.raises(StandinError) as info:
        await dev.faults()
    assert "替身交易所" in str(info.value)
    await dev.aclose()


def test_the_fault_switches_offered_match_the_standin():
    assert set(FAULT_KEYS) == {"order_timeout", "algo_create_fail", "position_query_fail", "partial_fill", "stream_drop",
                               "rate_limit", "listen_key_keepalive_fail"}
