"""持仓登记：登记、状态迁移、一仓一主、代际、持仓状态版本、事件与审计（计划 W2d / T0.12）。"""
from __future__ import annotations

import asyncio

import pytest

from dmr_kernel.core.events import PositionChanged, PositionClosed, PositionRegistered, ProtectionChanged
from dmr_kernel.core.registry import KeyOccupied, StaleVersion
from dmr_kernel.core.types import (
    ExitReason,
    ExitRuleset,
    Ownership,
    PendingReason,
    PositionStatus,
    ProtectionState,
)
from dmr_kernel.ledger.errors import IntegrityError
from dmr_kernel.ledger.records import PositionRecord
from registry_kit import (
    D,
    LONG,
    SHORT,
    SYM,
    T0,
    all_position_rows,
    audit_rows,
    raw_add_position,
    trade_count,
    trade_of,
)


# ================================================================ register_open


async def test_register_open_creates_managed_strategy_position(rig, clock):
    await rig.up()
    rec = await rig.registry.register_open(
        symbol=SYM, side=LONG, qty=D("2"), avg_price=D("100.5"), open_time_ms=T0 - 5_000,
        ownership=Ownership.STRATEGY, model="m1", intent_id=7, entry_reason="信号开多",
        list_snapshot_id=3, list_direction="LONG", list_score=D("0.85"), leverage=3, margin_mode="isolated")
    assert isinstance(rec, PositionRecord)
    assert (rec.symbol, rec.side) == (SYM, LONG)
    assert rec.status is PositionStatus.MANAGED
    assert rec.ownership is Ownership.STRATEGY
    assert rec.exit_ruleset is ExitRuleset.STRATEGY
    assert rec.protection_state is ProtectionState.NONE  # 保护「无保护」，等保护单管理去挂止损
    assert (rec.qty, rec.avg_price, rec.max_qty) == (D("2"), D("100.5"), D("2"))
    assert rec.open_time_ms == T0 - 5_000 and rec.anchor_ms == T0 - 5_000
    assert rec.open_time_estimated is False and rec.is_mixed is False and rec.signal_exit is False
    assert rec.generation == 1 and rec.version == 1 and rec.stop_cycle == 0
    assert rec.opened_by_intent_id == 7 and rec.entry_reason == "信号开多"
    assert (rec.list_snapshot_id, rec.list_direction, rec.list_score) == (3, "LONG", D("0.85"))
    assert (rec.leverage, rec.margin_mode) == (3, "isolated")
    assert rec.stop_price is None and rec.stop_client_id is None and rec.stop_algo_id is None
    assert rec.stale_stops == [] and rec.handed_over_stop is None and rec.pending_flags == []
    assert rec.pending_reason is None and rec.exit_reason is None and rec.closed_ms is None
    assert rec.created_ms == rec.updated_ms == clock.now_ms()

    # 缓存里查得到，且与账本一致
    assert rig.registry.get_active(SYM, LONG) == rec
    assert rig.registry.get_active(SYM, SHORT) is None
    assert rig.registry.by_id(rec.id) == rec
    assert rig.registry.all_active() == [rec]
    assert rig.registry.registered() == [rec]
    assert await rig.registry.fetch(rec.id) == rec


async def test_register_open_ai_ownership_also_uses_strategy_ruleset(rig):
    await rig.up()
    rec = await rig.open_strategy(ownership=Ownership.AI, model="m3")
    assert rec.ownership is Ownership.AI
    assert rec.exit_ruleset is ExitRuleset.STRATEGY
    assert rec.status is PositionStatus.MANAGED


@pytest.mark.parametrize("ownership", [Ownership.TAKEOVER, Ownership.NONE])
async def test_register_open_rejects_ownership_that_is_not_strategy_or_ai(rig, ownership):
    await rig.up()
    with pytest.raises(ValueError):
        await rig.open_strategy(ownership=ownership)
    assert await all_position_rows(rig.ledger) == []
    assert rig.registry.all_active() == []


@pytest.mark.parametrize("qty, price", [(0, 100), (-1, 100), (1, 0), (1, -5)])
async def test_register_open_rejects_non_positive_numbers(rig, qty, price):
    await rig.up()
    with pytest.raises(ValueError):
        await rig.open_strategy(qty=qty, price=price)
    assert await all_position_rows(rig.ledger) == []


async def test_register_open_publishes_registered_event_after_cache_is_updated(rig, clock):
    await rig.up()
    seen_in_handler = []
    rig.bus.subscribe(PositionRegistered, lambda e: seen_in_handler.append(rig.registry.by_id(e.position_id)),
                      name="reader")
    rec = await rig.open_strategy()
    events = await rig.events()
    assert events == [PositionRegistered(position_id=rec.id, symbol=SYM, side=LONG, ownership="strategy",
                                         status="managed", at_ms=clock.now_ms())]
    assert seen_in_handler == [rec]  # 订阅者收到事件时，登记簿里已经查得到


