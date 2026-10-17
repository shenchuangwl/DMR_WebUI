"""控制接口（uvicorn）的启动与停止路径：起不来、提前退出、迟迟不起、正常停止、赖着不走。

禁网包装下不能绑端口，所以这里把 ``uvicorn.Server`` 换成替身（只提供内核用到的 ``started``/``should_exit``/``force_exit``/
``serve()``）；真实的 uvicorn 在端到端里（tests/e2e_local/test_kernel_full.py，含端口被占用的真实情形）。
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from dmr_kernel.api.logsafe import TokenScrubFilter
from dmr_kernel.core import scheduler
from dmr_kernel.core.scheduler import KernelError
from sched_kit import spy_async
from tests.support.kernel import FakeRecovery, make_kernel


def install_server(monkeypatch, behaviour) -> list:
    """把内核用的 uvicorn 服务器换成替身；``behaviour`` 是 ``async def (server)``，相当于 ``serve()`` 的函数体。
    返回内核造出来的替身实例（按创建顺序）。"""
    created: list = []

    class Server:
        def __init__(self, config) -> None:
            self.config = config
            self.started = False
            self.should_exit = False
            self.force_exit = False
            self.exited = False
            created.append(self)

        async def serve(self) -> None:
            await behaviour(self)

    monkeypatch.setattr(scheduler, "_KernelServer", Server)
    return created


async def comes_up_and_waits_for_the_exit_request(server) -> None:
    server.started = True
    while not server.should_exit:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # 收尾
    server.exited = True


# ---------------------------------------------------------------- 起不来


async def test_a_port_that_cannot_be_bound_aborts_the_start_with_a_clear_reason_and_no_system_exit(tmp_path, monkeypatch):
    """真实的 uvicorn 绑不上端口时调 ``sys.exit(1)``；SystemExit 在任务里会穿过事件循环、把整个进程直接带走（来不及关停），
    所以内核把它接住、换成带端口的中文原因，再按顺序关停。"""

    async def cannot_bind(server) -> None:
        raise SystemExit(1)

    install_server(monkeypatch, cannot_bind)
    recovery = FakeRecovery()
    kernel, _ = make_kernel(tmp_path, env={"DMR_KERNEL_PORT": "18999"}, recovery_factory=recovery)
    with pytest.raises(KernelError) as caught:
        await kernel.start(serve_http=True, run_jobs=False)
    message = str(caught.value)
    assert "控制接口没有起来" in message and "127.0.0.1:18999" in message and "退出码 1" in message
    assert recovery.runs == 0, "控制接口都没起来，不往下做重启恢复"
    await kernel.stop()  # 失败后照常收拾已经起来的部分
    assert kernel.ledger.closed


async def test_a_control_interface_that_returns_without_ever_listening_is_reported(tmp_path, monkeypatch):
    async def returns_at_once(server) -> None:
        return None

    install_server(monkeypatch, returns_at_once)
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        with pytest.raises(KernelError, match="提前退出"):
            await kernel.start(serve_http=True, run_jobs=False)
    finally:
        await kernel.stop()
    assert kernel.ledger.closed


async def test_a_control_interface_that_never_listens_is_given_up_after_the_time_limit(tmp_path, monkeypatch):
    async def never_comes_up(server) -> None:
        while not server.should_exit:
            await asyncio.sleep(0.01)

    created = install_server(monkeypatch, never_comes_up)
    monkeypatch.setattr(scheduler, "HTTP_START_TIMEOUT_S", 0.3)
    recovery = FakeRecovery()
    kernel, _ = make_kernel(tmp_path, recovery_factory=recovery)
    started = time.monotonic()
    try:
        with pytest.raises(KernelError, match="内没有起来"):
            await kernel.start(serve_http=True, run_jobs=False)
        assert 0.25 < time.monotonic() - started < 5
        assert recovery.runs == 0
    finally:
        await kernel.stop()
    assert created[0].should_exit is True and created[0].force_exit is False  # 礼貌地请它退出，它也真的退了
    assert kernel.ledger.closed


# ---------------------------------------------------------------- 起来与停止


async def test_the_control_interface_is_up_during_the_recovery_and_is_stopped_after_the_exchange_is_closed(
        tmp_path, monkeypatch):
    order: list[str] = []
    seen: dict = {}

    async def behaviour(server) -> None:
        await comes_up_and_waits_for_the_exit_request(server)
        order.append("http.exited")

    created = install_server(monkeypatch, behaviour)

    async def during_recovery(kwargs) -> None:
        seen["up"] = created[0].started  # 恢复期间控制接口可用
        order.append("recovery")

    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery(on_run=during_recovery))
    spy_async(kernel.adapter, "close", order=order, label="adapter.close")
    spy_async(kernel.ledger, "close", order=order, label="ledger.close")
    await kernel.start(serve_http=True, run_jobs=False)
    await kernel.stop()
    assert seen == {"up": True}
    assert len(created) == 1 and created[0].exited is True and created[0].force_exit is False
    assert order == ["recovery", "adapter.close", "http.exited", "ledger.close"]
    # 控制接口的日志对令牌是抹掉的（事件流握手的令牌在查询串里）
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert any(isinstance(item, TokenScrubFilter) for item in logging.getLogger(name).filters), name


async def test_a_control_interface_that_will_not_leave_is_forced_out_after_the_time_limit(tmp_path, monkeypatch, caplog):
    async def stubborn(server) -> None:
        server.started = True
        while not server.force_exit:  # 礼貌的退出请求不理，只认强制
            await asyncio.sleep(0.01)
        server.exited = True

    created = install_server(monkeypatch, stubborn)
    monkeypatch.setattr(scheduler, "HTTP_STOP_TIMEOUT_S", 0.3)
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=True, run_jobs=False)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="dmr_kernel.core.scheduler"):
        await kernel.stop()
    assert time.monotonic() - started < 10
    server = created[0]
    assert server.should_exit is True and server.force_exit is True
    assert kernel._server_task.done()
    assert "强制退出" in caplog.text
    assert kernel.ledger.closed


async def test_a_control_interface_that_died_on_its_own_does_not_hold_up_the_stop(tmp_path, monkeypatch):
    async def comes_up_and_dies(server) -> None:
        server.started = True
        await asyncio.sleep(0.05)

    install_server(monkeypatch, comes_up_and_dies)
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=True, run_jobs=False)
    await asyncio.sleep(0.2)
    assert kernel._server_task.done()
    started = time.monotonic()
    await kernel.stop()
    assert time.monotonic() - started < 3
    assert kernel.ledger.closed


async def test_a_kernel_started_without_http_has_no_server_to_stop(tmp_path, monkeypatch):
    created = install_server(monkeypatch, comes_up_and_waits_for_the_exit_request)
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=False)
    await kernel.stop()
    assert created == [] and kernel._server is None and kernel._server_task is None
