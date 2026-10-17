"""账本：单写线程、提交后才返回、异常回滚、写锁等待、只读连接（计划 T0.3 用例 3、4、5、6、14）。"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.ledger import repo_meta
from dmr_kernel.ledger.errors import IntegrityError
from dmr_kernel.ledger.models import Meta
from dmr_kernel.ledger.records import MetaRecord
from dmr_kernel.ledger.writer import LedgerError


def _count_meta(session, prefix: str) -> int:
    return session.scalar(select(func.count()).select_from(Meta).where(Meta.key.like(f"{prefix}%")))


async def test_write_runs_on_single_writer_thread_in_order(ledger):
    calls: list[tuple[int, int, str]] = []

    def fn(session, i):
        current = threading.current_thread()
        calls.append((i, current.ident, current.name))
        session.add(Meta(key=f"order-{i:02d}", value=str(i)))
        session.flush()
        return i

    results = await asyncio.gather(*(ledger.write(fn, i) for i in range(50)))
    assert results == list(range(50))
    assert [i for i, _, _ in calls] == list(range(50))  # 按提交顺序
    assert len({ident for _, ident, _ in calls}) == 1  # 同一个线程
    assert {name for _, _, name in calls} == {"ledger-writer"}
    assert calls[0][1] != threading.get_ident()  # 不在事件循环线程上执行
    assert await ledger.read(_count_meta, "order-") == 50


def test_write_sync_from_many_threads_never_overlaps(ledger):
    lock = threading.Lock()
    active = 0
    peak = 0
    names: set[str] = set()

    def fn(session, i):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            names.add(threading.current_thread().name)
            session.add(Meta(key=f"t-{i:03d}", value="x"))
            session.flush()
            time.sleep(0.001)  # 拉宽竞争窗口
        finally:
            with lock:
                active -= 1

    def worker(k):
        for j in range(10):
            ledger.write_sync(fn, k * 10 + j)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert peak == 1
    assert names == {"ledger-writer"}
    assert ledger.read_sync(_count_meta, "t-") == 40


async def test_write_returns_only_after_commit(ledger, raw):
    seen_inside: list[list[tuple]] = []

    def fn(session):
        session.add(Meta(key="visible", value="yes"))
        session.flush()
        seen_inside.append(raw.query("SELECT value FROM meta WHERE key = 'visible'"))  # 另一个连接：提交前看不到
        return "ok"

    assert await ledger.write(fn) == "ok"
    assert seen_inside == [[]]
    assert raw.query("SELECT value FROM meta WHERE key = 'visible'") == [("yes",)]  # 返回时已经提交


async def test_write_exception_rolls_back(ledger, raw):
    class Boom(Exception):
        pass

    error = Boom("故意失败")

    def fn(session):
        session.add(Meta(key="half", value="written"))
        session.flush()
        raise error

    with pytest.raises(Boom) as info:
        await ledger.write(fn)
    assert info.value is error  # 原样抛给调用方
    assert raw.query("SELECT * FROM meta WHERE key = 'half'") == []
    await ledger.write(repo_meta.put, "after", "ok")  # 写线程照常工作
    assert await ledger.read(repo_meta.get, "after") == "ok"


async def test_crash_signal_propagates_and_rolls_back(ledger, raw):
    def fn(session):
        session.add(Meta(key="crash", value="1"))
        session.flush()
        raise CrashSignal("open_sent")

    with pytest.raises(CrashSignal):
        await ledger.write(fn)
    assert raw.query("SELECT * FROM meta WHERE key = 'crash'") == []
    assert await ledger.write(repo_meta.put, "still", "alive") is None


async def test_integrity_error_reaches_caller_and_writer_survives(ledger):
    await ledger.write(repo_meta.put, "dup", "1")

    def insert_dup(session):
        session.add(Meta(key="dup", value="2"))
        session.flush()

    with pytest.raises(IntegrityError):
        await ledger.write(insert_dup)
    assert await ledger.read(repo_meta.get, "dup") == "1"
    await ledger.write(repo_meta.put, "dup", "3")
    assert await ledger.read(repo_meta.get, "dup") == "3"


@pytest.mark.timeout_s(20)
async def test_begin_immediate_blocks_competing_writer(ledger, raw):
    await ledger.write(repo_meta.put, "counter", "1")
    entered: list[str] = []

    def incr(session):
        entered.append("start")
        row = session.get(Meta, "counter")
        entered.append(row.value)
        row.value = str(int(row.value) + 1)
        return row.value

    outside = raw.connect()
    try:
        outside.execute("BEGIN IMMEDIATE")  # 外部连接拿着写锁、改了一半
        outside.execute("UPDATE meta SET value = '2' WHERE key = 'counter'")
        task = asyncio.ensure_future(ledger.write(incr))
        await asyncio.sleep(0.3)
        assert not task.done()  # 在等写锁（busy_timeout），没有失败
        assert entered == []  # BEGIN IMMEDIATE 先拿锁，写函数还没开始执行
        outside.execute("COMMIT")
    finally:
        outside.close()
    assert await asyncio.wait_for(task, 5) == "3"
    assert entered == ["start", "2"]  # 读到的是外部提交后的值，不是旧快照


async def test_reads_do_not_wait_for_writers_and_see_only_committed_data(ledger, raw):
    await ledger.write(repo_meta.put, "counter", "1")
    outside = raw.connect()
    try:
        outside.execute("BEGIN IMMEDIATE")
        outside.execute("UPDATE meta SET value = '2' WHERE key = 'counter'")
        assert await asyncio.wait_for(ledger.read(repo_meta.get, "counter"), 2) == "1"  # 不等锁、不读脏数据
    finally:
        outside.execute("ROLLBACK")
        outside.close()

    started = threading.Event()
    release = threading.Event()

    def slow(session):
        started.set()
        release.wait(5)
        session.add(Meta(key="slow", value="1"))

    task = asyncio.ensure_future(ledger.write(slow))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert await asyncio.wait_for(ledger.read(repo_meta.get, "slow"), 2) is None  # 读不排在写后面
    finally:
        release.set()
    await asyncio.wait_for(task, 5)
    assert await ledger.read(repo_meta.get, "slow") == "1"


async def test_read_uses_readonly_connection(ledger, raw):
    def sneaky_sql(session):
        session.execute(text("INSERT INTO meta(key, value) VALUES ('sneaky', 'x')"))

    def sneaky_orm(session):
        session.add(Meta(key="sneaky", value="y"))
        session.flush()

    with pytest.raises(OperationalError, match="readonly"):
        await ledger.read(sneaky_sql)
    with pytest.raises(OperationalError, match="readonly"):
        await ledger.read(sneaky_orm)
    assert raw.query("SELECT * FROM meta WHERE key = 'sneaky'") == []

    def where(session):
        return threading.current_thread().name, session.execute(text("PRAGMA query_only")).scalar()

    name, query_only = await ledger.read(where)
    assert name.startswith("ledger-read")
    assert query_only == 1
    assert ledger.read_sync(where)[0].startswith("ledger-read")


async def test_cancelled_write_is_skipped_if_not_started(ledger):
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def blocker(session):
        started.set()
        release.wait(5)
        return "first"

    def second(session):
        calls.append("second")
        session.add(Meta(key="second", value="1"))

    first = asyncio.ensure_future(ledger.write(blocker))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        queued = asyncio.ensure_future(ledger.write(second))
        await asyncio.sleep(0)
        queued.cancel()
    finally:
        release.set()
    assert await first == "first"
    with pytest.raises(asyncio.CancelledError):
        await queued
    await ledger.write(repo_meta.put, "barrier", "1")  # 队列已排空
    assert calls == []
    assert await ledger.read(repo_meta.get, "second") is None


async def test_nested_write_from_writer_thread_is_refused(ledger):
    def outer(session):
        return ledger.write_sync(repo_meta.put, "nested", "x")

    with pytest.raises(LedgerError, match="写线程"):
        await asyncio.wait_for(ledger.write(outer), 5)
    assert await ledger.read(repo_meta.get, "nested") is None


async def test_repo_functions_must_not_return_orm_objects(ledger, raw):
    def leaky(session):
        obj = Meta(key="leak", value="1")
        session.add(obj)
        session.flush()
        return obj

    def leaky_list(session):
        return [session.get(Meta, "ledger_nonce")]

    def leaky_result(session):
        return session.execute(select(Meta))

    with pytest.raises(TypeError, match="ORM"):
        await ledger.write(leaky)
    assert raw.query("SELECT * FROM meta WHERE key = 'leak'") == []  # 违反约定的写不提交
    with pytest.raises(TypeError, match="ORM"):
        await ledger.read(leaky_list)
    with pytest.raises(TypeError):
        await ledger.read(leaky_result)

    def as_record(session):
        return MetaRecord.from_row(session.get(Meta, "ledger_nonce"))

    record = await ledger.read(as_record)
    assert record == MetaRecord(key="ledger_nonce", value=ledger.nonce)


@pytest.mark.timeout_s(20)
async def test_write_lock_timeout_fails_that_write_and_writer_survives(db_path, clock, raw, monkeypatch):
    from dmr_kernel.ledger import connect
    from dmr_kernel.ledger.writer import Ledger

    monkeypatch.setattr(connect, "BUSY_TIMEOUT_MS", 200)  # 只为缩短测试；生产固定 5000
    ledger = Ledger.open(db_path, backup_dir=None, clock=clock)
    try:
        entered: list[str] = []

        def fn(session):
            entered.append("in")
            session.add(Meta(key="late", value="1"))

        outside = raw.connect()
        try:
            outside.execute("BEGIN IMMEDIATE")  # 外部连接一直拿着写锁
            with pytest.raises(OperationalError, match="locked"):
                await ledger.write(fn)
            assert entered == []  # 没拿到写锁就不执行写函数
        finally:
            outside.execute("ROLLBACK")
            outside.close()
        await ledger.write(repo_meta.put, "after_lock", "ok")  # 写线程照常工作
        assert await ledger.read(repo_meta.get, "after_lock") == "ok"
        assert await ledger.read(repo_meta.get, "late") is None
    finally:
        await ledger.close()


async def test_repo_functions_cannot_commit_or_rollback(ledger, raw):
    def commits(session):
        session.add(Meta(key="self_commit", value="1"))
        session.commit()

    def rolls_back(session):
        session.rollback()

    with pytest.raises(LedgerError, match="提交"):
        await ledger.write(commits)
    assert raw.query("SELECT * FROM meta WHERE key = 'self_commit'") == []
    with pytest.raises(LedgerError, match="回滚"):
        await ledger.write(rolls_back)
    with pytest.raises(LedgerError, match="回滚"):
        await ledger.read(rolls_back)

    def with_savepoint(session):  # 保存点可以用：内层回滚、外层照常提交
        session.add(Meta(key="outer", value="1"))
        try:
            with session.begin_nested():
                session.add(Meta(key="inner", value="1"))
                session.flush()
                raise ValueError("只撤内层")
        except ValueError:
            pass

    await ledger.write(with_savepoint)
    assert raw.query("SELECT key FROM meta WHERE key IN ('outer', 'inner')") == [("outer",)]


async def test_nested_read_from_writer_or_reader_thread_is_refused(ledger):
    def read_inside_write(session):
        return ledger.read_sync(repo_meta.get, "ledger_nonce")

    def read_inside_read(session):
        return ledger.read_sync(repo_meta.get, "ledger_nonce")

    with pytest.raises(LedgerError, match="写线程"):
        await ledger.write(read_inside_write)
    with pytest.raises(LedgerError, match="只读线程"):
        await ledger.read(read_inside_read)


async def test_repo_functions_must_not_return_rows_or_lazy_results(ledger):
    def rows(session):
        return session.execute(select(Meta.key, Meta.value)).all()

    def generator(session):
        return (r.key for r in session.scalars(select(Meta)))

    def scalars(session):
        return session.scalars(select(Meta.key))

    def plain(session):
        return [tuple(r) for r in session.execute(select(Meta.key, Meta.value).order_by(Meta.key)).all()]

    for fn in (rows, generator, scalars):
        with pytest.raises(TypeError):
            await ledger.read(fn)
    assert [key for key, _ in await ledger.read(plain)] == ["created_ms", "ledger_nonce"]
