"""周期复核与互斥（core.guard）测试的公共夹具。

- 旧入口互斥一律用 tmp_path 下模拟的 DMR_WebUI/data/runtime_state，绝不碰真实目录；另一个进程持锁用 ``holders``。
- 时间用 ManualClock，五分钟一次的检查靠 ``clock.advance`` 推进，不 sleep。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus
from guard_kit import build_env
from lock_kit import LockHolder, make_runtime_dir
from tests.support.ledger import START_MS, temp_ledger


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START_MS)


@pytest.fixture
def ledger(tmp_path, clock):
    with temp_ledger(tmp_path, clock) as opened:
        yield opened


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def runtime_dir(tmp_path) -> Path:
    """模拟的 data/runtime_state：目录 0700，停用标记 LEGACY_DISABLED 已建。"""
    return make_runtime_dir(tmp_path)


@pytest.fixture
def make_env(tmp_path, clock, ledger, bus):
    """``make_env(environment="standin", ...)`` 装配一个 Guard 环境（见 guard_kit.build_env）；收尾时释放持有的锁，
    并核对非实盘的 Env 没有碰过旧入口互斥的目录。"""
    built = []

    def factory(**kwargs):
        env = build_env(tmp_path, clock, ledger, bus, **kwargs)
        built.append(env)
        return env

    yield factory
    for env in built:
        env.legacy.release_all()
    for env in built:
        if env.expect_untouched:
            assert not env.runtime_dir.parent.exists(), "旧入口互斥未启用，却创建了它的目录"


@pytest.fixture
def holders():
    """``holders(path)`` 起一个持锁的子进程；用例结束时全部收掉。"""
    started: list[LockHolder] = []

    def hold(path: Path, *, write_pid: bool = True) -> LockHolder:
        holder = LockHolder(path, write_pid=write_pid)
        started.append(holder)
        return holder

    yield hold
    for holder in started:
        holder.stop()
