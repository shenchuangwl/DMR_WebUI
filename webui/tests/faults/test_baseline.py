"""对照组：不注入任何故障时，完整装配的内核经测试专用入口造仓，止损马上就在，通用标准都满足（说明帮手本身量得对）。"""
from __future__ import annotations

from dmr_kernel.core.types import IntentState, Ownership, ProtectionState
from tests.faults.kit import LONG, Rig, fill_time_ms


async def test_without_faults_the_test_entry_opens_a_strategy_position_that_is_protected_at_once(tmp_path):
    rig = await Rig.start(tmp_path)
    try:
        record = await rig.open_strategy()
        assert record.state is IntentState.COMPLETED
        waited = await rig.run_until(rig.has_stop, limit_s=30)
        assert waited == 0  # 不用推进注入时钟：成交后的保护周期立即挂上
        assert rig.now_ms() - fill_time_ms(rig, record.id) == 0
        pos = rig.position()
        assert pos is not None and pos.ownership is Ownership.STRATEGY
        assert pos.protection_state is ProtectionState.SYSTEM
        assert rig.system_stop()["client_algo_id"] == pos.stop_client_id
        await rig.step(65)  # 一分钟里所有作业都跑过一轮：没有误动作
        assert rig.has_stop() and rig.position().protection_state is ProtectionState.SYSTEM
        rig.assert_general()
        assert rig.job_errors == []
        assert rig.position().side is LONG
    finally:
        await rig.stop()


async def test_without_faults_a_trailing_update_moves_the_stop_up_with_no_unprotected_gap(tmp_path):
    from tests.faults.kit import watch_gap, trail_up

    rig = await Rig.start(tmp_path)
    try:
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        old = rig.system_stop()
        await trail_up(rig)
        new = rig.system_stop()
        assert new is not None and new["client_algo_id"] != old["client_algo_id"]
        assert float(new["trigger_price"]) > float(old["trigger_price"])  # 朝有利方向移动
        gap, present = await watch_gap(rig, 10)
        assert (gap, present) == (0, 1)
        assert rig.position().stop_client_id == new["client_algo_id"] and len(rig.system_stops()) == 1
        rig.assert_general()
    finally:
        await rig.stop()
