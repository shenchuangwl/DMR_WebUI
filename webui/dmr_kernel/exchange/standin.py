"""替身环境的适配层（第 1 层）：经 ``Transport`` 连接独立的替身交易所进程（协议见 docs/设计约定.md 7.2）。

读方法：成功 -> ``Known``（字符串数值解析成 ``Decimal``）；查询失败、超时、连接中断、限频、响应无法解析 -> ``Unknown``，
**绝不返回空列表**。``fetch_order`` / ``fetch_algo_order`` 收到 -2013 才是 ``Known(None)``（交易所明确答复不存在）。
写方法：受理 -> ``WriteAccepted``；业务拒绝 -> ``WriteRejected(kind, code, msg)``；超时、连接中断、503、已受理但响应无法解析
-> ``WriteUnknown``（必须先按编号回查）。撤单：成功 -> CANCELLED，-2011 -> NOT_FOUND，其余 -> UNKNOWN。
401/403（令牌错误）抛 ``AuthConfigError``——这是配置错误，不是「未知」也不是「被拒」。

限频：每个响应头的 ``X-MBX-USED-WEIGHT-1M`` 与状态码交给 ``RateLimiter``。已用权重超过 ``weight_soft_limit`` 或 429/418 退避期间，
**非保护类读请求**（K 线、价格、成交、收益流水、合约清单、交易所限制）不发请求，直接返回 ``Unknown``（由调用方下个周期再试）；
**保护类读请求**（持仓、账户、按编号回查、挂单与条件单，以及服务器时间、持仓模式、杠杆这几个小请求）和全部写请求不受限。

解码函数 ``decode_*`` 把协议里的 JSON 解析成 ``core.types`` 数据类；缺字段、坏数值、未知枚举统一抛 ``DecodeError``。
数据流（``exchange.stream``）与适配层共用这些解码函数。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping, TypeVar

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.types import (
    AccountInfo,
    AlgoOrderInfo,
    AlgoType,
    CancelOutcome,
    CancelResult,
    Candle,
    IncomeInfo,
    Instrument,
    Known,
    LeverageInfo,
    OrderInfo,
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
    Unknown,
    WorkingType,
    WriteAccepted,
    WriteRejected,
    WriteResult,
    WriteUnknown,
)
from dmr_kernel.exchange.adapter import AccountStream, ExchangeAdapter, WriteCapability
from dmr_kernel.exchange.errors import classify, parse_error_body
from dmr_kernel.exchange.ratelimit import RateLimiter
from dmr_kernel.exchange.transport import Response, Transport, TransportConnectionError, TransportError, TransportTimeout

log = logging.getLogger(__name__)

T = TypeVar("T")

MAX_CANDLES = 99  # 一次最多取多少根 K 线（权重最低的档位）

# ====================================================================== 解码（替身协议 -> core.types）


class DecodeError(ValueError):
    """响应的形状或取值不符合协议。"""


def _obj(value: Any, what: str = "响应") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DecodeError(f"{what}应是 JSON 对象：{type(value).__name__}")
    return value


def _list(value: Any, what: str = "响应") -> list[Any]:
    if not isinstance(value, list):
        raise DecodeError(f"{what}应是 JSON 数组：{type(value).__name__}")
    return value


def _field(d: Mapping[str, Any], key: str) -> Any:
    value = d.get(key)
    if value is None:
        raise DecodeError(f"缺少字段 {key}")
    return value


def _to_decimal(value: Any, key: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DecodeError(f"字段 {key} 应是字符串数值：{value!r}")
    try:
        out = Decimal(str(value).strip())
    except InvalidOperation:
        raise DecodeError(f"字段 {key} 不是数值：{value!r}") from None
    if not out.is_finite():
        raise DecodeError(f"字段 {key} 不是有限数值：{value!r}")
    return out


def _dec(d: Mapping[str, Any], key: str) -> Decimal:
    return _to_decimal(_field(d, key), key)


def _dec_opt(d: Mapping[str, Any], key: str) -> Decimal | None:
    value = d.get(key)
    return None if value is None else _to_decimal(value, key)


def _to_int(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise DecodeError(f"字段 {key} 应是整数：{value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise DecodeError(f"字段 {key} 应是整数：{value!r}")


def _int(d: Mapping[str, Any], key: str) -> int:
    return _to_int(_field(d, key), key)


def _int_opt(d: Mapping[str, Any], key: str, default: int | None = None) -> int | None:
    value = d.get(key)
    return default if value is None else _to_int(value, key)


def _str(d: Mapping[str, Any], key: str) -> str:
    value = _field(d, key)
    if not isinstance(value, str):
        raise DecodeError(f"字段 {key} 应是字符串：{value!r}")
    return value


def _str_opt(d: Mapping[str, Any], key: str) -> str | None:
    value = d.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise DecodeError(f"字段 {key} 应是字符串：{value!r}")
    return value


def _id(d: Mapping[str, Any], key: str) -> str:
    value = _field(d, key)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DecodeError(f"字段 {key} 应是编号：{value!r}")
    return str(value)


def _id_opt(d: Mapping[str, Any], key: str) -> str | None:
    value = d.get(key)
    if value is None or value == "":
        return None
    return _id(d, key)


def _bool(d: Mapping[str, Any], key: str, default: bool | None = None) -> bool:
    value = d.get(key)
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise DecodeError(f"字段 {key} 应是 true 或 false：{value!r}")
    return value


def _enum(cls: type[Enum], d: Mapping[str, Any], key: str) -> Any:
    value = _str(d, key)
    try:
        return cls(value)
    except ValueError:
        raise DecodeError(f"字段 {key} 的取值不认识：{value!r}") from None


def decode_instrument(d: Any) -> Instrument:
    d = _obj(d, "合约规则")
    return Instrument(
        symbol=_str(d, "symbol"), base=_str(d, "base"), quote=_str(d, "quote"),
        contract_type=_str(d, "contract_type"), status=_str(d, "status"),
        tick_size=_dec(d, "tick_size"), step_size=_dec(d, "step_size"), min_qty=_dec(d, "min_qty"),
        min_notional=_dec(d, "min_notional"), market_step_size=_dec_opt(d, "market_step_size"),
        market_max_qty=_dec_opt(d, "market_max_qty"), onboard_ms=_int_opt(d, "onboard_ms"))


def decode_account(d: Any) -> AccountInfo:
    d = _obj(d, "账户")
    return AccountInfo(equity=_dec(d, "equity"), wallet_balance=_dec(d, "wallet_balance"),
                       available_balance=_dec(d, "available_balance"), unrealized_pnl=_dec(d, "unrealized_pnl"),
                       update_time_ms=_int(d, "update_time_ms"))


def decode_position(d: Any) -> PositionInfo:
    """数量取绝对值；强平价为 0（交易所用 0 表示没有）按 ``None``。"""
    d = _obj(d, "持仓")
    liquidation = _dec_opt(d, "liquidation_price")
    return PositionInfo(
        symbol=_str(d, "symbol"), side=_enum(PositionSide, d, "position_side"), qty=abs(_dec(d, "qty")),
        entry_price=_dec(d, "entry_price"), mark_price=_dec_opt(d, "mark_price"),
        liquidation_price=liquidation if liquidation is not None and liquidation > 0 else None,
        unrealized_pnl=_dec_opt(d, "unrealized_pnl"), leverage=_int_opt(d, "leverage"),
        margin_mode=_str_opt(d, "margin_mode"), update_time_ms=_int_opt(d, "update_time_ms", 0) or 0)


def decode_order(d: Any) -> OrderInfo:
    """平均成交价 ≤ 0（没有成交）按 ``None``。"""
    d = _obj(d, "订单")
    avg = _dec_opt(d, "avg_price")
    return OrderInfo(
        symbol=_str(d, "symbol"), client_order_id=_str_opt(d, "client_order_id"), exchange_order_id=_id(d, "order_id"),
        side=_enum(OrderSide, d, "side"), position_side=_enum(PositionSide, d, "position_side"),
        order_type=_str(d, "type"), qty=_dec(d, "qty"), filled_qty=_dec(d, "filled_qty"),
        avg_price=avg if avg is not None and avg > 0 else None, status=_str(d, "status"),
        raw_status=_str(d, "raw_status"), close_position=_bool(d, "close_position", False),
        reduce_only=_bool(d, "reduce_only", False), update_time_ms=_int_opt(d, "update_time_ms", 0) or 0)


def decode_algo(d: Any) -> AlgoOrderInfo:
    d = _obj(d, "条件单")
    return AlgoOrderInfo(
        symbol=_str(d, "symbol"), client_algo_id=_str_opt(d, "client_algo_id"), algo_id=_id(d, "algo_id"),
        side=_enum(OrderSide, d, "side"), position_side=_enum(PositionSide, d, "position_side"),
        algo_type=_enum(AlgoType, d, "type"), trigger_price=_dec(d, "trigger_price"),
        working_type=_enum(WorkingType, d, "working_type"), close_position=_bool(d, "close_position"),
        qty=_dec_opt(d, "qty"), raw_status=_str(d, "status"), actual_order_id=_id_opt(d, "actual_order_id"),
        create_time_ms=_int(d, "create_time_ms"), update_time_ms=_int(d, "update_time_ms"))


def decode_trade(d: Any) -> TradeInfo:
    """成交不带自定义编号（与 Binance REST 一致），用 ``exchange_order_id`` 关联。"""
    d = _obj(d, "成交")
    return TradeInfo(
        trade_id=_id(d, "trade_id"), exchange_order_id=_id(d, "order_id"), symbol=_str(d, "symbol"),
        side=_enum(OrderSide, d, "side"), position_side=_enum(PositionSide, d, "position_side"),
        qty=_dec(d, "qty"), price=_dec(d, "price"), commission=_dec(d, "commission"),
        commission_asset=_str(d, "commission_asset"), realized_pnl=_dec(d, "realized_pnl"), time_ms=_int(d, "time_ms"),
        maker=_bool(d, "maker", False))


def decode_income(d: Any) -> IncomeInfo:
    d = _obj(d, "收益流水")
    return IncomeInfo(income_id=_id(d, "tran_id"), income_type=_str(d, "type"), symbol=_str_opt(d, "symbol"),
                      amount=_dec(d, "amount"), asset=_str(d, "asset"), time_ms=_int(d, "time_ms"),
                      info=str(d.get("info") or ""))


def decode_leverage(d: Any) -> LeverageInfo:
    d = _obj(d, "杠杆")
    return LeverageInfo(symbol=_str(d, "symbol"), leverage=_int(d, "leverage"), margin_mode=_str(d, "margin_mode"))


def decode_ticker(d: Any) -> Ticker:
    d = _obj(d, "行情")
    return Ticker(symbol=_str(d, "symbol"), last=_dec(d, "last"), mark=_dec(d, "mark"), time_ms=_int(d, "time_ms"))


def _to_float(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise DecodeError(f"K 线的{what}应是数值：{value!r}")
    try:
        out = float(value)
    except ValueError:
        raise DecodeError(f"K 线的{what}不是数值：{value!r}") from None
    if not math.isfinite(out):
        raise DecodeError(f"K 线的{what}不是有限数值：{value!r}")
    return out


def decode_candle(row: Any) -> Candle:
    """``[开盘时间, 开, 高, 低, 收, 量, 收盘时间]``（升序，最后一根可能未收盘）；价格与成交量是 float（交给 pandas）。"""
    if not isinstance(row, (list, tuple)) or len(row) < 7:
        raise DecodeError(f"K 线应是至少 7 项的数组：{row!r}")
    return Candle(open_time_ms=_to_int(row[0], "开盘时间"), open=_to_float(row[1], "开盘价"),
                  high=_to_float(row[2], "最高价"), low=_to_float(row[3], "最低价"),
                  close=_to_float(row[4], "收盘价"), volume=_to_float(row[5], "成交量"),
                  close_time_ms=_to_int(row[6], "收盘时间"))


def decode_restrictions(d: Any) -> RestrictionInfo:
    d = _obj(d, "交易限制")
    detail = d.get("detail") or {}
    if not isinstance(detail, Mapping):
        raise DecodeError("交易限制的 detail 应是 JSON 对象")
    return RestrictionInfo(restricted=_bool(d, "restricted"),
                           detail=tuple(sorted((str(k), str(v)) for k, v in detail.items())))


def decode_order_update(d: Any) -> StreamOrder:
    """数据流的 ``order_update`` 帧（7.3）：订单、执行类型，以及有成交时的成交记录。"""
    d = _obj(d, "订单推送")
    fill = d.get("fill")
    return StreamOrder(order=decode_order(_field(d, "order")), execution=_str(d, "execution"),
                       fill=None if fill is None else decode_trade(fill), event_time_ms=_int(d, "event_time_ms"))


def decode_algo_update(d: Any) -> StreamAlgo:
    """数据流的 ``algo_update`` 帧（只在行为开关 ``push_algo_events`` 打开时才有）。"""
    d = _obj(d, "条件单推送")
    return StreamAlgo(algo=decode_algo(_field(d, "algo")), event_time_ms=_int(d, "event_time_ms"))


def decode_account_update(d: Any) -> StreamAccount:
    """数据流的 ``account_update`` 帧：变动合约的持仓（含数量为 0 的，即已平）与钱包余额。"""
    d = _obj(d, "账户推送")
    positions = tuple(decode_position(p) for p in _list(_field(d, "positions"), "持仓列表"))
    raw_balances = d.get("balances")
    balances = []
    for row in [] if raw_balances is None else _list(raw_balances, "余额列表"):
        row = _obj(row, "余额")
        balances.append((_str(row, "asset"), _dec(row, "wallet_balance")))
    return StreamAccount(positions=positions, reason=_str(d, "reason"), event_time_ms=_int(d, "event_time_ms"),
                         balances=tuple(balances))


def _decode_list(body: Any, fn: Callable[[Any], T], what: str) -> list[T]:
    return [fn(item) for item in _list(body, what)]


# ====================================================================== 适配器


@dataclass(frozen=True)
class _Reply:
    response: Response | None
    failure: str = ""  # response 为 None 时的原因（超时、连接中断……）


def _fmt(value: Decimal) -> str:
    """数值发给交易所一律用普通小数，不用科学计数法。"""
    return format(value, "f")


def _describe(status: int, code: int | None, msg: str) -> str:
    return f"交易所返回 HTTP {status}" + (f"（代码 {code}）" if code is not None else "") + (f"：{msg}" if msg else "")


class StandinAdapter(ExchangeAdapter):
    def __init__(self, transport: Transport, *, capability: WriteCapability | None, bus: EventBus, clock: Clock,
                 config_get: Callable[[str], Any], request_timeout_s: float = 10.0):
        super().__init__(capability=capability)
        self._transport = transport
        self._bus = bus
        self._clock = clock
        self._config_get = config_get
        self._timeout_s = request_timeout_s
        self._limiter = RateLimiter(clock, lambda: int(config_get("weight_soft_limit")))
        self._stream: AccountStream | None = None

    # ------------------------------------------------------------ 请求

    async def _exchange(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                        auth: bool = True) -> _Reply:
        try:
            response = await self._transport.request(method, path, params=params, body=body, auth=auth,
                                                     timeout_s=self._timeout_s)
        except TransportTimeout:
            return _Reply(None, f"请求超时（{self._timeout_s:g} 秒内没有响应）：{method} {path}")
        except TransportConnectionError as exc:
            return _Reply(None, f"连接中断：{method} {path}（{exc}）")
        except TransportError as exc:
            return _Reply(None, f"传输失败：{method} {path}（{exc}）")
        self._limiter.observe(response.headers, response.status)
        return _Reply(response)

    async def _read(self, path: str, parse: Callable[[Any], T], *, params: dict | None = None, auth: bool = True,
                    pausable: bool = False, missing_is_none: bool = False) -> ReadResult:
        if pausable:
            wait = self._limiter.pause_s(False)
            if wait > 0:
                return Unknown(f"限频暂停（非保护类读请求）：已用权重 {self._limiter.used_weight}，约 {math.ceil(wait)} 秒后恢复",
                               code=-1003)
        reply = await self._exchange("GET", path, params=params, auth=auth)
        if reply.response is None:
            return Unknown(reply.failure)
        response = reply.response
        if response.status == 200:
            try:
                return Known(parse(response.body))
            except DecodeError as exc:
                return Unknown(f"响应无法解析（GET {path}）：{exc}", http_status=200)
        code, msg = parse_error_body(response.body)
        kind = classify(response.status, code, msg)  # 401/403 抛 AuthConfigError
        if missing_is_none and response.status == 400 and code == -2013:
            return Known(None)
        if code is None and kind is RejectKind.RATE_LIMITED:
            code = -1003
        return Unknown(_describe(response.status, code, msg), code=code, http_status=response.status)

    async def _write(self, method: str, path: str, parse: Callable[[Any], WriteAccepted], *, body: dict | None = None,
                     params: dict | None = None) -> WriteResult:
        reply = await self._exchange(method, path, params=params, body=body)
        if reply.response is None:
            return WriteUnknown(reply.failure)
        response = reply.response
        if response.status == 200:
            try:
                return parse(response.body)
            except DecodeError as exc:
                return WriteUnknown(f"交易所已答复成功但响应无法解析（{method} {path}）：{exc}", http_status=200)
        code, msg = parse_error_body(response.body)
        kind = classify(response.status, code, msg)  # 401/403 抛 AuthConfigError
        if kind is None:
            return WriteUnknown(_describe(response.status, code, msg), code=code, http_status=response.status)
        if code is None and kind is RejectKind.RATE_LIMITED:
            code = -1003
        return WriteRejected(kind, code, msg or _describe(response.status, code, msg), http_status=response.status)

    async def _cancel(self, path: str, params: dict, ok: Callable[[Any], CancelResult]) -> CancelResult:
        reply = await self._exchange("DELETE", path, params=params)
        if reply.response is None:
            return CancelResult(CancelOutcome.UNKNOWN, message=reply.failure)
        response = reply.response
        if response.status == 200:
            try:
                return ok(response.body)
            except DecodeError as exc:
                return CancelResult(CancelOutcome.UNKNOWN, message=f"撤单响应无法解析（DELETE {path}）：{exc}")
        code, msg = parse_error_body(response.body)
        classify(response.status, code, msg)  # 401/403 抛 AuthConfigError
        if response.status == 400 and code == -2011:
            return CancelResult(CancelOutcome.NOT_FOUND, code=code, message=msg)
        return CancelResult(CancelOutcome.UNKNOWN, code=code, message=_describe(response.status, code, msg))

    # ------------------------------------------------------------ 读方法

    async def server_time(self) -> ReadResult[int]:
        return await self._read("/api/public/time", lambda b: _int(_obj(b), "server_time_ms"), auth=False)

    async def load_instruments(self) -> ReadResult[list[Instrument]]:
        return await self._read("/api/public/instruments", lambda b: _decode_list(b, decode_instrument, "合约清单"),
                                auth=False, pausable=True)

    async def fetch_position_mode(self) -> ReadResult[bool]:
        return await self._read("/api/private/position_mode", lambda b: _bool(_obj(b), "dual_side"))

    async def fetch_account(self) -> ReadResult[AccountInfo]:
        return await self._read("/api/private/account", decode_account)

    async def fetch_positions(self) -> ReadResult[list[PositionInfo]]:
        def parse(body: Any) -> list[PositionInfo]:
            return [p for p in _decode_list(body, decode_position, "持仓列表") if p.qty != 0]

        return await self._read("/api/private/positions", parse)

    async def fetch_open_orders(self, symbol: str | None = None) -> ReadResult[list[OrderInfo]]:
        return await self._read("/api/private/open_orders", lambda b: _decode_list(b, decode_order, "挂单列表"),
                                params={"symbol": symbol} if symbol else None)

    async def fetch_open_algo_orders(self, symbol: str | None = None) -> ReadResult[list[AlgoOrderInfo]]:
        return await self._read("/api/private/open_algo_orders", lambda b: _decode_list(b, decode_algo, "条件单列表"),
                                params={"symbol": symbol} if symbol else None)

    async def fetch_order(self, symbol: str, *, client_order_id: str | None = None,
                          exchange_order_id: str | None = None) -> ReadResult[OrderInfo | None]:
        if not client_order_id and not exchange_order_id:
            raise ValueError("按编号回查订单必须给出自定义编号或交易所单号")
        params: dict[str, Any] = {"symbol": symbol}
        if client_order_id:
            params["client_order_id"] = client_order_id
        if exchange_order_id:
            params["order_id"] = exchange_order_id
        return await self._read("/api/private/order", decode_order, params=params, missing_is_none=True)

    async def fetch_algo_order(self, symbol: str, *, client_algo_id: str | None = None,
                               algo_id: str | None = None) -> ReadResult[AlgoOrderInfo | None]:
        if not client_algo_id and not algo_id:
            raise ValueError("按编号回查条件单必须给出自定义编号或条件单号")
        params: dict[str, Any] = {"symbol": symbol}
        if client_algo_id:
            params["client_algo_id"] = client_algo_id
        if algo_id:
            params["algo_id"] = algo_id
        return await self._read("/api/private/algo_order", decode_algo, params=params, missing_is_none=True)

    async def fetch_trades(self, symbol: str, start_ms: int, end_ms: int | None = None,
                           limit: int = 500) -> ReadResult[list[TradeInfo]]:
        params = {"symbol": symbol, "start_ms": start_ms, "end_ms": end_ms, "limit": limit}
        return await self._read("/api/private/trades", lambda b: _decode_list(b, decode_trade, "成交列表"),
                                params=params, pausable=True)

    async def fetch_income(self, start_ms: int, end_ms: int | None = None, income_type: str | None = None,
                           limit: int = 1000) -> ReadResult[list[IncomeInfo]]:
        params = {"start_ms": start_ms, "end_ms": end_ms, "type": income_type, "limit": limit}
        return await self._read("/api/private/income", lambda b: _decode_list(b, decode_income, "收益流水"),
                                params=params, pausable=True)

    async def fetch_candles(self, symbol: str, interval: str, limit: int = MAX_CANDLES) -> ReadResult[list[Candle]]:
        if not isinstance(limit, int) or not 1 <= limit <= MAX_CANDLES:
            raise ValueError(f"K 线数量必须是 1 到 {MAX_CANDLES} 的整数：{limit!r}")
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        return await self._read("/api/public/klines", lambda b: _decode_list(b, decode_candle, "K 线"),
                                params=params, auth=False, pausable=True)

    async def fetch_tickers(self, symbols: list[str] | None = None) -> ReadResult[dict[str, Ticker]]:
        if symbols is not None and not symbols:
            return Known({})

        def parse(body: Any) -> dict[str, Ticker]:
            return {str(sym): decode_ticker(row) for sym, row in _obj(body, "行情表").items()}

        params = {"symbols": ",".join(symbols)} if symbols else None
        return await self._read("/api/public/tickers", parse, params=params, auth=False, pausable=True)

    async def fetch_leverage(self, symbol: str) -> ReadResult[LeverageInfo]:
        return await self._read("/api/private/leverage", decode_leverage, params={"symbol": symbol})

    async def fetch_restrictions(self) -> ReadResult[RestrictionInfo]:
        return await self._read("/api/private/trading_status", decode_restrictions, pausable=True)

    # ------------------------------------------------------------ 数据流、权重与关闭

    def account_stream(self) -> AccountStream:
        if self._stream is None:
            from dmr_kernel.exchange.stream import StandinAccountStream  # 延迟导入：stream 要用本模块的解码函数

            self._stream = StandinAccountStream(self._transport, bus=self._bus, clock=self._clock,
                                                config_get=self._config_get, request_timeout_s=self._timeout_s)
        return self._stream

    def used_weight(self) -> int | None:
        return self._limiter.used_weight

    async def close(self) -> None:
        """停掉数据流并关闭传输（适配层接管传输的生命周期）。可重复调用。"""
        if self._stream is not None:
            await self._stream.stop()
        await self._transport.close()

    # ------------------------------------------------------------ 写方法的实现（出口校验在基类）

    async def _create_market_order(self, *, symbol, side, position_side, qty, client_order_id) -> WriteResult:
        body = {"symbol": symbol, "side": side.value, "position_side": position_side.value, "type": "MARKET",
                "qty": _fmt(qty), "client_order_id": client_order_id}
        return await self._write("POST", "/api/private/order", lambda b: WriteAccepted(order=decode_order(b)),
                                 body=body)

    async def _create_stop_order(self, *, symbol, side, position_side, trigger_price, working_type, client_algo_id,
                                 close_position, qty, algo_type) -> WriteResult:
        body = {"symbol": symbol, "side": side.value, "position_side": position_side.value, "type": algo_type.value,
                "trigger_price": _fmt(trigger_price), "working_type": working_type.value,
                "close_position": close_position, "qty": None if qty is None else _fmt(qty),
                "client_algo_id": client_algo_id}
        return await self._write("POST", "/api/private/algo_order", lambda b: WriteAccepted(algo=decode_algo(b)),
                                 body=body)

    async def _cancel_order(self, *, symbol, client_order_id, exchange_order_id) -> CancelResult:
        params: dict[str, Any] = {"symbol": symbol}
        if client_order_id:
            params["client_order_id"] = client_order_id
        if exchange_order_id:
            params["order_id"] = exchange_order_id
        return await self._cancel("/api/private/order", params,
                                  lambda b: CancelResult(CancelOutcome.CANCELLED, order=decode_order(b)))

    async def _cancel_algo_order(self, *, symbol, client_algo_id, algo_id) -> CancelResult:
        params: dict[str, Any] = {"symbol": symbol}
        if client_algo_id:
            params["client_algo_id"] = client_algo_id
        if algo_id:
            params["algo_id"] = algo_id
        return await self._cancel("/api/private/algo_order", params,
                                  lambda b: CancelResult(CancelOutcome.CANCELLED, algo=decode_algo(b)))

    async def _set_leverage(self, *, symbol, leverage) -> WriteResult:
        return await self._write("POST", "/api/private/leverage",
                                 lambda b: WriteAccepted(leverage=decode_leverage(b)),
                                 body={"symbol": symbol, "leverage": leverage})

    async def _set_margin_mode(self, *, symbol, margin_mode) -> WriteResult:
        return await self._write("POST", "/api/private/margin_mode",
                                 lambda b: WriteAccepted(leverage=decode_leverage(b)),
                                 body={"symbol": symbol, "margin_mode": margin_mode})
