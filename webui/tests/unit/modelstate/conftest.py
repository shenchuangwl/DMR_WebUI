"""模型状态测试的公共夹具：手动时钟、临时账本、事件收集、绕过账本直接读写库文件的小工具。

``raw`` 用独立的 sqlite3 连接：测试用它核对「落盘的到底是什么」，以及把一行写坏
（账本自己的仓储函数写不出非法取值）或装一个会失败的触发器来模拟事务中途出错。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus, ModelStateChanged
from tests.support.ledger import START_MS, temp_ledger


class RawDB:
    def __init__(self, path: Path) -> None:
        self.path = path

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None, timeout=5)) as conn:
            return conn.execute(sql, params).fetchall()

    def exec(self, sql: str, params: tuple = ()) -> None:
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=5)) as conn:
            conn.execute(sql, params)


class EventLog:
    """订阅 ModelStateChanged 并收集；``flush`` 让总线把已发布的事件都送到。"""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.events: list[ModelStateChanged] = []
        bus.subscribe(ModelStateChanged, self.events.append, name="test-model-state")

    async def flush(self) -> list[ModelStateChanged]:
        await self.bus.start()
        await self.bus.drain()
        return list(self.events)

    @property
    def versions(self) -> list[int]:
        return [e.version for e in self.events]


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
def events(bus) -> EventLog:
    return EventLog(bus)
