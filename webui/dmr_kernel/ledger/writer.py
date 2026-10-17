"""账本（第 1 层）：唯一的写线程、只读线程池、在线备份、检查点、关库（docs/设计约定.md 第 8 节；计划 T0.3）。

用法::

    ledger = Ledger.open(db_path, backup_dir=..., clock=...)  # 同步：建目录、必要时先备份、迁移、起线程
    await ledger.write(repo_fn, *args)  # 写线程里开 BEGIN IMMEDIATE 事务执行 repo_fn(session, *args)，提交后才返回
    await ledger.read(repo_fn, *args)   # 只读线程池里用只读会话执行
    await ledger.backup(dest_dir); await ledger.checkpoint(); await ledger.close()

约定与保证：

- 写：所有写函数在同一个线程（``ledger-writer``）里按提交顺序逐个执行，绝不并发。先发 ``BEGIN IMMEDIATE`` 拿到写锁
  （拿不到按 busy_timeout 等待），然后才调用写函数；写函数正常返回 → 提交 → 调用方拿到返回值，所以
  ``await ledger.write(...)`` 返回时数据已经落盘（「先落盘后下单」靠这一点）。写函数抛异常（包括模拟崩溃的
  ``CrashSignal``）→ 整个事务回滚，异常原样抛给调用方，写线程照常工作。
- 写函数不能自己 commit/rollback（会被拒绝），也不能在写函数或读函数里再调用账本的读写（会死锁，或者读不到本事务
  还没提交的改动）：直接用传进来的 session。
- 读：只读连接（``mode=ro`` + ``query_only``），一次读是一个读事务（多条查询看到同一个快照），不等写锁，
  看不到未提交的数据。读要短：长的读事务会挡住检查点。
- 返回值只能是 records.py 的记录或基本类型（及其组成的列表、元组、字典、集合、数据类）。返回 ORM 对象、
  SQLAlchemy 的结果或行、惰性迭代器会报 TypeError，这次写随之回滚。
- 取消：等待 ``write()`` 的任务被取消时，还在排队的写函数不再执行；已经开始执行的照常提交，任务在它做完之后
  才收到 CancelledError（不会出现「调用方以为没写、其实稍后写进去了」的时间差）。
- 关库：``close()`` 之后不再接受新的读写；关库前已经排队的写照常完成，进行中的读与备份做完，然后做检查点
  （TRUNCATE）并关闭所有连接。重复关闭无害。
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import dataclasses
import logging
import queue
import sqlite3
import threading
import types
from collections.abc import Callable, Iterator
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy.orm import Session

from dmr_kernel.core.clock import Clock
from dmr_kernel.ledger import connect, migrate, repo_meta
from dmr_kernel.ledger.backup import online_backup, restore_backup
from dmr_kernel.ledger.errors import LedgerClosed, LedgerError
from dmr_kernel.ledger.models import Base
from dmr_kernel.ledger.records import Record

__all__ = ["Ledger", "LedgerClosed", "LedgerError", "restore_backup"]

T = TypeVar("T")
log = logging.getLogger(__name__)

WRITER_THREAD_NAME = "ledger-writer"
READ_THREAD_PREFIX = "ledger-read"
READ_THREADS = 4
PRE_UPGRADE_LABEL = "pre-upgrade"


class _GuardedSession(Session):
    """交给仓储函数的会话：事务由 Ledger 管，仓储函数不能自己提交或回滚（保存点 begin_nested 可以用）。"""

    def commit(self) -> None:
        raise LedgerError("仓储函数不能自己提交：事务由 Ledger 管，函数正常返回就提交")

    def rollback(self) -> None:
        raise LedgerError("仓储函数不能自己回滚：抛出异常即可，Ledger 会回滚整个事务")


# ---------------------------------------------------------------- 返回值检查

_PLAIN_ATOMS = (str, bytes, int, float, complex, Decimal, Enum, type(None))  # bool 是 int


def _describe(fn: Any) -> str:
    name = getattr(fn, "__qualname__", None) or repr(fn)
    module = getattr(fn, "__module__", None)
    return f"仓储函数 {module}.{name}" if module else f"仓储函数 {name}"


def _check_result(value: Any, fn: Any) -> None:
    """仓储函数只能返回记录或基本类型；ORM 对象、SQLAlchemy 的结果与行、惰性迭代器一律拒绝。"""
    pending = [value]
    seen: set[int] = set()
    while pending:
        obj = pending.pop()
        if isinstance(obj, _PLAIN_ATOMS) or isinstance(obj, Record):
            continue
        if isinstance(obj, Base) or hasattr(obj, "_sa_instance_state"):
            raise TypeError(f"{_describe(fn)} 返回了 ORM 对象 {type(obj).__name__}：仓储函数只能返回 records.py 的"
                            f"记录或基本类型（用 Record.from_row 转换）")
        module = getattr(type(obj), "__module__", "") or ""
        if module == "sqlalchemy" or module.startswith("sqlalchemy."):
            raise TypeError(f"{_describe(fn)} 返回了 SQLAlchemy 的 {type(obj).__name__}：请在函数里把结果取成"
                            f"记录或基本类型")
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, dict):
            pending.extend(obj.keys())
            pending.extend(obj.values())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            pending.extend(obj)
        elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            pending.extend(getattr(obj, f.name) for f in dataclasses.fields(obj))
        elif isinstance(obj, (Iterator, types.GeneratorType)):
            raise TypeError(f"{_describe(fn)} 返回了惰性迭代器 {type(obj).__name__}：会话关闭后就取不到了，"
                            f"请在函数里取完（list(...)）")


# ---------------------------------------------------------------- 任务与等待


@dataclasses.dataclass(eq=False, slots=True)
class _Job:
    kind: str  # write | checkpoint | close
    fn: Callable[..., Any] | None
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: cf.Future


class _Waiter(asyncio.Future):
    """协程一侧的等待对象。

    被取消时同步地取消写线程里还没开始的任务（``asyncio.wrap_future`` 要到下一轮事件循环才转达取消，
    写线程可能已经把任务拿走了）；任务已经开始执行就不接受取消，等它做完（结果照常提交），等待它的任务随后
    收到 CancelledError。"""

    def __init__(self, source: cf.Future, *, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(loop=loop)
        self._source = source

    def cancel(self, msg: Any = None) -> bool:
        if not self.done() and not self._source.cancel():
            return False
        return super().cancel(msg=msg)

    def _settle(self) -> None:
        if self.done():
            return
        source = self._source
        if source.cancelled():
            super().cancel()
            return
        exc = source.exception()
        if exc is not None:
            self.set_exception(exc)
        else:
            self.set_result(source.result())


def _waiter_for(future: cf.Future) -> _Waiter:
    loop = asyncio.get_running_loop()
    waiter = _Waiter(future, loop=loop)

    def _on_done(_: cf.Future) -> None:
        try:
            loop.call_soon_threadsafe(waiter._settle)
        except RuntimeError:  # 事件循环已经关闭：没有人在等这个结果了
            pass

    future.add_done_callback(_on_done)
    return waiter


# ---------------------------------------------------------------- 账本


class Ledger:
    """账本。用 ``Ledger.open()`` 打开；``path`` 是库文件，``nonce`` 是建库时生成的 2 位随机串。"""

    path: Path
    nonce: str

    def __init__(self, path: Path, *, clock: Clock, read_threads: int = READ_THREADS) -> None:
        """只起线程与引擎，不建库、不迁移：请用 ``Ledger.open()``。"""
        self.path = Path(path)
        self.nonce = ""
        self._clock = clock
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue[_Job] = queue.SimpleQueue()
        self._closing = False
        self._close_future: cf.Future | None = None
        self._reader_local = threading.local()
        self._write_engine = connect.writer_engine(self.path)
        self._read_engine = connect.reader_engine(self.path, pool_size=read_threads)
        self._read_pool = cf.ThreadPoolExecutor(max_workers=read_threads, thread_name_prefix=READ_THREAD_PREFIX,
                                                initializer=self._mark_reader_thread)
        self._writer = threading.Thread(target=self._writer_main, name=WRITER_THREAD_NAME, daemon=True)
        self._writer.start()

    # ------------------------------------------------------------ 打开

    @classmethod
    def open(cls, path: Path, *, backup_dir: Path | None, clock: Clock) -> Ledger:
        """同步打开账本：建目录（0700）与库文件（0600）；库已存在且需要升级时先在线备份到 backup_dir；
        Alembic 升级到最新；起写线程与只读线程池；首次建库时在 meta 表生成 ledger_nonce 与 created_ms。"""
        path = Path(path)
        connect.ensure_private_dir(path.parent)
        connect.create_private_file(path)
        try:
            state = migrate.inspect_db(path)
        except sqlite3.DatabaseError as exc:
            raise LedgerError(f"无法读取账本 {path}：{exc}") from exc
        if not state.known:
            raise LedgerError(f"账本 {path} 的结构版本 {state.current} 不在本程序的迁移里（库比程序新，"
                              f"或者不是本程序建的库），拒绝打开")
        if state.current is None and state.user_tables:
            raise LedgerError(f"账本 {path} 里已有表 {list(state.user_tables[:5])}，却没有结构版本记录："
                              f"不是本程序建的库，拒绝打开")
        if state.needs_upgrade:
            if state.current is not None and backup_dir is not None:
                try:
                    saved = online_backup(path, Path(backup_dir), now_ms=clock.now_ms(), label=PRE_UPGRADE_LABEL)
                except Exception as exc:
                    raise LedgerError(f"账本升级前的在线备份失败，没有升级（库保持原样）：{exc}") from exc
                log.info("账本升级前已备份：%s（%s → %s）", saved, state.current, state.head)
            try:
                migrate.upgrade_db(path)
            except Exception as exc:
                raise LedgerError(f"账本迁移失败（{state.current or '空库'} → {state.head}），库保持原样：{exc}") \
                    from exc
        ledger = cls(path, clock=clock)
        try:
            ledger.nonce = ledger.write_sync(repo_meta.ensure_identity, now_ms=clock.now_ms())
        except BaseException:
            ledger.close_sync()
            raise
        return ledger

    # ------------------------------------------------------------ 读写

    async def write(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """交给唯一的写线程：在 BEGIN IMMEDIATE 事务里执行 ``fn(session, *args, **kwargs)``，提交后返回。"""
        future: cf.Future = cf.Future()
        waiter = _waiter_for(future)
        self._submit(_Job("write", fn, args, kwargs, future))
        return await waiter

    def write_sync(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """同 ``write``，阻塞等待（给命令行与测试用；同样经写线程）。"""
        future: cf.Future = cf.Future()
        self._submit(_Job("write", fn, args, kwargs, future))
        return future.result()

    async def read(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """在只读线程池里用只读会话执行 ``fn(session, *args, **kwargs)``。"""
        return await asyncio.wrap_future(self._submit_read(self._execute_read, fn, args, kwargs))

    def read_sync(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        return self._submit_read(self._execute_read, fn, args, kwargs).result()

    # ------------------------------------------------------------ 备份、检查点、关库

    async def backup(self, dest_dir: Path) -> Path:
        """SQLite 在线备份到 ``dest_dir``（文件名带 UTC 时间戳），返回备份文件路径。不挡写。"""
        return await asyncio.wrap_future(self._submit_read(self._backup_now, Path(dest_dir)))

    async def checkpoint(self) -> None:
        """``PRAGMA wal_checkpoint(TRUNCATE)``（在写线程里，排在已提交的写后面）。"""
        future: cf.Future = cf.Future()
        waiter = _waiter_for(future)
        self._submit(_Job("checkpoint", None, (), {}, future))
        await waiter

    async def close(self) -> None:
        """检查点后关库（关停顺序的最后一步）。"""
        await asyncio.shield(asyncio.wrap_future(self._request_close()))

    def close_sync(self) -> None:
        self._request_close().result()
        self._writer.join()

    @property
    def closed(self) -> bool:
        return self._closing

    def __repr__(self) -> str:
        return f"Ledger(path={str(self.path)!r}, nonce={self.nonce!r}, closed={self._closing})"

    # ------------------------------------------------------------ 内部：提交

    def _mark_reader_thread(self) -> None:
        self._reader_local.active = True

    def _refuse_reentry(self, what: str) -> None:
        if threading.current_thread() is self._writer:
            raise LedgerError(f"不能在写线程里调用账本的{what}：写函数里直接用传进来的 session"
                              f"（嵌套调用会死锁，或者读不到本事务还没提交的改动）")
        if getattr(self._reader_local, "active", False):
            raise LedgerError(f"不能在只读线程里调用账本的{what}：读函数里直接用传进来的 session")

    def _submit(self, job: _Job) -> None:
        self._refuse_reentry("写" if job.kind == "write" else "检查点")
        with self._lock:
            if self._closing:
                raise LedgerClosed(f"账本已关闭：{self.path}")
            self._queue.put(job)

    def _submit_read(self, target: Callable[..., Any], *args: Any) -> cf.Future:
        self._refuse_reentry("读")
        with self._lock:
            if self._closing:
                raise LedgerClosed(f"账本已关闭：{self.path}")
            try:
                return self._read_pool.submit(target, *args)
            except RuntimeError as exc:  # 线程池已经关闭
                raise LedgerClosed(f"账本已关闭：{self.path}") from exc

    def _request_close(self) -> cf.Future:
        self._refuse_reentry("关库")
        with self._lock:
            if self._close_future is None:
                self._closing = True
                self._close_future = cf.Future()
                self._queue.put(_Job("close", None, (), {}, self._close_future))
            return self._close_future

    # ------------------------------------------------------------ 内部：写线程

    def _writer_main(self) -> None:
        while True:
            job = self._queue.get()
            if job.kind == "close":
                self._finish_close(job)
                return
            self._run_job(job)

    def _run_job(self, job: _Job) -> None:
        if not job.future.set_running_or_notify_cancel():
            return  # 排队时已被取消：不执行
        try:
            if job.kind == "write":
                result = self._execute_write(job.fn, job.args, job.kwargs)
            else:
                result = self._checkpoint_now()
        except BaseException as exc:  # noqa: BLE001  包括模拟崩溃的 CrashSignal：交给调用方，写线程继续
            job.future.set_exception(exc)
        else:
            job.future.set_result(result)

    def _execute_write(self, fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        with _GuardedSession(self._write_engine, expire_on_commit=False) as session:
            with session.begin():
                session.connection()  # 先发 BEGIN IMMEDIATE 拿到写锁，拿到之后才调用写函数
                result = fn(session, *args, **kwargs)
                _check_result(result, fn)
        return result

    def _checkpoint_now(self) -> None:
        raw = self._write_engine.raw_connection()  # 不经会话：检查点不能在事务里做
        try:
            cursor = raw.cursor()
            try:
                busy, wal_frames, copied = cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                cursor.close()
        finally:
            raw.close()
        if busy:
            log.warning("账本检查点没能做完（有读事务未结束）：WAL %s 帧，已写回 %s 帧", wal_frames, copied)

    def _finish_close(self, job: _Job) -> None:
        running = job.future.set_running_or_notify_cancel()
        try:
            self._shutdown_connections()
        except BaseException as exc:  # noqa: BLE001
            if running:
                job.future.set_exception(exc)
            else:
                log.exception("关库时出错")
        else:
            if running:
                job.future.set_result(None)

    def _shutdown_connections(self) -> None:
        first_error: BaseException | None = None
        steps = (
            lambda: self._read_pool.shutdown(wait=True),  # 进行中的读与备份做完
            self._read_engine.dispose,
            self._checkpoint_now,
            self._write_engine.dispose,  # 最后一条连接关闭时 SQLite 删除 -wal 与 -shm
        )
        for step in steps:
            try:
                step()
            except BaseException as exc:  # noqa: BLE001  每一步都要做，第一个错误留到最后报
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise LedgerError(f"关库时出错：{first_error}") from first_error

    # ------------------------------------------------------------ 内部：只读线程池

    def _execute_read(self, fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        with _GuardedSession(self._read_engine, expire_on_commit=False) as session:
            with session.begin():
                result = fn(session, *args, **kwargs)
                _check_result(result, fn)
        return result

    def _backup_now(self, dest_dir: Path) -> Path:
        return online_backup(self.path, dest_dir, now_ms=self._clock.now_ms())
