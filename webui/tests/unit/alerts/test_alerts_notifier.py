"""alerts.notifier：告警的本地留痕、发布、外部发送（Apprise；测试里全部用假发送函数，不真发）、重试与去重（T0.8）。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque

import pytest

from dmr_kernel.alerts import notifier as notifier_module
from dmr_kernel.alerts.notifier import Notifier, apprise_send
from dmr_kernel.core.events import AlertRaised
from dmr_kernel.core.types import AlertLevel
from dmr_kernel.ledger import repo_alerts
from tests.support.ledger import START_MS

URL_A = "fake://token-AAA-secret@alpha"
URL_B = "fake://token-BBB-secret@beta"
NOTIFIER_LOGGER = "dmr_kernel.alerts.notifier"


class FakeSender:
    """假发送函数：记录调用，按顺序给出结果（True/False/异常），可以卡住（模拟网络不通时的阻塞）。"""

    def __init__(self, results=(), *, default=True, block: threading.Event | None = None, block_timeout=5.0):
        self.calls: list[tuple] = []
        self.threads: list[threading.Thread] = []
        self.times: list[float] = []
        self._results = deque(results)
        self.default = default
        self.block = block
        self.block_timeout = block_timeout

    def __call__(self, url, title, body, level):
        self.calls.append((url, title, body, level))
        self.threads.append(threading.current_thread())
        self.times.append(time.monotonic())
        if self.block is not None:
            self.block.wait(self.block_timeout)
        result = self._results.popleft() if self._results else self.default
        if isinstance(result, BaseException):
            raise result
        return result


async def wait_until(predicate, timeout=3.0, step=0.005):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(step)


async def wait_row(notifier: Notifier, predicate, timeout=3.0, step=0.005):
    """等到最新一条告警满足条件，返回它。"""
    deadline = time.monotonic() + timeout
    while True:
        rows = await notifier.recent()
        if rows and predicate(rows[0]):
            return rows[0]
        if time.monotonic() > deadline:
            raise AssertionError(f"等待告警记录满足条件超时，当前：{rows[:1]}")
        await asyncio.sleep(step)


def build(ledger, bus, clock, **kwargs) -> Notifier:
    kwargs.setdefault("backoff_s", (0.0,))
    kwargs.setdefault("stop_grace_s", 0.05)
    return Notifier(ledger, bus, clock, **kwargs)


@contextlib.asynccontextmanager
async def running(notifier: Notifier):
    await notifier.start()
    try:
        yield notifier
    finally:
        await notifier.stop()


# ====================================================================== 没配地址：只本地留痕


async def test_without_urls_the_alert_is_recorded_published_and_counted_as_delivered(ledger, bus, clock, event_log):
    log = event_log(AlertRaised)
    n = build(ledger, bus, clock)
    async with running(n):
        assert n.raise_alert(AlertLevel.WARNING, "stream_down", "数据流中断") is None
        clock.advance(7)  # 处理发生在稍后：记录的时间是提出告警的时间
        await n.flush()
    rows = await n.recent()
    assert len(rows) == 1
    row = rows[0]
    assert (row.level, row.kind, row.message) == (AlertLevel.WARNING, "stream_down", "数据流中断")
    assert row.at_ms == START_MS
    assert row.delivered is True and row.delivered_ms == START_MS + 7 and row.attempts == 0  # 本地即送达，没有外部尝试
    events = await log.settle()
    assert events == [AlertRaised(alert_id=row.id, level=AlertLevel.WARNING, kind="stream_down",
                                  message="数据流中断", at_ms=START_MS)]


async def test_without_urls_the_sender_is_never_called(ledger, bus, clock):
    sender = FakeSender()
    n = build(ledger, bus, clock, sender=sender)  # 注入了发送函数，但没有地址
    async with running(n):
        n.raise_alert(AlertLevel.CRITICAL, "k", "m")
        await n.flush()
    assert sender.calls == []
    assert (await n.recent())[0].delivered is True


@pytest.mark.parametrize("level, levelno", [
    (AlertLevel.INFO, logging.INFO), (AlertLevel.WARNING, logging.WARNING), (AlertLevel.CRITICAL, logging.CRITICAL)])
async def test_every_alert_is_logged_at_its_level(ledger, bus, clock, caplog, level, levelno):
    caplog.set_level(logging.DEBUG, logger=NOTIFIER_LOGGER)
    n = build(ledger, bus, clock)
    async with running(n):
        n.raise_alert(level, "kind_x", "消息内容")
        await n.flush()
    lines = [r for r in caplog.records if r.name == NOTIFIER_LOGGER and "kind_x" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].levelno == levelno and "消息内容" in lines[0].getMessage()


async def test_alerts_are_recorded_in_the_order_they_were_raised(ledger, bus, clock, event_log):
    log = event_log(AlertRaised)
    n = build(ledger, bus, clock)
    async with running(n):
        for i in range(40):
            n.raise_alert(AlertLevel.INFO, "seq", f"第{i}条")
        await n.flush()
    rows = list(reversed(await n.recent(100)))
    assert [r.message for r in rows] == [f"第{i}条" for i in range(40)]
    ids = [r.id for r in rows]
    assert ids == sorted(ids) and len(set(ids)) == 40
    assert [e.alert_id for e in await log.settle()] == ids


async def test_recent_is_newest_first_and_respects_the_limit(ledger, bus, clock):
    n = build(ledger, bus, clock)
    async with running(n):
        for i in range(5):
            n.raise_alert(AlertLevel.INFO, "k", f"m{i}")
        await n.flush()
        assert [r.message for r in await n.recent()] == ["m4", "m3", "m2", "m1", "m0"]
        assert [r.message for r in await n.recent(limit=2)] == ["m4", "m3"]


# ====================================================================== 不阻塞、不抛异常


async def test_raise_alert_does_not_block_the_event_loop_while_the_sender_blocks(ledger, bus, clock):
    release = threading.Event()
    sender = FakeSender(block=release)
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    async with running(n):
        task = asyncio.create_task(ticker())
        try:
            started = time.monotonic()
            n.raise_alert(AlertLevel.CRITICAL, "stream_down", "数据流中断")
            assert time.monotonic() - started < 0.05  # 入队就返回
            await wait_until(lambda: len(sender.calls) == 1)  # 发送函数已经在某个线程里卡住了
            assert sender.threads[0] is not threading.main_thread()  # 不在事件循环线程里
            before = ticks
            await asyncio.sleep(0.15)
            assert ticks - before >= 10  # 事件循环照常转
            rows = await n.recent()
            assert len(rows) == 1 and rows[0].delivered is False and rows[0].attempts == 0  # 本地已留痕，送达还在进行
            release.set()
            await n.flush()
            rows = await n.recent()
            assert rows[0].delivered is True and rows[0].attempts == 1
        finally:
            release.set()
            task.cancel()


async def test_a_stuck_channel_does_not_delay_recording_the_next_alerts(ledger, bus, clock, event_log):
    release = threading.Event()
    sender = FakeSender(block=release)
    log = event_log(AlertRaised)
    await bus.start()
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender)
    async with running(n):
        try:
            n.raise_alert(AlertLevel.WARNING, "first", "1")
            await wait_until(lambda: len(sender.calls) == 1)  # 第一条的发送卡住了
            n.raise_alert(AlertLevel.WARNING, "second", "2")
            n.raise_alert(AlertLevel.WARNING, "third", "3")
            await wait_until(lambda: len(log.events) == 3)  # 后两条照样落账并发布
            assert [r.kind for r in reversed(await n.recent())] == ["first", "second", "third"]
        finally:
            release.set()
        await n.flush()
    assert all(r.delivered for r in await n.recent())


async def test_raise_alert_never_raises(ledger, bus, clock, caplog):
    n = build(ledger, bus, clock, max_queue=2)
    # 没有 start，队列只有 2 个位置：多出来的丢弃并记日志，调用方不受影响
    for i in range(6):
        assert n.raise_alert(AlertLevel.INFO, "k", f"m{i}") is None
    assert "丢弃" in caplog.text
    await n.flush()  # 没 start 时 flush 就地处理已入队的
    assert [r.message for r in reversed(await n.recent())] == ["m0", "m1"]


async def test_an_unknown_level_is_recorded_as_a_warning_instead_of_raising(ledger, bus, clock):
    n = build(ledger, bus, clock)
    async with running(n):
        assert n.raise_alert("loud", "k", "级别写错了") is None  # type: ignore[arg-type]
        assert n.raise_alert("critical", "k2", "字符串形式的有效级别") is None  # type: ignore[arg-type]
        await n.flush()
    rows = {r.kind: r for r in await n.recent()}
    assert rows["k"].level is AlertLevel.WARNING and rows["k2"].level is AlertLevel.CRITICAL


async def test_non_string_kind_and_message_are_stringified(ledger, bus, clock):
    n = build(ledger, bus, clock)
    async with running(n):
        assert n.raise_alert(AlertLevel.INFO, 42, None) is None  # type: ignore[arg-type]
        await n.flush()
    row = (await n.recent())[0]
    assert (row.kind, row.message) == ("42", "None")


async def test_a_failed_ledger_write_loses_neither_the_log_line_nor_the_next_alerts(ledger, bus, clock, caplog,
                                                                                   monkeypatch):
    caplog.set_level(logging.DEBUG, logger=NOTIFIER_LOGGER)
    real_insert = repo_alerts.insert
    calls = {"n": 0}

    def flaky(session, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("磁盘故障")
        return real_insert(session, **kwargs)

    monkeypatch.setattr(repo_alerts, "insert", flaky)
    n = build(ledger, bus, clock)
    async with running(n):
        n.raise_alert(AlertLevel.CRITICAL, "first", "第一条")
        n.raise_alert(AlertLevel.INFO, "second", "第二条")
        await n.flush()
    monkeypatch.undo()
    assert [r.kind for r in await n.recent()] == ["second"]  # 工作协程没有因为一次失败而死掉
    assert "first" in caplog.text and "第一条" in caplog.text  # 写账本失败时，日志里仍有这条告警
    assert "写入账本失败" in caplog.text  # 并且留下了写账本失败的错误日志


# ====================================================================== 外部发送：参数、重试、留痕


async def test_sender_receives_the_address_title_body_and_level(ledger, bus, clock):
    sender = FakeSender()
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender)
    async with running(n):
        n.raise_alert(AlertLevel.CRITICAL, "stream_down", "数据流中断超过 90 秒")
        await n.flush()
    assert len(sender.calls) == 1
    url, title, body, level = sender.calls[0]
    assert url == URL_A and body == "数据流中断超过 90 秒" and level is AlertLevel.CRITICAL
    assert "stream_down" in title and "严重" in title
    row = (await n.recent())[0]
    assert (row.delivered, row.attempts, row.delivered_ms) == (True, 1, START_MS)


@pytest.mark.parametrize("level, label", [
    (AlertLevel.INFO, "提示"), (AlertLevel.WARNING, "警告"), (AlertLevel.CRITICAL, "严重")])
async def test_title_carries_the_level_in_chinese(ledger, bus, clock, level, label):
    sender = FakeSender()
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender)
    async with running(n):
        n.raise_alert(level, "kk", "mm")
        await n.flush()
    assert label in sender.calls[0][1]


async def test_a_failing_channel_is_retried_and_every_attempt_is_counted(ledger, bus, clock):
    sender = FakeSender([False, RuntimeError("连接被拒绝"), True])
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender, max_attempts=5)
    async with running(n):
        n.raise_alert(AlertLevel.CRITICAL, "k", "m")
        await n.flush()
    assert len(sender.calls) == 3
    assert len({c for c in sender.calls}) == 1  # 每次发的内容一样
    row = (await n.recent())[0]
    assert (row.delivered, row.attempts, row.delivered_ms) == (True, 3, START_MS)


async def test_each_failed_attempt_is_persisted_as_it_happens(ledger, bus, clock):
    gate = threading.Event()
    calls: list[int] = []

    def sender(url, title, body, level):
        calls.append(1)
        if len(calls) == 1:
            return False
        gate.wait(5)
        return True

    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender)
    async with running(n):
        try:
            n.raise_alert(AlertLevel.WARNING, "k", "m")
            await wait_until(lambda: len(calls) == 2)  # 第二次尝试已经开始（卡在 gate 上）
            row = (await n.recent())[0]
            assert (row.attempts, row.delivered) == (1, False)  # 第一次失败已经记下
        finally:
            gate.set()
        await n.flush()
    row = (await n.recent())[0]
    assert (row.attempts, row.delivered) == (2, True)


async def test_retries_stop_after_max_attempts_and_the_alert_stays_traced_locally(ledger, bus, clock, event_log):
    log = event_log(AlertRaised)
    sender = FakeSender(default=False)
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender, max_attempts=3)
    async with running(n):
        n.raise_alert(AlertLevel.CRITICAL, "k", "渠道不可达")
        await n.flush()
        await asyncio.sleep(0.05)  # 之后不会再有尝试
    assert len(sender.calls) == 3
    row = (await n.recent())[0]
    assert (row.delivered, row.delivered_ms, row.attempts) == (False, None, 3)
    assert row.message == "渠道不可达"  # 本地留痕
    assert len(await log.settle()) == 1  # 事件照常发布一次


async def test_exceptions_from_the_sender_count_as_failures_and_do_not_kill_the_worker(ledger, bus, clock):
    def sender(url, title, body, level):
        if "first" in title:
            raise ValueError("发送函数出错")
        return True

    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender, max_attempts=2)
    async with running(n):
        n.raise_alert(AlertLevel.WARNING, "first", "1")
        n.raise_alert(AlertLevel.WARNING, "second", "2")
        await n.flush()
    rows = {r.kind: r for r in await n.recent()}
    assert (rows["first"].delivered, rows["first"].attempts) == (False, 2)  # 异常算失败，重试到上限
    assert (rows["second"].delivered, rows["second"].attempts) == (True, 1)  # 后面的告警不受影响


async def test_backoff_spaces_the_attempts(ledger, bus, clock):
    sender = FakeSender([False, False, True])
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender, backoff_s=(0.06, 0.12))
    async with running(n):
        n.raise_alert(AlertLevel.WARNING, "k", "m")
        await n.flush()
    gaps = [b - a for a, b in zip(sender.times, sender.times[1:])]
    assert len(gaps) == 2 and gaps[0] >= 0.05 and gaps[1] >= 0.10  # 逐次退避
    assert (await n.recent())[0].attempts == 3


async def test_the_last_backoff_value_repeats_when_attempts_outnumber_the_schedule(ledger, bus, clock):
    sender = FakeSender(default=False)
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender, backoff_s=(0.0, 0.05), max_attempts=4)
    async with running(n):
        n.raise_alert(AlertLevel.WARNING, "k", "m")
        await n.flush()
    gaps = [b - a for a, b in zip(sender.times, sender.times[1:])]
    assert len(gaps) == 3 and gaps[1] >= 0.04 and gaps[2] >= 0.04


async def test_a_sender_that_hangs_past_the_timeout_counts_as_a_failed_attempt(ledger, bus, clock):
    release = threading.Event()
    sender = FakeSender(block=release, block_timeout=0.4)
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender, max_attempts=1, send_timeout_s=0.05)
    async with running(n):
        started = time.monotonic()
        n.raise_alert(AlertLevel.WARNING, "k", "m")
        await n.flush()
        assert time.monotonic() - started < 0.35  # 没有等满发送函数的阻塞时间
    release.set()
    row = (await n.recent())[0]
    assert (row.delivered, row.attempts) == (False, 1)


async def test_with_several_addresses_only_the_failed_ones_are_retried(ledger, bus, clock):
    by_url: dict[str, int] = {}

    def sender(url, title, body, level):
        by_url[url] = by_url.get(url, 0) + 1
        return not (url == URL_B and by_url[url] == 1)  # B 第一次失败

    n = build(ledger, bus, clock, urls=(URL_A, URL_B), sender=sender)
    async with running(n):
        n.raise_alert(AlertLevel.CRITICAL, "k", "m")
        await n.flush()
    assert by_url == {URL_A: 1, URL_B: 2}  # 已送达的 A 不重发
    row = (await n.recent())[0]
    assert (row.delivered, row.attempts) == (True, 2)


async def test_with_several_addresses_one_dead_channel_leaves_the_alert_undelivered(ledger, bus, clock):
    def sender(url, title, body, level):
        return url == URL_A

    n = build(ledger, bus, clock, urls=(URL_A, URL_B), sender=sender, max_attempts=2)
    async with running(n):
        n.raise_alert(AlertLevel.CRITICAL, "k", "m")
        await n.flush()
    row = (await n.recent())[0]
    assert (row.delivered, row.attempts) == (False, 2)


# ====================================================================== 去重


async def test_same_dedupe_key_is_recorded_once_per_window(ledger, bus, clock, event_log):
    log = event_log(AlertRaised)
    sender = FakeSender()
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender, dedupe_s=300)
    async with running(n):
        n.raise_alert(AlertLevel.WARNING, "stream", "断了", dedupe_key="stream_down")  # 记
        n.raise_alert(AlertLevel.WARNING, "stream", "又断了", dedupe_key="stream_down")  # 同一时刻：丢
        clock.advance(299_999)
        n.raise_alert(AlertLevel.WARNING, "stream", "还断着", dedupe_key="stream_down")  # 窗口最后 1 毫秒：丢
        await n.flush()
        assert [r.message for r in await n.recent()] == ["断了"]
        clock.advance(1)  # 距第一条正好 300 秒：窗口过了
        n.raise_alert(AlertLevel.WARNING, "stream", "三百秒后", dedupe_key="stream_down")  # 记
        n.raise_alert(AlertLevel.WARNING, "stream", "紧接着", dedupe_key="stream_down")  # 窗口从第二条重新算：丢
        await n.flush()
    rows = list(reversed(await n.recent()))
    assert [r.message for r in rows] == ["断了", "三百秒后"]
    assert [r.at_ms for r in rows] == [START_MS, START_MS + 300_000]
    assert len(await log.settle()) == 2  # 被去重的不发事件
    assert len(sender.calls) == 2  # 也不对外发送


async def test_dedupe_is_per_key_and_ignores_kind_level_and_message(ledger, bus, clock):
    n = build(ledger, bus, clock, dedupe_s=300)
    async with running(n):
        n.raise_alert(AlertLevel.INFO, "a", "x", dedupe_key="k1")
        n.raise_alert(AlertLevel.CRITICAL, "b", "y", dedupe_key="k1")  # 同一个键：丢（级别、内容不同也一样）
        n.raise_alert(AlertLevel.INFO, "a", "x", dedupe_key="k2")  # 另一个键：记
        await n.flush()
    assert sorted(r.kind for r in await n.recent()) == ["a", "a"]
    assert len(await n.recent()) == 2


async def test_alerts_without_a_dedupe_key_are_never_merged(ledger, bus, clock):
    n = build(ledger, bus, clock, dedupe_s=300)
    async with running(n):
        for _ in range(3):
            n.raise_alert(AlertLevel.INFO, "same", "same")
        await n.flush()
    assert len(await n.recent()) == 3


async def test_dedupe_window_of_zero_disables_dedupe(ledger, bus, clock):
    n = build(ledger, bus, clock, dedupe_s=0)
    async with running(n):
        for _ in range(3):
            n.raise_alert(AlertLevel.INFO, "same", "same", dedupe_key="k")
        await n.flush()
    assert len(await n.recent()) == 3


async def test_dedupe_is_decided_when_the_alert_is_raised_not_when_it_is_processed(ledger, bus, clock):
    n = build(ledger, bus, clock, dedupe_s=300)  # 还没 start：入队时就去重
    n.raise_alert(AlertLevel.INFO, "k", "1", dedupe_key="same")
    n.raise_alert(AlertLevel.INFO, "k", "2", dedupe_key="same")
    await n.flush()
    assert [r.message for r in await n.recent()] == ["1"]


async def test_a_clock_that_jumps_back_does_not_suppress_alerts_forever(ledger, bus, clock):
    n = build(ledger, bus, clock, dedupe_s=300)
    async with running(n):
        n.raise_alert(AlertLevel.INFO, "k", "before", dedupe_key="same")
        clock.advance(-3_600_000)  # 时钟回拨一小时
        n.raise_alert(AlertLevel.INFO, "k", "after", dedupe_key="same")
        await n.flush()
    assert [r.message for r in reversed(await n.recent())] == ["before", "after"]


async def test_a_dropped_alert_does_not_extend_the_window(ledger, bus, clock):
    """被去重丢掉的告警不延长窗口：窗口从最后一次「记下」的那条起算。"""
    n = build(ledger, bus, clock, dedupe_s=100)
    async with running(n):
        n.raise_alert(AlertLevel.INFO, "k", "1", dedupe_key="same")  # 第 0 秒：记
        for i in range(5):  # 第 30、60、90 秒：丢；第 120 秒：窗口（100 秒）已过，记；第 150 秒：新窗口内，丢
            clock.advance(30_000)
            n.raise_alert(AlertLevel.INFO, "k", f"dup{i}", dedupe_key="same")
        await n.flush()
    rows = list(reversed(await n.recent()))
    assert [r.message for r in rows] == ["1", "dup3"]
    assert [r.at_ms for r in rows] == [START_MS, START_MS + 120_000]


# ====================================================================== 生命周期


async def test_start_and_stop_are_idempotent_and_restartable(ledger, bus, clock):
    sender = FakeSender()
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender)
    await n.start()
    await n.start()
    n.raise_alert(AlertLevel.INFO, "one", "1")
    await n.flush()
    await n.stop()
    await n.stop()
    await n.start()  # 停了可以再启动（新的发送线程池）
    n.raise_alert(AlertLevel.INFO, "two", "2")
    await n.flush()
    await n.stop()
    assert sorted(r.kind for r in await n.recent()) == ["one", "two"]
    assert len(sender.calls) == 2


async def test_stop_records_everything_that_was_queued(ledger, bus, clock):
    n = build(ledger, bus, clock)
    await n.start()
    for i in range(8):
        n.raise_alert(AlertLevel.WARNING, "k", f"m{i}")
    await n.stop()  # 不先 flush：停的时候队列里的告警也要落账
    assert len(await n.recent()) == 8


async def test_stop_does_not_hang_on_a_stuck_sender_or_pending_retries(ledger, bus, clock):
    release = threading.Event()
    sender = FakeSender(block=release, block_timeout=10)
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender, backoff_s=(30.0,), stop_grace_s=0.05)
    await n.start()
    try:
        n.raise_alert(AlertLevel.CRITICAL, "k", "m")
        await wait_until(lambda: len(sender.calls) == 1)
        started = time.monotonic()
        await asyncio.wait_for(n.stop(), timeout=3)
        assert time.monotonic() - started < 2
    finally:
        release.set()
    row = (await n.recent())[0]
    assert row.kind == "k" and row.delivered is False  # 本地已留痕


async def test_stop_does_not_wait_out_a_long_backoff(ledger, bus, clock):
    sender = FakeSender(default=False)
    n = build(ledger, bus, clock, urls=(URL_A,), sender=sender, backoff_s=(60.0,), stop_grace_s=0.5)
    await n.start()
    n.raise_alert(AlertLevel.CRITICAL, "k", "m")
    await wait_row(n, lambda r: r.attempts == 1)  # 第一次失败已记下，正在 60 秒的退避里
    started = time.monotonic()
    await asyncio.wait_for(n.stop(), timeout=3)
    assert time.monotonic() - started < 0.4  # 退避被叫停，而不是睡满 60 秒（也不必等满宽限时间）
    assert len(sender.calls) == 1
    row = (await n.recent())[0]
    assert (row.attempts, row.delivered) == (1, False)


async def test_after_stop_raise_alert_still_logs_but_cannot_record(ledger, bus, clock, caplog):
    caplog.set_level(logging.DEBUG, logger=NOTIFIER_LOGGER)
    n = build(ledger, bus, clock)
    await n.start()
    await n.stop()
    assert n.raise_alert(AlertLevel.CRITICAL, "late", "停止之后的告警") is None
    assert "late" in caplog.text and "停止之后的告警" in caplog.text
    assert await n.recent() == []


async def test_flush_works_on_a_notifier_that_was_never_started(ledger, bus, clock, event_log):
    log = event_log(AlertRaised)
    n = build(ledger, bus, clock)
    n.raise_alert(AlertLevel.INFO, "a", "1")
    n.raise_alert(AlertLevel.INFO, "b", "2")
    await n.flush()
    assert [r.kind for r in reversed(await n.recent())] == ["a", "b"]
    assert len(await log.settle()) == 2


# ====================================================================== 不泄露渠道地址


async def test_channel_addresses_never_appear_in_logs_or_repr(ledger, bus, clock, caplog):
    caplog.set_level(logging.DEBUG)
    sender = FakeSender([False, RuntimeError(f"无法连接 {URL_A}"), True, False, True])
    n = build(ledger, bus, clock, urls=(URL_A, URL_B), sender=sender)
    async with running(n):
        n.raise_alert(AlertLevel.CRITICAL, "k", "m")
        await n.flush()
    assert "token-AAA-secret" not in caplog.text and "token-BBB-secret" not in caplog.text
    assert "fake://" not in caplog.text  # 地址的任何部分都不进日志
    assert "token-AAA-secret" not in repr(n) and "alpha" not in repr(n)
    row = (await n.recent())[0]
    assert "secret" not in row.message and "secret" not in row.kind


# ====================================================================== Apprise 默认发送函数（不联网）


def test_the_default_sender_fails_cleanly_for_an_unusable_address():
    assert apprise_send("notaurl://nowhere", "标题", "内容", AlertLevel.INFO) is False
    assert apprise_send("", "标题", "内容", AlertLevel.CRITICAL) is False


def test_every_alert_level_has_an_apprise_notify_type():
    assert set(notifier_module.NOTIFY_TYPES) == set(AlertLevel)
    assert notifier_module.NOTIFY_TYPES[AlertLevel.CRITICAL] == "failure"


async def test_the_default_sender_is_apprise_when_none_is_given(ledger, bus, clock, monkeypatch):
    seen: list[tuple] = []

    def fake_apprise(url, title, body, level):
        seen.append((url, level))
        return True

    monkeypatch.setattr(notifier_module, "apprise_send", fake_apprise)
    n = build(ledger, bus, clock, urls=(URL_A,))  # 不给 sender
    async with running(n):
        n.raise_alert(AlertLevel.WARNING, "k", "m")
        await n.flush()
    assert seen == [(URL_A, AlertLevel.WARNING)]
