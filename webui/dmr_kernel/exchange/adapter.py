"""交易所适配层的统一接口（第 1 层）。内核只经这个接口进出交易所（计划报告 4.8）。

实现：
- ``exchange.standin.StandinAdapter``：替身环境，经回环 HTTP 与 WebSocket 连接独立的替身交易所进程。
- ``exchange.binance.BinanceAdapter``：Binance USDT-M（ccxt 4.5.78）。本轮只实现公共行情与合约规则；
  私有接口在接入 Demo Trading 时实现，在此之前构造时直接拒绝。

写方法的出口校验（基类负责，子类只实现 ``_xxx``）：
1. 必须出示装配时交给本适配层的那一个 ``WriteCapability``（只有协调者和应急入口持有）。
2. 订单与条件单必须带系统编号；应急入口的令牌只接受应急编号（4.6、4.9）。
3. 数量与价格必须为正；全平型止损不得带数量。
4. 只读影子（``writes_disabled``）下，写方法整体禁用，不触达交易所。

读方法失败时返回 ``Unknown``，不返回空列表（T0.4 验收）。
"""
from __future__ import annotations

import abc
from decimal import Decimal
from typing import AsyncIterator

from dmr_kernel.core.types import (
    AccountInfo,
    AlgoOrderInfo,
    AlgoType,
    CancelOutcome,
    CancelResult,
    Candle,
    IncomeInfo,
    Instrument,
    LeverageInfo,
    OrderInfo,
    OrderOrigin,
    OrderSide,
    PositionInfo,
    PositionSide,
    ReadResult,
    RejectKind,
    RestrictionInfo,
    StreamAccount,
    StreamAlgo,
    StreamOrder,
    Ticker,
    TradeInfo,
    WorkingType,
    WriteRejected,
    WriteResult,
)
from dmr_kernel.exchange import ids


# 所有签名请求的时间窗（4.8）：超过它的请求交易所不再受理；F1 第 9 步判定「订单不存在」以此为依据。
SIGN_WINDOW_MS = 5000


class CapabilityError(PermissionError):
    """调用写方法时没有出示正确的写权限令牌。属于程序错误，不是交易所拒绝。"""


class ClientOrderIdError(ValueError):
    """写请求没有带合规的系统编号。漏传时 ccxt 会自动生成 x-cvBPrNm9 前缀的编号，必须在出口拦住。"""


class WriteCapability:
    """写权限令牌。装配层只创建一次并交给协调者；应急入口自己创建 owner='emergency' 的一张。"""

    __slots__ = ("owner",)

    def __init__(self, owner: str):
        self.owner = owner

    def __repr__(self) -> str:
        return f"WriteCapability({self.owner!r})"


StreamItem = StreamOrder | StreamAlgo | StreamAccount


class AccountStream(abc.ABC):
    """账户数据流。判活（静默超时、listenKey 续期）由实现负责，并经事件总线发出连接状态。"""

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @property
    @abc.abstractmethod
    def state(self) -> str:
        """connecting | connected | interrupted | stopped"""

    @abc.abstractmethod
    def items(self) -> AsyncIterator[StreamItem]: ...

    async def check_liveness(self) -> None:
        """判活、续期、退避重连、稳定窗口的评估入口。数据流自身没有定时器：调度器至少每 5 秒调用一次。"""
        return None


