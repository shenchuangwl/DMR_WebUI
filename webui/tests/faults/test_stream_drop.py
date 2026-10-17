"""故障 7：数据流断线（替身 ``stream_drop``：``close`` 关掉全部连接并在时长内拒绝重连；``silent`` 连接保持但不发任何帧）。

断言：断线期间开的仓，从成交到止损存在不超过 30 秒（成交回报不能等数据流，靠 REST 应答与补拉）；断线期间用户手动减仓、
手动撤掉系统止损，内核靠轮询（持仓对账、挂单对账）跟上：登记簿与交易所一致，止损在 90 秒内重新存在；断线结束后数据流恢复；
同一个意图没有第二张订单。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import IntentState, ProtectionState
from tests.faults.kit import Rig, SYM, fill_time_ms

STOP_DEADLINE_S = 30
MANUAL_CANCEL_DEADLINE_S = 90


def drop(mode: str, duration_s: int) -> dict:
    return {"faults": {"stream_drop": {"enabled": True, "mode": mode, "duration_s": duration_s}}}


@pytest.mark.parametrize("mode", ["close", "silent"])
async def test_a_position_opened_while_the_stream_is_down_is_protected_within_30_seconds(tmp_path, mode):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(drop(mode, 300))
        await rig.step(3)
        record = await rig.open_strategy()
        assert record.state is IntentState.COMPLETED  # 市价单的 REST 应答里就是已成交
        filled_ms = fill_time_ms(rig, record.id)
        waited = await rig.run_until(rig.has_stop, limit_s=STOP_DEADLINE_S)
        assert waited is not None, "数据流断着，成交后 30 秒内也要把止损挂上"
        assert rig.now_ms() - filled_ms <= STOP_DEADLINE_S * 1000
        assert rig.position().protection_state is ProtectionState.SYSTEM
        await rig.step(60)
        rig.assert_general()
    finally:
        await rig.stop()


@pytest.mark.parametrize("mode", ["close", "silent"])
async def test_while_the_stream_is_down_polling_keeps_the_registry_and_the_stop_in_line(tmp_path, mode):
    rig = await Rig.start(tmp_path)
    try:
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        assert rig.has_stop()
        pos = rig.position()
        rig.standin.disp.switches.update(drop(mode, 240))
        await rig.step(5)
        await rig.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "LONG",
                                               "qty": str(pos.qty / 2)})
        caught = await rig.run_until(lambda: rig.position().qty == pos.qty / 2, limit_s=30)
        assert caught is not None, "数据流断着，持仓对账也应当让登记簿跟上手动减仓"
        stop = rig.system_stop()
        await rig.manual("/dev/manual/cancel", {"symbol": SYM, "client_algo_id": stop["client_algo_id"]})
        assert not rig.has_stop()
        back = await rig.run_until(rig.has_stop, limit_s=MANUAL_CANCEL_DEADLINE_S)
        assert back is not None, "数据流断着，手动撤掉的系统止损也要在 90 秒内重新存在"
        assert rig.system_stop()["client_algo_id"] != stop["client_algo_id"]
        rig.assert_general()
        await rig.step(240)  # 断线窗口结束，数据流恢复
        assert rig.kernel.stream.state == "connected"
        rig.assert_general()
        assert len(rig.system_stops()) == 1
    finally:
        await rig.stop()


async def test_reconcile_backfills_a_filled_system_open_whose_fills_the_stream_never_delivered(tmp_path):
    """复现：数据流断着，开仓单 REST 应答已 FILLED、意图完成，但成交明细没入账，登记簿没有这个仓。持仓对账看到「交易所有、
    登记簿没有」且系统订单已成交而成交没入账时，要先补拉这个合约的成交（同数量不一致的处理），而不是干等 60 秒一次的补拉。"""
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(drop("close", 300))
        await rig.step(3)
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        assert rig.position() is None  # 成交还没入账
        await rig.run_jobs("reconcile_positions")
        assert rig.position() is not None, "持仓对账应当补拉成交，让登记簿登记这个系统仓"
    finally:
        await rig.stop()
