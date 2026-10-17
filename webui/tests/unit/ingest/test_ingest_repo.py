"""ledger/repo_ingest.py 的仓储函数（直接经账本调用）：认领、去重、撞号、分块查询、水位、收益流水。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.types import FillSource, OrderOrigin, OrderState
from dmr_kernel.ledger import repo_ingest

D = Decimal


async def record(ledger, t, *, client_order_id="web_k", origin=OrderOrigin.EXTERNAL, source=FillSource.EXTERNAL,
                 path="rest", view=None):
    return await ledger.write(repo_ingest.record_trade, trade=t, client_order_id=client_order_id, origin=origin,
                              source=source, path=path, now_ms=1, view=view)


# ---------------------------------------------------------------- lookup

async def test_lookup_reports_duplicates_claims_and_siblings(ledger, acked_order, trade):
    order = await acked_order(exchange_order_id="9001")
    lookup = repo_ingest.lookup

    # 新成交，订单表里按交易所订单号认领
    dup, claimed, sibling = await ledger.read(lookup, symbol="BTCUSDT", trade_id="T1", client_order_id=None,
                                              exchange_order_id="9001")
    assert (dup, claimed.client_order_id, sibling) == (False, order.client_order_id, None)

    # 认领不到、也没有编号：沿用同一张交易所订单已入账成交里记的编号
    await record(ledger, trade(trade_id="E1", exchange_order_id="123"), client_order_id="web_q")
    dup, claimed, sibling = await ledger.read(lookup, symbol="BTCUSDT", trade_id="E2", client_order_id=None,
                                              exchange_order_id="123")
    assert (dup, claimed, sibling) == (False, None, "web_q")

    # 调用方给了编号：不用沿用；别的合约上相同的订单号也不沿用
    assert await ledger.read(lookup, symbol="BTCUSDT", trade_id="E3", client_order_id="web_other",
                             exchange_order_id="123") == (False, None, None)
    assert await ledger.read(lookup, symbol="ETHUSDT", trade_id="E4", client_order_id=None,
                             exchange_order_id="123") == (False, None, None)

    # 已入账的成交号
    assert await ledger.read(lookup, symbol="BTCUSDT", trade_id="E1", client_order_id=None,
                             exchange_order_id="123") == (True, None, None)


async def test_a_sibling_without_a_client_id_is_not_a_sibling(ledger, trade):
    await record(ledger, trade(trade_id="U1", exchange_order_id="555"), client_order_id=None,
                 origin=OrderOrigin.UNKNOWN)
    assert await ledger.read(repo_ingest.lookup, symbol="BTCUSDT", trade_id="U2", client_order_id=None,
                             exchange_order_id="555") == (False, None, None)


# ---------------------------------------------------------------- 成交号撞号与分块

async def test_known_trade_ids_respects_the_symbol_collision_rule(ledger, trade):
    btc = trade(trade_id="777", symbol="BTCUSDT", exchange_order_id="1")
    eth = trade(trade_id="777", symbol="ETHUSDT", exchange_order_id="2")
    assert await record(ledger, btc) is not None
    assert await ledger.read(repo_ingest.known_trade_ids, "BTCUSDT", ["777"]) == {"777"}
    assert await ledger.read(repo_ingest.known_trade_ids, "ETHUSDT", ["777"]) == set()  # 另一个合约的同号成交不算重复
    assert await record(ledger, eth) is not None  # 撞号：存成「合约:成交号」
    assert await ledger.read(repo_ingest.known_trade_ids, "ETHUSDT", ["777"]) == {"777"}
    assert await record(ledger, eth) is None and await record(ledger, btc) is None


async def test_known_trade_ids_over_several_chunks(ledger, trade, monkeypatch):
    monkeypatch.setattr(repo_ingest, "_CHUNK", 3)
    for n in range(7):
        await record(ledger, trade(trade_id=f"K{n}", exchange_order_id=str(n)))
    asked = [f"K{n}" for n in range(10)] + ["K1"]  # 含重复的询问
    assert await ledger.read(repo_ingest.known_trade_ids, "BTCUSDT", asked) == {f"K{n}" for n in range(7)}
    assert await ledger.read(repo_ingest.known_trade_ids, "BTCUSDT", []) == set()


# ---------------------------------------------------------------- record_trade 的返回值

async def test_record_trade_returns_the_row_and_the_order_before_and_after(ledger, acked_order, trade, order_info):
    order = await acked_order(qty="1")
    fill, before, after = await record(ledger, trade(order, trade_id="R1", qty="1", price="100"),
                                       client_order_id=order.client_order_id,
                                       view=order_info(order, status="FILLED", filled="1", avg="100"))
    assert (fill.trade_id, fill.client_order_id, fill.origin, fill.source) == ("R1", order.client_order_id,
                                                                              OrderOrigin.SYSTEM, FillSource.SYSTEM)
    assert before == order and after.state is OrderState.FILLED and after.raw_status == "FILLED"


async def test_a_claim_found_at_write_time_overrides_a_stale_classification(ledger, acked_order, trade):
    order = await acked_order(qty="1", exchange_order_id="9001")
    fill, before, after = await record(ledger, trade(order, exchange_order_id="9001"), client_order_id=None,
                                       origin=OrderOrigin.UNKNOWN, source=FillSource.EXTERNAL)
    assert (fill.origin, fill.source, fill.intent_id, fill.client_order_id) == (
        OrderOrigin.SYSTEM, FillSource.SYSTEM, order.intent_id, order.client_order_id)
    assert before is not None and after.filled_qty == D("0.5")


async def test_unclaimed_trades_return_no_order_records(ledger, trade):
    fill, before, after = await record(ledger, trade(trade_id="X1"))
    assert (before, after) == (None, None) and fill.intent_id is None


# ---------------------------------------------------------------- 订单事件的认领范围

async def test_order_events_only_claim_within_the_same_symbol(ledger, acked_order, order_info, stranger):
    await acked_order(exchange_order_id="9001", symbol="BTCUSDT")
    view = order_info(stranger("web_o", symbol="ETHUSDT"), status="CANCELED", exchange_order_id="9001")
    assert await ledger.write(repo_ingest.merge_order_event, view, now_ms=1) is None


async def test_algo_events_only_claim_within_the_same_symbol(ledger, acked_stop, algo_info, stranger):
    await acked_stop(algo_id="5001", symbol="BTCUSDT")
    foreign = stranger("user_stop", symbol="ETHUSDT")
    view = algo_info(foreign, raw="CANCELED", algo_id="5001", client_algo_id="user_stop")
    assert await ledger.write(repo_ingest.merge_algo_event, view, now_ms=1) is None


async def test_algo_events_find_our_stop_by_algo_id_when_the_client_id_is_missing(ledger, acked_stop, algo_info,
                                                                                  orders):
    stop = await acked_stop(algo_id="5001")
    view = algo_info(stop, raw="CANCELED", algo_id="5001", client_algo_id=None)
    before, after = await ledger.write(repo_ingest.merge_algo_event, view, now_ms=1)
    assert (before.client_order_id, after.state) == (stop.client_order_id, OrderState.CANCELLED)


# ---------------------------------------------------------------- 水位

async def test_watermarks_only_move_forward_and_are_per_key(ledger):
    assert await ledger.read(repo_ingest.trade_watermark, "BTCUSDT") is None
    await ledger.write(repo_ingest.set_trade_watermark, "BTCUSDT", 100)
    await ledger.write(repo_ingest.set_trade_watermark, "BTCUSDT", 50)  # 不后退
    await ledger.write(repo_ingest.set_trade_watermark, "BTCUSDT", 100)
    assert await ledger.read(repo_ingest.trade_watermark, "BTCUSDT") == 100
    assert await ledger.read(repo_ingest.trade_watermark, "ETHUSDT") is None
    await ledger.write(repo_ingest.set_watermark, repo_ingest.INCOME_WM_KEY, 7)
    await ledger.write(repo_ingest.set_watermark, repo_ingest.INCOME_WM_KEY, 3)
    assert await ledger.read(repo_ingest.get_watermark, repo_ingest.INCOME_WM_KEY) == 7


# ---------------------------------------------------------------- 查询

async def test_list_fills_filters_and_orders_by_arrival(ledger, acked_order, trade):
    order = await acked_order(qty="1")
    await record(ledger, trade(order, trade_id="A"), client_order_id=order.client_order_id)
    await record(ledger, trade(trade_id="B", symbol="ETHUSDT", exchange_order_id="2"))
    await record(ledger, trade(trade_id="C", exchange_order_id="3"))
    ids_of = lambda rows: [r.trade_id for r in rows]  # noqa: E731
    assert ids_of(await ledger.read(repo_ingest.list_fills)) == ["A", "B", "C"]
    assert ids_of(await ledger.read(repo_ingest.list_fills, symbol="ETHUSDT")) == ["B"]
    assert ids_of(await ledger.read(repo_ingest.list_fills, client_order_id=order.client_order_id)) == ["A"]
    assert ids_of(await ledger.read(repo_ingest.list_fills, intent_id=order.intent_id)) == ["A"]
    assert await ledger.read(repo_ingest.list_fills, intent_id=999) == []


# ---------------------------------------------------------------- 收益流水

async def test_record_incomes_over_several_chunks_returns_only_the_new_ones(ledger, income, monkeypatch):
    monkeypatch.setattr(repo_ingest, "_CHUNK", 2)
    first = [income(income_id=f"I{n}") for n in range(2)]
    assert len(await ledger.write(repo_ingest.record_incomes, first, now_ms=1)) == 2
    batch = first + [income(income_id=f"I{n}") for n in range(2, 6)]
    fresh = await ledger.write(repo_ingest.record_incomes, batch, now_ms=2)
    assert [r.income_id for r in fresh] == ["I2", "I3", "I4", "I5"]
    assert len(await ledger.read(repo_ingest.list_incomes)) == 6


@pytest.mark.parametrize("amount", ["0", "-0.00000001", "123456789.12345678"])
async def test_income_amounts_keep_full_decimal_precision(ledger, income, amount):
    await ledger.write(repo_ingest.record_incomes, [income(income_id="P", amount=amount)], now_ms=1)
    [row] = await ledger.read(repo_ingest.list_incomes)
    assert row.amount == D(amount)
