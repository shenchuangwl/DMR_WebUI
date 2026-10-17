"""对账消费补拉成交的失败（FU5）：读成交失败不是「没有新成交」。

``Ingest.backfill_trades`` 返回的结果带 ``failed``（没能补拉完整的合约）；对账在这些合约的可疑键上不下结论：
整轮记为 ``unknown``（不算完整的一轮，不累计「连续未知」），不判「对账不一致」、不判已平。读恢复之后再判。
补拉读到了而没有新成交，才是「无法解释」。
"""
from __future__ import annotations

import json

from dmr_kernel.core.types import ExitReason, Known, PositionStatus, Suppression, Tri, Unknown
from dmr_kernel.ledger import repo_reconcile
from reconcile_kit import BUY, D, LONG, SELL, SYM, SYM2, pos, trade, web_view


async def open_diffs(env):
    return await env.ledger.read(repo_reconcile.list_diffs, open_only=True)


async def rounds(env, kind="positions"):
    return await env.ledger.read(repo_reconcile.recent_runs, kind=kind)


# ================================================================ 数量差异


async def test_a_quantity_difference_is_undecided_when_the_trade_backfill_could_not_read(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "2")]
    env.fail("fetch_trades")
    await env.clear_events()

    assert await env.reconciler.reconcile_positions() == "unknown"
    await env.settle()

    assert Suppression.RECONCILE_MISMATCH not in env.suppressions() and env.suppressions() == set()
    assert await open_diffs(env) == [] and env.alerts("reconcile_mismatch") == []
    stored = await env.registry.fetch(rec.id)
    assert stored.qty == D("1")  # 登记簿没有被改
    assert [e.status for e in env.completed("positions")] == ["unknown"]  # 不算完整的一轮
    assert env.reconciler.status()["unknown_streak"] == 0  # 持仓读本身是成功的：不累计「连续未知」，不置「对账未知」
    assert env.reconciler.status()["positions_status"] == "unknown"


async def test_the_same_difference_with_a_readable_but_empty_backfill_is_still_a_mismatch(make_env):
    """反例：补拉读到了、确实没有新成交，才是「无法解释」——只有读失败才不下结论。"""
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "2")]
    env.trades = []

    assert await env.reconciler.reconcile_positions() == "mismatch"
    await env.settle()

    assert Suppression.RECONCILE_MISMATCH in env.suppressions()
    [diff] = await open_diffs(env)
    assert (diff.symbol, diff.kind, diff.local_qty, diff.exchange_qty) == (SYM, "qty_mismatch", D("1"), D("2"))


async def test_a_difference_the_trades_can_explain_is_not_called_a_mismatch_while_they_cannot_be_read(make_env):
    """用户手动加仓 0.5：补拉读不到成交的那一轮不下结论（不置「对账不一致」——那要人工确认）；读恢复后成交解释了差异。"""
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    added = trade("t1", side=BUY, position_side=LONG, qty="0.5")
    env.trades = [added]
    env.order_views[added.exchange_order_id] = web_view(added)
    env.positions = [pos(SYM, LONG, "1.5")]
    env.fail("fetch_trades")

    assert await env.reconciler.reconcile_positions() == "unknown"
    await env.settle()
    assert env.suppressions() == set() and await open_diffs(env) == []
    assert (await env.registry.fetch(rec.id)).qty == D("1")  # 成交没读到，登记簿还没跟上

    env.heal()
    assert await env.reconciler.reconcile_positions() == "ok"
    await env.settle()
    stored = await env.registry.fetch(rec.id)
    assert stored.qty == D("1.5") and stored.consistent is True
    assert env.suppressions() == set() and await open_diffs(env) == []
    assert [e.status for e in env.completed("positions")] == ["unknown", "ok"]


async def test_after_the_read_recovers_an_unexplainable_difference_is_judged_normally(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "2")]
    env.fail("fetch_trades")
    assert await env.reconciler.reconcile_positions() == "unknown"
    assert await env.reconciler.reconcile_positions() == "unknown"  # 失败的轮次不会自己变成「不一致」
    assert Suppression.RECONCILE_MISMATCH not in env.suppressions()

    env.heal()
    assert await env.reconciler.reconcile_positions() == "mismatch"
    assert Suppression.RECONCILE_MISMATCH in env.suppressions() and len(await open_diffs(env)) == 1


