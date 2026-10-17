"""上限快照的及时刷新（规格 1.1 第 11 条）：持仓登记、数量变化、归零之后尽快触发一次持仓对账，否则同一根 K 线上
完成得快的开仓之后，后面的开仓可能短暂越过合约数上限。

反应只在恢复返回之后、并且在跑定时作业（run_jobs=True）时才有；关停第一步起不再反应。
"""
from __future__ import annotations

import logging

from dmr_kernel.core.events import PositionChanged, PositionClosed, PositionRegistered
from dmr_kernel.core.types import PositionSide
from dmr_kernel.ledger.repo_registry import EXTERNAL_ADJUST
from tests.support.kernel import FakeRecovery, make_kernel, started_kernel

LONG = PositionSide.LONG
POSITIONS = "/api/private/positions"


def registered(kernel) -> PositionRegistered:
    return PositionRegistered(position_id=1, symbol="BTCUSDT", side=LONG, ownership="strategy", status="managed",
                              at_ms=kernel.clock.now_ms())


def changed(kernel, cause: str) -> PositionChanged:
    return PositionChanged(position_id=1, symbol="BTCUSDT", side=LONG, cause=cause, version=2,
                           at_ms=kernel.clock.now_ms())


def closed(kernel) -> PositionClosed:
    return PositionClosed(position_id=1, symbol="BTCUSDT", side=LONG, exit_reason="stop", at_ms=kernel.clock.now_ms())


async def react(kernel, *events) -> None:
    for event in events:
        kernel.bus.publish(event)
    await kernel.bus.drain(5)
    await kernel.idle()


def position_reads(standin) -> int:
    return len(standin.transport.sent("GET", POSITIONS))


async def test_a_newly_registered_position_triggers_a_position_reconcile_right_away(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, standin):
        assert kernel.reconciler.status()["positions_status"] is None
        await react(kernel, registered(kernel))
        assert kernel.reconciler.status()["positions_status"] == "ok"
        assert position_reads(standin) == 1


async def test_every_change_that_moves_the_quantity_triggers_it(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, standin):
        reads = 0
        for event in (changed(kernel, "fill"), changed(kernel, EXTERNAL_ADJUST), changed(kernel, "qty_change"),
                      closed(kernel)):
            await react(kernel, event)
            reads += 1
            assert position_reads(standin) == reads, event


async def test_changes_that_leave_the_quantity_alone_do_not_trigger_it(tmp_path):
    """移动止损、清理旧止损、移交止损这些每分钟都在发生，不值得每次都对一遍账。"""
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, standin):
        await react(kernel, *(changed(kernel, cause) for cause in
                              ("protection", "stale_stops", "handed_over", "managed", "ignored", "pending")))
        assert position_reads(standin) == 0


async def test_a_burst_of_events_is_coalesced_into_at_most_two_reconciles(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, standin):
        await react(kernel, *(registered(kernel) for _ in range(12)))
        assert 1 <= position_reads(standin) <= 2


async def test_the_reaction_does_not_exist_without_the_scheduled_jobs(tmp_path):
    async with started_kernel(tmp_path, run_jobs=False) as (kernel, standin):
        await react(kernel, registered(kernel))
        assert position_reads(standin) == 0


async def test_events_during_the_recovery_do_not_trigger_it(tmp_path):
    """恢复里自己有全量对账；反应在恢复返回之后才订阅，避免和恢复并发。"""
    seen: dict = {}

    async def during_recovery(kwargs) -> None:
        kernel = seen["kernel"]
        kernel.bus.publish(registered(kernel))
        await kernel.bus.drain(5)
        await kernel.idle()

    kernel, standin = make_kernel(tmp_path, recovery_factory=FakeRecovery(on_run=during_recovery))
    seen["kernel"] = kernel
    try:
        await kernel.start(serve_http=False, run_jobs=True)
        assert position_reads(standin) == 0
    finally:
        await kernel.stop()


async def test_after_stop_accepting_the_reaction_is_off(tmp_path):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, standin):
        kernel.stop_accepting()
        await react(kernel, registered(kernel))
        assert position_reads(standin) == 0


async def test_a_failing_reconcile_is_logged_and_the_next_event_still_works(tmp_path, caplog):
    async with started_kernel(tmp_path, run_jobs=True) as (kernel, standin):
        original = kernel.reconciler.reconcile_positions
        calls: list[int] = []

        async def broken() -> str:
            calls.append(1)
            raise RuntimeError("交易所读失败")

        kernel.reconciler.reconcile_positions = broken
        with caplog.at_level(logging.ERROR, logger="dmr_kernel.core.scheduler"):
            await react(kernel, registered(kernel))
        assert calls == [1]
        assert any("交易所读失败" in str(record.exc_info[1]) for record in caplog.records if record.exc_info)
        kernel.reconciler.reconcile_positions = original
        await react(kernel, registered(kernel))
        assert kernel.reconciler.status()["positions_status"] == "ok"