async def test_register_open_writes_audit_row(rig):
    await rig.up()
    rec = await rig.open_strategy(intent_id=9, model="m1")
    rows = [r for r in await audit_rows(rig.ledger) if r.action == "position.register"]
    assert len(rows) == 1
    row = rows[0]
    assert row.operator == "system" and row.target == f"position:{rec.id}" and row.result == "ok"
    assert row.after["status"] == "managed" and row.after["ownership"] == "strategy"
    assert row.after["model"] == "m1" and row.after["intent_id"] == 9


@pytest.mark.parametrize("qty, price", [(0, 100), (-1, 100), (1, 0), (1, -5)])
async def test_pending_and_takeover_registration_reject_non_positive_numbers(rig, qty, price):
    await rig.up()
    with pytest.raises(ValueError):
        await rig.pending(qty=qty, price=price)
    with pytest.raises(ValueError):
        await rig.takeover(qty=qty, price=price)
    assert await all_position_rows(rig.ledger) == []


# ================================================================ 一仓一主


async def test_partial_unique_index_allows_one_active_row_per_key(ledger):
    first = await raw_add_position(ledger, symbol=SYM, side=LONG)
    assert first > 0
    with pytest.raises(IntegrityError):  # 同一键再来一条非 closed 的（任何状态）
        await raw_add_position(ledger, symbol=SYM, side=LONG, status="pending")
    # 已平的不占位；另一侧、另一合约互不影响
    await raw_add_position(ledger, symbol=SYM, side=LONG, status="closed", generation=0)
    await raw_add_position(ledger, symbol=SYM, side=LONG, status="closed", generation=-1)
    await raw_add_position(ledger, symbol=SYM, side=SHORT)
    await raw_add_position(ledger, symbol="ETHUSDT", side=LONG)


async def test_register_open_on_occupied_key_is_rejected_and_changes_nothing(rig):
    await rig.up()
    first = await rig.open_strategy(intent_id=1)
    with pytest.raises(KeyOccupied):
        await rig.open_strategy(intent_id=2)
    with pytest.raises(KeyOccupied):
        await rig.pending()
    with pytest.raises(KeyOccupied):
        await rig.takeover()
    assert rig.registry.get_active(SYM, LONG) == first
    assert [r.id for r in await all_position_rows(rig.ledger)] == [first.id]
    assert (await rig.registry.fetch(first.id)).version == 1


async def test_key_occupied_names_the_key_and_the_occupant(rig):
    await rig.up()
    first = await rig.open_strategy()
    with pytest.raises(KeyOccupied) as caught:
        await rig.open_strategy(intent_id=2)
    assert (caught.value.symbol, caught.value.side) == (SYM, LONG)
    assert SYM in str(caught.value) and str(first.id) in str(caught.value)


async def test_concurrent_register_open_on_same_key_only_one_succeeds(rig):
    await rig.up()
    results = await asyncio.gather(*(rig.open_strategy(intent_id=i) for i in range(1, 7)), return_exceptions=True)
    won = [r for r in results if isinstance(r, PositionRecord)]
    lost = [r for r in results if isinstance(r, KeyOccupied)]
    assert len(won) == 1 and len(lost) == 5
    assert [r.id for r in await all_position_rows(rig.ledger)] == [won[0].id]
    assert rig.registry.all_active() == won


