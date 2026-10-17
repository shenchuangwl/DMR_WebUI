"""账本的结构迁移（第 1 层；Alembic 程序内配置，不依赖 alembic.ini）。

只在 ``Ledger.open`` 里、任何任务开始之前执行（命令行与测试也可以直接调用这里的函数）。

- 一次升级或回退在一个 ``BEGIN IMMEDIATE`` 事务里完成（env.py 沿用这个事务），失败时整个回滚，库保持原样；
- 迁移连接 ``foreign_keys=OFF``（SQLite 批量模式重建表的要求），提交前用 ``PRAGMA foreign_key_check`` 补查；
- 迁移脚本在 ``MIGRATIONS_DIR``，每次调用时现读（测试可以换成别的目录）。
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection

from dmr_kernel.ledger import connect
from dmr_kernel.ledger.errors import LedgerError

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
VERSION_TABLE = "alembic_version"

__all__ = [
    "MIGRATIONS_DIR", "SchemaState", "current_revision", "downgrade_db", "head_revision", "inspect_db",
    "known_revisions", "upgrade_db",
]


@dataclass(frozen=True)
class SchemaState:
    """库的结构版本与程序自带的迁移之间的关系。"""

    current: str | None  # 库里记录的版本；None 表示没有版本（新库，或已回退到 base）
    head: str  # 程序的最新版本
    known: bool  # current 是不是程序认识的版本（None 也算）
    user_tables: tuple[str, ...]  # 库里除 Alembic 版本表以外的表

    @property
    def needs_upgrade(self) -> bool:
        return self.current != self.head


def _config(connection: Connection | None = None) -> Config:
    config = Config()  # 不读 alembic.ini，也不读 pyproject.toml
    config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(_config())


def head_revision() -> str:
    heads = _script().get_heads()
    if len(heads) != 1:
        raise LedgerError(f"账本迁移应当只有一个最新版本，实际是 {sorted(heads)}")
    return heads[0]


def known_revisions() -> set[str]:
    return {script.revision for script in _script().walk_revisions()}


def _read_revision(conn) -> str | None:  # noqa: ANN001  sqlite3 连接
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (VERSION_TABLE,)).fetchone()
    if not exists:
        return None
    rows = [row[0] for row in conn.execute(f"SELECT version_num FROM {VERSION_TABLE}")]
    if len(rows) > 1:
        raise LedgerError(f"账本记录了多个结构版本 {sorted(rows)}（迁移分叉），无法判断该怎么升级")
    return rows[0] if rows else None


def current_revision(path: Path) -> str | None:
    """库里记录的结构版本（只读打开；库不存在时报 FileNotFoundError）。"""
    with closing(connect.connect_raw(Path(path), readonly=True)) as conn:
        return _read_revision(conn)


def inspect_db(path: Path) -> SchemaState:
    """只读地看一眼库：当前版本、程序的最新版本、库里有哪些表。"""
    with closing(connect.connect_raw(Path(path), readonly=True)) as conn:
        current = _read_revision(conn)
        tables = tuple(sorted(
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row[0].startswith("sqlite_") and row[0] != VERSION_TABLE))
    head = head_revision()
    known = current is None or current in known_revisions()
    return SchemaState(current=current, head=head, known=known, user_tables=tables)


def _run(path: Path, action: Callable[[Config], None]) -> None:
    path = Path(path)
    connect.ensure_private_dir(path.parent)
    connect.create_private_file(path)
    engine = connect.migration_engine(path)
    try:
        with engine.begin() as conn:  # BEGIN IMMEDIATE；env.py 里的 begin_transaction() 沿用它
            action(_config(conn))
            problems = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise LedgerError(f"迁移后外键检查不通过（已回滚）：{problems[:5]}")
    finally:
        engine.dispose()


def upgrade_db(path: Path, revision: str = "head") -> None:
    """升级到指定版本（默认最新）。失败时库保持原样，异常原样抛出。"""
    _run(path, lambda config: command.upgrade(config, revision))


def downgrade_db(path: Path, revision: str) -> None:
    """回退到指定版本（``"base"`` 是全部回退）。失败时库保持原样，异常原样抛出。"""
    _run(path, lambda config: command.downgrade(config, revision))
