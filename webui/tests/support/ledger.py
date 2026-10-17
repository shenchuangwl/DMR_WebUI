"""测试用的临时账本。

用法（同步夹具里打开，协程测试里直接 ``await ledger.write(...)``）::

    @pytest.fixture
    def ledger(tmp_path, clock):
        with temp_ledger(tmp_path, clock) as opened:
            yield opened
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dmr_kernel.core.clock import Clock, ManualClock
from dmr_kernel.ledger.writer import Ledger

START_MS = 1_760_000_000_000  # 2025-10-09T08:53:20Z


def open_ledger(tmp_path: Path, clock: Clock | None = None, *, name: str = "ledger.sqlite") -> Ledger:
    """在 tmp_path/kernel/ 下打开（不存在就新建）一个账本；调用方负责 ``close_sync()``。"""
    return Ledger.open(tmp_path / "kernel" / name, backup_dir=tmp_path / "backup", clock=clock or ManualClock(START_MS))


@contextmanager
def temp_ledger(tmp_path: Path, clock: Clock | None = None, *, name: str = "ledger.sqlite") -> Iterator[Ledger]:
    opened = open_ledger(tmp_path, clock, name=name)
    try:
        yield opened
    finally:
        opened.close_sync()
