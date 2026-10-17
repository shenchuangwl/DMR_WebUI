"""关停（报告 4.4）：停止接收新指令 → 等在途意图 → 关数据流 → 关交易所连接 → 检查点后关库；交易所侧的保护单不撤。"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal

from apscheduler.schedulers.base import STATE_PAUSED

from dmr_kernel.alerts.heartbeat import Heartbeat
from dmr_kernel.core import scheduler
from sched_kit import spy_async
from tests.support.kernel import FakeRecovery, api_client, make_kernel, started_kernel

D = Decimal


def first_occurrences(items: list[str]) -> list[str]:
    return [item for index, item in enumerate(items) if item not in items[:index]]


async def test_the_shutdown_steps_run_in_the_agreed_order(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery(), config_overrides={"shutdown_drain_s": 3})
    await kernel.start(serve_http=False, run_jobs=True)
    order: list[str] = []
    seen: dict = {}

    def at_protection_stop() -> None:
        seen["accepting"] = kernel.app.state.accepting
        seen["scheduler_state"] = kernel.scheduler.state

    spy_async(kernel.protection, "stop", order=order, label="protection.stop", before=at_protection_stop)
    drain_calls = spy_async(kernel.coordinator, "stop", order=order, label="coordinator.stop")
    spy_async(kernel.stream, "stop", order=order, label="stream.stop")
    spy_async(kernel.guard, "stop", order=order, label="guard.stop")
    spy_async(kernel.adapter, "close", order=order, label="adapter.close")
    spy_async(kernel.notifier, "stop", order=order, label="notifier.stop")
    spy_async(kernel.ledger, "close", order=order, label="ledger.close")
    release = kernel.legacy.release_all

    def recording_release() -> None:
        order.append("legacy.release_all")
        release()

    kernel.legacy.release_all = recording_release
    await kernel.stop()

    # 适配层的 close 里会再停一次数据流；只看每一步第一次出现的位置
    assert first_occurrences(order) == [
        "protection.stop", "coordinator.stop", "stream.stop", "guard.stop", "adapter.close", "notifier.stop",
        "ledger.close", "legacy.release_all"]
    # 第一步：控制接口已不再受理改变状态的请求、调度器已不再触发新作业，然后才停保护单管理与协调者
    assert seen == {"accepting": False, "scheduler_state": STATE_PAUSED}
    args, kwargs = drain_calls[0]
    assert (kwargs.get("drain_s") if "drain_s" in kwargs else args[0]) == 3  # 等在途意图的时限取配置 shutdown_drain_s
    assert kernel.ledger.closed and not kernel.scheduler.running


async def test_stop_can_be_called_again_and_concurrently(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=True)
    closes = spy_async(kernel.ledger, "close")
    await asyncio.gather(kernel.stop(), kernel.stop())
    await kernel.stop()
    assert len(closes) == 1 and kernel.ledger.closed


async def test_stopping_a_kernel_that_was_never_started_closes_its_ledger(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.stop()
    assert kernel.ledger.closed


async def test_the_exchange_side_protective_orders_are_left_alone(tmp_path):
    """交易所侧的保护单不撤：内核关停不向交易所发任何写请求。"""
    async with started_kernel(tmp_path) as (kernel, standin):
        engine = standin.engine
        engine.place_market_order(symbol="BTCUSDT", side="BUY", position_side="LONG", qty=D("0.01"))
        engine.place_algo_order(symbol="BTCUSDT", side="SELL", position_side="LONG", algo_type="STOP_MARKET",
                                trigger_price=D("45000"), close_position=True,
                                client_algo_id=f"DK{kernel.settings.instance_id}{kernel.ledger.nonce}-1-1-s")
        await kernel.jobs["reconcile_orders"]()
        assert len(engine.open_algo_orders()) == 1
        mark = len(standin.transport.requests)
    after = standin.transport.requests[mark:]
    assert [r for r in after if r[0] in ("POST", "PUT", "DELETE")] == []
    assert len(engine.open_algo_orders()) == 1  # 保护单还在交易所


async def test_the_api_stops_accepting_state_changes_first_but_keeps_answering_reads(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        token = kernel.app.state.ctx.local_token
        async with api_client(kernel, token) as client:
            put = {"op_id": "t-1", "value": 3}
            assert (await client.put("/api/v1/config/leverage", json=put)).status_code == 200
            kernel.stop_accepting()
            refused = await client.put("/api/v1/config/leverage", json={"op_id": "t-2", "value": 4})
            assert refused.status_code == 503
            assert refused.json()["code"] == "unavailable"
            assert "关停" in refused.json()["message"]
            login = await client.post("/api/v1/auth/login", json={"username": "x", "password": "y"})
            assert login.status_code == 503  # 登录也是改变状态的请求
            assert (await client.get("/api/v1/health")).status_code == 200
            config = await client.get("/api/v1/config")
            assert config.status_code == 200
        assert kernel.config.get("leverage") == 3  # 被拒的那次没有生效


async def test_stop_accepting_also_stops_the_scheduler_from_firing_new_jobs_and_is_idempotent(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, _):
        kernel.stop_accepting()
        kernel.stop_accepting()
        assert kernel.scheduler.state == STATE_PAUSED
        assert kernel.app.state.accepting is False


# ---------------------------------------------------------------- 时限：任何一步卡住都不能把关停拖过去


async def test_a_hanging_step_is_abandoned_after_its_timeout_and_the_rest_still_runs(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=False)
    kernel.lifecycle.hook_timeout_s = 0.2

    async def hang() -> None:
        await asyncio.sleep(3600)

    kernel.notifier.stop = hang
    kernel.stream.stop = hang
    released: list[bool] = []
    kernel.legacy.release_all = lambda: released.append(True)
    started = time.monotonic()
    await kernel.stop()
    assert time.monotonic() - started < 5
    assert kernel.ledger.closed and released == [True]


async def test_a_coordinator_that_will_not_drain_is_abandoned_after_the_drain_budget(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=False)
    kernel.lifecycle.shutdown_drain_s = 0.3

    async def hang(drain_s) -> None:
        await asyncio.sleep(3600)

    kernel.coordinator.stop = hang
    started = time.monotonic()
    await kernel.stop()
    assert time.monotonic() - started < 5
    assert kernel.ledger.closed


async def test_the_whole_shutdown_has_a_deadline_of_its_own(tmp_path):
    """各步的时限加起来可能超过守护脚本给的 30 秒；整体再加一道期限，到点就不再等，只做最后的两件小事
    （关账本做检查点、释放旧入口的锁），其余交给下次启动的恢复。"""
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=False)
    assert 0 < kernel.stop_deadline_s < 30
    kernel.stop_deadline_s = 0.3
    kernel.lifecycle.hook_timeout_s = 60

    async def hang() -> None:
        await asyncio.sleep(3600)

    kernel.guard.stop = hang
    released: list[bool] = []
    kernel.legacy.release_all = lambda: released.append(True)
    closed_adapter = spy_async(kernel.adapter, "close")
    started = time.monotonic()
    await kernel.stop()
    assert time.monotonic() - started < 5
    assert closed_adapter == []  # 卡住的那一步之后的步骤没有再走
    assert kernel.ledger.closed and released == [True]


async def test_an_idle_kernel_shuts_down_quickly_and_leaves_the_heartbeat_alone(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=True)
    beat = Heartbeat.read(kernel.heartbeat.path)
    started = time.monotonic()
    await kernel.stop()
    assert time.monotonic() - started < 5
    assert Heartbeat.read(kernel.heartbeat.path) == beat  # 关停不删心跳文件（看护方据此判断内核何时停的）


# ---------------------------------------------------------------- 在途的定时作业


async def fire_now(kernel, name: str) -> None:
    kernel.scheduler.modify_job(name, next_run_time=datetime.now(timezone.utc))


async def test_stop_lets_a_running_job_finish_before_the_stream_is_closed(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=True)
    order: list[str] = []
    entered = asyncio.Event()

    async def slow_job() -> None:
        entered.set()
        await asyncio.sleep(0.3)
        order.append("job.finished")

    kernel.jobs["seat_expiry"] = slow_job
    spy_async(kernel.stream, "stop", order=order, label="stream.stop")
    await fire_now(kernel, "seat_expiry")
    await asyncio.wait_for(entered.wait(), 5)
    await kernel.stop()
    assert first_occurrences(order)[:2] == ["job.finished", "stream.stop"]
    assert kernel.ledger.closed


async def test_a_job_that_will_not_finish_is_cancelled_after_the_time_limit_and_does_not_hold_up_the_stop(
        tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "JOB_QUIESCE_S", 0.2)
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=True)
    entered = asyncio.Event()
    cancelled: list[bool] = []

    async def stuck_job() -> None:
        entered.set()
        try:
            await asyncio.sleep(3600)
        finally:
            cancelled.append(True)

    kernel.jobs["seat_expiry"] = stuck_job
    await fire_now(kernel, "seat_expiry")
    await asyncio.wait_for(entered.wait(), 5)
    started = time.monotonic()
    await kernel.stop()
    assert time.monotonic() - started < 5
    for _ in range(50):
        if cancelled:
            break
        await asyncio.sleep(0.02)
    assert cancelled == [True]
    assert kernel.ledger.closed and not kernel.scheduler.running