async def test_database_decides_races_between_two_registries(rig):
    other = rig.another()
    await rig.up()
    await other.up()
    results = await asyncio.gather(rig.open_strategy(intent_id=1), other.open_strategy(intent_id=2),
                                   return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["KeyOccupied", "PositionRecord"]
    assert len(await all_position_rows(rig.ledger)) == 1


async def test_both_sides_and_other_symbols_can_be_held_at_once(rig):
    await rig.up()
    a = await rig.open_strategy(side=LONG)
    b = await rig.open_strategy(side=SHORT)
    c = await rig.open_strategy(symbol="ETHUSDT", side=LONG)
    assert {r.id for r in rig.registry.all_active()} == {a.id, b.id, c.id}


# ================================================================ 代际


async def test_generation_counts_openings_from_zero_per_key(rig):
    await rig.up()
    first = await rig.open_strategy(intent_id=1)
    other_side = await rig.open_strategy(side=SHORT, intent_id=2)
    other_symbol = await rig.open_strategy(symbol="ETHUSDT", intent_id=3)
    assert (first.generation, other_side.generation, other_symbol.generation) == (1, 1, 1)

    await rig.registry.mark_closed(first.id, exit_reason=ExitReason.MANUAL, closed_ms=T0 + 1_000)
    second = await rig.open_strategy(intent_id=4)
    assert second.generation == 2 and second.id != first.id
    await rig.registry.mark_closed(second.id, exit_reason=ExitReason.SIGNAL, closed_ms=T0 + 2_000)
    third = await rig.pending()
    assert third.generation == 3
    assert rig.registry.get_active(SYM, SHORT).generation == 1  # 另一侧不受影响


# ================================================================ register_pending


async def test_register_pending_creates_unowned_pending_record(rig, clock):
    await rig.up()
    rec = await rig.registry.register_pending(symbol=SYM, side=SHORT, qty=D("0.5"), avg_price=D("200"),
                                              reason=PendingReason.PREEXISTING, flags=("legacy_stop", "x"),
                                              note="启动时已有")
    assert rec.status is PositionStatus.PENDING
    assert rec.ownership is Ownership.NONE and rec.exit_ruleset is None and rec.is_mixed is False
    assert rec.pending_reason is PendingReason.PREEXISTING
    assert rec.pending_flags == ["legacy_stop", "x"] and rec.origin_note == "启动时已有"
    assert (rec.qty, rec.avg_price, rec.max_qty) == (D("0.5"), D("200"), D("0.5"))
    assert rec.open_time_ms is None and rec.anchor_ms is None
    assert rec.protection_state is ProtectionState.NONE
    assert rec.generation == 1 and rec.version == 1 and rec.opened_by_intent_id is None
    assert rec.created_ms == clock.now_ms()
    assert rig.registry.get_active(SYM, SHORT) == rec
    assert rig.registry.registered() == []  # 待确认不是登记仓
    events = await rig.events()
    assert events == [PositionRegistered(position_id=rec.id, symbol=SYM, side=SHORT, ownership="none",
                                         status="pending", at_ms=clock.now_ms())]


async def test_register_pending_defaults_and_validation(rig):
    await rig.up()
    rec = await rig.pending()
    assert rec.pending_flags == [] and rec.origin_note == ""
    with pytest.raises(ValueError):
        await rig.pending(symbol="ETHUSDT", qty=0)


# ================================================================ begin_takeover


async def test_begin_takeover_turns_pending_into_taking_over(rig, clock):
    await rig.up()
    pend = await rig.pending(qty=2, price=100, flags=["x"], note="n")
    rig.clear_events()
    clock.advance(1_000)
    took = await rig.registry.begin_takeover(
        symbol=SYM, side=LONG, qty=D("3"), avg_price=D("101"), open_time_ms=T0 - 1_000, open_time_estimated=True,
        anchor_ms=T0 - 500, signal_exit=True, position_id=pend.id, expected_version=pend.version)
    assert took.id == pend.id and took.generation == pend.generation and took.version == pend.version + 1
    assert took.status is PositionStatus.TAKING_OVER
    assert took.ownership is Ownership.TAKEOVER and took.exit_ruleset is ExitRuleset.TAKEOVER
    assert (took.qty, took.avg_price, took.max_qty) == (D("3"), D("101"), D("3"))
    assert took.open_time_ms == T0 - 1_000 and took.open_time_estimated is True and took.anchor_ms == T0 - 500
    assert took.signal_exit is True
    assert took.pending_reason is None and took.pending_flags == []
    assert took.updated_ms == clock.now_ms() and took.created_ms == pend.created_ms
    assert rig.registry.by_id(pend.id) == took
    assert rig.registry.registered() == [took]
    assert await rig.events() == [PositionChanged(position_id=took.id, symbol=SYM, side=LONG, cause="takeover",
                                                  version=took.version, at_ms=clock.now_ms())]


async def test_begin_takeover_keeps_peak_quantity_of_the_pending_record(rig):
    await rig.up()
    pend = await rig.pending(qty=5)
    took = await rig.registry.begin_takeover(
        symbol=SYM, side=LONG, qty=D("3"), avg_price=D("100"), open_time_ms=T0, open_time_estimated=False,
        anchor_ms=T0, signal_exit=False, position_id=pend.id)
    assert took.qty == D("3") and took.max_qty == D("5")


async def test_begin_takeover_without_position_id_creates_taking_over_record(rig, clock):
    await rig.up()
    rec = await rig.takeover(qty=2, price=50, open_time_estimated=True, signal_exit=True)
    assert rec.status is PositionStatus.TAKING_OVER
    assert rec.ownership is Ownership.TAKEOVER and rec.exit_ruleset is ExitRuleset.TAKEOVER
    assert rec.generation == 1 and rec.version == 1 and rec.max_qty == D("2")
    assert rec.open_time_estimated is True and rec.signal_exit is True and rec.anchor_ms == T0
    assert rec.protection_state is ProtectionState.NONE
    assert await rig.events() == [PositionRegistered(position_id=rec.id, symbol=SYM, side=LONG,
                                                     ownership="takeover", status="taking_over",
                                                     at_ms=clock.now_ms())]


async def test_begin_takeover_validates_target_record(rig):
    await rig.up()
    managed = await rig.open_strategy()
    pend_short = await rig.pending(side=SHORT)
    kwargs = dict(qty=D("1"), avg_price=D("100"), open_time_ms=T0, open_time_estimated=False, anchor_ms=T0,
                  signal_exit=False)
    with pytest.raises(ValueError):  # 只有待确认仓能转接管
        await rig.registry.begin_takeover(symbol=SYM, side=LONG, position_id=managed.id, **kwargs)
    with pytest.raises(ValueError):  # 合约与方向对不上
        await rig.registry.begin_takeover(symbol=SYM, side=LONG, position_id=pend_short.id, **kwargs)
    with pytest.raises(LookupError):
        await rig.registry.begin_takeover(symbol=SYM, side=SHORT, position_id=9_999, **kwargs)
    with pytest.raises(KeyOccupied):  # 没给 position_id 就是新建：键被占了就拒绝
        await rig.registry.begin_takeover(symbol=SYM, side=SHORT, **kwargs)
    assert rig.registry.by_id(managed.id) == managed
    assert rig.registry.by_id(pend_short.id) == pend_short


async def test_begin_takeover_rejects_stale_version(rig):
    await rig.up()
    pend = await rig.pending()
    await rig.registry.set_pending(pend.id, reason=PendingReason.UNKNOWN_ORIGIN)  # 版本 +1，界面手里的是旧版本
    kwargs = dict(symbol=SYM, side=LONG, qty=D("1"), avg_price=D("100"), open_time_ms=T0, open_time_estimated=False,
                  anchor_ms=T0, signal_exit=False, position_id=pend.id)
    with pytest.raises(StaleVersion):
        await rig.registry.begin_takeover(expected_version=pend.version, **kwargs)
    assert rig.registry.by_id(pend.id).status is PositionStatus.PENDING
    ok = await rig.registry.begin_takeover(expected_version=pend.version + 1, **kwargs)
    assert ok.status is PositionStatus.TAKING_OVER


# ================================================================ mark_managed / set_closing


async def test_mark_managed_moves_taking_over_to_managed(rig, clock):
    await rig.up()
    rec = await rig.takeover()
    rig.clear_events()
    managed = await rig.registry.mark_managed(rec.id)
    assert managed.status is PositionStatus.MANAGED and managed.version == rec.version + 1
    assert managed.ownership is Ownership.TAKEOVER
    assert rig.registry.by_id(rec.id) == managed
    assert await rig.events() == [PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="managed",
                                                  version=managed.version, at_ms=clock.now_ms())]
    again = await rig.registry.mark_managed(rec.id)  # 重复调用无害：不改版本、不发事件
    assert again == managed
    rig.clear_events()
    assert await rig.events() == []


