"""对账（core.reconcile）测试的公共夹具：手动时钟、临时账本、事件总线、装配好的 Env。

对账模块的协作者里，账本、登记簿、模型状态、订单、入账、意图、合约数上限都是真实的；交易所用可编程的 FakeAdapter，
协调者与通知器用假的（只记录）。时间用 ManualClock，不 sleep。协程夹具在本工程的测试约定里不可用：
夹具只造 Env，测试里 ``env = await make_env().boot()``。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus
from reconcile_kit import Env
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
def make_env(clock, ledger, bus):
    """``make_env(**配置覆盖)`` 装配一个 Env（还没 boot）；``instance_filter=True`` 时把本实例号传给 Reconciler。"""

    def factory(*, instance_filter: bool = False, **config) -> Env:
        return Env(clock, ledger, bus, config=config, instance_filter=instance_filter)

    return factory
