"""reconcile_key：对单个「合约 + 方向」即时读一次交易所，返回三态（存在、不存在、未知），并做同样的比对。"""
from __future__ import annotations

from dmr_kernel.core.events import (
    AccountSnapshotTaken,
    PositionSnapshotTaken,
    ReconcileCompleted,
    UnregisteredPositionFound,
)
from dmr_kernel.core.types import PositionStatus, Suppression, Tri
from dmr_kernel.ledger import repo_reconcile
from reconcile_kit import BUY, D, LONG, SHORT, SYM, pos, trade, web_view


async def test_reconcile_key_answers_with_the_three_states(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "1")]

    assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.EXISTS
    assert await env.reconciler.reconcile_key(SYM, SHORT) is Tri.ABSENT
    env.fail("fetch_positions")
    assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.UNKNOWN  # 读不到不是「不存在」


async def test_an_unknown_answer_changes_nothing(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    await env.clear_events()
    env.fail("fetch_positions")

    assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.UNKNOWN
    await env.settle()

    assert env.events == []
    assert (await env.registry.fetch(rec.id)).exchange_qty is None
    assert env.reconciler.status()["unknown_streak"] == 0  # 周期对账的连续未知次数只由周期对账累计
    assert env.adapter.writes == []


async def test_it_publishes_no_round_level_events_and_leaves_the_limit_snapshot_alone(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "1")]
    await env.add_position(qty="1")
    await env.clear_events()

    await env.reconciler.reconcile_key(SYM, LONG)
    await env.settle()

    assert not env.events_of(PositionSnapshotTaken, AccountSnapshotTaken, ReconcileCompleted)
    assert env.limits.occupancy(degraded=False, now_ms=env.clock.now_ms()).stale  # 单个键不是全账户快照
    assert (await env.registry.fetch(1)).consistent is True  # 但这个键的交易所视图是更新了的


async def test_an_unregistered_key_is_reported_like_in_a_round_and_only_once(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "0.5")]
    await env.clear_events()

    assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.EXISTS
    assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.EXISTS
    await env.reconciler.reconcile_positions()  # 周期对账与按键对账共用「已报告」的记忆
    await env.settle()

    [found] = env.events_of(UnregisteredPositionFound)
    assert found.trigger == "reconcile" and found.qty == D("0.5")


async def test_a_busy_key_is_not_judged(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "0.5")]
    env.coordinator.busy.add((SYM, LONG))
    await env.clear_events()

    assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.EXISTS
    await env.settle()
    assert env.events_of(UnregisteredPositionFound) == []


async def test_an_unexplained_difference_is_flagged_and_an_explained_one_is_not(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    added = trade("t1", side=BUY, position_side=LONG, qty="0.5")
    env.trades = [added]
    env.order_views[added.exchange_order_id] = web_view(added)
    env.positions = [pos(SYM, LONG, "1.5")]

    assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.EXISTS  # 补拉到手动加仓：登记簿跟上了
    await env.settle()
    assert (await env.registry.fetch(rec.id)).qty == D("1.5") and env.suppressions() == set()

    env.positions = [pos(SYM, LONG, "2.5")]  # 这次没有任何成交可以解释
    assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.EXISTS
    assert Suppression.RECONCILE_MISMATCH in env.suppressions()
    [diff] = await env.ledger.read(repo_reconcile.list_diffs, open_only=True)
    assert (diff.local_qty, diff.exchange_qty) == (D("1.5"), D("2.5"))


async def test_zero_readings_from_single_key_calls_do_not_count_towards_closing(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = []

    for _ in range(5):
        assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.ABSENT
    assert env.registry.get_active(SYM, LONG).id == rec.id

    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()
    assert env.registry.get_active(SYM, LONG).id == rec.id
    await env.reconciler.reconcile_positions()  # 连续第三个周期对账的零
    closed = await env.registry.fetch(rec.id)
    assert closed.status is PositionStatus.CLOSED
