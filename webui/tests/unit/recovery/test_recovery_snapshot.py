"""重启恢复第 2 步：拉一次全账户快照（持仓、普通挂单、条件单、服务器时间）。读不到就重试；仍读不到就什么都不判——
「未知」绝不当成「空」：不登记待确认、不判已平、不补保护，也不放开交易；暂缓的成交全部应用（没有快照就没有哪些「已被反映」的说法）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.recovery import Recovery
from dmr_kernel.core.types import PositionStatus, Suppression
from recovery_kit import algo, open_order, pos, trade

READS = ["server_time", "fetch_positions", "fetch_open_orders", "fetch_open_algo_orders"]


async def test_the_snapshot_reads_positions_open_orders_algo_orders_and_the_server_time(make_env):
    env = await make_env().boot()
    env.positions = [pos()]
    env.open_orders = [open_order()]
    env.algos = [algo(cid="web_9")]
    result = await env.run()
    read = {c[1] for c in env.calls if isinstance(c, tuple) and c[0] == "read"}
    assert set(READS) <= read
    snap = result.steps["snapshot"]
    assert snap["ok"] is True and snap["attempts"] == 1
    assert (snap["positions"], snap["open_orders"], snap["algo_orders"]) == (1, 1, 1)
    assert snap["snapshot_ms"] == env.clock.now_ms()


async def test_the_server_time_is_read_before_the_other_items(make_env):
    """快照的「截止时间」是先读到的服务器时间（与持仓对账一致）：之后到的成交算「快照之后」，释放时会应用。"""
    env = await make_env().boot()
    await env.run()
    reads = [c[1] for c in env.calls if isinstance(c, tuple) and c[0] == "read"]
    assert reads.index("server_time") < reads.index("fetch_positions")


@pytest.mark.parametrize("name", READS)
async def test_an_unreadable_item_is_retried_and_the_snapshot_still_succeeds(make_env, name):
    env = await make_env().boot()
    env.fail_next(name, Recovery.SNAPSHOT_ATTEMPTS - 1)
    result = await env.run()
    snap = result.steps["snapshot"]
    assert snap["ok"] is True and snap["attempts"] == Recovery.SNAPSHOT_ATTEMPTS
    assert result.tradable is True


@pytest.mark.parametrize("name", READS)
async def test_a_snapshot_that_stays_unknown_changes_nothing_and_does_not_open_trading(make_env, name):
    env = await make_env().boot()
    registered = await env.add_position()  # 登记仓，没有止损
    gone = await env.add_position(symbol="ETHUSDT")  # 登记仓，交易所这一侧「看起来」没有
    env.positions = [pos()]  # 另有一个交易所有、登记簿没有的仓
    env.positions.append(pos("DOGEUSDT"))
    env.fail(name)
    result = await env.run()
    snap = result.steps["snapshot"]
    assert snap["ok"] is False and snap["attempts"] == Recovery.SNAPSHOT_ATTEMPTS
    # 未知不是空：不补保护、不判已平、不登记待确认
    assert env.protection.rebuilt == []
    assert env.registry.get_active("DOGEUSDT", registered.side) is None
    assert env.registry.by_id(registered.id).status is PositionStatus.MANAGED
    assert env.registry.by_id(gone.id).status is PositionStatus.MANAGED
    # 不放开交易，并说清原因；暂缓的成交照样释放
    assert result.tradable is False and any("快照" in p for p in result.problems)
    assert Suppression.RECOVERING in env.suppressions()
    assert env.registry.holding is False and env.released == [0]
    assert env.alerts("recovery_snapshot_unknown")


async def test_without_a_snapshot_every_held_fill_is_applied_in_order(make_env):
    """没有快照就没有「已被快照反映」的成交：暂缓的成交全部应用（release 传 0）。"""
    env = await make_env().boot()
    env.fail("fetch_positions")

    async def fill_arrives_over_the_stream():
        await env.ingest.ingest_trade(trade("s1", qty="1", time_ms=env.clock.now_ms() - 60_000, order_id="X1"),
                                      client_order_id=env.own_id(intent=5, leg="o", model="1"), path="stream")

    env.stream.on_start = fill_arrives_over_the_stream
    await env.run()
    rec = env.registry.get_active("BTCUSDT", pos().side)
    assert rec is not None and rec.status is PositionStatus.MANAGED  # 一分钟前的系统开仓成交也应用了


async def test_a_read_that_raises_is_treated_as_unknown(make_env):
    env = await make_env().boot()

    def boom(**kwargs):
        raise RuntimeError("适配层炸了")

    env.adapter.set_read("fetch_positions", boom)
    result = await env.run()
    assert result.steps["snapshot"]["ok"] is False and result.tradable is False


async def test_the_exchange_is_read_again_on_a_second_run(make_env):
    env = await make_env().boot()
    recovery = env.recovery()
    await recovery.run()
    env.positions = [pos()]
    result = await recovery.run()
    assert result.steps["snapshot"]["positions"] == 1
