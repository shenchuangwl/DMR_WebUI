"""操作席位服务（api.seat.SeatService）：申请、续租、释放、强制接管、租约到期自动释放、只有一个持有者。

真实的账本与手动时钟；席位变化经总线发布 ``SeatChanged``（本地事件，事件流把它映射成 ``state`` 帧）。
"""
from __future__ import annotations

import asyncio

import pytest

from api_kit import T0
from dmr_kernel.api.common import Principal
from dmr_kernel.api.errors import ApiError
from dmr_kernel.ledger import repo_audit

LEASE_MS = 60_000


def audit(ledger, **filters):
    return ledger.read_sync(repo_audit.recent, limit=100, **filters)


async def fails(coro) -> ApiError:
    with pytest.raises(ApiError) as caught:
        await coro
    return caught.value


def causes(events):
    return [(e.cause, e.holder_session_id) for e in events]


# ---------------------------------------------------------------- 查看

async def test_an_empty_seat_has_no_holder_and_is_not_mine(rig):
    principal = await rig.principal()
    assert await rig.seat.view(principal) == {
        "holder_session_id": None, "holder_operator": None, "lease_until_ms": None, "mine": False}


async def test_the_cli_principal_sees_the_seat_but_never_owns_it(rig):
    alice = await rig.principal()
    await rig.seat.acquire(alice)
    view = await rig.seat.view(Principal.cli())
    assert view["holder_operator"] == "alice" and view["mine"] is False


# ---------------------------------------------------------------- 申请

async def test_acquire_gives_the_seat_with_a_lease_and_announces_it(rig, ledger):
    alice = await rig.principal()
    view = await rig.seat.acquire(alice)
    assert view == {"holder_session_id": alice.session_id, "holder_operator": "alice",
                    "lease_until_ms": T0 + LEASE_MS, "mine": True}
    assert causes(await rig.settle()) == [("acquire", alice.session_id)]
    (row,) = audit(ledger, action="seat.acquire")
    assert row.operator == "alice" and row.target == "seat"
    assert row.before["holder_session_id"] is None and row.after["holder_session_id"] == alice.session_id


async def test_other_sessions_see_the_holder_but_not_as_mine(rig):
    alice = await rig.principal("alice")
    bob = await rig.principal("bob")
    await rig.seat.acquire(alice)
    view = await rig.seat.view(bob)
    assert view["holder_operator"] == "alice" and view["holder_session_id"] == alice.session_id
    assert view["mine"] is False


async def test_only_one_session_holds_the_seat_and_the_refusal_names_the_holder(rig):
    alice, bob = await rig.principal("alice"), await rig.principal("bob")
    await rig.seat.acquire(alice)
    error = await fails(rig.seat.acquire(bob))
    assert (error.status, error.code) == (409, "seat_taken")
    assert error.extra == {"holder_operator": "alice", "lease_until_ms": T0 + LEASE_MS}
    assert (await rig.seat.view(bob))["holder_session_id"] == alice.session_id  # 申请失败没有改任何东西


async def test_a_second_session_of_the_same_operator_does_not_share_the_seat(rig):
    first = await rig.principal("alice")
    second = await rig.auth.authenticate((await rig.login("alice")).token)
    await rig.seat.acquire(first)
    assert (await fails(rig.seat.acquire(second))).code == "seat_taken"


async def test_the_holder_acquiring_again_is_a_silent_renewal(rig, clock, ledger):
    alice = await rig.principal()
    await rig.seat.acquire(alice)
    clock.advance(20_000)
    view = await rig.seat.acquire(alice)
    assert view["lease_until_ms"] == T0 + 20_000 + LEASE_MS and view["mine"] is True
    assert causes(await rig.settle()) == [("acquire", alice.session_id)]  # 只有第一次发了事件
    assert len(audit(ledger, action="seat.acquire")) == 1


async def test_the_lease_length_comes_from_the_configuration(rig):
    rig.config["seat_lease_s"] = 15
    alice = await rig.principal()
    assert (await rig.seat.acquire(alice))["lease_until_ms"] == T0 + 15_000


async def test_the_cli_token_cannot_take_a_seat(rig):
    cli = Principal.cli()
    for call in (rig.seat.acquire, rig.seat.renew, rig.seat.release):
        error = await fails(call(cli))
        assert (error.status, error.code) == (409, "not_allowed")
    assert (await rig.seat.view(cli))["holder_session_id"] is None


async def test_a_session_that_was_revoked_meanwhile_cannot_take_the_seat(rig):
    alice = await rig.principal()
    await rig.auth.logout(alice)
    error = await fails(rig.seat.acquire(alice))
    assert (error.status, error.code) == (401, "unauthorized")


async def test_concurrent_acquires_produce_exactly_one_holder(rig):
    sessions = [await rig.principal(f"user{i}") for i in range(5)]
    results = await asyncio.gather(*(rig.seat.acquire(p) for p in sessions), return_exceptions=True)
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, ApiError)]
    assert len(winners) == 1 and len(losers) == 4 and all(e.code == "seat_taken" for e in losers)
    assert len(await rig.settle()) == 1


# ---------------------------------------------------------------- 续租

async def test_renew_extends_the_lease_from_now_without_event_or_audit(rig, clock, ledger):
    alice = await rig.principal()
    await rig.seat.acquire(alice)
    clock.advance(30_000)
    view = await rig.seat.renew(alice)
    assert view["lease_until_ms"] == T0 + 30_000 + LEASE_MS and view["mine"] is True
    assert causes(await rig.settle()) == [("acquire", alice.session_id)]
    assert audit(ledger, action="seat.renew") == []


