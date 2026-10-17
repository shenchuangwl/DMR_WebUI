"""行情测试的夹具：临时账本、直连库的核对帮手、事件总线与收集器、假世界（World）与行情服务的工厂。

World、truth、EventLog 等帮手在同目录的 marketdata_world.py（测试文件直接导入）。
"""
from __future__ import annotations

from collections.abc import Callable

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus
from marketdata_world import DEFAULT_SERVER_MS, EventLog, RawDB, World
from tests.support.ledger import START_MS, temp_ledger


@pytest.fixture
def ledger(tmp_path):
    with temp_ledger(tmp_path, ManualClock(START_MS)) as opened:
        yield opened


@pytest.fixture
def raw_db(tmp_path) -> RawDB:
    return RawDB(tmp_path / "kernel" / "ledger.sqlite")


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def event_log(bus):
    def make(*types: type) -> EventLog:
        return EventLog(bus, *types)

    return make


@pytest.fixture
def make_world() -> Callable[..., World]:
    def make(**kwargs) -> World:
        kwargs.setdefault("server_ms", DEFAULT_SERVER_MS)  # 某个整点后 5 秒
        return World(**kwargs)

    return make


@pytest.fixture
def make_service(ledger, bus):
    # 延迟导入：时钟与仓储的测试不依赖 CandleService，各自按自己的模块是否存在变红变绿
    from dmr_kernel.marketdata.candles import CandleService
    from dmr_kernel.marketdata.clock import ServerClock

    def make(world: World, *, deadline: float = 20, with_ledger=None, **kwargs) -> tuple[CandleService, ServerClock]:
        server_clock = ServerClock(world.clock)
        config = {"kline_ready_deadline_s": deadline}
        service = CandleService(world.adapter, with_ledger or ledger, bus, world.clock, server_clock,
                                config_get=config.__getitem__, **kwargs)
        return service, server_clock

    return make
