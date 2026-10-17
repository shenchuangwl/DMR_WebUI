"""协调者测试的公共夹具：手动时钟、临时账本、装配好的 Rig。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.clock import ManualClock
from tests.support.ledger import START_MS, temp_ledger
from coordinator_kit import Rig


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


@pytest.fixture
def make_rig(ledger, clock):
    """要改配置或实盘标志时用：make_rig(max_symbols=2) / make_rig(live=True)。"""

    def factory(**kw) -> Rig:
        return Rig(ledger, clock, **kw)

    return factory
