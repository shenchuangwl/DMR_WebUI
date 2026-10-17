"""故障 3：持仓查询失败（替身 ``position_query_fail``：``error`` 回 500，``hang`` 扣住不答直到客户端超时）。

原则（报告 F8）：读不到就是「未知」，不是 0 —— 不据此开仓、不据此判平仓、不改登记簿、不补挂第二张止损；恢复之后照常对上账。

断言：开仓前读不到持仓就放弃（交易所一张单都没有）；已有的仓在读不到的这段时间里登记簿不变、止损一直在、不发任何平仓单；
期间用户手动减仓，恢复之后登记簿跟上交易所；同一个意图没有第二张订单。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import IntentState, PositionStatus, ProtectionState
from tests.faults.kit import D, Rig, SYM

ORDER = "/api/private/order"


def query_fail(mode: str, count: int | None = None) -> dict:
    return {"faults": {"position_query_fail": {"enabled": True, "mode": mode, "count": count}}}


def recover(rig: Rig) -> None:
    rig.standin.disp.switches.update({"faults": {"position_query_fail": {"enabled": False}}})


@pytest.mark.parametrize("mode", ["error", "hang"])
async def test_an_open_is_given_up_when_the_position_cannot_be_read(tmp_path, mode):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(query_fail(mode))
        record = await rig.open_strategy()
        assert record.state is IntentState.REJECTED
        assert "持仓读数未知" in record.state_reason
        assert rig.exchange_orders() == [] and rig.position() is None
        recover(rig)
        again = await rig.open_strategy()  # 反例：读得到时照常开仓并挂上止损
        assert again.state is IntentState.COMPLETED
        assert await rig.run_until(rig.has_stop, limit_s=30) is not None
        rig.assert_general()
    finally:
        await rig.stop()


@pytest.mark.parametrize("mode", ["error", "hang"])
async def test_an_unreadable_position_changes_nothing_and_the_registry_catches_up_afterwards(tmp_path, mode):
    rig = await Rig.start(tmp_path)
    try:
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
        before = rig.position()
        stop_id = rig.system_stop()["client_algo_id"]
        orders_before = len(rig.standin.transport.sent("POST", ORDER))
        rig.standin.disp.switches.update(query_fail(mode))
        await rig.step(70)  # 七轮持仓对账、一轮挂单对账都读不到持仓
        during = rig.position()
        assert during is not None and during.id == before.id and during.status is PositionStatus.MANAGED
        assert during.qty == before.qty
        assert during.protection_state is ProtectionState.SYSTEM
        assert rig.system_stop()["client_algo_id"] == stop_id  # 止损没被撤、也没补出第二张
        assert len(rig.standin.transport.sent("POST", ORDER)) == orders_before  # 没有据「读不到」平仓
        # 期间用户在网页上手动减了一半：交易所变了，内核暂时看不到持仓
        await rig.manual("/dev/manual/order", {"symbol": SYM, "side": "SELL", "position_side": "LONG",
                                               "qty": str(before.qty / 2)})
        await rig.step(20)
        recover(rig)
        caught = await rig.run_until(lambda: rig.position().qty == before.qty / 2, limit_s=30)
        assert caught is not None, "持仓读得到之后，登记簿应当跟上交易所"
        assert rig.system_stop()["client_algo_id"] == stop_id  # 全平型止损照样保护剩下的仓
        await rig.step(60)
        rig.assert_general()
        assert rig.exchange_positions() == {(SYM, "LONG"): D(str(before.qty / 2))}
    finally:
        await rig.stop()
