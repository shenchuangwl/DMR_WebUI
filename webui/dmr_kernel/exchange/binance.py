"""Binance USDⓈ-M 的适配层（第 1 层）。本轮只有公共行情与合约规则，委托 ``BinancePublicClient``（ccxt 4.5.78）。

私有读（账户、持仓、挂单、回查、成交、收益、杠杆、交易限制）、全部写方法与账户数据流一律抛 ``NotImplementedError``：
它们要在接入 Demo Trading、做完报告 6.1 的 16 项实测之后实现。构造时带写权限令牌也直接抛——在此之前，
任何装配都不可能让这个适配层拿到写权限。
"""
from __future__ import annotations

from decimal import Decimal
from typing import NoReturn

from dmr_kernel.core.types import (
    AccountInfo,
    AlgoOrderInfo,
    AlgoType,
    CancelResult,
    Candle,
    IncomeInfo,
    Instrument,
    LeverageInfo,
    OrderInfo,
    OrderSide,
    PositionInfo,
    PositionSide,
    ReadResult,
    RestrictionInfo,
    Ticker,
    TradeInfo,
    WorkingType,
    WriteResult,
)
from dmr_kernel.exchange.adapter import AccountStream, ExchangeAdapter, WriteCapability
from dmr_kernel.exchange.binance_public import MAX_CANDLES, BinancePublicClient

PRIVATE_NOT_READY = "Binance 私有接口要在接入 Demo Trading 时实现（6.1 的 16 项实测之后）"


def _not_ready() -> NoReturn:
    raise NotImplementedError(PRIVATE_NOT_READY)


class BinanceAdapter(ExchangeAdapter):
    def __init__(self, *, capability: WriteCapability | None = None, writes_disabled: bool = False,
                 public: BinancePublicClient | None = None, timeout_s: float = 10.0):
        if capability is not None:
            _not_ready()
        super().__init__(capability=None, writes_disabled=writes_disabled)
        self._public = public if public is not None else BinancePublicClient(timeout_s=timeout_s)

    # ------------------------------------------------------------ 公共读方法（委托）

    async def server_time(self) -> ReadResult[int]:
        return await self._public.server_time()

    async def load_instruments(self) -> ReadResult[list[Instrument]]:
        return await self._public.load_instruments()

    async def fetch_candles(self, symbol: str, interval: str, limit: int = MAX_CANDLES) -> ReadResult[list[Candle]]:
        return await self._public.fetch_candles(symbol, interval, limit)

    async def fetch_tickers(self, symbols: list[str] | None = None) -> ReadResult[dict[str, Ticker]]:
        return await self._public.fetch_tickers(symbols)

    async def close(self) -> None:
        await self._public.close()

    # ------------------------------------------------------------ 私有读方法与数据流：尚未实现

    async def fetch_position_mode(self) -> ReadResult[bool]:
        _not_ready()

    async def fetch_account(self) -> ReadResult[AccountInfo]:
        _not_ready()

    async def fetch_positions(self) -> ReadResult[list[PositionInfo]]:
        _not_ready()

    async def fetch_open_orders(self, symbol: str | None = None) -> ReadResult[list[OrderInfo]]:
        _not_ready()

    async def fetch_open_algo_orders(self, symbol: str | None = None) -> ReadResult[list[AlgoOrderInfo]]:
        _not_ready()

    async def fetch_order(self, symbol: str, *, client_order_id: str | None = None,
                          exchange_order_id: str | None = None) -> ReadResult[OrderInfo | None]:
        _not_ready()

    async def fetch_algo_order(self, symbol: str, *, client_algo_id: str | None = None,
                               algo_id: str | None = None) -> ReadResult[AlgoOrderInfo | None]:
        _not_ready()

    async def fetch_trades(self, symbol: str, start_ms: int, end_ms: int | None = None,
                           limit: int = 500) -> ReadResult[list[TradeInfo]]:
        _not_ready()

    async def fetch_income(self, start_ms: int, end_ms: int | None = None, income_type: str | None = None,
                           limit: int = 1000) -> ReadResult[list[IncomeInfo]]:
        _not_ready()

    async def fetch_leverage(self, symbol: str) -> ReadResult[LeverageInfo]:
        _not_ready()

    async def fetch_restrictions(self) -> ReadResult[RestrictionInfo]:
        _not_ready()

    def account_stream(self) -> AccountStream:
        _not_ready()

    # ------------------------------------------------------------ 写方法：尚未实现

    def _check_capability(self, cap: WriteCapability | None) -> None:
        # 每个写方法的第一步：不论出示什么令牌，都明确告知「尚未实现」，而不是含糊的「没有写权限」
        _not_ready()

    async def _create_market_order(self, *, symbol: str, side: OrderSide, position_side: PositionSide, qty: Decimal,
                                   client_order_id: str) -> WriteResult:
        _not_ready()

    async def _create_stop_order(self, *, symbol: str, side: OrderSide, position_side: PositionSide,
                                 trigger_price: Decimal, working_type: WorkingType, client_algo_id: str,
                                 close_position: bool, qty: Decimal | None, algo_type: AlgoType) -> WriteResult:
        _not_ready()

    async def _cancel_order(self, *, symbol: str, client_order_id: str | None,
                            exchange_order_id: str | None) -> CancelResult:
        _not_ready()

    async def _cancel_algo_order(self, *, symbol: str, client_algo_id: str | None,
                                 algo_id: str | None) -> CancelResult:
        _not_ready()

    async def _set_leverage(self, *, symbol: str, leverage: int) -> WriteResult:
        _not_ready()

    async def _set_margin_mode(self, *, symbol: str, margin_mode: str) -> WriteResult:
        _not_ready()
