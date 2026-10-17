"""故障 9：手动撤掉系统的止损（替身 ``/dev/manual/cancel``，用户令牌，模拟用户在 Binance 网页上撤单）。

内核发现的途径有两条：替身推送条件单事件时（行为开关 ``push_algo_events``，实测第 11 项）数据流事件立即看到；不推送时
（缺省）靠条件单对账（``reconcile_orders``，每 60 秒）的快照。两条都先直接问交易所确认不在，再按登记的止损价补挂。

断言（报告 4.11）：手动撤掉后 90 秒内重新存在（新的一张，客户编号不同）；持仓登记与交易所一致；同一个意图没有第二张订单；
交易所上这一侧始终最多一张系统止损；撤掉不等于平仓（仓不动、不告「无保护」的最高级告警）。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import AlertLevel, IntentState, Ownership, ProtectionState
from tests.faults.kit import Rig, SYM

DEADLINE_S = 90


def push(on: bool) -> dict:
    return {"behaviors": {"push_algo_events": on}}


async def cancel_system_stop(rig: Rig) -> dict:
    stop = rig.system_stop()
    assert stop is not None
    await rig.manual("/dev/manual/cancel", {"symbol": SYM, "client_algo_id": stop["client_algo_id"]})
    assert not rig.has_stop()
    return stop


async def assert_rebuilt(rig: Rig, old: dict) -> int:
    back = await rig.run_until(rig.has_stop, limit_s=DEADLINE_S)
    assert back is not None, "手动撤掉的系统止损 90 秒内要重新存在"
    new = rig.system_stop()
    assert new["client_algo_id"] != old["client_algo_id"]
    assert new["trigger_price"] == old["trigger_price"]  # 按登记的止损价补挂
    await rig.step(3)
    pos = rig.position()
    assert pos.protection_state is ProtectionState.SYSTEM and pos.stop_client_id == new["client_algo_id"]
    return back


@pytest.mark.parametrize("pushed", [False, True], ids=["snapshot", "stream_event"])
async def test_a_cancelled_stop_of_a_strategy_position_is_back_within_90_seconds(tmp_path, pushed):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(push(pushed))
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        pos = rig.position()
        await rig.step(7)  # 错开对账的整点，免得刚好撞在挂单对账那一秒
        old = await cancel_system_stop(rig)
        back = await assert_rebuilt(rig, old)
        if pushed:  # 数据流事件立即可见：不必等 60 秒一次的挂单对账
            assert back <= 5
        await rig.step(70)
        assert len(rig.system_stops()) == 1
        assert rig.exchange_positions() == {(SYM, "LONG"): pos.qty}  # 撤止损不等于平仓
        assert [a for a in await rig.alerts("protect_unprotected") if a.level is AlertLevel.CRITICAL] == []
        assert len(await rig.alerts("stop_missing_rebuilt")) >= 1
        rig.assert_general()
    finally:
        await rig.stop()


async def test_a_cancelled_stop_of_a_takeover_position_is_back_within_90_seconds(tmp_path):
    rig = await Rig.start(tmp_path)
    try:
        pos = await rig.take_over_manual()
        assert pos.ownership is Ownership.TAKEOVER
        assert await rig.run_until(rig.has_stop, limit_s=30) is not None
        await rig.step(7)
        old = await cancel_system_stop(rig)
        await assert_rebuilt(rig, old)
        await rig.step(70)
        assert len(rig.system_stops()) == 1
        assert rig.exchange_positions() == {(SYM, "LONG"): pos.qty}
        rig.assert_general()
    finally:
        await rig.stop()


async def test_a_stop_cancelled_again_and_again_is_rebuilt_every_time_one_at_a_time(tmp_path):
    rig = await Rig.start(tmp_path)
    try:
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        seen: set[str] = set()
        for _ in range(3):
            await rig.step(5)
            old = await cancel_system_stop(rig)
            seen.add(old["client_algo_id"])
            await assert_rebuilt(rig, old)
            assert rig.system_stop()["client_algo_id"] not in seen
        await rig.step(70)
        assert len(rig.system_stops()) == 1
        rig.assert_general()
    finally:
        await rig.stop()


async def test_a_stop_cancelled_while_the_kernel_is_down_is_back_within_30_seconds_of_the_restart(tmp_path):
    """手动撤掉发生在进程被杀期间：重启恢复按「已登记而无止损」补上（标准：重启后 30 秒内）。"""
    rig = await Rig.start(tmp_path)
    try:
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        await rig.kill()
        old = await cancel_system_stop(rig)
        rig.clock.advance(20_000)
        rig = await rig.restart()
        back = await rig.run_until(rig.has_stop, limit_s=30)
        assert back is not None, "重启后 30 秒内要给已登记而无止损的仓补上"
        assert rig.system_stop()["client_algo_id"] != old["client_algo_id"]
        await rig.step(70)
        assert len(rig.system_stops()) == 1
        rig.assert_general()
    finally:
        await rig.stop()
