"""单元测试用的可编程适配层（内存实现，不连任何东西）。

读方法：返回 ``set_read(name, value)`` 预设的值；value 可以是普通值（包成 Known）、``Known``/``Unknown``、
或 ``callable(**kwargs)``（按调用参数动态返回）。没有预设的读方法返回 ``Unknown("未预设")``——
这正好检验调用方没有把「未知」当成「空」。

写方法：经基类的出口校验（写权限令牌、系统编号、数量为正、只读影子）后记录到 ``writes``，
返回 ``push_write_result(name, result)`` 排队的结果；没有排队时返回 ``WriteAccepted()``。

需要真实撮合行为的测试用 ``tests/support/inproc.py``（进程内直连替身交易所的分发器）。
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Callable

from dmr_kernel.core.types import CancelOutcome, CancelResult, Known, Unknown, WriteAccepted
from dmr_kernel.exchange.adapter import AccountStream, ExchangeAdapter

READ_METHODS = (
    "server_time", "load_instruments", "fetch_position_mode", "fetch_account", "fetch_positions",
    "fetch_open_orders", "fetch_open_algo_orders", "fetch_order", "fetch_algo_order", "fetch_trades",
    "fetch_income", "fetch_candles", "fetch_tickers", "fetch_leverage", "fetch_restrictions",
)


class FakeAdapter(ExchangeAdapter):
    def __init__(self, *, capability=None, writes_disabled: bool = False):
        super().__init__(capability=capability, writes_disabled=writes_disabled)
        self._reads: dict[str, Any] = {}
        self.read_calls: list[tuple[str, dict]] = []
        self.writes: list[tuple[str, dict]] = []
        self._write_results: dict[str, deque] = defaultdict(deque)
        self.stream: AccountStream | None = None
        self.weight: int | None = None

    # ---------------------------------------------------------------- 预设

    def set_read(self, name: str, value: Any) -> None:
        if name not in READ_METHODS:
            raise ValueError(f"未知的读方法 {name}")
        self._reads[name] = value

    def clear_read(self, name: str) -> None:
        self._reads.pop(name, None)

    def push_write_result(self, name: str, result: Any) -> None:
        self._write_results[name].append(result)

    def calls(self, name: str) -> list[dict]:
        return [kw for n, kw in self.read_calls if n == name]

    def write_calls(self, name: str) -> list[dict]:
        return [kw for n, kw in self.writes if n == name]

    # ---------------------------------------------------------------- 读

    async def _read(self, name: str, **kwargs):
        self.read_calls.append((name, kwargs))
        if name not in self._reads:
            return Unknown("未预设")
        value = self._reads[name]
        if callable(value) and not isinstance(value, (Known, Unknown)):
            value = value(**kwargs)
        if isinstance(value, (Known, Unknown)):
            return value
        return Known(value)

    async def server_time(self):
        return await self._read("server_time")

    async def load_instruments(self):
        return await self._read("load_instruments")

    async def fetch_position_mode(self):
        return await self._read("fetch_position_mode")

    async def fetch_account(self):
        return await self._read("fetch_account")

    async def fetch_positions(self):
        return await self._read("fetch_positions")

    async def fetch_open_orders(self, symbol=None):
        return await self._read("fetch_open_orders", symbol=symbol)

    async def fetch_open_algo_orders(self, symbol=None):
        return await self._read("fetch_open_algo_orders", symbol=symbol)

    async def fetch_order(self, symbol, *, client_order_id=None, exchange_order_id=None):
        return await self._read("fetch_order", symbol=symbol, client_order_id=client_order_id,
                                exchange_order_id=exchange_order_id)

    async def fetch_algo_order(self, symbol, *, client_algo_id=None, algo_id=None):
        return await self._read("fetch_algo_order", symbol=symbol, client_algo_id=client_algo_id, algo_id=algo_id)

    async def fetch_trades(self, symbol, start_ms, end_ms=None, limit=500):
        return await self._read("fetch_trades", symbol=symbol, start_ms=start_ms, end_ms=end_ms, limit=limit)

    async def fetch_income(self, start_ms, end_ms=None, income_type=None, limit=1000):
        return await self._read("fetch_income", start_ms=start_ms, end_ms=end_ms, income_type=income_type,
                                limit=limit)

    async def fetch_candles(self, symbol, interval, limit=99):
        return await self._read("fetch_candles", symbol=symbol, interval=interval, limit=limit)

    async def fetch_tickers(self, symbols=None):
        return await self._read("fetch_tickers", symbols=symbols)

    async def fetch_leverage(self, symbol):
        return await self._read("fetch_leverage", symbol=symbol)

    async def fetch_restrictions(self):
        return await self._read("fetch_restrictions")

    def account_stream(self) -> AccountStream:
        if self.stream is None:
            raise RuntimeError("FakeAdapter 没有配置数据流")
        return self.stream

    def used_weight(self):
        return self.weight

    # ---------------------------------------------------------------- 写

    def _write(self, name: str, kwargs: dict, default: Callable[[], Any]):
        self.writes.append((name, kwargs))
        queue = self._write_results.get(name)
        if queue:
            result = queue.popleft()
            return result(**kwargs) if callable(result) else result
        return default()

    async def _create_market_order(self, **kw):
        return self._write("create_market_order", kw, WriteAccepted)

    async def _create_stop_order(self, **kw):
        return self._write("create_stop_order", kw, WriteAccepted)

    async def _cancel_order(self, **kw):
        return self._write("cancel_order", kw, lambda: CancelResult(CancelOutcome.CANCELLED))

    async def _cancel_algo_order(self, **kw):
        return self._write("cancel_algo_order", kw, lambda: CancelResult(CancelOutcome.CANCELLED))

    async def _set_leverage(self, **kw):
        return self._write("set_leverage", kw, WriteAccepted)

    async def _set_margin_mode(self, **kw):
        return self._write("set_margin_mode", kw, WriteAccepted)
