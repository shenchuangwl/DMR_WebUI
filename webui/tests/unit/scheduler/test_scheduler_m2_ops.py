"""M2 操作服务的装配（M2I）：一键全平与切换（``ops.close_all.CloseAll``、``ops.switch.Switch``）接进内核。

- 构造时：``kernel.close_all`` / ``kernel.switch`` 用真实的意图服务、总线、崩溃点；控制接口从 ``app.state.bulk_ops`` 取同一对；
  它们与 M2A 的各入口共用同一套 ``OpsDeps``（``app.state.ops_services.deps``）。
- 启动时：``ops.close_all.install(kernel.close_all)``（``/ops/stop`` 带 close_all 用），``switch.start()``（订阅席位变化、
  清理到期预览）；关停时撤下。
- 定时作业 ``close_all_tick``：恢复返回之后注册，每 2 秒调一次 ``close_all.tick()``（单实例、合并错过的触发）。
"""
from __future__ import annotations

from datetime import timedelta

from dmr_kernel.core import scheduler
from dmr_kernel.ops import close_all as close_all_mod
from dmr_kernel.ops.close_all import CloseAll
from dmr_kernel.ops.switch import BulkOps, Switch
from sched_kit import spy_async
from tests.support.kernel import FakeRecovery, make_kernel, started_kernel


async def test_build_creates_close_all_and_switch_with_the_kernels_own_parts(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        assert isinstance(kernel.close_all, CloseAll) and isinstance(kernel.switch, Switch)
        bulk = kernel.app.state.bulk_ops
        assert isinstance(bulk, BulkOps)
        assert bulk.close_all is kernel.close_all and bulk.switch is kernel.switch
        assert kernel.close_all._intents is kernel.intents and kernel.switch._intents is kernel.intents
        assert kernel.close_all._bus is kernel.bus and kernel.switch._bus is kernel.bus
        assert kernel.close_all._crash is kernel.crash and kernel.switch._crash is kernel.crash
        assert kernel.switch._close_all is kernel.close_all
    finally:
        await kernel.stop()


async def test_bulk_operations_share_the_ops_deps_of_the_other_entries(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        services = kernel.app.state.ops_services
        deps = services.deps
        assert kernel.close_all._deps is deps and kernel.switch._deps is deps
        assert services.startstop._deps is deps
        assert deps.coordinator is kernel.coordinator and deps.registry is kernel.registry
        assert deps.modelstate is kernel.modelstate and deps.orders is kernel.orders
        assert deps.recover is not None and deps.last_recovery is not None
    finally:
        await kernel.stop()


async def test_start_installs_close_all_for_stop_with_close_all_and_stop_takes_it_back(tmp_path):
    close_all_mod.install(None)
    async with started_kernel(tmp_path) as (kernel, _):
        assert close_all_mod._installed is kernel.close_all
    assert close_all_mod._installed is None


async def test_stopping_an_older_kernel_does_not_take_back_a_newer_ones_close_all(tmp_path):
    old, _ = make_kernel(tmp_path / "a", recovery_factory=FakeRecovery())
    new, _ = make_kernel(tmp_path / "b", recovery_factory=FakeRecovery())
    try:
        await old.start(serve_http=False, run_jobs=False)
        await new.start(serve_http=False, run_jobs=False)
        assert close_all_mod._installed is new.close_all
        await old.stop()
        assert close_all_mod._installed is new.close_all
    finally:
        await old.stop()
        await new.stop()


async def test_start_starts_the_switch_and_stop_stops_it(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        assert kernel.switch._sub is not None and kernel.switch._sweeper is not None
        switch = kernel.switch
    assert switch._sub is None and switch._sweeper is None


async def test_the_close_all_tick_job_calls_close_all_tick(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        assert "close_all_tick" in scheduler.JOB_NAMES and "close_all_tick" in kernel.jobs
        calls = spy_async(kernel.close_all, "tick")
        await kernel.jobs["close_all_tick"]()
        assert len(calls) == 1


async def test_the_close_all_tick_job_is_registered_after_recovery_every_two_seconds(tmp_path):
    seen: dict = {}

    async def during_recovery(kwargs) -> None:
        seen["jobs"] = {job.id for job in seen["kernel"].scheduler.get_jobs()}

    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery(on_run=during_recovery))
    seen["kernel"] = kernel
    try:
        await kernel.start(serve_http=False, run_jobs=True)
        assert "close_all_tick" not in seen["jobs"]
        job = kernel.scheduler.get_job("close_all_tick")
        assert job is not None
        assert job.trigger.interval == timedelta(seconds=scheduler.CLOSE_ALL_TICK_S) == timedelta(seconds=2)
        assert job.max_instances == 1 and job.coalesce is True
    finally:
        await kernel.stop()
