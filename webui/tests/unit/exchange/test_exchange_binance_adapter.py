"""BinanceAdapter：本轮只有公共行情与合约规则（委托 BinancePublicClient）；私有读写与数据流一律抛 NotImplementedError。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.types import (
    AlgoType,
    Known,
    OrderSide,
    PositionSide,
    Unknown,
    WorkingType,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.adapter import ExchangeAdapter, WriteCapability
from dmr_kernel.exchange.binance import PRIVATE_NOT_READY, BinanceAdapter
from dmr_kernel.exchange.binance_public import BinancePublicClient

D = Decimal


class StubPublic:
    """顶替 BinancePublicClient：记录调用，返回预设结果。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.closed = 0
        self.results = {
            "load_instruments": Known(["inst"]), "fetch_candles": Known(["candle"]),
            "fetch_tickers": Known({"BTCUSDT": "tick"}), "server_time": Known(1234),
        }

    async def load_instruments(self):
        self.calls.append(("load_instruments",))
        return self.results["load_instruments"]

    async def fetch_candles(self, symbol, interval, limit=99):
        self.calls.append(("fetch_candles", symbol, interval, limit))
        return self.results["fetch_candles"]

    async def fetch_tickers(self, symbols=None):
        self.calls.append(("fetch_tickers", symbols))
        return self.results["fetch_tickers"]

    async def server_time(self):
        self.calls.append(("server_time",))
        return self.results["server_time"]

    async def close(self):
        self.closed += 1


def adapter_with_stub(**kw):
    stub = StubPublic()
    return BinanceAdapter(public=stub, **kw), stub


def test_it_is_an_exchange_adapter_that_can_be_instantiated():
    adapter, _ = adapter_with_stub()
    assert isinstance(adapter, ExchangeAdapter)
    assert adapter.used_weight() is None


@pytest.mark.parametrize("capability", [WriteCapability("coordinator"), WriteCapability("emergency")])
def test_constructing_with_a_write_capability_is_refused(capability):
    with pytest.raises(NotImplementedError) as caught:
        BinanceAdapter(capability=capability, public=StubPublic())
    assert str(caught.value) == PRIVATE_NOT_READY
    assert "Demo Trading" in PRIVATE_NOT_READY and "16 项实测" in PRIVATE_NOT_READY


# ================================================================ 公共读方法：委托


async def test_public_reads_delegate_to_the_public_client_and_return_its_result_unchanged():
    adapter, stub = adapter_with_stub()
    assert await adapter.server_time() == Known(1234)
    assert await adapter.load_instruments() == Known(["inst"])
    assert await adapter.fetch_candles("BTCUSDT", "15m", 50) == Known(["candle"])
    assert await adapter.fetch_candles("BTCUSDT", "15m") == Known(["candle"])
    assert await adapter.fetch_tickers(["BTCUSDT"]) == Known({"BTCUSDT": "tick"})
    assert await adapter.fetch_tickers() == Known({"BTCUSDT": "tick"})
    assert stub.calls == [("server_time",), ("load_instruments",), ("fetch_candles", "BTCUSDT", "15m", 50),
                          ("fetch_candles", "BTCUSDT", "15m", 99), ("fetch_tickers", ["BTCUSDT"]),
                          ("fetch_tickers", None)]


async def test_a_failing_public_read_stays_unknown():
    adapter, stub = adapter_with_stub()
    unknown = Unknown("交易所不可用", code=-1001, http_status=503)
    for name in stub.results:
        stub.results[name] = unknown
    assert await adapter.server_time() is unknown
    assert await adapter.load_instruments() is unknown
    assert await adapter.fetch_candles("BTCUSDT", "15m") is unknown
    assert await adapter.fetch_tickers(None) is unknown


# ================================================================ 私有读方法与数据流：一律抛


