"""挂单与条件单对账（F8）：两次查询、快照事件、未知不下结论、待清理的旧止损。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.events import OrdersSnapshotTaken
from dmr_kernel.core.types import AlertLevel, IntentAction, IntentSource, IntentState
from reconcile_kit import LONG, SHORT, SYM, SYM2, T0, algo, open_order, pos


async def test_a_successful_round_takes_two_separate_queries_and_publishes_the_snapshot(make_env, clock):
    env = await make_env().boot()
    env.open_orders = [open_order(SYM, LONG)]
    env.algos = [algo(SYM, LONG, cid="web_stop", algo_id="9100", create_ms=T0)]
    env.limits.update_positions([], T0)
    await env.clear_events()

    assert await env.reconciler.reconcile_orders() == "ok"
    await env.settle()

    assert env.adapter.calls("fetch_open_orders") == [{"symbol": None}]  # 普通挂单与条件单分两次查询，都是全账户
    assert env.adapter.calls("fetch_open_algo_orders") == [{"symbol": None}]
    [snap] = env.events_of(OrdersSnapshotTaken)
    assert snap.open_orders == tuple(env.open_orders) and snap.algo_orders == tuple(env.algos)
    assert snap.at_ms == clock.now_ms()
    assert [(e.kind, e.status) for e in env.completed("orders")] == [("orders", "ok")]
    seen = env.limits.occupancy(degraded=False, now_ms=clock.now_ms())
    assert seen.keys == {(SYM, "LONG")} and not seen.stale
    assert env.reconciler.status()["orders_status"] == "ok"


@pytest.mark.parametrize("name", ["fetch_open_orders", "fetch_open_algo_orders"])
async def test_either_query_failing_means_no_conclusion_at_all(make_env, name):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    old = await env.system_stop(algo_id="9001")
    await env.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id="9001")
    env.algos = []  # 如果把「未知」当成「空」，旧止损会被当成「已不在」而从列表里移走
    env.limits.update_positions([pos(SYM2)], T0)
    env.limits.update_orders([open_order(SYM2)], [], T0)
    await env.clear_events()
    env.fail(name)

    assert await env.reconciler.reconcile_orders() == "unknown"
    await env.settle()

    assert env.events_of(OrdersSnapshotTaken) == []
    assert (await env.registry.fetch(rec.id)).stale_stops == [
        {"client_algo_id": old.client_algo_id, "algo_id": "9001"}]
    assert env.coordinator.submitted == []
    assert env.limits.occupancy(degraded=False, now_ms=T0).keys == {(SYM2, "LONG")}
    assert [e.status for e in env.completed("orders")] == ["unknown"]
    assert env.suppressions() == set()  # 挂单对账不置「对账未知」抑制：快照过期时合约数上限自己会拦住新开仓
    assert env.reconciler.status()["unknown_streak"] == 0  # 持仓对账的计数互不相干


async def test_orders_unknown_streak_alerts_on_the_third_and_recovers(make_env):
    env = await make_env().boot()
    env.fail("fetch_open_algo_orders")
    for _ in range(2):
        await env.reconciler.reconcile_orders()
    assert env.alerts("reconcile_orders_unknown") == []
    await env.reconciler.reconcile_orders()
    await env.reconciler.reconcile_orders()
    [alert] = env.alerts("reconcile_orders_unknown")
    assert alert.level is AlertLevel.WARNING
    assert env.reconciler.status()["orders_unknown_streak"] == 4

    env.heal()
    assert await env.reconciler.reconcile_orders() == "ok"
    assert env.reconciler.status()["orders_unknown_streak"] == 0
    [recovered] = env.alerts("reconcile_orders_recovered")
    assert recovered.level is AlertLevel.INFO
    assert env.suppressions() == set()


async def test_unknown_orders_are_requeried_after_the_snapshot(make_env, monkeypatch):
    env = await make_env().boot()
    calls: list[str] = []

    async def spy():
        calls.append("requery")
        return []

    monkeypatch.setattr(env.orders, "requery_unknown", spy)
    assert await env.reconciler.reconcile_orders() == "ok"
    assert calls == ["requery"]

    env.fail("fetch_open_orders")  # 交易所读不到：这一轮不回查
    await env.reconciler.reconcile_orders()
    assert calls == ["requery"]


async def test_a_failing_requery_does_not_spoil_the_round(make_env, monkeypatch):
    env = await make_env().boot()

    async def broken():
        raise RuntimeError("账本忙")

    monkeypatch.setattr(env.orders, "requery_unknown", broken)
    assert await env.reconciler.reconcile_orders() == "ok"
    await env.settle()
    assert [e.status for e in env.completed("orders")] == ["ok"]


# ================================================================ 待清理的旧止损


async def stale_setup(env, **stop_kwargs):
    """一个托管仓：当前止损（登记簿引用）和一张待清理的旧止损，两张都在交易所。"""
    current = await env.system_stop(algo_id="9002", trigger="97")
    old = await env.system_stop(algo_id="9001", trigger="95", **stop_kwargs)
    rec = await env.add_position(stop=(current.client_algo_id, "9002", "97"))
    rec = await env.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id="9001")
    return rec, current, old


async def test_a_stale_stop_still_on_the_exchange_is_cancelled_through_the_coordinator(make_env):
    env = await make_env().boot()
    rec, current, old = await stale_setup(env)

    assert await env.reconciler.reconcile_orders() == "ok"

    [spec] = env.coordinator.submitted
    assert (spec.action, spec.source, spec.model) == (IntentAction.STOP_CANCEL, IntentSource.RECONCILE, "op")
    assert (spec.symbol, spec.side, spec.position_id) == (SYM, LONG, rec.id)
    assert spec.params == {"client_algo_id": old.client_algo_id, "algo_id": "9001"}
    assert spec.idem_key == f"stale:{rec.id}:9001"
    # 撤单有没有成功还不知道：条目留着，交易所里确认不在了再移走
    assert (await env.registry.fetch(rec.id)).stale_stops == [
        {"client_algo_id": old.client_algo_id, "algo_id": "9001"}]
    assert env.adapter.writes == []  # 对账自己从不直接写交易所


async def test_a_stale_stop_that_is_gone_is_removed_from_the_list(make_env):
    env = await make_env().boot()
    rec, current, old = await stale_setup(env)
    env.algos = [current]  # 旧单已经不在交易所的未结条件单里

    assert await env.reconciler.reconcile_orders() == "ok"

    assert (await env.registry.fetch(rec.id)).stale_stops == []
    assert env.coordinator.submitted == []


async def test_a_stale_stop_that_is_listed_but_no_longer_open_counts_as_gone(make_env):
    env = await make_env().boot()
    rec, current, old = await stale_setup(env)
    env.algos = [current, algo(SYM, LONG, cid=old.client_algo_id, algo_id="9001", raw_status="CANCELED")]

    await env.reconciler.reconcile_orders()

    assert (await env.registry.fetch(rec.id)).stale_stops == []
    assert env.coordinator.submitted == []


async def test_a_cancel_pending_order_row_whose_stop_is_still_on_the_exchange_is_still_in_force(make_env):
    # 撤单结果未知时订单行停在 CANCEL_PENDING；交易所里它还挂着，就是仍然有效：旧止损继续撤，不当成「已撤」
    env = await make_env().boot()
    rec, current, old = await stale_setup(env, cancel_pending=True)
    assert (await env.orders.get(old.client_algo_id)).state.value == "CANCEL_PENDING"

    await env.reconciler.reconcile_orders()

    [spec] = env.coordinator.submitted
    assert spec.params["algo_id"] == "9001"
    assert (await env.registry.fetch(rec.id)).stale_stops != []


async def test_a_stale_entry_that_is_the_current_stop_is_left_alone(make_env):
    env = await make_env().boot()
    rec, current, old = await stale_setup(env)
    await env.registry.add_stale_stop(rec.id, client_algo_id=current.client_algo_id, algo_id="9002")

    await env.reconciler.reconcile_orders()

    assert [s.params["algo_id"] for s in env.coordinator.submitted] == ["9001"]  # 当前止损绝不当旧单撤
    assert {"client_algo_id": current.client_algo_id, "algo_id": "9002"} in (
        await env.registry.fetch(rec.id)).stale_stops


async def test_stale_entries_may_name_the_order_by_either_id(make_env):
    env = await make_env().boot()
    current = await env.system_stop(algo_id="9010", trigger="97")
    rec = await env.add_position(stop=(current.client_algo_id, "9010", "97"))  # 当前止损挂着：旧单才可以撤（复核补充）
    by_algo = await env.system_stop(algo_id="9011")
    by_cid = await env.system_stop(algo_id="9012")
    await env.registry.add_stale_stop(rec.id, client_algo_id=None, algo_id="9011")
    await env.registry.add_stale_stop(rec.id, client_algo_id=by_cid.client_algo_id, algo_id=None)

    await env.reconciler.reconcile_orders()

    assert sorted(s.idem_key for s in env.coordinator.submitted) == [f"stale:{rec.id}:9011", f"stale:{rec.id}:9012"]
    assert by_algo.algo_id == "9011"


async def test_a_pending_cancel_is_not_submitted_twice(make_env):
    env = await make_env().boot()
    await stale_setup(env)
    await env.reconciler.reconcile_orders()
    await env.reconciler.reconcile_orders()  # 第一个撤单意图还没结束

    assert len(env.coordinator.submitted) == 1


async def test_a_failed_cancel_is_retried_under_a_new_key_up_to_five_times(make_env):
    env = await make_env().boot()
    rec, current, old = await stale_setup(env)

    keys = []
    for _ in range(5):
        await env.reconciler.reconcile_orders()
        spec = env.coordinator.submitted[-1]
        keys.append(spec.idem_key)
        intent = await env.intents.get_by_key(spec.idem_key)
        await env.intents.transition(intent.id, IntentState.REJECTED, reason="撤单请求已过签名时间窗，回查仍然存在")
    assert keys == [f"stale:{rec.id}:9001"] + [f"stale:{rec.id}:9001:{n}" for n in range(1, 5)]

    await env.reconciler.reconcile_orders()  # 第六次不再试，转而告警
    assert len(env.coordinator.submitted) == 5
    [alert] = env.alerts("cancel_attempts_exhausted")
    assert alert.level is AlertLevel.CRITICAL and "9001" in alert.message


async def test_a_coordinator_that_refuses_the_cancel_does_not_break_the_round(make_env):
    env = await make_env().boot()
    await stale_setup(env)
    env.coordinator.submit_error = RuntimeError("协调者已停止")

    assert await env.reconciler.reconcile_orders() == "ok"
    await env.settle()
    assert [e.status for e in env.completed("orders")] == ["ok"]


async def test_stale_stops_are_cleaned_on_every_kind_of_active_record(make_env):
    env = await make_env().boot()
    rec = await env.add_position(symbol=SYM2, side=SHORT, status="ignored")
    old = await env.system_stop(symbol=SYM2, side=SHORT, algo_id="9021")
    await env.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id="9021")

    await env.reconciler.reconcile_orders()

    [spec] = env.coordinator.submitted
    assert (spec.symbol, spec.side) == (SYM2, SHORT)
