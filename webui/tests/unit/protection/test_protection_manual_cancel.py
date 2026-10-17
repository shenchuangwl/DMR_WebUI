"""保护单管理：系统止损被人手动撤掉（F2 第 9 步，4.11：90 秒内重新存在）。

发现的两条路：数据流的条件单事件（交易所推送打开时）与条件单对账的快照。发现后先直接问交易所确认（读不到不动），
确认不在才按登记的止损价补挂。
"""
from __future__ import annotations

import asyncio

import pytest

from dmr_kernel.core.events import AlgoOrderObserved, OrdersSnapshotTaken
from dmr_kernel.core.types import (
    AlertLevel,
    CancelOutcome,
    CancelResult,
    ProtectionState,
    Unknown,
)
from protection_kit import LONG, SYM, D, flat_history, history_then

RISING = [100.5, 101.0, 101.5, 102.0, 102.5, 103.0]


async def protected_position(rig, *, start=True, price="97"):
    """一个登记成系统保护的策略仓（止损在交易所上），并让保护单管理开始订阅事件。"""
    rec = await rig.open_strategy()
    rec, algo = await rig.with_system_stop(rec, price)
    if start:
        await rig.protection.start()
    return rec, algo


def observe(rig, algo, **changes):
    view = rig.exchange.mutate(algo, **changes) if changes else algo
    rig.bus.publish(AlgoOrderObserved(algo=view, source="stream", at_ms=rig.clock.now_ms()))


async def snapshot(rig, at_ms=None):
    rig.bus.publish(OrdersSnapshotTaken(open_orders=(), algo_orders=tuple(rig.exchange.open_algos()),
                                        at_ms=rig.clock.now_ms() if at_ms is None else at_ms))
    await rig.settle()


# ---------------------------------------------------------------- 数据流事件


async def test_a_cancel_seen_on_the_stream_is_rebuilt_at_the_registered_price(rig):
    await rig.start()
    rec, old = await protected_position(rig)
    rig.candles.set(SYM, flat_history(center=110.0))  # 现在重新算会得到别的价：补挂必须按登记的 97
    observe(rig, old, raw_status="CANCELED")
    await rig.settle()
    (create,) = rig.writes("create_stop_order")
    assert create["trigger_price"] == D("97")
    (new,) = rig.exchange.stops()
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97")
    assert (row.stop_client_id, row.stop_algo_id) == (new.client_algo_id, new.algo_id) != (old.client_algo_id, old.algo_id)
    (alert,) = rig.notifier.of("stop_missing_rebuilt")
    assert alert["level"] is AlertLevel.WARNING


@pytest.mark.parametrize("raw", ["CANCELED", "EXPIRED", "REJECTED"])
async def test_a_stop_that_ended_without_triggering_is_rebuilt(rig, raw):
    await rig.start()
    rec, old = await protected_position(rig)
    observe(rig, old, raw_status=raw)
    await rig.settle()
    assert len(rig.writes("create_stop_order")) == 1 and len(rig.exchange.stops()) == 1


@pytest.mark.parametrize("raw", ["TRIGGERED", "FINISHED"])
async def test_a_stop_that_triggered_is_not_rebuilt(rig, raw):
    """止损触发了（成交会让持仓归零）：不是被撤，不补挂。"""
    await rig.start()
    rec, old = await protected_position(rig)
    observe(rig, old, raw_status=raw)
    await rig.settle()
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM


async def test_an_open_or_unrelated_stop_event_changes_nothing(rig):
    await rig.start()
    rec, old = await protected_position(rig)
    observe(rig, old)  # 还开着的（比如刚建单的推送）
    stranger = rig.exchange.add_stop(SYM, LONG, "80", cid=None)
    observe(rig, stranger, raw_status="CANCELED")  # 用户自己的单被撤
    other_side = rig.exchange.add_stop(SYM, LONG, "80", cid="auto")  # 系统编号但不是登记的那张
    observe(rig, other_side, raw_status="CANCELED")
    await rig.settle()
    assert rig.exchange.writes == []


async def test_the_cancel_event_of_a_stop_we_replaced_ourselves_is_ignored(rig):
    """移动止损撤掉旧单后，旧单的「已撤」推送随后才到：旧单已不是登记的当前止损，不能补挂（否则会多出一张）。"""
    await rig.start()
    rec, old = await protected_position(rig)
    rig.candles.set(SYM, history_then(RISING))
    rig.exchange.set_price(SYM, "105")
    await rig.protection.trailing(rig.candles.frame(SYM, "15m")["open_time_ms"].iloc[-1].item())
    await rig.settle()
    assert len(rig.writes("create_stop_order")) == 1
    observe(rig, rig.exchange.find(SYM, algo_id=old.algo_id))  # 旧单已被协调者撤掉：状态是 CANCELED
    await rig.settle()
    assert len(rig.writes("create_stop_order")) == 1 and len(rig.exchange.stops()) == 1


