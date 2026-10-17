"""run_kernel：环境检查（退出码 2）→ 取内核锁 → 装配 → 启动 → 等终止信号 → 关停 → 释放锁。

装配本身换成假内核（真实的在 test_scheduler_start/shutdown 与集成测试里），这里只验证编排：退出码、锁、信号、
启动中收到终止信号也要在时限内收尾。信号用进程自己的 SIGTERM（先把处理方式设成忽略，万一处理器没装上也不会杀掉测试进程）。
"""
from __future__ import annotations

import asyncio
import os
import signal
from contextlib import contextmanager

from dmr_kernel.core import scheduler
from dmr_kernel.core.lifecycle import Lifecycle, acquire_kernel_lock
from dmr_kernel.core.scheduler import KernelError, StartRefused, run_kernel
from tests.support.kernel import make_settings


class FakeKernel:
    """run_kernel 用到的那一小撮接口：lifecycle、start、stop。"""

    def __init__(self, settings, *, start_impl=None) -> None:
        self.settings = settings
        self.lifecycle = Lifecycle(shutdown_drain_s=1.0)
        self.start_impl = start_impl
        self.events: list = []

    async def start(self, *, serve_http: bool = True, run_jobs: bool = True) -> None:
        self.events.append(("start", serve_http, run_jobs))
        if self.start_impl is not None:
            await self.start_impl(self)
        self.events.append("started")

    async def stop(self) -> None:
        self.events.append("stop")


def install(monkeypatch, settings, **kwargs) -> FakeKernel:
    kernel = FakeKernel(settings, **kwargs)
    monkeypatch.setattr(scheduler, "build_kernel", lambda received, **_: kernel)
    return kernel


def lock_is_free(settings) -> bool:
    try:
        acquire_kernel_lock(settings.kernel_lock_path).release()
    except Exception:
        return False
    return True


@contextmanager
def sigterm_guard():
    """测试期间 SIGTERM 先设成忽略；run_kernel 装上自己的处理器之后才会被它接走。"""
    old = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


# ---------------------------------------------------------------- 拒绝与失败


async def test_other_environments_are_refused_with_exit_code_two_before_anything_is_touched(tmp_path, capsys):
    settings = make_settings(tmp_path, DMR_ENV="testnet")
    assert await run_kernel(settings) == 2
    assert "Demo Trading" in capsys.readouterr().err  # 原因写到标准错误
    assert not settings.kernel_lock_path.exists() and not settings.db_path.exists()


async def test_a_second_kernel_does_not_start_and_does_not_touch_the_ledger(tmp_path, capsys, monkeypatch):
    settings = make_settings(tmp_path)
    built = install(monkeypatch, settings)
    holder = acquire_kernel_lock(settings.kernel_lock_path)
    try:
        assert await run_kernel(settings) == 1
        assert "另一个内核正在运行" in capsys.readouterr().err
        assert built.events == []  # 没有装配、没有启动
    finally:
        holder.release()
    assert not settings.db_path.exists()


async def test_a_refusal_raised_while_building_is_also_exit_code_two(tmp_path, capsys, monkeypatch):
    settings = make_settings(tmp_path)

    def refuse(received, **_):
        raise StartRefused("不允许启动")

    monkeypatch.setattr(scheduler, "build_kernel", refuse)
    assert await run_kernel(settings) == 2
    assert "不允许启动" in capsys.readouterr().err
    assert lock_is_free(settings)


async def test_a_build_error_is_exit_code_one_and_the_lock_is_released(tmp_path, capsys, monkeypatch):
    settings = make_settings(tmp_path)

    def broken(received, **_):
        raise KernelError("找不到令牌文件")

    monkeypatch.setattr(scheduler, "build_kernel", broken)
    assert await run_kernel(settings) == 1
    assert "找不到令牌文件" in capsys.readouterr().err
    assert lock_is_free(settings)