@pytest.mark.parametrize("kind", ["pending", "ignored"])
async def test_mark_managed_rejects_other_states(rig, kind):
    await rig.up()
    rec = await rig.pending()
    if kind == "ignored":
        rec = await rig.registry.ignore(rec.id, expected_version=None)
    with pytest.raises(ValueError):
        await rig.registry.mark_managed(rec.id)
    assert rig.registry.by_id(rec.id) == rec


async def test_mark_managed_rejects_closing_positions(rig):
    await rig.up()
    rec = await rig.managed_takeover()
    closing = await rig.registry.set_closing(rec.id, True, cause="close_one")
    with pytest.raises(ValueError):  # 平仓中不能被「标为托管中」悄悄改回去
        await rig.registry.mark_managed(rec.id)
    assert rig.registry.by_id(rec.id) == closing


async def test_set_closing_toggles_between_managed_and_closing(rig, clock):
    await rig.up()
    rec = await rig.open_strategy()
    rig.clear_events()
    closing = await rig.registry.set_closing(rec.id, True, cause="partial_close_sent")
    assert closing.status is PositionStatus.CLOSING and closing.version == rec.version + 1
    assert rig.registry.registered() == [closing]  # 平仓中仍是登记仓
    back = await rig.registry.set_closing(rec.id, False, cause="partial_close_done")
    assert back.status is PositionStatus.MANAGED and back.version == rec.version + 2
    assert await rig.events() == [
        PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="partial_close_sent",
                        version=rec.version + 1, at_ms=clock.now_ms()),
        PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="partial_close_done",
                        version=rec.version + 2, at_ms=clock.now_ms()),
    ]


async def test_set_closing_is_idempotent_and_state_checked(rig):
    await rig.up()
    rec = await rig.open_strategy()
    assert await rig.registry.set_closing(rec.id, False, cause="x") == rec  # 本来就不在平仓中
    closing = await rig.registry.set_closing(rec.id, True, cause="x")
    assert await rig.registry.set_closing(rec.id, True, cause="y") == closing  # 重复置位：不改版本
    taking = await rig.takeover(side=SHORT)
    pending = await rig.pending(symbol="ETHUSDT")
    for bad in (taking, pending):
        with pytest.raises(ValueError):  # 只有托管中 ↔ 平仓中
            await rig.registry.set_closing(bad.id, True, cause="x")
        assert rig.registry.by_id(bad.id) == bad


# ================================================================ set_protection 等


async def test_set_protection_sets_state_and_stop_fields(rig, clock):
    await rig.up()
    rec = await rig.open_strategy()
    rig.clear_events()
    clock.advance(10)
    prot = await rig.registry.set_protection(
        rec.id, state=ProtectionState.SYSTEM, stop_price=D("95.5"), stop_client_id="DKa1-1-1-s", stop_algo_id="777",
        stop_cycle=2, note="初始止损")
    assert prot.protection_state is ProtectionState.SYSTEM
    assert (prot.stop_price, prot.stop_client_id, prot.stop_algo_id) == (D("95.5"), "DKa1-1-1-s", "777")
    assert prot.stop_cycle == 2 and prot.stop_note == "初始止损"
    assert prot.version == rec.version + 1 and prot.updated_ms == clock.now_ms()
    assert rig.registry.by_id(rec.id) == prot
    assert await rig.events() == [
        PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="protection", version=prot.version,
                        at_ms=clock.now_ms()),
        ProtectionChanged(position_id=rec.id, state="system", stop_price=D("95.5"), at_ms=clock.now_ms()),
    ]


