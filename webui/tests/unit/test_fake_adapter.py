"""tests/support/fake_adapter.py 自身的行为（其它单元测试都依赖它）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core import types as t
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.adapter import CapabilityError, WriteCapability
from tests.support.fake_adapter import FakeAdapter


async def test_unset_reads_are_unknown_not_empty():
    a = FakeAdapter()
    r = await a.fetch_positions()
    assert isinstance(r, t.Unknown)


async def test_preset_values_and_callables():
    a = FakeAdapter()
    a.set_read("fetch_positions", [])
    a.set_read("fetch_order", lambda **kw: t.Known(None) if kw["client_order_id"] == "x" else t.Unknown("boom"))
    assert (await a.fetch_positions()) == t.Known([])
    assert (await a.fetch_order("BTCUSDT", client_order_id="x")) == t.Known(None)
    assert isinstance(await a.fetch_order("BTCUSDT", client_order_id="y"), t.Unknown)
    assert a.calls("fetch_order")[0]["client_order_id"] == "x"


async def test_writes_go_through_base_checks_and_queue():
    cap = WriteCapability("coordinator")
    a = FakeAdapter(capability=cap)
    rej = t.WriteRejected(t.RejectKind.IMMEDIATE_TRIGGER, -2021, "Order would immediately trigger.")
    a.push_write_result("create_stop_order", rej)
    cid = ids.make_client_id("A1", "0", 3, "s")
    r = await a.create_stop_order(cap, symbol="BTCUSDT", side=t.OrderSide.SELL, position_side=t.PositionSide.LONG,
                                  trigger_price=Decimal("100"), working_type=t.WorkingType.CONTRACT_PRICE,
                                  client_algo_id=cid)
    assert r is rej
    assert a.write_calls("create_stop_order")[0]["client_algo_id"] == cid
    with pytest.raises(CapabilityError):
        await a.create_market_order(None, symbol="BTCUSDT", side=t.OrderSide.BUY, position_side=t.PositionSide.LONG,
                                    qty=Decimal("1"), client_order_id=cid)
