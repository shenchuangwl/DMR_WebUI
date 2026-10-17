"""K 线服务（第 2 层 marketdata）：对时、只给已收盘的 K 线、增量刷新、落盘与预热、收盘后的就绪通知。

核心约束：**未收盘的 K 线永远不会出现在这里的返回值、缓存和 candle 表里**。是否收盘只按服务器时间判断（``ServerClock``）：
``open_time + 周期 <= 服务器时间`` 才算收盘，不信本机时钟。判断用的是**发出请求之前**取得的服务器时间，
所以请求途中才收盘的那根不采信，下一轮自然取到。

缓存与刷新
- 每个（合约, 周期）一份缓存：按开盘时间升序、**连续**（相邻两根正好差一个周期）、最多 ``cache_bars`` 根。出现缺口
  （停机很久、交易所少给了一根）就丢掉缺口之前的旧段，只留最新的连续一段，使用方拿到的序列里永远没有洞。
- ``refresh``：缓存已有截至此刻最新一根已收盘的 K 线就不请求；否则 ``limit = min(99, 缺的根数 + 2)``
  （多要的 2 根：1 根与缓存重叠用来接上，1 根是正在走的、会被丢掉）；缓存是空的取 99 根。每个合约每轮最多一次请求、
  ``limit <= 99``（权重预算）；所有请求（含 ``fetch_closed``）共用一个并发上限 ``max_concurrency``（默认 10）。
- 返回 True = 刷新后缓存里已有截至此刻最新一根已收盘的 K 线。读失败、交易所还没给出最新一根、校准不了服务器时间
  都是 False，缓存保持原样，下一轮按缓存里实际缺的根数补。
- 落盘：一轮里各合约新增（或被交易所修正）的 K 线在轮末**一个事务**写进 candle 表（``repo_candles.upsert_bars``），
  同时把写到的合约修剪到最新 ``cache_bars`` 根。写失败只记日志，数据照常留在内存里并记为「未落盘」，下一轮补写。
- 本轮被时限或取消打断时，已经回来的合约照常并进缓存；被打断的请求一律取消，并等它们真正结束后才返回。

收盘后的就绪（``on_bar_close``）
- 先做一轮刷新（时限 ``kline_ready_deadline_s``，每次调用现读配置，**从调用起算、含校准时间**），就绪 = 刷新后缓存里
  有 ``bar_open_ms`` 这一根；到时没回来的请求被取消，对应合约不在名单里。本方法不重试，要重试由调用方再调一次。
- 发布 ``CandlesReady``（名单为空也发布，让订阅者知道这一根已经处理过了）。
- ``missing_periods``：连续几根 K 线没就绪。同一根重试只计一次；就绪后清零；迟到的旧 K 线重试（比已评估的最新一根还早）
  既不计数也不清零；按服务器时间还没收盘的那根（调用方来早了）不算缺。校准失败时无法判断，按缺计。
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from dmr_kernel.core.clock import Clock, floor_to_interval, interval_ms
from dmr_kernel.core.events import CandlesReady, EventBus
from dmr_kernel.core.types import Candle, Known, ReadResult, Unknown
from dmr_kernel.exchange.adapter import ExchangeAdapter
from dmr_kernel.ledger import repo_candles
from dmr_kernel.ledger.writer import Ledger
from dmr_kernel.marketdata.clock import MAX_ROUND_TRIP_MS, ServerClock

log = logging.getLogger(__name__)

__all__ = ["CandleService", "FRAME_DTYPES", "MAX_REQUEST_LIMIT"]

# 单次读 K 线最多要这么多根：交易所按 limit 档位计权重，不到 100 根是最低一档
MAX_REQUEST_LIMIT = 99
FRAME_DTYPES = {"open_time_ms": "int64", "open": "float64", "high": "float64", "low": "float64", "close": "float64",
                "volume": "float64"}
_FAILURE_SAMPLE = 3  # 一轮里读失败的合约，日志里最多列这么多个


@dataclass(slots=True)
class _Series:
    """一个（合约, 周期）的缓存与收盘评估状态。"""

    bars: list[Candle] = field(default_factory=list)  # 已收盘、升序、连续
    unsaved: set[int] = field(default_factory=set)  # bars 里还没写进账本的开盘时间
    last_bar_ms: int | None = None  # on_bar_close 最近评估过的（最新一根）开盘时间
    missing: int = 0  # 连续没就绪的 K 线根数


@dataclass(frozen=True, slots=True)
class _Round:
    cutoff_ms: int | None  # 本轮判断「是否收盘」用的服务器时间；None = 校准不了，本轮什么都没取
    fresh: dict[str, bool]  # 各合约刷新后缓存是否已到最新一根已收盘的 K 线


# ---------------------------------------------------------------- 参数检查


def _check_symbol(symbol: object) -> str:
    if not isinstance(symbol, str):
        raise TypeError(f"合约必须是字符串：{symbol!r}")
    if not symbol.strip():
        raise ValueError("合约不能为空")
    return symbol


def _check_symbols(symbols: object) -> list[str]:
    """合约集合 -> 去重后保持输入顺序的列表。单个字符串会被逐字符迭代，必须明确拒绝。"""
    if isinstance(symbols, (str, bytes)):
        raise TypeError(f"symbols 必须是合约的集合，不是单个字符串：{symbols!r}")
    names: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:  # type: ignore[attr-defined]
        _check_symbol(symbol)
        if symbol not in seen:
            seen.add(symbol)
            names.append(symbol)
    return names


def _check_timeframe(timeframe: object) -> int:
    if not isinstance(timeframe, str):
        raise TypeError(f"周期必须是字符串：{timeframe!r}")
    return interval_ms(timeframe)  # 不认识的周期抛 ValueError


def _check_int(what: str, value: object, *, low: int = 1, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} 必须是整数：{value!r}")
    if value < low or (high is not None and value > high):
        bound = f"不小于 {low}" if high is None else f"在 {low} 到 {high} 之间"
        raise ValueError(f"{what} 必须{bound}：{value}")
    return value


def _check_bar_open(bar_open_ms: object, period_ms: int) -> int:
    if isinstance(bar_open_ms, bool) or not isinstance(bar_open_ms, int):
        raise TypeError(f"bar_open_ms 必须是整数毫秒：{bar_open_ms!r}")
    if bar_open_ms <= 0 or bar_open_ms % period_ms:
        raise ValueError(f"bar_open_ms 必须是周期（{period_ms} 毫秒）的整数倍：{bar_open_ms}")
    return bar_open_ms


# ---------------------------------------------------------------- 纯函数


def _closed_only(bars: Iterable[Candle], period_ms: int, cutoff_ms: int) -> list[Candle]:
    """去重（同一根出现多次以最后出现的为准）、升序，并剔除按 ``cutoff_ms`` 还没收盘的。"""
    by_open = {bar.open_time_ms: bar for bar in bars}
    return [by_open[open_ms] for open_ms in sorted(by_open) if open_ms + period_ms <= cutoff_ms]


def _holds(series: _Series, bar_open_ms: int, period_ms: int) -> bool:
    """缓存里有没有开盘时间正好是 ``bar_open_ms`` 的这一根（缓存连续，按位置直接算）。"""
    if not series.bars:
        return False
    index, rest = divmod(bar_open_ms - series.bars[0].open_time_ms, period_ms)
    return rest == 0 and 0 <= index < len(series.bars) and series.bars[index].open_time_ms == bar_open_ms


def _note_bar(series: _Series, bar_open_ms: int, *, ready: bool, countable: bool) -> None:
    """记下 on_bar_close 对这根 K 线的评估结果，维护连续没就绪的根数。"""
    last = series.last_bar_ms
    if last is not None and bar_open_ms < last:
        return  # 迟到的旧 K 线重试：以已评估的最新一根为准，不改计数
    if ready:
        series.missing = 0
    elif countable:
        if bar_open_ms != last:  # 同一根重试不重复计
            series.missing += 1
    else:
        return  # 按服务器时间这根还没收盘，调用方来早了：不算缺，也不算评估过
    series.last_bar_ms = bar_open_ms


class CandleService:
    def __init__(self, adapter: ExchangeAdapter, ledger: Ledger, bus: EventBus, clock: Clock,
                 server_clock: ServerClock, *, config_get: Callable[[str], Any], max_concurrency: int = 10,
                 cache_bars: int = 500):
        _check_int("max_concurrency", max_concurrency)
        _check_int("cache_bars", cache_bars)
        self._adapter = adapter
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._server_clock = server_clock
        self._config_get = config_get
        self._cache_bars = cache_bars
        self._slots = asyncio.Semaphore(max_concurrency)  # 所有读 K 线的请求共用
        self._series: dict[tuple[str, str], _Series] = {}
        self._syncing: asyncio.Future[bool] | None = None

    # ------------------------------------------------------------------ 对时

    async def sync_server_time(self) -> bool:
        """读一次交易所服务器时间并校准；成功返回 True。

        读不到、往返太慢（样本太粗）、读数离谱都返回 False 并记警告，已有的偏移保持不变。
        同时进行的多个调用共用同一次读取。
        """
        pending = self._syncing
        if pending is None or pending.done():
            pending = self._syncing = asyncio.ensure_future(self._sample_server_time())
            pending.add_done_callback(self._sync_finished)
        # shield：等待者被取消（比如撞上时限）不能把大家共用的这次读取一起取消
        return await asyncio.shield(pending)

    def _sync_finished(self, task: asyncio.Future[bool]) -> None:
        if self._syncing is task:
            self._syncing = None

    async def _sample_server_time(self) -> bool:
        sent_ms = self._clock.now_ms()
        try:
            result = await self._adapter.server_time()
        except Exception as exc:  # 取消不在此列（CancelledError 不是 Exception）
            log.warning("读取交易所服务器时间时适配层异常，未校准：%s: %s", type(exc).__name__, exc)
            return False
        recv_ms = self._clock.now_ms()
        if isinstance(result, Unknown):
            log.warning("读取交易所服务器时间失败，未校准：%s", result.reason)
            return False
        if recv_ms - sent_ms > MAX_ROUND_TRIP_MS:
            log.warning("读取交易所服务器时间往返 %d 毫秒（上限 %d），样本太粗不采用",
                        recv_ms - sent_ms, MAX_ROUND_TRIP_MS)
            return False
        try:
            self._server_clock.update(result.value, sent_ms, recv_ms)
        except (TypeError, ValueError) as exc:
            log.warning("交易所服务器时间的读数不可用，未校准：%s", exc)
            return False
        return True

    async def _ensure_calibrated(self) -> bool:
        if self._server_clock.skew_ms is not None:
            return True
        return await self.sync_server_time()

    # ------------------------------------------------------------------ 只返回已收盘的 K 线

    async def fetch_closed(self, symbol: str, timeframe: str,
                           limit: int = MAX_REQUEST_LIMIT) -> ReadResult[list[Candle]]:
        """向交易所要最新 ``limit`` 根 K 线，剔除按服务器时间还没收盘的，升序返回（所以可能不足 ``limit`` 根）。

        还没校准过服务器时间会先校准一次；校准不了就不请求，返回 ``Unknown``。读失败与适配层异常都转成 ``Unknown``。
        """
        _check_symbol(symbol)
        period = _check_timeframe(timeframe)
        _check_int("limit", limit, high=MAX_REQUEST_LIMIT)
        if not await self._ensure_calibrated():
            return Unknown("没有可用的服务器时间（校准失败），无法判断 K 线是否收盘")
        return await self._read_closed(symbol, timeframe, limit, period, self._server_clock.now_server_ms())

    async def _read_closed(self, symbol: str, timeframe: str, limit: int, period: int,
                           cutoff_ms: int) -> ReadResult[list[Candle]]:
        try:
            async with self._slots:
                result = await self._adapter.fetch_candles(symbol, timeframe, limit)
        except Exception as exc:  # 取消不在此列
            log.warning("读取 %s %s K 线时适配层异常：%s: %s", symbol, timeframe, type(exc).__name__, exc)
            return Unknown(f"读取 {symbol} {timeframe} K 线时适配层异常：{type(exc).__name__}: {exc}")
        if isinstance(result, Unknown):
            return result
        return Known(_closed_only(result.value, period, cutoff_ms))

    # ------------------------------------------------------------------ 增量刷新

    async def refresh(self, symbols: Iterable[str], timeframe: str) -> dict[str, bool]:
        """增量刷新一批合约的缓存，返回 {合约: 刷新后缓存是否已到最新一根已收盘的 K 线}（顺序同输入，去重）。"""
        period = _check_timeframe(timeframe)
        names = _check_symbols(symbols)
        if not names:
            return {}
        return (await self._round(names, timeframe, period, budget_s=None)).fresh

    async def _round(self, names: list[str], timeframe: str, period: int, *, budget_s: float | None) -> _Round:
        """一轮刷新：校准（需要时）→ 逐个合约规划并发请求 → 并进缓存 → 轮末一次事务落盘。

        ``budget_s`` 不为 None 时整个取数阶段（含校准）受它限制；超时只打断还没回来的请求，已回来的照常并进缓存并落盘。
        """
        cutoff: int | None = None
        failures: dict[str, str] = {}
        deadline = asyncio.timeout(budget_s)
        try:
            async with deadline:
                if await self._ensure_calibrated():
                    cutoff = self._server_clock.now_server_ms()
                    await self._fetch_all(names, timeframe, period, cutoff, failures)
                else:
                    log.warning("%s K 线本轮没有取数：服务器时间校准失败", timeframe)
        except TimeoutError:
            if not deadline.expired():
                raise
            log.warning("%s K 线刷新超过 %s 秒的时限，没回来的请求已取消", timeframe, budget_s)
        if failures:
            sample = "；".join(f"{name}（{reason}）" for name, reason in list(failures.items())[:_FAILURE_SAMPLE])
            log.warning("%s K 线有 %d 个合约读取失败：%s", timeframe, len(failures), sample)
        if cutoff is None:
            return _Round(None, {name: False for name in names})
        await self._persist(names, timeframe)
        return _Round(cutoff, {name: self._plan(name, timeframe, period, cutoff) is None for name in names})

    async def _fetch_all(self, names: list[str], timeframe: str, period: int, cutoff: int,
                         failures: dict[str, str]) -> None:
        async with asyncio.TaskGroup() as group:  # 被取消时先取消并等完所有子任务，再向上传
            for name in names:
                limit = self._plan(name, timeframe, period, cutoff)
                if limit is not None:
                    group.create_task(self._refresh_one(name, timeframe, period, limit, cutoff, failures))

    async def _refresh_one(self, name: str, timeframe: str, period: int, limit: int, cutoff: int,
                           failures: dict[str, str]) -> None:
        try:
            result = await self._read_closed(name, timeframe, limit, period, cutoff)
            if isinstance(result, Unknown):
                failures[name] = result.reason
            else:
                self._store(name, timeframe, period, result.value, fresh=True)
        except Exception:  # 一个合约出意外不能连累整轮的其它合约
            log.exception("刷新 %s %s 的 K 线时出错", name, timeframe)
            failures[name] = "内部错误"

    def _plan(self, name: str, timeframe: str, period: int, cutoff: int) -> int | None:
        """这个合约本轮要向交易所要多少根；None = 缓存已经是最新，不用请求。"""
        series = self._series.get((name, timeframe))
        if series is None or not series.bars:
            return MAX_REQUEST_LIMIT
        newest = floor_to_interval(cutoff, timeframe) - period  # 按 cutoff 最新一根已收盘 K 线的开盘时间
        last = series.bars[-1].open_time_ms
        if last >= newest:
            return None
        missing = -(-(newest - last) // period)  # 向上取整
        return min(MAX_REQUEST_LIMIT, missing + 2)

    # ------------------------------------------------------------------ 缓存与落盘

    def _series_for(self, name: str, timeframe: str) -> _Series:
        key = (name, timeframe)
        series = self._series.get(key)
        if series is None:
            series = self._series[key] = _Series()
        return series

    def _store(self, name: str, timeframe: str, period: int, bars: Iterable[Candle], *, fresh: bool) -> None:
        """把一批已收盘的 K 线并进缓存，保持「升序、连续、最多 cache_bars 根」。

        ``fresh``：交易所刚给的，覆盖缓存里同一根，新增或被修正的记为未落盘；否则是账本里读回来的，缓存里已有的那根以缓存为准。
        """
        series = self._series_for(name, timeframe)
        by_open = {bar.open_time_ms: bar for bar in series.bars}
        for bar in bars:
            known = by_open.get(bar.open_time_ms)
            if fresh:
                if known != bar:
                    by_open[bar.open_time_ms] = bar
                    series.unsaved.add(bar.open_time_ms)
            elif known is None:
                by_open[bar.open_time_ms] = bar
        opens = sorted(by_open)
        if not opens:
            series.bars = []
            series.unsaved.clear()
            return
        start = len(opens) - 1  # 从最新一根往回走到第一个缺口为止
        while start > 0 and opens[start] - opens[start - 1] == period:
            start -= 1
        run = opens[max(start, len(opens) - self._cache_bars):]
        series.bars = [by_open[open_ms] for open_ms in run]
        series.unsaved.intersection_update(run)  # 被丢掉的不用再存

    async def _persist(self, names: list[str], timeframe: str) -> None:
        pending: dict[str, list[Candle]] = {}
        for name in names:
            series = self._series.get((name, timeframe))
            if series is not None and series.unsaved:
                pending[name] = [bar for bar in series.bars if bar.open_time_ms in series.unsaved]
        if not pending:
            return
        try:
            await self._ledger.write(repo_candles.upsert_bars, timeframe, pending, keep=self._cache_bars)
        except Exception:  # 落盘只是为了重启后预热；写不进去不能让行情停摆
            log.exception("%s K 线落盘失败（%d 个合约），数据保留在内存里，下一轮补写", timeframe, len(pending))
            return
        for name, bars in pending.items():
            series = self._series[(name, timeframe)]
            current = {bar.open_time_ms: bar for bar in series.bars}
            for bar in bars:
                if current.get(bar.open_time_ms, bar) == bar:  # 写的期间被交易所修正过的那根，存上的是旧版本，留给下一轮
                    series.unsaved.discard(bar.open_time_ms)

    def closed(self, symbol: str, timeframe: str) -> list[Candle]:
        """缓存里已收盘的 K 线（升序、连续），返回副本；没有缓存是空列表。"""
        series = self._series.get((symbol, timeframe))
        return list(series.bars) if series is not None else []

    def frame(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """同 ``closed``，整理成表格：列 ``FRAME_DTYPES``（open_time_ms 是 int64，其余 float64），升序，每次新建。"""
        bars = self.closed(symbol, timeframe)
        return pd.DataFrame({column: pd.Series([getattr(bar, column) for bar in bars], dtype=dtype)
                             for column, dtype in FRAME_DTYPES.items()})

    # ------------------------------------------------------------------ 预热

    async def warm_up(self, symbols: Iterable[str], timeframe: str, limit: int = 500) -> None:
        """重启后从账本 candle 表把缓存预热回来（每个合约最新的 ``limit`` 根，最多 ``cache_bars`` 根）。不碰交易所。

        读回来的 K 线同样要过「按服务器时间已收盘」和「连续」两道关，并与缓存里已有的合并（同一根以缓存为准）。
        账本读不出来只记日志，缓存保持原样——首次刷新会向交易所补齐。
        """
        period = _check_timeframe(timeframe)
        names = _check_symbols(symbols)
        _check_int("limit", limit, high=repo_candles.MAX_LIMIT)
        if not names:
            return
        try:
            loaded = await self._ledger.read(repo_candles.load_recent, timeframe, names, min(limit, self._cache_bars))
        except Exception:
            log.exception("从账本预热 %s K 线失败，缓存保持原样", timeframe)
            return
        cutoff = self._server_clock.now_server_ms()
        for name in names:
            bars = [Candle(open_time_ms=row.open_time_ms, open=row.open, high=row.high, low=row.low, close=row.close,
                           volume=row.volume, close_time_ms=row.open_time_ms + period - 1)
                    for row in loaded.get(name, ()) if row.open_time_ms + period <= cutoff]
            if bars:
                self._store(name, timeframe, period, bars, fresh=False)

    # ------------------------------------------------------------------ 收盘后的就绪

    async def on_bar_close(self, timeframe: str, bar_open_ms: int, symbols: Iterable[str]) -> CandlesReady:
        """一根 K 线收盘后：刷新一轮，缓存里已有 ``bar_open_ms`` 这根的合约算就绪，发布并返回 ``CandlesReady``。"""
        period = _check_timeframe(timeframe)
        _check_bar_open(bar_open_ms, period)
        names = _check_symbols(symbols)
        ready: list[str] = []
        if names:
            outcome = await self._round(names, timeframe, period, budget_s=self._deadline_s())
            # 校准不了时没法判断这根收盘没有，按调用方说的收盘了算
            reached = outcome.cutoff_ms is None or bar_open_ms + period <= outcome.cutoff_ms
            for name in names:
                series = self._series_for(name, timeframe)
                is_ready = _holds(series, bar_open_ms, period)
                if is_ready:
                    ready.append(name)
                _note_bar(series, bar_open_ms, ready=is_ready, countable=reached)
        event = CandlesReady(timeframe=timeframe, bar_open_ms=bar_open_ms, symbols=tuple(ready),
                             at_ms=self._clock.now_ms())
        self._bus.publish(event)
        return event

    def missing_periods(self, symbol: str, timeframe: str) -> int:
        """这个合约在这个周期上连续几根 K 线没就绪（``on_bar_close`` 评估过的）；没评估过是 0。"""
        series = self._series.get((symbol, timeframe))
        return series.missing if series is not None else 0

    def _deadline_s(self) -> float:
        value = self._config_get("kline_ready_deadline_s")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"kline_ready_deadline_s 必须是正数：{value!r}")
        return float(value)
