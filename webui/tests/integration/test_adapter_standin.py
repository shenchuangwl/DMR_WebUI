"""StandinAdapter 对进程内替身交易所（T0.4 读方法、T0.6 写方法）：解析、失败语义、方向组合、限频、超时后的回查。

全部走 ``tests/support/inproc.py``：直接调替身的分发器，不开套接字；时间用同一个 ManualClock。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core import types as t
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
from dmr_kernel.exchange.adapter import AccountStream, CapabilityError, ClientOrderIdError
from dmr_kernel.exchange.errors import AuthConfigError
from dmr_kernel.exchange.transport import Response, TransportConnectionError, TransportTimeout
from tests.support.inproc import T0_MS, make_stack

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
BUY, SELL = OrderSide.BUY, OrderSide.SELL
MINUTE = 60_000


def cid(n: int, leg: str = "o", model: str = "1") -> str:
    return ids.make_client_id("A1", model, n, leg)


async def market(s, side, position_side, qty, n, *, symbol="BTCUSDT", leg="o"):
    return await s.adapter.create_market_order(s.cap, symbol=symbol, side=side, position_side=position_side,
                                               qty=D(qty), client_order_id=cid(n, leg))


async def stop(s, n, trigger="40000", *, position_side=LONG, side=SELL, symbol="BTCUSDT", close_position=True,
               qty=None, algo_type=AlgoType.STOP_MARKET, working_type=WorkingType.CONTRACT_PRICE):
    return await s.adapter.create_stop_order(
        s.cap, symbol=symbol, side=side, position_side=position_side, trigger_price=D(trigger),
        working_type=working_type, client_algo_id=cid(n, "s", "0"), close_position=close_position,
        qty=None if qty is None else D(qty), algo_type=algo_type)


def positions_of(result):
    assert isinstance(result, Known), result
    return {(p.symbol, p.side): p for p in result.value}


# ================================================================ 读方法：解析


async def test_server_time_is_the_exchange_clock():
    s = make_stack()
    assert await s.adapter.server_time() == Known(T0_MS)
    s.clock.advance(1500)
    assert await s.adapter.server_time() == Known(T0_MS + 1500)


async def test_load_instruments_parses_rules_including_chinese_names():
    s = make_stack()
    r = await s.adapter.load_instruments()
    assert isinstance(r, Known) and all(isinstance(i, t.Instrument) for i in r.value)
    by = {i.symbol: i for i in r.value}
    assert by["BTCUSDT"].tick_size == D("0.1") and by["BTCUSDT"].step_size == D("0.001")
    assert by["BTCUSDT"].min_notional == D("50") and by["BTCUSDT"].tradable
    assert "哈基米USDT" in by and by["哈基米USDT"].base == "哈基米"
    assert "1000PEPEUSDT" in by
    assert isinstance(by["BTCUSDT"].onboard_ms, int)


async def test_position_mode_account_and_leverage():
    s = make_stack()
    assert await s.adapter.fetch_position_mode() == Known(True)
    acct = (await s.adapter.fetch_account()).value
    assert isinstance(acct, t.AccountInfo)
    assert (acct.equity, acct.wallet_balance, acct.available_balance, acct.unrealized_pnl) == \
        (D("1000"), D("1000"), D("1000"), D("0"))
    assert await s.adapter.fetch_leverage("BTCUSDT") == Known(t.LeverageInfo("BTCUSDT", 20, "cross"))
    assert await s.adapter.fetch_restrictions() == Known(t.RestrictionInfo(False, ()))


async def test_positions_are_parsed_with_liquidation_price_and_only_non_zero_ones():
    s = make_stack()
    flat = await s.adapter.fetch_positions()
    assert flat == Known([])  # 查询成功且确实没有仓：才是「已知的空」
    assert isinstance(await market(s, BUY, LONG, "0.02", 1), WriteAccepted)
    assert isinstance(await market(s, SELL, SHORT, "0.01", 2), WriteAccepted)
    pos = positions_of(await s.adapter.fetch_positions())
    assert set(pos) == {("BTCUSDT", LONG), ("BTCUSDT", SHORT)}
    long_ = pos[("BTCUSDT", LONG)]
    assert (long_.qty, long_.entry_price, long_.mark_price, long_.leverage, long_.margin_mode) == \
        (D("0.02"), D("50000"), D("50000"), 20, "cross")
    assert long_.liquidation_price == D("47750.0")  # 50000 × (1 − 1/20 + 0.5%)
    assert pos[("BTCUSDT", SHORT)].liquidation_price == D("52250.0")
    assert all(isinstance(p.qty, Decimal) and p.qty > 0 for p in pos.values())


async def test_open_orders_and_open_algo_orders_with_symbol_filter():
    s = make_stack()
    s.disp.switches.update({"partial_fill": {"enabled": True, "ratio": "0.5", "remainder": "later", "delay_s": 600}})
    await market(s, BUY, LONG, "0.02", 1)  # 成交一半，余量挂着
    await market(s, BUY, LONG, "0.02", 5, symbol="ETHUSDT")  # 第二个合约，用来验证按合约过滤
    s.disp.switches.reset()
    await stop(s, 2)
    await stop(s, 3, symbol="ETHUSDT", trigger="2000")
    orders = (await s.adapter.fetch_open_orders()).value
    assert {(o.symbol, o.status) for o in orders} == {("BTCUSDT", "PARTIALLY_FILLED"), ("ETHUSDT", "PARTIALLY_FILLED")}
    btc_orders = (await s.adapter.fetch_open_orders("BTCUSDT")).value
    assert [(o.client_order_id, o.filled_qty, o.qty) for o in btc_orders] == [(cid(1), D("0.01"), D("0.02"))]
    algos = (await s.adapter.fetch_open_algo_orders()).value
    assert {(a.symbol, a.client_algo_id) for a in algos} == {("BTCUSDT", cid(2, "s", "0")), ("ETHUSDT", cid(3, "s", "0"))}
    only_eth = (await s.adapter.fetch_open_algo_orders("ETHUSDT")).value
    assert [a.symbol for a in only_eth] == ["ETHUSDT"] and only_eth[0].is_open and only_eth[0].qty is None


async def test_fetch_order_by_client_id_and_by_exchange_id():
    s = make_stack()
    placed = (await market(s, BUY, LONG, "0.01", 1)).order
    by_client = await s.adapter.fetch_order("BTCUSDT", client_order_id=cid(1))
    by_exchange = await s.adapter.fetch_order("BTCUSDT", exchange_order_id=placed.exchange_order_id)
    assert by_client == by_exchange == Known(placed)
    assert placed.status == "FILLED" and placed.filled_qty == D("0.01") and placed.avg_price == D("50000")


async def test_missing_orders_are_known_none_while_failures_are_unknown():
    s = make_stack()
    assert await s.adapter.fetch_order("BTCUSDT", client_order_id=cid(99)) == Known(None)
    assert await s.adapter.fetch_algo_order("BTCUSDT", client_algo_id=cid(99, "s", "0")) == Known(None)
    assert await s.adapter.fetch_algo_order("BTCUSDT", algo_id="123456") == Known(None)
    s.disp.switches.update({"rate_limit": {"enabled": True, "http": 429, "scope": "reads"}})
    r = await s.adapter.fetch_order("BTCUSDT", client_order_id=cid(99))
    assert isinstance(r, Unknown) and r.http_status == 429  # 限频不能被当成「不存在」


async def test_fetch_order_requires_an_identifier():
    s = make_stack()
    with pytest.raises(ValueError):
        await s.adapter.fetch_order("BTCUSDT")
    with pytest.raises(ValueError):
        await s.adapter.fetch_algo_order("BTCUSDT")


async def test_fetch_algo_order_by_both_ids():
    s = make_stack()
    algo = (await stop(s, 1)).algo
    assert await s.adapter.fetch_algo_order("BTCUSDT", client_algo_id=cid(1, "s", "0")) == Known(algo)
    assert await s.adapter.fetch_algo_order("BTCUSDT", algo_id=algo.algo_id) == Known(algo)


async def test_trades_have_no_client_order_id_and_link_by_exchange_order_id():
    s = make_stack()
    placed = (await market(s, BUY, LONG, "0.01", 1)).order
    r = await s.adapter.fetch_trades("BTCUSDT", T0_MS - 1000)
    assert isinstance(r, Known) and len(r.value) == 1
    trade = r.value[0]
    assert isinstance(trade, t.TradeInfo) and trade.exchange_order_id == placed.exchange_order_id
    assert (trade.side, trade.position_side, trade.qty, trade.price) == (BUY, LONG, D("0.01"), D("50000"))
    assert trade.commission == D("0.25") and trade.commission_asset == "USDT"  # 500 × 0.05%
    assert not hasattr(trade, "client_order_id")
    # 窗口之后没有成交：查询成功的空，不是失败
    assert await s.adapter.fetch_trades("BTCUSDT", T0_MS + 10_000) == Known([])
    assert await s.adapter.fetch_trades("ETHUSDT", T0_MS - 1000) == Known([])


async def test_trades_limit_and_end_are_passed_through():
    s = make_stack()
    for n in (1, 2, 3):
        await market(s, BUY, LONG, "0.01", n)
    assert len((await s.adapter.fetch_trades("BTCUSDT", 0, limit=2)).value) == 2
    assert len((await s.adapter.fetch_trades("BTCUSDT", 0, end_ms=T0_MS - 1)).value) == 0
    sent = s.transport.sent("GET", "/api/private/trades")
    assert sent[0][2]["limit"] == 2 and sent[1][2]["end_ms"] == T0_MS - 1


async def test_income_rows_and_type_filter():
    s = make_stack()
    await market(s, BUY, LONG, "0.03", 1)
    await market(s, SELL, LONG, "0.01", 2, leg="c")  # 价格没变：盈亏为 0，不产生盈亏流水
    s.feed.set_price("BTCUSDT", "51000")
    await market(s, SELL, LONG, "0.01", 3, leg="c")  # (51000 − 50000) × 0.01
    s.engine.settle_funding("BTCUSDT")  # 剩下的 0.01 多仓付资金费
    rows = (await s.adapter.fetch_income(0)).value
    assert {r.income_type for r in rows} == {"COMMISSION", "REALIZED_PNL", "FUNDING_FEE"}
    assert all(isinstance(r, t.IncomeInfo) and isinstance(r.amount, Decimal) and r.asset == "USDT" for r in rows)
    pnl = (await s.adapter.fetch_income(0, income_type="REALIZED_PNL")).value
    assert [r.amount for r in pnl] == [D("10")] and pnl[0].symbol == "BTCUSDT"
    funding = (await s.adapter.fetch_income(0, income_type="FUNDING_FEE")).value
    assert [r.amount for r in funding] == [D("-0.051")]  # 0.01 × 51000 × 0.01%，多仓付
    assert (await s.adapter.fetch_income(0, end_ms=0)).value == []


async def test_candles_are_floats_and_ascending():
    s = make_stack()
    base = T0_MS - T0_MS % 900_000
    s.feed.set_candles("BTCUSDT", "15m", [
        [base - 900_000 * 2, "100", "110", "90", "105", "12.5", base - 900_000 - 1],
        [base - 900_000, "105", "120", "100", "115", "7", base - 1],
        [base, "115", "116", "114", "115.5", "1", base + 899_999],
    ])
    r = await s.adapter.fetch_candles("BTCUSDT", "15m", 99)
    assert isinstance(r, Known) and len(r.value) == 3
    assert [c.open_time_ms for c in r.value] == sorted(c.open_time_ms for c in r.value)
    first = r.value[0]
    assert first == t.Candle(base - 1_800_000, 100.0, 110.0, 90.0, 105.0, 12.5, base - 900_001)
    two = (await s.adapter.fetch_candles("BTCUSDT", "15m", 2)).value
    assert [c.open_time_ms for c in two] == [base - 900_000, base]


async def test_candle_limit_over_99_is_a_programming_error_and_unknown_symbol_is_unknown():
    s = make_stack()
    with pytest.raises(ValueError):
        await s.adapter.fetch_candles("BTCUSDT", "15m", 100)
    with pytest.raises(ValueError):
        await s.adapter.fetch_candles("BTCUSDT", "15m", 0)
    r = await s.adapter.fetch_candles("NOPEUSDT", "15m", 5)
    assert isinstance(r, Unknown) and r.http_status == 400


async def test_tickers_for_some_or_all_symbols():
    s = make_stack()
    one = await s.adapter.fetch_tickers(["BTCUSDT", "ETHUSDT"])
    assert one == Known({"BTCUSDT": t.Ticker("BTCUSDT", D("50000"), D("50000"), T0_MS),
                         "ETHUSDT": t.Ticker("ETHUSDT", D("3000"), D("3000"), T0_MS)})
    everything = (await s.adapter.fetch_tickers()).value
    assert {"BTCUSDT", "ETHUSDT", "DOGEUSDT", "哈基米USDT"} <= set(everything)
    assert sent_query(s, "/api/public/tickers")[0] == {"symbols": "BTCUSDT,ETHUSDT"}


def sent_query(s, path):
    return [r[2] or {} for r in s.transport.sent("GET", path)]


# ================================================================ 读方法：失败绝不当成「空」（T0.4）


async def test_position_query_failure_is_unknown_never_an_empty_list():
    s = make_stack()
    await market(s, BUY, LONG, "0.01", 1)  # 交易所里确实有仓
    s.disp.switches.update({"position_query_fail": {"enabled": True, "mode": "error"}})
    r = await s.adapter.fetch_positions()
    assert isinstance(r, Unknown) and not isinstance(r, Known)
    assert (r.code, r.http_status) == (-1001, 500) and r.reason
    # 故障关闭后读回真实持仓
    s.disp.switches.reset()
    assert len(positions_of(await s.adapter.fetch_positions())) == 1


async def test_position_query_hang_is_unknown_after_the_request_timeout():
    s = make_stack(request_timeout_s=0.05)
    s.disp.switches.update({"position_query_fail": {"enabled": True, "mode": "hang"}})
    r = await s.adapter.fetch_positions()
    assert isinstance(r, Unknown) and "超时" in r.reason and r.http_status is None


async def test_every_read_method_returns_unknown_on_transport_failure():
    s = make_stack(request_timeout_s=0.05)

    class Dead:
        def __init__(self, exc):
            self.exc = exc

        async def request(self, *a, **k):
            raise self.exc

        async def ws_connect(self, *a, **k):
            raise self.exc

        async def close(self):
            return None

    for exc, word in ((TransportTimeout("x"), "超时"), (TransportConnectionError("x"), "连接")):
        s.adapter._transport = Dead(exc)  # 换掉底层传输
        results = [
            await s.adapter.server_time(), await s.adapter.load_instruments(), await s.adapter.fetch_position_mode(),
            await s.adapter.fetch_account(), await s.adapter.fetch_positions(), await s.adapter.fetch_open_orders(),
            await s.adapter.fetch_open_algo_orders(), await s.adapter.fetch_order("BTCUSDT", client_order_id="x"),
            await s.adapter.fetch_algo_order("BTCUSDT", client_algo_id="x"),
            await s.adapter.fetch_trades("BTCUSDT", 0), await s.adapter.fetch_income(0),
            await s.adapter.fetch_candles("BTCUSDT", "15m", 5), await s.adapter.fetch_tickers(),
            await s.adapter.fetch_leverage("BTCUSDT"), await s.adapter.fetch_restrictions(),
        ]
        assert all(isinstance(r, Unknown) and word in r.reason for r in results), results


class Scripted:
    """按脚本回答的传输：每个请求返回同一个 Response。"""

    def __init__(self, response):
        self.response = response
        self.count = 0

    async def request(self, *a, **k):
        self.count += 1
        return self.response

    async def ws_connect(self, *a, **k):
        raise TransportConnectionError("无")

    async def close(self):
        return None


@pytest.mark.parametrize("body", [{"unexpected": 1}, None, "<html>oops</html>", [{"symbol": "BTCUSDT"}], 42])
async def test_unparseable_success_bodies_are_unknown_not_empty(body):
    s = make_stack()
    s.adapter._transport = Scripted(Response(200, body, {}))
    for call in (s.adapter.fetch_positions(), s.adapter.fetch_open_orders(), s.adapter.fetch_open_algo_orders(),
                 s.adapter.fetch_account(), s.adapter.load_instruments(), s.adapter.fetch_trades("BTCUSDT", 0),
                 s.adapter.fetch_income(0), s.adapter.fetch_candles("BTCUSDT", "15m", 3), s.adapter.server_time()):
        r = await call
        assert isinstance(r, Unknown) and "无法解析" in r.reason, r


async def test_a_5xx_html_error_page_is_unknown_with_the_status():
    s = make_stack()
    s.adapter._transport = Scripted(Response(502, "<html>Bad Gateway</html>", {}))
    r = await s.adapter.fetch_positions()
    assert isinstance(r, Unknown) and r.http_status == 502 and r.code is None


async def test_wrong_token_raises_auth_config_error_instead_of_unknown():
    s = make_stack(token="not-the-kernel-token")
    with pytest.raises(AuthConfigError):
        await s.adapter.fetch_account()
    with pytest.raises(AuthConfigError):
        await market(s, BUY, LONG, "0.01", 1)
    # 公共接口不需要令牌
    assert isinstance(await s.adapter.server_time(), Known)


# ================================================================ 写方法：四种方向组合（T0.6）


async def test_four_side_combinations_open_and_close_the_right_leg():
    s = make_stack()

    async def legs():
        return {(sym, side): p.qty for (sym, side), p in positions_of(await s.adapter.fetch_positions()).items()}

    r = await market(s, BUY, LONG, "0.02", 1)  # BUY LONG：开多
    assert isinstance(r, WriteAccepted) and r.order.side is BUY and r.order.position_side is LONG
    assert await legs() == {("BTCUSDT", LONG): D("0.02")}
    r = await market(s, SELL, LONG, "0.01", 2, leg="c")  # SELL LONG：平多（减一半）
    assert isinstance(r, WriteAccepted) and r.order.side is SELL
    assert await legs() == {("BTCUSDT", LONG): D("0.01")}
    r = await market(s, SELL, SHORT, "0.02", 3)  # SELL SHORT：开空
    assert isinstance(r, WriteAccepted)
    assert await legs() == {("BTCUSDT", LONG): D("0.01"), ("BTCUSDT", SHORT): D("0.02")}
    r = await market(s, BUY, SHORT, "0.02", 4, leg="c")  # BUY SHORT：平空（平光）
    assert isinstance(r, WriteAccepted)
    assert await legs() == {("BTCUSDT", LONG): D("0.01")}
    r = await market(s, SELL, LONG, "0.01", 5, leg="c")
    assert isinstance(r, WriteAccepted)
    assert await legs() == {}


async def test_closing_with_the_opening_side_combination_does_not_flatten_the_other_leg():
    s = make_stack()
    await market(s, BUY, LONG, "0.02", 1)
    await market(s, SELL, SHORT, "0.02", 2)
    # SELL SHORT 是继续开空，不是平多
    await market(s, SELL, SHORT, "0.01", 3)
    pos = positions_of(await s.adapter.fetch_positions())
    assert pos[("BTCUSDT", LONG)].qty == D("0.02") and pos[("BTCUSDT", SHORT)].qty == D("0.03")


async def test_market_order_request_carries_the_position_side_and_no_reduce_only_flag():
    s = make_stack()
    await market(s, SELL, SHORT, "0.01", 7)
    method, path, params, body, auth = s.transport.sent("POST", "/api/private/order")[0]
    assert body == {"symbol": "BTCUSDT", "side": "SELL", "position_side": "SHORT", "type": "MARKET",
                    "qty": "0.01", "client_order_id": cid(7)}
    assert auth is True and params is None


async def test_accepted_order_is_parsed_and_realised_pnl_lands_on_the_account():
    s = make_stack()
    await market(s, BUY, LONG, "0.02", 1)
    s.feed.set_price("BTCUSDT", "51000")
    r = await market(s, SELL, LONG, "0.02", 2, leg="c")
    assert isinstance(r, WriteAccepted) and r.order.avg_price == D("51000")
    acct = (await s.adapter.fetch_account()).value
    assert acct.wallet_balance == D("1000") + D("20") - D("0.5") - D("0.51")  # 盈亏 20 − 开仓费 0.5 − 平仓费 0.51


# ================================================================ 写方法：交易所拒绝 -> WriteRejected


async def test_close_without_a_position_is_rejected_as_reduce_rejected():
    s = make_stack()
    r = await market(s, SELL, LONG, "0.01", 1, leg="c")
    assert isinstance(r, WriteRejected) and r.kind is RejectKind.REDUCE_REJECTED and r.code == -2022
    assert r.http_status == 400 and r.message


async def test_closing_more_than_held_is_rejected():
    s = make_stack()
    await market(s, BUY, LONG, "0.01", 1)
    r = await market(s, SELL, LONG, "0.02", 2, leg="c")
    assert isinstance(r, WriteRejected) and r.kind is RejectKind.REDUCE_REJECTED


async def test_min_notional_margin_precision_and_duplicate_id_rejections():
    s = make_stack()
    r = await market(s, BUY, LONG, "0.006", 1, symbol="ETHUSDT")  # 18 USDT < 最小名义金额 20
    assert isinstance(r, WriteRejected) and (r.kind, r.code) == (RejectKind.MIN_NOTIONAL, -4164)
    r = await market(s, BUY, LONG, "1", 2)  # 需要 2500 USDT 保证金
    assert isinstance(r, WriteRejected) and (r.kind, r.code) == (RejectKind.INSUFFICIENT_MARGIN, -2019)
    r = await market(s, BUY, LONG, "0.0015", 3)  # 不是步长的整数倍
    assert isinstance(r, WriteRejected) and (r.kind, r.code) == (RejectKind.INVALID_PARAMS, -1111)
    s.disp.switches.update({"behaviors": {"duplicate_client_id_after_fill": "reject"}})
    assert isinstance(await market(s, BUY, LONG, "0.01", 4), WriteAccepted)
    r = await market(s, BUY, LONG, "0.01", 4)
    assert isinstance(r, WriteRejected) and (r.kind, r.code) == (RejectKind.DUPLICATE_CLIENT_ID, -4116)


# ================================================================ 写方法：条件单


async def test_stop_order_accepted_with_close_position_and_no_quantity():
    s = make_stack()
    await market(s, BUY, LONG, "0.01", 1)
    r = await stop(s, 2, "49000", working_type=WorkingType.MARK_PRICE)
    assert isinstance(r, WriteAccepted) and r.order is None
    a = r.algo
    assert (a.client_algo_id, a.side, a.position_side, a.algo_type) == (cid(2, "s", "0"), SELL, LONG,
                                                                        AlgoType.STOP_MARKET)
    assert (a.trigger_price, a.working_type, a.close_position, a.qty, a.raw_status) == \
        (D("49000"), WorkingType.MARK_PRICE, True, None, "NEW")
    body = s.transport.sent("POST", "/api/private/algo_order")[0][3]
    assert body == {"symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG", "type": "STOP_MARKET",
                    "trigger_price": "49000", "working_type": "MARK_PRICE", "close_position": True, "qty": None,
                    "client_algo_id": cid(2, "s", "0")}


async def test_quantity_stop_and_take_profit_types():
    s = make_stack()
    r = await stop(s, 1, "49000", close_position=False, qty="0.005")
    assert isinstance(r, WriteAccepted) and r.algo.qty == D("0.005") and not r.algo.close_position
    tp = await stop(s, 2, "52000", algo_type=AlgoType.TAKE_PROFIT_MARKET)
    assert isinstance(tp, WriteAccepted) and tp.algo.algo_type is AlgoType.TAKE_PROFIT_MARKET


async def test_stop_that_would_trigger_immediately_is_rejected_with_2021():
    s = make_stack()
    r = await stop(s, 1, "50500")  # 多仓止损价高于现价 50000
    assert isinstance(r, WriteRejected)
    assert (r.kind, r.code, r.http_status) == (RejectKind.IMMEDIATE_TRIGGER, -2021, 400)


async def test_second_close_position_stop_on_the_same_leg_is_rejected_with_4130():
    s = make_stack()
    assert isinstance(await stop(s, 1, "49000"), WriteAccepted)
    r = await stop(s, 2, "48000")
    assert isinstance(r, WriteRejected) and (r.kind, r.code) == (RejectKind.DUAL_CLOSE_POSITION, -4130)
    # 允许放开（实测第 5/16 项的另一种取值）
    s.disp.switches.update({"behaviors": {"dual_close_position_stop": "allow"}})
    assert isinstance(await stop(s, 3, "47000"), WriteAccepted)


async def test_conditional_order_limit_is_rejected_with_4045():
    s = make_stack()
    for i in range(10):
        assert isinstance(await stop(s, 100 + i, str(40000 + i), close_position=False, qty="0.001"), WriteAccepted)
    r = await stop(s, 200, "41000", close_position=False, qty="0.001")
    assert isinstance(r, WriteRejected) and (r.kind, r.code) == (RejectKind.STOP_LIMIT_REACHED, -4045)


async def test_algo_create_fail_reject_and_unknown_modes():
    s = make_stack()
    s.disp.switches.update({"algo_create_fail": {"enabled": True, "mode": "reject", "code": -2010, "count": 1}})
    r = await stop(s, 1)
    assert isinstance(r, WriteRejected) and (r.kind, r.code) == (RejectKind.OTHER, -2010)
    s.disp.switches.update({"algo_create_fail": {"enabled": True, "mode": "unknown", "count": 1}})
    r = await stop(s, 2)
    assert isinstance(r, WriteUnknown) and (r.code, r.http_status) == (-1007, 503)
    # 开关用完后恢复正常；失败的那两次都没有留下条件单
    assert isinstance(await stop(s, 3), WriteAccepted)
    assert len((await s.adapter.fetch_open_algo_orders()).value) == 1


# ================================================================ 超时之后的回查（4.11 第 1 种）


async def test_accept_then_hang_is_write_unknown_and_lookup_by_client_id_finds_it_filled():
    s = make_stack(request_timeout_s=0.05)
    s.disp.switches.update({"order_timeout": {"enabled": True, "mode": "accept_then_hang", "hang_s": 30,
                                              "count": 1}})
    r = await market(s, BUY, LONG, "0.01", 1)
    assert isinstance(r, WriteUnknown) and "超时" in r.reason and r.http_status is None
    found = await s.adapter.fetch_order("BTCUSDT", client_order_id=cid(1))
    assert isinstance(found, Known) and found.value.status == "FILLED" and found.value.filled_qty == D("0.01")
    assert positions_of(await s.adapter.fetch_positions())[("BTCUSDT", LONG)].qty == D("0.01")


async def test_drop_is_write_unknown_and_lookup_finds_nothing():
    s = make_stack(request_timeout_s=0.5)
    s.disp.switches.update({"order_timeout": {"enabled": True, "mode": "drop", "hang_s": 0.01, "count": 1}})
    r = await market(s, BUY, LONG, "0.01", 1)
    assert isinstance(r, WriteUnknown) and "连接" in r.reason
    assert await s.adapter.fetch_order("BTCUSDT", client_order_id=cid(1)) == Known(None)
    assert await s.adapter.fetch_positions() == Known([])


async def test_drop_with_long_hang_times_out_and_lookup_finds_nothing():
    s = make_stack(request_timeout_s=0.05)
    s.disp.switches.update({"order_timeout": {"enabled": True, "mode": "drop", "hang_s": 30, "count": 1}})
    r = await market(s, BUY, LONG, "0.01", 1)
    assert isinstance(r, WriteUnknown) and "超时" in r.reason
    assert await s.adapter.fetch_order("BTCUSDT", client_order_id=cid(1)) == Known(None)


async def test_algo_timeout_is_write_unknown_and_the_stop_can_be_found_by_client_algo_id():
    s = make_stack(request_timeout_s=0.05)
    s.disp.switches.update({"order_timeout": {"enabled": True, "mode": "accept_then_hang", "scope": "algo",
                                              "hang_s": 30, "count": 1}})
    r = await stop(s, 1, "49000")
    assert isinstance(r, WriteUnknown)
    found = await s.adapter.fetch_algo_order("BTCUSDT", client_algo_id=cid(1, "s", "0"))
    assert isinstance(found, Known) and found.value is not None and found.value.is_open


async def test_http_503_unknown_is_write_unknown_with_code_and_status():
    s = make_stack()
    s.disp.switches.update({"algo_create_fail": {"enabled": True, "mode": "unknown"}})
    r = await stop(s, 1)
    assert isinstance(r, WriteUnknown) and (r.code, r.http_status) == (-1007, 503)


async def test_unparseable_accepted_order_is_write_unknown_not_accepted():
    s = make_stack()
    s.adapter._transport = Scripted(Response(200, {"order_id": "1"}, {}))
    r = await market(s, BUY, LONG, "0.01", 1)
    assert isinstance(r, WriteUnknown) and "无法解析" in r.reason


# ================================================================ 撤单三态


async def test_cancel_order_three_states():
    s = make_stack()
    s.disp.switches.update({"partial_fill": {"enabled": True, "ratio": "0.5", "remainder": "later", "delay_s": 600,
                                             "count": 1}})
    placed = (await market(s, BUY, LONG, "0.02", 1)).order
    assert placed.status == "PARTIALLY_FILLED"
    done = await s.adapter.cancel_order(s.cap, symbol="BTCUSDT", client_order_id=cid(1))
    assert done.outcome is CancelOutcome.CANCELLED and done.order.status == "CANCELED"
    assert done.order.filled_qty == D("0.01")
    again = await s.adapter.cancel_order(s.cap, symbol="BTCUSDT", client_order_id=cid(1))
    assert again.outcome is CancelOutcome.NOT_FOUND and again.code == -2011
    never = await s.adapter.cancel_order(s.cap, symbol="BTCUSDT", client_order_id=cid(55))
    assert never.outcome is CancelOutcome.NOT_FOUND
    s.disp.switches.update({"rate_limit": {"enabled": True, "http": 429, "scope": "writes", "count": 1}})
    limited = await s.adapter.cancel_order(s.cap, symbol="BTCUSDT", client_order_id=cid(1))
    assert limited.outcome is CancelOutcome.UNKNOWN and limited.code == -1003


async def test_cancel_by_exchange_order_id():
    s = make_stack()
    s.disp.switches.update({"partial_fill": {"enabled": True, "ratio": "0.5", "remainder": "later", "delay_s": 600,
                                             "count": 1}})
    placed = (await market(s, BUY, LONG, "0.02", 1)).order
    r = await s.adapter.cancel_order(s.cap, symbol="BTCUSDT", exchange_order_id=placed.exchange_order_id)
    assert r.outcome is CancelOutcome.CANCELLED


async def test_cancel_algo_order_three_states():
    s = make_stack()
    algo = (await stop(s, 1, "49000")).algo
    done = await s.adapter.cancel_algo_order(s.cap, symbol="BTCUSDT", client_algo_id=cid(1, "s", "0"))
    assert done.outcome is CancelOutcome.CANCELLED and done.algo.raw_status == "CANCELED"
    assert (await s.adapter.fetch_open_algo_orders()).value == []
    again = await s.adapter.cancel_algo_order(s.cap, symbol="BTCUSDT", client_algo_id=cid(1, "s", "0"))
    assert again.outcome is CancelOutcome.NOT_FOUND
    # 已结束的条件单只能按 algo_id 查
    assert (await s.adapter.fetch_algo_order("BTCUSDT", algo_id=algo.algo_id)).value.raw_status == "CANCELED"
    by_id = (await stop(s, 2, "49000")).algo
    assert (await s.adapter.cancel_algo_order(s.cap, symbol="BTCUSDT", algo_id=by_id.algo_id)).outcome \
        is CancelOutcome.CANCELLED


async def test_cancel_with_timeout_or_server_error_is_unknown():
    s = make_stack(request_timeout_s=0.05)
    await stop(s, 1, "49000")
    s.disp.switches.update({"order_timeout": {"enabled": True, "mode": "accept_then_hang", "scope": "all",
                                              "hang_s": 30, "count": 1}})
    r = await s.adapter.cancel_algo_order(s.cap, symbol="BTCUSDT", client_algo_id=cid(1, "s", "0"))
    assert r.outcome is CancelOutcome.UNKNOWN and "超时" in r.message
    # 请求其实已被执行：再撤是「不存在」
    assert (await s.adapter.cancel_algo_order(s.cap, symbol="BTCUSDT", client_algo_id=cid(1, "s", "0"))).outcome \
        is CancelOutcome.NOT_FOUND


# ================================================================ 杠杆与保证金模式


async def test_set_leverage_and_margin_mode():
    s = make_stack()
    r = await s.adapter.set_leverage(s.cap, symbol="BTCUSDT", leverage=10)
    assert isinstance(r, WriteAccepted) and r.leverage == t.LeverageInfo("BTCUSDT", 10, "cross")
    r = await s.adapter.set_margin_mode(s.cap, symbol="BTCUSDT", margin_mode="isolated")
    assert isinstance(r, WriteAccepted) and r.leverage == t.LeverageInfo("BTCUSDT", 10, "isolated")
    assert (await s.adapter.fetch_leverage("BTCUSDT")).value == t.LeverageInfo("BTCUSDT", 10, "isolated")


async def test_margin_mode_change_with_position_or_orders_is_rejected():
    s = make_stack()
    await market(s, BUY, LONG, "0.01", 1)
    r = await s.adapter.set_margin_mode(s.cap, symbol="BTCUSDT", margin_mode="isolated")
    assert isinstance(r, WriteRejected) and (r.kind, r.code) == (RejectKind.HAS_POSITION_OR_ORDERS, -4048)
    r = await s.adapter.set_margin_mode(s.cap, symbol="ETHUSDT", margin_mode="isolated")
    assert isinstance(r, WriteAccepted)
    await stop(s, 2, "2000", symbol="ETHUSDT")
    r = await s.adapter.set_margin_mode(s.cap, symbol="ETHUSDT", margin_mode="cross")
    assert isinstance(r, WriteRejected) and (r.kind, r.code) == (RejectKind.HAS_POSITION_OR_ORDERS, -4047)


# ================================================================ 出口校验仍然有效

async def test_write_exit_checks_still_apply_through_the_standin_adapter():
    s = make_stack()
    with pytest.raises(CapabilityError):
        await s.adapter.create_market_order(None, symbol="BTCUSDT", side=BUY, position_side=LONG, qty=D("0.01"),
                                            client_order_id=cid(1))
    with pytest.raises(ClientOrderIdError):
        await s.adapter.create_market_order(s.cap, symbol="BTCUSDT", side=BUY, position_side=LONG, qty=D("0.01"),
                                            client_order_id="web_abcdef")
    with pytest.raises(ValueError):
        await market(s, BUY, LONG, "0", 1)
    assert s.transport.sent("POST") == []  # 校验失败的请求一个也没发出去
    readonly = make_stack(capability=None)
    assert isinstance(await readonly.adapter.fetch_account(), Known)
    with pytest.raises(CapabilityError):
        await readonly.adapter.create_market_order(readonly.cap, symbol="BTCUSDT", side=BUY, position_side=LONG,
                                                   qty=D("0.01"), client_order_id=cid(1))


# ================================================================ 限频（权重头、软上限、429/418）


async def test_used_weight_header_is_recorded():
    s = make_stack()
    assert s.adapter.used_weight() is None
    await s.adapter.fetch_positions()
    assert s.adapter.used_weight() == 5
    await s.adapter.fetch_account()
    assert s.adapter.used_weight() == 10
    await s.adapter.fetch_open_orders()  # 不带合约：权重 40
    assert s.adapter.used_weight() == 50 == s.disp.used_weight()
    s.clock.advance(61_000)  # 一分钟后滚出
    await s.adapter.server_time()
    assert s.adapter.used_weight() == 1


async def test_soft_limit_pauses_non_protective_reads_but_not_protective_reads_or_writes():
    s = make_stack(config={"weight_soft_limit": 100})
    await s.adapter.fetch_open_orders()
    await s.adapter.fetch_open_algo_orders()
    await s.adapter.fetch_open_orders()  # 120 > 100
    assert s.adapter.used_weight() == 120
    before = len(s.transport.requests)
    # 非保护类读：K 线、价格、成交、收益流水、合约清单、交易所限制 -> 不发请求，直接 Unknown
    paused = [await s.adapter.fetch_candles("BTCUSDT", "15m", 5), await s.adapter.fetch_tickers(),
              await s.adapter.fetch_trades("BTCUSDT", 0), await s.adapter.fetch_income(0),
              await s.adapter.load_instruments(), await s.adapter.fetch_restrictions()]
    assert all(isinstance(r, Unknown) and "限频" in r.reason for r in paused), paused
    assert len(s.transport.requests) == before
    # 保护类读与写不受限
    assert isinstance(await s.adapter.fetch_positions(), Known)
    assert isinstance(await s.adapter.fetch_account(), Known)
    assert isinstance(await s.adapter.fetch_order("BTCUSDT", client_order_id=cid(1)), Known)
    assert isinstance(await s.adapter.fetch_open_orders("BTCUSDT"), Known)
    assert isinstance(await s.adapter.fetch_open_algo_orders("BTCUSDT"), Known)
    assert isinstance(await market(s, BUY, LONG, "0.01", 1), WriteAccepted)
    # 下一分钟恢复
    s.clock.advance(MINUTE)
    assert isinstance(await s.adapter.fetch_candles("BTCUSDT", "15m", 5), Known)


async def test_raising_the_soft_limit_in_config_applies_immediately():
    s = make_stack(config={"weight_soft_limit": 100})
    for _ in range(3):
        await s.adapter.fetch_open_orders()
    assert isinstance(await s.adapter.fetch_tickers(), Unknown)
    s.config["weight_soft_limit"] = 2400
    assert isinstance(await s.adapter.fetch_tickers(), Known)


@pytest.mark.parametrize("http,retry_after_s", [(429, 60), (418, 120)])
async def test_rate_limited_read_is_unknown_and_backs_off_non_protective_reads(http, retry_after_s):
    s = make_stack()
    s.disp.switches.update({"rate_limit": {"enabled": True, "http": http, "scope": "reads", "count": 1}})
    r = await s.adapter.fetch_candles("BTCUSDT", "15m", 5)
    assert isinstance(r, Unknown) and (r.code, r.http_status) == (-1003, http)
    sent = len(s.transport.requests)
    # 退避期间：非保护类读不再发请求
    again = await s.adapter.fetch_candles("BTCUSDT", "15m", 5)
    assert isinstance(again, Unknown) and "限频" in again.reason and len(s.transport.requests) == sent
    # 保护类读照常发出（故障已用完，成功）
    assert isinstance(await s.adapter.fetch_positions(), Known)
    assert len(s.transport.requests) == sent + 1
    s.clock.advance(retry_after_s * 1000 - 1)
    assert isinstance(await s.adapter.fetch_candles("BTCUSDT", "15m", 5), Unknown)
    s.clock.advance(1)
    assert isinstance(await s.adapter.fetch_candles("BTCUSDT", "15m", 5), Known)


async def test_rate_limited_protective_read_is_unknown_not_empty():
    s = make_stack()
    s.disp.switches.update({"rate_limit": {"enabled": True, "http": 429, "scope": "reads", "count": 1}})
    r = await s.adapter.fetch_positions()
    assert isinstance(r, Unknown) and r.http_status == 429
    assert isinstance(await s.adapter.fetch_positions(), Known)  # 保护类不受退避影响，下一次就成功


@pytest.mark.parametrize("http", [429, 418])
async def test_rate_limited_write_is_rejected_as_rate_limited(http):
    s = make_stack()
    s.disp.switches.update({"rate_limit": {"enabled": True, "http": http, "scope": "writes", "count": 1}})
    r = await market(s, BUY, LONG, "0.01", 1)
    assert isinstance(r, WriteRejected)
    assert (r.kind, r.code, r.http_status) == (RejectKind.RATE_LIMITED, -1003, http)
    assert await s.adapter.fetch_positions() == Known([])  # 确实没有下出去
    # 写不被自己的退避拦住：故障用完后下一次就能下
    assert isinstance(await market(s, BUY, LONG, "0.01", 2), WriteAccepted)


async def test_writes_ignore_the_reads_backoff_and_the_soft_limit():
    s = make_stack(config={"weight_soft_limit": 100})
    s.disp.switches.update({"rate_limit": {"enabled": True, "http": 429, "scope": "reads", "count": 1}})
    await s.adapter.fetch_candles("BTCUSDT", "15m", 5)
    for _ in range(3):
        await s.adapter.fetch_open_orders()
    assert isinstance(await market(s, BUY, LONG, "0.01", 1), WriteAccepted)
    assert isinstance(await stop(s, 2, "49000"), WriteAccepted)


# ================================================================ 数据流入口与关闭


async def test_account_stream_is_created_once_and_close_stops_everything():
    s = make_stack()
    stream = s.adapter.account_stream()
    assert isinstance(stream, AccountStream) and s.adapter.account_stream() is stream
    assert stream.state == "stopped"  # 还没有 start
    await s.adapter.close()
    await s.adapter.close()  # 重复关闭不出错
    assert s.disp.hub.count() == 0