async def test_only_the_symbol_whose_trades_could_not_be_read_is_left_undecided(make_env):
    env = await make_env().boot()
    await env.add_position(symbol=SYM, qty="1")
    await env.add_position(symbol=SYM2, qty="1")
    env.positions = [pos(SYM, LONG, "2"), pos(SYM2, LONG, "2")]
    env.adapter.set_read("fetch_trades", lambda symbol, start_ms, end_ms, limit:
                         Unknown("限频") if symbol == SYM else Known([]))

    assert await env.reconciler.reconcile_positions() == "mismatch"  # SYM2 照常判定
    assert {d.symbol for d in await open_diffs(env)} == {SYM2}  # SYM 没读到成交：不下结论

    env.adapter.set_read("fetch_trades", lambda symbol, start_ms, end_ms, limit: Known([]))  # SYM 的读也恢复了
    assert await env.reconciler.reconcile_positions() == "mismatch"
    assert {d.symbol for d in await open_diffs(env)} == {SYM, SYM2}  # SYM 现在也判了


async def test_the_round_record_says_which_symbol_could_not_be_backfilled(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "2")]
    env.fail("fetch_trades")
    await env.reconciler.reconcile_positions()

    [run] = await rounds(env)
    detail = json.loads(run.detail)
    assert run.status == "unknown" and detail["undecided"] == 1
    assert any(SYM in note and "补拉" in note for note in detail["notes"])


# ================================================================ 零持仓


async def test_a_zero_reading_is_not_judged_closed_while_the_trade_backfill_cannot_read(make_env):
    """成交记录可能正是这个仓「已被外部平掉」的证据：读不到成交时不能把登记仓判成「出场原因未知」的已平。"""
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = []
    env.fail("fetch_trades")

    for _ in range(4):  # 超过连续 3 次零读数，但每一轮的补拉都没读到成交
        assert await env.reconciler.reconcile_positions() == "unknown"
    assert env.registry.get_active(SYM, LONG) is not None
    assert (await env.registry.fetch(rec.id)).status is not PositionStatus.CLOSED
    assert env.alerts("position_closed_by_reconcile") == []

    env.heal()
    assert await env.reconciler.reconcile_positions() == "ok"  # 补拉读到了、没有成交可解释：这才按已平处理
    assert env.registry.get_active(SYM, LONG) is None


async def test_a_trade_record_that_closes_the_position_wins_over_the_zero_count_once_it_can_be_read(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    exit_fill = trade("t9", side=SELL, position_side=LONG, qty="1")
    env.trades = [exit_fill]
    env.order_views[exit_fill.exchange_order_id] = web_view(exit_fill)
    env.positions = []
    env.fail("fetch_trades")

    assert await env.reconciler.reconcile_positions() == "unknown"
    assert env.registry.get_active(SYM, LONG) is not None

    env.heal()
    assert await env.reconciler.reconcile_positions() == "ok"
    await env.settle()
    closed = await env.registry.fetch(rec.id)
    assert closed.status is PositionStatus.CLOSED and closed.exit_reason is ExitReason.EXTERNAL  # 按成交归零，不是「未知」


# ================================================================ 单键对账与旧的返回值


async def test_a_single_key_check_does_not_declare_a_mismatch_when_the_backfill_cannot_read(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "2")]
    env.fail("fetch_trades")

    assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.EXISTS
    await env.settle()
    assert env.suppressions() == set() and await open_diffs(env) == []

    env.heal()
    assert await env.reconciler.reconcile_key(SYM, LONG) is Tri.EXISTS
    assert Suppression.RECONCILE_MISMATCH in env.suppressions()  # 读得到了才判


async def test_a_backfill_that_returns_a_bare_count_still_works(make_env, monkeypatch):
    """没有 ``failed`` 属性的返回值（只返回笔数的替身）按「没有失败」处理：对账不因此出错。"""
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "2")]

    async def bare_count(symbols):
        return 0

    monkeypatch.setattr(env.ingest, "backfill_trades", bare_count)
    assert await env.reconciler.reconcile_positions() == "mismatch"


async def test_a_backfill_that_raises_is_still_undecided(make_env, monkeypatch):
    """原有的保护不变：补拉直接抛出（账本忙等）同样不下结论。"""
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "2")]

    async def broken(symbols):
        raise RuntimeError("账本忙")

    monkeypatch.setattr(env.ingest, "backfill_trades", broken)
    assert await env.reconciler.reconcile_positions() == "unknown"
    assert env.suppressions() == set() and await open_diffs(env) == []
