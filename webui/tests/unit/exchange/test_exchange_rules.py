"""exchange.rules：合约规则缓存（刷新失败保持旧缓存）与名单合约代码的精确查表（F10 第 4 步）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.types import Instrument, Known, Unknown
from dmr_kernel.exchange.errors import AuthConfigError
from dmr_kernel.exchange.rules import InstrumentRules
from tests.support.fake_adapter import FakeAdapter

D = Decimal


def inst(symbol, *, base=None, quote="USDT", contract_type="PERPETUAL", status="TRADING", tick="0.1", step="0.001",
         min_qty="0.001", min_notional="5"):
    return Instrument(symbol=symbol, base=base or symbol.removesuffix(quote), quote=quote, contract_type=contract_type,
                      status=status, tick_size=D(tick), step_size=D(step), min_qty=D(min_qty),
                      min_notional=D(min_notional), market_step_size=D(step), market_max_qty=D("1000"),
                      onboard_ms=1_569_398_400_000)


BTC = inst("BTCUSDT")
ETH = inst("ETHUSDT", tick="0.01")
PEPE = inst("1000PEPEUSDT", base="1000PEPE", tick="0.0000001", step="1", min_qty="1")
CN = inst("哈基米USDT", base="哈基米", tick="0.00001", step="1", min_qty="1")
QUARTER = inst("BTCUSDT_261225", base="BTC", contract_type="CURRENT_QUARTER")
SETTLING = inst("LUNAUSDT", status="SETTLING")
USDC = inst("BTCUSDC", base="BTC", quote="USDC")
ALL = [BTC, ETH, PEPE, CN, QUARTER, SETTLING, USDC]


def make(instruments=ALL):
    clock = ManualClock(1_791_943_200_000)
    adapter = FakeAdapter()
    adapter.set_read("load_instruments", list(instruments))
    return InstrumentRules(clock), adapter, clock


async def test_fresh_rules_are_empty():
    rules, _, _ = make()
    assert rules.refreshed_ms is None
    assert rules.all() == [] and rules.get("BTCUSDT") is None and rules.lookup_list_code("BTCUSDT") is None


async def test_refresh_loads_the_cache_and_stamps_the_time():
    rules, adapter, clock = make()
    assert await rules.refresh(adapter) is True
    assert rules.refreshed_ms == clock.now_ms()
    assert rules.get("BTCUSDT") == BTC
    assert sorted(i.symbol for i in rules.all()) == sorted(i.symbol for i in ALL)


async def test_all_returns_a_copy():
    rules, adapter, _ = make()
    await rules.refresh(adapter)
    listing = rules.all()
    listing.clear()
    assert len(rules.all()) == len(ALL)


async def test_get_knows_every_instrument_including_untradable_ones():
    rules, adapter, _ = make()
    await rules.refresh(adapter)
    # 持仓所在的合约即使已不在交易状态，也要能查到规则
    assert rules.get("LUNAUSDT") == SETTLING
    assert rules.get("BTCUSDT_261225") == QUARTER
    assert rules.get("NOPEUSDT") is None


# ---------------------------------------------------------------- 名单合约代码的精确查表


async def test_lookup_returns_only_trading_usdt_perpetuals():
    rules, adapter, _ = make()
    await rules.refresh(adapter)
    assert rules.lookup_list_code("BTCUSDT") == BTC
    assert rules.lookup_list_code("ETHUSDT") == ETH
    assert rules.lookup_list_code("LUNAUSDT") is None  # 不在交易状态
    assert rules.lookup_list_code("BTCUSDT_261225") is None  # 交割合约
    assert rules.lookup_list_code("BTCUSDC") is None  # 计价币不是 USDT


async def test_lookup_handles_1000_prefix_and_chinese_contract_names_by_raw_code():
    rules, adapter, _ = make()
    await rules.refresh(adapter)
    assert rules.lookup_list_code("1000PEPEUSDT") == PEPE
    assert rules.lookup_list_code("哈基米USDT") == CN


async def test_lookup_never_builds_symbols_by_string_concatenation():
    rules, adapter, _ = make()
    await rules.refresh(adapter)
    assert rules.lookup_list_code("BTC") is None  # 不补 USDT
    assert rules.lookup_list_code("PEPEUSDT") is None  # 不猜 1000 前缀
    assert rules.lookup_list_code("PEPE") is None
    assert rules.lookup_list_code("哈基米") is None
    assert rules.lookup_list_code("btcusdt") is None  # 不改大小写
    assert rules.lookup_list_code(" BTCUSDT") is None  # 不去空白
    assert rules.lookup_list_code("BTC/USDT:USDT") is None  # 不认 ccxt 的统一写法
    assert rules.lookup_list_code("") is None


async def test_lookup_matches_the_instrument_tradable_property():
    rules, adapter, _ = make()
    await rules.refresh(adapter)
    for i in ALL:
        assert (rules.lookup_list_code(i.symbol) is not None) == i.tradable


# ---------------------------------------------------------------- 刷新失败


async def test_failed_refresh_keeps_the_old_cache_and_time():
    rules, adapter, clock = make()
    assert await rules.refresh(adapter) is True
    stamped = rules.refreshed_ms
    adapter.set_read("load_instruments", Unknown("请求超时"))
    clock.advance(60_000)
    assert await rules.refresh(adapter) is False
    assert rules.refreshed_ms == stamped
    assert rules.get("BTCUSDT") == BTC and len(rules.all()) == len(ALL)


async def test_first_refresh_failing_leaves_the_rules_empty_not_invented():
    rules, adapter, _ = make()
    adapter.set_read("load_instruments", Unknown("连接中断"))
    assert await rules.refresh(adapter) is False
    assert rules.refreshed_ms is None and rules.all() == []


async def test_an_empty_listing_is_treated_as_a_failure():
    rules, adapter, _ = make()
    await rules.refresh(adapter)
    adapter.set_read("load_instruments", Known([]))
    assert await rules.refresh(adapter) is False
    assert rules.get("BTCUSDT") == BTC


async def test_successful_refresh_replaces_the_whole_cache():
    rules, adapter, clock = make()
    await rules.refresh(adapter)
    clock.advance(3_600_000)
    changed = inst("BTCUSDT", tick="0.5")
    adapter.set_read("load_instruments", [changed, ETH])  # 其余合约已下架
    assert await rules.refresh(adapter) is True
    assert rules.refreshed_ms == clock.now_ms()
    assert rules.get("BTCUSDT") == changed
    assert rules.get("1000PEPEUSDT") is None and rules.lookup_list_code("哈基米USDT") is None
    assert len(rules.all()) == 2


async def test_programming_errors_from_the_adapter_are_not_swallowed():
    rules, adapter, _ = make()
    await rules.refresh(adapter)

    def boom(**_):
        raise AuthConfigError("令牌无效")

    adapter.set_read("load_instruments", boom)
    with pytest.raises(AuthConfigError):
        await rules.refresh(adapter)
    assert rules.get("BTCUSDT") == BTC  # 缓存没被破坏


async def test_duplicate_symbols_keep_the_last_one():
    rules, adapter, _ = make([inst("BTCUSDT", tick="0.1"), inst("BTCUSDT", tick="0.5")])
    await rules.refresh(adapter)
    assert rules.get("BTCUSDT").tick_size == D("0.5")
    assert len(rules.all()) == 1
