"""控制接口测试的公共夹具：手动时钟、临时账本、绕过账本直接读库的小工具、事件总线，以及装配好的 Kit。

``raw`` 用独立的只读 sqlite3 连接，核对「落盘的到底是什么」（令牌哈希、审计行、各表行数）。
``kit`` 是真实服务（账本、总线、模型状态、登记簿、配置、风控、合约数、告警）加进程内 TestClient，见 api_kit.py。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus
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

    def counts(self) -> dict[str, int]:
        """每张业务表的行数（不含迁移版本表与 SQLite 内部表）。"""
        tables = [name for (name,) in self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'alembic_version' ORDER BY name")]
        return {name: self.query(f'SELECT COUNT(*) FROM "{name}"')[0][0] for name in tables}


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
def kit(ledger, clock, tmp_path):
    from api_kit import Kit  # 延迟导入：只有用到 kit 的测试才需要整个应用

    with Kit.open(ledger, clock, tmp_path) as opened:
        yield opened


@pytest.fixture
def open_kit(ledger, clock, tmp_path):
    """需要换环境或换客户端选项时用：``with open_kit(env="testnet") as kit:``（一个测试里只开一个）。"""
    from api_kit import Kit

    def make(**options):
        return Kit.open(ledger, clock, tmp_path, **options)

    return make


@pytest.fixture
def rig(ledger, clock, bus):
    from api_kit import AuthRig

    return AuthRig.make(ledger, clock, bus)
