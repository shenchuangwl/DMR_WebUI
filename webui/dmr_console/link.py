"""与内核的连通状态。

规则：任何一次传输层失败（连不上、超时、响应无法解析）立刻算「失联」——失联时界面把全部按钮禁用，
所以宁可误判也不漏判；第一次成功立刻恢复。内核回了错误信封（含 401、503）说明内核活着，不算失联。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from dmr_console.clock import Clock


@dataclass(frozen=True)
class LinkStatus:
    lost: bool = False
    reason: str = ""  # 失联原因（中文，不含令牌）
    kind: str = ""  # connect / timeout / protocol
    lost_since_ms: int | None = None
    last_ok_ms: int | None = None
    failures: int = 0  # 连续失败次数


class LinkMonitor:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._status = LinkStatus()

    @property
    def status(self) -> LinkStatus:
        return self._status

    def record_ok(self) -> LinkStatus:
        self._status = LinkStatus(last_ok_ms=self._clock.now_ms())
        return self._status

    def record_failure(self, reason: str, kind: str = "connect") -> LinkStatus:
        now = self._clock.now_ms()
        previous = self._status
        self._status = replace(
            previous, lost=True, reason=reason, kind=kind,
            lost_since_ms=previous.lost_since_ms if previous.lost else now,
            failures=previous.failures + 1,
        )
        return self._status
