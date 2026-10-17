"""持仓登记测试的公共夹具：手动时钟、临时账本、装配好的 Rig（登记簿 + 总线 + 事件收集器）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.clock import ManualClock
from tests.support.ledger import START_MS, temp_ledger
from registry_kit import Rig


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START_MS)


@pytest.fixture
def ledger(tmp_path, clock):
    with temp_ledger(tmp_path, clock) as opened:
        yield opened


@pytest.fixture
def rig(ledger, clock) -> Rig:
    return Rig(ledger, clock)
