"""重启恢复第 3 步：回查账本里所有未到终态的订单与意图。快照里带系统前缀的单按编号认领、回写单号；其余逐条按编号回查，
查不清的保持未知（绝不当成「不存在」去拒绝）；补拉成交；协调者接手孤立意图（只回查、绝不重发）。"""
from __future__ import annotations

from dmr_kernel.core.types import IntentState, OrderState, Suppression
from recovery_kit import D, LONG, SYM2, algo, open_order, order_view, pos

DAY_MS = 24 * 3_600_000


def fetch_calls(env, name: str) -> list:
    return [kw for n, kw in env.adapter.read_calls if n == name]


# ================================================================ 认领与回查


async def test_system_algo_orders_in_the_snapshot_are_claimed_and_their_ids_written_back(make_env):
    env = await make_env().boot()
    info = await env.system_stop(state="pending")  # 落盘后没有回音；交易所上已经有这张条件单
    cid = env.last_stop_order.client_order_id
    before = await env.orders.get(cid)
    assert before.algo_id is None and before.state is OrderState.PENDING_SEND
    result = await env.run()
    after = await env.orders.get(cid)
    assert after.algo_id == info.algo_id and after.state is OrderState.ACKNOWLEDGED
    assert result.steps["orders"]["claimed"] == 1 and result.steps["orders"]["requeried"] == 0
    assert fetch_calls(env, "fetch_algo_order") == []  # 快照里认领到的不再单独回查


async def test_system_open_orders_in_the_snapshot_are_claimed_too(make_env):
    env = await make_env().boot()
    order = await env.open_order_row(exchange_view=None)
    view = order_view(cid=order.client_order_id, oid="5555", status="NEW", filled="0")
    env.open_orders = [view]
    result = await env.run()
    after = await env.orders.get(order.client_order_id)
    assert after.exchange_order_id == "5555" and after.state is OrderState.ACKNOWLEDGED
    assert result.steps["orders"]["claimed"] == 1 and fetch_calls(env, "fetch_order") == []


async def test_other_unfinished_orders_are_requeried_by_client_id(make_env):
    env = await make_env().boot()
    order = await env.open_order_row()  # 交易所那边已经全部成交，回执没到
    result = await env.run()
    after = await env.orders.get(order.client_order_id)
    assert after.state is OrderState.FILLED and after.filled_qty == D("1") and after.exchange_order_id
    assert result.steps["orders"]["requeried"] == 1 and result.steps["orders"]["unresolved"] == []
    assert fetch_calls(env, "fetch_order")[0]["client_order_id"] == order.client_order_id


async def test_a_finished_stop_is_requeried_by_its_algo_id_when_known(make_env):
    """条件单已触发（不再在未结列表里）：登记过条件单号的按条件单号查，查得到终态。"""
    env = await make_env().boot()
    info = await env.system_stop(state="acked", on_exchange=False)
    env.algo_views[info.algo_id] = algo(cid=info.client_algo_id, algo_id=info.algo_id, raw_status="FINISHED",
                                        actual_order_id="TRIG1")
    await env.run()
    after = await env.orders.get(env.last_stop_order.client_order_id)
    assert after.state in (OrderState.FILLED, OrderState.CANCELLED) and after.actual_order_id == "TRIG1"
    assert fetch_calls(env, "fetch_algo_order")[0]["algo_id"] == info.algo_id


async def test_orders_that_are_already_final_are_not_queried(make_env):
    env = await make_env().boot()
    order = await env.open_order_row()
    await env.orders.requery(order.client_order_id)  # 先查成终态（FILLED）
    env.adapter.read_calls.clear()
    await env.run()
    assert fetch_calls(env, "fetch_order") == []


# ================================================================ 查不清：保持未知，不当成「不存在」


