"""M5 内核单元测试的公共夹具：借用持仓登记与协调者测试的装配（把它们的目录放进导入路径）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_UNIT = Path(__file__).resolve().parents[1]
for _name in ("registry", "coordinator"):
    if str(_UNIT / _name) not in sys.path:
        sys.path.append(str(_UNIT / _name))

import coordinator_kit  # noqa: E402
import registry_kit  # noqa: E402

from dmr_kernel.core.clock import ManualClock  # noqa: E402
from tests.support.ledger import START_MS, temp_ledger  # noqa: E402


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START_MS)


@pytest.fixture
def ledger(tmp_path, clock):
    with temp_ledger(tmp_path, clock) as opened:
        yield opened


@pytest.fixture
def reg_rig(ledger, clock):
    return registry_kit.Rig(ledger, clock)


@pytest.fixture
def coord_rig(ledger, clock):
    return coordinator_kit.Rig(ledger, clock)
