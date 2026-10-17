"""名单同步的装配（M4，T3.1）：生产路径造真正的 ListSync；接入后多出 ``list_poll`` 与一次性的 ``list_poll_first`` 作业；
自检里的「名单有效」、K 线监控集合里的名单成员都来自它；关停时关掉它的长连接。测试一律用替身，不联网。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apscheduler.triggers.date import DateTrigger

from dmr_kernel.core.scheduler import build_kernel
from dmr_kernel.core.types import PositionSide
from dmr_kernel.listsync import ListMember, ListSync, ListView
from sched_kit import JOB_NAMES, spy_async
from tests.support.kernel import FakeRecovery, make_kernel, make_settings, started_kernel

UTC = timezone.utc
LIST_JOBS = {"list_poll", "list_poll_first"}


class FakeListSync:
    """满足 ListSync 接口的替身：``poll`` 记次数（可以让它抛错），``current`` 返回事先放好的视图。"""

    def __init__(self, view: ListView | None = None, *, fail: bool = False) -> None:
        self.view = view or make_view(valid=False, reason="还没有拉取过")
        self.fail = fail
        self.polls = 0
        self.closed = 0

    async def poll(self) -> ListView:
        self.polls += 1
        if self.fail:
            raise RuntimeError("名单接口炸了")
        return self.view

    def current(self) -> ListView:
        return self.view

    def member(self, symbol: str) -> ListMember | None:
        return next((m for m in self.view.members if m.symbol == symbol), None)

    async def close(self) -> None:
        self.closed += 1


def make_view(*, valid: bool, reason: str = "", members: tuple[ListMember, ...] = (),
              valid_until_ms: int | None = None) -> ListView:
    return ListView(valid=valid, reason=reason, snapshot_id=1 if valid else None, fetched_ms=None,
                    valid_until_ms=valid_until_ms, business_date=None, status=None, members=members, rejected=())


def member(symbol: str, side: PositionSide = PositionSide.LONG) -> ListMember:
    return ListMember(symbol=symbol, direction=side, score=Decimal("1"), first_selected_ms=None, raw_code=symbol)


def jobs_of(kernel) -> dict:
    return {job.id: job for job in kernel.scheduler.get_jobs()}


# ---------------------------------------------------------------- 生产装配


async def test_the_production_build_creates_the_real_list_sync_on_the_configured_url(tmp_path):
    settings = make_settings(tmp_path)
    settings.standin_token_path.parent.mkdir(parents=True)
    settings.standin_token_path.write_text("t\n", encoding="utf-8")
    kernel = build_kernel(settings, recovery_factory=FakeRecovery())  # 没有传 transport：生产路径
    try:
        assert isinstance(kernel.listsync, ListSync)
        assert kernel.listsync._url == settings.list_url  # 只拉配置的那一个地址（不带参数）
        assert "list_poll" in kernel.jobs
    finally:
        await kernel.stop()


async def test_a_test_build_has_no_list_sync_and_no_list_job(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        assert kernel.listsync is None and "list_poll" not in kernel.jobs
        assert kernel.list_valid() is False
    finally:
        await kernel.stop()


# ---------------------------------------------------------------- 作业


async def test_attaching_a_list_sync_adds_the_list_poll_job(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        fake = FakeListSync()
        kernel.set_listsync(fake)
        assert sorted(kernel.jobs) == sorted({*JOB_NAMES, "list_poll"})
        await kernel.jobs["list_poll"]()
        assert fake.polls == 1
    finally:
        await kernel.stop()


async def test_the_scheduler_registers_the_poll_at_the_configured_interval_and_one_early_first_poll(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery(), config_overrides={"list_poll_s": 30})
    fake = FakeListSync()
    kernel.set_listsync(fake)
    try:
        await kernel.start(serve_http=False, run_jobs=True)
        jobs = jobs_of(kernel)
        assert set(jobs) == set(JOB_NAMES) | LIST_JOBS
        assert jobs["list_poll"].trigger.interval == timedelta(seconds=30)
        first = jobs["list_poll_first"]
        assert isinstance(first.trigger, DateTrigger)  # 启动后马上拉一次，不等一个间隔
        assert first.next_run_time - datetime.now(UTC) <= timedelta(seconds=2)
        for job in (jobs["list_poll"], first):
            assert job.max_instances == 1 and job.coalesce is True and job.misfire_grace_time is None
    finally:
        await kernel.stop()


async def test_a_failing_poll_is_logged_and_does_not_escape_the_job(tmp_path, caplog):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        fake = FakeListSync(fail=True)
        kernel.set_listsync(fake)
        with caplog.at_level(logging.ERROR, logger="dmr_kernel.core.scheduler"):
            await kernel.jobs["list_poll"]()
        assert fake.polls == 1
        assert any("名单" in r.getMessage() for r in caplog.records)
    finally:
        await kernel.stop()


# ---------------------------------------------------------------- 名单有效、监控集合、关停


async def test_list_valid_follows_the_current_view_and_reaches_the_start_check(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        fake = FakeListSync(make_view(valid=False, reason="HTTP 503"))
        kernel.set_listsync(fake)
        assert kernel.list_valid() is False
        fake.view = make_view(valid=True)  # 有效但为空也算有效
        assert kernel.list_valid() is True
        assert kernel.app.state.ops_services.deps.list_valid() is True  # 启动自检读的就是它
    finally:
        await kernel.stop()


async def test_the_monitored_symbols_include_the_members_of_a_valid_list(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        far = kernel.clock.now_ms() + 3_600_000
        fake = FakeListSync(make_view(valid=True, valid_until_ms=far,
                                      members=(member("ETHUSDT"), member("SOLUSDT", PositionSide.SHORT))))
        kernel.set_listsync(fake)
        assert {"ETHUSDT", "SOLUSDT"} <= set(await kernel.monitored_symbols())
        fake.view = make_view(valid=False, reason="已过期")
        assert not {"ETHUSDT", "SOLUSDT"} & set(await kernel.monitored_symbols())


async def test_stop_closes_the_list_sync_after_the_jobs_are_quiet(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    fake = FakeListSync()
    kernel.set_listsync(fake)
    await kernel.start(serve_http=False, run_jobs=True)
    order: list[str] = []
    spy_async(kernel.stream, "stop", order=order, label="stream.stop")
    spy_async(fake, "close", order=order, label="listsync.close")
    spy_async(kernel.ledger, "close", order=order, label="ledger.close")
    await kernel.stop()
    assert fake.closed == 1  # 长连接关掉，不留未关闭的会话
    firsts = [x for i, x in enumerate(order) if x not in order[:i]]
    assert firsts.index("stream.stop") < firsts.index("listsync.close") < firsts.index("ledger.close")
