"""内核进程的生命周期（第 0a 层，只用标准库）：单实例锁、终止信号、按 4.4 的顺序关停。

关停顺序（计划报告 4.4）：停止接收新指令 → 等在途意图到终态（最多 shutdown_drain_s 秒）→ 关数据流
→ 关交易所连接 → 做一次检查点后关库。交易所侧的保护单不撤。
"""
from __future__ import annotations

import asyncio
import fcntl
import inspect
import logging
import os
import signal
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

SHUTDOWN_PHASES = ("stop_accepting", "drain_intents", "close_stream", "close_exchange", "checkpoint_close_db")
PHASE_LABELS = {
    "stop_accepting": "停止接收新指令",
    "drain_intents": "等在途意图到终态",
    "close_stream": "关数据流",
    "close_exchange": "关交易所连接",
    "checkpoint_close_db": "检查点后关库",
}

Hook = Callable[[], Awaitable[None] | None]


class KernelLockError(RuntimeError):
    pass


class KernelLock:
    """var/kernel.lock 上的独占 flock：防止两个内核同时运行，也供应急入口确认内核已经退出（4.9）。"""

    def __init__(self, fd: int, path: Path):
        self.fd = fd
        self.path = path

    def release(self) -> None:
        if self.fd < 0:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = -1


def acquire_kernel_lock(path: Path) -> KernelLock:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        try:
            holder = os.pread(fd, 32, 0).decode(errors="replace").strip()
        finally:
            os.close(fd)
        raise KernelLockError(f"另一个内核正在运行（锁 {path}，进程号 {holder or '未知'}）") from exc
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    os.fsync(fd)
    return KernelLock(fd, path)


class Lifecycle:
    def __init__(self, *, shutdown_drain_s: float = 10.0, hook_timeout_s: float = 15.0):
        self.shutdown_drain_s = shutdown_drain_s
        self.hook_timeout_s = hook_timeout_s
        self._hooks: dict[str, list[tuple[str, Hook]]] = {p: [] for p in SHUTDOWN_PHASES}
        self._stop = asyncio.Event()
        self._done = False
        self.stop_reason = ""

    def on_shutdown(self, phase: str, hook: Hook, *, name: str) -> None:
        if phase not in self._hooks:
            raise ValueError(f"未知的关停阶段：{phase}")
        self._hooks[phase].append((name, hook))

    def request_stop(self, reason: str = "") -> None:
        self.stop_reason = reason
        self._stop.set()

    async def wait_stop(self) -> None:
        await self._stop.wait()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop, f"signal {sig.name}")

    async def shutdown(self) -> None:
        if self._done:
            return
        self._done = True
        loop = asyncio.get_running_loop()
        for phase in SHUTDOWN_PHASES:
            log.info("shutdown phase %s（%s）", phase, PHASE_LABELS[phase])
            budget = self.shutdown_drain_s if phase == "drain_intents" else self.hook_timeout_s
            deadline = loop.time() + budget
            for name, hook in self._hooks[phase]:
                remaining = max(0.0, deadline - loop.time())
                try:
                    await asyncio.wait_for(_call(hook), remaining if phase == "drain_intents" else budget)
                except asyncio.TimeoutError:
                    log.warning("关停阶段 %s 的 %s 超时，继续下一步", phase, name)
                except Exception:
                    log.exception("关停阶段 %s 的 %s 失败，继续下一步", phase, name)
        log.info("shutdown complete")


async def _call(hook: Hook) -> None:
    result = hook()
    if inspect.isawaitable(result):
        await result