async def test_set_protection_only_touches_given_fields(rig):
    await rig.up()
    rec = await rig.open_strategy()
    first = await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("95"),
                                              stop_client_id="c1", stop_algo_id="a1", stop_cycle=1, note="n1")
    rebuilding = await rig.registry.set_protection(rec.id, state=ProtectionState.REBUILDING)
    assert rebuilding.protection_state is ProtectionState.REBUILDING
    assert (rebuilding.stop_price, rebuilding.stop_client_id, rebuilding.stop_algo_id) == (D("95"), "c1", "a1")
    assert rebuilding.stop_cycle == 1 and rebuilding.stop_note == "n1"
    moved = await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("96"),
                                              stop_cycle=2, note="")
    assert moved.stop_price == D("96") and moved.stop_cycle == 2 and moved.stop_note == ""
    assert moved.stop_client_id == "c1" and moved.stop_algo_id == "a1"
    assert moved.version == first.version + 2


async def test_set_protection_clear_stop_forgets_the_stop(rig):
    await rig.up()
    rec = await rig.open_strategy()
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("95"),
                                      stop_client_id="c1", stop_algo_id="a1")
    cleared = await rig.registry.set_protection(rec.id, state=ProtectionState.NONE, clear_stop=True)
    assert cleared.protection_state is ProtectionState.NONE
    assert (cleared.stop_price, cleared.stop_client_id, cleared.stop_algo_id) == (None, None, None)
    with pytest.raises(ValueError):  # 清除与设置同时给：含义不明，拒绝
        await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("90"), clear_stop=True)
    assert rig.registry.by_id(rec.id) == cleared


async def test_set_protection_without_change_keeps_version_and_publishes_nothing(rig):
    await rig.up()
    rec = await rig.open_strategy()
    prot = await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("95"))
    rig.clear_events()
    same = await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("95.00"))
    assert same == prot and same.version == prot.version
    assert await rig.events() == []


async def test_protection_event_only_when_state_or_price_changes(rig):
    await rig.up()
    rec = await rig.open_strategy()
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("95"), stop_client_id="c1")
    rig.clear_events()
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_algo_id="a1", note="已受理")
    events = await rig.events()
    assert [type(e) for e in events] == [PositionChanged]  # 只有编号与说明变化，保护状态与止损价没变


async def test_stale_stops_add_remove_and_deduplicate(rig, clock):
    await rig.up()
    rec = await rig.open_strategy()
    rig.clear_events()
    one = await rig.registry.add_stale_stop(rec.id, client_algo_id="c1", algo_id="a1")
    assert one.stale_stops == [{"client_algo_id": "c1", "algo_id": "a1"}] and one.version == rec.version + 1
    dup = await rig.registry.add_stale_stop(rec.id, client_algo_id="c1", algo_id="a1")
    assert dup == one  # 同一张不重复登记，也不改版本
    two = await rig.registry.add_stale_stop(rec.id, client_algo_id="c2", algo_id="a2")
    assert [s["client_algo_id"] for s in two.stale_stops] == ["c1", "c2"]
    left = await rig.registry.remove_stale_stop(rec.id, client_algo_id="c1", algo_id="a1")
    assert left.stale_stops == [{"client_algo_id": "c2", "algo_id": "a2"}]
    assert await rig.registry.remove_stale_stop(rec.id, client_algo_id="nope", algo_id="x") == left  # 不存在：无变化
    events = await rig.events(PositionChanged)
    assert [e.cause for e in events] == ["stale_stops"] * 3
    assert rig.registry.by_id(rec.id) == left


async def test_remove_stale_stop_can_match_by_either_identifier(rig):
    await rig.up()
    rec = await rig.open_strategy()
    await rig.registry.add_stale_stop(rec.id, client_algo_id="c1", algo_id="a1")
    await rig.registry.add_stale_stop(rec.id, client_algo_id="c2", algo_id=None)
    only_algo = await rig.registry.remove_stale_stop(rec.id, algo_id="a1")
    assert only_algo.stale_stops == [{"client_algo_id": "c2", "algo_id": None}]
    only_client = await rig.registry.remove_stale_stop(rec.id, client_algo_id="c2")
    assert only_client.stale_stops == []
    with pytest.raises(ValueError):
        await rig.registry.remove_stale_stop(rec.id)