async def test_an_unexpected_build_error_is_exit_code_one_and_logged_with_its_traceback(tmp_path, caplog, monkeypatch):
    settings = make_settings(tmp_path)

    def broken(received, **_):
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(scheduler, "build_kernel", broken)
    assert await run_kernel(settings) == 1
    assert any(record.exc_info and "boom" in str(record.exc_info[1]) for record in caplog.records)
    assert lock_is_free(settings)


async def test_a_failing_start_still_stops_the_kernel_and_releases_the_lock(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)

    async def failing(kernel) -> None:
        raise RuntimeError("恢复失败")

    kernel = install(monkeypatch, settings, start_impl=failing)
    assert await run_kernel(settings) == 1
    assert kernel.events[-1] == "stop" and "started" not in kernel.events
    assert lock_is_free(settings)


# ---------------------------------------------------------------- 正常流程与信号


async def test_the_lock_is_held_while_running_and_released_after_the_stop(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    seen: dict = {}

    async def running(kernel) -> None:
        seen["lock_pid"] = settings.kernel_lock_path.read_text().strip()
        seen["second_start_possible"] = lock_is_free(settings)
        asyncio.get_running_loop().call_later(0.05, kernel.lifecycle.request_stop, "test")

    kernel = install(monkeypatch, settings, start_impl=running)
    assert await asyncio.wait_for(run_kernel(settings), 10) == 0
    assert kernel.events[0] == ("start", True, True)  # 起控制接口、注册定时作业
    assert kernel.events[1:] == ["started", "stop"]
    assert seen == {"lock_pid": str(os.getpid()), "second_start_possible": False}
    assert lock_is_free(settings)


async def test_sigterm_after_the_start_runs_the_shutdown_and_exits_zero(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)

    async def running(kernel) -> None:
        asyncio.get_running_loop().call_later(0.1, os.kill, os.getpid(), signal.SIGTERM)

    kernel = install(monkeypatch, settings, start_impl=running)
    with sigterm_guard():
        assert await asyncio.wait_for(run_kernel(settings), 10) == 0
    assert kernel.events[-2:] == ["started", "stop"]
    assert kernel.lifecycle.stop_reason == "signal SIGTERM"
    assert lock_is_free(settings)


async def test_sigterm_during_a_long_start_abandons_the_start_and_still_shuts_down_in_time(tmp_path, monkeypatch):
    """恢复可能要 30 秒：这时收到终止信号不能等恢复做完（守护只给 30 秒），要放弃启动、直接按顺序关停。"""
    settings = make_settings(tmp_path)
    cancelled: list[bool] = []

    async def endless(kernel) -> None:
        asyncio.get_running_loop().call_later(0.1, os.kill, os.getpid(), signal.SIGTERM)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    kernel = install(monkeypatch, settings, start_impl=endless)
    with sigterm_guard():
        assert await asyncio.wait_for(run_kernel(settings), 10) == 0
    assert cancelled == [True]
    assert kernel.events[-1] == "stop" and "started" not in kernel.events
    assert lock_is_free(settings)


async def test_sigint_is_handled_like_sigterm(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)

    async def running(kernel) -> None:
        asyncio.get_running_loop().call_later(0.1, os.kill, os.getpid(), signal.SIGINT)

    kernel = install(monkeypatch, settings, start_impl=running)
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        assert await asyncio.wait_for(run_kernel(settings), 10) == 0
    finally:
        signal.signal(signal.SIGINT, old)
    assert kernel.lifecycle.stop_reason == "signal SIGINT"


async def test_run_kernel_hands_the_settings_to_the_build(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    received: list = []

    def build(given, **kwargs):
        received.append((given, kwargs))
        kernel = FakeKernel(given)
        kernel.lifecycle.request_stop("test")
        return kernel

    monkeypatch.setattr(scheduler, "build_kernel", build)
    assert await asyncio.wait_for(run_kernel(settings), 10) == 0
    assert received == [(settings, {})]  # 生产入口不传测试用的传输、时钟、崩溃点、恢复工厂
