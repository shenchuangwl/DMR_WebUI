"""持仓登记：键占用判定（F1 第 3 步）、状态计数、冷却、重启 load 后缓存一致。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.registry import KeyOccupancy
from dmr_kernel.core.types import ExitReason, Ownership, PositionSide, ProtectionState
from dmr_kernel.ledger import repo_registry
from registry_kit import D, EXTERNAL_ORIGIN, LONG, SHORT, SYM, T0, all_position_rows

REVERSAL = "对侧有策略仓：属于反手，由 F11 处理"


async def _hold(rig, kind: str | None, side: PositionSide, symbol: str = SYM):
    """在 (symbol, side) 上造一个指定类型的仓。"""
    if kind is None:
        return None
    if kind == "strategy":
        return await rig.open_strategy(symbol=symbol, side=side)
    if kind == "closing":
        rec = await rig.open_strategy(symbol=symbol, side=side)
        return await rig.registry.set_closing(rec.id, True, cause="close_one")
    if kind == "ai":
        return await rig.open_strategy(symbol=symbol, side=side, ownership=Ownership.AI, model="m3")
    if kind == "mixed":
        await rig.open_strategy(symbol=symbol, side=side)
        return await rig.apply(symbol=symbol, position_side=side, qty=1, price=100, origin=EXTERNAL_ORIGIN, purpose=None)
    if kind == "pending":
        return await rig.pending(symbol=symbol, side=side)
    if kind == "ignored":
        rec = await rig.pending(symbol=symbol, side=side)
        return await rig.registry.ignore(rec.id, expected_version=None)
    if kind == "taking_over":
        return await rig.takeover(symbol=symbol, side=side)
    if kind == "managed_takeover":
        return await rig.managed_takeover(symbol=symbol, side=side)
    raise AssertionError(kind)


# (同方向已有的, 对侧已有的, 期望原因里的关键词；None 表示可以开)
BLOCK_CASES = [
    (None, None, None),
    ("strategy", None, "同方向已有策略仓"),
    ("closing", None, "同方向已有策略仓"),
    (None, "strategy", REVERSAL),
    (None, "closing", REVERSAL),
    ("strategy", "strategy", "同方向已有策略仓"),
    ("ignored", None, "外部仓"),
    (None, "ignored", "外部仓"),
    ("pending", None, "待确认"),
    (None, "pending", "待确认"),
    ("taking_over", None, "接管仓"),
    (None, "taking_over", "接管仓"),
    ("managed_takeover", None, "接管仓"),
    (None, "managed_takeover", "接管仓"),
    ("mixed", None, "混合仓"),
    (None, "mixed", "混合仓"),
    ("ai", None, "AI 仓"),
    (None, "ai", "AI 仓"),
    # 任一方向上的外部仓、待确认仓、接管仓、混合仓优先于策略仓的两条
    ("strategy", "managed_takeover", "接管仓"),
    ("managed_takeover", "strategy", "接管仓"),
    ("strategy", "ignored", "外部仓"),
    ("strategy", "pending", "待确认"),
    ("strategy", "mixed", "混合仓"),
]


@pytest.mark.parametrize("ask", [LONG, SHORT])
@pytest.mark.parametrize("same, opposite, expected", BLOCK_CASES)
async def test_strategy_open_block_follows_f1_step_three(rig, ask, same, opposite, expected):
    await rig.up()
    await _hold(rig, same, ask)
    await _hold(rig, opposite, ask.opposite())
    occ = rig.registry.occupancy(SYM)
    reason = occ.strategy_open_block(ask)
    if expected is None:
        assert reason is None
    elif expected == REVERSAL:
        assert reason == REVERSAL  # 原文
    else:
        assert reason is not None and expected in reason and SYM in reason


async def test_occupancy_exposes_both_sides_and_ignores_other_symbols(rig):
    await rig.up()
    long_rec = await rig.open_strategy(side=LONG)
    short_rec = await rig.pending(side=SHORT)
    other = await rig.open_strategy(symbol="ETHUSDT", side=LONG)
    occ = rig.registry.occupancy(SYM)
    assert isinstance(occ, KeyOccupancy)
    assert occ.symbol == SYM and occ.long == long_rec and occ.short == short_rec
    eth = rig.registry.occupancy("ETHUSDT")
    assert eth.long == other and eth.short is None
    assert eth.strategy_open_block(SHORT) == REVERSAL
    assert rig.registry.occupancy("XRPUSDT").strategy_open_block(LONG) is None


async def test_occupancy_is_freed_when_the_position_closes(rig):
    await rig.up()
    rec = await rig.open_strategy(side=LONG)
    assert rig.registry.occupancy(SYM).strategy_open_block(LONG) is not None
    await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=T0 + 1)
    occ = rig.registry.occupancy(SYM)
    assert occ.long is None and occ.strategy_open_block(LONG) is None and occ.strategy_open_block(SHORT) is None


async def test_occupancy_snapshot_does_not_change_when_the_registry_does(rig):
    await rig.up()
    await rig.open_strategy(side=LONG)
    occ = rig.registry.occupancy(SYM)
    await rig.open_strategy(side=SHORT)
    assert occ.short is None and rig.registry.occupancy(SYM).short is not None


# ================================================================ 查询与计数

async def test_queries_cover_every_non_closed_state(rig):
    await rig.up()
    managed = await rig.open_strategy(symbol="AUSDT")
    taking = await rig.takeover(symbol="BUSDT")
    closing = await _hold(rig, "closing", LONG, "CUSDT")
    pending = await rig.pending(symbol="DUSDT")
    ignored = await _hold(rig, "ignored", LONG, "EUSDT")
    done = await rig.open_strategy(symbol="FUSDT")
    await rig.registry.mark_closed(done.id, exit_reason=ExitReason.MANUAL, closed_ms=T0 + 1)

    assert [r.id for r in rig.registry.all_active()] == [managed.id, taking.id, closing.id, pending.id, ignored.id]
    assert [r.id for r in rig.registry.registered()] == [managed.id, taking.id, closing.id]
    assert rig.registry.by_id(pending.id) == pending and rig.registry.by_id(done.id) is None
    assert rig.registry.get_active("FUSDT", LONG) is None
    assert (await rig.registry.fetch(done.id)).exit_reason is ExitReason.MANUAL  # 含已平


async def test_counts_by_state_and_unprotected_registered_positions(rig):
    await rig.up()
    assert rig.registry.counts() == {"managed": 0, "unprotected": 0, "pending": 0, "external": 0,
                                     "taking_over": 0, "closing": 0}
    await rig.open_strategy(symbol="AUSDT")                                        # 托管中，无保护
    b = await rig.open_strategy(symbol="BUSDT")                                    # 托管中，系统保护
    await rig.registry.set_protection(b.id, state=ProtectionState.SYSTEM, stop_price=D(90))
    await _hold(rig, "closing", LONG, "CUSDT")                                     # 平仓中，无保护
    await rig.takeover(symbol="DUSDT")                                             # 接管中，无保护
    e = await rig.managed_takeover(symbol="EUSDT")                                 # 托管中，外部保护
    await rig.registry.set_protection(e.id, state=ProtectionState.EXTERNAL)
    h = await rig.open_strategy(symbol="HUSDT")                                    # 托管中，重建中：不算「无保护」
    await rig.registry.set_protection(h.id, state=ProtectionState.REBUILDING)
    await rig.pending(symbol="IUSDT")                                              # 待确认：只计数，不算无保护
    await _hold(rig, "ignored", LONG, "JUSDT")                                     # 外部（已忽略）
    assert rig.registry.counts() == {"managed": 4, "unprotected": 3, "pending": 1, "external": 1,
                                     "taking_over": 1, "closing": 1}


async def test_counts_follow_state_changes(rig):
    await rig.up()
    rec = await rig.open_strategy()
    assert rig.registry.counts()["unprotected"] == 1
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D(90))
    assert rig.registry.counts()["unprotected"] == 0
    await rig.registry.set_closing(rec.id, True, cause="x")
    assert (rig.registry.counts()["managed"], rig.registry.counts()["closing"]) == (0, 1)
    await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=T0 + 1)
    assert sum(rig.registry.counts().values()) == 0


# ================================================================ 冷却

async def test_cooldown_is_stored_per_symbol_and_ends_at_the_deadline(rig, clock):
    await rig.up()
    assert rig.registry.cooldown_until(SYM) is None
    until = clock.now_ms() + 60_000
    assert await rig.registry.set_cooldown(SYM, until, "止损后冷却") is None
    assert rig.registry.cooldown_until(SYM) == until
    assert rig.registry.cooldown_until("ETHUSDT") is None
    clock.advance(59_999)
    assert rig.registry.cooldown_until(SYM) == until
    clock.advance(1)
    assert rig.registry.cooldown_until(SYM) is None  # 到点即结束；过期的不再返回


async def test_cooldown_replaces_the_previous_one_and_is_persisted(rig, clock):
    await rig.up()
    await rig.registry.set_cooldown(SYM, clock.now_ms() + 120_000, "第一次")
    clock.advance(1_000)
    later = clock.now_ms() + 30_000
    await rig.registry.set_cooldown(SYM, later, "第二次")  # 后一次覆盖前一次，哪怕更短
    assert rig.registry.cooldown_until(SYM) == later
    rows = await rig.ledger.read(repo_registry.list_cooldowns)
    assert [(r.symbol, r.until_ms, r.reason, r.set_ms) for r in rows] == [(SYM, later, "第二次", clock.now_ms())]

    reborn = rig.another()
    await reborn.up()
    assert reborn.registry.cooldown_until(SYM) == later
    clock.advance(40_000)
    assert reborn.registry.cooldown_until(SYM) is None


# ================================================================ 重启 load

async def test_load_restores_the_same_cache_after_restart(rig, clock):
    await rig.up()
    await rig.open_strategy(symbol="AUSDT", side=LONG)
    await rig.open_strategy(symbol="AUSDT", side=SHORT)
    await rig.takeover(symbol="BUSDT")
    await rig.managed_takeover(symbol="CUSDT")
    await _hold(rig, "closing", LONG, "DUSDT")
    await _hold(rig, "ignored", LONG, "EUSDT")
    await rig.pending(symbol="FUSDT")
    gone = await rig.open_strategy(symbol="GUSDT")
    await rig.registry.mark_closed(gone.id, exit_reason=ExitReason.SIGNAL, closed_ms=T0 + 5)
    await rig.registry.set_cooldown("GUSDT", clock.now_ms() + 10_000, "平仓后冷却")
    prot = await rig.open_strategy(symbol="HUSDT")
    await rig.registry.set_protection(prot.id, state=ProtectionState.SYSTEM, stop_price=D(90), stop_client_id="c")

    reborn = rig.another()
    assert reborn.registry.all_active() == []  # load 之前缓存是空的
    await reborn.up()
    assert reborn.registry.all_active() == rig.registry.all_active()
    assert reborn.registry.registered() == rig.registry.registered()
    assert reborn.registry.counts() == rig.registry.counts()
    for rec in rig.registry.all_active():
        assert reborn.registry.by_id(rec.id) == rec
        assert reborn.registry.get_active(rec.symbol, rec.side) == rec
    assert reborn.registry.by_id(gone.id) is None and reborn.registry.get_active("GUSDT", LONG) is None
    assert reborn.registry.cooldown_until("GUSDT") == rig.registry.cooldown_until("GUSDT")
    assert reborn.registry.occupancy("AUSDT") == rig.registry.occupancy("AUSDT")


async def test_cache_equals_the_ledger_after_a_mixed_sequence_of_changes(rig):
    await rig.up()
    a = await rig.open_strategy(symbol="AUSDT", qty=2)
    await rig.apply(symbol="AUSDT", qty=1, price=110, origin=EXTERNAL_ORIGIN, purpose=None)       # 混合
    await rig.apply(symbol="AUSDT", qty=1, price=110, opening=False, origin=EXTERNAL_ORIGIN, purpose=None)
    b = await rig.pending(symbol="BUSDT")
    await rig.registry.begin_takeover(symbol="BUSDT", side=LONG, qty=D(1), avg_price=D(100), open_time_ms=T0,
                                      open_time_estimated=False, anchor_ms=T0, signal_exit=False, position_id=b.id)
    await rig.registry.mark_managed(b.id)
    await rig.registry.update_exchange_view(a.id, exchange_qty=D(2), liquidation_price=D(50), seen_ms=T0)
    await rig.registry.mark_closed(b.id, exit_reason=ExitReason.EXTERNAL, closed_ms=T0 + 9)
    ledger_open = await rig.ledger.read(repo_registry.list_open)
    assert rig.registry.all_active() == ledger_open
    assert ledger_open == [r for r in await all_position_rows(rig.ledger) if r.status.value != "closed"]


async def test_load_replaces_a_stale_cache(rig):
    await rig.up()
    other = rig.another()
    await other.up()
    rec = await other.open_strategy()
    assert rig.registry.all_active() == []  # 另一个登记簿写的，这边的缓存不知道
    await rig.registry.load()
    assert rig.registry.all_active() == [rec]
    await other.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=T0 + 1)
    await rig.registry.load()
    assert rig.registry.all_active() == []