async def test_the_cancel_event_arriving_during_a_replace_does_not_cause_a_second_stop(rig):
    await rig.start()
    rec, old = await protected_position(rig)
    rig.candles.set(SYM, history_then(RISING))
    rig.exchange.set_price(SYM, "105")

    async def hook(name, kw):
        if name == "create_stop_order":  # 旧单刚撤、新单还没挂的空窗里，旧单的「已撤」推送到了
            observe(rig, rig.exchange.find(SYM, algo_id=old.algo_id))
            for _ in range(5):
                await asyncio.sleep(0)

    rig.exchange.hook = hook
    await rig.protection.trailing(rig.candles.frame(SYM, "15m")["open_time_ms"].iloc[-1].item())
    await rig.settle()
    assert len(rig.writes("create_stop_order")) == 1 and len(rig.exchange.stops()) == 1
    assert (await rig.row(rec.id)).stop_price == D("97.5")


# ---------------------------------------------------------------- 条件单快照


async def test_a_snapshot_without_the_stop_rebuilds_it_within_ninety_seconds(rig):
    await rig.start()
    rec, old = await protected_position(rig)
    cancelled_at = rig.clock.now_ms()
    rig.exchange.mutate(old, raw_status="CANCELED")  # 用户手动撤掉；推送没打开，只能靠对账快照发现
    rig.advance(60_000)  # 条件单对账周期 60 秒
    await snapshot(rig)
    (new,) = rig.exchange.stops()
    assert rig.clock.now_ms() - cancelled_at <= 90_000  # 4.11：手动撤掉系统止损后 90 秒内重新存在
    row = await rig.row(rec.id)
    assert row.stop_price == D("97") and row.stop_algo_id == new.algo_id and rig.notifier.of("stop_missing_rebuilt")


async def test_a_snapshot_with_the_stop_changes_nothing(rig):
    await rig.start()
    rec, old = await protected_position(rig)
    for _ in range(3):
        await snapshot(rig)
    assert rig.exchange.writes == [] and rig.notifier.alerts == []


