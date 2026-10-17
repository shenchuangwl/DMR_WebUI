"""持仓对账成功的一轮：快照事件、合约数上限、交易所视图、服务器时间偏差、强平价告警、状态。"""
from __future__ import annotations

import json

import pytest

from dmr_kernel.core.events import (
    AccountSnapshotTaken,
    PositionChanged,
    PositionSnapshotTaken,
    ReconcileCompleted,
)
from dmr_kernel.core.types import AlertLevel, Suppression
from reconcile_kit import D, LONG, SHORT, SYM, SYM2, T0, pos


async def test_a_successful_round_publishes_the_snapshots_then_completion(make_env, clock):
    env = await make_env().boot()
    await env.add_position(symbol=SYM, side=LONG)
    await env.add_position(symbol=SYM2, side=SHORT, qty="2")
    env.positions = [pos(SYM, LONG, "1"), pos(SYM2, SHORT, "2")]
    env.equity = D("1234.5")
    await env.clear_events()

    assert await env.reconciler.reconcile_positions() == "ok"
    await env.settle()

    [snap] = env.events_of(PositionSnapshotTaken)
    assert snap.positions == tuple(env.positions)
    assert snap.server_time_ms == clock.now_ms() and snap.at_ms == clock.now_ms()
    [account] = env.events_of(AccountSnapshotTaken)
    assert account.equity == D("1234.5")  # 权益取账户读数
    assert account.available == D("1184.5") and account.wallet == D("1229.5")
    assert account.at_ms == clock.now_ms()
    [done] = env.events_of(ReconcileCompleted)
    assert (done.kind, done.status, done.at_ms) == ("positions", "ok", clock.now_ms())
    kinds = [type(e) for e in env.events if isinstance(e, (PositionSnapshotTaken, AccountSnapshotTaken,
                                                           ReconcileCompleted))]
    assert kinds[-1] is ReconcileCompleted  # 对账结束的事件最后发


async def test_the_snapshot_carries_the_exchange_server_time_not_the_local_one(make_env, clock):
    env = await make_env().boot()
    env.skew_ms = 400
    await env.reconciler.reconcile_positions()
    await env.settle()
    [snap] = env.events_of(PositionSnapshotTaken)
    assert snap.server_time_ms == clock.now_ms() + 400 and snap.at_ms == clock.now_ms()


async def test_a_successful_round_refreshes_the_symbol_limit_snapshot(make_env, clock):
    env = await make_env().boot()
    await env.add_position(symbol=SYM, side=LONG)
    env.positions = [pos(SYM, LONG), pos(SYM2, SHORT)]  # 第二个是登记簿没有的外部仓：也占交易所的合约数
    env.limits.update_orders([], [], T0)
    await env.reconciler.reconcile_positions()

    seen = env.limits.occupancy(degraded=False, now_ms=clock.now_ms())
    assert seen.keys == {(SYM, "LONG"), (SYM2, "SHORT")} and not seen.stale

    clock.advance(21_000)  # 超过 2 × reconcile_positions_s 没有更新：快照过期
    assert env.limits.occupancy(degraded=False, now_ms=clock.now_ms()).stale


async def test_registered_positions_get_the_exchange_view_without_a_state_change(make_env, clock):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1", liq="60")]
    await env.clear_events()

    await env.reconciler.reconcile_positions()
    await env.settle()

    stored = await env.registry.fetch(rec.id)
    assert stored.exchange_qty == D("1") and stored.liquidation_price == D("60")
    assert stored.consistent is True and stored.exchange_seen_ms == clock.now_ms()
    assert stored.version == rec.version and not env.events_of(PositionChanged)


@pytest.mark.parametrize("skew", [0, 999, 1000, -1000])
async def test_skew_within_the_threshold_sets_nothing(make_env, skew):
    env = await make_env().boot()
    env.skew_ms = skew
    assert await env.reconciler.reconcile_positions() == "ok"
    assert Suppression.CLOCK_SKEW not in env.suppressions()
    assert env.alerts("clock_skew") == []
    status = env.reconciler.status()
    assert status["clock_skew_ms"] == skew and status["clock_skew_exceeded"] is False


@pytest.mark.parametrize("skew", [1001, -1001, 5000])
async def test_skew_beyond_the_threshold_sets_the_suppression_and_alerts(make_env, skew):
    env = await make_env().boot()
    env.skew_ms = skew
    assert await env.reconciler.reconcile_positions() == "ok"  # 这一轮对账本身是完整成功的
    assert Suppression.CLOCK_SKEW in env.suppressions()
    [alert] = env.alerts("clock_skew")
    assert alert.level is AlertLevel.WARNING
    assert env.reconciler.status()["clock_skew_exceeded"] is True


