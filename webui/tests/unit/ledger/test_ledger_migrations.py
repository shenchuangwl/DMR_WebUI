"""账本：迁移可重复升降、与模型一致、已有库升级前先备份（计划 T0.3 用例 11、12）。"""
from __future__ import annotations

import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from dmr_kernel.ledger import migrate, models, repo_meta
from dmr_kernel.ledger.writer import Ledger


def _normalize(sql: str | None) -> str | None:
    return None if sql is None else re.sub(r"\s+", " ", sql).strip()


def schema_snapshot(path: Path) -> dict:
    """库结构的完整快照：sqlite_master 里的全部对象（表、索引含部分索引的条件、触发器）及每张表的列、索引、外键。
    Alembic 自己的 alembic_version 表不算在内。"""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        objects = sorted((kind, name, tbl, _normalize(sql)) for kind, name, tbl, sql in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE tbl_name != 'alembic_version'"))
        tables = sorted(name for kind, name, _tbl, _sql in objects if kind == "table" and not name.startswith("sqlite_"))
        details = {}
        for table in tables:
            indexes = sorted(
                (name, unique, origin, partial, [r[2] for r in conn.execute(f"PRAGMA index_info('{name}')")])
                for _seq, name, unique, origin, partial in conn.execute(f"PRAGMA index_list('{table}')"))
            details[table] = {
                "columns": conn.execute(f"PRAGMA table_xinfo('{table}')").fetchall(),
                "indexes": indexes,
                "fks": sorted(conn.execute(f"PRAGMA foreign_key_list('{table}')").fetchall()),
            }
    return {"objects": objects, "tables": details}


def _triggers(snapshot: dict) -> set[str]:
    return {name for kind, name, _tbl, _sql in snapshot["objects"] if kind == "trigger"}


def _version_rows(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return conn.execute("SELECT version_num FROM alembic_version").fetchall()


def test_migrations_upgrade_downgrade_repeatable(tmp_path):
    path = tmp_path / "m.sqlite"
    migrate.upgrade_db(path)
    first = schema_snapshot(path)
    assert set(first["tables"]) == set(models.Base.metadata.tables)
    assert _triggers(first) == {"audit_log_no_update", "audit_log_no_delete"}
    assert ("index", "uq_position_active_key", "position",
            "CREATE UNIQUE INDEX uq_position_active_key ON position (symbol, side) WHERE status != 'closed'"
            ) in first["objects"]
    head = migrate.head_revision()
    assert head == "0002"
    assert migrate.current_revision(path) == head

    for _round in range(2):
        migrate.downgrade_db(path, "base")
        emptied = schema_snapshot(path)
        assert emptied == {"objects": [], "tables": {}}  # 回退删得干干净净：表、索引、触发器都不剩
        assert _version_rows(path) == []
        assert migrate.current_revision(path) is None
        migrate.upgrade_db(path)
        assert schema_snapshot(path) == first  # 每一轮的结构完全一致
        assert migrate.current_revision(path) == head


def test_migration_matches_models(tmp_path):
    migrated = tmp_path / "migrated.sqlite"
    migrate.upgrade_db(migrated)
    created = tmp_path / "created.sqlite"
    engine = create_engine(f"sqlite:///{created}")
    try:
        models.Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    assert schema_snapshot(migrated) == schema_snapshot(created)


def test_autogenerate_finds_no_drift(tmp_path):
    path = tmp_path / "m.sqlite"
    migrate.upgrade_db(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            assert compare_metadata(context, models.Base.metadata) == []
    finally:
        engine.dispose()


def test_failed_migration_leaves_database_untouched(tmp_path, monkeypatch):
    path = tmp_path / "m.sqlite"
    migrate.upgrade_db(path)
    before = schema_snapshot(path)
    location = _copy_migrations(tmp_path, monkeypatch, BROKEN_MIGRATION)
    assert location.exists()
    with pytest.raises(Exception, match="故意失败"):
        migrate.upgrade_db(path)
    assert schema_snapshot(path) == before  # 整个升级在一个事务里，失败时连已建的表也回滚
    assert _version_rows(path) == [("0002",)]


# ---------------------------------------------------------------- 用例 12：已有库升级前先在线备份

PROBE_MIGRATION = '''"""测试用：模拟一次需要升级的结构变更（批量模式加一列）。"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cooldown") as batch_op:
        batch_op.add_column(sa.Column("probe_note", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cooldown") as batch_op:
        batch_op.drop_column("probe_note")
'''

BROKEN_MIGRATION = '''"""测试用：建了一张表之后失败。"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("half_done", sa.Column("id", sa.Integer(), primary_key=True))
    raise RuntimeError("故意失败")


def downgrade() -> None:
    op.drop_table("half_done")
'''


def _copy_migrations(tmp_path: Path, monkeypatch, extra: str) -> Path:
    location = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, location, ignore=shutil.ignore_patterns("__pycache__"))
    (location / "versions" / "0003_probe.py").write_text(extra, encoding="utf-8")
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", location)
    return location


def _backups(backup_dir: Path) -> list[Path]:
    return sorted(backup_dir.iterdir()) if backup_dir.exists() else []


def test_backup_before_migration_when_db_exists(db_path, backup_dir, clock, monkeypatch):
    ledger = Ledger.open(db_path, backup_dir=backup_dir, clock=clock)
    try:
        ledger.write_sync(repo_meta.put, "probe", "旧库里的数据")
        nonce = ledger.nonce
    finally:
        ledger.close_sync()
    assert _backups(backup_dir) == []  # 新建的库不备份

    Ledger.open(db_path, backup_dir=backup_dir, clock=clock).close_sync()
    assert _backups(backup_dir) == []  # 已是最新版本，不需要升级，也不备份

    _copy_migrations(db_path.parent.parent, monkeypatch, PROBE_MIGRATION)  # 程序带来了新的迁移
    clock.advance(1_000)
    ledger = Ledger.open(db_path, backup_dir=backup_dir, clock=clock)
    try:
        backups = _backups(backup_dir)
        assert len(backups) == 1
        backup = backups[0]
        assert backup.name.startswith("ledger-20251009T085321.000Z")  # 文件名带 UTC 时间戳（取注入的时钟）
        assert _version_rows(backup) == [("0002",)]  # 备份的是升级前的库
        with closing(sqlite3.connect(f"file:{backup}?mode=ro", uri=True)) as conn:
            assert conn.execute("SELECT value FROM meta WHERE key = 'probe'").fetchall() == [("旧库里的数据",)]
            assert "probe_note" not in [r[1] for r in conn.execute("PRAGMA table_info('cooldown')")]
            assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert migrate.current_revision(db_path) == "0003"  # 升级完成
        assert ledger.read_sync(repo_meta.get, "probe") == "旧库里的数据"
        assert ledger.nonce == nonce
    finally:
        ledger.close_sync()


def test_no_backup_dir_means_upgrade_without_backup(db_path, backup_dir, clock, monkeypatch):
    Ledger.open(db_path, backup_dir=None, clock=clock).close_sync()
    _copy_migrations(db_path.parent.parent, monkeypatch, PROBE_MIGRATION)
    ledger = Ledger.open(db_path, backup_dir=None, clock=clock)
    try:
        assert migrate.current_revision(db_path) == "0003"
    finally:
        ledger.close_sync()
    assert _backups(backup_dir) == []


def test_failed_pre_upgrade_backup_aborts_upgrade(db_path, tmp_path, clock, monkeypatch):
    from dmr_kernel.ledger.writer import LedgerError

    Ledger.open(db_path, backup_dir=None, clock=clock).close_sync()
    _copy_migrations(db_path.parent.parent, monkeypatch, PROBE_MIGRATION)
    not_a_dir = tmp_path / "backup-is-a-file"
    not_a_dir.write_text("x")
    with pytest.raises(LedgerError, match="备份"):
        Ledger.open(db_path, backup_dir=not_a_dir, clock=clock)
    assert migrate.current_revision(db_path) == "0002"  # 备份不成就不升级
