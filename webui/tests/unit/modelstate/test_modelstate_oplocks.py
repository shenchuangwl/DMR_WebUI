"""操作锁：获取/释放规则（切换预览只能在没有别的锁时加；切换中要求没有批量平仓中；批量平仓中要求没有切换中，预览随之取消）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.modelstate import ModelStateService, OpLockConflict, StaleVersion
from dmr_kernel.core.types import Gate, Model, OpLock, Suppression
from dmr_kernel.ledger import repo_audit

PREVIEW, SWITCHING, BULK = OpLock.SWITCH_PREVIEW, OpLock.SWITCHING, OpLock.BULK_CLOSING


@pytest.fixture
def svc(ledger, clock, bus) -> ModelStateService:
    """未加载的服务：协程测试里第一行 ``await svc.load()``。"""
    return ModelStateService(ledger, bus, clock)


async def audit_rows(ledger, **filters):
    return list(reversed(await ledger.read(repo_audit.recent, limit=1000, **filters)))


async def hold(svc, *locks: OpLock, owner: str = "alice") -> None:
    """按合法顺序拿到若干把锁（预览在前）。"""
    for lock in sorted(locks, key=lambda x: x is not PREVIEW):
        await svc.acquire_op_lock(lock, owner=owner)


# (已持有, 要获取的锁, 成功后持有的锁；None 表示应当冲突)
RULES = {
    "无锁→预览": ((), PREVIEW, {PREVIEW}),
    "预览→预览": ((PREVIEW,), PREVIEW, None),
    "切换中→预览": ((SWITCHING,), PREVIEW, None),
    "批量平仓中→预览": ((BULK,), PREVIEW, None),
    "无锁→切换中": ((), SWITCHING, {SWITCHING}),
    "预览→切换中（预览不挡切换）": ((PREVIEW,), SWITCHING, {PREVIEW, SWITCHING}),
    "批量平仓中→切换中": ((BULK,), SWITCHING, None),
    "切换中→切换中": ((SWITCHING,), SWITCHING, None),
    "无锁→批量平仓中": ((), BULK, {BULK}),
    "预览→批量平仓中（预览随之取消）": ((PREVIEW,), BULK, {BULK}),
    "切换中→批量平仓中": ((SWITCHING,), BULK, None),
    "批量平仓中→批量平仓中": ((BULK,), BULK, None),
    "预览+切换中→批量平仓中": ((PREVIEW, SWITCHING), BULK, None),
}


@pytest.mark.parametrize("held,lock,result", RULES.values(), ids=RULES.keys())
async def test_acquire_rules(svc, ledger, held, lock, result):
    await svc.load()
    await hold(svc, *held)
    before = svc.current()
    audits = len(await audit_rows(ledger))

    if result is None:
        with pytest.raises(OpLockConflict) as err:
            await svc.acquire_op_lock(lock, owner="bob")
        assert err.value.held == frozenset(held)
        assert svc.current() == before  # 冲突不改状态、不增版本、不留审计
        assert len(await audit_rows(ledger)) == audits
    else:
        snap = await svc.acquire_op_lock(lock, owner="bob")
        assert snap.op_locks == frozenset(result)
        assert snap.version == before.version + 1
        assert svc.current() == snap


async def test_conflict_message_names_the_held_lock(svc):
    await svc.load()
    await svc.acquire_op_lock(BULK, owner="alice")
    with pytest.raises(OpLockConflict, match="批量平仓"):
        await svc.acquire_op_lock(PREVIEW, owner="bob")
    with pytest.raises(OpLockConflict, match="批量平仓"):
        await svc.acquire_op_lock(SWITCHING, owner="bob")


# ---------------------------------------------------------------- 持有者与有效期


async def test_owner_and_expiry_are_recorded_with_the_lock(svc):
    await svc.load()
    snap = await svc.acquire_op_lock(PREVIEW, owner="seat-1", expires_ms=1_760_000_060_000)
    assert (snap.op_lock_owner, snap.op_lock_expires_ms) == ("seat-1", 1_760_000_060_000)
    assert not snap.can_open(Model.M1)[0] and not snap.can_auto_takeover()[0]


async def test_confirming_the_switch_takes_over_owner_and_stops_the_preview_clock(svc):
    await svc.load()
    await svc.acquire_op_lock(PREVIEW, owner="seat-1", expires_ms=1_760_000_060_000)
    snap = await svc.acquire_op_lock(SWITCHING, owner="seat-1")  # 确认后预览有效期不再计时
    assert snap.op_locks == {PREVIEW, SWITCHING}
    assert (snap.op_lock_owner, snap.op_lock_expires_ms) == ("seat-1", None)


async def test_bulk_closing_cancels_the_preview_and_says_so(svc, ledger):
    await svc.load()
    await svc.acquire_op_lock(PREVIEW, owner="seat-1", expires_ms=1_760_000_060_000)
    snap = await svc.acquire_op_lock(BULK, owner="alice")
    assert snap.op_locks == {BULK} and (snap.op_lock_owner, snap.op_lock_expires_ms) == ("alice", None)
    row = (await audit_rows(ledger, action="model_state.op_lock_acquire"))[-1]
    assert row.before["op_locks"] == ["switch_preview"] and row.after["op_locks"] == ["bulk_closing"]
    assert "预览" in row.after["reason"] and "取消" in row.after["reason"]


async def test_acquire_does_not_look_at_suppressions_or_gate(svc):
    # 有抑制原因时能不能切换由 allowed_operations 判断；这里只管锁与锁之间的规则
    await svc.load()
    await svc.set_gate(Gate.RUNNING, reason="x")
    await svc.add_suppression(Suppression.CLOCK_SKEW, reason="y")
    snap = await svc.acquire_op_lock(PREVIEW, owner="alice")
    assert snap.op_locks == {PREVIEW}


async def test_acquire_accepts_the_string_value_and_rejects_unknown_locks(svc):
    await svc.load()
    snap = await svc.acquire_op_lock("bulk_closing", owner="alice")  # type: ignore[arg-type]
    assert snap.op_locks == {BULK}
    with pytest.raises(ValueError):
        await svc.acquire_op_lock("frozen", owner="alice")  # type: ignore[arg-type]


# ---------------------------------------------------------------- 乐观并发：expected_version


async def test_acquire_checks_expected_version_before_the_lock_rules(svc, ledger):
    await svc.load()
    await svc.acquire_op_lock(BULK, owner="alice")
    before = svc.current()
    with pytest.raises(StaleVersion):  # 版本过期优先于锁冲突
        await svc.acquire_op_lock(PREVIEW, owner="bob", expected_version=before.version - 1)
    with pytest.raises(OpLockConflict):  # 版本对了才轮到锁规则
        await svc.acquire_op_lock(PREVIEW, owner="bob", expected_version=before.version)
    assert svc.current() == before


async def test_acquire_with_current_version_succeeds(svc):
    await svc.load()
    v = svc.current().version
    snap = await svc.acquire_op_lock(PREVIEW, owner="alice", expected_version=v)
    assert snap.version == v + 1


# ---------------------------------------------------------------- 释放


async def test_release_clears_owner_and_expiry_when_no_lock_remains(svc):
    await svc.load()
    await svc.acquire_op_lock(PREVIEW, owner="seat-1", expires_ms=1_760_000_060_000)
    snap = await svc.release_op_lock(PREVIEW, owner="seat-1")
    assert snap.op_locks == frozenset()
    assert (snap.op_lock_owner, snap.op_lock_expires_ms) == (None, None)


async def test_release_by_another_owner_is_refused(svc):
    await svc.load()
    await svc.acquire_op_lock(PREVIEW, owner="seat-1")
    before = svc.current()
    with pytest.raises(OpLockConflict, match="seat-1"):
        await svc.release_op_lock(PREVIEW, owner="seat-2")
    assert svc.current() == before


async def test_release_without_owner_is_a_forced_release(svc):
    await svc.load()
    await svc.acquire_op_lock(BULK, owner="alice")
    assert (await svc.release_op_lock(BULK)).op_locks == frozenset()


async def test_releasing_one_lock_keeps_the_other_and_the_owner(svc):
    await svc.load()
    await hold(svc, PREVIEW, SWITCHING, owner="seat-1")
    snap = await svc.release_op_lock(PREVIEW)
    assert snap.op_locks == {SWITCHING} and snap.op_lock_owner == "seat-1"
    assert (await svc.release_op_lock(SWITCHING)).op_lock_owner is None


async def test_releasing_a_lock_that_is_not_held_changes_nothing(svc, ledger):
    await svc.load()
    await svc.acquire_op_lock(PREVIEW, owner="seat-1")
    before, audits = svc.current(), len(await audit_rows(ledger))
    # 即使 owner 对不上，没持有的锁也只是空操作（清理路径可以放心多调）
    assert await svc.release_op_lock(BULK, owner="someone-else") == before
    assert await svc.release_op_lock(SWITCHING) == before
    assert len(await audit_rows(ledger)) == audits


async def test_lock_can_be_taken_again_after_release(svc):
    await svc.load()
    await svc.acquire_op_lock(BULK, owner="alice")
    await svc.release_op_lock(BULK, owner="alice")
    assert (await svc.acquire_op_lock(PREVIEW, owner="bob")).op_locks == {PREVIEW}


async def test_lock_audit_records_the_owner(svc, ledger):
    await svc.load()
    await svc.acquire_op_lock(PREVIEW, owner="seat-1", expires_ms=123)
    row = (await audit_rows(ledger, action="model_state.op_lock_acquire"))[-1]
    assert row.after["op_locks"] == ["switch_preview"] and row.after["op_lock_owner"] == "seat-1"
    assert row.after["op_lock_expires_ms"] == 123
    await svc.release_op_lock(PREVIEW, owner="seat-1")
    row = (await audit_rows(ledger, action="model_state.op_lock_release"))[-1]
    assert row.before["op_locks"] == ["switch_preview"] and row.after["op_locks"] == []
