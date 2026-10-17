"""BinancePublicClient：ccxt 4.5.78 异步 binanceusdm，只做公共 GET（合约规则、K 线、行情、服务器时间）。

测试里一律用假的 ccxt 对象，不连任何网络：验证解析（字符串 -> Decimal、原始合约代码、K 线收盘时间）、
超时、失败一律是 Unknown、白名单（调用 ccxt 的任何其它方法立即失败）、不配密钥。
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import ccxt
import pytest

from dmr_kernel.core.types import Candle, Instrument, Known, Ticker, Unknown
from dmr_kernel.exchange.binance_public import (
    ALLOWED_CCXT_METHODS,
    BinancePublicClient,
    WhitelistError,
    build_default_exchange,
)

D = Decimal
T0 = 1_791_943_200_000


# ================================================================ 假的 ccxt 对象


def _as_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def raw_market(symbol="BTCUSDT", base="BTC", quote="USDT", contract_type="PERPETUAL", status="TRADING", tick="0.10",
               step="0.001", min_qty="0.001", market_step="0.001", market_max="120", notional="100",
               onboard="1569398400000", filters=None):
    """ccxt ``fetch_markets`` 对 binanceusdm 的返回形状：统一字段 + ``info``（交易所原始的 symbols 条目）。"""
    info = {
        "symbol": symbol, "pair": symbol, "contractType": contract_type, "deliveryDate": 4133404800000,
        "onboardDate": onboard, "status": status, "baseAsset": base, "quoteAsset": quote, "marginAsset": quote,
        "pricePrecision": 2, "quantityPrecision": 3,
        "filters": filters if filters is not None else [
            {"filterType": "PRICE_FILTER", "minPrice": "556.80", "maxPrice": "4529764", "tickSize": tick},
            {"filterType": "LOT_SIZE", "maxQty": "1000", "minQty": min_qty, "stepSize": step},
            {"filterType": "MARKET_LOT_SIZE", "maxQty": market_max, "minQty": min_qty, "stepSize": market_step},
            {"filterType": "MAX_NUM_ORDERS", "limit": 200},
            {"filterType": "MAX_NUM_ALGO_ORDERS", "limit": 10},
            {"filterType": "MIN_NOTIONAL", "notional": notional},
            {"filterType": "PERCENT_PRICE", "multiplierUp": "1.0500", "multiplierDown": "0.9500",
             "multiplierDecimal": "4"},
        ],
    }
    if onboard is None:
        del info["onboardDate"]
    unified = f"{base}/{quote}:{quote}" + ("" if contract_type == "PERPETUAL" else "-240628")
    return {"id": symbol, "symbol": unified, "base": base, "quote": quote, "type": "swap", "swap": True,
            "active": status == "TRADING", "precision": {"amount": _as_float(step), "price": _as_float(tick)},
            "limits": {}, "info": info}


BTC = raw_market()
CN = raw_market("币安人生USDT", "币安人生", tick="0.00001", step="1", min_qty="1", market_step="1", market_max="500000",
                notional="5", onboard="1760000000000")
SHIB = raw_market("1000SHIBUSDT", "1000SHIB", tick="0.000001", step="1", min_qty="1", market_step="1",
                  market_max="2000000", notional="5")
QUARTER = raw_market("BTCUSDT_241227", "BTC", contract_type="CURRENT_QUARTER", notional="100")
SETTLING = raw_market("LUNAUSDT", "LUNA", status="SETTLING")


def ticker24(raw, last, close_time=T0 + 1000):
    base = raw.removesuffix("USDT")
    return {"symbol": f"{base}/USDT:USDT", "last": float(last), "timestamp": close_time,
            "info": {"symbol": raw, "lastPrice": str(last), "closeTime": str(close_time), "priceChangePercent": "1.2"}}


def mark_row(raw, price, time=T0 + 2000):
    base = raw.removesuffix("USDT")
    return {"symbol": f"{base}/USDT:USDT", "markPrice": float(price),
            "info": {"symbol": raw, "markPrice": str(price), "indexPrice": str(price), "time": str(time)}}


class FakeCcxt:
    """只实现白名单里的方法；其余方法一旦被调用就记进 ``forbidden`` 并抛错（白名单代理应当在更早的地方拦住）。"""

    apiKey = None
    secret = None
    password = None

    def __init__(self, *, markets=None, tickers=None, marks=None, ohlcv=None, time_ms=T0):
        self.markets = {m["symbol"]: m for m in (markets if markets is not None else [BTC, CN, SHIB])}
        self.tickers = {t["info"]["symbol"]: t for t in (tickers if tickers is not None else [
            ticker24("BTCUSDT", "50000"), ticker24("币安人生USDT", "0.5"), ticker24("1000SHIBUSDT", "0.02")])}
        self.marks = {t["info"]["symbol"]: t for t in (marks if marks is not None else [
            mark_row("BTCUSDT", "50010"), mark_row("币安人生USDT", "0.51"), mark_row("1000SHIBUSDT", "0.0201")])}
        self.ohlcv = ohlcv if ohlcv is not None else [[T0 + i * 900_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0 + i]
                                                      for i in range(3)]
        self.time_ms = time_ms
        self.calls: list[tuple[str, tuple, dict]] = []
        self.errors: dict[str, BaseException] = {}
        self.hang: set[str] = set()
        self.forbidden: list[str] = []
        self.closed = 0

    async def _enter(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self.hang:
            await asyncio.sleep(3600)
        if name in self.errors:
            raise self.errors[name]

    def called(self, name):
        return [(a, k) for n, a, k in self.calls if n == name]

    async def load_markets(self, reload=False, params={}):
        await self._enter("load_markets", reload)
        return dict(self.markets)

    async def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params={}):
        await self._enter("fetch_ohlcv", symbol, timeframe, since=since, limit=limit)
        return [list(row) for row in self.ohlcv]

    async def fetch_tickers(self, symbols=None, params={}):
        await self._enter("fetch_tickers", symbols)
        wanted = None if symbols is None else set(symbols)
        return {t["symbol"]: t for raw, t in self.tickers.items() if wanted is None or raw in wanted}

    async def fetch_mark_prices(self, symbols=None, params={}):
        await self._enter("fetch_mark_prices", symbols)
        wanted = None if symbols is None else set(symbols)
        return {t["symbol"]: t for raw, t in self.marks.items() if wanted is None or raw in wanted}

    async def fetch_time(self, params={}):
        await self._enter("fetch_time")
        return self.time_ms

    async def close(self):
        self.calls.append(("close", (), {}))
        self.closed += 1

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        async def forbidden(*args, **kwargs):
            self.forbidden.append(name)
            raise AssertionError(f"白名单之外的 ccxt 方法被调用了：{name}")

        return forbidden


def client_for(fake=None, **kw):
    fake = FakeCcxt() if fake is None else fake
    kw.setdefault("timeout_s", 1.0)
    return BinancePublicClient(exchange=fake, **kw), fake


# ================================================================ 默认的 ccxt 对象与密钥


def test_default_exchange_is_a_keyless_binanceusdm_with_timeout_and_no_currency_fetch():
    ex = build_default_exchange(7.5)
    assert ex.id == "binanceusdm"
    assert not ex.apiKey and not ex.secret and not ex.password and not getattr(ex, "uid", None)
    assert ex.timeout == 7500 and ex.enableRateLimit is True
    assert ex.options.get("fetchCurrencies") is False  # 不触发需要密钥的币种接口
    assert ex.options["defaultType"] == "swap" and ex.options["defaultSubType"] == "linear"


async def test_default_client_builds_its_own_keyless_exchange_without_touching_the_network():
    client = BinancePublicClient(timeout_s=2.0)
    assert client.ccxt is not None
    await client.close()  # 还没发过任何请求：关闭也不出错
    await client.close()


@pytest.mark.parametrize("attr", ["apiKey", "secret", "password", "uid", "privateKey", "walletAddress"])
def test_client_refuses_an_exchange_that_carries_credentials(attr):
    fake = FakeCcxt()
    setattr(fake, attr, "not-empty")
    with pytest.raises(ValueError):
        BinancePublicClient(exchange=fake)


def test_client_rejects_a_non_positive_timeout():
    with pytest.raises(ValueError):
        BinancePublicClient(exchange=FakeCcxt(), timeout_s=0)


# ================================================================ 白名单


def test_whitelist_is_exactly_the_public_methods():
    assert ALLOWED_CCXT_METHODS == frozenset({"load_markets", "fetch_ohlcv", "fetch_tickers", "fetch_mark_prices",
                                              "fetch_time", "close"})


@pytest.mark.parametrize("name", [
    "create_order", "create_market_order", "create_stop_market_order", "cancel_order", "cancel_all_orders",
    "fetch_balance", "fetch_positions", "fetch_position", "fetch_my_trades", "fetch_open_orders", "fetch_order",
    "set_leverage", "set_margin_mode", "set_position_mode", "fetch_position_mode", "transfer", "withdraw",
    "fapiPrivateGetPositionRisk", "fapiPrivatePostOrder", "fapiPrivatePostListenKey", "sapiGetCapitalConfigGetall",
    "fapiPublicGetExchangeInfo", "request", "fetch", "sign", "private_get", "watch_tickers", "markets", "apiKey",
    "secret", "options", "urls", "set_sandbox_mode", "enable_demo_trading", "_private",
])
def test_every_non_whitelisted_ccxt_attribute_fails_immediately(name):
    client, fake = client_for()
    with pytest.raises(WhitelistError):
        getattr(client.ccxt, name)
    assert fake.forbidden == [] and fake.calls == []


def test_whitelisted_methods_are_reachable_through_the_guard():
    client, fake = client_for()
    for name in ALLOWED_CCXT_METHODS:
        assert callable(getattr(client.ccxt, name))


def test_the_guard_cannot_be_written_to_or_deleted_from():
    client, _ = client_for()
    with pytest.raises(WhitelistError):
        client.ccxt.apiKey = "oops"
    with pytest.raises(WhitelistError):
        client.ccxt.fetch_time = lambda: 0
    with pytest.raises(WhitelistError):
        del client.ccxt.fetch_time


async def test_the_public_methods_only_ever_use_whitelisted_ccxt_methods():
    client, fake = client_for()
    await client.load_instruments()
    await client.fetch_candles("BTCUSDT", "15m", 3)
    await client.fetch_tickers(None)
    await client.fetch_tickers(["BTCUSDT"])
    await client.server_time()
    await client.close()
    assert {name for name, _, _ in fake.calls} <= ALLOWED_CCXT_METHODS
    assert fake.forbidden == []


# ================================================================ 合约规则


async def test_load_instruments_parses_the_exchange_filters_into_decimals():
    client, fake = client_for()
    result = await client.load_instruments()
    assert isinstance(result, Known)
    by = {i.symbol: i for i in result.value}
    assert by["BTCUSDT"] == Instrument(
        symbol="BTCUSDT", base="BTC", quote="USDT", contract_type="PERPETUAL", status="TRADING",
        tick_size=D("0.10"), step_size=D("0.001"), min_qty=D("0.001"), min_notional=D("100"),
        market_step_size=D("0.001"), market_max_qty=D("120"), onboard_ms=1569398400000)
    assert all(isinstance(getattr(by["BTCUSDT"], f), D) for f in ("tick_size", "step_size", "min_qty", "min_notional"))
    assert by["BTCUSDT"].tradable


async def test_load_instruments_keeps_the_exchange_codes_chinese_names_and_1000_prefix():
    client, _ = client_for()
    result = await client.load_instruments()
    by = {i.symbol: i for i in result.value}
    assert set(by) == {"BTCUSDT", "币安人生USDT", "1000SHIBUSDT"}  # 原始代码，不是 BTC/USDT:USDT
    assert by["币安人生USDT"].base == "币安人生" and by["币安人生USDT"].tick_size == D("0.00001")
    assert by["币安人生USDT"].market_max_qty == D("500000") and by["币安人生USDT"].onboard_ms == 1760000000000
    assert by["1000SHIBUSDT"].base == "1000SHIB" and by["1000SHIBUSDT"].step_size == D("1")


async def test_load_instruments_returns_every_contract_type_and_status_unfiltered():
    client, _ = client_for(FakeCcxt(markets=[BTC, QUARTER, SETTLING]))
    by = {i.symbol: i for i in (await client.load_instruments()).value}
    assert by["BTCUSDT_241227"].contract_type == "CURRENT_QUARTER" and not by["BTCUSDT_241227"].tradable
    assert by["LUNAUSDT"].status == "SETTLING" and not by["LUNAUSDT"].tradable  # 过滤交给 InstrumentRules


async def test_load_instruments_reloads_from_the_exchange_every_time():
    client, fake = client_for()
    await client.load_instruments()
    await client.load_instruments()
    assert fake.called("load_markets") == [((True,), {}), ((True,), {})]


async def test_optional_fields_default_to_none():
    no_market_lot = raw_market("ABCUSDT", "ABC", onboard=None, filters=[
        {"filterType": "PRICE_FILTER", "tickSize": "0.01"}, {"filterType": "LOT_SIZE", "minQty": "1", "stepSize": "1"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"}])
    client, _ = client_for(FakeCcxt(markets=[no_market_lot]))
    inst = (await client.load_instruments()).value[0]
    assert inst.market_step_size is None and inst.market_max_qty is None and inst.onboard_ms is None


@pytest.mark.parametrize("broken", [
    raw_market("XUSDT", "X", filters=[]),
    raw_market("XUSDT", "X", filters=[{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]),
    raw_market("XUSDT", "X", tick="abc"),
    raw_market("XUSDT", "X", tick="NaN"),
    raw_market("XUSDT", "X", tick="0"),
    raw_market("XUSDT", "X", tick="-0.1"),
    raw_market("XUSDT", "X", step="0"),
    raw_market("XUSDT", "X", step="-1"),
    raw_market("XUSDT", "X", notional=None),
    {**raw_market("XUSDT", "X"), "info": None},
    {**raw_market("XUSDT", "X"), "info": {"filters": "x"}},
    "not a market",
])
async def test_a_broken_market_is_skipped_with_a_warning_and_the_rest_survive(broken, caplog):
    fake = FakeCcxt(markets=[BTC])
    fake.markets["X/USDT:USDT"] = broken
    client, _ = client_for(fake)
    with caplog.at_level(logging.WARNING):
        result = await client.load_instruments()
    assert isinstance(result, Known) and [i.symbol for i in result.value] == ["BTCUSDT"]
    assert any("合约" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("markets", [[], [{"id": "X", "info": {}}], ["junk"]])
async def test_an_empty_or_entirely_unparsable_contract_list_is_unknown_not_empty(markets):
    fake = FakeCcxt(markets=[])
    fake.markets = {f"M{i}": m for i, m in enumerate(markets)}
    client, _ = client_for(fake)
    result = await client.load_instruments()
    assert isinstance(result, Unknown) and result.reason


async def test_load_instruments_failure_is_unknown():
    fake = FakeCcxt()
    fake.errors["load_markets"] = ccxt.ExchangeNotAvailable('binanceusdm GET https://fapi.binance.com/fapi/v1/exchangeInfo 502')
    client, _ = client_for(fake)
    result = await client.load_instruments()
    assert isinstance(result, Unknown) and "ExchangeNotAvailable" in result.reason


# ================================================================ K 线


async def test_fetch_candles_converts_rows_and_derives_the_close_time():
    client, fake = client_for()
    result = await client.fetch_candles("BTCUSDT", "15m", 3)
    assert fake.called("fetch_ohlcv") == [(("BTCUSDT", "15m"), {"since": None, "limit": 3})]
    assert isinstance(result, Known) and len(result.value) == 3
    first = result.value[0]
    assert first == Candle(open_time_ms=T0, open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0,
                           close_time_ms=T0 + 900_000 - 1)
    assert all(isinstance(getattr(first, f), float) for f in ("open", "high", "low", "close", "volume"))
    assert [c.open_time_ms for c in result.value] == sorted(c.open_time_ms for c in result.value)  # 升序


@pytest.mark.parametrize("interval, ms", [("1m", 60_000), ("5m", 300_000), ("1h", 3_600_000), ("4h", 14_400_000),
                                          ("1d", 86_400_000)])
async def test_candle_close_time_is_open_plus_interval_minus_one(interval, ms):
    client, _ = client_for(FakeCcxt(ohlcv=[[T0, 1.0, 2.0, 0.5, 1.5, 7.0]]))
    candle = (await client.fetch_candles("BTCUSDT", interval, 1)).value[0]
    assert candle.close_time_ms == T0 + ms - 1


async def test_fetch_candles_default_limit_is_the_maximum_99():
    client, fake = client_for()
    await client.fetch_candles("BTCUSDT", "15m")
    assert fake.called("fetch_ohlcv")[0][1]["limit"] == 99


@pytest.mark.parametrize("limit", [0, -1, 100, 1500, 2.5, "99", None, True])
async def test_fetch_candles_rejects_a_limit_outside_1_to_99(limit):
    client, fake = client_for()
    with pytest.raises(ValueError):
        await client.fetch_candles("BTCUSDT", "15m", limit)
    assert fake.calls == []


@pytest.mark.parametrize("interval", ["", "m", "15", "0m", "15x", "1M", "abc", None])
async def test_fetch_candles_rejects_an_unknown_interval(interval):
    client, fake = client_for()
    with pytest.raises(ValueError):
        await client.fetch_candles("BTCUSDT", interval, 3)
    assert fake.calls == []


@pytest.mark.parametrize("rows", [[[T0, 1, 2]], [["x", 1, 2, 3, 4, 5]], [[T0, None, 2, 1, 1.5, 3]], "oops", [None]])
async def test_unparsable_candle_rows_make_the_whole_result_unknown(rows):
    client, _ = client_for(FakeCcxt(ohlcv=rows))
    result = await client.fetch_candles("BTCUSDT", "15m", 3)
    assert isinstance(result, Unknown) and result.reason


async def test_an_empty_candle_list_from_the_exchange_is_a_known_empty_result():
    client, _ = client_for(FakeCcxt(ohlcv=[]))
    assert await client.fetch_candles("BTCUSDT", "15m", 3) == Known([])


# ================================================================ 行情

async def test_fetch_tickers_combines_last_and_mark_keyed_by_exchange_code():
    client, fake = client_for()
    result = await client.fetch_tickers(None)
    assert isinstance(result, Known)
    assert set(result.value) == {"BTCUSDT", "币安人生USDT", "1000SHIBUSDT"}
    assert result.value["BTCUSDT"] == Ticker(symbol="BTCUSDT", last=D("50000"), mark=D("50010"), time_ms=T0 + 1000)
    assert result.value["币安人生USDT"].last == D("0.5") and result.value["币安人生USDT"].mark == D("0.51")
    assert all(isinstance(t.last, D) and isinstance(t.mark, D) for t in result.value.values())
    assert fake.called("fetch_tickers") == [((None,), {})] and fake.called("fetch_mark_prices") == [((None,), {})]


async def test_fetch_tickers_for_selected_symbols_passes_exchange_codes_through():
    client, fake = client_for()
    result = await client.fetch_tickers(["BTCUSDT", "1000SHIBUSDT"])
    assert set(result.value) == {"BTCUSDT", "1000SHIBUSDT"}
    assert fake.called("fetch_tickers") == [((["BTCUSDT", "1000SHIBUSDT"],), {})]
    assert fake.called("fetch_mark_prices") == [((["BTCUSDT", "1000SHIBUSDT"],), {})]


async def test_fetch_tickers_with_an_empty_request_is_an_empty_known_without_calls():
    client, fake = client_for()
    assert await client.fetch_tickers([]) == Known({})
    assert fake.calls == []


async def test_a_symbol_missing_from_either_response_is_left_out():
    fake = FakeCcxt(marks=[mark_row("BTCUSDT", "50010")])
    client, _ = client_for(fake)
    result = await client.fetch_tickers(None)
    assert set(result.value) == {"BTCUSDT"}


async def test_unparsable_ticker_rows_are_skipped_but_nothing_at_all_is_unknown():
    bad = ticker24("BTCUSDT", "50000")
    bad["info"]["lastPrice"] = "abc"
    fake = FakeCcxt(tickers=[bad, ticker24("币安人生USDT", "0.5")])
    client, _ = client_for(fake)
    result = await client.fetch_tickers(None)
    assert set(result.value) == {"币安人生USDT"}
    fake = FakeCcxt(tickers=[bad], marks=[mark_row("BTCUSDT", "50010")])
    client, _ = client_for(fake)
    result = await client.fetch_tickers(None)
    assert isinstance(result, Unknown)  # 全都解析不了：不能当成「没有行情」


@pytest.mark.parametrize("bad", ["abc", "0", "-1", None, "", "NaN", True])
async def test_non_positive_or_unparsable_prices_are_never_returned_as_tickers(bad):
    last = ticker24("BTCUSDT", "50000")
    last["info"]["lastPrice"] = bad
    mark = mark_row("BTCUSDT", "50010")
    ok_last, ok_mark = ticker24("币安人生USDT", "0.5"), mark_row("币安人生USDT", "0.51")
    client, _ = client_for(FakeCcxt(tickers=[last, ok_last], marks=[mark, ok_mark]))
    assert set((await client.fetch_tickers(None)).value) == {"币安人生USDT"}
    bad_mark = mark_row("BTCUSDT", "50010")
    bad_mark["info"]["markPrice"] = bad
    client, _ = client_for(FakeCcxt(tickers=[ticker24("BTCUSDT", "50000"), ok_last], marks=[bad_mark, ok_mark]))
    assert set((await client.fetch_tickers(None)).value) == {"币安人生USDT"}


async def test_the_ticker_time_falls_back_to_the_mark_price_time():
    row = ticker24("BTCUSDT", "50000")
    del row["info"]["closeTime"]
    client, _ = client_for(FakeCcxt(tickers=[row], marks=[mark_row("BTCUSDT", "50010", T0 + 7)]))
    assert (await client.fetch_tickers(None)).value["BTCUSDT"].time_ms == T0 + 7


@pytest.mark.parametrize("failing", ["fetch_tickers", "fetch_mark_prices"])
async def test_either_ticker_call_failing_makes_the_result_unknown(failing):
    fake = FakeCcxt()
    fake.errors[failing] = ccxt.NetworkError("boom")
    client, _ = client_for(fake)
    result = await client.fetch_tickers(None)
    assert isinstance(result, Unknown) and "NetworkError" in result.reason


# ================================================================ 服务器时间

async def test_server_time():
    client, fake = client_for()
    assert await client.server_time() == Known(T0)
    assert fake.called("fetch_time") == [((), {})]


@pytest.mark.parametrize("value", [None, "abc", -5, 0, 1.5, True, [T0]])
async def test_an_unusable_server_time_is_unknown(value):
    client, _ = client_for(FakeCcxt(time_ms=value))
    assert isinstance(await client.server_time(), Unknown)


# ================================================================ 失败、超时、取消


@pytest.mark.parametrize("exc, code, http_status", [
    (ccxt.RateLimitExceeded('binanceusdm {"code":-1003,"msg":"Too many requests"}'), -1003, 429),
    (ccxt.DDoSProtection('binanceusdm {"code":-1003,"msg":"IP banned until 1700000000000"}'), -1003, 418),
    (ccxt.RateLimitExceeded("binanceusdm Too Many Requests"), -1003, 429),  # 没带代码：限频按 -1003
    (ccxt.ExchangeNotAvailable('binanceusdm {"code":-1001,"msg":"Internal error"}'), -1001, None),
    (ccxt.BadSymbol('binanceusdm {"code":-1121,"msg":"Invalid symbol."}'), -1121, None),
    (ccxt.NetworkError("connection reset"), None, None),
    (ccxt.RequestTimeout("binanceusdm GET https://fapi.binance.com/fapi/v1/time request timed out"), None, None),
    (RuntimeError("unexpected"), None, None),
    (OSError("no route to host"), None, None),
])
@pytest.mark.parametrize("call", [
    lambda c: c.server_time(), lambda c: c.load_instruments(), lambda c: c.fetch_candles("BTCUSDT", "15m", 3),
    lambda c: c.fetch_tickers(None),
])
async def test_any_ccxt_failure_is_unknown_with_code_and_status(call, exc, code, http_status):
    fake = FakeCcxt()
    for name in ("fetch_time", "load_markets", "fetch_ohlcv", "fetch_tickers", "fetch_mark_prices"):
        fake.errors[name] = exc
    client, _ = client_for(fake)
    result = await call(client)
    assert isinstance(result, Unknown)
    assert result.code == code and result.http_status == http_status
    assert type(exc).__name__ in result.reason


@pytest.mark.parametrize("call", [
    lambda c: c.server_time(), lambda c: c.load_instruments(), lambda c: c.fetch_candles("BTCUSDT", "15m", 3),
    lambda c: c.fetch_tickers(None),
])
async def test_every_request_has_a_timeout(call):
    fake = FakeCcxt()
    fake.hang = {"fetch_time", "load_markets", "fetch_ohlcv", "fetch_tickers", "fetch_mark_prices"}
    client, _ = client_for(fake, timeout_s=0.05)
    result = await asyncio.wait_for(call(client), 5)
    assert isinstance(result, Unknown) and "超时" in result.reason


async def test_cancellation_is_not_swallowed():
    fake = FakeCcxt()
    fake.hang = {"fetch_time"}
    client, _ = client_for(fake, timeout_s=30)
    task = asyncio.create_task(client.server_time())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_whitelist_violation_inside_a_call_is_a_programming_error_not_unknown():
    client, fake = client_for()
    with pytest.raises(WhitelistError):
        await client._fetch("测试", lambda: client.ccxt.create_order("BTC/USDT:USDT", "market", "buy", 1))
    assert fake.calls == [] and fake.forbidden == []


async def test_a_failed_call_does_not_poison_the_next_one():
    fake = FakeCcxt()
    fake.errors["fetch_time"] = ccxt.NetworkError("boom")
    client, _ = client_for(fake)
    assert isinstance(await client.server_time(), Unknown)
    del fake.errors["fetch_time"]
    assert await client.server_time() == Known(T0)


# ================================================================ 关闭


async def test_close_releases_the_single_exchange_instance_and_is_idempotent():
    client, fake = client_for()
    await client.server_time()
    await client.close()
    await client.close()
    assert fake.closed == 1
    result = await client.server_time()  # 关闭后再用：不悄悄重开
    assert isinstance(result, Unknown) and "已关闭" in result.reason


async def test_one_exchange_instance_is_built_and_reused_for_every_call(monkeypatch):
    from dmr_kernel.exchange import binance_public

    built = []

    def factory(timeout_s):
        built.append(timeout_s)
        fake = FakeCcxt()
        built.append(fake)
        return fake

    monkeypatch.setattr(binance_public, "build_default_exchange", factory)
    client = BinancePublicClient(timeout_s=4.0)
    await client.server_time()
    await client.fetch_candles("BTCUSDT", "15m", 3)
    await client.load_instruments()
    await client.close()
    assert built[0] == 4.0 and len(built) == 2  # 只建了一个
    assert built[1].closed == 1 and {n for n, _, _ in built[1].calls} == {"fetch_time", "fetch_ohlcv", "load_markets",
                                                                          "close"}
