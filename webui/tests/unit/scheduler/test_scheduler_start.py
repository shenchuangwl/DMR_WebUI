"""Kernel.start：启动顺序、传给恢复的组件、恢复失败与不可交易时的处理、重复启动。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from dmr_kernel.core.events import StreamStatusChanged
from dmr_kernel.core.types import Gate
from tests.support.kernel import FakeRecovery, RecoveryStub, make_kernel, started_kernel
from sched_kit import collect

# 规格 3.3：Recovery.__init__ 的关键字参数（rules 不在其中：合约清单由装配在恢复之前刷新）
RECOVERY_KWARGS = {
    "settings", "adapter", "stream", "ledger", "bus", "clock", "config_get", "modelstate", "registry", "intents",
    "orders", "ingest", "coordinator", "reconciler", "protection", "guard", "candles", "operations", "notifier",
}


async def test_start_builds_the_recovery_once_from_the_assembled_components_and_runs_it(tmp_path):
    recovery = FakeRecovery()
    async with started_kernel(tmp_path, recovery_factory=recovery) as (kernel, _):
        assert recovery.runs == 1 and len(recovery.constructed) == 1
        kwargs = recovery.constructed[0]
        assert set(kwargs) == RECOVERY_KWARGS
        for name in RECOVERY_KWARGS - {"config_get", "stream"}:
            assert kwargs[name] is getattr(kernel, name), name
        assert kwargs["config_get"] == kernel.config.get
        assert kwargs["stream"] is kernel.stream is not None
        # 写权限令牌只在协调者与适配层手里，不会经恢复流出去
        capability = kernel.adapter._capability
        assert not any(value is capability for value in kwargs.values())
        assert kernel.recovery_result is recovery.result


async def test_everything_the_recovery_needs_is_loaded_and_running_before_it_runs(tmp_path):
    seen: dict = {}

    async def inspect(kwargs):
        kernel = seen["kernel"]
        seen["leverage"] = kernel.config.get("leverage")  # 配置已加载
        seen["gate"] = kernel.modelstate.current().gate  # 模型状态已加载
        seen["rules"] = kernel.rules.get("BTCUSDT")  # 合约清单已刷新（恢复要用它算数量和价格）
        seen["rules_ms"] = kernel.rules.refreshed_ms
        seen["now"] = kernel.clock.now_ms()
        events = seen["events"]
        kernel.bus.publish(StreamStatusChanged(state="connected", reason="test", at_ms=0))
        await kernel.bus.drain(5)  # 总线已经启动
        seen["bus_delivered"] = len(events)

    recovery = FakeRecovery(on_run=inspect)
    kernel, _ = make_kernel(tmp_path, recovery_factory=recovery)
    seen["kernel"] = kernel
    seen["events"] = collect(kernel, StreamStatusChanged)
    try:
        await kernel.start(serve_http=False, run_jobs=False)
    finally:
        await kernel.stop()
    assert seen["leverage"] == 2
    assert seen["gate"] is Gate.STOPPED
    assert seen["rules"] is not None and seen["rules_ms"] == seen["now"]
    assert seen["bus_delivered"] == 1


async def test_the_guard_subscribes_before_the_recovery_so_no_stream_event_is_missed(tmp_path):
    """恢复里会立刻起账户数据流；守护要在那之前订阅，否则首次建流失败的「中断」事件会漏掉。"""
    seen: dict = {}

    async def inspect(kwargs):
        kernel = seen["kernel"]
        seen["subscribed"] = kernel.guard._subscription is not None

    recovery = FakeRecovery(on_run=inspect)
    kernel, _ = make_kernel(tmp_path, recovery_factory=recovery)
    seen["kernel"] = kernel
    try:
        await kernel.start(serve_http=False, run_jobs=False)
    finally:
        await kernel.stop()
    assert seen["subscribed"] is True


async def test_what_the_guards_startup_check_read_is_noted_for_the_api_right_after_the_recovery(tmp_path):
    """恢复第 1 步（守护的启动自检）已经读过持仓模式与交易所限制；/state 不用等 60 秒后的第一次周期复核才有这两项。"""
    seen: dict = {}

    async def like_the_real_recovery(kwargs) -> None:
        await seen["kernel"].guard.startup()

    live_before: dict = {}
    kernel, standin = make_kernel(tmp_path, recovery_factory=FakeRecovery(on_run=like_the_real_recovery))
    seen["kernel"] = kernel
    live = kernel.app.state.services.live
    try:
        live_before["mode"], live_before["restrictions"] = live.position_mode, live.restrictions
        await kernel.start(serve_http=False, run_jobs=False)
        assert live_before == {"mode": None, "restrictions": None}
        assert live.position_mode.dual_side is True and live.position_mode.at_ms == standin.clock.now_ms()
        assert live.restrictions.restricted is False
    finally:
        await kernel.stop()


async def test_a_recovery_that_never_asked_the_guard_leaves_those_two_unknown(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        live = kernel.app.state.services.live
        assert live.position_mode is None and live.restrictions is None


async def test_the_stream_is_none_for_the_recovery_when_the_stream_is_disabled(tmp_path):
    recovery = FakeRecovery()
    async with started_kernel(tmp_path, recovery_factory=recovery,
                              config_overrides={"ex_stream_enabled": False}) as (kernel, standin):
        assert kernel.stream is None
        assert recovery.constructed[0]["stream"] is None
        assert standin.transport.ws_paths == []  # 内核自己从不起数据流（那是恢复的事）


async def test_the_kernel_itself_never_starts_the_account_stream(tmp_path):
    """数据流由恢复在第 2 步启动（置「恢复中」、暂缓应用成交之后）；装配不抢跑。"""
    async with started_kernel(tmp_path) as (kernel, standin):
        assert kernel.stream.state == "stopped"
        assert standin.transport.ws_paths == []


async def test_a_second_start_is_refused(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        with pytest.raises(RuntimeError, match="已经启动"):
            await kernel.start(serve_http=False, run_jobs=False)


async def test_a_kernel_cannot_be_started_after_it_was_stopped(tmp_path):
    """协调者是一次性对象：停止后要重新装配一个内核，不是重启这个。"""
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=False)
    await kernel.stop()
    with pytest.raises(RuntimeError):
        await kernel.start(serve_http=False, run_jobs=False)


# ---------------------------------------------------------------- 恢复的结果


async def test_a_failing_recovery_aborts_the_start_and_stop_still_cleans_up(tmp_path):
    recovery = FakeRecovery(error=RuntimeError("boom"))
    kernel, _ = make_kernel(tmp_path, recovery_factory=recovery)
    with pytest.raises(RuntimeError, match="boom"):
        await kernel.start(serve_http=False, run_jobs=True)
    assert kernel.recovery_result is None
    await kernel.stop()  # 启动失败后由调用方 stop() 收尾：恢复之前就起来的调度器要关掉
    assert kernel.ledger.closed
    assert kernel.scheduler.running is False


async def test_a_recovery_that_finds_problems_does_not_stop_the_kernel(tmp_path, caplog):
    """不可交易 = 恢复自己保持抑制并告警；内核继续运行（对账、心跳、止损管理都要照常）。"""
    result = RecoveryStub(tradable=False, problems=("持仓模式不是对冲",))
    recovery = FakeRecovery(result=result)
    with caplog.at_level(logging.WARNING, logger="dmr_kernel.core.scheduler"):
        async with started_kernel(tmp_path, recovery_factory=recovery, run_jobs=True) as (kernel, _):
            assert kernel.recovery_result is result
            assert {job.id for job in kernel.scheduler.get_jobs()} >= {"reconcile_positions", "protection_tick"}
    assert any("持仓模式不是对冲" in record.getMessage() for record in caplog.records)


async def test_the_instrument_list_is_refreshed_again_soon_when_the_first_refresh_fails(tmp_path):
    """启动时交易所读不到合约清单：不能干等一小时，30 秒后再试（只在有调度器时）。"""
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    attempts: list = []

    async def failing(adapter) -> bool:
        attempts.append(adapter)
        return False

    kernel.rules.refresh = failing
    try:
        await kernel.start(serve_http=False, run_jobs=True)
        assert attempts == [kernel.adapter]  # 启动时刷新过一次，失败了也照常启动
        retry = kernel.scheduler.get_job("rules_refresh_retry")
        assert retry is not None
        wait_s = (retry.next_run_time - datetime.now(timezone.utc)).total_seconds()
        assert 0 < wait_s <= 30.5
    finally:
        await kernel.stop()


async def test_without_a_scheduler_a_failed_first_refresh_schedules_nothing(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())

    async def failing(adapter) -> bool:
        return False

    kernel.rules.refresh = failing
    try:
        await kernel.start(serve_http=False, run_jobs=False)
        assert kernel.scheduler is None
    finally:
        await kernel.stop()
