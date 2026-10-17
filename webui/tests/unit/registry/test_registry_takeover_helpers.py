"""登记簿的两个公开方法：忽略混合仓、按原归属恢复待确认仓（取代 ops 对私有方法的调用）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.events import PositionChanged
from dmr_kernel.core.registry import StaleVersion
from dmr_kernel.core.types import Ownership, PendingReason, PositionStatus
from registry_kit import EXTERNAL_ORIGIN, LONG, SYM, T0


async def _mixed(rig):
    await rig.open_strategy()
    return await rig.apply(symbol=SYM, position_side=LONG, qty=1, price=100, origin=EXTERNAL_ORIGIN, purpose=None)


async def test_ignore_mixed_moves_it_to_external_and_updates_the_cache(rig):
    await rig.up()
    mixed = await _mixed(rig)
    assert mixed.is_mixed
    rig.clear_events()
    rec = await rig.registry.ignore_mixed(mixed.id, expected_version=mixed.version)
    assert rec.status is PositionStatus.IGNORED and rec.ownership is Ownership.NONE and not rec.is_mixed
    assert rig.registry.by_id(mixed.id).status is PositionStatus.IGNORED
    events = await rig.events(PositionChanged)
    assert [e.cause for e in events] == ["ignored"]


async def test_ignore_mixed_checks_the_version_and_the_kind(rig):
    await rig.up()
    mixed = await _mixed(rig)
    with pytest.raises(StaleVersion):
        await rig.registry.ignore_mixed(mixed.id, expected_version=mixed.version + 5)
    plain = await rig.pending(symbol="ETHUSDT")
    with pytest.raises(Exception):
        await rig.registry.ignore_mixed(plain.id, expected_version=None)


async def test_restore_original_turns_a_pending_position_into_the_original_owner(rig):
    await rig.up()
    pending = await rig.pending(reason=PendingReason.ORIGIN_TO_VERIFY)
    rig.clear_events()
    rec = await rig.registry.restore_original(
        pending.id, ownership=Ownership.STRATEGY, open_time_ms=T0, opened_by_intent_id=None,
        note="恢复托管：测试", expected_version=pending.version)
    assert rec.status is PositionStatus.MANAGED and rec.ownership is Ownership.STRATEGY
    assert rec.open_time_ms == T0 and "恢复托管" in rec.origin_note
    assert rig.registry.by_id(pending.id).status is PositionStatus.MANAGED
    assert [e.cause for e in await rig.events(PositionChanged)] == ["restored"]


async def test_restore_original_rejects_a_stale_version_and_non_original_owners(rig):
    await rig.up()
    pending = await rig.pending(reason=PendingReason.ORIGIN_TO_VERIFY)
    with pytest.raises(StaleVersion):
        await rig.registry.restore_original(
            pending.id, ownership=Ownership.AI, open_time_ms=T0, opened_by_intent_id=None, note="x",
            expected_version=pending.version + 3)
    with pytest.raises(ValueError):
        await rig.registry.restore_original(
            pending.id, ownership=Ownership.TAKEOVER, open_time_ms=T0, opened_by_intent_id=None, note="x",
            expected_version=None)
