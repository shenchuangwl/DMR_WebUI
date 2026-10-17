"""持仓登记：暂缓应用成交（hold_fills / release_fills，重启恢复 F9 第 2 步与第 10 步）。

暂缓期间成交照常入账（入账不归登记簿管，测试里 make_fill 先写 fill 行），经 FillIngested 事件到达的成交只暂存、不改登记簿；
释放时早于（含等于）快照时间的成交只入账、不改登记簿（快照已经反映了它们），之后的按成交时间顺序应用。
直接调用 apply_fill 不受暂缓影响（恢复自己要回放一部分成交时用）。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from dmr_kernel.core.events import PositionClosed, PositionRegistered
from dmr_kernel.core.registry import FillRelease
from dmr_kernel.core.types import OrderOrigin
from registry_kit import D, LONG, SYM, T0, add_intent, fill_position_id, make_fill, trade_of

SNAP = T0 + 1_000  # 快照时间

ETH = "ETHUSDT"


async def up(rig):
    """装配好并订阅成交事件（和内核启动一样）。"""
    await rig.up()
    await rig.registry.start()
    return rig


async def deliver(rig, *fills) -> None:
    """把成交事件交给总线并等订阅者处理完。"""
    for fill in fills:
        rig.bus.publish(fill)
    await rig.events()


# ================================================================ 暂缓期间：只暂存，不改登记簿


async def test_a_held_fill_is_booked_but_not_applied_to_the_registry(rig):
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    fill = await make_fill(rig.ledger, qty="0.4", price="100", intent_id=iid, time_ms=SNAP + 5)
    await deliver(rig, fill)
    assert rig.registry.get_active(SYM, LONG) is None and rig.registry.all_active() == []
    assert await fill_position_id(rig.ledger, fill.fill_id) is None  # 成交行在账本里（已入账），但没有关联到持仓
    assert [e for e in await rig.events(PositionRegistered)] == []
    assert rig.registry.held_fills() == (fill,)
    assert rig.registry.holding is True


async def test_without_a_hold_the_subscription_applies_fills_at_once(rig):
    """对照：没有暂缓时订阅照常应用成交。"""
    await up(rig)
    iid = await add_intent(rig.ledger)
    fill = await make_fill(rig.ledger, qty="0.4", price="100", intent_id=iid, time_ms=SNAP + 5)
    await deliver(rig, fill)
    rec = rig.registry.get_active(SYM, LONG)
    assert rec is not None and rec.qty == D("0.4")
    assert rig.registry.holding is False and rig.registry.held_fills() == ()


async def test_hold_before_start_still_holds_once_the_subscription_exists(rig):
    await rig.up()
    rig.registry.hold_fills()
    await rig.registry.start()
    iid = await add_intent(rig.ledger)
    await deliver(rig, await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 1))
    assert rig.registry.all_active() == [] and len(rig.registry.held_fills()) == 1


async def test_holding_twice_keeps_what_was_already_held(rig):
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    first = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 1)
    await deliver(rig, first)
    rig.registry.hold_fills()  # 重复调用无害：已暂存的不丢
    assert rig.registry.held_fills() == (first,)


async def test_held_fills_are_listed_in_arrival_order(rig):
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    later = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 50)
    earlier = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 10)
    await deliver(rig, later, earlier)
    assert rig.registry.held_fills() == (later, earlier)  # 到达顺序，不是成交时间顺序


async def test_apply_fill_called_directly_is_not_held(rig):
    """恢复自己回放成交时直接调 apply_fill：不受暂缓影响。"""
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    fill = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP - 10)
    rec = await rig.registry.apply_fill(fill)
    assert rec is not None and rig.registry.get_active(SYM, LONG) == rec
    assert await fill_position_id(rig.ledger, fill.fill_id) == rec.id


# ================================================================ 释放：早于（含等于）快照的只入账，之后的按成交时间顺序应用


async def test_release_applies_later_fills_in_trade_time_order_not_arrival_order(rig):
    """到达顺序是 平0.4、开1、平0.6，成交时间顺序是 开1、平0.4、平0.6：按到达顺序应用会让平仓落空、留下 0.4 的残仓。"""
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    close_a = await make_fill(rig.ledger, qty="0.4", price="110", opening=False, intent_id=iid, time_ms=SNAP + 30)
    opening = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 10)
    close_b = await make_fill(rig.ledger, qty="0.6", price="110", opening=False, intent_id=iid, time_ms=SNAP + 50)
    await deliver(rig, close_a, opening, close_b)
    assert rig.registry.all_active() == []
    result = await rig.registry.release_fills(SNAP)
    assert result == FillRelease(applied=3, skipped=0, failed=0)
    assert rig.registry.get_active(SYM, LONG) is None  # 开 → 减 → 平：归零
    closed = [e for e in await rig.events(PositionClosed)]
    assert len(closed) == 1
    position_id = closed[0].position_id
    for fill in (opening, close_a, close_b):
        assert await fill_position_id(rig.ledger, fill.fill_id) == position_id
    trade = await trade_of(rig.ledger, position_id)
    assert trade is not None and trade.max_qty == D("1")


async def test_fills_in_the_same_millisecond_keep_their_arrival_order(rig):
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    opening = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 7)
    close = await make_fill(rig.ledger, qty="1", price="110", opening=False, intent_id=iid, time_ms=SNAP + 7)
    await deliver(rig, opening, close)
    result = await rig.registry.release_fills(SNAP)
    assert result.applied == 2
    assert rig.registry.get_active(SYM, LONG) is None  # 同一毫秒按到达顺序：先开后平


async def test_release_skips_fills_at_or_before_the_snapshot_time_and_boundary_is_inclusive(rig):
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    before = await make_fill(rig.ledger, qty="1", price="100", symbol=ETH, intent_id=iid, time_ms=SNAP - 1)
    exactly = await make_fill(rig.ledger, qty="1", price="100", symbol=ETH, position_side=LONG, intent_id=iid,
                              time_ms=SNAP)
    after = await make_fill(rig.ledger, qty="2", price="100", intent_id=iid, time_ms=SNAP + 1)
    await deliver(rig, before, exactly, after)
    result = await rig.registry.release_fills(SNAP)
    assert result == FillRelease(applied=1, skipped=2, failed=0)
    assert rig.registry.get_active(ETH, LONG) is None  # 早于或等于快照：只入账，不改登记簿
    assert await fill_position_id(rig.ledger, before.fill_id) is None
    assert await fill_position_id(rig.ledger, exactly.fill_id) is None
    btc = rig.registry.get_active(SYM, LONG)
    assert btc is not None and btc.qty == D("2")  # 之后一毫秒的成交照常应用
    assert await fill_position_id(rig.ledger, after.fill_id) == btc.id


async def test_a_skipped_fill_leaves_an_existing_position_untouched(rig):
    """快照已经反映了早于它的成交：登记簿里已有的仓不再被这笔成交改动（数量、版本都不变）。"""
    await up(rig)
    iid = await add_intent(rig.ledger)
    pos = await rig.open_strategy(qty="1", price="100", intent_id=iid)
    rig.registry.hold_fills()
    old = await make_fill(rig.ledger, qty="0.5", price="100", opening=False, intent_id=iid, time_ms=SNAP - 100)
    await deliver(rig, old)
    await rig.registry.release_fills(SNAP)
    rec = rig.registry.get_active(SYM, LONG)
    assert (rec.qty, rec.version) == (pos.qty, pos.version)


async def test_release_returns_the_counts_and_ends_the_hold(rig):
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    await deliver(rig,
                  await make_fill(rig.ledger, qty="1", price="100", symbol=ETH, intent_id=iid, time_ms=SNAP - 5),
                  await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 5))
    result = await rig.registry.release_fills(SNAP)
    assert (result.applied, result.skipped, result.failed) == (1, 1, 0)
    assert rig.registry.holding is False and rig.registry.held_fills() == ()


async def test_after_the_release_new_fills_are_applied_immediately(rig):
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    await rig.registry.release_fills(SNAP)
    fill = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 99)
    await deliver(rig, fill)
    assert rig.registry.get_active(SYM, LONG) is not None


async def test_release_without_a_hold_is_a_no_op(rig):
    await up(rig)
    assert await rig.registry.release_fills(SNAP) == FillRelease(0, 0, 0)
    assert rig.registry.holding is False


async def test_releasing_twice_does_not_apply_anything_twice(rig):
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    await deliver(rig, await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 5))
    assert (await rig.registry.release_fills(SNAP)).applied == 1
    assert await rig.registry.release_fills(SNAP) == FillRelease(0, 0, 0)
    assert rig.registry.get_active(SYM, LONG).qty == D("1")


async def test_a_fill_that_was_replayed_by_hand_is_not_applied_twice_at_release(rig):
    """恢复先用 apply_fill 回放一部分成交，释放时同一笔晚于快照的成交再来一次也只算一次（幂等）。"""
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    fill = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 5)
    await rig.registry.apply_fill(fill)
    await deliver(rig, fill)
    result = await rig.registry.release_fills(SNAP)
    assert result.applied == 1 and result.failed == 0
    rec = rig.registry.get_active(SYM, LONG)
    assert rec is not None and rec.qty == D("1") and rec.version == 1


# ================================================================ 释放期间到达的成交、出错的成交


async def test_a_fill_arriving_while_the_release_is_applying_waits_for_its_turn(rig):
    """释放期间到达的成交仍进缓冲，等这一批应用完再接着处理：不会插到前面去应用，顺序不乱。"""
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    first = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 10)
    second = await make_fill(rig.ledger, qty="1", price="110", opening=False, intent_id=iid, time_ms=SNAP + 30)
    await deliver(rig, first)
    entered, gate = asyncio.Event(), asyncio.Event()
    original = rig.registry.apply_fill
    applied_order: list[int] = []

    async def slow_apply(event):
        applied_order.append(event.fill_id)
        entered.set()
        await gate.wait()
        return await original(event)

    rig.registry.apply_fill = slow_apply  # type: ignore[method-assign]
    release = asyncio.create_task(rig.registry.release_fills(SNAP))
    await asyncio.wait_for(entered.wait(), 5)
    await deliver(rig, second)  # 此时释放还在应用 first
    assert rig.registry.holding is True and applied_order == [first.fill_id]
    gate.set()
    result = await asyncio.wait_for(release, 5)
    assert applied_order == [first.fill_id, second.fill_id]
    assert result.applied == 2
    assert rig.registry.holding is False
    assert rig.registry.get_active(SYM, LONG) is None  # 开 → 平，顺序对


async def test_a_fill_that_cannot_be_applied_does_not_stop_the_rest(rig):
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    good = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 20)
    ghost = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 10)
    ghost = replace(ghost, fill_id=987654)  # 账本里没有这一行
    await deliver(rig, ghost, good)
    result = await rig.registry.release_fills(SNAP)
    assert result == FillRelease(applied=1, skipped=0, failed=1)
    assert rig.registry.get_active(SYM, LONG) is not None
    assert rig.registry.holding is False


@pytest.mark.parametrize("bad", [-1, 1.5, "100", None, True])
async def test_the_snapshot_time_must_be_a_non_negative_integer(rig, bad):
    await up(rig)
    rig.registry.hold_fills()
    with pytest.raises(ValueError):
        await rig.registry.release_fills(bad)
    assert rig.registry.holding is True  # 参数错误不影响暂缓状态


async def test_a_cancelled_release_keeps_the_unapplied_fills_held(rig):
    """释放被取消：还没应用的成交退回缓冲、仍在暂缓，之后可以再释放（已经应用的靠幂等不会重复）。"""
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    first = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 10)
    second = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 20)
    await deliver(rig, first, second)
    original = rig.registry.apply_fill
    entered, gate = asyncio.Event(), asyncio.Event()

    async def slow_apply(event):
        entered.set()
        await gate.wait()
        return await original(event)

    rig.registry.apply_fill = slow_apply  # type: ignore[method-assign]
    release = asyncio.create_task(rig.registry.release_fills(SNAP))
    await asyncio.wait_for(entered.wait(), 5)
    release.cancel()
    with pytest.raises(asyncio.CancelledError):
        await release
    gate.set()
    await asyncio.sleep(0)
    assert rig.registry.holding is True
    assert len(rig.registry.held_fills()) >= 1  # 没应用完的仍在缓冲里
    rig.registry.apply_fill = original  # type: ignore[method-assign]
    result = await rig.registry.release_fills(SNAP)
    assert result.failed == 0 and rig.registry.holding is False
    assert rig.registry.get_active(SYM, LONG).qty == D("2")  # 两笔都应用，且只应用一次


async def test_external_fills_are_held_too(rig):
    """暂缓对所有来源一视同仁：外部成交也先暂存。"""
    await up(rig)
    rig.registry.hold_fills()
    await deliver(rig, await make_fill(rig.ledger, qty="1", price="100", origin=OrderOrigin.EXTERNAL,
                                       time_ms=SNAP + 5))
    assert len(rig.registry.held_fills()) == 1 and rig.registry.all_active() == []


async def test_the_same_fill_arriving_twice_while_held_is_applied_once(rig):
    """同一笔成交（数据流与补拉各送一次）在暂缓期间到了两次：释放时只改一次登记簿（开 1 再加 0.5 = 1.5，不是 2）。"""
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    opening = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP + 5)
    adding = await make_fill(rig.ledger, qty="0.5", price="100", intent_id=iid, time_ms=SNAP + 9)
    await deliver(rig, adding, opening, adding)
    await rig.registry.release_fills(SNAP)
    rec = rig.registry.get_active(SYM, LONG)
    assert rec is not None and rec.qty == D("1.5")


async def test_a_duplicate_of_a_fill_at_the_snapshot_time_is_still_only_booked(rig):
    """边界上的成交（时间等于快照时间）重复到达：两次都只入账，登记簿不动。"""
    await up(rig)
    iid = await add_intent(rig.ledger)
    rig.registry.hold_fills()
    edge = await make_fill(rig.ledger, qty="1", price="100", intent_id=iid, time_ms=SNAP)
    await deliver(rig, edge, edge)
    assert await rig.registry.release_fills(SNAP) == FillRelease(applied=0, skipped=2, failed=0)
    assert rig.registry.get_active(SYM, LONG) is None
    assert await fill_position_id(rig.ledger, edge.fill_id) is None
