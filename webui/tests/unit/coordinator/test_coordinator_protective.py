"""平仓与保护类意图在有抑制原因、闸关闭、操作锁、风控触发时照常执行；它们不占合约数名额，也不受订单频率限制。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import (
    CancelOutcome,
    CancelResult,
    IntentState,
    OpLock,
    OrderState,
    RiskDecision,
    Suppression,
)

from coordinator_kit import (
    LONG, SYM, SHORT, close_spec, fill_ok, order_cancel_spec, partial_spec, position, spec,
    stop_cancel_spec, stop_place_spec, stop_replace_spec,
)

S = IntentState


async def lock_everything(rig, *, running: bool = False):
    """闸关闭 + 抑制原因（含风控触发）+ 操作锁 + 风控闸说不 + 名单失效 + 快照过期。"""
    await rig.start(running=running)
    await rig.modelstate.add_suppression(Suppression.UNPROTECTED, reason="测试")
    await rig.modelstate.add_suppression(Suppression.RISK_TRIPPED, reason="测试")
    await rig.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner="test")
    rig.riskgate.decision = RiskDecision(False, "账户级触发", "account")
    rig.list_result = (False, "名单已失效")
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.clock.advance(3_600_000)  # 持仓与挂单快照早就过期了


CASES = {
    "close": (lambda rig: close_spec(), S.COMPLETED, ["create_market_order"]),
    "partial_close": (lambda rig: partial_spec("0.004"), S.ACTIVE, ["create_market_order"]),
    "stop_place": (lambda rig: stop_place_spec("45000"), S.COMPLETED, ["create_stop_order"]),
    "stop_replace": (lambda rig: stop_replace_spec("46000"), S.COMPLETED, ["cancel_algo_order", "create_stop_order"]),
    "stop_cancel": (lambda rig: stop_cancel_spec(), S.COMPLETED, ["cancel_algo_order"]),
}


@pytest.mark.parametrize("name", sorted(CASES))
async def test_closing_and_protective_intents_run_as_usual_when_everything_blocks_new_opens(rig, name):
    make, expected_state, expected_writes = CASES[name]
    await lock_everything(rig)
    if name == "close":
        rig.adapter.push_write_result("create_market_order", fill_ok())
    rec = await rig.go(make(rig))
    assert rec.state is expected_state, rec.state_reason
    assert rig.names() == expected_writes
    assert rig.riskgate.calls == [] and rig.list_calls == []  # 不新增风险的动作不过风控闸、不问名单
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 0  # 不占名额


async def test_order_cancel_also_runs_with_everything_blocked(rig):
    await lock_everything(rig)
    rig.adapter.push_write_result("cancel_order", CancelResult(CancelOutcome.NOT_FOUND))
    rec = await rig.go(order_cancel_spec("DKTxx-1-9-o"))
    assert rec.state is S.COMPLETED and rig.names() == ["cancel_order"]


async def test_the_same_conditions_do_stop_an_open(rig):
    await lock_everything(rig)
    rec = await rig.go(spec())
    assert rec.state is S.REJECTED and "开仓闸已停止" in rec.state_reason
    assert rig.names() == [] and rig.riskgate.calls == []


@pytest.mark.parametrize("model", ["m1", "m2", "m3", "op", "protection", "no-such-model"])
async def test_closing_does_not_care_which_model_asks(rig, model):
    await rig.start(running=False)
    rig.positions = [position(SYM, LONG, "0.01")]
    rec = await rig.go(close_spec(model=model))
    assert rec.state is S.ACTIVE and len(rig.sent("create_market_order")) == 1


async def test_the_order_rate_limit_does_not_hold_back_closes_and_stops_but_does_count_their_orders(make_rig):
    rig = make_rig(max_orders_per_symbol_10m=2)
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    await rig.go(close_spec())
    await rig.go(stop_place_spec("45000"))
    await rig.go(stop_place_spec("44000"))  # 同一合约 10 分钟内第 3 笔：保护类动作不受限
    assert len(rig.sent("create_stop_order")) == 2
    assert rig.limits.order_rate_ok(SYM, rig.clock.now_ms()) is False  # 每笔发出的订单都计数
    rig.positions = []
    rig.refresh_limits()
    assert (await rig.go(spec())).state is S.REJECTED  # 开仓则受限


async def test_a_full_symbol_cap_does_not_stop_a_close(make_rig):
    rig = make_rig(max_symbols=1)
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01"), position("ETHUSDT", LONG, "0.5", "3000")]
    rig.refresh_limits()
    rec = await rig.go(close_spec())
    assert rec.state is S.ACTIVE


async def test_a_close_is_not_blocked_by_a_stopped_risk_state_even_if_the_risk_gate_raises(rig):
    await rig.start()
    rig.riskgate.error = RuntimeError("账本忙")
    rig.positions = [position(SYM, SHORT, "0.01")]
    rec = await rig.go(close_spec(side=SHORT))
    assert rec.state is S.ACTIVE and rig.riskgate.calls == []
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.ACKNOWLEDGED
