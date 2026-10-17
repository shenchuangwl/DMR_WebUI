"""限频（第 1 层，纯内存，不做 I/O）。

适配层把每个响应的头和状态码交给 ``RateLimiter.observe``；发非保护类读请求之前问 ``pause_s(False)``，大于 0 就不发
（适配层直接返回 ``Unknown``，由调用方下个周期再试）。保护类读请求（持仓、账户、按编号回查、挂单与条件单）和全部写请求
不受限，调用方传 ``protective=True`` 就永远得到 0。

两种暂停：
- 软上限：最近一次读数 ``X-MBX-USED-WEIGHT-1M`` 超过 ``soft_limit_get()`` 时，暂停到读数所在这一分钟结束（交易所按分钟计权重，
  下一分钟读数滚出后自动恢复）；
- 退避：收到 429 或 418 后按 ``Retry-After`` 退避（缺省：429 等到下一分钟，418 按 120 秒），不早于 1 秒。
"""
from __future__ import annotations

import math
from typing import Callable, Mapping

from dmr_kernel.core.clock import Clock

WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"
RETRY_AFTER_HEADER = "Retry-After"

_MINUTE_MS = 60_000
_MIN_BACKOFF_MS = 1_000
_DEFAULT_BAN_MS = 120_000  # 418 没有 Retry-After 时按长封禁处理


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, val in headers.items():
        if key.lower() == lowered:
            return val
    return None


def _next_minute_ms(ts_ms: int) -> int:
    return (ts_ms // _MINUTE_MS + 1) * _MINUTE_MS


class RateLimiter:
    def __init__(self, clock: Clock, soft_limit_get: Callable[[], int]):
        self._clock = clock
        self._soft_limit_get = soft_limit_get
        self.used_weight: int | None = None  # 最近一次响应头里的读数；没有新响应前一直保留
        self._observed_ms: int | None = None
        self._backoff_until_ms: int | None = None

    def observe(self, headers: Mapping[str, str], status: int) -> None:
        now = self._clock.now_ms()
        raw = _header(headers, WEIGHT_HEADER)
        if raw is not None:
            try:
                weight = int(str(raw).strip())
            except ValueError:
                weight = -1
            if weight >= 0:
                self.used_weight = weight
                self._observed_ms = now
        if status in (429, 418):
            until = now + self._retry_after_ms(headers, status, now)
            if self._backoff_until_ms is None or until > self._backoff_until_ms:
                self._backoff_until_ms = until

    @staticmethod
    def _retry_after_ms(headers: Mapping[str, str], status: int, now: int) -> int:
        raw = _header(headers, RETRY_AFTER_HEADER)
        if raw is not None:
            try:
                seconds = float(str(raw).strip())
            except ValueError:
                seconds = math.nan
            if math.isfinite(seconds):
                return max(_MIN_BACKOFF_MS, int(round(seconds * 1000)))
        if status == 418:
            return _DEFAULT_BAN_MS
        return max(_MIN_BACKOFF_MS, _next_minute_ms(now) - now)

    def pause_s(self, protective: bool) -> float:
        """现在要等多少秒才能发非保护类读请求；0 表示不用等。保护类请求永远是 0。"""
        if protective:
            return 0.0
        now = self._clock.now_ms()
        wait_ms = 0
        if self._backoff_until_ms is not None and now < self._backoff_until_ms:
            wait_ms = self._backoff_until_ms - now
        if self.used_weight is not None and self._observed_ms is not None and self.used_weight > self._soft_limit_get():
            window_end = _next_minute_ms(self._observed_ms)
            if now < window_end:
                wait_ms = max(wait_ms, window_end - now)
        return wait_ms / 1000.0
