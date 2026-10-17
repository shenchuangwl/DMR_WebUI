"""孤儿条件单（F8）：五条同时满足才经协调者 STOP_CANCEL 撤，每一条各有反例。

五条：带系统前缀；登记簿里没有任何活动持仓或在途意图引用它；没有被标为已移交；建立已超过一个条件单对账周期；
该合约该方向在最近一次（新鲜的）持仓快照里明确为零。持仓非零或未知时不撤，只告警。不带系统前缀的一律不撤。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import (
    AlertLevel, AlgoType, ExitReason, IntentAction, IntentSource, IntentState, ProtectionState,
)
from reconcile_kit import D, LONG, SHORT, SYM, SYM2, T0, algo, pos

PERIOD_S = 60  # reconcile_orders_s 的默认值


async def orphan_world(env, **stop_kwargs):
    """一张系统止损挂在交易所（意图已完成），登记簿里没有任何引用；再做一轮持仓对账，得到新鲜的、该键为零的快照。"""
    stop = await env.system_stop(algo_id="9001", **stop_kwargs)
    assert await env.reconciler.reconcile_positions() == "ok"
    return stop


async def cancelled(env, *, rounds: int = 1) -> list:
    for _ in range(rounds):
        assert await env.reconciler.reconcile_orders() == "ok"
    return env.coordinator.submitted


def kept(env):
    return env.alerts("orphan_stop_kept")


# ================================================================ 五条都满足：撤


async def test_an_orphan_is_cancelled_through_the_coordinator_with_a_stable_key(make_env):
    env = await make_env().boot()
    stop = await orphan_world(env)

    [spec] = await cancelled(env)

    assert (spec.action, spec.source, spec.model) == (IntentAction.STOP_CANCEL, IntentSource.RECONCILE, "op")
    assert (spec.symbol, spec.side, spec.position_id) == (SYM, LONG, None)
    assert spec.params == {"client_algo_id": stop.client_algo_id, "algo_id": "9001"}
    assert spec.idem_key == "orphan:9001"
    assert env.adapter.writes == [] and kept(env) == []  # 对账自己不写交易所；撤了就不再告警


async def test_the_same_orphan_is_not_submitted_again_while_its_cancel_is_in_flight(make_env):
    env = await make_env().boot()
    await orphan_world(env)
    await cancelled(env, rounds=3)
    assert len(env.coordinator.submitted) == 1


async def test_every_algo_type_can_be_an_orphan_and_only_open_ones_are(make_env):
    env = await make_env().boot()
    take_profit = await env.system_stop(algo_id="9003")
    old = T0 - 120_000
    env.algos = [algo(SYM, LONG, cid=take_profit.client_algo_id, algo_id="9003", create_ms=old,
                      algo_type=AlgoType.TAKE_PROFIT_MARKET),
                 algo(SYM, LONG, cid=env.own_id(intent=88), algo_id="9004", create_ms=old, raw_status="CANCELED"),
                 algo(SYM, LONG, cid=env.own_id(intent=89), algo_id="9005", create_ms=old, raw_status="FINISHED")]
    await env.reconciler.reconcile_positions()

    await cancelled(env)

    assert [s.params["algo_id"] for s in env.coordinator.submitted] == ["9003"]


async def test_several_orphans_are_each_cancelled_once(make_env):
    env = await make_env().boot()
    await env.system_stop(algo_id="9001")
    await env.system_stop(symbol=SYM2, side=SHORT, algo_id="9002")
    await env.reconciler.reconcile_positions()

    await cancelled(env, rounds=2)

    assert sorted((s.symbol, s.side, s.idem_key) for s in env.coordinator.submitted) == [
        (SYM, LONG, "orphan:9001"), (SYM2, SHORT, "orphan:9002")]


async def test_a_cancel_pending_order_row_that_is_still_on_the_exchange_is_still_an_orphan(make_env):
    # 撤单结果未知时订单行停在 CANCEL_PENDING，交易所里它还挂着：仍然有效，也仍然是没人认领的孤儿
    env = await make_env().boot()
    await orphan_world(env, cancel_pending=True)

    [spec] = await cancelled(env)
    assert spec.params["algo_id"] == "9001"


# ================================================================ 条件 1：系统前缀


@pytest.mark.parametrize("cid", [
    "web_abc123",                              # 用户在网页上手动挂的
    "DMR12_SHORT_buy_LONG_1760000000000",      # 旧程序的编号
    "x-cvBPrNm9abcdefgh",                      # ccxt 默认编号
    "autoclose-123",                           # 强平
    "DKE-abc-1",                               # 应急入口也不是系统编号
    None,                                      # 没有编号
])
async def test_condition_1_orders_without_the_system_prefix_are_never_cancelled(make_env, cid):
    env = await make_env().boot()
    env.algos = [algo(SYM, LONG, cid=cid, algo_id="9100", create_ms=T0 - 3_600_000)]
    await env.reconciler.reconcile_positions()

    await cancelled(env, rounds=2)

    assert env.coordinator.submitted == []
    assert kept(env) == []  # 外部单是正常的，也不告警


async def test_condition_1_a_system_id_of_another_instance_is_not_ours_when_the_instance_is_known(make_env):
    env = await make_env(instance_filter=True).boot()
    env.algos = [algo(SYM, LONG, cid=env.other_instance_id(), algo_id="9100", create_ms=T0 - 3_600_000)]
    await env.reconciler.reconcile_positions()

    await cancelled(env)
    assert env.coordinator.submitted == []


async def test_condition_1_without_a_known_instance_any_system_prefix_counts(make_env):
    env = await make_env().boot()
    env.algos = [algo(SYM, LONG, cid=env.other_instance_id(), algo_id="9100", create_ms=T0 - 3_600_000)]
    await env.reconciler.reconcile_positions()

    [spec] = await cancelled(env)
    assert spec.params["algo_id"] == "9100"


# ================================================================ 条件 2：登记簿或在途意图引用


@pytest.mark.parametrize("how", ["stop_cid", "stop_algo_id_only"])
@pytest.mark.parametrize("status", ["managed", "pending", "ignored"])
async def test_condition_2_an_active_position_whose_stop_it_is_blocks_the_cancel(make_env, how, status):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    rec = await env.add_position(status=status)
    if how == "stop_cid":
        await env.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("95"),
                                          stop_client_id=stop.client_algo_id, stop_algo_id=None)
    else:
        await env.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("95"),
                                          stop_client_id="DKx-0-zz-s", stop_algo_id="9001")
    await env.reconciler.reconcile_positions()  # 交易所此刻读到该键为零，登记仓还没判已平（要连续 3 次）

    await cancelled(env, rounds=2)

    assert env.coordinator.submitted == [] and kept(env) == []


@pytest.mark.parametrize("how", ["cid", "algo_id_only"])
async def test_condition_2_a_stale_entry_is_the_stale_cleanups_business_not_an_orphan(make_env, how):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    current = await env.system_stop(algo_id="9002", trigger="97")
    rec = await env.add_position(stop=(current.client_algo_id, "9002", "97"))  # 当前止损挂着：旧单才可以撤（复核补充）
    await env.registry.add_stale_stop(rec.id, client_algo_id=stop.client_algo_id if how == "cid" else None,
                                      algo_id=None if how == "cid" else "9001")
    await env.reconciler.reconcile_positions()

    await cancelled(env)

    assert [s.idem_key for s in env.coordinator.submitted] == [f"stale:{rec.id}:9001"]  # 按待清理的旧单撤，不按孤儿


async def test_condition_2_a_closed_record_no_longer_references_it(make_env):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    rec = await env.add_position(stop=(stop.client_algo_id, "9001", "95"))
    await env.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=T0)
    await env.reconciler.reconcile_positions()

    [spec] = await cancelled(env)  # 仓位归零后遗留的止损：登记簿里已没有活动持仓引用它
    assert spec.idem_key == "orphan:9001"


@pytest.mark.parametrize("variant", ["own_intent_still_open", "cancel_in_flight", "replace_in_flight"])
async def test_condition_2_an_in_flight_intent_that_references_it_blocks_the_cancel(make_env, variant):
    env = await make_env().boot()
    if variant == "own_intent_still_open":
        stop = await env.system_stop(algo_id="9001", intent_state=IntentState.ACTIVE)
    else:
        stop = await env.system_stop(algo_id="9001")
        if variant == "cancel_in_flight":
            await env.new_intent(action=IntentAction.STOP_CANCEL, state=IntentState.RECONCILING, model="op",
                                 params={"client_algo_id": stop.client_algo_id, "algo_id": "9001"})
        else:
            await env.new_intent(action=IntentAction.STOP_REPLACE, state=IntentState.ACTIVE, model="op",
                                 params={"old": {"client_algo_id": stop.client_algo_id, "algo_id": "9001"},
                                         "trigger_price": "96"})
    await env.reconciler.reconcile_positions()

    await cancelled(env)
    assert env.coordinator.submitted == []
    assert kept(env) == []


async def test_condition_2_an_intent_that_has_ended_no_longer_blocks_it(make_env):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    await env.new_intent(action=IntentAction.STOP_REPLACE, state=IntentState.COMPLETED, model="op",
                         params={"old": {"client_algo_id": stop.client_algo_id, "algo_id": "9001"}})
    await env.reconciler.reconcile_positions()

    [spec] = await cancelled(env)
    assert spec.idem_key == "orphan:9001"


# ================================================================ 条件 3：已移交


async def test_condition_3_a_handed_over_stop_is_never_cancelled_while_the_position_is_active(make_env):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    rec = await env.add_position(status="ignored")  # 取消托管并移交：仓位变成「外部（已忽略）」
    await env.registry.set_handed_over(rec.id, {"client_algo_id": stop.client_algo_id, "algo_id": "9001"})
    await env.reconciler.reconcile_positions()  # 交易所读到零，但登记仓还没判已平

    await cancelled(env, rounds=2)
    assert env.coordinator.submitted == []
    assert kept(env) == []


async def test_condition_3_once_the_position_is_gone_the_handed_over_stop_is_cleaned_up(make_env):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    rec = await env.add_position(status="ignored")
    await env.registry.set_handed_over(rec.id, {"client_algo_id": stop.client_algo_id, "algo_id": "9001"})
    await env.registry.mark_closed(rec.id, exit_reason=ExitReason.EXTERNAL, closed_ms=T0)  # 仓位归零（F8）
    await env.reconciler.reconcile_positions()

    [spec] = await cancelled(env)
    assert spec.idem_key == "orphan:9001"


# ================================================================ 条件 4：建立已超过一个条件单对账周期


@pytest.mark.parametrize("age_s, cancels", [(0, False), (30, False), (PERIOD_S - 1, False), (PERIOD_S, False),
                                            (PERIOD_S + 1, True), (3600, True)])
async def test_condition_4_the_order_must_be_older_than_one_orders_reconcile_period(make_env, age_s, cancels):
    env = await make_env().boot()
    await orphan_world(env, age_s=age_s)

    await cancelled(env)

    assert bool(env.coordinator.submitted) is cancels
    assert bool(kept(env)) is False  # 太新的单只是等一等，不算异常


async def test_condition_4_the_period_follows_the_config(make_env):
    env = await make_env(reconcile_orders_s=120).boot()
    await orphan_world(env, age_s=90)
    await cancelled(env)
    assert env.coordinator.submitted == []

    env.clock.advance(31_000)  # 现在 121 秒了
    await env.reconciler.reconcile_positions()
    await cancelled(env)
    assert len(env.coordinator.submitted) == 1


async def test_condition_4_an_unknown_creation_time_is_not_old_enough(make_env):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    env.algos = [algo(SYM, LONG, cid=stop.client_algo_id, algo_id="9001", create_ms=0)]
    await env.reconciler.reconcile_positions()

    await cancelled(env)
    assert env.coordinator.submitted == []


# ================================================================ 条件 5：持仓在新鲜的快照里明确为零


async def test_condition_5_a_non_zero_position_means_alert_not_cancel(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "0.3")]
    await env.system_stop(algo_id="9001")
    await env.reconciler.reconcile_positions()

    await cancelled(env, rounds=2)

    assert env.coordinator.submitted == []
    [alert] = kept(env)[:1]
    assert alert.level is AlertLevel.WARNING and "0.3" in alert.message and "9001" in alert.message
    assert alert.dedupe_key == "orphan_kept:9001"


async def test_condition_5_only_the_same_symbol_and_side_counts(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM, SHORT, "1"), pos(SYM2, LONG, "1")]  # 同合约的对侧、别的合约的同侧：都不算
    await env.system_stop(algo_id="9001")
    await env.reconciler.reconcile_positions()

    [spec] = await cancelled(env)
    assert spec.params["algo_id"] == "9001"


async def test_condition_5_no_snapshot_yet_is_unknown(make_env):
    env = await make_env().boot()
    await env.system_stop(algo_id="9001")  # 还没做过持仓对账

    await cancelled(env)

    assert env.coordinator.submitted == []
    assert len(kept(env)) == 1 and "未知" in kept(env)[0].message


async def test_condition_5_a_failed_latest_positions_round_makes_it_unknown(make_env):
    env = await make_env().boot()
    await orphan_world(env)
    env.fail("fetch_positions")
    assert await env.reconciler.reconcile_positions() == "unknown"  # 更早那次读到的零不能再用

    await cancelled(env)

    assert env.coordinator.submitted == []
    assert len(kept(env)) == 1 and "未知" in kept(env)[0].message


async def test_condition_5_a_stale_snapshot_is_unknown(make_env):
    env = await make_env().boot()
    await orphan_world(env)  # reconcile_positions_s=10：快照最多算新鲜 20 秒
    env.clock.advance(20_001)

    await cancelled(env)
    assert env.coordinator.submitted == []
    assert len(kept(env)) == 1


async def test_condition_5_a_snapshot_exactly_two_periods_old_is_still_fresh(make_env):
    env = await make_env().boot()
    await orphan_world(env)
    env.clock.advance(20_000)

    await cancelled(env)
    assert len(env.coordinator.submitted) == 1


async def test_condition_5_the_position_appearing_in_a_later_snapshot_stops_the_cancel(make_env):
    env = await make_env().boot()
    await orphan_world(env)
    env.positions = [pos(SYM, LONG, "1")]  # 登记簿落后于交易所：此刻交易所有仓了
    await env.reconciler.reconcile_positions()

    await cancelled(env)
    assert env.coordinator.submitted == []
    assert len(kept(env)) == 1


# ================================================================ 重试与上限


async def test_a_failed_orphan_cancel_is_retried_under_a_new_key(make_env):
    env = await make_env().boot()
    await orphan_world(env)
    await cancelled(env)
    first = await env.intents.get_by_key("orphan:9001")
    await env.intents.transition(first.id, IntentState.REJECTED, reason="撤单请求已过签名时间窗，回查条件单仍然存在")

    await cancelled(env)

    assert [s.idem_key for s in env.coordinator.submitted] == ["orphan:9001", "orphan:9001:1"]


async def test_an_orphan_that_cannot_be_cancelled_is_given_up_with_an_alert(make_env):
    env = await make_env().boot()
    await orphan_world(env)
    for _ in range(5):
        await cancelled(env)
        intent = await env.intents.get_by_key(env.coordinator.submitted[-1].idem_key)
        await env.intents.transition(intent.id, IntentState.REJECTED, reason="仍然存在")

    await cancelled(env)

    assert len(env.coordinator.submitted) == 5
    [alert] = env.alerts("cancel_attempts_exhausted")
    assert alert.level is AlertLevel.CRITICAL and "9001" in alert.message


async def test_a_refusing_coordinator_does_not_stop_the_other_orphans(make_env):
    env = await make_env().boot()
    await env.system_stop(algo_id="9001")
    await env.system_stop(symbol=SYM2, side=SHORT, algo_id="9002")
    await env.reconciler.reconcile_positions()
    env.coordinator.submit_error = RuntimeError("协调者已停止")

    assert await env.reconciler.reconcile_orders() == "ok"

    env.coordinator.submit_error = None
    await cancelled(env)
    assert len(env.coordinator.submitted) == 2

