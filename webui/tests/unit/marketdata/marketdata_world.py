"""行情测试的公共设施（测试文件直接导入；夹具在同目录的 conftest.py）。

World 把「服务器时间 + 本机时钟 + 假适配层」放在一起：假交易所按服务器时间生成它会返回的 K 线
（含未收盘的最后一根，和真实交易所一样），可以让某些合约读失败、迟迟不出最新一根、读取时抛异常，并记录每次请求。
"""
from __future__ import annotations

import asyncio
import sqlite3
import zlib
from contextlib import closing
from pathlib import Path

from dmr_kernel.core.clock import ManualClock, floor_to_interval, interval_ms
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.types import Candle, Known, Unknown
from tests.support.fake_adapter import FakeAdapter
from tests.support.ledger import START_MS

P15 = interval_ms("15m")
P1H = interval_ms("1h")
# 对齐到整点：15 分钟与 1 小时的 K 线边界都落在它的整数倍上
BASE_MS = floor_to_interval(START_MS, "1h")
# make_world 默认的服务器时间：某个整点后 5 秒；这个整点同时是 15 分钟与 1 小时的 K 线边界
BOUNDARY_MS = BASE_MS + 10 * P1H
DEFAULT_SERVER_MS = BOUNDARY_MS + 5_000


class EventLog:
    """订阅总线并把收到的事件收进列表；``settle`` 等所有事件处理完。"""

    def __init__(self, bus: EventBus, *types: type):
        self.bus = bus
        self.events: list = []
        bus.subscribe(types or None, self.events.append, name="test-event-log")

    async def settle(self) -> list:
        await self.bus.start()
        await self.bus.drain()
        return list(self.events)


def make_bar(symbol: str, open_ms: int, period_ms: int) -> Candle:
    """确定性的 K 线：同一个合约、同一个开盘时间永远得到同样的值。"""
    base = 100.0 + (zlib.crc32(symbol.encode()) % 50)
    idx = open_ms // period_ms
    o = base + (idx % 10)
    return Candle(open_time_ms=open_ms, open=o, high=o + 2.0, low=o - 2.0, close=o + 1.0,
                  volume=1000.0 + (idx % 7), close_time_ms=open_ms + period_ms - 1)


def truth(symbol: str, period_ms: int, first_open_ms: int, last_open_ms: int) -> list[Candle]:
    """交易所对 [first_open_ms, last_open_ms]（含两端）这些整根 K 线应有的内容，升序。"""
    return [make_bar(symbol, open_ms, period_ms) for open_ms in range(first_open_ms, last_open_ms + 1, period_ms)]


class RawDB:
    """绕过账本直接连库，核对落盘结果。"""

    def __init__(self, path: Path):
        self.path = path

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=5)) as conn:
            return conn.execute(sql, params).fetchall()

    def candle_times(self, symbol: str, timeframe: str) -> list[int]:
        rows = self.query("SELECT open_time_ms FROM candle WHERE symbol=? AND timeframe=? ORDER BY open_time_ms",
                          (symbol, timeframe))
        return [row[0] for row in rows]

    def candle_rows(self, symbol: str, timeframe: str) -> list[tuple]:
        return self.query("SELECT open_time_ms, open, high, low, close, volume FROM candle "
                          "WHERE symbol=? AND timeframe=? ORDER BY open_time_ms", (symbol, timeframe))

    def candle_count(self) -> int:
        return self.query("SELECT COUNT(*) FROM candle")[0][0]


class SpyLedger:
    """转发给真账本，同时记下每次读写（仓储函数名、位置参数、关键字参数）；``fail_writes``/``fail_reads`` 为真时一律抛异常。"""

    def __init__(self, inner):
        self.inner = inner
        self.writes: list[tuple[str, tuple, dict]] = []
        self.reads: list[tuple[str, tuple, dict]] = []
        self.fail_writes = False
        self.fail_reads = False

    async def write(self, fn, /, *args, **kwargs):
        self.writes.append((fn.__name__, args, kwargs))
        if self.fail_writes:
            raise RuntimeError("模拟账本写入失败")
        return await self.inner.write(fn, *args, **kwargs)

    async def read(self, fn, /, *args, **kwargs):
        self.reads.append((fn.__name__, args, kwargs))
        if self.fail_reads:
            raise RuntimeError("模拟账本读取失败")
        return await self.inner.read(fn, *args, **kwargs)


class GatedLedger(SpyLedger):
    """第一次写入真正落盘之后先不返回，等 ``release`` 被置位——模拟「已经落盘、调用方还没醒」的那一瞬间。"""

    def __init__(self, inner):
        super().__init__(inner)
        self.first_write_done = asyncio.Event()
        self.release = asyncio.Event()
        self._held = False

    async def write(self, fn, /, *args, **kwargs):
        result = await super().write(fn, *args, **kwargs)
        if not self._held:
            self._held = True
            self.first_write_done.set()
            await self.release.wait()
        return result


