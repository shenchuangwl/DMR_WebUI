"""M2B（一键全平、模型切换）对进程内替身交易所：真实的协调者、意图、订单、登记簿、账本；操作服务是真实的 ``CloseAll`` / ``Switch``。

只验证「接得上」：真实协调者下逐仓平仓走完、替身交易所上持仓归零、锁释放；切换两种方式都落到新模型的已停止。
时序、时限、各种结局由 ``tests/unit/ops_m2b`` 的假世界覆盖。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from dmr_kernel.core.types import Gate, Model, OperationStatus, OpLock, PositionSide
from dmr_kernel.ops.close_all import CloseAll
from dmr_kernel.ops.common import Caller, OpsDeps
from dmr_kernel.ops.operations import Operations
from dmr_kernel.ops.switch import Switch
from tests.integration.test_coordinator_standin import World, make_world, spec, world  # noqa: F401 (夹具)

LONG = PositionSide.LONG
SYMBOLS = ("BTCUSDT", "ETHUSDT", "DOGEUSDT")


def services(world: World):
    operations = Operations(world.ledger, world.clock, world.bus)
    deps = OpsDeps(clock=world.clock, modelstate=world.modelstate, registry=world.registry, operations=operations,
                   config_get=world.config.__getitem__, models={}, coordinator=world.coordinator,
                   adapter=world.adapter, rules=world.rules, orders=world.orders)
    close_all = CloseAll(deps, intents=world.intents, bus=world.bus, crash=world.crash, poll_s=0.01)
    switch = Switch(deps, intents=world.intents, close_all=close_all, bus=world.bus, crash=world.crash, poll_s=0.01)
    return operations, close_all, switch


def session(world: World) -> Caller:
    return Caller(operator="alice", session_id=1, is_session=True, holds_seat=True, reauth_ms=world.clock.now_ms())


async def open_positions(world: World, symbols=SYMBOLS) -> None:
    for symbol in symbols:
        await world.go(spec(symbol=symbol))
    assert {key[0] for key in await world.exchange_positions()} >= set(symbols)


async def exchange_flat(world: World) -> bool:
    return all(pos.qty == Decimal(0) for pos in (await world.exchange_positions()).values())


async def test_close_all_closes_every_managed_position_on_the_standin(world):
    await world.start()
    await open_positions(world)
    operations, close_all, _ = services(world)
    version = world.modelstate.current().version
    record, is_new = await close_all.start(session(world), op_id="ca-1", scope="all_managed", model_version=version)
    assert is_new and record.status is OperationStatus.RUNNING
    await asyncio.wait_for(close_all.wait("ca-1"), 30)
    await world.settle()
    done = await operations.get("ca-1")
    assert done.status is OperationStatus.SUCCEEDED
    assert {row["symbol"]: row["result"] for row in done.per_position} == {s: "closed" for s in SYMBOLS}
    assert await exchange_flat(world)
    state = world.modelstate.current()
    assert state.gate is Gate.STOPPED and state.op_locks == frozenset()
    assert all(world.registry.get_active(s, LONG) is None for s in SYMBOLS)


async def test_the_default_scope_of_model_1_closes_its_strategy_positions(world):
    await world.start()
    await open_positions(world, SYMBOLS[:2])
    _, close_all, _ = services(world)
    preview = await close_all.preview("model")
    assert {rec.symbol for rec in preview["included"]} == set(SYMBOLS[:2]) and preview["mixed_included"] == []


async def test_switch_keep_moves_to_the_new_model_stopped_and_leaves_positions_alone(world):
    await world.start()
    await open_positions(world, SYMBOLS[:1])
    operations, _, switch = services(world)
    shown = await switch.preview(session(world), op_id="pv-1", target="m2")
    assert shown["counts_by_ownership"]["strategy"] == 1
    record, payload = await switch.confirm(session(world), op_id="cf-1", model_version=shown["model_version"],
                                           mode="keep")
    assert record.status is OperationStatus.SUCCEEDED and payload["active_model"] == "m2"
    state = world.modelstate.current()
    assert state.active_model is Model.M2 and state.gate is Gate.STOPPED and state.op_locks == frozenset()
    assert not await exchange_flat(world)  # 持仓没动
    assert world.registry.get_active(SYMBOLS[0], LONG) is not None


async def test_switch_close_all_first_closes_everything_and_then_switches(world):
    await world.start()
    await open_positions(world)
    operations, _, switch = services(world)
    shown = await switch.preview(session(world), op_id="pv-1", target="m3")
    record, payload = await switch.confirm(session(world), op_id="cf-1", model_version=shown["model_version"],
                                           mode="close_all_first")
    assert payload["status"] == "running"
    await asyncio.wait_for(switch.wait("cf-1"), 30)
    await world.settle()
    final = await operations.get("cf-1")
    assert final.status is OperationStatus.SUCCEEDED and final.result["active_model"] == "m3"
    assert await exchange_flat(world)
    assert (await operations.get("cf-1:ca")).status is OperationStatus.SUCCEEDED
    state = world.modelstate.current()
    assert state.active_model is Model.M3 and state.gate is Gate.STOPPED and OpLock.SWITCHING not in state.op_locks


async def test_a_preview_blocks_opens_and_cancel_lets_them_through_again(world):
    await world.start()
    _, _, switch = services(world)
    await switch.preview(session(world), op_id="pv-1", target="m2")
    allowed, why = world.modelstate.current().can_open(Model.M1)
    assert allowed is False and "切换预览中" in why
    assert await switch.cancel(session(world), op_id="cx-1") == {"cancelled": True}
    assert world.modelstate.current().op_locks == frozenset()
