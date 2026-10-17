"""M2B 单元测试的公共夹具：手动时钟、临时账本、事件总线。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus
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
