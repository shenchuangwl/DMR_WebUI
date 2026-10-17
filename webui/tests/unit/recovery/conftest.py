"""重启恢复（core.recovery）测试的公共夹具：手动时钟、临时账本、事件总线、装配好的 Env；重试间隔一律设为 0，测试里不 sleep。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.recovery import Recovery
from recovery_kit import Env
from tests.support.ledger import START_MS, temp_ledger


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    monkeypatch.setattr(Recovery, "SNAPSHOT_RETRY_S", 0.0)
    monkeypatch.setattr(Recovery, "REBUILD_RETRY_S", 0.0)


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
def make_env(tmp_path, clock, ledger, bus):
    """``make_env(**配置覆盖)`` 装配一个 Env（还没 boot）。"""

    def factory(**config) -> Env:
        return Env(tmp_path, clock, ledger, bus, config=config)

    return factory
