"""对账记录（reconcile_run）：值得留痕的一轮才写，安静的 ok 轮不刷屏。"""
from __future__ import annotations

import json

from dmr_kernel.ledger import repo_reconcile
from reconcile_kit import LONG, SYM, pos


async def runs(env, kind="positions"):
    return await env.ledger.read(repo_reconcile.recent_runs, kind=kind)


async def test_quiet_ok_rounds_write_one_record_then_stay_silent(make_env, clock):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1")]

    for _ in range(5):
        assert await env.reconciler.reconcile_positions() == "ok"
        clock.advance(10_000)

    [run] = await runs(env)  # 第一次（状态从「没有」到 ok）写一条，之后每轮都没变化
    assert (run.kind, run.status) == ("positions", "ok")


async def test_unknown_mismatch_and_recovery_are_each_recorded_with_details(make_env, clock):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1")]
    await env.reconciler.reconcile_positions()

    env.fail("fetch_positions")
    await env.reconciler.reconcile_positions()
    env.heal()
    clock.advance(10_000)
    env.positions = [pos(SYM, LONG, "2")]
    await env.reconciler.reconcile_positions()
    clock.advance(10_000)
    env.positions = [pos(SYM, LONG, "1")]
    await env.reconciler.reconcile_positions()

    found = await runs(env)
    assert [r.status for r in found] == ["ok", "mismatch", "unknown", "ok"]  # 新的在前
    unknown = found[2]
    assert "fetch_positions" in json.loads(unknown.detail)["notes"][0]
    mismatch = json.loads(found[1].detail)
    assert mismatch["diffs"] == [{"symbol": SYM, "side": "LONG", "kind": "qty_mismatch", "local": "1",
                                  "exchange": "2"}]
    assert all(r.duration_ms is not None and r.duration_ms >= 0 for r in found)


async def test_deferred_keys_alone_do_not_make_a_round_worth_recording(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "2")]
    env.coordinator.busy.add((SYM, LONG))

    for _ in range(4):
        assert await env.reconciler.reconcile_positions() == "ok"

    assert len(await runs(env)) == 1  # 有系统意图在途先不判：每轮都一样，只在第一次留痕
    assert "在途" in json.loads((await runs(env))[0].detail)["deferred"][0]


async def test_orders_rounds_are_recorded_under_their_own_kind(make_env):
    env = await make_env().boot()
    await env.reconciler.reconcile_orders()
    env.fail("fetch_open_orders")
    await env.reconciler.reconcile_orders()

    assert [r.status for r in await runs(env, "orders")] == ["unknown", "ok"]
    assert await runs(env, "positions") == []
