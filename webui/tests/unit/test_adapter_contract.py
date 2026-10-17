"""exchange.adapter：写方法出口的强制校验（写权限令牌、只读影子禁用写、必须带系统编号）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core import types as t
from dmr_kernel.exchange import adapter as ad
from dmr_kernel.exchange import ids


class _ReadStubs:
    """读方法不在本测试范围：用抛 NotImplementedError 的桩补齐抽象方法（生产代码的基类保持严格）。"""


def _not_implemented(self, *args, **kwargs):
    raise NotImplementedError


for _name in [n for n in ad.ExchangeAdapter.__abstractmethods__ if not n.startswith("_")]:
    setattr(_ReadStubs, _name, _not_implemented)


class RecordingAdapter(_ReadStubs, ad.ExchangeAdapter):
    """只实现写方法的内部实现，记录被调用的次数。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = []

    async def _create_market_order(self, **kw):
        self.calls.append(("market", kw))
        return t.WriteAccepted()

    async def _create_stop_order(self, **kw):
        self.calls.append(("stop", kw))
        return t.WriteAccepted()

    async def _cancel_order(self, **kw):
        self.calls.append(("cancel", kw))
        return t.CancelResult(t.CancelOutcome.CANCELLED)

    async def _cancel_algo_order(self, **kw):
        self.calls.append(("cancel_algo", kw))
        return t.CancelResult(t.CancelOutcome.CANCELLED)

    async def _set_leverage(self, **kw):
        self.calls.append(("lev", kw))
        return t.WriteAccepted()

    async def _set_margin_mode(self, **kw):
        self.calls.append(("mm", kw))
        return t.WriteAccepted()


CID = ids.make_client_id("A1", "1", 7, "o")


def market_kwargs(cid=CID):
    return dict(symbol="BTCUSDT", side=t.OrderSide.BUY, position_side=t.PositionSide.LONG,
                qty=Decimal("0.001"), client_order_id=cid)


async def test_write_with_the_assembled_capability_reaches_the_implementation():
    cap = ad.WriteCapability("coordinator")
    a = RecordingAdapter(capability=cap)
    assert isinstance(await a.create_market_order(cap, **market_kwargs()), t.WriteAccepted)
    assert a.calls and a.calls[0][0] == "market"


async def test_write_without_or_with_a_foreign_capability_is_refused():
    cap = ad.WriteCapability("coordinator")
    a = RecordingAdapter(capability=cap)
    with pytest.raises(ad.CapabilityError):
        await a.create_market_order(None, **market_kwargs())
    with pytest.raises(ad.CapabilityError):
        await a.create_market_order(ad.WriteCapability("someone-else"), **market_kwargs())
    assert a.calls == []


async def test_adapter_built_for_reads_only_refuses_every_write():
    a = RecordingAdapter(capability=None)
    with pytest.raises(ad.CapabilityError):
        await a.create_market_order(ad.WriteCapability("x"), **market_kwargs())


@pytest.mark.parametrize("cid", [None, "", "web_123", "x-cvBPrNm9abc", "A" * 37])
async def test_writes_must_carry_a_system_client_id(cid):
    cap = ad.WriteCapability("coordinator")
    a = RecordingAdapter(capability=cap)
    with pytest.raises(ad.ClientOrderIdError):
        await a.create_market_order(cap, **market_kwargs(cid))
    with pytest.raises(ad.ClientOrderIdError):
        await a.create_stop_order(cap, symbol="BTCUSDT", side=t.OrderSide.SELL,
                                  position_side=t.PositionSide.LONG, trigger_price=Decimal("1"),
                                  working_type=t.WorkingType.CONTRACT_PRICE, client_algo_id=cid)
    assert a.calls == []


async def test_emergency_prefix_is_accepted_for_the_emergency_capability_only():
    cap = ad.WriteCapability("emergency")
    a = RecordingAdapter(capability=cap)
    eid = ids.make_emergency_id(1791949893522, 1)
    await a.create_market_order(cap, **market_kwargs(eid))
    coord = ad.WriteCapability("coordinator")
    b = RecordingAdapter(capability=coord)
    with pytest.raises(ad.ClientOrderIdError):
        await b.create_market_order(coord, **market_kwargs(eid))


async def test_shadow_mode_disables_all_writes_at_the_exit():
    cap = ad.WriteCapability("coordinator")
    a = RecordingAdapter(capability=cap, writes_disabled=True)
    r = await a.create_market_order(cap, **market_kwargs())
    assert isinstance(r, t.WriteRejected) and r.kind is t.RejectKind.WRITES_DISABLED
    c = await a.cancel_algo_order(cap, symbol="BTCUSDT", client_algo_id=CID)
    assert c.outcome is t.CancelOutcome.UNKNOWN
    assert a.calls == []


async def test_non_positive_quantities_and_prices_are_programming_errors():
    cap = ad.WriteCapability("coordinator")
    a = RecordingAdapter(capability=cap)
    with pytest.raises(ValueError):
        await a.create_market_order(cap, **dict(market_kwargs(), qty=Decimal("0")))
    with pytest.raises(ValueError):
        await a.create_stop_order(cap, symbol="BTCUSDT", side=t.OrderSide.SELL,
                                  position_side=t.PositionSide.LONG, trigger_price=Decimal("0"),
                                  working_type=t.WorkingType.CONTRACT_PRICE, client_algo_id=CID)


async def test_close_position_stop_must_not_carry_quantity():
    cap = ad.WriteCapability("coordinator")
    a = RecordingAdapter(capability=cap)
    with pytest.raises(ValueError):
        await a.create_stop_order(cap, symbol="BTCUSDT", side=t.OrderSide.SELL,
                                  position_side=t.PositionSide.LONG, trigger_price=Decimal("1"),
                                  working_type=t.WorkingType.CONTRACT_PRICE, client_algo_id=CID,
                                  close_position=True, qty=Decimal("1"))


def test_reads_are_abstract():
    for name in ("server_time", "load_instruments", "fetch_position_mode", "fetch_account",
                 "fetch_positions", "fetch_open_orders", "fetch_open_algo_orders", "fetch_order",
                 "fetch_algo_order", "fetch_trades", "fetch_income", "fetch_candles",
                 "fetch_tickers", "fetch_leverage", "fetch_restrictions", "account_stream"):
        assert hasattr(ad.ExchangeAdapter, name), name


async def test_account_stream_check_liveness_has_a_default_noop():
    class Minimal(ad.AccountStream):
        async def start(self): ...
        async def stop(self): ...
        @property
        def state(self): return "stopped"
        def items(self): return iter(())
    assert await Minimal().check_liveness() is None
