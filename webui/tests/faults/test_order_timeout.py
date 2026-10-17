"""故障 1：下单超时（替身 ``order_timeout``）。

- ``accept_then_hang``：交易所照常成交/建单，但应答被扣住直到客户端超时 —— 内核不知道结果，只能按编号回查。
- ``drop``：交易所没有受理，扣住后断开 —— 同样不知道结果，回查之后确认不存在。

断言（报告 4.11 的通用标准）：持仓登记与交易所一致；同一个意图在交易所没有第二张订单/条件单（开仓单从不重发、止损单回查到了就认领）；
从成交到止损存在不超过 30 秒（注入时钟）。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import IntentState, ProtectionState
from tests.faults.kit import STOP_GAP_MAX_S, Rig, fill_time_ms, trail_up, watch_gap

STOP_DEADLINE_S = 30


def timeout_fault(mode: str, scope: str, count: int = 1) -> dict:
    return {"faults": {"order_timeout": {"enabled": True, "mode": mode, "scope": scope, "count": count,
                                         "hang_s": 5}}}


async def test_an_open_order_whose_reply_is_lost_is_found_filled_and_protected_within_30_seconds(tmp_path):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(timeout_fault("accept_then_hang", "market"))
        record = await rig.open_strategy()
        # 应答没回来：要么还在回查（RECONCILING），要么数据流的成交推送先到、已经判完
        assert record.state in (IntentState.RECONCILING, IntentState.COMPLETED)
        filled_ms = fill_time_ms(rig, record.id)  # 交易所其实已经成交
        waited = await rig.run_until(rig.has_stop, limit_s=STOP_DEADLINE_S)
        assert waited is not None, "成交后 30 秒内止损没有挂上"
        assert rig.now_ms() - filled_ms <= STOP_DEADLINE_S * 1000
        intent = await rig.kernel.intents.get(record.id)
        assert intent.state is IntentState.COMPLETED  # 回查到已成交
        pos = rig.position()
        assert pos is not None and pos.protection_state is ProtectionState.SYSTEM
        await rig.step(70)
        rig.assert_general()
        assert len([o for o in rig.exchange_orders() if o["source"] == "api"]) == 1  # 开仓单没有重发
    finally:
        await rig.stop()


async def test_an_open_order_that_was_never_accepted_is_given_up_without_a_second_order(tmp_path):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(timeout_fault("drop", "market"))
        record = await rig.open_strategy()
        assert record.state is IntentState.RECONCILING
        assert rig.exchange_orders() == []  # 交易所没有受理
        done = await rig.run_until(
            lambda: _terminal(rig, record.id), limit_s=120)
        assert done is not None, "确认不存在之后意图应当结束"
        intent = await rig.kernel.intents.get(record.id)
        assert intent.state is not IntentState.COMPLETED
        assert rig.exchange_orders() == []  # 开仓单从不重发：一张都没有
        assert rig.position() is None and rig.exchange_positions() == {}
        rig.assert_general()
    finally:
        await rig.stop()


async def test_a_stop_whose_reply_is_lost_is_adopted_not_placed_twice(tmp_path):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(timeout_fault("accept_then_hang", "algo"))
        record = await rig.open_strategy()
        assert record.state is IntentState.COMPLETED
        filled_ms = fill_time_ms(rig, record.id)
        assert len(rig.system_stops()) == 1  # 交易所已经建了，内核还不知道
        waited = await rig.run_until(
            lambda: rig.position().protection_state is ProtectionState.SYSTEM, limit_s=STOP_DEADLINE_S)
        assert waited is not None
        assert rig.now_ms() - filled_ms <= STOP_DEADLINE_S * 1000
        await rig.step(70)
        stops = rig.system_stops()
        assert len(stops) == 1 and len(rig.exchange_algos()) == 1  # 没有第二张止损
        assert rig.position().stop_client_id == stops[0]["client_algo_id"]
        rig.assert_general()
    finally:
        await rig.stop()


async def test_a_stop_that_was_never_accepted_is_placed_again_within_30_seconds(tmp_path):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(timeout_fault("drop", "algo"))
        record = await rig.open_strategy()
        filled_ms = fill_time_ms(rig, record.id)
        assert not rig.has_stop()
        waited = await rig.run_until(rig.has_stop, limit_s=STOP_DEADLINE_S)
        assert waited is not None, "第一次建单没被受理，30 秒内应当重试挂上"
        assert rig.now_ms() - filled_ms <= STOP_DEADLINE_S * 1000
        await rig.step(70)
        assert len(rig.system_stops()) == 1
        assert rig.position().protection_state is ProtectionState.SYSTEM
        rig.assert_general()
    finally:
        await rig.stop()


async def _terminal(rig: Rig, intent_id: int) -> bool:
    intent = await rig.kernel.intents.get(intent_id)
    return intent.state in (IntentState.COMPLETED, IntentState.REJECTED, IntentState.EXPIRED, IntentState.CANCELLED)


@pytest.mark.parametrize("mode", ["accept_then_hang", "drop"])
async def test_a_trailing_update_whose_new_stop_reply_is_lost_keeps_the_gap_within_stop_gap_max_s(tmp_path, mode):
    """移动止损（gap 模式）时新止损的应答丢了：``accept_then_hang`` 交易所其实建了（回查认领，不挂第二张），``drop`` 没建
    （确认不在后补挂）。两种都要求无保护间隙不超过 ``stop_gap_max_s``，最后只有一张系统止损。"""
    rig = await Rig.start(tmp_path)
    try:
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        old = rig.system_stop()
        rig.standin.disp.switches.update(timeout_fault(mode, "algo"))
        await trail_up(rig)
        assert rig.standin.disp.switches.fault("order_timeout")["enabled"] is False  # 故障确实打在这次更新上
        gap, present = await watch_gap(rig, STOP_GAP_MAX_S + 30)
        assert gap <= STOP_GAP_MAX_S, f"移动止损的无保护间隙 {gap} 秒超过 stop_gap_max_s"
        assert present == 1 and len(rig.system_stops()) == 1
        assert rig.system_stop()["client_algo_id"] != old["client_algo_id"]
        assert rig.position() is not None and rig.position().protection_state is ProtectionState.SYSTEM
        await rig.step(70)
        assert len(rig.system_stops()) == 1
        rig.assert_general()
    finally:
        await rig.stop()