async def test_set_handed_over_stores_and_clears_the_handed_over_stop(rig):
    await rig.up()
    rec = await rig.managed_takeover()
    stop = {"client_algo_id": "user1", "algo_id": "9", "trigger_price": "90"}
    handed = await rig.registry.set_handed_over(rec.id, stop)
    assert handed.handed_over_stop == stop and handed.version == rec.version + 1
    assert await rig.registry.set_handed_over(rec.id, dict(stop)) == handed  # 同样的值：无变化
    cleared = await rig.registry.set_handed_over(rec.id, None)
    assert cleared.handed_over_stop is None and cleared.version == handed.version + 1
    events = await rig.events(PositionChanged)
    assert [e.cause for e in events if e.cause == "handed_over"] == ["handed_over", "handed_over"]


# ================================================================ update_exchange_view


async def test_update_exchange_view_records_observation_without_touching_version(rig, clock):
    await rig.up()
    rec = await rig.open_strategy(qty=2)
    rig.clear_events()
    clock.advance(5_000)
    seen = await rig.registry.update_exchange_view(rec.id, exchange_qty=D("2"), liquidation_price=D("50"),
                                                   seen_ms=clock.now_ms())
    assert (seen.exchange_qty, seen.liquidation_price, seen.exchange_seen_ms) == (D("2"), D("50"), clock.now_ms())
    assert seen.consistent is True
    assert seen.version == rec.version  # 对账观测值：不算登记簿状态变化，不动版本（否则界面的版本校验永远过期）
    assert seen.qty == rec.qty and seen.status == rec.status
    assert rig.registry.by_id(rec.id) == seen

    clock.advance(5_000)
    off = await rig.registry.update_exchange_view(rec.id, exchange_qty=D("1.5"), liquidation_price=None,
                                                  seen_ms=clock.now_ms())
    assert off.consistent is False and off.liquidation_price is None and off.version == rec.version
    assert await rig.events() == []


# ================================================================ ignore / set_pending


async def test_ignore_turns_pending_into_external_ignored(rig, clock):
    await rig.up()
    rec = await rig.pending(qty=2)
    rig.clear_events()
    ignored = await rig.registry.ignore(rec.id, expected_version=rec.version)
    assert ignored.status is PositionStatus.IGNORED
    assert ignored.ownership is Ownership.NONE and ignored.exit_ruleset is None and ignored.signal_exit is False
    assert ignored.pending_reason == rec.pending_reason  # 保留来源，方便恢复
    assert ignored.version == rec.version + 1 and ignored.qty == rec.qty
    assert rig.registry.registered() == []
    assert rig.registry.by_id(rec.id) == ignored
    assert await rig.events() == [PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="ignored",
                                                  version=ignored.version, at_ms=clock.now_ms())]
    rows = [r for r in await audit_rows(rig.ledger) if r.action == "position.ignore"]
    assert len(rows) == 1 and rows[0].before["status"] == "pending" and rows[0].after["status"] == "ignored"


async def test_ignore_cancels_takeover_but_keeps_stop_references(rig):
    await rig.up()
    rec = await rig.managed_takeover()
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("90"),
                                      stop_client_id="c1", stop_algo_id="a1")
    await rig.registry.add_stale_stop(rec.id, client_algo_id="old", algo_id="o1")
    current = rig.registry.by_id(rec.id)
    ignored = await rig.registry.ignore(rec.id, expected_version=current.version)
    assert ignored.status is PositionStatus.IGNORED and ignored.ownership is Ownership.NONE
    # 取消托管后要撤的止损还得认得出来：编号留着，由保护单管理去清
    assert (ignored.stop_client_id, ignored.stop_algo_id) == ("c1", "a1")
    assert ignored.stale_stops == [{"client_algo_id": "old", "algo_id": "o1"}]


async def test_ignore_rejects_stale_version_and_closed_records(rig):
    await rig.up()
    rec = await rig.pending()
    await rig.registry.set_pending(rec.id, reason=PendingReason.LEGACY)
    with pytest.raises(StaleVersion):
        await rig.registry.ignore(rec.id, expected_version=rec.version)
    assert rig.registry.by_id(rec.id).status is PositionStatus.PENDING
    closed = await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.EXTERNAL, closed_ms=T0 + 1)
    with pytest.raises(ValueError):
        await rig.registry.ignore(closed.id, expected_version=None)


@pytest.mark.parametrize("ownership, model", [(Ownership.STRATEGY, "m1"), (Ownership.AI, "m3")])
async def test_ignore_refuses_positions_the_system_opened(rig, ownership, model):
    """系统自己开的仓不是外部仓：忽略它会让它的止损与意图失去归属。"""
    await rig.up()
    rec = await rig.open_strategy(ownership=ownership, model=model)
    with pytest.raises(ValueError):
        await rig.registry.ignore(rec.id, expected_version=None)
    assert rig.registry.by_id(rec.id) == rec and await rig.registry.fetch(rec.id) == rec


async def test_ignore_twice_is_a_no_op(rig):
    await rig.up()
    rec = await rig.pending()
    ignored = await rig.registry.ignore(rec.id, expected_version=None)
    rig.clear_events()
    assert await rig.registry.ignore(rec.id, expected_version=ignored.version) == ignored
    assert await rig.events() == []


