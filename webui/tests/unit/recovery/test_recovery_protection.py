"""重启恢复第 5 步（补保护）：登记仓、快照里数量非零、且同合约同方向没有登记的那张带系统前缀止损类条件单的，一律交给
``protection.rebuild()``（它认领现成的、不会补出第二张、读不到交易所返回 unknown）。各仓并发，一个出错不挡别的。"""
from __future__ import annotations

import asyncio

import pytest

from dmr_kernel.core.recovery import Recovery
from dmr_kernel.core.types import AlgoType, PositionStatus, ProtectionState, Suppression
from recovery_kit import LONG, SHORT, SYM, SYM2, algo, pos

RECORDED = ("DKT-0-1-s", "9001", "95")


def rebuilt_ids(env) -> list[int]:
    return sorted(pid for pid, _ in env.protection.rebuilt)


async def test_a_registered_position_without_a_system_stop_is_rebuilt(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = [pos()]
    result = await env.run()
    assert env.protection.rebuilt == [(rec.id, "recovery")]
    assert result.steps["protection"]["rebuilt"] == [
        {"position_id": rec.id, "symbol": SYM, "side": "LONG", "result": "protected"}]
    assert result.steps["summary"]["stops_rebuilt"] == 1


async def test_a_position_whose_recorded_system_stop_is_on_the_exchange_is_left_alone(make_env):
    env = await make_env().boot()
    await env.add_position(stop=RECORDED)
    env.positions = [pos()]
    env.algos = [algo(cid=RECORDED[0], algo_id=RECORDED[1])]
    await env.run()
    assert env.protection.rebuilt == []


async def test_a_system_stop_the_registry_does_not_know_is_handed_to_rebuild_so_it_gets_adopted(make_env):
    """已登记而保护状态记着「无保护」，交易所上其实已有系统止损（挂上了没来得及记）：交给 rebuild 认领，不会补出第二张。"""
    env = await make_env().boot()
    rec = await env.add_position()  # 保护状态：无保护
    env.positions = [pos()]
    env.algos = [algo(cid="DKT-0-7-s", algo_id="7001")]
    await env.run()
    assert rebuilt_ids(env) == [rec.id]


async def test_a_different_system_stop_than_the_recorded_one_is_handed_to_rebuild(make_env):
    env = await make_env().boot()
    rec = await env.add_position(stop=RECORDED)
    env.positions = [pos()]
    env.algos = [algo(cid="DKT-0-8-s", algo_id="7002")]  # 登记的那张不在，另有一张
    await env.run()
    assert rebuilt_ids(env) == [rec.id]


@pytest.mark.parametrize("status", ["managed", "taking_over", "closing"])
async def test_every_registered_status_is_protected(make_env, status):
    env = await make_env().boot()
    rec = await env.add_position(status=status)
    env.positions = [pos()]
    await env.run()
    assert rebuilt_ids(env) == [rec.id]


@pytest.mark.parametrize("status", ["pending", "ignored"])
async def test_pending_and_ignored_positions_are_not_protected(make_env, status):
    env = await make_env().boot()
    await env.add_position(status=status)
    env.positions = [pos()]
    await env.run()
    assert env.protection.rebuilt == []


async def test_a_position_that_is_flat_on_the_exchange_is_not_protected(make_env):
    env = await make_env().boot()
    await env.add_position()
    env.positions = []
    await env.run()
    assert env.protection.rebuilt == []


async def test_only_system_prefixed_open_stop_kind_orders_on_the_same_side_count_as_covering(make_env):
    """别的方向、别的合约、止盈类、用户自己的、已经不在挂的条件单，都不算这个仓有系统止损。"""
    env = await make_env().boot()
    rec = await env.add_position(stop=RECORDED)
    env.positions = [pos()]
    env.algos = [
        algo(cid=RECORDED[0], algo_id=RECORDED[1], side=SHORT),  # 另一个方向
        algo(SYM2, LONG, cid=RECORDED[0], algo_id="1"),  # 另一个合约
        algo(cid=RECORDED[0], algo_id="2", algo_type=AlgoType.TAKE_PROFIT_MARKET),  # 止盈
        algo(cid="web_1", algo_id="3"),  # 用户自己的
        algo(cid=RECORDED[0], algo_id=RECORDED[1], raw_status="FINISHED"),  # 已经不在挂
    ]
    await env.run()
    assert rebuilt_ids(env) == [rec.id]


async def test_a_users_own_stop_does_not_count_so_rebuild_decides_whether_it_is_external_protection(make_env):
    """接管仓上用户自己的止损：恢复不替保护单管理判断，一律交给 rebuild（它会认「外部保护」而不再另挂）。"""
    env = await make_env().boot()
    rec = await env.add_position(status="taking_over")
    env.positions = [pos()]
    env.algos = [algo(cid="web_9", algo_id="5")]
    await env.run()
    assert rebuilt_ids(env) == [rec.id]


async def test_rebuilds_run_concurrently_so_many_positions_are_protected_within_the_time_target(make_env):
    env = await make_env().boot()
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    for symbol in symbols:
        await env.add_position(symbol=symbol)
    env.positions = [pos(symbol) for symbol in symbols]
    barrier = asyncio.Barrier(len(symbols))  # 三个重建都到齐才放行：串行执行的话第一个就永远等下去
    env.protection.gate = barrier.wait
    await asyncio.wait_for(env.run(), 5)
    assert len(env.protection.rebuilt) == 3


async def test_an_unknown_answer_is_retried_a_few_times(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = [pos()]
    env.protection.results[rec.id] = ["unknown", "unknown", "protected"]
    result = await env.run()
    assert len(env.protection.rebuilt) == 3
    assert result.steps["protection"]["rebuilt"][0]["result"] == "protected"
    assert result.tradable is True


async def test_a_position_that_stays_unknown_keeps_the_recovering_suppression(make_env):
    """补保护时一直读不到交易所：这个仓有没有保护是未知的，不放开交易。"""
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = [pos()]
    env.protection.results[rec.id] = "unknown"
    result = await env.run()
    assert len(env.protection.rebuilt) == Recovery.REBUILD_ATTEMPTS
    assert result.steps["protection"]["unknown"][0]["position_id"] == rec.id
    assert result.tradable is False and any("读不到交易所" in p for p in result.problems)
    assert Suppression.RECOVERING in env.suppressions()


async def test_a_failed_attempt_to_protect_is_reported_by_protection_itself_and_is_not_a_recovery_problem(make_env):
    """rebuild 返回 unprotected：保护单管理已经置了「无保护」抑制并发了最高级告警，恢复只如实记下。"""
    env = await make_env().boot()
    rec = await env.add_position(status="taking_over")
    env.positions = [pos()]
    env.protection.results[rec.id] = "unprotected"
    result = await env.run()
    assert result.steps["protection"]["rebuilt"][0]["result"] == "unprotected"
    assert result.tradable is True


async def test_a_rebuild_that_raises_is_recorded_and_blocks_trading(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = [pos()]
    env.protection.results[rec.id] = RuntimeError("保护单管理出错")
    result = await env.run()
    assert result.steps["protection"]["errors"][0]["position_id"] == rec.id
    assert result.tradable is False


async def test_nothing_is_protected_without_a_snapshot(make_env):
    env = await make_env().boot()
    await env.add_position()
    env.fail("fetch_open_algo_orders")
    result = await env.run()
    assert env.protection.rebuilt == []
    assert result.steps["protection"]["skipped"] == "没有快照"


async def test_short_timeframe_candles_are_prepared_before_any_rebuild(make_env):
    """补保护要算初始止损（极值 ± ATR）：K 线要先备好，否则只能退回按 A% 设置。"""
    env = await make_env().boot()
    await env.add_position()
    env.positions = [pos()]
    await env.run()
    sync = env.index_of("candles.sync")
    refresh = next(i for i, c in enumerate(env.calls) if isinstance(c, tuple) and c[0] == "candles.refresh")
    rebuild = env.index_of("rebuild")
    assert sync < refresh < rebuild
    assert env.calls[refresh][2] == "15m"


async def test_the_status_of_a_position_taken_over_is_not_changed_by_recovery_itself(make_env):
    """接管中的补挂成功后转托管中是保护单管理的事；恢复自己不动持仓状态。"""
    env = await make_env().boot()
    rec = await env.add_position(status="taking_over")
    env.positions = [pos()]
    await env.run()
    assert env.registry.by_id(rec.id).status is PositionStatus.TAKING_OVER
    assert env.registry.by_id(rec.id).protection_state is ProtectionState.NONE
