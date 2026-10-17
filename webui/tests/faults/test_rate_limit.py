"""故障 8：限频被拒（替身 ``rate_limit``：``http`` 429/418，``scope`` reads/writes/all，``count`` 次后自动关闭）。

被拒的请求交易所一概没有执行（替身在路由之前就返回 −1003）。内核侧：读被拒是 ``Unknown``（不当成空、不清登记、不撤保护），
写被拒是 ``WriteRejected(RATE_LIMITED)``（确定没下）；收到 429/418 后适配层对非保护类读请求退避，保护类读与写不受影响。

断言（报告 4.11 的通用标准）：持仓登记与交易所一致；同一个意图没有第二张订单；止损挂得上时从成交/接管登记到止损存在
不超过 30 秒；被拒期间已挂的止损不被撤、登记不被清。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import IntentState, Ownership, ProtectionState
from tests.faults.kit import Rig, SYM, fill_time_ms

STOP_DEADLINE_S = 30


def limited(scope: str, count: int | None, http: int = 429) -> dict:
    return {"faults": {"rate_limit": {"enabled": True, "http": http, "scope": scope, "count": count}}}


def calm() -> dict:
    return {"faults": {"rate_limit": {"enabled": False}}}


@pytest.mark.parametrize("http", [429, 418])
async def test_a_rejected_open_order_is_not_resent_and_leaves_nothing_behind(tmp_path, http):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(limited("writes", 1, http))
        record = await rig.open_strategy()
        await rig.step(30)
        intent = await rig.kernel.intents.get(record.id)
        assert intent.state is IntentState.REJECTED  # 被拒就是确定没下：意图结束，不重发
        assert rig.exchange_orders() == []  # 被拒的开仓单交易所没有执行，内核也没有重发
        assert rig.exchange_positions() == {} and rig.position() is None
        rig.assert_general()
        # 退避过去之后（429 到下一分钟，418 缺省 120 秒；退避期间开仓前的非保护类读不发，开仓会被拒）照常开仓并保护
        await rig.step(130)
        again = await rig.open_strategy()
        assert again.state is IntentState.COMPLETED
        waited = await rig.run_until(rig.has_stop, limit_s=STOP_DEADLINE_S)
        assert waited is not None and rig.now_ms() - fill_time_ms(rig, again.id) <= STOP_DEADLINE_S * 1000
        rig.assert_general()
    finally:
        await rig.stop()


@pytest.mark.parametrize("http", [429, 418])
async def test_a_rejected_stop_for_a_takeover_is_retried_and_exists_within_30_seconds(tmp_path, http):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(limited("writes", 1, http))  # 手动开仓不受限；接管后的第一次挂止损被拒
        taken_ms = rig.now_ms()
        pos = await rig.take_over_manual()
        assert pos.ownership is Ownership.TAKEOVER
        assert not rig.has_stop()
        waited = await rig.run_until(rig.has_stop, limit_s=STOP_DEADLINE_S)
        assert waited is not None, "限频拒掉的止损要在 30 秒内重试挂上"
        assert rig.now_ms() - taken_ms <= STOP_DEADLINE_S * 1000
        await rig.step(5)
        assert rig.position().protection_state is ProtectionState.SYSTEM
        await rig.step(60)
        assert len(rig.system_stops()) == 1
        rig.assert_general()
    finally:
        await rig.stop()


async def test_while_the_reads_are_backing_off_a_takeover_is_still_protected_within_30_seconds(tmp_path):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(limited("reads", 1))
        await rig.run_jobs("reconcile_positions")  # 这一轮的读被拒（429）：适配层对非保护类读退避到下一分钟
        assert rig.standin.disp.switches.fault("rate_limit")["enabled"] is False  # 那一次已经用掉，之后交易所不再拒
        # 退避期间开仓：开仓前读最新价是非保护类读，不发、直接放弃开仓（意图被拒，交易所上没有订单）
        record = await rig.open_strategy()
        assert record.state is IntentState.REJECTED and "限频" in record.state_reason
        assert rig.exchange_orders() == []
        # 退避期间接管一个手动仓：算止损的现价改用持仓里的标记价（保护类读），30 秒内止损存在
        taken_ms = rig.now_ms()
        pos = await rig.take_over_manual()
        waited = await rig.run_until(rig.has_stop, limit_s=STOP_DEADLINE_S)
        assert waited is not None, "非保护类读在退避，保护不能因此耽误"
        assert rig.now_ms() - taken_ms <= STOP_DEADLINE_S * 1000
        await rig.step(3)
        assert rig.position().id == pos.id and rig.position().protection_state is ProtectionState.SYSTEM
        await rig.step(70)
        assert len(rig.system_stops()) == 1
        rig.assert_general()
    finally:
        await rig.stop()


@pytest.mark.parametrize("http", [429, 418])
async def test_while_every_request_is_rejected_nothing_is_cleared_or_cancelled(tmp_path, http):
    rig = await Rig.start(tmp_path)
    try:
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        stop = rig.system_stop()
        pos = rig.position()
        rig.standin.disp.switches.update(limited("all", None, http))
        await rig.step(90)  # 对账、条件单对账、补拉都被拒：全是「未知」
        assert rig.system_stop()["client_algo_id"] == stop["client_algo_id"]  # 止损没被撤、也没另挂
        assert rig.position() is not None and rig.position().id == pos.id and rig.position().qty == pos.qty
        assert rig.position().protection_state is ProtectionState.SYSTEM
        rig.standin.disp.switches.update(calm())
        await rig.step(130)  # 418 的退避缺省 120 秒
        assert rig.system_stop()["client_algo_id"] == stop["client_algo_id"]
        assert rig.position().protection_state is ProtectionState.SYSTEM
        rig.assert_general()
        assert {(SYM, "LONG")} == set(rig.exchange_positions())
    finally:
        await rig.stop()


async def test_a_trailing_update_rejected_by_the_rate_limit_keeps_the_old_stop(tmp_path):
    """移动止损（gap 模式先撤旧）时撤旧单被限频拒绝：旧止损留着（不出现无保护间隙），交易所上仍只有一张系统止损。"""
    from tests.faults.kit import STOP_GAP_MAX_S, trail_up, watch_gap

    rig = await Rig.start(tmp_path)
    try:
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        rig.standin.disp.switches.update(limited("writes", 1))
        await trail_up(rig)
        assert rig.standin.disp.switches.fault("rate_limit")["enabled"] is False  # 故障确实打在这次更新上
        gap, present = await watch_gap(rig, 60)
        assert gap <= STOP_GAP_MAX_S and present == 1
        assert len(rig.system_stops()) == 1
        assert rig.position().protection_state is ProtectionState.SYSTEM
        rig.assert_general()
    finally:
        await rig.stop()
