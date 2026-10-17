"""Binance USDⓈ-M 的公共行情与合约规则（第 1 层；ccxt 4.5.78 异步 ``binanceusdm``）。

只做公共 GET：合约规则、K 线、最新价与标记价、服务器时间。不配任何密钥；整个客户端只有一个 ccxt 实例（长连接复用）；
每个请求都有超时（既设给 ccxt 的 ``timeout``，又用 ``asyncio.wait_for`` 兜底）；失败一律返回 ``Unknown``，
绝不把「读不到」当成「没有」。

白名单：对 ccxt 对象的任何访问都经 ``client.ccxt`` 这个代理，只放行 ``ALLOWED_CCXT_METHODS`` 里的方法，
其它任何属性（下单、撤单、账户、持仓、listenKey、私有接口、``request``、``apiKey``……）一律立即抛 ``WhitelistError``。
构造时若 ccxt 对象带着任何密钥字段，直接拒绝。

合约一律用交易所原始代码（取自 ``info.symbol``，如 ``BTCUSDT``、``1000SHIBUSDT``、中文合约名），ccxt 的统一写法
（``BTC/USDT:USDT``）只在本模块内部出现。数值取自交易所原始的字符串，转 ``Decimal``，不经过 ccxt 的浮点数。
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Mapping, TypeVar

from dmr_kernel.core.clock import interval_ms
from dmr_kernel.core.types import Candle, Instrument, Known, ReadResult, Ticker, Unknown

log = logging.getLogger(__name__)

T = TypeVar("T")

MAX_CANDLES = 99  # 一次最多取多少根 K 线（权重最低的档位）

# 公共客户端允许调用的 ccxt 方法；``load_markets`` 就是合约规则（exchangeInfo），``fetch_mark_prices`` 是标记价（premiumIndex）
ALLOWED_CCXT_METHODS = frozenset({"load_markets", "fetch_ohlcv", "fetch_tickers", "fetch_mark_prices", "fetch_time",
                                  "close"})

_CREDENTIAL_ATTRS = ("apiKey", "secret", "password", "uid", "login", "privateKey", "walletAddress", "token")
_CODE_IN_TEXT = re.compile(r'"code"\s*:\s*(-?\d+)')


class WhitelistError(RuntimeError):
    """访问了白名单之外的 ccxt 属性。属于程序错误：公共客户端绝不能碰下单、账户、私有接口。"""


class _Unparsable(ValueError):
    """交易所的响应缺字段或取值不合法。"""


class _Failed(Exception):
    """一次 ccxt 调用失败（超时、网络错误、交易所报错、客户端已关闭）；带着要返回的 ``Unknown``。"""

    def __init__(self, unknown: Unknown):
        super().__init__(unknown.reason)
        self.unknown = unknown


class _WhitelistedExchange:
    """ccxt 对象的白名单代理：只读，只放行 ``ALLOWED_CCXT_METHODS``。"""

    __slots__ = ("__exchange",)

    def __init__(self, exchange: Any):
        object.__setattr__(self, "_WhitelistedExchange__exchange", exchange)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)  # 让 hasattr、copy 等 Python 协议照常工作
        if name not in ALLOWED_CCXT_METHODS:
            raise WhitelistError(f"ccxt 的 {name!r} 不在公共客户端的白名单内（只允许：{', '.join(sorted(ALLOWED_CCXT_METHODS))}）")
        return getattr(self.__exchange, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise WhitelistError(f"不允许修改 ccxt 对象的 {name!r}")

    def __delattr__(self, name: str) -> None:
        raise WhitelistError(f"不允许删除 ccxt 对象的 {name!r}")


def build_default_exchange(timeout_s: float = 10.0) -> Any:
    """真实的 ccxt 异步 ``binanceusdm``：不配密钥、开内置限速、请求超时、不取币种表（那是需要密钥的私有接口）。"""
    import ccxt.async_support as ccxt_async  # 延迟导入：只有真正要建 ccxt 对象时才加载

    return ccxt_async.binanceusdm({
        "enableRateLimit": True,
        "timeout": round(timeout_s * 1000),
        "options": {"defaultType": "swap", "defaultSubType": "linear", "fetchCurrencies": False},
    })


# ====================================================================== 解析（交易所原始字符串 -> core.types）


def _decimal(d: Mapping[str, Any], key: str) -> Decimal:
    value = d.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _Unparsable(f"字段 {key} 应是数值：{value!r}")
    try:
        out = Decimal(repr(value) if isinstance(value, float) else str(value).strip())
    except InvalidOperation:
        raise _Unparsable(f"字段 {key} 不是数值：{value!r}") from None
    if not out.is_finite():
        raise _Unparsable(f"字段 {key} 不是有限数值：{value!r}")
    return out


def _positive(d: Mapping[str, Any], key: str) -> Decimal:
    out = _decimal(d, key)
    if out <= 0:
        raise _Unparsable(f"字段 {key} 必须大于 0：{out}")
    return out


def _decimal_opt(d: Mapping[str, Any], key: str) -> Decimal | None:
    return None if d.get(key) is None else _decimal(d, key)


def _int_opt(d: Mapping[str, Any], key: str) -> int | None:
    value = d.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _Unparsable(f"字段 {key} 应是整数：{value!r}")
    try:
        return int(str(value).strip())
    except ValueError:
        raise _Unparsable(f"字段 {key} 不是整数：{value!r}") from None


def _text(d: Mapping[str, Any], key: str) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value:
        raise _Unparsable(f"缺少字段 {key}")
    return value


def _instrument(market: Any) -> Instrument:
    if not isinstance(market, Mapping):
        raise _Unparsable("条目不是对象")
    info = market.get("info")
    if not isinstance(info, Mapping):
        raise _Unparsable("缺少交易所原始信息 info")
    symbol = _text(info, "symbol")
    raw_filters = info.get("filters")
    if not isinstance(raw_filters, list):
        raise _Unparsable(f"{symbol} 缺少 filters")
    filters = {f["filterType"]: f for f in raw_filters if isinstance(f, Mapping) and isinstance(f.get("filterType"), str)}
    price, lot, notional = filters.get("PRICE_FILTER"), filters.get("LOT_SIZE"), filters.get("MIN_NOTIONAL")
    if price is None or lot is None or notional is None:
        raise _Unparsable(f"{symbol} 缺少 PRICE_FILTER、LOT_SIZE 或 MIN_NOTIONAL")
    market_lot = filters.get("MARKET_LOT_SIZE") or {}
    return Instrument(
        symbol=symbol, base=_text(info, "baseAsset"), quote=_text(info, "quoteAsset"),
        contract_type=_text(info, "contractType"), status=_text(info, "status"),
        tick_size=_positive(price, "tickSize"), step_size=_positive(lot, "stepSize"), min_qty=_decimal(lot, "minQty"),
        min_notional=_decimal(notional, "notional"), market_step_size=_decimal_opt(market_lot, "stepSize"),
        market_max_qty=_decimal_opt(market_lot, "maxQty"), onboard_ms=_int_opt(info, "onboardDate"))


def _parse_instruments(markets: Any) -> list[Instrument]:
    if isinstance(markets, Mapping):
        rows = list(markets.values())
    elif isinstance(markets, list):
        rows = markets
    else:
        raise _Unparsable(f"合约清单应是对象或数组：{type(markets).__name__}")
    out: list[Instrument] = []
    for row in rows:
        try:
            out.append(_instrument(row))
        except _Unparsable as exc:
            ident = row.get("id") if isinstance(row, Mapping) else None
            log.warning("跳过无法解析的合约规则 %s：%s", ident, exc)
    if not out:
        raise _Unparsable("合约清单为空，或全部无法解析")
    return out


def _float(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _Unparsable(f"K 线的{what}应是数值：{value!r}")
    try:
        out = float(value)
    except ValueError:
        raise _Unparsable(f"K 线的{what}不是数值：{value!r}") from None
    if not math.isfinite(out):
        raise _Unparsable(f"K 线的{what}不是有限数值：{value!r}")
    return out


def _parse_candles(rows: Any, step_ms: int) -> list[Candle]:
    """ccxt 的 ``[开盘时间, 开, 高, 低, 收, 量]``（升序，最后一根可能未收盘）；收盘时间 = 开盘时间 + 周期 - 1。"""
    if not isinstance(rows, list):
        raise _Unparsable(f"K 线应是数组：{type(rows).__name__}")
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise _Unparsable(f"K 线应是至少 6 项的数组：{row!r}")
        opened = row[0]
        if isinstance(opened, bool) or not isinstance(opened, int):
            raise _Unparsable(f"K 线的开盘时间应是整数毫秒：{opened!r}")
        out.append(Candle(open_time_ms=opened, open=_float(row[1], "开盘价"), high=_float(row[2], "最高价"),
                          low=_float(row[3], "最低价"), close=_float(row[4], "收盘价"), volume=_float(row[5], "成交量"),
                          close_time_ms=opened + step_ms - 1))
    return out


def _infos_by_code(rows: Any, what: str) -> dict[str, Mapping[str, Any]]:
    """ccxt 的行情表（统一代码 -> 行情）-> {交易所原始代码: 原始 info}。"""
    if not isinstance(rows, Mapping):
        raise _Unparsable(f"{what}应是对象：{type(rows).__name__}")
    out: dict[str, Mapping[str, Any]] = {}
    for row in rows.values():
        info = row.get("info") if isinstance(row, Mapping) else None
        code = info.get("symbol") if isinstance(info, Mapping) else None
        if isinstance(code, str) and code:
            out[code] = info
    return out


def _parse_tickers(last_rows: Any, mark_rows: Any) -> dict[str, Ticker]:
    lasts, marks = _infos_by_code(last_rows, "最新价表"), _infos_by_code(mark_rows, "标记价表")
    out: dict[str, Ticker] = {}
    for code in lasts.keys() & marks.keys():
        last_info, mark_info = lasts[code], marks[code]
        try:
            time_ms = _int_opt(last_info, "closeTime") or _int_opt(mark_info, "time")
            if not time_ms:
                raise _Unparsable("缺少行情时间")
            out[code] = Ticker(symbol=code, last=_positive(last_info, "lastPrice"), mark=_positive(mark_info, "markPrice"),
                               time_ms=time_ms)
        except _Unparsable as exc:
            log.warning("跳过无法解析的行情 %s：%s", code, exc)
    if not out:
        raise _Unparsable("没有可用的行情")
    return {code: out[code] for code in sorted(out)}


def _unknown_from(what: str, exc: BaseException) -> Unknown:
    """ccxt 抛出的异常 -> ``Unknown``（带交易所错误码；限频带状态码）。按类名判断，不需要导入 ccxt。"""
    names = {cls.__name__ for cls in type(exc).__mro__}
    text = " ".join(str(exc).split())[:300]
    match = _CODE_IN_TEXT.search(text)
    code = int(match.group(1)) if match else None
    http_status = 429 if "RateLimitExceeded" in names else 418 if "DDoSProtection" in names else None
    if code is None and http_status is not None:
        code = -1003
    kind = "超时" if "RequestTimeout" in names or isinstance(exc, TimeoutError) else "失败"
    return Unknown(f"{what}{kind}：{type(exc).__name__}：{text}", code=code, http_status=http_status)


# ====================================================================== 客户端


def _has_value(value: Any) -> bool:
    return isinstance(value, (str, bytes)) and len(value) > 0


class BinancePublicClient:
    def __init__(self, *, exchange: Any = None, timeout_s: float = 10.0):
        if not timeout_s > 0:
            raise ValueError("timeout_s 必须大于 0")
        raw = exchange if exchange is not None else build_default_exchange(timeout_s)
        carried = [attr for attr in _CREDENTIAL_ATTRS if _has_value(getattr(raw, attr, None))]
        if carried:
            raise ValueError(f"公共行情客户端不得配置密钥，ccxt 对象带着：{', '.join(carried)}")
        self._ccxt = _WhitelistedExchange(raw)
        self._timeout_s = float(timeout_s)
        self._closed = False

    @property
    def ccxt(self) -> _WhitelistedExchange:
        """ccxt 对象的白名单代理（对 ccxt 的一切访问都从这里走）。"""
        return self._ccxt

    # ------------------------------------------------------------ 公共读方法

    async def load_instruments(self) -> ReadResult[list[Instrument]]:
        """全部 USDⓈ-M 合约（不按类型和状态过滤，过滤交给 ``InstrumentRules``）。每次都向交易所重新取。"""
        return await self._read("合约规则", lambda: self._fetch("合约规则", lambda: self._ccxt.load_markets(True)),
                                _parse_instruments)

    async def fetch_candles(self, symbol: str, interval: str, limit: int = MAX_CANDLES) -> ReadResult[list[Candle]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CANDLES:
            raise ValueError(f"K 线数量必须是 1 到 {MAX_CANDLES} 的整数：{limit!r}")
        if not isinstance(interval, str):
            raise ValueError(f"无法识别的周期：{interval!r}")
        step_ms = interval_ms(interval)  # 无法识别抛 ValueError
        return await self._read(
            "K 线", lambda: self._fetch("K 线", lambda: self._ccxt.fetch_ohlcv(symbol, interval, limit=limit)),
            lambda rows: _parse_candles(rows, step_ms))

    async def fetch_tickers(self, symbols: list[str] | None = None) -> ReadResult[dict[str, Ticker]]:
        """最新价（24 小时行情）与标记价（premiumIndex）两张表取交集；``symbols`` 为 None 取全部。"""
        if symbols is not None and not symbols:
            return Known({})
        wanted = None if symbols is None else list(symbols)

        async def both() -> tuple[Any, Any]:
            last = await self._fetch("最新价", lambda: self._ccxt.fetch_tickers(wanted))
            mark = await self._fetch("标记价", lambda: self._ccxt.fetch_mark_prices(wanted))
            return last, mark

        return await self._read("行情", both, lambda pair: _parse_tickers(*pair))

    async def server_time(self) -> ReadResult[int]:
        def parse(value: Any) -> int:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise _Unparsable(f"服务器时间应是正整数毫秒：{value!r}")
            return value

        return await self._read("服务器时间", lambda: self._fetch("服务器时间", lambda: self._ccxt.fetch_time()), parse)

    async def close(self) -> None:
        """释放唯一的 ccxt 实例；可重复调用。关闭之后的读调用返回 ``Unknown``，不会悄悄重开。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self._ccxt.close()
        except Exception as exc:  # 关闭时的错误只记录
            log.debug("关闭 ccxt 对象时出错：%s", exc)

    # ------------------------------------------------------------ 内部

    async def _fetch(self, what: str, make: Callable[[], Awaitable[T]]) -> T:
        """一次 ccxt 调用：带超时；失败抛 ``_Failed``。白名单错误与取消照常向上抛。"""
        if self._closed:
            raise _Failed(Unknown(f"{what}失败：公共行情客户端已关闭"))
        try:
            return await asyncio.wait_for(make(), self._timeout_s)
        except (asyncio.CancelledError, WhitelistError):
            raise
        except TimeoutError:
            raise _Failed(Unknown(f"{what}超时（{self._timeout_s:g} 秒内没有响应）")) from None
        except Exception as exc:
            raise _Failed(_unknown_from(what, exc)) from None

    async def _read(self, what: str, produce: Callable[[], Awaitable[Any]], parse: Callable[[Any], T]) -> ReadResult[T]:
        try:
            return Known(parse(await produce()))
        except _Failed as failed:
            return failed.unknown
        except _Unparsable as exc:
            return Unknown(f"{what}的响应无法解析：{exc}")
