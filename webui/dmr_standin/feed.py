"""替身交易所的行情源（docs/设计约定.md 7.1；计划 T0.5「行情源」）。

三种行情源实现同一组方法，``engine`` 与 ``dispatcher`` 只用这些：

- ``source``：``"binance"`` | ``"replay"`` | ``"static"``
- ``await start()`` / ``await stop()``：启动（Binance：取合约清单）/ 关停（Binance：关闭长连接）
- ``instruments()`` / ``instrument(symbol)`` / ``symbols()``：合约规则（``InstrumentSpec``）
- ``quote(symbol) -> Quote | None``：最新价与标记价（同步：取缓存或按时间计算）；没有行情时为 None
- ``funding_rate(symbol) -> Decimal | None``：资金费率
- ``await klines(symbol, interval, limit)``：协议行 ``[open_ms, "o", "h", "l", "c", "v", close_ms]``，升序，
  最后一根可能未收盘；未知合约 ``FeedError(-1121)``，不支持的周期 ``FeedError(-1120)``
- ``await ensure_quotes(symbols)``：缺价的合约当场取一次（只有 Binance 源真的去取）
- ``set_interest(symbols)``：「关心的合约」（分发器每次 tick 设置：持仓、挂单、条件单、/dev/subscribe 订阅的）
- ``await poll_once(marks)``：轮询一次关心的合约的最新价；``marks=True`` 时连同标记价与资金费率
- ``status()``：``{"source", "healthy", "last_ok_ms", "errors": [{"time_ms", "msg"}]}``

时钟一律是返回 UTC 毫秒的可调用对象；价格一律是 ``Decimal``。

``BinancePublicFeed`` 只经 ccxt 4.5.78 async ``binanceusdm`` 的五个公共 GET（``ALLOWED_PUBLIC_METHODS``，
名字已对照 ``ccxt/abstract/binanceusdm.py`` 核实），不配置任何密钥；单实例长连接复用；每个请求既有 ccxt 的
``timeout``（毫秒）又有 ``asyncio.wait_for``；失败按指数退避重试。

``ReplayFeed`` 只读 ``<DMR_WebUI>/bit/data/bit_short_term/*.csv``（15m）与 ``bit_long_term/*.csv``（1h）：

- 去掉 CSV 的末行（未收盘），其余 N 行按「正放到头再倒放」往返循环：虚拟序号 r = k mod 2N，r < N 取第 r 行；
  否则取第 2N−1−r 行并把开收互换（高低不变）。这样首尾相接处价格连续，长时间运行也不漂移。
- 启动时刻 S：最后一根已收盘的行（第 N−1 行）对齐到 floor(S, 周期) − 周期 那根，之后按真实时间推进。
- 当根 K 线内按时间在开、高、低、收之间线性插值：阳线 开→低→高→收，阴线 开→高→低→收，各占三分之一。
  1m/3m/5m 由 15m 序列的这条路径切分；30m 由 15m 聚合；2h 到 1d 由 1h 聚合；最新价取 15m 路径。
- 合约规则用 ``data/instruments_sample.json``（``tools/make_sample.py`` 一次性生成）。样例里的其它合约：按 crc32
  选一条基础序列并错开相位，按参考价缩放，使启动时刻的价格等于 ``ref_price``；价格按该合约的价格步长四舍五入。
- 资金费率固定 0.01%。
"""
from __future__ import annotations

import asyncio
import csv
import json
import zlib
from collections import deque
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

Clock = Callable[[], int]
Sleep = Callable[[float], Awaitable[Any]]

DATA_DIR = Path(__file__).resolve().parent / "data"
SAMPLE_PATH = DATA_DIR / "instruments_sample.json"

MAX_KLINES = 99
INTERVAL_MS: dict[str, int] = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
    "12h": 43_200_000, "1d": 86_400_000,
}
BINANCE_INTERVALS = frozenset(INTERVAL_MS) | {"3d", "1w", "1M"}
REPLAY_FUNDING_RATE = Decimal("0.0001")
DEFAULT_FUNDING_RATE = Decimal("0.0001")

