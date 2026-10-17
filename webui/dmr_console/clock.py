"""时钟注入：控制台里需要「现在」的地方都经 ``Clock``，测试用手动时钟推进（控制台不导入 dmr_kernel，所以自备）。"""
from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)
