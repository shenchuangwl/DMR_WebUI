"""具名作业（规格 3.2 的 17 个）：每个作业直接调用 ``kernel.jobs[名字]()`` 就能工作，并且做的是任务表里写的那件事。

时间全靠 ManualClock 推进（与进程内替身共用）；作业此处不经调度器（run_jobs=False），调度见 test_scheduler_schedule.py。
"""
from __future__ import annotations

import inspect
import logging
import os
from decimal import Decimal

import pytest

from dmr_kernel.alerts.heartbeat import Heartbeat
from dmr_kernel.core.scheduler import WAL_ALERT_BYTES
from dmr_kernel.core.types import (AlertLevel, IntentState, OrderPurpose, OrderSide, PendingReason, PositionInfo,
                                   PositionSide)
from sched_kit import JOB_NAMES, create_intent, register_position, spy_async
from tests.support.kernel import FakeRecovery, make_kernel, settle, started_kernel

D = Decimal
HOUR_MS = 3_600_000
BAR_MS = 900_000


def test_job_names_are_the_agreed_seventeen_plus_the_m2_close_all_tick():
    assert len(JOB_NAMES) == len(set(JOB_NAMES)) == 18 and JOB_NAMES[-1] == "close_all_tick"


async def test_every_job_exists_right_after_build_as_a_coroutine_function_without_arguments(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        assert sorted(kernel.jobs) == sorted(JOB_NAMES)
        for name, job in kernel.jobs.items():
            assert inspect.iscoroutinefunction(job), name
            assert not inspect.signature(job).parameters, name
    finally:
        await kernel.stop()


# ---------------------------------------------------------------- 5 秒一档：数据流判活、未知订单回查、意图过期


async def test_stream_liveness_calls_the_streams_check_liveness(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        calls = spy_async(kernel.stream, "check_liveness")
        await kernel.jobs["stream_liveness"]()
        assert len(calls) == 1


async def test_a_silent_stream_is_declared_interrupted_by_the_job(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        await kernel.stream.start()
        assert kernel.stream.state == "connected"
        standin.clock.advance(89_000)  # stream_silence_s = 90
        await kernel.jobs["stream_liveness"]()
        assert kernel.stream.state == "connected"
        standin.clock.advance(2_000)
        await kernel.jobs["stream_liveness"]()
        assert kernel.stream.state == "interrupted"


async def test_stream_liveness_does_nothing_when_the_stream_is_disabled(tmp_path):
    async with started_kernel(tmp_path, config_overrides={"ex_stream_enabled": False}) as (kernel, standin):
        assert kernel.stream is None
        await kernel.jobs["stream_liveness"]()
        assert standin.transport.ws_paths == []


async def test_resolve_unknown_hands_over_to_the_coordinator(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        calls = spy_async(kernel.coordinator, "resolve_unknown_orders")
        await kernel.jobs["resolve_unknown"]()
        assert len(calls) == 1


async def test_expire_intents_expires_exactly_the_intents_that_are_due(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        short = await create_intent(kernel, "BTCUSDT", ttl_s=60)
        long_lived = await create_intent(kernel, "ETHUSDT", ttl_s=600)
        forever = await create_intent(kernel, "DOGEUSDT", ttl_s=None)
        standin.clock.advance(59_000)
        await kernel.jobs["expire_intents"]()
        assert (await kernel.intents.get(short.id)).state is IntentState.CREATED
        standin.clock.advance(1_000)
        await kernel.jobs["expire_intents"]()
        assert (await kernel.intents.get(short.id)).state is IntentState.EXPIRED
        assert (await kernel.intents.get(long_lived.id)).state is IntentState.CREATED
        assert (await kernel.intents.get(forever.id)).state is IntentState.CREATED


# ---------------------------------------------------------------- 对账


async def test_reconcile_positions_runs_a_round_and_refreshes_the_limits_snapshot(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        now = standin.clock.now_ms()
        assert kernel.limits.occupancy(degraded=False, now_ms=now).stale is True  # 还没有快照
        await kernel.jobs["reconcile_positions"]()
        status = kernel.reconciler.status()
        assert status["positions_status"] == "ok" and status["positions_at_ms"] == now
        assert standin.transport.sent("GET", "/api/private/positions")
        # 挂单快照还没有，所以整体仍按未知处理；持仓快照已是新的
        await kernel.jobs["reconcile_orders"]()
        assert kernel.reconciler.status()["orders_status"] == "ok"
        assert kernel.limits.occupancy(degraded=False, now_ms=now).stale is False


async def test_reconcile_orders_takes_the_orders_and_algo_orders_snapshot(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        await kernel.jobs["reconcile_orders"]()
        status = kernel.reconciler.status()
        assert status["orders_status"] == "ok" and status["orders_at_ms"] == standin.clock.now_ms()
        assert status["positions_status"] is None  # 另一类对账没有被顺带跑


async def test_an_error_inside_a_job_reaches_the_caller(tmp_path):
    """作业不吞异常：交给调用方（调度器会记日志并继续按时触发，测试能直接看到）。"""
    async with started_kernel(tmp_path) as (kernel, _):
        async def broken() -> str:
            raise RuntimeError("交易所读失败")

        kernel.reconciler.reconcile_positions = broken
        with pytest.raises(RuntimeError, match="交易所读失败"):
            await kernel.jobs["reconcile_positions"]()


# ---------------------------------------------------------------- 补拉成交、收益流水


async def test_backfill_trades_covers_positions_orders_intents_and_recent_intents_only(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        clock = standin.clock
        await settle(kernel)
        # 登记簿里的持仓
        await register_position(kernel, "BTCUSDT")
        # 未到终态的订单（连同它的意图）
        eth = await create_intent(kernel, "ETHUSDT", key="test:eth")
        await kernel.orders.create_pending(
            intent=eth, leg="o", purpose=OrderPurpose.OPEN, kind="market", order_type="MARKET", side=OrderSide.BUY,
            position_side=PositionSide.LONG, qty=D("0.1"))
        # 未到终态的意图
        await create_intent(kernel, "DOGEUSDT", key="test:doge")
        # 交易所有仓而登记簿没有的（外部仓）：合约数上限的快照里有它
        kernel.limits.update_positions(
            [PositionInfo(symbol="BNBUSDT", side=PositionSide.LONG, qty=D("0.1"), entry_price=D("741"))],
            clock.now_ms())
        # 终态不久的意图：成交回报可能还在路上
        old = await create_intent(kernel, "XRPUSDT", key="test:xrp")
        await kernel.intents.transition(old.id, IntentState.REJECTED, reason="test")
        clock.advance(5 * 60_000)
        recent = await create_intent(kernel, "SOLUSDT", key="test:sol")
        await kernel.intents.transition(recent.id, IntentState.REJECTED, reason="test")
        clock.advance(6 * 60_000)  # XRP 的意图是 11 分钟前终态的，SOL 的是 6 分钟前
        await settle(kernel)

        calls = spy_async(kernel.ingest, "backfill_trades")
        await kernel.jobs["backfill_trades"]()
        assert len(calls) == 1
        assert sorted(calls[0][0][0]) == ["BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT"]


async def test_backfill_trades_with_nothing_to_watch_asks_the_exchange_for_nothing(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        await kernel.jobs["backfill_trades"]()
        assert standin.transport.sent("GET", "/api/private/trades") == []


async def test_backfill_income_hands_over_to_ingest(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        calls = spy_async(kernel.ingest, "backfill_income")
        await kernel.jobs["backfill_income"]()
        assert len(calls) == 1
        assert standin.transport.sent("GET", "/api/private/income")


# ---------------------------------------------------------------- 周期复核与登记到控制接口的实时缓存


async def test_guard_periodic_runs_and_then_notes_the_position_mode_and_restrictions_for_the_api(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        live = kernel.app.state.services.live
        assert live.position_mode is None and live.restrictions is None  # 没读到之前不编造
        calls = spy_async(kernel.guard, "periodic")
        await kernel.jobs["guard_periodic"]()
        assert len(calls) == 1
        now = standin.clock.now_ms()
        assert live.position_mode.dual_side is True and live.position_mode.at_ms == now
        assert live.restrictions.restricted is False and live.restrictions.at_ms == now


async def test_the_notes_follow_the_guards_status(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        live = kernel.app.state.services.live

        async def no_periodic() -> None:
            return None

        kernel.guard.periodic = no_periodic
        status = {"position_mode": "one_way", "position_mode_checked_ms": 111, "restricted": True,
                  "restriction_detail": {"canTrade": "false"}, "restrictions_checked_ms": 222}
        kernel.guard.status = lambda: status
        await kernel.jobs["guard_periodic"]()
        assert (live.position_mode.dual_side, live.position_mode.at_ms) == (False, 111)
        assert live.restrictions.restricted is True and live.restrictions.at_ms == 222
        assert live.restrictions.detail == (("canTrade", "false"),)


async def test_nothing_is_noted_for_what_the_guard_could_not_read(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        live = kernel.app.state.services.live

        async def no_periodic() -> None:
            return None

        kernel.guard.periodic = no_periodic
        kernel.guard.status = lambda: {"position_mode": "unknown", "position_mode_checked_ms": None,
                                       "restricted": None, "restriction_detail": {}, "restrictions_checked_ms": None}
        await kernel.jobs["guard_periodic"]()
        assert live.position_mode is None and live.restrictions is None


# ---------------------------------------------------------------- K 线


def monitored_clock(standin, bars: int = 1, delay_ms: int = 3000) -> int:
    """把时钟推到某个 15 分钟边界之后 delay 毫秒，返回这个边界。"""
    boundary = standin.clock.now_ms() + bars * BAR_MS
    standin.clock.set(boundary + delay_ms)
    return boundary


async def test_bar_close_short_asks_for_the_bar_that_just_closed_for_the_monitored_symbols(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        await register_position(kernel, "BTCUSDT")
        eth = await create_intent(kernel, "ETHUSDT", key="test:eth")
        await kernel.orders.create_pending(
            intent=eth, leg="o", purpose=OrderPurpose.OPEN, kind="market", order_type="MARKET", side=OrderSide.BUY,
            position_side=PositionSide.LONG, qty=D("0.1"))
        boundary = monitored_clock(standin)
        calls = spy_async(kernel.candles, "on_bar_close")
        await kernel.jobs["bar_close_short"]()
        assert len(calls) == 1
        assert calls[0][0] == ("15m", boundary - BAR_MS, ["BTCUSDT", "ETHUSDT"])


async def test_the_monitored_set_is_managed_positions_plus_symbols_with_open_orders(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        await register_position(kernel, "BTCUSDT")
        await kernel.registry.register_pending(symbol="SOLUSDT", side=PositionSide.SHORT, qty=D("1"),
                                               avg_price=D("100"), reason=PendingReason.OUT_OF_SCOPE)
        assert await kernel.monitored_symbols() == ["BTCUSDT"]  # 待确认的外部仓不在监控集合里
        await create_intent(kernel, "DOGEUSDT", key="test:doge")  # 只有意图、还没有订单：不算
        assert await kernel.monitored_symbols() == ["BTCUSDT"]


async def test_bar_close_with_nothing_monitored_still_publishes_an_empty_ready_event(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        monitored_clock(standin)
        calls = spy_async(kernel.candles, "on_bar_close")
        await kernel.jobs["bar_close_short"]()
        assert calls[0][0][2] == []


async def test_bar_close_long_uses_the_long_timeframe_and_its_own_boundary(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        await register_position(kernel, "BTCUSDT")
        boundary = standin.clock.now_ms() + HOUR_MS  # T0 在整点上
        standin.clock.set(boundary + 3000)
        calls = spy_async(kernel.candles, "on_bar_close")
        await kernel.jobs["bar_close_long"]()
        assert calls[0][0] == ("1h", boundary - HOUR_MS, ["BTCUSDT"])


async def test_the_timeframes_and_the_delay_come_from_the_configuration(tmp_path):
    overrides = {"timeframe_short": "5m", "timeframe_long": "4h", "kline_delay_s": 7}
    async with started_kernel(tmp_path, config_overrides=overrides) as (kernel, standin):
        boundary = standin.clock.now_ms() + 6 * HOUR_MS  # T0 是 02:00，+6 小时 = 08:00 在 4 小时的边界上
        standin.clock.set(boundary + 7000)
        calls = spy_async(kernel.candles, "on_bar_close")
        await kernel.jobs["bar_close_short"]()
        await kernel.jobs["bar_close_long"]()
        assert calls[0][0][:2] == ("5m", boundary - 300_000)
        assert calls[1][0][:2] == ("4h", boundary - 4 * HOUR_MS)
        standin.clock.set(boundary + 6999)  # 还没到「边界 + 延迟」：上一根才是最新收盘的
        await kernel.jobs["bar_close_short"]()
        assert calls[2][0][:2] == ("5m", boundary - 600_000)


# ---------------------------------------------------------------- 服务器时间、合约清单、心跳、席位、保护单、账本


async def test_server_time_calibrates_the_server_clock(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        await kernel.jobs["server_time"]()
        assert kernel.server_clock.updated_ms == standin.clock.now_ms()
        assert kernel.server_clock.skew_ms == 0  # 替身与内核共用一个时钟


async def test_rules_refresh_reloads_the_instrument_list(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        started = kernel.rules.refreshed_ms
        assert started == standin.clock.now_ms()
        standin.clock.advance(HOUR_MS)
        await kernel.jobs["rules_refresh"]()
        assert kernel.rules.refreshed_ms == started + HOUR_MS


async def test_a_failed_refresh_keeps_the_old_list_and_asks_for_an_early_retry_only_if_there_is_none(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, standin):
        assert kernel.scheduler.get_job("rules_refresh_retry") is None
        calls: list = []

        async def failing(adapter) -> bool:
            calls.append(adapter)
            return False

        kernel.rules.refresh = failing
        await kernel.jobs["rules_refresh"]()
        assert calls and kernel.rules.get("BTCUSDT") is not None  # 旧清单还在
        assert kernel.scheduler.get_job("rules_refresh_retry") is None  # 有旧清单：等下一个整点就行


async def test_heartbeat_writes_the_pid_and_the_time(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        path = kernel.heartbeat.path
        assert Heartbeat.read(path) is None
        await kernel.jobs["heartbeat"]()
        assert Heartbeat.read(path) == {"pid": os.getpid(), "at_ms": standin.clock.now_ms()}
        assert path.stat().st_mode & 0o777 == 0o600
        standin.clock.advance(30_000)
        await kernel.jobs["heartbeat"]()
        assert Heartbeat.read(path)["at_ms"] == standin.clock.now_ms()


async def test_a_heartbeat_that_cannot_be_written_is_logged_and_alerted_but_does_not_stop_the_job(tmp_path, caplog):
    async with started_kernel(tmp_path) as (kernel, _):
        def disk_full() -> None:
            raise OSError(28, "No space left on device")

        kernel.heartbeat.beat = disk_full
        with caplog.at_level(logging.ERROR, logger="dmr_kernel.core.scheduler"):
            await kernel.jobs["heartbeat"]()
            await kernel.jobs["heartbeat"]()  # 再失败一次：告警按去重键只记一条
        assert any("心跳" in record.getMessage() for record in caplog.records)
        await kernel.notifier.flush()
        alerts = [a for a in await kernel.notifier.recent() if a.kind == "heartbeat_write_failed"]
        assert len(alerts) == 1


async def test_seat_expiry_clears_expired_seats_in_the_api(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        seat = kernel.app.state.services.seat
        calls = spy_async(seat, "expire_due")
        await kernel.jobs["seat_expiry"]()
        assert len(calls) == 1


async def test_protection_tick_drives_the_protection_manager(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        calls = spy_async(kernel.protection, "tick")
        await kernel.jobs["protection_tick"]()
        assert len(calls) == 1


async def test_ledger_backup_writes_an_online_backup_outside_var(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        backup_dir = kernel.settings.backup_dir
        assert backup_dir == tmp_path / "backup" and kernel.settings.var_dir not in backup_dir.parents
        before = len(list(backup_dir.glob("ledger-*.sqlite")))
        await kernel.jobs["ledger_backup"]()
        standin.clock.advance(1000)  # 文件名带 UTC 时间戳
        await kernel.jobs["ledger_backup"]()
        assert len(list(backup_dir.glob("ledger-*.sqlite"))) == before + 2


async def test_a_failing_backup_is_alerted_once_and_the_error_still_reaches_the_caller(tmp_path, caplog):
    """备份悄悄失败（磁盘满、目录没权限）而没人知道，等到要用的时候才发现一份都没有：失败要有告警，不光是日志。"""
    async with started_kernel(tmp_path) as (kernel, _):
        async def disk_full(dest_dir):
            raise OSError(28, "No space left on device")

        kernel.ledger.backup = disk_full
        with caplog.at_level(logging.ERROR, logger="dmr_kernel.core.scheduler"):
            for _ in range(2):  # 再失败一次：告警按去重键只记一条
                with pytest.raises(OSError, match="No space left"):
                    await kernel.jobs["ledger_backup"]()
        assert any("备份" in record.getMessage() for record in caplog.records)
        await kernel.notifier.flush()
        alerts = [a for a in await kernel.notifier.recent() if a.kind == "ledger_backup_failed"]
        assert len(alerts) == 1 and alerts[0].level is AlertLevel.WARNING
        assert "No space left" in alerts[0].message


async def test_a_successful_backup_raises_no_alert(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        await kernel.jobs["ledger_backup"]()
        await kernel.notifier.flush()
        assert [a for a in await kernel.notifier.recent() if a.kind == "ledger_backup_failed"] == []


async def test_ledger_checkpoint_asks_the_ledger_for_a_checkpoint(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        calls = spy_async(kernel.ledger, "checkpoint")
        await kernel.jobs["ledger_checkpoint"]()
        assert len(calls) == 1


async def test_a_wal_file_that_is_still_large_after_the_checkpoint_raises_one_warning(tmp_path, caplog):
    """报告 4.4「数据库维护」：WAL 文件过大时告警。看的是检查点做完之后的大小：截不断才说明有读事务一直没结束。"""
    async with started_kernel(tmp_path) as (kernel, _):
        order: list[str] = []
        spy_async(kernel.ledger, "checkpoint", order=order, label="checkpoint")

        def too_big() -> int:
            order.append("wal_bytes")
            return WAL_ALERT_BYTES + 1

        kernel._wal_bytes = too_big
        with caplog.at_level(logging.WARNING, logger="dmr_kernel.core.scheduler"):
            await kernel.jobs["ledger_checkpoint"]()
            await kernel.jobs["ledger_checkpoint"]()  # 还是那么大：告警按去重键只记一条
        assert order == ["checkpoint", "wal_bytes", "checkpoint", "wal_bytes"]
        assert any("WAL" in record.getMessage() for record in caplog.records)
        await kernel.notifier.flush()
        alerts = [a for a in await kernel.notifier.recent() if a.kind == "ledger_wal_large"]
        assert len(alerts) == 1 and alerts[0].level is AlertLevel.WARNING
        assert "WAL" in alerts[0].message and str(WAL_ALERT_BYTES // (1024 * 1024)) in alerts[0].message


async def test_a_wal_file_at_the_limit_or_below_it_raises_nothing(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        for size in (0, 4096, WAL_ALERT_BYTES):
            kernel._wal_bytes = lambda size=size: size
            await kernel.jobs["ledger_checkpoint"]()
        await kernel.notifier.flush()
        assert [a for a in await kernel.notifier.recent() if a.kind == "ledger_wal_large"] == []


async def test_the_real_wal_file_is_measured_and_a_missing_one_counts_as_empty(tmp_path):
    async with started_kernel(tmp_path) as (kernel, _):
        wal = kernel.settings.db_path.with_name(kernel.settings.db_path.name + "-wal")
        await kernel.jobs["ledger_checkpoint"]()  # 截断检查点之后文件还在（连接开着），大小为 0
        assert kernel._wal_bytes() == (wal.stat().st_size if wal.exists() else 0) < WAL_ALERT_BYTES
        await kernel.notifier.flush()
        assert [a for a in await kernel.notifier.recent() if a.kind == "ledger_wal_large"] == []
    assert not wal.exists() or wal.stat().st_size == 0
    assert kernel._wal_bytes() == 0  # 关库后 SQLite 把 -wal 删了：不存在就是 0，不抛错


async def test_stream_liveness_passes_the_streams_last_frame_time_to_the_status_cache(tmp_path):
    async with started_kernel(tmp_path) as (kernel, standin):
        await kernel.stream.start()
        standin.clock.advance(30_000)
        kernel.stream.last_frame_ms = standin.clock.now_ms()  # 心跳帧到达（不发任何业务事件）
        await kernel.jobs["stream_liveness"]()
        assert kernel.app.state.services.live.last_frame_ms == standin.clock.now_ms()