# ccxt 4.5.78 binanceusdm 的隐式公共方法（ccxt/abstract/binanceusdm.py）：都是 fapiPublic 的 GET
ALLOWED_PUBLIC_METHODS = frozenset({
    "fapiPublicGetExchangeInfo",  # GET /fapi/v1/exchangeInfo 合约清单
    "fapiPublicGetTime",  # GET /fapi/v1/time
    "fapiPublicGetKlines",  # GET /fapi/v1/klines（limit ≤ 99 时权重 1）
    "fapiPublicGetTickerPrice",  # GET /fapi/v1/ticker/price 最新价
    "fapiPublicGetPremiumIndex",  # GET /fapi/v1/premiumIndex 标记价与资金费率
})

_ERRORS_KEPT = 20
_PER_SYMBOL_MAX = 3  # 关心的合约不超过这么多时逐个取（权重更低），否则一次取全部再过滤
_KLINE_WATCH_MS = 10 * 60_000  # 最近这么久内被请求过 K 线的合约进入价格轮询


class FeedError(Exception):
    """行情源的错误：``code`` 是 Binance 风格的错误码，``http_status`` 是分发器应返回的状态码。"""

    def __init__(self, code: int, msg: str, http_status: int = 400):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status


# ---------------------------------------------------------------- 数值工具


def fmt(value: Decimal | int | None) -> str | None:
    """协议里的数值字符串：去掉多余的零，不用科学计数法（``Decimal("1E+2")`` -> ``"100"``）。"""
    if value is None:
        return None
    d = Decimal(value)
    if d == 0:
        return "0"
    return format(d.normalize(), "f")


def to_decimal(value: Any) -> Decimal:
    """字符串或数转成有限的 Decimal；不接受布尔、空值与 NaN/Infinity（抛 ValueError）。"""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"不是数值：{value!r}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (int, float, str)):
        try:
            d = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValueError(f"不是数值：{value!r}") from None
    else:
        raise ValueError(f"不是数值：{value!r}")
    if not d.is_finite():
        raise ValueError(f"不是有限的数值：{value!r}")
    return d