class ExchangeAdapter(abc.ABC):
    def __init__(self, *, capability: WriteCapability | None, writes_disabled: bool = False):
        self._capability = capability
        self._writes_disabled = writes_disabled

    # ------------------------------------------------------------ 读方法（各层可用）

    @abc.abstractmethod
    async def server_time(self) -> ReadResult[int]: ...

    @abc.abstractmethod
    async def load_instruments(self) -> ReadResult[list[Instrument]]: ...

    @abc.abstractmethod
    async def fetch_position_mode(self) -> ReadResult[bool]:
        """True 表示对冲模式（dualSidePosition）。"""

    @abc.abstractmethod
    async def fetch_account(self) -> ReadResult[AccountInfo]: ...

    @abc.abstractmethod
    async def fetch_positions(self) -> ReadResult[list[PositionInfo]]:
        """全账户一次拉取，只返回数量非零的持仓，带强平价。"""

    @abc.abstractmethod
    async def fetch_open_orders(self, symbol: str | None = None) -> ReadResult[list[OrderInfo]]: ...

    @abc.abstractmethod
    async def fetch_open_algo_orders(self, symbol: str | None = None) -> ReadResult[list[AlgoOrderInfo]]: ...

    @abc.abstractmethod
    async def fetch_order(self, symbol: str, *, client_order_id: str | None = None,
                          exchange_order_id: str | None = None) -> ReadResult[OrderInfo | None]:
        """Known(None) 表示交易所明确答复「不存在」；查询失败是 Unknown。"""

    @abc.abstractmethod
    async def fetch_algo_order(self, symbol: str, *, client_algo_id: str | None = None,
                               algo_id: str | None = None) -> ReadResult[AlgoOrderInfo | None]: ...

    @abc.abstractmethod
    async def fetch_trades(self, symbol: str, start_ms: int, end_ms: int | None = None,
                           limit: int = 500) -> ReadResult[list[TradeInfo]]: ...

    @abc.abstractmethod
    async def fetch_income(self, start_ms: int, end_ms: int | None = None, income_type: str | None = None,
                           limit: int = 1000) -> ReadResult[list[IncomeInfo]]: ...

    @abc.abstractmethod
    async def fetch_candles(self, symbol: str, interval: str, limit: int = 99) -> ReadResult[list[Candle]]:
        """原样返回交易所的 K 线（可能含未收盘的最后一根）；剔除未收盘由 marketdata 按服务器时间做。"""

    @abc.abstractmethod
    async def fetch_tickers(self, symbols: list[str] | None = None) -> ReadResult[dict[str, Ticker]]: ...

    @abc.abstractmethod
    async def fetch_leverage(self, symbol: str) -> ReadResult[LeverageInfo]: ...

    @abc.abstractmethod
    async def fetch_restrictions(self) -> ReadResult[RestrictionInfo]: ...

    @abc.abstractmethod
    def account_stream(self) -> AccountStream: ...

    def used_weight(self) -> int | None:
        """最近一次响应头里的已用权重（整个出口 IP 的用量）；不知道时为 None。"""
        return None

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------ 写方法（只有协调者与应急入口）

    def _check_capability(self, cap: WriteCapability | None) -> None:
        if self._capability is None or cap is None or cap is not self._capability:
            raise CapabilityError("没有写权限：只有协调者（或内核不可用时的应急入口）能调用适配层的写方法")

    def _check_new_order_id(self, cap: WriteCapability, cid: str | None) -> None:
        if not cid or not ids.SAFE_CHARS.fullmatch(cid):
            raise ClientOrderIdError(f"写请求必须带合规的自定义编号（≤36 个字符）：{cid!r}")
        origin = ids.parse_client_id(cid).origin
        wanted = OrderOrigin.EMERGENCY if cap.owner == "emergency" else OrderOrigin.SYSTEM
        if origin is not wanted:
            raise ClientOrderIdError(f"写请求的编号必须是{('应急' if wanted is OrderOrigin.EMERGENCY else '系统')}编号：{cid!r}")

    @staticmethod
    def _check_cancel_target(cid: str | None, exchange_id: str | None) -> None:
        if cid:
            if ids.parse_client_id(cid).origin not in (OrderOrigin.SYSTEM, OrderOrigin.EMERGENCY):
                raise ClientOrderIdError("按自定义编号撤单只允许系统或应急编号；撤用户的单要用交易所单号")
        elif not exchange_id:
            raise ClientOrderIdError("撤单必须给出自定义编号或交易所单号")

    @staticmethod
    def _positive(name: str, value: Decimal | None) -> None:
        if value is None or not isinstance(value, Decimal) or not value > 0:
            raise ValueError(f"{name} 必须是正的 Decimal：{value!r}")

    def _disabled(self) -> WriteRejected | None:
        if self._writes_disabled:
            return WriteRejected(RejectKind.WRITES_DISABLED, None, "只读影子：写方法在适配层出口禁用")
        return None

    async def create_market_order(self, cap: WriteCapability | None, *, symbol: str, side: OrderSide,
                                  position_side: PositionSide, qty: Decimal, client_order_id: str) -> WriteResult:
        """市价单。对冲模式：直接传持仓方向，不传只减仓标记（4.8：不使用 ccxt 的 hedged/reduceOnly）。"""
        self._check_capability(cap)
        self._check_new_order_id(cap, client_order_id)
        self._positive("qty", qty)
        if (disabled := self._disabled()) is not None:
            return disabled
        return await self._create_market_order(symbol=symbol, side=side, position_side=position_side, qty=qty,
                                               client_order_id=client_order_id)

    async def create_stop_order(self, cap: WriteCapability | None, *, symbol: str, side: OrderSide,
                                position_side: PositionSide, trigger_price: Decimal, working_type: WorkingType,
                                client_algo_id: str, close_position: bool = True, qty: Decimal | None = None,
                                algo_type: AlgoType = AlgoType.STOP_MARKET) -> WriteResult:
        """条件单（默认全平型止损市价）。触发价类型必须显式给出。"""
        self._check_capability(cap)
        self._check_new_order_id(cap, client_algo_id)
        self._positive("trigger_price", trigger_price)
        if close_position and qty is not None:
            raise ValueError("全平型条件单不得带数量")
        if not close_position:
            self._positive("qty", qty)
        if not isinstance(working_type, WorkingType):
            raise ValueError("必须显式给出触发价类型")
        if (disabled := self._disabled()) is not None:
            return disabled
        return await self._create_stop_order(symbol=symbol, side=side, position_side=position_side,
                                             trigger_price=trigger_price, working_type=working_type,
                                             client_algo_id=client_algo_id, close_position=close_position,
                                             qty=qty, algo_type=algo_type)

    async def cancel_order(self, cap: WriteCapability | None, *, symbol: str, client_order_id: str | None = None,
                           exchange_order_id: str | None = None) -> CancelResult:
        self._check_capability(cap)
        self._check_cancel_target(client_order_id, exchange_order_id)
        if self._writes_disabled:
            return CancelResult(CancelOutcome.UNKNOWN, message="只读影子：写方法在适配层出口禁用")
        return await self._cancel_order(symbol=symbol, client_order_id=client_order_id,
                                        exchange_order_id=exchange_order_id)

    async def cancel_algo_order(self, cap: WriteCapability | None, *, symbol: str, client_algo_id: str | None = None,
                                algo_id: str | None = None) -> CancelResult:
        """撤条件单，返回三态：已撤、不存在、未知。"""
        self._check_capability(cap)
        self._check_cancel_target(client_algo_id, algo_id)
        if self._writes_disabled:
            return CancelResult(CancelOutcome.UNKNOWN, message="只读影子：写方法在适配层出口禁用")
        return await self._cancel_algo_order(symbol=symbol, client_algo_id=client_algo_id, algo_id=algo_id)

    async def set_leverage(self, cap: WriteCapability | None, *, symbol: str, leverage: int) -> WriteResult:
        self._check_capability(cap)
        if not isinstance(leverage, int) or leverage < 1:
            raise ValueError("杠杆必须是正整数")
        if (disabled := self._disabled()) is not None:
            return disabled
        return await self._set_leverage(symbol=symbol, leverage=leverage)

    async def set_margin_mode(self, cap: WriteCapability | None, *, symbol: str, margin_mode: str) -> WriteResult:
        self._check_capability(cap)
        if margin_mode not in ("isolated", "cross"):
            raise ValueError("保证金模式只能是 isolated 或 cross")
        if (disabled := self._disabled()) is not None:
            return disabled
        return await self._set_margin_mode(symbol=symbol, margin_mode=margin_mode)

    # ------------------------------------------------------------ 子类实现

    @abc.abstractmethod
    async def _create_market_order(self, *, symbol: str, side: OrderSide, position_side: PositionSide,
                                   qty: Decimal, client_order_id: str) -> WriteResult: ...

    @abc.abstractmethod
    async def _create_stop_order(self, *, symbol: str, side: OrderSide, position_side: PositionSide,
                                 trigger_price: Decimal, working_type: WorkingType, client_algo_id: str,
                                 close_position: bool, qty: Decimal | None, algo_type: AlgoType) -> WriteResult: ...

    @abc.abstractmethod
    async def _cancel_order(self, *, symbol: str, client_order_id: str | None,
                            exchange_order_id: str | None) -> CancelResult: ...

    @abc.abstractmethod
    async def _cancel_algo_order(self, *, symbol: str, client_algo_id: str | None,
                                 algo_id: str | None) -> CancelResult: ...

    @abc.abstractmethod
    async def _set_leverage(self, *, symbol: str, leverage: int) -> WriteResult: ...

    @abc.abstractmethod
    async def _set_margin_mode(self, *, symbol: str, margin_mode: str) -> WriteResult: ...
