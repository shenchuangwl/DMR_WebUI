"""故障 4：进程被杀后重启。

「杀」= 进程内崩溃点（``CrashRegistry.arm`` + ``CrashSignal``）或在任意时刻直接丢弃内核实例（``abandon``：不走关停、不撤任何单）；
「重启」= 用同一账本、同一替身重新装配完整内核，跑真实的重启恢复（``core.recovery``）。停机期间注入时钟照走（20 秒）。

断言：重启后 30 秒内（注入时钟，从新内核开始启动算起）给已登记而无止损的仓补上止损；已经有止损的不补第二张（认领）；
开仓单发出后被杀的，重启后按编号回查到成交、登记并保护，开仓单不重发；平仓成交后、清理保护单之前被杀的，重启后残留止损被撤掉、
登记簿记为已平；持仓登记与交易所一致。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.crashpoints import CrashRegistry, CrashSignal
from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.types import IntentAction, IntentSource, IntentState, PositionStatus, ProtectionState
from tests.faults.kit import LONG, Rig, SYM, next_key

RESTART_DEADLINE_S = 30
DOWNTIME_MS = 20_000


async def kill_and_restart(rig: Rig) -> tuple[Rig, int]:
    """被杀、停机 20 秒、重启；返回新的 Rig 与新内核开始启动的时刻。"""
    await rig.kill()
    rig.clock.advance(DOWNTIME_MS)
    rig.standin.disp.tick()
    started_ms = rig.now_ms()
    return await rig.restart(), started_ms


async def test_killed_after_the_open_order_was_sent_the_restart_finds_the_fill_and_protects_it(tmp_path):
    crash = CrashRegistry(live=False)
    crash.arm("open_sent")
    rig = await Rig.start(tmp_path, crash=crash)
    with pytest.raises(CrashSignal):
        await rig.open_strategy()
    assert (SYM, "LONG") in rig.exchange_positions()  # 交易所已成交
    assert not rig.has_stop()
    rig2, started_ms = await kill_and_restart(rig)
    try:
        waited = await rig2.run_until(rig2.has_stop, limit_s=RESTART_DEADLINE_S)
        assert waited is not None and rig2.now_ms() - started_ms <= RESTART_DEADLINE_S * 1000
        pos = rig2.position()
        assert pos is not None and pos.protection_state is ProtectionState.SYSTEM
        intent = await rig2.kernel.intents.get(pos.opened_by_intent_id)
        assert intent.state is IntentState.COMPLETED
        await rig2.step(70)
        assert len(rig2.exchange_orders()) == 1  # 开仓单没有重发
        rig2.assert_general()
    finally:
        await rig2.stop()


async def test_killed_before_the_first_stop_the_restart_places_it_within_30_seconds(tmp_path):
    crash = CrashRegistry(live=False)
    crash.arm("open_filled_before_stop")
    rig = await Rig.start(tmp_path, crash=crash)
    assert (await rig.open_strategy()).state is IntentState.COMPLETED
    await rig.settle()
    assert rig.kernel.protection.crashed is not None
    assert rig.position() is not None and not rig.has_stop()  # 已登记而无止损
    rig2, started_ms = await kill_and_restart(rig)
    try:
        waited = await rig2.run_until(rig2.has_stop, limit_s=RESTART_DEADLINE_S)
        assert waited is not None, "重启后 30 秒内应当给已登记而无止损的仓补上止损"
        assert rig2.now_ms() - started_ms <= RESTART_DEADLINE_S * 1000
        await rig2.step(70)
        assert len(rig2.system_stops()) == 1 and len(rig2.exchange_algos()) == 1
        assert rig2.position().protection_state is ProtectionState.SYSTEM
        rig2.assert_general()
    finally:
        await rig2.stop()


@pytest.mark.parametrize("kind", ["strategy", "takeover"])
async def test_a_protected_position_keeps_its_one_stop_across_a_kill(tmp_path, kind):
    rig = await Rig.start(tmp_path)
    if kind == "strategy":
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
    else:
        await rig.take_over_manual()
    assert await rig.run_until(rig.has_stop, limit_s=30) is not None
    stop_id = rig.system_stop()["client_algo_id"]
    position_id = rig.position().id
    rig2, _ = await kill_and_restart(rig)
    try:
        await rig2.step(70)
        assert rig2.position().id == position_id
        assert rig2.position().protection_state is ProtectionState.SYSTEM
        assert [a["client_algo_id"] for a in rig2.system_stops()] == [stop_id]  # 认领原来那张，不补第二张
        assert len(rig2.exchange_algos()) == 1
        rig2.assert_general()
    finally:
        await rig2.stop()


async def test_killed_after_the_close_filled_the_restart_cleans_up_the_leftover_stop(tmp_path):
    crash = CrashRegistry(live=False)
    rig = await Rig.start(tmp_path, crash=crash)
    assert (await rig.open_strategy()).state is IntentState.COMPLETED
    assert rig.has_stop()
    position_id = rig.position().id
    crash.arm("close_filled_before_cleanup")
    submitted = await rig.kernel.coordinator.submit(IntentSpec(
        action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR, symbol=SYM, side=LONG,
        idem_key=next_key("close"), reason="故障注入：平仓后被杀", position_id=position_id))
    await rig.kernel.coordinator.wait_terminal(submitted.intent_id, 1.0)
    await rig.settle()
    await rig.run_jobs("backfill_trades")
    assert rig.exchange_positions() == {}
    assert rig.has_stop()  # 清理之前被杀：止损还挂着
    assert rig.kernel.protection.crashed is not None
    rig2, started_ms = await kill_and_restart(rig)
    try:
        cleaned = await rig2.run_until(lambda: rig2.open_algos() == [], limit_s=RESTART_DEADLINE_S)
        assert cleaned is not None, "重启后应当撤掉已平仓位的残留止损"
        await rig2.step(70)
        closed = await rig2.kernel.registry.fetch(position_id)
        assert closed.status is PositionStatus.CLOSED
        assert rig2.position() is None
        rig2.assert_general()
    finally:
        await rig2.stop()