class SlowAdapter(FakeAdapter):
    """读 K 线要等一会儿（模拟网络延迟），并记下同时在途的请求数。"""

    def __init__(self, delay_s: float = 0.0, slow_symbols: dict[str, float] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.delay_s = delay_s
        self.slow_symbols = slow_symbols or {}
        self.in_flight = 0
        self.max_in_flight = 0

    async def fetch_candles(self, symbol, interval, limit=99):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.slow_symbols.get(symbol, self.delay_s))
            return await super().fetch_candles(symbol, interval, limit)
        finally:
            self.in_flight -= 1


class SlowClockAdapter(SlowAdapter):
    """读服务器时间也要等一会儿（模拟网络延迟）。"""

    def __init__(self, clock_delay_s: float, **kwargs):
        super().__init__(**kwargs)
        self.clock_delay_s = clock_delay_s

    async def server_time(self):
        await asyncio.sleep(self.clock_delay_s)
        return await super().server_time()


class World:
    """服务器时间（``server_ms``）与本机时钟（``clock``，可以和服务器差一个偏移）一起走；假适配层按服务器时间答复。"""

    def __init__(self, *, server_ms: int, local_offset_ms: int = 0, adapter: FakeAdapter | None = None,
                 history: int = 5000):
        self.server_ms = server_ms
        self.clock = ManualClock(server_ms + local_offset_ms)
        self.adapter = adapter or FakeAdapter()
        self.history = history  # 每个合约最多有这么多根历史 K 线
        self.failing: set[str] = set()  # 读失败（Unknown）的合约
        self.raising: set[str] = set()  # 适配层抛异常的合约
        self.lagging: set[str] = set()  # 读成功但最新一根已收盘的 K 线还没出来的合约
        self.requests: list[dict] = []  # 每次读 K 线的 {symbol, interval, limit}
        self.server_time_calls = 0
        self.server_time_ok = True
        self.server_time_answer = None  # 不为 None 时，读服务器时间一律答复这个值（模拟离谱的答复）
        self.rtt_ms = 0  # 读服务器时间的往返耗时：服务器在中途读数，往返两边对称；负数表示本机时钟在途中被往回拨
        self.adapter.set_read("server_time", self._server_time)
        self.adapter.set_read("fetch_candles", self._fetch_candles)

    def advance(self, ms: int) -> None:
        self.server_ms += ms
        self.clock.advance(ms)

    def _server_time(self):
        self.server_time_calls += 1
        if not self.server_time_ok:
            return Unknown("模拟读服务器时间失败")
        if self.server_time_answer is not None:
            return Known(self.server_time_answer)
        if self.rtt_ms < 0:  # 本机时钟被往回拨：只动本机时钟
            self.clock.advance(self.rtt_ms)
            return Known(self.server_ms)
        reading = self.server_ms + self.rtt_ms // 2  # 服务器在往返的中点读数
        self.advance(self.rtt_ms)
        return Known(reading)

    def _fetch_candles(self, symbol: str, interval: str, limit: int):
        self.requests.append({"symbol": symbol, "interval": interval, "limit": limit})
        if symbol in self.raising:
            raise RuntimeError(f"模拟适配层异常 {symbol}")
        if symbol in self.failing:
            return Unknown("模拟读失败")
        period = interval_ms(interval)
        forming = floor_to_interval(self.server_ms, interval)
        first = forming - (self.history - 1) * period
        bars = []
        for k in range(limit):
            open_ms = forming - k * period
            if open_ms < first:
                break
            if symbol in self.lagging and k == 1:  # 最新一根已收盘的还没出来
                continue
            bars.append(make_bar(symbol, open_ms, period))
        bars.reverse()
        return bars

    def requests_for(self, symbol: str, interval: str | None = None) -> list[dict]:
        return [r for r in self.requests if r["symbol"] == symbol and (interval is None or r["interval"] == interval)]

    def last_closed_open(self, interval: str) -> int:
        """此刻（服务器时间）最新一根已收盘 K 线的开盘时间。"""
        return floor_to_interval(self.server_ms, interval) - interval_ms(interval)

    def latest_closed(self, symbol: str, interval: str, count: int) -> list[Candle]:
        """此刻（服务器时间）最新的 ``count`` 根已收盘 K 线，升序。"""
        period = interval_ms(interval)
        last = self.last_closed_open(interval)
        return truth(symbol, period, last - (count - 1) * period, last)
