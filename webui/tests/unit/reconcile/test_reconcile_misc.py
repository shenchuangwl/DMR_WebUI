"""对账的其余边界：零数量的持仓条目、并发调用、全新实例（重启）接着对账、本实例编号、没做过对账时的状态。"""
from __future__ import annotations

import asyncio
import json

from dmr_kernel.core.events import UnregisteredPositionFound
from dmr_kernel.core.reconcile import Reconciler
from dmr_kernel.ledger import repo_reconcile
from reconcile_kit import LONG, SYM, algo, pos


def another_reconciler(env) -> Reconciler:
    """同一个账本、同一批协作者、全新的内存状态：相当于内核重启之后的对账模块。"""
    return Reconciler(adapter=env.adapter, registry=env.registry, modelstate=env.modelstate,
                      coordinator=env.coordinator, ingest=env.ingest, orders=env.orders, limits=env.limits,
                      ledger=env.ledger, bus=env.bus, clock=env.clock, config_get=env.config.__getitem__,
                      notifier=env.notifier)


async def test_a_zero_quantity_entry_in_the_position_list_is_not_a_position(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = [pos(SYM, LONG, "0")]  # 适配层只返回非零持仓；万一混进来一个零，也只当没有

    for _ in range(3):
        assert await env.reconciler.reconcile_positions() == "ok"

    assert (await env.registry.fetch(rec.id)).status.value == "closed"  # 三次明确的零
    assert env.events_of(UnregisteredPositionFound) == []


async def test_concurrent_rounds_are_serialised(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1")]

    results = await asyncio.gather(*(env.reconciler.reconcile_positions() for _ in range(4)),
                                   *(env.reconciler.reconcile_orders() for _ in range(3)))

    assert results == ["ok"] * 7
    assert len(env.completed("positions")) == 4 and len(env.completed("orders")) == 3


async def test_a_fresh_instance_picks_up_the_open_diffs_and_resolves_them(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]
    assert await env.reconciler.reconcile_positions() == "mismatch"
    assert len(await env.ledger.read(repo_reconcile.list_diffs, open_only=True)) == 1

    restarted = another_reconciler(env)  # 重启：内存里没有这条差异，账本里有
    env.positions = [pos(SYM, LONG, "1")]
    assert await restarted.reconcile_positions() == "ok"

    assert await env.ledger.read(repo_reconcile.list_diffs, open_only=True) == []  # 已一致：记为已解决
    assert restarted.status()["diffs"] == []


async def test_a_fresh_instance_announces_a_still_unregistered_position_again(make_env):
    # 「已报告」只记在内存里：重启之后模型 2 的归类状态不一定还在，仍未登记的仓要重新报一次
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "0.5")]
    await env.reconciler.reconcile_positions()
    restarted = another_reconciler(env)
    await restarted.reconcile_positions()
    await env.settle()

    assert len(env.events_of(UnregisteredPositionFound)) == 2
    assert len(await env.ledger.read(repo_reconcile.list_diffs, open_only=True)) == 1  # 差异行不堆积


async def test_a_fresh_instance_does_not_repeat_the_backfill_for_a_known_unexplained_difference(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]
    await env.reconciler.reconcile_positions()
    calls = len(env.adapter.calls("fetch_trades"))

    restarted = another_reconciler(env)
    assert await restarted.reconcile_positions() == "mismatch"
    assert len(env.adapter.calls("fetch_trades")) == calls


async def test_with_an_instance_given_its_own_system_ids_are_cancelled(make_env):
    env = await make_env(instance_filter=True).boot()
    stop = await env.system_stop(algo_id="9001")
    await env.reconciler.reconcile_positions()

    await env.reconciler.reconcile_orders()

    [spec] = env.coordinator.submitted
    assert spec.params == {"client_algo_id": stop.client_algo_id, "algo_id": "9001"}


async def test_the_status_before_any_round_is_empty_and_plain_json(make_env):
    env = await make_env().boot()
    status = env.reconciler.status()
    json.dumps(status)
    assert status["positions_status"] is None and status["orders_status"] is None
    assert status["positions_at_ms"] is None and status["positions_ok_ms"] is None
    assert (status["unknown_streak"], status["orders_unknown_streak"], status["diffs"]) == (0, 0, [])
    assert status["clock_skew_ms"] is None and status["mismatch_since_ms"] is None
    assert status["reconcile_unknown"] is False and status["reconcile_mismatch"] is False


async def test_an_algo_with_a_system_id_but_a_zero_create_time_is_never_old_enough(make_env):
    env = await make_env().boot()
    env.algos = [algo(SYM, LONG, cid=env.own_id(), algo_id="9100", create_ms=0)]
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_orders()
    assert env.coordinator.submitted == []
