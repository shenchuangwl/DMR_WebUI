"""故障 6：停机期间止损触发。内核被杀（不走关停），停机期间用 ``/dev/price_override`` 让价格越过止损，替身照常触发、平掉该侧；
再用同一账本、同一替身重启，跑真实的重启恢复。

断言：重启后登记簿把这个仓记为已平（止损出场），交易所与登记簿都没有持仓；没有残留的系统条件单；内核没有为这个仓再发任何单
（不补止损、不重复平仓）；同一个意图没有第二张订单；之后一分钟里的对账也不再变化。策略仓与接管仓各一例。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import ExitReason, IntentState, PositionStatus
from tests.faults.kit import D, Rig, SYM

DOWNTIME_MS = 30_000


@pytest.mark.parametrize("kind", ["strategy", "takeover"])
async def test_a_stop_that_fired_while_the_kernel_was_down_is_booked_as_a_stop_exit(tmp_path, kind):
    rig = await Rig.start(tmp_path)
    if kind == "strategy":
        assert (await rig.open_strategy()).state is IntentState.COMPLETED
    else:
        await rig.take_over_manual()
    assert await rig.run_until(rig.has_stop, limit_s=30) is not None
    position_id = rig.position().id
    stop_price = D(rig.system_stop()["trigger_price"])
    await rig.kill()

    rig.clock.advance(DOWNTIME_MS // 2)
    await rig.manual("/dev/price_override", {"symbol": SYM, "price": str(stop_price - 100)})
    rig.standin.disp.tick()  # 替身每秒检查一次触发
    assert rig.exchange_positions() == {}  # 停机期间止损触发、平掉了
    assert rig.open_algos() == []
    rig.clock.advance(DOWNTIME_MS // 2)
    old_transport = rig.standin.transport
    requests_before = len(old_transport.sent("POST"))

    rig2 = await rig.restart()
    try:
        await rig2.step(70)
        closed = await rig2.kernel.registry.fetch(position_id)
        assert closed.status is PositionStatus.CLOSED
        assert closed.exit_reason is ExitReason.STOP
        assert rig2.position() is None
        assert rig2.open_algos() == []
        writes = [r for r in rig2.standin.transport.sent("POST") if r[1].startswith("/api/private/") and
                  r[1] not in ("/api/private/listen_key",)]
        assert writes == []  # 新内核没有为这个仓发任何单
        assert len(old_transport.sent("POST")) == requests_before  # 旧内核被杀后也没再发
        rig2.assert_general()
    finally:
        await rig2.stop()
