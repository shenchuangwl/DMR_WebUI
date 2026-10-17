"""ops.test_entry：替身环境专用的测试造仓（TEST_ENTRY 开仓意图，绕过开仓闸与名单）。

覆盖：成功造仓并登记为策略仓；闸停止、有抑制原因时照样可造；实盘拒绝；参数校验（合约、方向、金额）；
金额低于最小名义金额、该方向已有仓时意图被拒（结果 failed 并带原因）；席位；同一操作号只执行一次。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.types import (
    Gate,
    IntentAction,
    IntentSource,
    OperationStatus,
    OperationType,
    Ownership,
    PositionSide,
    Suppression,
)
from dmr_kernel.ops.common import Caller, OpRejected
from dmr_kernel.ops.test_entry import TestEntry as Entry
from tests.support.kernel import settle
from tests.unit.ops_m2a.m2a_kit import make_deps, rig

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
SESSION = Caller("alice", 7, True, True, None)
NOSEAT = Caller("bob", 8, True, False, None)
CLI = Caller.cli()


async def reject(coro, status=None, code=None):
    with pytest.raises(OpRejected) as caught:
        await coro
    if status is not None:
        assert caught.value.status == status
    if code is not None:
        assert caught.value.code == code
    return caught.value


async def register(r):
    await r.kernel.jobs["backfill_trades"]()
    await settle(r.kernel)


async def test_test_open_builds_a_strategy_position_without_the_gate(tmp_path):
    async with rig(tmp_path) as r:
        assert r.kernel.modelstate.current().gate is Gate.STOPPED
        svc = Entry(make_deps(r.kernel))
        response = await svc.open(SESSION, op_id="t-1", symbol="BTCUSDT", side="LONG", notional=D("400"))
        assert response["op_id"] == "t-1" and response["status"] == "succeeded"
        assert set(response["result"]) >= {"intent_id", "state"} and response["result"]["state"] == "COMPLETED"
        intent = await r.kernel.intents.get(response["result"]["intent_id"])
        assert intent.action is IntentAction.OPEN and intent.source is IntentSource.TEST_ENTRY
        assert intent.symbol == "BTCUSDT" and intent.side is LONG and intent.operator == "alice"
        assert intent.op_id == "t-1" and D(str(intent.params["notional"])) == D("400")
        await register(r)
        position = r.kernel.registry.get_active("BTCUSDT", LONG)
        assert position.ownership is Ownership.STRATEGY and position.qty == D("0.008")
        record = await r.kernel.operations.get("t-1")
        assert record.op_type is OperationType.TEST_OPEN and record.status is OperationStatus.SUCCEEDED


async def test_test_open_works_for_shorts_and_under_a_suppression(tmp_path):
    async with rig(tmp_path) as r:
        await r.kernel.modelstate.add_suppression(Suppression.RECONCILE_UNKNOWN, reason="t")
        svc = Entry(make_deps(r.kernel))
        response = await svc.open(CLI, op_id="t-1", symbol="ETHUSDT", side="short", notional="200")
        assert response["result"]["state"] == "COMPLETED"
        await register(r)
        assert r.kernel.registry.get_active("ETHUSDT", SHORT) is not None


async def test_test_open_is_refused_outside_the_standin_environment(tmp_path):
    async with rig(tmp_path) as r:
        svc = Entry(make_deps(r.kernel, is_live=True))
        error = await reject(svc.open(SESSION, op_id="t-1", symbol="BTCUSDT", side="LONG", notional=D("400")), 409,
                             "not_allowed")
        assert "非实盘" in error.message
        assert await r.kernel.operations.get("t-1") is None
        assert await r.exchange_qty() == D("0")


@pytest.mark.parametrize("kwargs", [
    {"symbol": "NOPEUSDT"}, {"symbol": ""}, {"side": "BOTH"}, {"side": ""}, {"notional": D("0")},
    {"notional": D("-5")}, {"notional": "abc"}, {"notional": "NaN"}, {"notional": D("Infinity")}])
async def test_test_open_validates_its_arguments(tmp_path, kwargs):
    async with rig(tmp_path) as r:
        args = dict(op_id="t-1", symbol="BTCUSDT", side="LONG", notional=D("400"))
        args.update(kwargs)
        await reject(Entry(make_deps(r.kernel)).open(SESSION, **args), None, None)
        assert await r.kernel.operations.get("t-1") is None
        assert await r.exchange_qty() == D("0")


async def test_a_notional_below_the_minimum_gets_an_intent_rejection_reported_as_failed(tmp_path):
    async with rig(tmp_path) as r:
        response = await Entry(make_deps(r.kernel)).open(SESSION, op_id="t-1", symbol="BTCUSDT", side="LONG",
                                                         notional=D("20"))
        assert response["status"] == "failed" and response["result"]["state"] == "REJECTED"
        assert response["result"]["reason"]
        assert (await r.kernel.operations.get("t-1")).status is OperationStatus.FAILED
        assert await r.exchange_qty() == D("0")


async def test_a_second_open_on_the_same_side_is_rejected_by_the_coordinator(tmp_path):
    async with rig(tmp_path) as r:
        svc = Entry(make_deps(r.kernel))
        await svc.open(SESSION, op_id="t-1", symbol="BTCUSDT", side="LONG", notional=D("400"))
        await register(r)
        response = await svc.open(SESSION, op_id="t-2", symbol="BTCUSDT", side="LONG", notional=D("400"))
        assert response["status"] == "failed" and response["result"]["state"] == "REJECTED"
        assert await r.exchange_qty() == D("0.008")


async def test_the_seat_is_required_for_sessions(tmp_path):
    async with rig(tmp_path) as r:
        await reject(Entry(make_deps(r.kernel)).open(NOSEAT, op_id="t-1", symbol="BTCUSDT", side="LONG",
                                                     notional=D("400")), 409, "seat_required")
        assert await r.exchange_qty() == D("0")


async def test_the_same_operation_id_opens_only_once(tmp_path):
    async with rig(tmp_path) as r:
        svc = Entry(make_deps(r.kernel))
        first = await svc.open(SESSION, op_id="t-1", symbol="BTCUSDT", side="LONG", notional=D("400"))
        again = await svc.open(SESSION, op_id="t-1", symbol="BTCUSDT", side="LONG", notional=D("400"))
        assert again == first
        assert await r.exchange_qty() == D("0.008")
