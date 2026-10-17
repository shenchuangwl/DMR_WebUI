"""持仓对账：查询失败是「未知」，绝不当成「空」（F8；4.6「对账未知」）。

- 未知时不清登记、不撤保护单、不补挂，只重查；
- 全账户查询连续 3 次未知才置「对账未知」抑制，一次成功即解除；
- 每轮持仓对账结束都发布 ReconcileCompleted(kind="positions")，未知轮是 status="unknown"。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.events import AccountSnapshotTaken, PositionSnapshotTaken
from dmr_kernel.core.types import AlertLevel, PositionStatus, Suppression
from reconcile_kit import LONG, SYM, SYM2, T0, pos


async def test_unknown_read_changes_nothing(make_env):
    env = await make_env().boot()
    rec = await env.add_position(stop=("DKx-0-1-s", "9001", "95"))
    env.limits.update_positions([pos(SYM2)], T0)
    await env.clear_events()
    env.fail("fetch_positions")

    result = await env.reconciler.reconcile_positions()
    await env.settle()

    assert result == "unknown"
    stored = await env.registry.fetch(rec.id)
    assert stored.status is PositionStatus.MANAGED and stored.version == rec.version
    assert stored.exchange_qty is None and stored.stop_client_id == "DKx-0-1-s"  # 登记与保护单登记都没动
    assert env.registry.get_active(SYM, LONG).id == rec.id
    assert env.adapter.writes == []  # 不撤保护单、不补挂、不下任何单
    assert env.coordinator.submitted == []
    assert not env.events_of(PositionSnapshotTaken, AccountSnapshotTaken)
    # 合约数上限的快照不被「空」替换
    assert env.limits.occupancy(degraded=False, now_ms=T0).keys == {(SYM2, "LONG")}
    assert [e.status for e in env.completed("positions")] == ["unknown"]


@pytest.mark.parametrize("name", ["fetch_positions", "fetch_account", "server_time"])
async def test_any_failing_read_makes_the_round_unknown(make_env, name):
    # 成功 = 持仓、账户权益、服务器时间三项都读到；只要有一项未知，这一轮就不算完整、成功的持仓对账
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = [pos()]
    env.fail(name)

    assert await env.reconciler.reconcile_positions() == "unknown"
    await env.settle()

    assert not env.events_of(PositionSnapshotTaken, AccountSnapshotTaken)
    assert (await env.registry.fetch(rec.id)).exchange_qty is None
    assert [e.status for e in env.completed("positions")] == ["unknown"]


async def test_a_read_that_raises_is_unknown_not_an_error(make_env):
    env = await make_env().boot()

    def boom(**_):
        raise RuntimeError("适配层内部错误")

    env.adapter.set_read("fetch_account", boom)
    assert await env.reconciler.reconcile_positions() == "unknown"
    await env.settle()
    assert [e.status for e in env.completed("positions")] == ["unknown"]


async def test_single_unknown_sets_no_suppression_the_third_consecutive_one_does(make_env):
    env = await make_env().boot()
    env.fail("fetch_positions")

    for streak in (1, 2):
        assert await env.reconciler.reconcile_positions() == "unknown"
        assert Suppression.RECONCILE_UNKNOWN not in env.suppressions()
        assert env.reconciler.status()["unknown_streak"] == streak
        assert env.alerts("reconcile_unknown") == []

    assert await env.reconciler.reconcile_positions() == "unknown"
    assert Suppression.RECONCILE_UNKNOWN in env.suppressions()
    assert env.reconciler.status()["unknown_streak"] == 3
    [alert] = env.alerts("reconcile_unknown")
    assert alert.level is AlertLevel.WARNING

    # 之后继续未知：抑制保持，不重复告警
    assert await env.reconciler.reconcile_positions() == "unknown"
    assert Suppression.RECONCILE_UNKNOWN in env.suppressions()
    assert len(env.alerts("reconcile_unknown")) == 1


async def test_one_success_lifts_the_suppression_and_restarts_the_count(make_env):
    env = await make_env().boot()
    env.fail("fetch_positions")
    for _ in range(3):
        await env.reconciler.reconcile_positions()
    assert Suppression.RECONCILE_UNKNOWN in env.suppressions()

    env.heal()
    assert await env.reconciler.reconcile_positions() == "ok"
    assert Suppression.RECONCILE_UNKNOWN not in env.suppressions()
    assert env.reconciler.status()["unknown_streak"] == 0
    [cleared] = env.alerts("reconcile_unknown_cleared")
    assert cleared.level is AlertLevel.INFO

    # 计数从头开始：又连续两次未知，不再置抑制
    env.fail("fetch_positions")
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()
    assert Suppression.RECONCILE_UNKNOWN not in env.suppressions()


async def test_a_success_between_failures_resets_the_streak(make_env):
    env = await make_env().boot()
    env.fail("fetch_positions")
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()
    env.heal()
    assert await env.reconciler.reconcile_positions() == "ok"
    env.fail("fetch_positions")
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()

    assert env.reconciler.status()["unknown_streak"] == 2
    assert Suppression.RECONCILE_UNKNOWN not in env.suppressions()


async def test_status_shows_the_last_result_and_the_streak(make_env, clock):
    env = await make_env().boot()
    env.fail("fetch_positions")
    await env.reconciler.reconcile_positions()
    status = env.reconciler.status()
    assert status["positions_status"] == "unknown" and status["positions_at_ms"] == clock.now_ms()
    assert status["positions_ok_ms"] is None

    clock.advance(10_000)
    env.heal()
    await env.reconciler.reconcile_positions()
    status = env.reconciler.status()
    assert status["positions_status"] == "ok" and status["positions_ok_ms"] == clock.now_ms()