def round_step(value: Decimal, step: Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
    """按步长取整（默认四舍五入）：结果是 step 的整数倍。"""
    return (value / step).quantize(Decimal(1), rounding=rounding) * step


def is_multiple(value: Decimal, step: Decimal) -> bool:
    return value % step == 0


# ---------------------------------------------------------------- 数据类


PROTOCOL_INSTRUMENT_FIELDS = ("symbol", "base", "quote", "contract_type", "status", "tick_size", "step_size",
                              "min_qty", "min_notional", "market_step_size", "market_max_qty", "onboard_ms")
_DECIMAL_FIELDS = ("tick_size", "step_size", "min_qty", "min_notional", "market_step_size", "market_max_qty",
                   "ref_price")


@dataclass(frozen=True)
class InstrumentSpec:
    """合约规则：字段同 ``dmr_kernel.core.types.Instrument``，另加回放用的参考价 ``ref_price``。"""

    symbol: str
    base: str
    quote: str
    contract_type: str
    status: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    market_step_size: Decimal | None = None
    market_max_qty: Decimal | None = None
    onboard_ms: int | None = None
    ref_price: Decimal | None = None

    @property
    def market_step(self) -> Decimal:
        """市价单的数量步长（有 MARKET_LOT_SIZE 时用它）。"""
        return self.market_step_size if self.market_step_size is not None else self.step_size

    def to_json(self) -> dict[str, Any]:
        """``/api/public/instruments`` 的一条：数值是字符串，``onboard_ms`` 是整数或空。"""
        out: dict[str, Any] = {}
        for name in PROTOCOL_INSTRUMENT_FIELDS:
            value = getattr(self, name)
            out[name] = fmt(value) if name in _DECIMAL_FIELDS else value
        return out

    def to_sample_json(self) -> dict[str, Any]:
        return {**self.to_json(), "ref_price": fmt(self.ref_price)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> InstrumentSpec:
        kwargs: dict[str, Any] = {}
        for name in (*PROTOCOL_INSTRUMENT_FIELDS, "ref_price"):
            value = data.get(name)
            if name in _DECIMAL_FIELDS:
                kwargs[name] = None if value is None else to_decimal(value)
            elif name == "onboard_ms":
                kwargs[name] = None if value is None else int(value)
            else:
                if value is None:
                    raise ValueError(f"合约规则缺少字段 {name}")
                kwargs[name] = str(value)
        spec = cls(**kwargs)
        if spec.tick_size <= 0 or spec.step_size <= 0:
            raise ValueError(f"{spec.symbol} 的价格步长与数量步长必须为正")
        return spec


@dataclass(frozen=True)
class Quote:
    last: Decimal
    mark: Decimal
    time_ms: int


def load_sample(path: Path | str = SAMPLE_PATH) -> list[InstrumentSpec]:
    """读内置的合约规则样例（``data/instruments_sample.json``）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data["instruments"] if isinstance(data, Mapping) else data
    specs = [InstrumentSpec.from_json(r) for r in rows]
    if not specs:
        raise ValueError(f"合约规则样例为空：{path}")
    return specs


def parse_exchange_info(info: Mapping[str, Any]) -> dict[str, InstrumentSpec]:
    """把 Binance ``/fapi/v1/exchangeInfo`` 的 ``symbols`` 解析成合约规则；规则不全的合约跳过。

    合约名照原样（含中文合约名）；没有 MARKET_LOT_SIZE 的合约 ``market_step_size`` 为空。
    """
    out: dict[str, InstrumentSpec] = {}
    for s in info.get("symbols", []) or []:
        try:
            filters = {f.get("filterType"): f for f in s.get("filters", []) or []}
            pf, lot = filters["PRICE_FILTER"], filters["LOT_SIZE"]
            mlot = filters.get("MARKET_LOT_SIZE") or {}
            mn = filters.get("MIN_NOTIONAL") or {}
            spec = InstrumentSpec(
                symbol=str(s["symbol"]), base=str(s.get("baseAsset", "")), quote=str(s.get("quoteAsset", "")),
                contract_type=str(s.get("contractType", "")), status=str(s.get("status", "")),
                tick_size=to_decimal(pf["tickSize"]), step_size=to_decimal(lot["stepSize"]),
                min_qty=to_decimal(lot.get("minQty", "0")), min_notional=to_decimal(mn.get("notional", "0")),
                market_step_size=to_decimal(mlot["stepSize"]) if mlot.get("stepSize") else None,
                market_max_qty=to_decimal(mlot["maxQty"]) if mlot.get("maxQty") else None,
                onboard_ms=int(s["onboardDate"]) if s.get("onboardDate") is not None else None,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if spec.tick_size > 0 and spec.step_size > 0:
            out[spec.symbol] = spec
    return out


def _describe(exc: BaseException | None) -> str:
    if exc is None:
        return "未知错误"
    if isinstance(exc, TimeoutError):
        return "请求超时"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__


# ---------------------------------------------------------------- 公共部分


class _FeedBase:
    source = "?"

    def __init__(self, clock: Clock):
        self._clock = clock
        self._instruments: dict[str, InstrumentSpec] = {}
        self._errors: deque[dict[str, Any]] = deque(maxlen=_ERRORS_KEPT)
        self.interest: set[str] = set()

    # 合约规则
    def instruments(self) -> list[InstrumentSpec]:
        return list(self._instruments.values())

    def instrument(self, symbol: str) -> InstrumentSpec | None:
        return self._instruments.get(symbol)

    def symbols(self) -> list[str]:
        return list(self._instruments)

    # 轮询与生命周期（默认什么也不做）
    def set_interest(self, symbols: Iterable[str]) -> None:
        self.interest = set(symbols)

    async def ensure_quotes(self, symbols: Iterable[str]) -> None:
        return None

    async def poll_once(self, marks: bool = False) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    # 状态
    def note_error(self, msg: str) -> None:
        self._errors.append({"time_ms": self._clock(), "msg": msg})

    def status(self) -> dict[str, Any]:
        return {"source": self.source, "healthy": True, "last_ok_ms": self._clock(), "errors": list(self._errors)}

    def _check(self, symbol: str, interval: str, allowed: Iterable[str], limit: int) -> None:
        if symbol not in self._instruments:
            raise FeedError(-1121, f"未知的合约：{symbol}")
        if interval not in allowed:
            raise FeedError(-1120, f"不支持的 K 线周期：{interval}")
        if type(limit) is not int or not 1 <= limit <= MAX_KLINES:
            raise FeedError(-1102, f"limit 必须是 1 到 {MAX_KLINES} 的整数")


# ---------------------------------------------------------------- StaticFeed（测试用）


class StaticFeed(_FeedBase):
    """测试用的行情源：价格、K 线、资金费率都由测试设定；不连任何网络。

    ``set_price(symbol, last, mark=None)``（mark 缺省等于 last）、``set_candles(symbol, tf, candles)``、
    ``set_funding_rate(symbol, rate)``（缺省 0.01%）。``interest`` 是分发器最近一次设置的关心的合约。
    """

    source = "static"

    def __init__(self, instruments: Iterable[InstrumentSpec], *, clock: Clock,
                 funding_rate: Decimal | str = DEFAULT_FUNDING_RATE):
        super().__init__(clock)
        self._instruments = {i.symbol: i for i in instruments}
        self._quotes: dict[str, Quote] = {}
        self._candles: dict[tuple[str, str], list[list[Any]]] = {}
        self._funding: dict[str, Decimal] = {}
        self._default_funding = to_decimal(funding_rate)

    def set_price(self, symbol: str, last: Any, mark: Any = None) -> None:
        last_d = to_decimal(last)
        mark_d = last_d if mark is None else to_decimal(mark)
        self._quotes[symbol] = Quote(last_d, mark_d, self._clock())

    def clear_price(self, symbol: str) -> None:
        self._quotes.pop(symbol, None)

    def set_candles(self, symbol: str, interval: str, candles: Iterable[Iterable[Any]]) -> None:
        self._candles[(symbol, interval)] = [list(c) for c in candles]

    def set_funding_rate(self, symbol: str, rate: Any) -> None:
        self._funding[symbol] = to_decimal(rate)

    def quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    def funding_rate(self, symbol: str) -> Decimal | None:
        if symbol not in self._instruments:
            return None
        return self._funding.get(symbol, self._default_funding)

    async def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        self._check(symbol, interval, BINANCE_INTERVALS, limit)
        rows = self._candles.get((symbol, interval), [])
        return [list(r) for r in rows[-limit:]]


# ---------------------------------------------------------------- ReplayFeed（回放 DMR 的 CSV）


_CSV_FIELDS = ("open", "high", "low", "close", "volume")


def _read_csv(path: Path) -> list[tuple[Decimal, ...]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = [tuple(Decimal(r[k]) for k in _CSV_FIELDS) for r in csv.DictReader(fh)]
    rows = rows[:-1]  # 末行未收盘
    if len(rows) < 2:
        raise ValueError(f"回放 CSV 行数太少：{path}")
    for row in rows:
        if any(x <= 0 for x in row[:4]) or not row[2] <= min(row[0], row[3]) <= max(row[0], row[3]) <= row[1]:
            raise ValueError(f"回放 CSV 有不合法的 K 线：{path}：{row}")
    return rows


class _Series:
    """一条基础序列（某个 CSV）：往返循环、对齐到启动时刻、当根内的价格路径。"""

    def __init__(self, rows: list[tuple[Decimal, ...]], period_ms: int, start_ms: int):
        self.rows = rows
        self.n = len(rows)
        self.period = period_ms
        # 第 N−1 根（最后一根已收盘的行）对齐到 floor(S, 周期) − 周期
        self.origin = (start_ms // period_ms) * period_ms - self.n * period_ms

    def bar(self, k: int) -> tuple[Decimal, ...]:
        r = k % (2 * self.n)
        if r < self.n:
            return self.rows[r]
        o, h, l, c, v = self.rows[2 * self.n - 1 - r]
        return (c, h, l, o, v)

    def k_at(self, t: int) -> int:
        return (t - self.origin) // self.period

    def open_ms(self, k: int) -> int:
        return self.origin + k * self.period

    def vertices(self, k: int) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        o, h, l, c, _ = self.bar(k)
        return (o, l, h, c) if c >= o else (o, h, l, c)

    def price_at(self, k: int, e: int) -> Decimal:
        """第 k 根开盘后 e 毫秒的价格（0 ≤ e ≤ 周期），按三段折线插值。"""
        vs = self.vertices(k)
        if e <= 0:
            return vs[0]
        if e >= self.period:
            return vs[3]
        seg = (3 * e) // self.period
        rem = 3 * e - seg * self.period
        a, b = vs[seg], vs[seg + 1]
        if rem == 0:
            return a
        return a + (b - a) * rem / self.period

    def path_range(self, k: int, e0: int, e1: int) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        """第 k 根内 [e0, e1] 这段路径的开、高、低、收与按时长分摊的成交量。"""
        vs = self.vertices(k)
        p0, p1 = self.price_at(k, e0), self.price_at(k, e1)
        pts = [p0, p1]
        for i in (1, 2):
            if 3 * e0 < i * self.period < 3 * e1:
                pts.append(vs[i])
        volume = self.bar(k)[4] * (e1 - e0) / self.period
        return p0, max(pts), min(pts), p1, volume


class _View:
    """某个合约在某条基础序列上的视图：相位（根数）与缩放（None 表示原样）。"""

    def __init__(self, series: _Series, phase: int, scale: Decimal | None, spec: InstrumentSpec):
        self.series = series
        self.phase = phase
        self.shift = phase * series.period
        self.scale = scale
        self.tick = spec.tick_size
        self.step = spec.step_size

    def raw_price(self, t: int) -> Decimal:
        tt = t + self.shift
        k = self.series.k_at(tt)
        return self.series.price_at(k, tt - self.series.open_ms(k))

    def price(self, t: int) -> Decimal:
        return self._px(self.raw_price(t))

    def _px(self, raw: Decimal) -> Decimal:
        return round_step(raw if self.scale is None else raw * self.scale, self.tick)

    def _vol(self, raw: Decimal) -> Decimal:
        return round_step(raw if self.scale is None else raw / self.scale, self.step)

    def ohlcv(self, start: int, end: int, now: int) -> tuple[Decimal, ...]:
        """真实时间 [start, end) 这根 K 线（截到 now 为止）：跨多根基础 K 线时聚合，落在一根之内时切分。"""
        period = self.series.period
        pieces: list[tuple[Decimal, ...]] = []
        t = start
        while t < end and t <= now:
            tt = t + self.shift
            k = self.series.k_at(tt)
            bar_open = self.series.open_ms(k)
            piece_end = min(end, bar_open - self.shift + period)
            e0 = tt - bar_open
            e1 = e0 + (piece_end - t) if piece_end <= now else now + self.shift - bar_open
            pieces.append(self.series.path_range(k, e0, e1))
            if piece_end > now:
                break
            t = piece_end
        o, c = pieces[0][0], pieces[-1][3]
        h = max(p[1] for p in pieces)
        l = min(p[2] for p in pieces)
        v = sum((p[4] for p in pieces), Decimal(0))
        return self._px(o), self._px(h), self._px(l), self._px(c), self._vol(v)


class ReplayFeed(_FeedBase):
    """回放 ``data_dir`` 下的 DMR CSV（只读）；合约规则缺省用内置样例。规则见模块文档。"""

    source = "replay"
    TIMEFRAMES = (("15m", "bit_short_term", "short_term"), ("1h", "bit_long_term", "long_term"))

    def __init__(self, *, clock: Clock, data_dir: Path | str, instruments: Iterable[InstrumentSpec] | None = None):
        super().__init__(clock)
        self.data_dir = Path(data_dir)
        specs = list(load_sample() if instruments is None else instruments)
        self._instruments = {i.symbol: i for i in specs}
        start_ms = clock()
        self.start_ms = start_ms
        series: dict[str, dict[str, _Series]] = {}
        short_dir = self.data_dir / "bit_short_term"
        for path in sorted(short_dir.glob("*_short_term_data.csv")):
            parts = path.name[: -len("_short_term_data.csv")].split("_")
            if len(parts) != 2:
                continue
            base, quote = parts
            symbol = base + quote
            per_tf: dict[str, _Series] = {}
            for tf, folder, suffix in self.TIMEFRAMES:
                csv_path = self.data_dir / folder / f"{base}_{quote}_{suffix}_data.csv"
                if not csv_path.exists():
                    raise ValueError(f"回放目录缺少 {csv_path}")
                per_tf[tf] = _Series(_read_csv(csv_path), INTERVAL_MS[tf], start_ms)
            series[symbol] = per_tf
        if not series:
            raise ValueError(f"回放目录里没有 CSV：{short_dir}")
        self.base_symbols = sorted(series)
        self._views: dict[str, dict[str, _View]] = {}
        self._info: dict[str, dict[str, Any]] = {}
        for spec in specs:
            self._build_view(spec, series, start_ms)

    def _build_view(self, spec: InstrumentSpec, series: dict[str, dict[str, _Series]], start_ms: int) -> None:
        views: dict[str, _View] = {}
        info: dict[str, Any] = {"scale": {}, "phase_bars": {}}
        if spec.symbol in series:
            info["base"] = spec.symbol
            for tf, s in series[spec.symbol].items():
                views[tf] = _View(s, 0, None, spec)
                info["scale"][tf], info["phase_bars"][tf] = "1", 0
        else:
            if spec.ref_price is None or spec.ref_price <= 0:
                raise ValueError(f"回放需要 {spec.symbol} 的参考价 ref_price")
            crc = zlib.crc32(spec.symbol.encode("utf-8"))
            base = self.base_symbols[crc % len(self.base_symbols)]
            info["base"] = base
            for tf, s in series[base].items():
                phase = (crc // len(self.base_symbols)) % (2 * s.n)
                probe = _View(s, phase, None, spec)
                scale = spec.ref_price / probe.raw_price(start_ms)
                views[tf] = _View(s, phase, scale, spec)
                info["scale"][tf], info["phase_bars"][tf] = str(scale), phase
        self._views[spec.symbol] = views
        self._info[spec.symbol] = info

    def describe(self, symbol: str) -> dict[str, Any]:
        """回放怎样生成这个合约：基础序列、各周期的缩放（字符串）与相位（根数）。"""
        info = self._info[symbol]
        return {"base": info["base"], "scale": dict(info["scale"]), "phase_bars": dict(info["phase_bars"])}

    def quote(self, symbol: str) -> Quote | None:
        views = self._views.get(symbol)
        if views is None:
            return None
        now = self._clock()
        px = views["15m"].price(now)
        return Quote(px, px, now)

    def funding_rate(self, symbol: str) -> Decimal | None:
        return REPLAY_FUNDING_RATE if symbol in self._instruments else None

    async def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        self._check(symbol, interval, INTERVAL_MS, limit)
        tf = INTERVAL_MS[interval]
        view = self._views[symbol]["15m" if tf < INTERVAL_MS["1h"] else "1h"]
        now = self._clock()
        last_open = (now // tf) * tf
        rows: list[list[Any]] = []
        for j in range(limit):
            start = last_open - (limit - 1 - j) * tf
            o, h, l, c, v = view.ohlcv(start, start + tf, now)
            rows.append([start, fmt(o), fmt(h), fmt(l), fmt(c), fmt(v), start + tf - 1])
        return rows


# ---------------------------------------------------------------- BinancePublicFeed（公共接口，无密钥）


def make_exchange(*, timeout_s: float = 10.0) -> Any:
    """ccxt 4.5.78 的 async ``binanceusdm``：不配置任何密钥；只取 linear 合约清单；关闭 fetchCurrencies（私有）。"""
    import ccxt.async_support as ccxt_async  # 延迟导入：ccxt 很重，回放与测试用不到

    return ccxt_async.binanceusdm({
        "timeout": int(round(timeout_s * 1000)),
        "enableRateLimit": True,
        "options": {"fetchMarkets": {"types": ["linear"]}, "fetchCurrencies": False},
    })


class BinancePublicFeed(_FeedBase):
    """Binance U 本位合约的公共行情（只经 ``call`` 调用白名单里的公共 GET）。

    价格轮询由分发器驱动：每 2 秒 ``poll_once()`` 取「关心的合约」（``set_interest`` 设置的，加上最近 10 分钟内
    被请求过 K 线的）的最新价，每 10 秒 ``poll_once(marks=True)`` 连同标记价与资金费率。
    """

    source = "binance"

    def __init__(self, *, clock: Clock, exchange_factory: Callable[[], Any] | None = None, timeout_s: float = 10.0,
                 retries: int = 3, backoff_s: float = 0.5, sleep: Sleep = asyncio.sleep):
        super().__init__(clock)
        self._factory = exchange_factory or (lambda: make_exchange(timeout_s=timeout_s))
        self._timeout_s = timeout_s
        self._retries = max(0, int(retries))
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._ex: Any = None
        self._healthy = False
        self._last_ok_ms: int | None = None
        self._last: dict[str, tuple[Decimal, int]] = {}
        self._mark: dict[str, Decimal] = {}
        self._funding: dict[str, Decimal] = {}
        self._kline_seen: dict[str, int] = {}

    # ------------------------------------------------------------ 连接与调用

    async def _exchange(self) -> Any:
        if self._ex is None:
            ex = self._factory()
            if getattr(ex, "apiKey", None) or getattr(ex, "secret", None):
                try:
                    await ex.close()
                except Exception:  # 关闭失败不影响拒绝
                    pass
                raise PermissionError("行情源只允许无密钥的公共接口：交易所对象带了 apiKey/secret，拒绝使用")
            self._ex = ex
        return self._ex

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """调用一个白名单里的公共 GET：每次尝试都有超时，失败按 backoff_s × 2^i 退避重试 retries 次。"""
        if method not in ALLOWED_PUBLIC_METHODS:
            raise PermissionError(f"行情源只允许白名单里的公共 GET，拒绝调用 {method}")
        ex = await self._exchange()
        fn = getattr(ex, method)
        last_exc: BaseException | None = None
        for attempt in range(self._retries + 1):
            try:
                result = await asyncio.wait_for(fn(dict(params or {})), self._timeout_s)
            except Exception as exc:  # 超时、网络错误、交易所报错；取消（CancelledError）照常向上传
                last_exc = exc
                self.note_error(f"Binance {method} 第 {attempt + 1} 次失败：{_describe(exc)}")
                if attempt < self._retries:
                    await self._sleep(self._backoff_s * (2 ** attempt))
                continue
            self._healthy = True
            self._last_ok_ms = self._clock()
            return result
        self._healthy = False
        raise FeedError(-1001, f"Binance 公共接口 {method} 失败（已重试 {self._retries} 次）：{_describe(last_exc)}",
                        http_status=503)

    async def start(self) -> None:
        info = await self.call("fapiPublicGetExchangeInfo", {})
        instruments = parse_exchange_info(info if isinstance(info, Mapping) else {})
        if not instruments:
            self._healthy = False
            raise FeedError(-1001, "Binance 合约清单为空或无法解析", http_status=503)
        self._instruments = instruments

    async def probe(self) -> None:
        """auto 模式的探测：合约清单 + 一次 K 线。"""
        await self.start()
        symbol = "BTCUSDT" if "BTCUSDT" in self._instruments else next(iter(self._instruments))
        await self.klines(symbol, "15m", 1)

    async def stop(self) -> None:
        ex, self._ex = self._ex, None
        if ex is not None:
            try:
                await ex.close()
            except Exception as exc:  # 关停时的错误只记录
                self.note_error(f"关闭 Binance 连接时出错：{_describe(exc)}")

    def status(self) -> dict[str, Any]:
        return {"source": self.source, "healthy": self._healthy, "last_ok_ms": self._last_ok_ms,
                "errors": list(self._errors)}

    # ------------------------------------------------------------ 行情

    def quote(self, symbol: str) -> Quote | None:
        last = self._last.get(symbol)
        if last is None:
            return None
        return Quote(last[0], self._mark.get(symbol, last[0]), last[1])

    def funding_rate(self, symbol: str) -> Decimal | None:
        return self._funding.get(symbol)

    def watched(self) -> set[str]:
        """价格轮询的对象：关心的合约 + 最近被请求过 K 线的合约。"""
        now = self._clock()
        for sym, seen in list(self._kline_seen.items()):
            if now - seen > _KLINE_WATCH_MS:
                del self._kline_seen[sym]
        return {s for s in self.interest | set(self._kline_seen) if s in self._instruments}

    async def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        self._check(symbol, interval, BINANCE_INTERVALS, limit)
        rows = await self.call("fapiPublicGetKlines", {"symbol": symbol, "interval": interval, "limit": limit})
        self._kline_seen[symbol] = self._clock()
        try:
            return [[int(r[0]), fmt(to_decimal(r[1])), fmt(to_decimal(r[2])), fmt(to_decimal(r[3])),
                     fmt(to_decimal(r[4])), fmt(to_decimal(r[5])), int(r[6])] for r in rows]
        except (TypeError, ValueError, IndexError) as exc:
            raise FeedError(-1001, f"Binance K 线无法解析：{_describe(exc)}", http_status=503) from exc

    async def poll_once(self, marks: bool = False) -> None:
        symbols = sorted(self.watched())
        if symbols:
            await self._fetch(symbols, marks=marks)

    async def ensure_quotes(self, symbols: Iterable[str]) -> None:
        missing = sorted({s for s in symbols if s in self._instruments and s not in self._last})
        if missing:
            await self._fetch(missing, marks=True)

    async def _fetch(self, symbols: list[str], *, marks: bool) -> None:
        wanted = set(symbols)
        for method, store in (("fapiPublicGetTickerPrice", self._store_price),
                              ("fapiPublicGetPremiumIndex", self._store_mark)):
            if method == "fapiPublicGetPremiumIndex" and not marks:
                continue
            if len(symbols) <= _PER_SYMBOL_MAX:
                rows = [await self.call(method, {"symbol": s}) for s in symbols]
            else:
                rows = await self.call(method, {})
            for row in rows if isinstance(rows, list) else [rows]:
                if isinstance(row, Mapping) and row.get("symbol") in wanted:
                    store(row)

    def _store_price(self, row: Mapping[str, Any]) -> None:
        try:
            price = to_decimal(row["price"])
            time_ms = int(row.get("time") or self._clock())
        except (KeyError, TypeError, ValueError):
            self.note_error(f"无法解析的最新价：{dict(row)}")
            return
        if price > 0:
            self._last[row["symbol"]] = (price, time_ms)

    def _store_mark(self, row: Mapping[str, Any]) -> None:
        try:
            mark = to_decimal(row["markPrice"])
            rate = row.get("lastFundingRate")
        except (KeyError, TypeError, ValueError):
            self.note_error(f"无法解析的标记价：{dict(row)}")
            return
        if mark > 0:
            self._mark[row["symbol"]] = mark
        if rate not in (None, ""):
            try:
                self._funding[row["symbol"]] = to_decimal(rate)
            except ValueError:
                self.note_error(f"无法解析的资金费率：{dict(row)}")


# ---------------------------------------------------------------- 按模式打开


async def open_feed(mode: str, *, clock: Clock, replay_dir: Path | str,
                    exchange_factory: Callable[[], Any] | None = None, probe_timeout_s: float = 20.0,
                    timeout_s: float = 10.0, retries: int = 3, backoff_s: float = 0.5, sleep: Sleep = asyncio.sleep,
                    instruments: Iterable[InstrumentSpec] | None = None) -> _FeedBase:
    """按 ``--feed`` 打开行情源。

    - ``replay``：只用回放，从不创建 Binance 连接。
    - ``binance``：探测（合约清单 + 一次 K 线，``probe_timeout_s`` 内）失败就抛 ``FeedError``，不退回。
    - ``auto``：探测失败（含超时）就关闭 Binance 连接，整次运行用回放，并在 ``status()["errors"]`` 里写明原因。
    交易所对象带密钥（``PermissionError``）在任何模式下都直接抛出，不退回。
    """
    if mode not in ("auto", "binance", "replay"):
        raise ValueError(f"未知的行情源模式：{mode}")
    reason = ""
    if mode != "replay":
        binance = BinancePublicFeed(clock=clock, exchange_factory=exchange_factory, timeout_s=timeout_s,
                                    retries=retries, backoff_s=backoff_s, sleep=sleep)
        try:
            await asyncio.wait_for(binance.probe(), probe_timeout_s)
            return binance
        except PermissionError:
            await binance.stop()
            raise
        except Exception as exc:
            await binance.stop()
            reason = f"{probe_timeout_s:g} 秒内探测未完成" if isinstance(exc, TimeoutError) else _describe(exc)
            if mode == "binance":
                raise FeedError(-1001, f"Binance 公共接口不可用：{reason}", http_status=503) from exc
    replay = ReplayFeed(clock=clock, data_dir=replay_dir, instruments=instruments)
    if mode == "auto":
        replay.note_error(f"Binance 公共接口不可用（{reason}），本次运行改用回放行情")
    await replay.start()
    return replay
