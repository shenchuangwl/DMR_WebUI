"""迁移 0002（stop_history）：能升级、能回退、可重复，和模型一致。"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from dmr_kernel.ledger import migrate, models


def _objects(path) -> list[tuple]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()


def _tables(path) -> set[str]:
    return {name for kind, name, _t, _s in _objects(path) if kind == "table"}


def test_head_is_0002_and_creates_stop_history(tmp_path):
    path = tmp_path / "m.sqlite"
    migrate.upgrade_db(path)
    assert migrate.head_revision() == "0002"
    assert "stop_history" in _tables(path)
    assert "stop_history" in models.Base.metadata.tables


def test_downgrade_to_0001_and_up_again_repeatably(tmp_path):
    path = tmp_path / "m.sqlite"
    migrate.upgrade_db(path)
    full = _objects(path)
    for _ in range(2):
        migrate.downgrade_db(path, "0001")
        assert migrate.current_revision(path) == "0001"
        assert "stop_history" not in _tables(path)
        assert "trade" in _tables(path)  # 只回退 0002
        migrate.upgrade_db(path)
        assert _objects(path) == full
    for _ in range(2):
        migrate.downgrade_db(path, "base")
        assert _tables(path) <= {"alembic_version"}
        migrate.upgrade_db(path)
        assert _objects(path) == full


def test_migration_matches_models(tmp_path):
    path = tmp_path / "m.sqlite"
    migrate.upgrade_db(path)
    created = tmp_path / "c.sqlite"
    engine = create_engine(f"sqlite:///{created}")
    try:
        models.Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    pick = lambda p: [o for o in _objects(p) if o[2] == "stop_history"]  # noqa: E731
    assert pick(path) == pick(created) and pick(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            assert compare_metadata(context, models.Base.metadata) == []
    finally:
        engine.dispose()
