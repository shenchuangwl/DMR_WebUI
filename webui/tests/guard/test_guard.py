"""禁网包装自检：确认测试进程确实连不上任何 IP 地址，而 asyncio 的线程唤醒照常工作。"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

import pytest


def test_guard_env_marker_is_set():
    assert os.environ.get("DMR_WEBUI_NO_NETWORK") == "1"


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_ip_sockets_cannot_be_created(family):
    with pytest.raises(OSError):
        socket.socket(family, socket.SOCK_STREAM)


def test_loopback_connect_is_blocked():
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", 18080), timeout=1)


def test_unix_socketpair_still_works():
    a, b = socket.socketpair()
    try:
        a.send(b"x")
        assert b.recv(1) == b"x"
    finally:
        a.close()
        b.close()


async def test_asyncio_thread_wakeups_work_under_guard():
    loop = asyncio.get_running_loop()
    result = await asyncio.wait_for(loop.run_in_executor(None, lambda: 41 + 1), 5)
    assert result == 42
    fut = loop.create_future()

    def later():
        time.sleep(0.02)
        loop.call_soon_threadsafe(fut.set_result, "ok")

    threading.Thread(target=later, daemon=True).start()
    assert await asyncio.wait_for(fut, 5) == "ok"
