"""core.lifecycle：内核单实例锁与按 4.4 顺序的关停（T0.1 验收：空壳能启动并按顺序退出）。"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dmr_kernel.core import lifecycle as lc

WEBUI = Path(__file__).resolve().parents[2]


def test_shutdown_phases_follow_report_4_4_order():
    assert lc.SHUTDOWN_PHASES == (
        "stop_accepting", "drain_intents", "close_stream", "close_exchange", "checkpoint_close_db",
    )


async def test_hooks_run_in_phase_order_regardless_of_registration_order():
    life = lc.Lifecycle(shutdown_drain_s=1)
    order = []
    for phase in reversed(lc.SHUTDOWN_PHASES):
        life.on_shutdown(phase, lambda p=phase: order.append(p), name=phase)
    await life.shutdown()
    assert order == list(lc.SHUTDOWN_PHASES)


async def test_failing_hook_does_not_stop_later_phases():
    life = lc.Lifecycle(shutdown_drain_s=1)
    order = []

    def boom():
        raise RuntimeError("x")

    life.on_shutdown("close_stream", boom, name="boom")
    life.on_shutdown("checkpoint_close_db", lambda: order.append("db"), name="db")
    await life.shutdown()
    assert order == ["db"]


async def test_drain_phase_is_bounded_by_shutdown_drain_s():
    life = lc.Lifecycle(shutdown_drain_s=0.2)

    async def never():
        await asyncio.sleep(30)

    life.on_shutdown("drain_intents", never, name="never")
    t0 = time.monotonic()
    await life.shutdown()
    assert time.monotonic() - t0 < 2


async def test_shutdown_is_idempotent():
    life = lc.Lifecycle(shutdown_drain_s=1)
    n = []
    life.on_shutdown("close_exchange", lambda: n.append(1), name="x")
    await life.shutdown()
    await life.shutdown()
    assert n == [1]


def test_kernel_lock_is_exclusive(tmp_path):
    p = tmp_path / "kernel.lock"
    first = lc.acquire_kernel_lock(p)
    try:
        assert p.read_text().strip() == str(os.getpid())
        with pytest.raises(lc.KernelLockError):
            lc.acquire_kernel_lock(p)
    finally:
        first.release()
    again = lc.acquire_kernel_lock(p)
    again.release()


def test_kernel_shell_starts_and_exits_in_documented_order(tmp_path):
    env = dict(os.environ, DMR_VAR_DIR=str(tmp_path / "var"), DMR_KERNEL_SHELL="1",
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("VIRTUAL_ENV", None)
    proc = subprocess.Popen([sys.executable, "-m", "dmr_kernel"], cwd=str(WEBUI), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 20
        lines = []
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if "kernel shell ready" in line:
                break
        assert any("kernel shell ready" in l for l in lines), lines
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
        lines.append(out)
    finally:
        if proc.poll() is None:
            proc.kill()
    text = "".join(lines)
    assert proc.returncode == 0, text
    positions = [text.find(f"shutdown phase {p}") for p in lc.SHUTDOWN_PHASES]
    assert all(pos >= 0 for pos in positions), text
    assert positions == sorted(positions), text