async def test_a_stop_in_cancel_pending_state_that_the_exchange_still_holds_is_still_valid(rig):
    """订单状态 CANCEL_PENDING（撤单结果未知）而交易所仍挂着的止损视为仍然有效：不补挂。"""
    await rig.start()
    rec = await rig.open_strategy()
    assert (await rig.protection.ensure_protected(rec.id, reason="registered")) == "protected"
    row = await rig.row(rec.id)
    await rig.orders.apply_cancel_result(row.stop_client_id, CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    assert (await rig.orders.get(row.stop_client_id)).state.value == "CANCEL_PENDING"
    await rig.protection.start()
    await snapshot(rig)
    assert len(rig.writes("create_stop_order")) == 1  # 只有最初那一张
    assert (await rig.row(rec.id)).stop_algo_id == row.stop_algo_id


async def test_a_snapshot_older_than_our_last_placement_does_not_count(rig):
    await rig.start()
    rec = await rig.open_strategy()
    await rig.protection.ensure_protected(rec.id, reason="registered")
    await rig.protection.start()
    reads = len(rig.exchange.calls("fetch_open_algo_orders"))
    rig.bus.publish(OrdersSnapshotTaken(open_orders=(), algo_orders=(), at_ms=rig.clock.now_ms() - 1))
    await rig.settle()  # 快照比刚才挂单还早：里面没有这张止损不说明什么
    assert len(rig.exchange.calls("fetch_open_algo_orders")) == reads and len(rig.writes("create_stop_order")) == 1


async def test_a_snapshot_that_missed_the_stop_is_checked_against_the_exchange_before_rebuilding(rig):
    await rig.start()
    rec, old = await protected_position(rig)
    rig.bus.publish(OrdersSnapshotTaken(open_orders=(), algo_orders=(), at_ms=rig.clock.now_ms()))  # 快照取得早了：没带上它
    await rig.settle()
    assert rig.exchange.writes == []  # 直接问交易所，止损其实还在：不补挂
    assert (await rig.row(rec.id)).stop_algo_id == old.algo_id


async def test_an_unreadable_exchange_means_no_rebuild(rig):
    """F8：未知不是不存在。读不到就不补挂、不改登记，等下一次再查。"""
    await rig.start()
    rec, old = await protected_position(rig)
    rig.exchange.mutate(old, raw_status="CANCELED")
    rig.exchange.set_read("fetch_open_algo_orders", Unknown("读不到"))
    observe(rig, rig.exchange.find(SYM, algo_id=old.algo_id))
    await rig.settle()
    assert rig.exchange.writes == []
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM
    rig.exchange.wire()
    await snapshot(rig)  # 下一次对账读到了：补挂
    assert len(rig.writes("create_stop_order")) == 1


# ---------------------------------------------------------------- 补挂本身走 F2 第 2–4 步


async def test_a_rebuild_whose_price_is_already_crossed_follows_f2_step_3(rig):
    await rig.start()
    rec, old = await protected_position(rig)
    rig.exchange.push_write_result("create_stop_order", rig.exchange.crossed())
    observe(rig, old, raw_status="CANCELED")
    await rig.settle()
    assert len(rig.writes("create_market_order")) == 1  # 策略仓：登记的止损价已被现价越过 -> 市价平掉
    assert rig.registry.cooldown_until(SYM) is not None


async def test_a_failed_rebuild_is_retried_across_the_window(rig):
    await rig.start()
    rec, old = await protected_position(rig)
    for _ in range(2):
        rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    observe(rig, old, raw_status="CANCELED")
    await rig.settle()
    assert (await rig.row(rec.id)).protection_state is ProtectionState.REBUILDING and not rig.suppressed()
    await rig.tick(10_000)
    await rig.tick(10_000)  # 第 2 次重试挂上了
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97")
    assert len(rig.writes("create_stop_order")) == 3


# ---------------------------------------------------------------- rebuild() 直接调用


async def test_rebuild_adopts_an_existing_system_stop_instead_of_placing_another(rig):
    await rig.start()
    rec = await rig.open_strategy()
    existing = rig.exchange.add_stop(SYM, LONG, "96")  # 登记簿不知道的系统止损（例如崩溃前挂上的）
    assert (await rig.protection.rebuild(rec.id, reason="recovery")) == "protected"
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("96")
    assert (row.stop_client_id, row.stop_algo_id) == (existing.client_algo_id, existing.algo_id)
    assert rig.exchange.writes == []


async def test_rebuild_records_surplus_system_stops_as_stale(rig):
    await rig.start()
    rec, registered = await protected_position(rig, start=False)
    extra = rig.exchange.add_stop(SYM, LONG, "95")
    assert (await rig.protection.rebuild(rec.id, reason="recovery")) == "protected"
    row = await rig.row(rec.id)
    assert row.stop_algo_id == registered.algo_id  # 登记的那张优先保留
    assert row.stale_stops == [{"client_algo_id": extra.client_algo_id, "algo_id": extra.algo_id}]


async def test_rebuild_places_the_registered_price_when_nothing_is_on_the_exchange(rig):
    await rig.start()
    rec, old = await protected_position(rig, start=False)
    rig.exchange.mutate(old, raw_status="CANCELED")
    assert (await rig.protection.rebuild(rec.id, reason="recovery")) == "protected"
    assert rig.writes("create_stop_order")[0]["trigger_price"] == D("97")


async def test_rebuild_computes_the_price_when_the_registry_has_none(rig):
    await rig.start()
    rig.candles.set(SYM, flat_history())
    rec = await rig.open_strategy()
    assert (await rig.protection.rebuild(rec.id, reason="recovery")) == "protected"
    assert rig.writes("create_stop_order")[0]["trigger_price"] == D("97")  # F9 第 5 步：登记里没有止损价的当刻计算


async def test_rebuild_does_nothing_when_the_exchange_cannot_be_read(rig):
    await rig.start()
    rec, old = await protected_position(rig, start=False)
    rig.exchange.set_read("fetch_open_algo_orders", Unknown("读不到"))
    assert (await rig.protection.rebuild(rec.id, reason="recovery")) == "unknown"
    assert rig.exchange.writes == []


async def test_rebuild_of_a_missing_or_unregistered_position(rig):
    from dmr_kernel.core.types import PendingReason

    await rig.start()
    assert (await rig.protection.rebuild(9999, reason="x")) == "closed"
    pending = await rig.registry.register_pending(symbol=SYM, side=LONG, qty=D(1), avg_price=D(100),
                                                  reason=PendingReason.OUT_OF_SCOPE)
    assert (await rig.protection.rebuild(pending.id, reason="x")) == "skipped"


# ---------------------------------------------------------------- stop_note：过渡说明补上后清掉，描述止损价来历的备注保留


async def test_the_transient_note_is_cleared_once_the_stop_is_back(rig):
    await rig.start()
    rec, old = await protected_position(rig)
    rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    observe(rig, old, raw_status="CANCELED")
    await rig.settle()
    assert (await rig.row(rec.id)).protection_state is ProtectionState.REBUILDING
    assert "补挂中" in (await rig.row(rec.id)).stop_note  # 重建期间说明原因
    await rig.tick(10_000)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_note == ""


async def test_a_note_describing_where_the_price_came_from_survives_a_rebuild(rig):
    await rig.start()
    rec, old = await protected_position(rig)
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, note="K 线不足以计算 ATR，初始止损按 A% 设置")
    observe(rig, old, raw_status="CANCELED")
    await rig.settle()
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and "A%" in row.stop_note


async def test_a_trailing_replacement_clears_the_note_about_the_initial_stop(rig):
    await rig.start()
    rec, old = await protected_position(rig, start=False)
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, note="K 线不足以计算 ATR，初始止损按 A% 设置")
    rig.candles.set(SYM, history_then(RISING))
    rig.exchange.set_price(SYM, "105")
    await rig.protection.trailing(rig.candles.frame(SYM, "15m")["open_time_ms"].iloc[-1].item())
    await rig.settle()
    row = await rig.row(rec.id)
    assert row.stop_price == D("97.5") and row.stop_note == ""