async def test_set_pending_reclassifies_pending_and_restores_ignored(rig, clock):
    await rig.up()
    rec = await rig.pending(reason=PendingReason.OUT_OF_SCOPE, flags=["a"], note="第一次")
    rig.clear_events()
    again = await rig.registry.set_pending(rec.id, reason=PendingReason.NOTIONAL_OUT_OF_RANGE, flags=["b", "c"],
                                           note="名义金额太小")
    assert again.status is PositionStatus.PENDING and again.version == rec.version + 1
    assert again.pending_reason is PendingReason.NOTIONAL_OUT_OF_RANGE
    assert again.pending_flags == ["b", "c"] and again.origin_note == "名义金额太小"
    ignored = await rig.registry.ignore(rec.id, expected_version=None)
    restored = await rig.registry.set_pending(rec.id, reason=PendingReason.MODEL_NOT_ACTIVE)
    assert restored.status is PositionStatus.PENDING and restored.ownership is Ownership.NONE
    assert restored.version == ignored.version + 1
    events = await rig.events(PositionChanged)
    assert [e.cause for e in events] == ["pending", "ignored", "pending"]


async def test_set_pending_without_change_and_on_wrong_state(rig):
    await rig.up()
    rec = await rig.pending(reason=PendingReason.LEGACY, flags=["a"], note="n")
    same = await rig.registry.set_pending(rec.id, reason=PendingReason.LEGACY, flags=["a"], note="n")
    assert same == rec
    managed = await rig.open_strategy(side=SHORT)
    with pytest.raises(ValueError):  # 已登记的仓不能退回待确认
        await rig.registry.set_pending(managed.id, reason=PendingReason.LEGACY)


async def test_takeover_and_restore_are_written_to_the_audit_log(rig):
    await rig.up()
    pend = await rig.pending(qty=2)
    took = await rig.registry.begin_takeover(
        symbol=SYM, side=LONG, qty=D(2), avg_price=D(100), open_time_ms=T0, open_time_estimated=False, anchor_ms=T0,
        signal_exit=True, position_id=pend.id)
    rows = [r for r in await audit_rows(rig.ledger) if r.action == "position.takeover"]
    assert len(rows) == 1 and rows[0].target == f"position:{took.id}" and rows[0].operator == "system"
    assert rows[0].before["status"] == "pending" and rows[0].after["status"] == "taking_over"
    assert rows[0].after["ownership"] == "takeover" and rows[0].after["signal_exit"] is True

    other = await rig.pending(symbol="ETHUSDT")
    await rig.registry.ignore(other.id, expected_version=None)
    await rig.registry.set_pending(other.id, reason=PendingReason.MODEL_NOT_ACTIVE)
    restores = [r for r in await audit_rows(rig.ledger) if r.action == "position.restore"]
    assert len(restores) == 1 and restores[0].before["status"] == "ignored" and restores[0].after["status"] == "pending"


# ================================================================ mark_closed

async def test_mark_closed_closes_record_writes_trade_and_publishes(rig, clock):
    await rig.up()
    rec = await rig.open_strategy(qty=2, price=100)
    prot = await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("95"),
                                             stop_client_id="DKa1-1-1-s", stop_algo_id="55")
    await rig.registry.add_stale_stop(rec.id, client_algo_id="old", algo_id="9")
    rig.clear_events()
    clock.advance(3_000)
    closed = await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=T0 + 2_500)
    assert closed.status is PositionStatus.CLOSED and closed.exit_reason is ExitReason.MANUAL
    assert closed.closed_ms == T0 + 2_500 and closed.qty == D("0")
    assert closed.version == prot.version + 2 and closed.updated_ms == clock.now_ms()
    # 清理保护单要用到的编号在已平记录里都还在
    assert (closed.stop_client_id, closed.stop_algo_id) == ("DKa1-1-1-s", "55")
    assert closed.stale_stops == [{"client_algo_id": "old", "algo_id": "9"}]
    # 缓存里已没有；账本里查得到
    assert rig.registry.by_id(rec.id) is None and rig.registry.get_active(SYM, LONG) is None
    assert rig.registry.all_active() == [] and rig.registry.registered() == []
    assert await rig.registry.fetch(rec.id) == closed
    # 写了 trade；发布 PositionClosed（不另发 PositionChanged）
    trade = await trade_of(rig.ledger, rec.id)
    assert trade is not None and trade.position_id == rec.id and trade.exit_reason is ExitReason.MANUAL
    assert trade.close_ms == T0 + 2_500
    assert await rig.events() == [PositionClosed(position_id=rec.id, symbol=SYM, side=LONG, exit_reason="manual",
                                                 at_ms=clock.now_ms())]
    rows = [r for r in await audit_rows(rig.ledger) if r.action == "position.close"]
    assert len(rows) == 1 and rows[0].after["exit_reason"] == "manual"


async def test_mark_closed_twice_changes_nothing_the_second_time(rig):
    await rig.up()
    rec = await rig.open_strategy()
    first = await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.SIGNAL, closed_ms=T0 + 10)
    rig.clear_events()
    second = await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.EXTERNAL, closed_ms=T0 + 99)
    assert second == first and second.exit_reason is ExitReason.SIGNAL
    assert await trade_count(rig.ledger) == 1
    assert await rig.events() == []