async def test_an_order_whose_query_is_unknown_stays_unknown_and_is_reported(make_env):
    env = await make_env().boot()
    order = await env.open_order_row()
    env.fail("fetch_order")
    result = await env.run()
    after = await env.orders.get(order.client_order_id)
    assert after.state is OrderState.PENDING_SEND and after.absent_count == 0  # 未知不计「不存在」
    assert order.client_order_id in result.steps["orders"]["unresolved"]


async def test_an_order_the_exchange_says_is_absent_is_not_rejected_by_recovery(make_env):
    """交易所明确答复「不存在」也只计一次：拒绝要连续 3 次、过了签名时间窗、持仓没变（协调者的回查去判），恢复自己不拒绝。"""
    env = await make_env().boot()
    order = await env.open_order_row(exchange_view=False)
    result = await env.run()
    after = await env.orders.get(order.client_order_id)
    assert after.state is OrderState.PENDING_SEND and after.absent_count == 1
    assert order.client_order_id in result.steps["orders"]["unresolved"]


async def test_a_query_that_raises_does_not_stop_the_other_orders(make_env):
    env = await make_env().boot()
    first = await env.open_order_row()
    second = await env.open_order_row(symbol=SYM2)
    original = env.orders.requery

    async def flaky(cid):
        if cid == first.client_order_id:
            raise RuntimeError("账本忙")
        return await original(cid)

    env.orders.requery = flaky
    result = await env.run()
    assert (await env.orders.get(second.client_order_id)).state is OrderState.FILLED
    assert first.client_order_id in result.steps["orders"]["unresolved"]


# ================================================================ 补拉成交、协调者接手孤立意图


async def test_trades_are_backfilled_and_the_coordinator_takes_over_orphan_intents_afterwards(make_env):
    env = await make_env().boot()
    await env.open_order_row()
    await env.run()
    order_query = env.index_of("read", nth=_first_read(env, "fetch_order"))
    trades_read = env.index_of("read", nth=_first_read(env, "fetch_trades"))
    resolve = env.index_of("resolve_unknown_orders")
    assert order_query < trades_read < resolve  # 先回查订单（回写订单号），再补拉成交（据此认领），最后协调者接手意图


def _first_read(env, name: str) -> int:
    """第几个 ('read', ...) 调用是 name（env.index_of 按序号找 'read'）。"""
    reads = [c for c in env.calls if isinstance(c, tuple) and c[0] == "read"]
    return next(i for i, c in enumerate(reads) if c[1] == name)


async def test_trades_are_backfilled_for_symbols_with_positions_orders_and_recent_or_open_intents(make_env):
    env = await make_env().boot()
    env.positions = [pos("BTCUSDT")]
    env.open_orders = [open_order("ETHUSDT")]
    await env.new_intent(symbol="DOGEUSDT", state=IntentState.ACTIVE)  # 未到终态的意图
    await env.new_intent(symbol="XRPUSDT", state=IntentState.COMPLETED)  # 近期的意图
    result = await env.run()
    expected = ["BTCUSDT", "DOGEUSDT", "ETHUSDT", "XRPUSDT"]
    assert sorted(kw["symbol"] for kw in fetch_calls(env, "fetch_trades")) == expected
    assert result.steps["orders"]["backfill_symbols"] == expected


async def test_symbols_with_registered_positions_are_backfilled_even_when_flat_on_the_exchange(make_env):
    env = await make_env().boot()
    await env.add_position(symbol="SOLUSDT")
    await env.run()
    assert [kw["symbol"] for kw in fetch_calls(env, "fetch_trades")] == ["SOLUSDT"]


async def test_old_intents_do_not_widen_the_backfill(make_env):
    env = await make_env().boot()
    await env.new_intent(symbol="OLDUSDT", state=IntentState.COMPLETED)
    env.clock.advance(3 * DAY_MS)
    await env.run()
    assert fetch_calls(env, "fetch_trades") == []


class _Backfill(int):
    """FU5 之后 Ingest.backfill_trades 返回的样子：整数（新入账笔数）加 failed（读成交返回未知的合约）。"""

    failed = frozenset({"BTCUSDT"})


