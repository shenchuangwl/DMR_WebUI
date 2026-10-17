"""告警与心跳测试的公共夹具：临时账本、手动时钟、事件总线与事件收集器，以及绕过账本的 sqlite3 小帮手
（只用来核对落盘结果）。"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus
from tests.support.ledger import START_MS, temp_ledger


class EventLog:
    """订阅总线并把收到的事件收进列表；``settle`` 等所有事件处理完。"""

    def __init__(self, bus: EventBus, *types: type):
        self.bus = bus
        self.events: list = []
        bus.subscribe(types or None, self.events.append, name="test-event-log")

    async def settle(self) -> list:
        await self.bus.start()
        await self.bus.drain()
        return list(self.events)


class RawDB:
    """绕过账本直接连库：制造脏数据、核对落盘结果。"""

    def __init__(self, path: Path):
        self.path = path

    def exec(self, sql: str, params: tuple = ()) -> None:
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=5)) as conn:
            conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=5)) as conn:
            return conn.execute(sql, params).fetchall()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START_MS)


@pytest.fixture
def ledger(tmp_path, clock):
    with temp_ledger(tmp_path, clock) as opened:
        yield opened


@pytest.fixture
def raw(ledger) -> RawDB:
    return RawDB(ledger.path)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def event_log(bus):
    def make(*types: type) -> EventLog:
        return EventLog(bus, *types)

    return make