@pytest.mark.parametrize("kind", ["pending", "ignored"])
async def test_mark_closed_also_closes_pending_and_ignored_records(rig, kind):
    await rig.up()
    rec = await rig.pending()
    if kind == "ignored":
        rec = await rig.registry.ignore(rec.id, expected_version=None)
    closed = await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.EXTERNAL, closed_ms=T0 + 10)
    assert closed.status is PositionStatus.CLOSED
    trade = await trade_of(rig.ledger, rec.id)
    assert trade.ownership is Ownership.NONE and trade.model is None


async def test_mutators_reject_unknown_position_ids(rig):
    await rig.up()
    calls = [
        lambda i: rig.registry.mark_managed(i),
        lambda i: rig.registry.set_closing(i, True, cause="x"),
        lambda i: rig.registry.set_protection(i, state=ProtectionState.NONE),
        lambda i: rig.registry.add_stale_stop(i, client_algo_id="c", algo_id="a"),
        lambda i: rig.registry.remove_stale_stop(i, client_algo_id="c"),
        lambda i: rig.registry.set_handed_over(i, None),
        lambda i: rig.registry.update_exchange_view(i, exchange_qty=D(0), liquidation_price=None, seen_ms=T0),
        lambda i: rig.registry.set_pending(i, reason=PendingReason.LEGACY),
        lambda i: rig.registry.ignore(i, expected_version=None),
        lambda i: rig.registry.mark_closed(i, exit_reason=ExitReason.MANUAL, closed_ms=T0),
    ]
    for call in calls:
        with pytest.raises(LookupError):
            await call(9_999)
    assert await rig.registry.fetch(9_999) is None
    assert rig.registry.by_id(9_999) is None


async def test_state_changes_reject_closed_positions(rig):
    await rig.up()
    rec = await rig.open_strategy()
    closed = await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=T0 + 1)
    calls = [
        lambda i: rig.registry.mark_managed(i),
        lambda i: rig.registry.set_closing(i, True, cause="x"),
        lambda i: rig.registry.update_exchange_view(i, exchange_qty=D(0), liquidation_price=None, seen_ms=T0),
        lambda i: rig.registry.set_pending(i, reason=PendingReason.LEGACY),
        lambda i: rig.registry.ignore(i, expected_version=None),
        lambda i: rig.registry.begin_takeover(
            symbol=SYM, side=LONG, qty=D(1), avg_price=D(100), open_time_ms=T0, open_time_estimated=False,
            anchor_ms=T0, signal_exit=False, position_id=i),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            await call(closed.id)
    assert await rig.registry.fetch(closed.id) == closed


async def test_stop_bookkeeping_still_works_after_close_so_cleanup_can_record_progress(rig):
    """已平记录要保留止损编号，供「归零后清理」去撤；清理做完后要能把这些编号记为已处理。"""
    await rig.up()
    rec = await rig.open_strategy()
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("95"),
                                      stop_client_id="c1", stop_algo_id="a1")
    await rig.registry.add_stale_stop(rec.id, client_algo_id="old", algo_id="o1")
    await rig.registry.set_handed_over(rec.id, {"client_algo_id": "user", "algo_id": "u1"})
    closed = await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=T0 + 1)
    rig.clear_events()

    a = await rig.registry.remove_stale_stop(rec.id, client_algo_id="old")
    b = await rig.registry.set_handed_over(rec.id, None)
    c = await rig.registry.set_protection(rec.id, state=ProtectionState.NONE, clear_stop=True)
    assert (a.version, b.version, c.version) == (closed.version + 1, closed.version + 2, closed.version + 3)
    assert c.status is PositionStatus.CLOSED and c.stale_stops == [] and c.handed_over_stop is None
    assert (c.stop_price, c.stop_client_id, c.stop_algo_id) == (None, None, None)
    assert c.protection_state is ProtectionState.NONE
    assert await rig.registry.fetch(rec.id) == c
    # 已平的记录不会回到缓存里
    assert rig.registry.by_id(rec.id) is None and rig.registry.all_active() == []


# ================================================================ 版本


async def test_position_version_goes_up_by_one_on_every_change(rig):
    await rig.up()
    rec = await rig.open_strategy()
    versions = [rec.version]
    steps = [
        lambda: rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("95")),
        lambda: rig.registry.add_stale_stop(rec.id, client_algo_id="c", algo_id="a"),
        lambda: rig.registry.set_handed_over(rec.id, {"x": 1}),
        lambda: rig.registry.set_closing(rec.id, True, cause="c"),
        lambda: rig.registry.set_closing(rec.id, False, cause="c"),
        lambda: rig.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=T0 + 1),
    ]
    for step in steps:
        versions.append((await step()).version)
    assert versions == [1, 2, 3, 4, 5, 6, 7]
    assert (await rig.registry.fetch(rec.id)).version == 7
