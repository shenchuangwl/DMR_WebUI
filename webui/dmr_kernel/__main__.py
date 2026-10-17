"""交易内核入口：``.venv-kernel/bin/python -m dmr_kernel``（第 8 层，只做启动）。

事件循环：内核自己创建并拥有唯一的事件循环，用标准库 asyncio 的默认实现，不用 uvloop
（T0.1 决定，见 docs/实施记录.md）。控制接口的 uvicorn 以编程方式作为这个循环里的一个任务启动。
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from dmr_kernel.settings import Settings, SettingsError, load_settings

WEBUI_ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("dmr_kernel")


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    fmt.converter = time.gmtime  # 日志时间一律 UTC
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    rotating = logging.handlers.RotatingFileHandler(settings.log_dir / "kernel.log", maxBytes=10 * 1024 * 1024,
                                                    backupCount=5, encoding="utf-8")
    rotating.setFormatter(fmt)
    root.addHandler(rotating)


async def run_shell(settings: Settings) -> int:
    """T0.1 自检用的空壳：取单实例锁，等终止信号，按 4.4 的顺序走一遍关停。不装配任何业务组件。"""
    from dmr_kernel.core.lifecycle import SHUTDOWN_PHASES, Lifecycle, acquire_kernel_lock

    lock = acquire_kernel_lock(settings.kernel_lock_path)
    try:
        life = Lifecycle(shutdown_drain_s=10)
        life.install_signal_handlers()
        for phase in SHUTDOWN_PHASES:
            life.on_shutdown(phase, lambda: None, name="shell")
        log.info("kernel shell ready（环境 %s，进程号 %d）", settings.environment.value, os.getpid())
        await life.wait_stop()
        await life.shutdown()
    finally:
        lock.release()
    return 0


def main() -> int:
    try:
        settings = load_settings(os.environ, webui_root=WEBUI_ROOT)
    except SettingsError as exc:
        print(f"启动配置错误：{exc}", file=sys.stderr)
        return 2
    configure_logging(settings)
    if settings.shell_only:
        return asyncio.run(run_shell(settings))
    from dmr_kernel.core.scheduler import run_kernel

    return asyncio.run(run_kernel(settings))


if __name__ == "__main__":
    sys.exit(main())