async def test_the_suppression_lifts_when_the_skew_is_back_within_the_threshold(make_env):
    env = await make_env().boot()
    env.skew_ms = 2500
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()  # 仍然超出：不重复告警
    assert len(env.alerts("clock_skew")) == 1

    env.skew_ms = -800
    await env.reconciler.reconcile_positions()
    assert Suppression.CLOCK_SKEW not in env.suppressions()
    [lifted] = env.alerts("clock_skew_cleared")
    assert lifted.level is AlertLevel.INFO


async def test_the_skew_threshold_is_read_from_the_config_every_round(make_env):
    env = await make_env(clock_skew_max_ms=300).boot()
    env.skew_ms = 301
    await env.reconciler.reconcile_positions()
    assert Suppression.CLOCK_SKEW in env.suppressions()
    env.skew_ms = 300
    await env.reconciler.reconcile_positions()
    assert Suppression.CLOCK_SKEW not in env.suppressions()
    env.config["clock_skew_max_ms"] = 100
    env.skew_ms = 150
    await env.reconciler.reconcile_positions()
    assert Suppression.CLOCK_SKEW in env.suppressions()


async def test_an_unreadable_server_time_leaves_the_skew_state_alone(make_env):
    env = await make_env().boot()
    env.skew_ms = 5000
    await env.reconciler.reconcile_positions()
    assert Suppression.CLOCK_SKEW in env.suppressions()

    env.fail("server_time")  # 读不到：不知道偏差，不能当成「已回到阈值内」
    assert await env.reconciler.reconcile_positions() == "unknown"
    assert Suppression.CLOCK_SKEW in env.suppressions()


@pytest.mark.parametrize("side, stop, liq, mark, alerts", [
    (LONG, "95", "96", "100", True),    # 多仓：强平价在止损价上方，先于止损被碰到
    (LONG, "95", "94", "100", False),
    (LONG, "95", "95", "100", False),   # 一样近不算「更靠近」
    (SHORT, "105", "104", "100", True),
    (SHORT, "105", "106", "100", False),
    (LONG, "95", "96", None, True),     # 没有标记价也能按顺序判定
    (LONG, "95", "105", "100", False),  # 强平价在现价的另一侧：数据不合理，不告警
])
async def test_liquidation_closer_to_the_price_than_the_stop_alerts(make_env, side, stop, liq, mark, alerts):
    env = await make_env().boot()
    rec = await env.add_position(side=side, stop=("DKx-0-1-s", "9001", stop))
    env.positions = [pos(SYM, side, "1", mark=mark, liq=liq)]

    await env.reconciler.reconcile_positions()

    found = env.alerts("liquidation_before_stop")
    assert bool(found) is alerts
    if alerts:
        assert found[0].level is AlertLevel.CRITICAL
        assert found[0].dedupe_key == f"liq_before_stop:{rec.id}"
        assert liq in found[0].message and stop in found[0].message


async def test_liquidation_alert_needs_both_prices_and_a_registered_position(make_env):
    env = await make_env().boot()
    await env.add_position(symbol=SYM, stop=None)                                  # 没有止损价
    await env.add_position(symbol=SYM2, stop=("DKx-0-2-s", "9002", "95"))          # 交易所没给强平价
    await env.add_position(symbol="SOLUSDT", status="pending")                     # 待确认仓不算
    env.positions = [pos(SYM, LONG, liq="99"), pos(SYM2, LONG, liq=None), pos("SOLUSDT", LONG, liq="99")]

    await env.reconciler.reconcile_positions()

    assert env.alerts("liquidation_before_stop") == []


async def test_status_is_plain_json(make_env):
    env = await make_env().boot()
    await env.add_position()
    env.positions = [pos(SYM, LONG, "2")]  # 数量对不上：差异列表里有 Decimal
    await env.reconciler.reconcile_positions()

    status = env.reconciler.status()
    json.dumps(status)
    assert status["positions_status"] == "mismatch"
    [diff] = status["diffs"]
    assert (diff["symbol"], diff["side"], diff["kind"], diff["local_qty"], diff["exchange_qty"]) == (
        SYM, "LONG", "qty_mismatch", "1", "2")
