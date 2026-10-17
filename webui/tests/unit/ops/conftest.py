"""操作记录与配置修改测试的公共夹具：临时账本、手动时钟、事件总线与事件收集器，
以及绕过账本直接读库的小工具（核对「落盘的到底是什么」）。"""
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
    """独立的只读 sqlite3 连接。"""

    def __init__(self, path: Path):
        self.path = path

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None, timeout=5)) as conn:
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