async def test_renew_by_a_non_holder_is_refused(rig):
    alice, bob = await rig.principal("alice"), await rig.principal("bob")
    error = await fails(rig.seat.renew(bob))  # 没人持有
    assert (error.status, error.code) == (409, "seat_required")
    await rig.seat.acquire(alice)
    assert (await fails(rig.seat.renew(bob))).code == "seat_required"  # 别人持有
    assert (await rig.seat.view(alice))["lease_until_ms"] == T0 + LEASE_MS  # 没有被动过


async def test_renew_after_the_lease_lapsed_is_refused_even_if_nobody_took_over(rig, clock):
    alice = await rig.principal()
    await rig.seat.acquire(alice)
    clock.advance(LEASE_MS)  # 恰好到点就算到期
    assert (await fails(rig.seat.renew(alice))).code == "seat_required"


# ---------------------------------------------------------------- 释放

async def test_release_frees_the_seat_and_announces_it(rig, ledger):
    alice, bob = await rig.principal("alice"), await rig.principal("bob")
    await rig.seat.acquire(alice)
    view = await rig.seat.release(alice)
    assert view == {"holder_session_id": None, "holder_operator": None, "lease_until_ms": None, "mine": False}
    assert causes(await rig.settle()) == [("acquire", alice.session_id), ("release", None)]
    assert (await rig.seat.acquire(bob))["mine"] is True  # 立刻可被别人申请
    (row,) = audit(ledger, action="seat.release")
    assert row.operator == "alice"


async def test_release_by_a_non_holder_changes_nothing(rig):
    alice, bob = await rig.principal("alice"), await rig.principal("bob")
    await rig.seat.acquire(alice)
    view = await rig.seat.release(bob)
    assert view["holder_session_id"] == alice.session_id and view["mine"] is False
    assert causes(await rig.settle()) == [("acquire", alice.session_id)]


async def test_releasing_an_empty_seat_is_harmless(rig):
    alice = await rig.principal()
    assert (await rig.seat.release(alice))["holder_session_id"] is None
    assert await rig.settle() == []


# ---------------------------------------------------------------- 到期自动释放

async def test_a_lapsed_lease_frees_the_seat_exactly_at_the_deadline(rig, clock):
    alice, bob = await rig.principal("alice"), await rig.principal("bob")
    await rig.seat.acquire(alice)
    clock.advance(LEASE_MS - 1)
    assert (await rig.seat.view(bob))["holder_session_id"] == alice.session_id  # 差 1 毫秒仍持有
    assert (await fails(rig.seat.acquire(bob))).code == "seat_taken"
    clock.advance(1)
    assert (await rig.seat.view(bob))["holder_session_id"] is None
    view = await rig.seat.acquire(bob)  # 不需要任何清理动作，申请时就把到期的当作没人
    assert view["holder_session_id"] == bob.session_id and view["mine"] is True


async def test_expire_due_clears_a_lapsed_lease_audits_it_and_announces_it(rig, clock, ledger):
    alice = await rig.principal()
    await rig.seat.acquire(alice)
    assert await rig.seat.expire_due() is None  # 租约还在
    clock.advance(LEASE_MS)
    assert await rig.seat.expire_due() == alice.session_id
    assert await rig.seat.expire_due() is None  # 已经清掉，不重复
    assert causes(await rig.settle()) == [("acquire", alice.session_id), ("expired", None)]
    (row,) = audit(ledger, action="seat.expired")
    assert row.operator == "system" and row.before["holder_operator"] == "alice"


async def test_renewing_keeps_the_seat_past_the_original_deadline(rig, clock):
    alice = await rig.principal()
    await rig.seat.acquire(alice)
    clock.advance(LEASE_MS - 1_000)
    await rig.seat.renew(alice)
    clock.advance(LEASE_MS - 1_000)  # 距登记已超过 60 秒，但续租过
    assert await rig.seat.expire_due() is None
    assert (await rig.seat.view(alice))["mine"] is True


# ---------------------------------------------------------------- 强制接管

async def test_force_takes_the_seat_from_the_holder_and_records_both_sides(rig, ledger):
    alice, bob = await rig.principal("alice"), await rig.principal("bob")
    await rig.seat.acquire(alice)
    view = await rig.seat.force(bob, request_id="req-7")
    assert view["holder_session_id"] == bob.session_id and view["mine"] is True
    assert view["lease_until_ms"] == T0 + LEASE_MS
    assert (await rig.seat.view(alice))["mine"] is False  # 原持有者立刻失去席位
    assert causes(await rig.settle()) == [("acquire", alice.session_id), ("force", bob.session_id)]
    (row,) = audit(ledger, action="seat.force")
    assert row.operator == "bob" and row.request_id == "req-7"
    assert row.before["holder_operator"] == "alice" and row.after["holder_operator"] == "bob"


async def test_force_on_an_empty_seat_also_works_and_is_audited(rig, ledger):
    bob = await rig.principal("bob")
    assert (await rig.seat.force(bob))["mine"] is True
    (row,) = audit(ledger, action="seat.force")
    assert row.before["holder_session_id"] is None


async def test_the_holder_forcing_its_own_seat_announces_nothing_new(rig, ledger):
    alice = await rig.principal()
    await rig.seat.acquire(alice)
    await rig.seat.force(alice)
    assert causes(await rig.settle()) == [("acquire", alice.session_id)]
    assert len(audit(ledger, action="seat.force")) == 1  # 但留痕


async def test_the_cli_token_cannot_force_the_seat(rig):
    assert (await fails(rig.seat.force(Principal.cli()))).code == "not_allowed"


async def test_force_by_a_revoked_session_is_unauthorized(rig):
    alice = await rig.principal()
    await rig.auth.logout(alice)
    assert (await fails(rig.seat.force(alice))).code == "unauthorized"
