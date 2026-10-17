"""时钟（第 0a 层）。内核的存储、计算、调度一律 UTC 毫秒；只有界面层才换成本地时间显示。

算法包不读时钟：需要时间的地方由调用方把时间作为参数传进去。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol

_UNITS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


class Clock(Protocol):
    def now_ms(self) -> int: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """测试用：时间只在调用 advance/set 时变化。"""

    def __init__(self, start_ms: int = 0):
        self._ms = int(start_ms)

    def now_ms(self) -> int:
        return self._ms

    def monotonic(self) -> float:
        return self._ms / 1000.0

    def advance(self, ms: int) -> None:
        self._ms += int(ms)

    def set(self, ms: int) -> None:
        self._ms = int(ms)


def interval_ms(timeframe: str) -> int:
    """'15m' -> 900000。只接受分钟、小时、天。"""
    if len(timeframe) < 2 or timeframe[-1] not in _UNITS or not timeframe[:-1].isdigit():
        raise ValueError(f"无法识别的周期：{timeframe!r}")
    n = int(timeframe[:-1])
    if n <= 0:
        raise ValueError(f"无法识别的周期：{timeframe!r}")
    return n * _UNITS[timeframe[-1]]


def floor_to_interval(ts_ms: int, timeframe: str) -> int:
    step = interval_ms(timeframe)
    return ts_ms - ts_ms % step


def ms_to_iso(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    if ts_ms % 1000:
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_to_ms(text: str) -> int:
    """解析带时区的 ISO 时间（'Z' 或 '+00:00'）；不带时区的拒绝，避免按本机时区误读。"""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"时间必须带时区：{text!r}")
    return int(dt.timestamp() * 1000)