PRIVATE_READS = [
    ("fetch_position_mode", (), {}),
    ("fetch_account", (), {}),
    ("fetch_positions", (), {}),
    ("fetch_open_orders", (), {}),
    ("fetch_open_orders", ("BTCUSDT",), {}),
    ("fetch_open_algo_orders", (), {}),
    ("fetch_order", ("BTCUSDT",), {"client_order_id": "DKA1-1-1-o"}),
    ("fetch_algo_order", ("BTCUSDT",), {"client_algo_id": "DKA1-0-1-s"}),
    ("fetch_trades", ("BTCUSDT", 1), {}),
    ("fetch_income", (1,), {}),
    ("fetch_leverage", ("BTCUSDT",), {}),
    ("fetch_restrictions", (), {}),
]


@pytest.mark.parametrize("name, args, kwargs", PRIVATE_READS)
async def test_every_private_read_raises_not_implemented(name, args, kwargs):
    adapter, stub = adapter_with_stub()
    with pytest.raises(NotImplementedError) as caught:
        await getattr(adapter, name)(*args, **kwargs)
    assert str(caught.value) == PRIVATE_NOT_READY
    assert stub.calls == []  # 绝不去碰公共客户端


def test_private_reads_cover_every_private_read_method_of_the_interface():
    public = {"server_time", "load_instruments", "fetch_candles", "fetch_tickers"}
    reads = {n for n in dir(ExchangeAdapter) if n.startswith("fetch_") or n in ("server_time", "load_instruments")}
    assert reads - public == {name for name, _, _ in PRIVATE_READS}


def test_account_stream_raises_not_implemented():
    adapter, _ = adapter_with_stub()
    with pytest.raises(NotImplementedError) as caught:
        adapter.account_stream()
    assert str(caught.value) == PRIVATE_NOT_READY


# ================================================================ 写方法：一律抛


def sid(n, leg="o", model="1"):
    return ids.make_client_id("A1", model, n, leg)


def writes(adapter, cap):
    return [
        adapter.create_market_order(cap, symbol="BTCUSDT", side=OrderSide.BUY, position_side=PositionSide.LONG,
                                    qty=D("0.01"), client_order_id=sid(1)),
        adapter.create_stop_order(cap, symbol="BTCUSDT", side=OrderSide.SELL, position_side=PositionSide.LONG,
                                  trigger_price=D("40000"), working_type=WorkingType.MARK_PRICE,
                                  client_algo_id=sid(1, "s", "0"), close_position=True, qty=None,
                                  algo_type=AlgoType.STOP_MARKET),
        adapter.cancel_order(cap, symbol="BTCUSDT", client_order_id=sid(1)),
        adapter.cancel_algo_order(cap, symbol="BTCUSDT", client_algo_id=sid(1, "s", "0")),
        adapter.set_leverage(cap, symbol="BTCUSDT", leverage=5),
        adapter.set_margin_mode(cap, symbol="BTCUSDT", margin_mode="isolated"),
    ]


@pytest.mark.parametrize("cap", [None, WriteCapability("coordinator"), WriteCapability("emergency")])
async def test_every_write_method_raises_not_implemented_whatever_the_capability(cap):
    adapter, stub = adapter_with_stub()
    for coro in writes(adapter, cap):
        with pytest.raises(NotImplementedError) as caught:
            await coro
        assert str(caught.value) == PRIVATE_NOT_READY
    assert stub.calls == []


async def test_writes_stay_refused_even_in_read_only_shadow_mode():
    adapter, _ = adapter_with_stub(writes_disabled=True)
    for coro in writes(adapter, None):
        with pytest.raises(NotImplementedError):
            await coro


# ================================================================ 关闭与默认构造


async def test_close_delegates_to_the_public_client():
    adapter, stub = adapter_with_stub()
    await adapter.close()
    assert stub.closed == 1


async def test_default_construction_builds_a_real_public_client_without_touching_the_network():
    adapter = BinanceAdapter()
    assert isinstance(adapter._public, BinancePublicClient)
    await adapter.close()
