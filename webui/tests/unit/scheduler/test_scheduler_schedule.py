"""定时作业的注册（APScheduler，UTC）：作业齐全、间隔、单实例与合并、UTC 对齐、恢复前后的注册时序。

调度器用的是真实时间（APScheduler 读系统时钟），所以这里只检查「注册成什么样」，并让调度器真的触发一次来证明
注册进去的就是 kernel.jobs 里的那些函数；业务时间的推进用 ManualClock，见 test_scheduler_jobs.py。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from dmr_kernel.alerts.heartbeat import Heartbeat
from dmr_kernel.core.clock import interval_ms
from sched_kit import JOB_NAMES, spy_async
from tests.support.kernel import FakeRecovery, make_kernel, settle, started_kernel

UTC = timezone.utc
INTERVAL_S = {
    "stream_liveness": 5, "resolve_unknown": 5, "expire_intents": 5, "reconcile_positions": 10, "reconcile_orders": 60,
    "backfill_trades": 60, "backfill_income": 3600, "guard_periodic": 60, "server_time": 60, "rules_refresh": 3600,
    "heartbeat": 30, "seat_expiry": 5, "protection_tick": 2, "ledger_backup": 3600, "ledger_checkpoint": 3600,
    "close_all_tick": 2,
}
BAR_JOBS = {"bar_close_short": "15m", "bar_close_long": "1h"}


def jobs_of(kernel) -> dict:
    return {job.id: job for job in kernel.scheduler.get_jobs()}


def delay_ms(kernel) -> int:
    return int(kernel.config.get("kline_delay_s")) * 1000


def assert_aligned(job, timeframe: str, delay: int) -> None:
    """下一次触发 = UTC 周期边界 + 延迟（纪元对齐），且在将来。"""
    fire_ms = round(job.next_run_time.timestamp() * 1000)
    assert (fire_ms - delay) % interval_ms(timeframe) == 0, (job.id, job.next_run_time)
    assert job.next_run_time > datetime.now(UTC)
    assert job.next_run_time - datetime.now(UTC) <= timedelta(milliseconds=interval_ms(timeframe) + delay)


# ---------------------------------------------------------------- 注册了什么


async def test_all_eighteen_jobs_are_registered_after_the_start(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, _):
        assert set(jobs_of(kernel)) == set(JOB_NAMES)


async def test_no_scheduler_exists_when_the_jobs_are_not_run(tmp_path):
    async with started_kernel(tmp_path, run_jobs=False) as (kernel, _):
        assert kernel.scheduler is None


async def test_every_job_is_single_instance_and_merges_missed_runs(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, _):
        for name, job in jobs_of(kernel).items():
            assert job.max_instances == 1, name
            assert job.coalesce is True, name
            assert job.misfire_grace_time is None, name  # 晚了也要跑（合并成一次），不能因为循环忙了一秒就丢
        assert kernel.scheduler.timezone == UTC


async def test_the_intervals_are_the_ones_of_the_task_table(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, _):
        jobs = jobs_of(kernel)
        for name, seconds in INTERVAL_S.items():
            assert jobs[name].trigger.interval == timedelta(seconds=seconds), name


async def test_the_intervals_that_depend_on_the_configuration_follow_it(tmp_path):
    overrides = {"reconcile_positions_s": 20, "reconcile_orders_s": 120, "heartbeat_s": 15}
    async with started_kernel(tmp_path, run_jobs=True, config_overrides=overrides) as (kernel, _):
        jobs = jobs_of(kernel)
        assert jobs["reconcile_positions"].trigger.interval == timedelta(seconds=20)
        assert jobs["reconcile_orders"].trigger.interval == timedelta(seconds=120)
        assert jobs["heartbeat"].trigger.interval == timedelta(seconds=15)


async def test_the_stream_liveness_job_is_not_registered_when_the_stream_is_disabled(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True, config_overrides={"ex_stream_enabled": False}) as (kernel, _):
        assert set(jobs_of(kernel)) == set(JOB_NAMES) - {"stream_liveness"}


# ---------------------------------------------------------------- UTC 对齐（本机是 +07）


async def test_the_bar_jobs_fire_at_utc_boundaries_plus_the_delay(tmp_path, bangkok):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, _):
        jobs = jobs_of(kernel)
        for name, timeframe in BAR_JOBS.items():
            assert_aligned(jobs[name], timeframe, 3000)


async def test_the_bar_jobs_follow_the_configured_timeframes_and_delay(tmp_path, bangkok):
    overrides = {"timeframe_short": "5m", "timeframe_long": "4h", "kline_delay_s": 20}
    async with started_kernel(tmp_path, run_jobs=True, config_overrides=overrides) as (kernel, _):
        jobs = jobs_of(kernel)
        assert_aligned(jobs["bar_close_short"], "5m", 20_000)
        assert_aligned(jobs["bar_close_long"], "4h", 20_000)


async def test_all_next_run_times_are_in_utc_even_when_the_local_zone_is_plus_seven(tmp_path, bangkok):
    assert datetime.now().astimezone().utcoffset() == timedelta(hours=7)
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, _):
        for name, job in jobs_of(kernel).items():
            assert job.next_run_time.utcoffset() == timedelta(0), name


async def test_changing_the_kline_delay_reschedules_the_bar_jobs_at_once(tmp_path, bangkok):
    """kline_delay_s 是「立即生效」的配置项：下一根 K 线就按新的延迟取数，不用等重启。"""
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, _):
        await kernel.config.set("kline_delay_s", 9, operator="test")
        await settle(kernel)
        jobs = jobs_of(kernel)
        for name, timeframe in BAR_JOBS.items():
            assert_aligned(jobs[name], timeframe, 9000)


async def test_changing_something_else_leaves_the_bar_jobs_alone(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, _):
        before = {name: jobs_of(kernel)[name].trigger for name in BAR_JOBS}
        await kernel.config.set("leverage", 3, operator="test")
        await settle(kernel)
        for name in BAR_JOBS:
            assert jobs_of(kernel)[name].trigger is before[name]


# ---------------------------------------------------------------- 时序：心跳与判活先于恢复，其余在恢复之后


async def test_heartbeat_and_stream_liveness_start_before_the_recovery_and_the_rest_after_it(tmp_path):
    seen: dict = {}

    async def during_recovery(kwargs) -> None:
        kernel = seen["kernel"]
        seen["jobs"] = set(jobs_of(kernel))
        seen["running"] = kernel.scheduler.running
        seen["beat"] = Heartbeat.read(kernel.heartbeat.path)

    recovery = FakeRecovery(on_run=during_recovery)
    kernel, _ = make_kernel(tmp_path, recovery_factory=recovery)
    seen["kernel"] = kernel
    try:
        await kernel.start(serve_http=False, run_jobs=True)
        assert seen["jobs"] == {"heartbeat", "stream_liveness"}  # 恢复进行中只有这两个
        assert seen["running"] is True
        assert seen["beat"] is not None  # 第一次心跳在恢复之前就写了
        assert set(jobs_of(kernel)) == set(JOB_NAMES)  # 恢复返回之后其余全部注册
    finally:
        await kernel.stop()


async def test_without_the_stream_only_the_heartbeat_runs_during_the_recovery(tmp_path):
    seen: dict = {}

    async def during_recovery(kwargs) -> None:
        seen["jobs"] = set(jobs_of(seen["kernel"]))

    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery(on_run=during_recovery),
                            config_overrides={"ex_stream_enabled": False})
    seen["kernel"] = kernel
    try:
        await kernel.start(serve_http=False, run_jobs=True)
        assert seen["jobs"] == {"heartbeat"}
    finally:
        await kernel.stop()


async def test_a_failed_recovery_never_registers_the_rest_of_the_jobs(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery(error=RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        await kernel.start(serve_http=False, run_jobs=True)
    try:
        assert set(jobs_of(kernel)) == {"heartbeat", "stream_liveness"}
    finally:
        await kernel.stop()


# ---------------------------------------------------------------- 调度器真的在调这些函数


async def test_the_scheduler_really_invokes_the_registered_jobs(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, _):
        calls = spy_async(kernel.protection, "tick")
        kernel.scheduler.modify_job("protection_tick", next_run_time=datetime.now(UTC))
        for _ in range(60):
            if calls:
                break
            await asyncio.sleep(0.05)
        assert len(calls) >= 1


async def test_the_scheduler_runs_whatever_kernel_jobs_holds_when_it_fires_and_tracks_it_while_it_runs(tmp_path):
    """故障注入的测试可以在内核起来之后替换 ``kernel.jobs[名字]``：调度器按名字在触发时才取；在途的作业记在 ``_inflight``
    里（关停时据此等它们收尾），做完就忘掉。"""
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, _):
        entered, release = asyncio.Event(), asyncio.Event()
        seen: dict = {}

        async def replacement() -> None:
            seen["task"] = asyncio.current_task()
            seen["tracked_while_running"] = seen["task"] in kernel._inflight
            entered.set()
            await release.wait()

        kernel.jobs["seat_expiry"] = replacement  # 注册之后才换
        kernel.scheduler.modify_job("seat_expiry", next_run_time=datetime.now(UTC))
        await asyncio.wait_for(entered.wait(), 5)
        assert seen["tracked_while_running"] is True
        release.set()
        for _ in range(100):
            if seen["task"] not in kernel._inflight:
                break
            await asyncio.sleep(0.02)
        assert seen["task"] not in kernel._inflight


async def test_the_scheduler_does_not_log_every_single_run_but_still_logs_errors(tmp_path):
    """每次触发两行 INFO 会把内核日志灌满；只留 WARNING 以上（作业出错是 ERROR，照常记）。"""
    async with started_kernel(tmp_path, run_jobs=True):
        logger = logging.getLogger("apscheduler.executors.default")
        assert not logger.isEnabledFor(logging.INFO)
        assert logger.isEnabledFor(logging.WARNING) and logger.isEnabledFor(logging.ERROR)


async def test_the_scheduler_is_shut_down_by_stop(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=True)
    assert kernel.scheduler.running
    await kernel.stop()
    assert not kernel.scheduler.running
