"""Binance 公共行情源与 auto 模式（计划 T0.5「行情源」）。用假的 ccxt 对象，不连任何网络。

假对象只提供白名单里的公共方法；访问任何其它属性立即失败，借此证明行情源不会调用私有接口。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from dmr_standin.feed import (ALLOWED_PUBLIC_METHODS, BinancePublicFeed, FeedError, ReplayFeed, make_exchange,
                              open_feed)
from standin_kit import T0, FakeClock

DATA = Path(__file__).resolve().parents[3] / "bit" / "data"

EXCHANGE_INFO = {
    "serverTime": T0,
    "symbols": [
        {"symbol": "BTCUSDT", "pair": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING",
         "baseAsset": "BTC", "quoteAsset": "USDT", "onboardDate": 1569398400000,
         "filters": [
             {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "556.80", "maxPrice": "4529764"},
             {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
             {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "120"},
             {"filterType": "MIN_NOTIONAL", "notional": "100"},
         ]},
        {"symbol": "币安人生USDT", "pair": "币安人生USDT", "contractType": "PERPETUAL", "status": "TRADING",
         "baseAsset": "币安人生", "quoteAsset": "USDT", "onboardDate": 1760000000000,
         "filters": [
             {"filterType": "PRICE_FILTER", "tickSize": "0.0000100"},
             {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
             {"filterType": "MARKET_LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "2000000"},
             {"filterType": "MIN_NOTIONAL", "notional": "5"},
         ]},
        {"symbol": "ETHUSDT_261225", "pair": "ETHUSDT", "contractType": "CURRENT_QUARTER", "status": "TRADING",
         "baseAsset": "ETH", "quoteAsset": "USDT", "onboardDate": 1750000000000,
         "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                     {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                     {"filterType": "MIN_NOTIONAL", "notional": "20"}]},
    ],
}


class FakeExchange:
    """只有白名单里的公共方法；其余任何属性访问都立即失败。"""

    apiKey = ""
    secret = ""

    def __init__(self, *, delay: float = 0.0, fail_times: int = 0):
        self.calls: list[tuple[str, dict]] = []
        self.delay = delay
        self.fail_times = fail_times
        self.closed = False
        self.prices = {"BTCUSDT": "50000.1", "币安人生USDT": "0.12345"}
        self.marks = {"BTCUSDT": ("50001.5", "0.00010000"), "币安人生USDT": ("0.12340", "-0.00030000")}

    def __getattr__(self, name):
        raise AssertionError(f"调用了白名单以外的方法或属性：{name}")

    async def _enter(self, name, params):
        self.calls.append((name, dict(params or {})))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("模拟网络错误")

    async def fapiPublicGetExchangeInfo(self, params=None):
        await self._enter("fapiPublicGetExchangeInfo", params)
        return EXCHANGE_INFO

    async def fapiPublicGetTime(self, params=None):
        await self._enter("fapiPublicGetTime", params)
        return {"serverTime": T0}

    async def fapiPublicGetKlines(self, params=None):
        await self._enter("fapiPublicGetKlines", params)
        limit = int(params["limit"])
        return [[T0 - 900_000 * (limit - i), "1.0", "2.0", "0.5", "1.5", "10.0", T0 - 900_000 * (limit - i - 1) - 1,
                 "15.0", 3, "5.0", "7.5", "0"] for i in range(limit)]

    async def fapiPublicGetTickerPrice(self, params=None):
        await self._enter("fapiPublicGetTickerPrice", params)
        if params and "symbol" in params:
            return {"symbol": params["symbol"], "price": self.prices[params["symbol"]], "time": T0}
        return [{"symbol": s, "price": p, "time": T0} for s, p in self.prices.items()]

    async def fapiPublicGetPremiumIndex(self, params=None):
        await self._enter("fapiPublicGetPremiumIndex", params)
        rows = [{"symbol": s, "markPrice": m, "lastFundingRate": r, "time": T0} for s, (m, r) in self.marks.items()]
        if params and "symbol" in params:
            return [r for r in rows if r["symbol"] == params["symbol"]][0]
        return rows

    async def close(self):
        self.closed = True


def _feed(ex: FakeExchange, **kw) -> BinancePublicFeed:
    kw.setdefault("timeout_s", 0.2)
    kw.setdefault("retries", 2)
    kw.setdefault("backoff_s", 0.001)
    return BinancePublicFeed(clock=FakeClock(), exchange_factory=lambda: ex, **kw)


def test_whitelist_is_public_get_only():
    assert ALLOWED_PUBLIC_METHODS == {"fapiPublicGetExchangeInfo", "fapiPublicGetTime", "fapiPublicGetKlines",
                                      "fapiPublicGetTickerPrice", "fapiPublicGetPremiumIndex"}


async def test_instruments_parsed_from_exchange_info():
    ex = FakeExchange()
    feed = _feed(ex)
    await feed.start()
    try:
        insts = {i.symbol: i for i in feed.instruments()}
        assert set(insts) == {"BTCUSDT", "币安人生USDT", "ETHUSDT_261225"}
        btc = insts["BTCUSDT"]
        assert (btc.tick_size, btc.step_size, btc.min_qty, btc.min_notional) == (
            Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), Decimal("100"))
        assert btc.market_step_size == Decimal("0.001") and btc.market_max_qty == Decimal("120")
        assert btc.contract_type == "PERPETUAL" and btc.onboard_ms == 1569398400000
        cn = insts["币安人生USDT"]
        assert cn.tick_size == Decimal("0.00001") and cn.base == "币安人生"  # 中文合约名照原样
        quarterly = insts["ETHUSDT_261225"]
        assert quarterly.contract_type == "CURRENT_QUARTER" and quarterly.market_step_size is None
        assert feed.status()["source"] == "binance" and feed.status()["healthy"] is True
    finally:
        await feed.stop()
    assert ex.closed  # 关停时关闭长连接
    assert {name for name, _ in ex.calls} <= ALLOWED_PUBLIC_METHODS


async def test_klines_are_mapped_to_protocol_rows():
    ex = FakeExchange()
    feed = _feed(ex)
    await feed.start()
    try:
        bars = await feed.klines("BTCUSDT", "15m", 3)
        assert len(bars) == 3 and bars[0] == [T0 - 2_700_000, "1", "2", "0.5", "1.5", "10", T0 - 1_800_000 - 1]
        assert ex.calls[-1] == ("fapiPublicGetKlines", {"symbol": "BTCUSDT", "interval": "15m", "limit": 3})
        assert "BTCUSDT" in feed.watched()  # 最近被请求过 K 线的合约进入价格轮询
    finally:
        await feed.stop()


async def test_poll_fetches_last_and_mark_prices_of_interesting_symbols():
    ex = FakeExchange()
    feed = _feed(ex)
    await feed.start()
    try:
        feed.set_interest({"BTCUSDT"})
        await feed.poll_once(marks=True)
        q = feed.quote("BTCUSDT")
        assert q.last == Decimal("50000.1") and q.mark == Decimal("50001.5")
        assert feed.funding_rate("BTCUSDT") == Decimal("0.0001")
        names = [n for n, _ in ex.calls]
        assert "fapiPublicGetTickerPrice" in names and "fapiPublicGetPremiumIndex" in names
        assert feed.quote("币安人生USDT") is None  # 不关心的合约不取
    finally:
        await feed.stop()


async def test_ensure_quotes_fetches_missing_symbol_on_demand():
    ex = FakeExchange()
    feed = _feed(ex)
    await feed.start()
    try:
        await feed.ensure_quotes(["币安人生USDT"])
        q = feed.quote("币安人生USDT")
        assert q.last == Decimal("0.12345") and q.mark == Decimal("0.12340")
        assert feed.funding_rate("币安人生USDT") == Decimal("-0.0003")
    finally:
        await feed.stop()


async def test_every_request_has_a_timeout_and_is_retried_with_backoff():
    ex = FakeExchange(delay=5)
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    feed = _feed(ex, timeout_s=0.05, retries=2, backoff_s=0.5, sleep=fake_sleep)
    with pytest.raises(FeedError):
        await asyncio.wait_for(feed.start(), 3)
    assert [n for n, _ in ex.calls] == ["fapiPublicGetExchangeInfo"] * 3  # 1 次 + 重试 2 次
    assert sleeps == [0.5, 1.0]  # 指数退避
    assert feed.status()["healthy"] is False and feed.status()["errors"]
    await feed.stop()


async def test_transient_failure_recovers_after_retry():
    ex = FakeExchange(fail_times=2)
    feed = _feed(ex, retries=3)
    await feed.start()
    try:
        assert len(feed.instruments()) == 3
        assert [n for n, _ in ex.calls].count("fapiPublicGetExchangeInfo") == 3
    finally:
        await feed.stop()


async def test_non_whitelisted_method_fails_immediately_without_calling_exchange():
    ex = FakeExchange()
    feed = _feed(ex)
    with pytest.raises(PermissionError):
        await feed.call("fapiPrivateGetAccount", {})
    with pytest.raises(PermissionError):
        await feed.call("fapiPublicPostListenKey", {})
    assert ex.calls == []


async def test_exchange_with_api_key_is_refused():
    ex = FakeExchange()
    ex.apiKey = "abc"  # 带密钥的实例一律拒绝
    feed = _feed(ex)
    with pytest.raises(PermissionError):
        await feed.start()


async def test_default_exchange_has_no_credentials_and_linear_only():
    ex = make_exchange(timeout_s=7)
    try:
        assert not ex.apiKey and not ex.secret
        assert ex.timeout == 7000
        assert ex.options["fetchMarkets"]["types"] == ["linear"]
        assert ex.options["fetchCurrencies"] is False
        assert ex.id == "binanceusdm"
    finally:
        await ex.close()


# ------------------------------------------------------------------ auto 模式


async def test_auto_falls_back_to_replay_when_binance_unavailable():
    ex = FakeExchange(fail_times=100)
    feed = await open_feed("auto", clock=FakeClock(), replay_dir=DATA, exchange_factory=lambda: ex,
                           probe_timeout_s=2, retries=1, backoff_s=0.001)
    try:
        assert isinstance(feed, ReplayFeed)
        status = feed.status()
        assert status["source"] == "replay"
        assert any("Binance" in e["msg"] for e in status["errors"])  # 标明为什么改用回放
    finally:
        await feed.stop()
    assert ex.closed


async def test_auto_falls_back_when_probe_exceeds_its_deadline():
    ex = FakeExchange(delay=5)
    feed = await open_feed("auto", clock=FakeClock(), replay_dir=DATA, exchange_factory=lambda: ex,
                           probe_timeout_s=0.1, timeout_s=10, retries=0)
    try:
        assert feed.status()["source"] == "replay"
    finally:
        await feed.stop()


async def test_auto_uses_binance_when_probe_succeeds():
    ex = FakeExchange()
    feed = await open_feed("auto", clock=FakeClock(), replay_dir=DATA, exchange_factory=lambda: ex,
                           probe_timeout_s=2)
    try:
        assert isinstance(feed, BinancePublicFeed) and feed.status()["source"] == "binance"
        names = [n for n, _ in ex.calls]
        assert names[:2] == ["fapiPublicGetExchangeInfo", "fapiPublicGetKlines"]  # 合约清单 + 一次 K 线
    finally:
        await feed.stop()


async def test_binance_mode_does_not_fall_back():
    ex = FakeExchange(fail_times=100)
    with pytest.raises(FeedError):
        await open_feed("binance", clock=FakeClock(), replay_dir=DATA, exchange_factory=lambda: ex,
                        probe_timeout_s=2, retries=0)


async def test_replay_mode_never_touches_binance():
    def boom():
        raise AssertionError("回放模式不应创建 Binance 连接")

    feed = await open_feed("replay", clock=FakeClock(), replay_dir=DATA, exchange_factory=boom)
    try:
        assert feed.status()["source"] == "replay"
    finally:
        await feed.stop()
