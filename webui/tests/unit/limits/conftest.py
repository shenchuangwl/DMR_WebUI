"""合约数上限与订单频率测试的公共夹具：手动时钟、可改的配置字典、构造持仓/挂单/条件单的小工厂。

Limits 是纯内存、同步的：所有时间由调用方显式给出（``at_ms`` 是快照时间，``now_ms`` 是判定时间）。
默认配置：持仓合约数上限 30，仅轮询降级时 10，同一合约 10 分钟内订单数上限 25，持仓对账间隔 10 秒（快照 20 秒没更新就过期），
挂单对账间隔 60 秒（120 秒）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Iterable

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.types import (
    AlgoOrderInfo,
    AlgoType,
    OrderInfo,
    OrderSide,
    PositionInfo,
    PositionSide,
    WorkingType,
)

D = Decimal
T0 = 1_760_000_000_000

DEFAULT_CONFIG: dict[str, Any] = {
    "max_symbols": 30,
    "max_symbols_degraded": 10,
    "max_orders_per_symbol_10m": 25,
    "reconcile_positions_s": 10,
    "reconcile_orders_s": 60,
}


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def config() -> dict[str, Any]:
    return dict(DEFAULT_CONFIG)


@pytest.fixture
def limits(clock, config) -> Limits:
    return Limits(clock, config_get=config.__getitem__)


@pytest.fixture
def make_position() -> Callable[..., PositionInfo]:
    def make(symbol: str = "BTCUSDT", side: PositionSide = PositionSide.LONG, qty: str = "0.01") -> PositionInfo:
        return PositionInfo(symbol=symbol, side=side, qty=D(qty), entry_price=D("60000"))

    return make


@pytest.fixture
def make_order() -> Callable[..., OrderInfo]:
    def make(symbol: str = "BTCUSDT", side: PositionSide = PositionSide.LONG, status: str = "NEW") -> OrderInfo:
        return OrderInfo(symbol=symbol, client_order_id=None, exchange_order_id="1",
                         side=side.open_side(), position_side=side, order_type="LIMIT", qty=D("0.01"),
                         filled_qty=D("0"), avg_price=None, status=status, raw_status=status)

    return make


@pytest.fixture
def make_algo() -> Callable[..., AlgoOrderInfo]:
    def make(symbol: str = "BTCUSDT", side: PositionSide = PositionSide.LONG, raw_status: str = "NEW") -> AlgoOrderInfo:
        return AlgoOrderInfo(symbol=symbol, client_algo_id=None, algo_id="9", side=OrderSide.SELL,
                             position_side=side, algo_type=AlgoType.STOP_MARKET, trigger_price=D("59000"),
                             working_type=WorkingType.CONTRACT_PRICE, close_position=True, qty=None,
                             raw_status=raw_status, actual_order_id=None, create_time_ms=T0, update_time_ms=T0)

    return make


@pytest.fixture
def feed(limits) -> Callable[..., None]:
    """把持仓快照与挂单快照一起喂给 limits（at_ms 默认 T0）。"""

    def do(positions: Iterable[PositionInfo] = (), orders: Iterable[OrderInfo] = (),
           algos: Iterable[AlgoOrderInfo] = (), at_ms: int = T0) -> None:
        limits.update_positions(positions, at_ms)
        limits.update_orders(orders, algos, at_ms)

    return do


@pytest.fixture
def symbols() -> Callable[[int], list[str]]:
    """n 个不同的合约代码。"""
    return lambda n: [f"C{i:02d}USDT" for i in range(n)]