async def test_a_symbol_whose_trades_could_not_be_read_is_undecided(make_env):
    """成交没补拉成：这个合约上不能把交易所的仓当成「登记簿没有的外部仓」——它可能是系统自己刚开的。"""
    env = await make_env().boot()
    env.positions = [pos("BTCUSDT"), pos("ETHUSDT")]

    async def backfill(symbols):
        return _Backfill(0)

    env.ingest.backfill_trades = backfill
    result = await env.run()
    assert env.registry.get_active("BTCUSDT", LONG) is None
    assert env.registry.get_active("ETHUSDT", LONG) is not None
    assert result.steps["orders"]["backfill_failed"] == ["BTCUSDT"]
    (deferred,) = result.steps["reconcile"]["deferred"]
    assert deferred["symbol"] == "BTCUSDT" and "没有补拉成功" in deferred["why"]


async def test_a_backfill_that_raises_leaves_every_symbol_undecided(make_env):
    env = await make_env().boot()
    env.positions = [pos("BTCUSDT")]

    async def backfill(symbols):
        raise RuntimeError("账本忙")

    env.ingest.backfill_trades = backfill
    result = await env.run()
    assert env.registry.get_active("BTCUSDT", LONG) is None
    assert "backfill_error" in result.steps["orders"]
    assert result.steps["reconcile"]["deferred"][0]["symbol"] == "BTCUSDT"


async def test_trades_that_could_not_be_read_keep_the_recovering_suppression(make_env):
    """读成交返回未知 = 登记簿可能缺了停机期间的成交（开仓没登记、止损触发没补记）：未知不当成「没有成交」，不放开交易。"""
    env = await make_env().boot()
    env.positions = [pos("BTCUSDT")]

    async def backfill(symbols):
        return _Backfill(0)

    env.ingest.backfill_trades = backfill
    result = await env.run()
    assert result.tradable is False
    assert any("BTCUSDT" in p and "成交" in p for p in result.problems)
    assert Suppression.RECOVERING in env.suppressions()
    assert env.alerts("recovery_backfill_failed")


async def test_a_backfill_that_raises_keeps_the_recovering_suppression(make_env):
    env = await make_env().boot()
    env.positions = [pos("BTCUSDT")]

    async def backfill(symbols):
        raise RuntimeError("账本忙")

    env.ingest.backfill_trades = backfill
    result = await env.run()
    assert result.tradable is False and any("账本忙" in p for p in result.problems)
    assert Suppression.RECOVERING in env.suppressions()


async def test_a_complete_backfill_is_not_a_problem(make_env):
    env = await make_env().boot()
    env.positions = [pos("BTCUSDT")]
    result = await env.run()
    assert result.steps["orders"]["backfill_symbols"] == ["BTCUSDT"]
    assert result.steps["orders"]["backfill_failed"] == []
    assert result.tradable is True and Suppression.RECOVERING not in env.suppressions()


async def test_without_a_snapshot_the_orders_are_still_requeried(make_env):
    """快照读不到也要回查订单（查订单是单独的读）：只是没有「认领」可做。"""
    env = await make_env().boot()
    order = await env.open_order_row()
    env.fail("fetch_positions")
    result = await env.run()
    assert (await env.orders.get(order.client_order_id)).state is OrderState.FILLED
    assert result.steps["orders"]["claimed"] == 0 and result.steps["orders"]["requeried"] == 1


async def test_an_algo_order_that_the_snapshot_does_not_show_is_not_claimed(make_env):
    """快照里没有的系统条件单（已触发、已撤）不能当成「认领」，要按编号回查。"""
    env = await make_env().boot()
    info = await env.system_stop(state="acked", on_exchange=False)
    await env.run()
    calls = fetch_calls(env, "fetch_algo_order")
    assert calls and calls[0]["algo_id"] == info.algo_id
