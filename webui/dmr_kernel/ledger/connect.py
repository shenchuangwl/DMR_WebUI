"""账本的连接（第 1 层）：文件与目录权限、三种引擎（写、只读、迁移）、绕过 ORM 的 sqlite3 连接。

连接参数（docs/设计约定.md 第 8 节）：WAL、``synchronous=FULL``、``busy_timeout=5000``、``foreign_keys=ON``。
事务由 SQLAlchemy 控制（SQLAlchemy 文档里 pysqlite 的做法）：DBAPI 连接设为 ``isolation_level=None``，
sqlite3 模块自己不再发 BEGIN；引擎的 begin 事件里发我们要的 BEGIN：

- 写引擎：``BEGIN IMMEDIATE``，开事务即拿写锁；拿不到时按 busy_timeout 等待，而不是读到旧快照后才发现冲突。
- 只读引擎：``mode=ro`` 打开并设 ``query_only``；``BEGIN``（延迟）让一次读里的多条查询看到同一个快照。
- 迁移引擎：``BEGIN IMMEDIATE``，整个升级在一个事务里；``foreign_keys=OFF``（批量模式重建表时 SQLite 要求
  关掉外键，且这个设置在事务里改不了），提交前用 ``PRAGMA foreign_key_check`` 补查。

连接都在 ``creator`` 里用 sqlite3 直接打开（不经 URL 解析，路径里有特殊字符也不出错），
``check_same_thread=False``：连接池保证同一时刻只有一个线程用一条连接。
"""
from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.pool import QueuePool

from dmr_kernel.ledger.errors import LedgerError

BUSY_TIMEOUT_MS = 5000
DIR_MODE = 0o700
FILE_MODE = 0o600
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")


# ---------------------------------------------------------------- 目录与文件


def side_files(path: Path) -> list[Path]:
    """库文件旁边 SQLite 自己的文件（WAL、共享内存索引、回滚日志）。"""
    return [path.with_name(path.name + suffix) for suffix in SIDE_SUFFIXES]


def ensure_private_dir(directory: Path) -> None:
    """目录不存在就建（0700）；已有的目录不改权限（可能是别人的上级目录）。"""
    if directory.is_dir():
        return
    directory.mkdir(parents=True, mode=DIR_MODE, exist_ok=True)
    os.chmod(directory, DIR_MODE)  # mkdir 的 mode 受 umask 影响，这里补成精确的 0700


def create_private_file(path: Path) -> bool:
    """库文件不存在就建一个空文件（0600；SQLite 把空文件当作空库，-wal、-shm 沿用库文件的权限）。
    已存在时把库文件和旁边的文件收紧到 0600。返回是否新建。"""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    except FileExistsError:
        for candidate in (path, *side_files(path)):
            try:
                if stat.S_IMODE(candidate.stat().st_mode) != FILE_MODE:
                    os.chmod(candidate, FILE_MODE)
            except FileNotFoundError:
                pass
        return False
    try:
        os.fchmod(fd, FILE_MODE)
    finally:
        os.close(fd)
    return True


def fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- sqlite3 连接


def sqlite_uri(path: Path, **params: str) -> str:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"file:{quote(str(Path(path).absolute()))}" + (f"?{query}" if query else "")


def connect_raw(path: Path, *, readonly: bool = False, immutable: bool = False, foreign_keys: bool = True,
                busy_timeout_ms: int | None = None) -> sqlite3.Connection:
    """绕过 ORM 的连接（自动提交模式，sqlite3 模块不自己发 BEGIN）。

    ``readonly`` 用 ``mode=ro``，库不存在时报错而不是新建；``immutable`` 另外声明文件不会变
    （不加锁、不读也不建 -wal/-shm），只用于校验备份这类离线文件。
    ``busy_timeout_ms`` 缺省时取调用当时的 ``BUSY_TIMEOUT_MS``。"""
    path = Path(path)
    if busy_timeout_ms is None:
        busy_timeout_ms = BUSY_TIMEOUT_MS
    if readonly or immutable:
        params = {"mode": "ro"}
        if immutable:
            params["immutable"] = "1"
        if not path.exists():
            raise FileNotFoundError(f"账本文件不存在：{path}")
        conn = sqlite3.connect(sqlite_uri(path, **params), uri=True, isolation_level=None,
                               check_same_thread=False, timeout=busy_timeout_ms / 1000)
    else:
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=busy_timeout_ms / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        if readonly or immutable:
            conn.execute("PRAGMA query_only = 1")
    except BaseException:
        conn.close()
        raise
    return conn


def _connect_rw(path: Path, *, foreign_keys: bool) -> sqlite3.Connection:
    conn = connect_raw(path, foreign_keys=foreign_keys)
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise LedgerError(f"账本无法切换到 WAL 模式（当前 {mode}）：{path}")
        conn.execute("PRAGMA synchronous = FULL")
    except BaseException:
        conn.close()
        raise
    return conn


def connect_writer(path: Path) -> sqlite3.Connection:
    return _connect_rw(path, foreign_keys=True)


def connect_migrator(path: Path) -> sqlite3.Connection:
    return _connect_rw(path, foreign_keys=False)


def connect_reader(path: Path) -> sqlite3.Connection:
    return connect_raw(path, readonly=True)


# ---------------------------------------------------------------- 引擎


def _engine(path: Path, creator: Callable[[], sqlite3.Connection], *, begin_sql: str, pool_size: int) -> Engine:
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(path)),
        creator=creator,
        poolclass=QueuePool,
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=30,
    )

    @event.listens_for(engine, "begin")
    def _begin(conn) -> None:  # noqa: ANN001
        conn.exec_driver_sql(begin_sql)

    return engine


def writer_engine(path: Path) -> Engine:
    """写线程专用：一条连接，``BEGIN IMMEDIATE``。"""
    path = Path(path)
    return _engine(path, lambda: connect_writer(path), begin_sql="BEGIN IMMEDIATE", pool_size=1)


def reader_engine(path: Path, pool_size: int) -> Engine:
    """只读线程池用：``mode=ro`` + ``query_only``，``BEGIN``（延迟，读快照）。"""
    path = Path(path)
    return _engine(path, lambda: connect_reader(path), begin_sql="BEGIN", pool_size=pool_size)


def migration_engine(path: Path) -> Engine:
    """迁移用：一条连接，``BEGIN IMMEDIATE``，外键检查在提交前单独做。"""
    path = Path(path)
    return _engine(path, lambda: connect_migrator(path), begin_sql="BEGIN IMMEDIATE", pool_size=1)
