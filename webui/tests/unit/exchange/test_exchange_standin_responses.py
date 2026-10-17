"""StandinAdapter 对各种失败响应的归类（脚本化传输，不连替身）：哪些是「明确不存在」、哪些是「未知」、哪些是「被拒」。

真实替身只会给出协议里列出的那几种响应；这里用固定响应覆盖协议之外的形状（缺错误码、非 JSON、状态码不对），
确保适配层从不把「读不到」当成「没有」、不把「状态未知」当成「被拒」。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.types import (
    AlgoType,
    CancelOutcome,
    Known,
    OrderSide,
    PositionSide,
    RejectKind,
    Unknown,
    WorkingType,
    WriteAccepted,
    WriteRejected,
    WriteUnknown,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.adapter import WriteCapability
from dmr_kernel.exchange.errors import AuthConfigError
from dmr_kernel.exchange.standin import StandinAdapter
from dmr_kernel.exchange.transport import Response
from tests.support.inproc import T0_MS, default_config

D = Decimal
ORDER = {"symbol": "BTCUSDT", "client_order_id": "DKA1-1-1-o", "order_id": "9001", "side": "BUY",
         "position_side": "LONG", "type": "MARKET", "qty": "0.010", "filled_qty": "0.010", "avg_price": "50000",
         "status": "FILLED", "raw_status": "FILLED", "close_position": False, "reduce_only": False,
         "update_time_ms": T0_MS}
ALGO = {"symbol": "BTCUSDT", "client_algo_id": "DKA1-0-1-s", "algo_id": "7001", "side": "SELL",
        "position_side": "LONG", "type": "STOP_MARKET", "trigger_price": "40000", "working_type": "MARK_PRICE",
        "close_position": True, "qty": None, "status": "NEW", "actual_order_id": None, "create_time_ms": T0_MS,
        "update_time_ms": T0_MS}


class Canned:
    """每个请求都返回同一个预设响应，并记下收到的请求。"""

    def __init__(self, response: Response):
        self.response = response
        self.requests: list[tuple[str, str, dict | None, dict | None, bool]] = []

    async def request(self, method, path, *, params=None, body=None, auth=True, timeout_s):
        self.requests.append((method, path, params, body, auth))
        return self.response

    async def ws_connect(self, path, *, params, timeout_s):
        raise AssertionError("这些测试不该建数据流")

    async def close(self):
        return None


def adapter_for(status, body, headers=None):
    transport = Canned(Response(status, body, headers or {}))
    cap = WriteCapability("coordinator")
    cfg = default_config()
    adapter = StandinAdapter(transport, capability=cap, bus=EventBus(), clock=ManualClock(T0_MS),
                             config_get=cfg.__getitem__, request_timeout_s=0.2)
    return adapter, transport, cap


def cid(n, leg="o", model="1"):
    return ids.make_client_id("A1", model, n, leg)


async def buy(adapter, cap, qty=D("0.01")):
    return await adapter.create_market_order(cap, symbol="BTCUSDT", side=OrderSide.BUY,
                                             position_side=PositionSide.LONG, qty=qty, client_order_id=cid(1))


# ================================================================ 回查：只有 400 + −2013 才是「明确不存在」

LOOKUPS = [
    ("fetch_order", {"client_order_id": "DKA1-1-1-o"}),
    ("fetch_algo_order", {"client_algo_id": "DKA1-0-1-s"}),
]


@pytest.mark.parametrize("method, kwargs", LOOKUPS)
@pytest.mark.parametrize("status, body, absent", [
    (400, {"code": -2013, "msg": "Order does not exist."}, True),
    (400, {"code": -1102, "msg": "Mandatory parameter was not sent."}, False),
    (400, {"code": -1121, "msg": "Invalid symbol."}, False),
    (400, {"code": -2011, "msg": "Unknown order sent."}, False),
    (400, {"msg": "no code"}, False),
    (400, "<html>bad request</html>", False),
    (404, {"code": -2013, "msg": "Order does not exist."}, False),
    (500, {"code": -2013, "msg": "x"}, False),
    (503, {"code": -1007, "msg": "Timeout waiting for response from backend server."}, False),
    (429, {"code": -1003, "msg": "Too many requests"}, False),
])
async def test_only_a_400_with_minus_2013_means_the_order_does_not_exist(method, kwargs, status, body, absent):
    adapter, _, _ = adapter_for(status, body)
    result = await getattr(adapter, method)("BTCUSDT", **kwargs)
    if absent:
        assert result == Known(None)
    else:
        assert isinstance(result, Unknown) and result.http_status == status


@pytest.mark.parametrize("method, kwargs", LOOKUPS)
@pytest.mark.parametrize("status", [401, 403])
async def test_lookup_with_a_rejected_token_raises_auth_config_error(method, kwargs, status):
    adapter, _, _ = adapter_for(status, {"code": -2015, "msg": "Invalid key"})
    with pytest.raises(AuthConfigError):
        await getattr(adapter, method)("BTCUSDT", **kwargs)


# ================================================================ 撤单：−2011 才是「不存在」，其余未知

@pytest.mark.parametrize("status, body, outcome", [
    (200, ORDER, CancelOutcome.CANCELLED),
    (400, {"code": -2011, "msg": "Unknown order sent."}, CancelOutcome.NOT_FOUND),
    (400, {"code": -2013, "msg": "Order does not exist."}, CancelOutcome.UNKNOWN),
    (400, {"code": -1102, "msg": "x"}, CancelOutcome.UNKNOWN),
    (400, {"msg": "no code"}, CancelOutcome.UNKNOWN),
    (404, {"code": -2011, "msg": "x"}, CancelOutcome.UNKNOWN),
    (429, {"code": -1003, "msg": "x"}, CancelOutcome.UNKNOWN),
    (500, "<html>oops</html>", CancelOutcome.UNKNOWN),
    (503, {"code": -1007, "msg": "x"}, CancelOutcome.UNKNOWN),
    (200, {"unexpected": "shape"}, CancelOutcome.UNKNOWN),
])
async def test_cancel_outcomes_by_response_shape(status, body, outcome):
    adapter, transport, cap = adapter_for(status, body)
    result = await adapter.cancel_order(cap, symbol="BTCUSDT", client_order_id=cid(1))
    assert result.outcome is outcome
    assert transport.requests[0][:2] == ("DELETE", "/api/private/order")


@pytest.mark.parametrize("status, body, outcome", [
    (200, {**ALGO, "status": "CANCELED"}, CancelOutcome.CANCELLED),
    (400, {"code": -2011, "msg": "Unknown order sent."}, CancelOutcome.NOT_FOUND),
    (400, {"code": -2013, "msg": "x"}, CancelOutcome.UNKNOWN),
    (503, {"code": -1007, "msg": "x"}, CancelOutcome.UNKNOWN),
])
async def test_algo_cancel_outcomes_by_response_shape(status, body, outcome):
    adapter, _, cap = adapter_for(status, body)
    result = await adapter.cancel_algo_order(cap, symbol="BTCUSDT", client_algo_id=cid(1, "s", "0"))
    assert result.outcome is outcome


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_token_raises_auth_config_error_for_writes_and_cancels_too(status):
    adapter, _, cap = adapter_for(status, {"code": -2015, "msg": "Invalid key"})
    with pytest.raises(AuthConfigError):
        await buy(adapter, cap)
    with pytest.raises(AuthConfigError):
        await adapter.cancel_order(cap, symbol="BTCUSDT", client_order_id=cid(1))
    with pytest.raises(AuthConfigError):
        await adapter.fetch_positions()


# ================================================================ 限频：缺错误码时按 −1003

@pytest.mark.parametrize("status, body", [(429, {"msg": "Too many requests"}), (418, "banned"), (429, None)])
async def test_a_rate_limit_without_an_error_code_is_still_minus_1003(status, body):
    adapter, _, cap = adapter_for(status, body, {"Retry-After": "2"})
    read = await adapter.fetch_positions()
    assert isinstance(read, Unknown) and read.code == -1003 and read.http_status == status
    write = await buy(adapter, cap)
    assert isinstance(write, WriteRejected) and write.kind is RejectKind.RATE_LIMITED
    assert write.code == -1003 and write.http_status == status


async def test_a_rate_limit_that_carries_its_own_code_keeps_it():
    adapter, _, cap = adapter_for(429, {"code": -1015, "msg": "Too many orders"})
    read = await adapter.fetch_account()
    assert isinstance(read, Unknown) and read.code == -1015
    write = await buy(adapter, cap)
    assert isinstance(write, WriteRejected) and write.kind is RejectKind.RATE_LIMITED and write.code == -1015


# ================================================================ 写：5xx 与未知代码

@pytest.mark.parametrize("status, body", [
    (503, {"code": -1007, "msg": "Timeout waiting for response from backend server."}),
    (500, {"code": -1001, "msg": "Internal error"}),
    (502, "<html>bad gateway</html>"),
    (200, "<html>not json</html>"),
    (200, {"unexpected": "shape"}),
])
async def test_status_unknown_responses_are_write_unknown_never_rejected(status, body):
    adapter, _, cap = adapter_for(status, body)
    assert isinstance(await buy(adapter, cap), WriteUnknown)


async def test_a_plain_400_without_a_known_code_is_rejected_as_other():
    adapter, _, cap = adapter_for(400, {"code": -9999, "msg": "something new"})
    result = await buy(adapter, cap)
    assert isinstance(result, WriteRejected) and result.kind is RejectKind.OTHER and result.code == -9999
    assert result.http_status == 400


# ================================================================ 持仓：只返回非零；请求体：普通小数

async def test_positions_with_zero_quantity_are_dropped_from_the_result():
    flat = {"symbol": "ETHUSDT", "position_side": "SHORT", "qty": "0", "entry_price": "0", "mark_price": "3000",
            "liquidation_price": "0", "unrealized_pnl": "0", "leverage": 20, "margin_mode": "cross",
            "update_time_ms": 1}
    held = {**flat, "symbol": "BTCUSDT", "position_side": "LONG", "qty": "0.01", "entry_price": "50000"}
    adapter, _, _ = adapter_for(200, [flat, held])
    result = await adapter.fetch_positions()
    assert isinstance(result, Known) and [(p.symbol, p.side) for p in result.value] == [("BTCUSDT", PositionSide.LONG)]
    adapter, _, _ = adapter_for(200, [flat])  # 全是零：已知的空，不是未知
    assert await adapter.fetch_positions() == Known([])


async def test_numbers_are_sent_as_plain_decimals_never_in_scientific_notation():
    adapter, transport, cap = adapter_for(200, ORDER)
    await buy(adapter, cap, qty=D("1E-7"))
    assert transport.requests[-1][3]["qty"] == "0.0000001"
    adapter, transport, cap = adapter_for(200, ALGO)
    await adapter.create_stop_order(cap, symbol="BTCUSDT", side=OrderSide.SELL, position_side=PositionSide.LONG,
                                    trigger_price=D("1E+5"), working_type=WorkingType.MARK_PRICE,
                                    client_algo_id=cid(1, "s", "0"), close_position=False, qty=D("2.50E+1"),
                                    algo_type=AlgoType.TAKE_PROFIT_MARKET)
    body = transport.requests[-1][3]
    assert body["trigger_price"] == "100000" and body["qty"] == "25.0"
    assert body["close_position"] is False and body["type"] == "TAKE_PROFIT_MARKET"


async def test_an_empty_ticker_request_is_an_empty_known_without_asking_the_exchange():
    adapter, transport, _ = adapter_for(200, {})
    assert await adapter.fetch_tickers([]) == Known({})
    assert transport.requests == []
    await adapter.fetch_tickers(None)
    assert transport.requests[-1][:2] == ("GET", "/api/public/tickers") and transport.requests[-1][2] is None
    await adapter.fetch_tickers(["BTCUSDT", "币安人生USDT"])
    assert transport.requests[-1][2] == {"symbols": "BTCUSDT,币安人生USDT"}


async def test_accepted_writes_are_parsed_from_a_200():
    adapter, _, cap = adapter_for(200, ORDER)
    result = await buy(adapter, cap)
    assert isinstance(result, WriteAccepted) and result.order.exchange_order_id == "9001"
