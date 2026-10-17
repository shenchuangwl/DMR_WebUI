"""账本：在线备份与恢复、检查点、关库（计划 T0.3 用例 13、15）。"""
from __future__ import annotations

import asyncio
import sqlite3
import stat
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from dmr_kernel.core.types import IntentAction, IntentSource, IntentState, PositionSide
from dmr_kernel.ledger import models, repo_audit, repo_meta
from dmr_kernel.ledger.records import IntentRecord, MetaRecord
from dmr_kernel.ledger.writer import Ledger, LedgerClosed, LedgerError, restore_backup

T0 = 1_760_000_000_000


def _seed(session, n: int) -> None:
    for i in range(n):
        session.add(models.Intent(idem_key=f"k-{i}", action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL,
                                  symbol="BTCUSDT", side=PositionSide.LONG, qty=Decimal("0.001") * (i + 1),
                                  params={"bar_open_ms": T0 + i, "note": "中文"}, state=IntentState.CREATED,
                                  created_ms=T0 + i, updated_ms=T0 + i))
    session.flush()
    repo_audit.append(session, at_ms=T0, operator="system", action="seed", after={"n": n})


def _dump(session) -> dict:
    return {
        "meta": [MetaRecord.from_row(r) for r in session.scalars(select(models.Meta).order_by(models.Meta.key))],
        "intent": [IntentRecord.from_row(r) for r in session.scalars(select(models.Intent).order_by(models.Intent.id))],
        "audit": repo_audit.recent(session),
    }


def _wal(path: Path) -> Path:
    return path.with_name(path.name + "-wal")


async def test_online_backup_restores_consistent_db(ledger, tmp_path, clock):
    await ledger.write(_seed, 20)
    expected = await ledger.read(_dump)
    backup_file = await ledger.backup(tmp_path / "bk")
    await ledger.write(repo_meta.put, "after_backup", "1")  # 备份之后的写不在备份里

    assert backup_file.parent == tmp_path / "bk"
    assert backup_file.name.startswith("ledger-20251009T085320.000Z") and backup_file.suffix == ".sqlite"
    assert stat.S_IMODE(backup_file.stat().st_mode) == 0o600
    assert not _wal(backup_file).exists()  # 备份是自包含的单个文件

    target = tmp_path / "restored" / "ledger.sqlite"
    restore_backup(backup_file, target)
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    restored = Ledger.open(target, backup_dir=None, clock=clock)
    try:
        assert restored.nonce == ledger.nonce
        assert await restored.read(_dump) == expected  # 数据一致
        assert await restored.read(repo_meta.get, "after_backup") is None
    finally:
        await restored.close()


async def test_backup_taken_between_writes_is_a_consistent_snapshot(ledger, tmp_path, clock):
    async def writer():
        for i in range(30):
            await ledger.write(repo_meta.put, f"w-{i:02d}", str(i))

    task = asyncio.ensure_future(writer())
    await asyncio.sleep(0)
    backup_file = await ledger.backup(tmp_path / "bk")
    await task
    with closing(sqlite3.connect(f"file:{backup_file}?mode=ro", uri=True)) as conn:
        keys = [r[0] for r in conn.execute("SELECT key FROM meta WHERE key LIKE 'w-%' ORDER BY key")]
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert keys == [f"w-{i:02d}" for i in range(len(keys))]  # 前若干个写全在、之后的全不在（没有半截）


async def test_backups_at_the_same_millisecond_do_not_overwrite(ledger, tmp_path):
    first = await ledger.backup(tmp_path / "bk")
    second = await ledger.backup(tmp_path / "bk")
    assert first != second and first.exists() and second.exists()
    assert stat.S_IMODE((tmp_path / "bk").stat().st_mode) == 0o700


def test_restore_refuses_existing_target(ledger, tmp_path):
    backup_file = asyncio.run(ledger.backup(tmp_path / "bk"))
    target = tmp_path / "restored" / "ledger.sqlite"
    target.parent.mkdir()
    target.write_bytes(b"live ledger")
    with pytest.raises(FileExistsError):
        restore_backup(backup_file, target)
    assert target.read_bytes() == b"live ledger"  # 不覆盖已有的库

    other = tmp_path / "restored" / "other.sqlite"
    _wal(other).write_bytes(b"stale wal")
    with pytest.raises(FileExistsError):  # 残留的 -wal 会被套到恢复出来的库上，同样拒绝
        restore_backup(backup_file, other)
    assert not other.exists()


@pytest.mark.parametrize("damage", ["garbage", "truncated"])
def test_restore_rejects_damaged_backup(ledger, tmp_path, damage):
    backup_file = asyncio.run(ledger.backup(tmp_path / "bk"))
    ledger.write_sync(_seed, 200)
    big = asyncio.run(ledger.backup(tmp_path / "bk"))
    if damage == "garbage":
        backup_file.write_bytes(b"this is not a sqlite database" * 100)
        broken = backup_file
    else:
        data = big.read_bytes()
        big.write_bytes(data[: len(data) // 2])
        broken = big
    target = tmp_path / "restored" / "ledger.sqlite"
    with pytest.raises(LedgerError):
        restore_backup(broken, target)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []  # 不留下半成品


def test_restore_missing_backup_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        restore_backup(tmp_path / "nope.sqlite", tmp_path / "restored.sqlite")


async def test_checkpoint_truncates_wal(ledger, db_path):
    await ledger.write(_seed, 50)
    assert _wal(db_path).stat().st_size > 0
    await ledger.checkpoint()
    assert _wal(db_path).stat().st_size == 0
    await ledger.write(repo_meta.put, "after_checkpoint", "1")  # 检查点之后照常可写
    assert await ledger.read(repo_meta.get, "after_checkpoint") == "1"


async def test_close_checkpoints_and_releases(db_path, backup_dir, clock):
    ledger = Ledger.open(db_path, backup_dir=backup_dir, clock=clock)
    await ledger.write(_seed, 50)
    assert _wal(db_path).exists() and _wal(db_path).stat().st_size > 0
    pending = asyncio.ensure_future(ledger.write(repo_meta.put, "last", "written"))
    await asyncio.sleep(0)
    await ledger.close()
    assert await pending is None  # 关库前已排队的写照常完成
    assert not _wal(db_path).exists() or _wal(db_path).stat().st_size == 0  # 检查点后 WAL 被截断或删除

    with pytest.raises(LedgerClosed):
        await ledger.write(repo_meta.put, "too", "late")
    with pytest.raises(LedgerClosed):
        await ledger.read(repo_meta.get, "last")
    with pytest.raises(LedgerClosed):
        ledger.write_sync(repo_meta.put, "too", "late")
    with pytest.raises(LedgerClosed):
        await ledger.backup(db_path.parent / "bk")
    await ledger.close()  # 重复关闭无害

    again = Ledger.open(db_path, backup_dir=backup_dir, clock=clock)
    try:
        assert again.nonce == ledger.nonce
        assert await again.read(repo_meta.get, "last") == "written"
        assert len((await again.read(_dump))["intent"]) == 50
    finally:
        again.close_sync()


def test_restore_rejects_sqlite_file_that_is_not_a_ledger(tmp_path):
    foreign = tmp_path / "foreign.sqlite"
    with closing(sqlite3.connect(foreign)) as conn:
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
    target = tmp_path / "restored" / "ledger.sqlite"
    with pytest.raises(LedgerError, match="ledger_nonce"):
        restore_backup(foreign, target)
    assert list(target.parent.iterdir()) == []
