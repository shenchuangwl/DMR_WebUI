"""故障 5：部分成交（替身 ``partial_fill``：市价单先成交 ``ratio``，余量 ``delay_s`` 秒后成交（``later``）或被作废（``expire``，
原始状态 EXPIRED_IN_MATCH））。

断言：开仓部分成交后，从（第一笔）成交到止损存在不超过 30 秒；余量成交后登记簿跟上；余量作废时按实际成交量登记；
平仓单部分成交时，余量成交后仓位归零并清理止损，余量作废时剩下的仓仍登记着、仍有止损；同一个意图在交易所只有一张单（余量不另发单）；
持仓登记与交易所一致。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.types import IntentAction, IntentSource, IntentState, PositionStatus, ProtectionState
from tests.faults.kit import D, LONG, Rig, SYM, next_key

STOP_DEADLINE_S = 30


def partial(remainder: str, count: int = 1) -> dict:
    return {"faults": {"partial_fill": {"enabled": True, "ratio": "0.5", "remainder": remainder, "delay_s": 5,
                                        "count": count}}}


def first_fill_ms(rig: Rig) -> int:
    orders = rig.exchange_orders()
    assert len(orders) == 1
    return int(orders[0]["create_ms"])


@pytest.mark.parametrize("remainder", ["later", "expire"])
async def test_a_partly_filled_open_is_protected_in_time_and_registered_at_the_filled_quantity(tmp_path, remainder):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(partial(remainder))
        record = await rig.open_strategy()
        filled_ms = first_fill_ms(rig)
        half = rig.exchange_positions()[(SYM, "LONG")]
        assert rig.exchange_orders()[0]["status"] == "PARTIALLY_FILLED"
        waited = await rig.run_until(rig.has_stop, limit_s=STOP_DEADLINE_S)
        assert waited is not None and rig.now_ms() - filled_ms <= STOP_DEADLINE_S * 1000
        await rig.step(70)  # 余量 5 秒后成交或作废，之后各对账都跑过
        full = rig.exchange_positions()[(SYM, "LONG")]
        assert full == (half * 2 if remainder == "later" else half)
        pos = rig.position()
        assert pos.qty == full and pos.protection_state is ProtectionState.SYSTEM
        assert len(rig.system_stops()) == 1  # 全平型止损一张就保护了整个仓（余量成交不另挂）
        intent = await rig.kernel.intents.get(record.id)
        assert intent.state is IntentState.COMPLETED
        assert len(rig.exchange_orders()) == 1  # 余量不另发单
        rig.assert_general()
    finally:
        await rig.stop()


@pytest.mark.parametrize("remainder", ["later", "expire"])
async def test_a_partly_filled_close_leaves_the_registry_matching_the_exchange(tmp_path, remainder):
    rig = await Rig.start(tmp_path)
    try:
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        assert rig.has_stop()
        pos = rig.position()
        rig.standin.disp.switches.update(partial(remainder))
        submitted = await rig.kernel.coordinator.submit(IntentSpec(
            action=IntentAction.CLOSE, model="op", source=IntentSource.OPERATOR, symbol=SYM, side=LONG,
            idem_key=next_key("close"), reason="故障注入：平仓部分成交", position_id=pos.id))
        await rig.kernel.coordinator.wait_terminal(submitted.intent_id, 1.0)
        await rig.settle()
        assert rig.exchange_positions() == {(SYM, "LONG"): pos.qty / 2}
        await rig.step(70)
        closes = [o for o in rig.exchange_orders() if o["side"] == "SELL"]
        assert len(closes) == 1  # 同一个平仓意图只有一张单
        if remainder == "later":
            assert rig.exchange_positions() == {}
            assert (await rig.kernel.registry.fetch(pos.id)).status is PositionStatus.CLOSED
            assert rig.open_algos() == []  # 平掉之后残留止损被清理
        else:
            assert rig.exchange_positions() == {(SYM, "LONG"): D(str(pos.qty / 2))}
            rest = rig.position()
            assert rest is not None and rest.id == pos.id and rest.qty == pos.qty / 2
            assert rest.status is PositionStatus.MANAGED  # 没有卡在「平仓中」
            assert rig.has_stop()  # 剩下的仓仍有止损
        rig.assert_general()
    finally:
        await rig.stop()
