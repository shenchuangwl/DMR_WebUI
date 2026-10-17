"""保护单管理：仓位归零后清理系统的保护单（F3 第 8 步、F5 第 8 步、F8、F2 第 7 步、F13）。

清理三类：当前止损、待清理的旧单、已移交的单。撤成功（已撤或交易所说不存在）才清掉登记簿里对应的编号；
仓位还有数量时绝不撤。崩溃点 ``close_filled_before_cleanup`` 在清理之前。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.core.events import OrdersSnapshotTaken
from dmr_kernel.core.types import (
    AlgoType,
    CancelOutcome,
    CancelResult,
    ExitReason,
    ProtectionState,
)
from protection_kit import LONG, SYM, D, history_then

RISING = [100.5, 101.0, 101.5, 102.0, 102.5, 103.0]


async def close_position(rig, rec):
    """登记簿里把仓位平掉（成交归零），交易所一侧也没有仓了。"""
    rig.exchange.set_held(rec.symbol, rec.side, 0)
    return await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.SIGNAL, closed_ms=rig.clock.now_ms())


async def strategy_with_stop(rig, price="97"):
    rec = await rig.open_strategy()
    return await rig.with_system_stop(rec, price)


async def test_cleanup_cancels_the_current_stop_and_clears_its_registration(rig):
    await rig.start()
    rec, stop = await strategy_with_stop(rig)
    await close_position(rig, rec)
    await rig.protection.cleanup_after_close(rec.id)
    (cancel,) = rig.writes("cancel_algo_order")
    assert cancel["client_algo_id"] == stop.client_algo_id
    assert rig.exchange.find(SYM, algo_id=stop.algo_id).raw_status == "CANCELED" and rig.exchange.open_algos() == []
    row = await rig.row(rec.id)
    assert (row.stop_client_id, row.stop_algo_id, row.stop_price) == (None, None, None)
    assert row.protection_state is ProtectionState.NONE


async def test_cleanup_also_cancels_the_stale_old_stops(rig):
    await rig.start()
    rec, stop = await strategy_with_stop(rig)
    old1, old2 = rig.exchange.add_stop(SYM, LONG, "95"), rig.exchange.add_stop(SYM, LONG, "94")
    for old in (old1, old2):
        await rig.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id=old.algo_id)
    await close_position(rig, rec)
    await rig.protection.cleanup_after_close(rec.id)
    assert rig.exchange.open_algos() == [] and len(rig.writes("cancel_algo_order")) == 3
    assert (await rig.row(rec.id)).stale_stops == []


async def test_cleanup_also_cancels_the_handed_over_stop_by_its_exchange_id(rig):
    """已移交的止损：仓位非零时系统不动它，归零后由系统撤销（避免它作用到之后同方向的新仓上）。用户的单只能按交易所单号撤。"""
    await rig.start()
    rec = await rig.open_takeover()
    user = rig.exchange.add_stop(SYM, LONG, "90", cid=None)
    await rig.registry.set_handed_over(rec.id, {"client_algo_id": None, "algo_id": user.algo_id})
    await close_position(rig, rec)
    await rig.protection.cleanup_after_close(rec.id)
    (cancel,) = rig.writes("cancel_algo_order")
    assert cancel["algo_id"] == user.algo_id and cancel["client_algo_id"] is None
    assert rig.exchange.open_algos() == [] and (await rig.row(rec.id)).handed_over_stop is None


async def test_cleanup_takes_all_three_kinds_and_never_touches_a_take_profit(rig):
    await rig.start()
    rec, stop = await strategy_with_stop(rig)
    old = rig.exchange.add_stop(SYM, LONG, "95")
    await rig.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id=old.algo_id)
    handed = rig.exchange.add_stop(SYM, LONG, "90", cid=None)
    await rig.registry.set_handed_over(rec.id, {"algo_id": handed.algo_id})
    tp = rig.exchange.add_stop(SYM, LONG, "120", cid=None, algo_type=AlgoType.TAKE_PROFIT_MARKET)
    await close_position(rig, rec)
    await rig.protection.cleanup_after_close(rec.id)
    assert rig.exchange.open_algos() == [tp]  # 用户的止盈一律不动
    row = await rig.row(rec.id)
    assert row.stale_stops == [] and row.handed_over_stop is None and row.stop_algo_id is None


async def test_a_stop_that_is_already_gone_counts_as_cleaned(rig):
    await rig.start()
    rec, stop = await strategy_with_stop(rig)
    rig.exchange.mutate(stop, raw_status="FINISHED")  # 止损触发了：撤单得到「不存在」
    await close_position(rig, rec)
    await rig.protection.cleanup_after_close(rec.id)
    assert (await rig.row(rec.id)).stop_algo_id is None


async def test_a_cancel_with_unknown_result_keeps_the_registration_for_a_retry(make_rig):
    rig = make_rig(wait_s=0.1)
    await rig.start()
    rec, stop = await strategy_with_stop(rig)
    rig.exchange.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    await close_position(rig, rec)
    await rig.protection.cleanup_after_close(rec.id)
    row = await rig.row(rec.id)
    assert row.stop_algo_id == stop.algo_id and rig.exchange.stops() != []  # 没撤成：编号留着
    await rig.protection.cleanup_after_close(rec.id)  # 再来一次：撤掉了
    row = await rig.row(rec.id)
    assert row.stop_algo_id is None and rig.exchange.open_algos() == []


async def test_cleanup_is_idempotent(rig):
    await rig.start()
    rec, _ = await strategy_with_stop(rig)
    await close_position(rig, rec)
    await rig.protection.cleanup_after_close(rec.id)
    await rig.protection.cleanup_after_close(rec.id)
    assert len(rig.writes("cancel_algo_order")) == 1


async def test_a_position_that_still_has_quantity_is_never_cleaned(rig):
    await rig.start()
    rec, stop = await strategy_with_stop(rig)
    await rig.protection.cleanup_after_close(rec.id)
    assert rig.exchange.writes == [] and rig.exchange.stops() != []
    assert (await rig.row(rec.id)).stop_algo_id == stop.algo_id


async def test_a_handed_over_stop_is_left_alone_while_the_position_is_open(rig):
    await rig.start()
    rec = await rig.open_takeover()
    rec = await rig.registry.mark_managed(rec.id)
    user = rig.exchange.add_stop(SYM, LONG, "90", cid=None)
    await rig.registry.set_handed_over(rec.id, {"algo_id": user.algo_id})
    await rig.registry.ignore(rec.id, expected_version=None)  # 取消托管：保留止损并移交，之后该仓转为外部（已忽略）
    await rig.protection.start()
    for _ in range(2):
        rig.bus.publish(OrdersSnapshotTaken(open_orders=(), algo_orders=tuple(rig.exchange.open_algos()),
                                            at_ms=rig.clock.now_ms()))
    await rig.settle()
    await rig.protection.tick()
    assert rig.exchange.writes == [] and user in rig.exchange.open_algos()


# ---------------------------------------------------------------- 崩溃点


async def test_crash_point_close_filled_before_cleanup_fires_before_anything_is_cancelled(rig):
    await rig.start()
    rec, stop = await strategy_with_stop(rig)
    await close_position(rig, rec)
    rig.crash.arm("close_filled_before_cleanup")
    with pytest.raises(CrashSignal):
        await rig.protection.cleanup_after_close(rec.id)
    assert rig.protection.crashed is not None and rig.exchange.writes == []
    assert (await rig.row(rec.id)).stop_algo_id == stop.algo_id  # 登记里还留着：重启后的实例照样清得掉
    reborn = rig.build_protection()
    await reborn.cleanup_after_close(rec.id)
    assert rig.exchange.open_algos() == [] and (await rig.row(rec.id)).stop_algo_id is None


async def test_after_a_crash_the_dead_instance_refuses_further_work(rig):
    await rig.start()
    rec, _ = await strategy_with_stop(rig)
    await close_position(rig, rec)
    rig.crash.arm("close_filled_before_cleanup")
    with pytest.raises(CrashSignal):
        await rig.protection.cleanup_after_close(rec.id)
    with pytest.raises(CrashSignal):
        await rig.protection.ensure_protected(rec.id, reason="x")
    await rig.protection.tick()  # 定时工作静默跳过


# ---------------------------------------------------------------- 事件驱动与时序


async def test_the_closed_event_triggers_the_cleanup(rig):
    await rig.start()
    rec, stop = await strategy_with_stop(rig)
    await rig.protection.start()
    await close_position(rig, rec)
    await rig.settle()
    assert rig.exchange.open_algos() == [] and (await rig.row(rec.id)).stop_algo_id is None


async def test_the_closed_event_clears_the_unprotected_suppression_of_a_position_that_is_gone(rig):
    await rig.start()
    rec = await rig.open_strategy()  # 无保护
    await rig.protection.start()
    await rig.protection.check_unprotected()
    assert rig.suppressed()
    await close_position(rig, rec)
    await rig.settle()
    assert not rig.suppressed()


async def test_a_new_stop_that_lands_on_an_already_flat_position_is_cleaned_up(rig):
    """gap 模式撤旧单得到「不存在」（旧止损触发、仓位归零），随后挂上的新止损落在零仓上：由归零后的清理撤掉。"""
    await rig.start()
    rec, old = await strategy_with_stop(rig)
    rig.candles.set(SYM, history_then(RISING))
    rig.exchange.set_price(SYM, "105")
    await rig.protection.start()

    fired = []

    async def hook(name, kw):
        if name == "cancel_algo_order" and not fired:  # 第一个撤单请求到达前：旧止损已触发，仓位归零
            fired.append(name)
            rig.exchange.mutate(old, raw_status="FINISHED")
            await close_position(rig, rec)

    rig.exchange.hook = hook
    await rig.protection.trailing(rig.candles.frame(SYM, "15m")["open_time_ms"].iloc[-1].item())
    await rig.settle()
    assert rig.exchange.open_algos() == []  # 新止损没有留在零仓上
    names = rig.names()
    assert names.count("create_stop_order") == 1 and names.count("cancel_algo_order") == 2  # 旧单（不存在）+ 新单（清理）
    row = await rig.row(rec.id)
    assert row.stop_algo_id is None and row.stale_stops == []


async def test_a_stop_placed_as_the_position_closes_is_cleaned_up(rig):
    await rig.start()
    rec = await rig.open_strategy()

    async def hook(name, kw):
        if name == "create_stop_order":
            await close_position(rig, rec)  # 挂止损的请求在途时，仓位被平掉了

    rig.exchange.hook = hook
    assert (await rig.protection.ensure_protected(rec.id, reason="registered")) == "closed"
    assert rig.exchange.open_algos() == []
    assert (await rig.row(rec.id)).stop_algo_id is None
