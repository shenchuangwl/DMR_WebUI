"""账本测试的公共夹具：临时账本、手动时钟、绕过账本的 sqlite3 连接（只用于核对落盘结果与制造外部写锁）。"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import pytest

from dmr_kernel.core.clock import ManualClock

START_MS = 1_760_000_000_000  # 2025-10-09T08:53:20Z


class RawDB:
    """绕过账本直接连库。测试用它核对「另一个连接看到了什么」，或者扮演抢写锁的外部连接。"""

    def __init__(self, path: Path):
        self.path = path

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            return sqlite3.connect(f"file:{quote(str(self.path))}?mode=ro", uri=True, isolation_level=None, timeout=5)
        return sqlite3.connect(self.path, isolation_level=None, timeout=5)

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with closing(self.connect(readonly=True)) as conn:
            return conn.execute(sql, params).fetchall()

    def columns(self, table: str) -> list[str]:
        return [row[1] for row in self.query(f"PRAGMA table_info('{table}')")]


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START_MS)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "kernel" / "ledger.sqlite"


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    return tmp_path / "backup"


@pytest.fixture
def raw(db_path: Path) -> RawDB:
    return RawDB(db_path)


@pytest.fixture
def ledger(db_path: Path, backup_dir: Path, clock: ManualClock):
    from dmr_kernel.ledger.writer import Ledger

    opened = Ledger.open(db_path, backup_dir=backup_dir, clock=clock)
    try:
        yield opened
    finally:
        opened.close_sync()
