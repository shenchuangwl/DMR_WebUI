"""登录与席位的仓储函数（ledger.repo_auth）：operator、session、seat 三张表。

签名一律 fn(session, ...)，经 Ledger.write/read 调用；这里用同步的 write_sync/read_sync，时间都是显式给的毫秒。
席位的判定（谁持有、到没到期）全部在写事务里完成，并发的申请由唯一的写线程串行。
"""
from __future__ import annotations

import asyncio

import pytest

from dmr_kernel.ledger import repo_audit, repo_auth
from dmr_kernel.ledger.errors import IntegrityError
from dmr_kernel.ledger.records import OperatorRecord, SessionRecord
from tests.support.ledger import START_MS

T0 = START_MS
LEASE = 60_000


def operator(ledger, name="alice", role="admin", now_ms=T0) -> OperatorRecord:
    return ledger.write_sync(repo_auth.create_operator, username=name, password_hash=f"scrypt$16$1$1$aa$bb-{name}",
                             role=role, now_ms=now_ms)


def open_session(ledger, op: OperatorRecord, token="token-1", now_ms=T0) -> SessionRecord:
    return ledger.write_sync(repo_auth.create_session, token_hash=f"hash-{token}", operator_id=op.id, now_ms=now_ms,
                             client_info="pytest")


def audit_rows(ledger, **filters):
    return ledger.read_sync(repo_audit.recent, limit=100, **filters)


def acquire(ledger, sess, op, now_ms=T0, lease_ms=LEASE):
    return ledger.write_sync(repo_auth.acquire_seat, session_id=sess.id, now_ms=now_ms,
                             lease_ms=lease_ms)


# ---------------------------------------------------------------- 操作员


def test_create_operator_returns_the_record(ledger):
    rec = operator(ledger, "alice", role="admin", now_ms=T0 + 5)
    assert (rec.username, rec.role, rec.created_ms, rec.disabled) == ("alice", "admin", T0 + 5, False)
    assert rec.id >= 1 and rec.password_hash.startswith("scrypt$")
    assert "scrypt" not in repr(rec)  # 记录的 repr 不带口令哈希


def test_operator_lookup_by_name_and_id_and_list(ledger):
    alice, bob = operator(ledger, "alice"), operator(ledger, "bob", role="viewer")
    assert ledger.read_sync(repo_auth.get_operator_by_name, "alice") == alice
    assert ledger.read_sync(repo_auth.get_operator, bob.id) == bob
    assert ledger.read_sync(repo_auth.get_operator_by_name, "nobody") is None
    assert ledger.read_sync(repo_auth.get_operator, 999) is None
    assert [o.username for o in ledger.read_sync(repo_auth.list_operators)] == ["alice", "bob"]


def test_operator_usernames_are_exact_and_unique(ledger):
    operator(ledger, "alice")
    with pytest.raises(repo_auth.OperatorExists):
        operator(ledger, "alice")
    operator(ledger, "Alice")  # 区分大小写：另一个操作员
    assert len(ledger.read_sync(repo_auth.list_operators)) == 2


def test_create_operator_writes_an_audit_row_without_the_hash(ledger):
    operator(ledger, "alice")
    (row,) = audit_rows(ledger, action="operator.create")
    assert row.target == "operator:alice" and row.result == "ok"
    assert "scrypt" not in repr(row.before) + repr(row.after)
    assert row.after == {"username": "alice", "role": "admin"}


# ---------------------------------------------------------------- 会话


def test_create_session_stores_only_the_given_hash_and_starts_fresh(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op, token="abc", now_ms=T0 + 10)
    assert sess.token_hash == "hash-abc" and sess.operator_id == op.id
    assert (sess.created_ms, sess.last_active_ms, sess.reauth_ms, sess.revoked) == (T0 + 10, T0 + 10, None, False)
    assert sess.client_info == "pytest"


def test_create_session_writes_a_login_audit_row(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op, now_ms=T0 + 7)
    (row,) = audit_rows(ledger, action="auth.login")
    assert (row.operator, row.target, row.result, row.at_ms) == ("alice", f"session:{sess.id}", "ok", T0 + 7)
    assert row.after == {"session_id": sess.id}


def test_create_session_for_an_unknown_operator_fails_and_writes_nothing(ledger):
    with pytest.raises((IntegrityError, LookupError)):
        ledger.write_sync(repo_auth.create_session, token_hash="h", operator_id=12345, now_ms=T0)
    assert audit_rows(ledger, action="auth.login") == []


def test_session_context_joins_the_operator(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    ctx = ledger.read_sync(repo_auth.get_session_context, "hash-token-1")
    assert ctx.session == sess and ctx.operator == op
    assert ledger.read_sync(repo_auth.get_session_context, "hash-unknown") is None


def test_touch_only_moves_activity_forward(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op, now_ms=T0)
    ledger.write_sync(repo_auth.touch_session, sess.id, now_ms=T0 + 5_000)
    assert ledger.read_sync(repo_auth.get_session_context, sess.token_hash).session.last_active_ms == T0 + 5_000
    ledger.write_sync(repo_auth.touch_session, sess.id, now_ms=T0 + 1_000)  # 乱序的旧时间不倒退
    assert ledger.read_sync(repo_auth.get_session_context, sess.token_hash).session.last_active_ms == T0 + 5_000


def test_set_reauth_records_the_time_and_audits_it(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    ledger.write_sync(repo_auth.set_reauth, sess.id, now_ms=T0 + 100, via="reauth")
    assert ledger.read_sync(repo_auth.get_session_context, sess.token_hash).session.reauth_ms == T0 + 100
    (row,) = audit_rows(ledger, action="auth.reauth")
    assert row.operator == "alice" and row.target == f"session:{sess.id}" and row.after == {"via": "reauth"}


def test_set_reauth_on_a_revoked_session_is_refused(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    ledger.write_sync(repo_auth.end_session, sess.id, now_ms=T0, reason="logout")
    with pytest.raises(repo_auth.SessionInactive):
        ledger.write_sync(repo_auth.set_reauth, sess.id, now_ms=T0 + 1, via="reauth")


def test_end_session_revokes_and_audits_once(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    first = ledger.write_sync(repo_auth.end_session, sess.id, now_ms=T0 + 1, reason="logout")
    assert (first.revoked, first.seat_released) == (True, False)
    assert ledger.read_sync(repo_auth.get_session_context, sess.token_hash).session.revoked is True
    second = ledger.write_sync(repo_auth.end_session, sess.id, now_ms=T0 + 2, reason="logout")
    assert (second.revoked, second.seat_released) == (False, False)
    (row,) = audit_rows(ledger, action="auth.logout")
    assert row.after == {"reason": "logout"} and row.target == f"session:{sess.id}"


def test_end_session_for_an_unknown_session_changes_nothing(ledger):
    result = ledger.write_sync(repo_auth.end_session, 999, now_ms=T0, reason="logout")
    assert (result.revoked, result.seat_released) == (False, False)
    assert audit_rows(ledger, action="auth.logout") == []


def test_end_session_releases_the_seat_it_holds_but_not_someone_elses(ledger):
    a, b = operator(ledger, "alice"), operator(ledger, "bob")
    sa, sb = open_session(ledger, a, "a"), open_session(ledger, b, "b")
    acquire(ledger, sa, a)
    assert ledger.write_sync(repo_auth.end_session, sb.id, now_ms=T0 + 1, reason="logout").seat_released is False
    assert ledger.read_sync(repo_auth.read_seat, now_ms=T0 + 1).holder_session_id == sa.id
    assert ledger.write_sync(repo_auth.end_session, sa.id, now_ms=T0 + 2, reason="logout").seat_released is True
    assert ledger.read_sync(repo_auth.read_seat, now_ms=T0 + 2).holder_session_id is None


# ---------------------------------------------------------------- 席位


def test_empty_seat_has_no_holder(ledger):
    state = ledger.read_sync(repo_auth.read_seat, now_ms=T0)
    assert (state.holder_session_id, state.holder_operator, state.lease_until_ms, state.acquired_ms) == (None,) * 4


def test_acquire_gives_the_seat_with_a_lease(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    out = acquire(ledger, sess, op, now_ms=T0 + 10)
    assert out.ok and out.changed and out.previous_session_id is None
    assert (out.state.holder_session_id, out.state.holder_operator) == (sess.id, "alice")
    assert out.state.lease_until_ms == T0 + 10 + LEASE and out.state.acquired_ms == T0 + 10
    assert ledger.read_sync(repo_auth.read_seat, now_ms=T0 + 11) == out.state


def test_second_session_cannot_take_a_live_seat(ledger):
    a, b = operator(ledger, "alice"), operator(ledger, "bob")
    sa, sb = open_session(ledger, a, "a"), open_session(ledger, b, "b")
    acquire(ledger, sa, a)
    out = acquire(ledger, sb, b, now_ms=T0 + 1_000)
    assert not out.ok and not out.changed
    assert (out.state.holder_session_id, out.state.holder_operator) == (sa.id, "alice")
    assert out.state.lease_until_ms == T0 + LEASE
    assert ledger.read_sync(repo_auth.read_seat, now_ms=T0 + 1_000).holder_session_id == sa.id


def test_two_sessions_of_the_same_operator_still_exclude_each_other(ledger):
    a = operator(ledger, "alice")
    s1, s2 = open_session(ledger, a, "one"), open_session(ledger, a, "two")
    acquire(ledger, s1, a)
    assert not acquire(ledger, s2, a, now_ms=T0 + 1).ok


def test_acquire_again_by_the_holder_just_renews_the_lease(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    acquire(ledger, sess, op, now_ms=T0)
    out = acquire(ledger, sess, op, now_ms=T0 + 20_000)
    assert out.ok and not out.changed
    assert out.state.lease_until_ms == T0 + 20_000 + LEASE and out.state.acquired_ms == T0
    assert len(audit_rows(ledger, action="seat.acquire")) == 1  # 续租不重复记


def test_lease_expiry_frees_the_seat_exactly_at_the_end(ledger):
    a, b = operator(ledger, "alice"), operator(ledger, "bob")
    sa, sb = open_session(ledger, a, "a"), open_session(ledger, b, "b")
    acquire(ledger, sa, a, now_ms=T0)
    assert ledger.read_sync(repo_auth.read_seat, now_ms=T0 + LEASE - 1).holder_session_id == sa.id
    assert ledger.read_sync(repo_auth.read_seat, now_ms=T0 + LEASE).holder_session_id is None
    assert not acquire(ledger, sb, b, now_ms=T0 + LEASE - 1).ok
    out = acquire(ledger, sb, b, now_ms=T0 + LEASE)
    assert out.ok and out.changed and out.previous_session_id == sa.id
    assert out.state.holder_operator == "bob"


def test_a_seat_held_by_a_revoked_session_counts_as_free(ledger):
    a, b = operator(ledger, "alice"), operator(ledger, "bob")
    sa, sb = open_session(ledger, a, "a"), open_session(ledger, b, "b")
    acquire(ledger, sa, a)
    ledger.write_sync(repo_auth.end_session, sa.id, now_ms=T0 + 1, reason="logout")
    assert ledger.read_sync(repo_auth.read_seat, now_ms=T0 + 2).holder_session_id is None
    assert acquire(ledger, sb, b, now_ms=T0 + 2).ok


def test_acquire_by_a_revoked_or_unknown_session_is_refused(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    ledger.write_sync(repo_auth.end_session, sess.id, now_ms=T0, reason="logout")
    with pytest.raises(repo_auth.SessionInactive):
        acquire(ledger, sess, op, now_ms=T0 + 1)
    with pytest.raises(repo_auth.SessionInactive):
        ledger.write_sync(repo_auth.acquire_seat, session_id=777, now_ms=T0, lease_ms=LEASE)
    assert ledger.read_sync(repo_auth.read_seat, now_ms=T0 + 1).holder_session_id is None


def test_renew_extends_only_for_the_live_holder(ledger):
    a, b = operator(ledger, "alice"), operator(ledger, "bob")
    sa, sb = open_session(ledger, a, "a"), open_session(ledger, b, "b")
    acquire(ledger, sa, a, now_ms=T0)
    out = ledger.write_sync(repo_auth.renew_seat, session_id=sa.id, now_ms=T0 + 30_000, lease_ms=LEASE)
    assert out.ok and not out.changed and out.state.lease_until_ms == T0 + 30_000 + LEASE
    assert out.state.acquired_ms == T0
    other = ledger.write_sync(repo_auth.renew_seat, session_id=sb.id, now_ms=T0 + 30_001, lease_ms=LEASE)
    assert not other.ok and other.state.holder_session_id == sa.id
    assert other.state.lease_until_ms == T0 + 30_000 + LEASE  # 别人的续租请求不改租约


def test_renew_after_the_lease_ran_out_is_refused_even_if_nobody_took_the_seat(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    acquire(ledger, sess, op, now_ms=T0)
    late = ledger.write_sync(repo_auth.renew_seat, session_id=sess.id, now_ms=T0 + LEASE, lease_ms=LEASE)
    assert not late.ok and late.state.holder_session_id is None


def test_renew_on_an_empty_seat_is_refused(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    out = ledger.write_sync(repo_auth.renew_seat, session_id=sess.id, now_ms=T0, lease_ms=LEASE)
    assert not out.ok and out.state.holder_session_id is None


def test_release_frees_the_seat_for_the_holder_only(ledger):
    a, b = operator(ledger, "alice"), operator(ledger, "bob")
    sa, sb = open_session(ledger, a, "a"), open_session(ledger, b, "b")
    acquire(ledger, sa, a)
    stranger = ledger.write_sync(repo_auth.release_seat, session_id=sb.id, now_ms=T0 + 1)
    assert not stranger.ok and not stranger.changed and stranger.state.holder_session_id == sa.id
    mine = ledger.write_sync(repo_auth.release_seat, session_id=sa.id, now_ms=T0 + 2)
    assert mine.ok and mine.changed and mine.state.holder_session_id is None
    assert acquire(ledger, sb, b, now_ms=T0 + 3).ok
    assert [r.target for r in audit_rows(ledger, action="seat.release")] == ["seat"]


def test_release_on_an_empty_seat_is_a_harmless_no_op(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    out = ledger.write_sync(repo_auth.release_seat, session_id=sess.id, now_ms=T0)
    assert not out.ok and not out.changed
    assert audit_rows(ledger, action="seat.release") == []


def test_force_takes_the_seat_from_a_live_holder_and_audits_before_and_after(ledger):
    a, b = operator(ledger, "alice"), operator(ledger, "bob")
    sa, sb = open_session(ledger, a, "a"), open_session(ledger, b, "b")
    acquire(ledger, sa, a, now_ms=T0)
    out = ledger.write_sync(repo_auth.force_seat, session_id=sb.id, now_ms=T0 + 5_000, lease_ms=LEASE,
                            request_id="req-1")
    assert out.ok and out.changed and out.previous_session_id == sa.id
    assert (out.state.holder_session_id, out.state.holder_operator) == (sb.id, "bob")
    assert out.state.lease_until_ms == T0 + 5_000 + LEASE
    (row,) = audit_rows(ledger, action="seat.force")
    assert (row.operator, row.target, row.result, row.request_id, row.at_ms) == ("bob", "seat", "ok", "req-1", T0 + 5_000)
    assert row.before["holder_session_id"] == sa.id and row.before["holder_operator"] == "alice"
    assert row.after["holder_session_id"] == sb.id and row.after["holder_operator"] == "bob"
    # 原持有者续租失败
    assert not ledger.write_sync(repo_auth.renew_seat, session_id=sa.id, now_ms=T0 + 5_001, lease_ms=LEASE).ok


def test_force_on_a_free_seat_still_audits_with_an_empty_before(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    out = ledger.write_sync(repo_auth.force_seat, session_id=sess.id, now_ms=T0, lease_ms=LEASE)
    assert out.ok and out.changed and out.previous_session_id is None
    (row,) = audit_rows(ledger, action="seat.force")
    assert row.before["holder_session_id"] is None and row.after["holder_session_id"] == sess.id


def test_force_by_the_current_holder_keeps_the_holder_and_refreshes_the_lease(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    acquire(ledger, sess, op, now_ms=T0)
    out = ledger.write_sync(repo_auth.force_seat, session_id=sess.id, now_ms=T0 + 10_000,
                            lease_ms=LEASE)
    assert out.ok and not out.changed and out.state.lease_until_ms == T0 + 10_000 + LEASE


def test_force_by_a_revoked_session_is_refused(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    ledger.write_sync(repo_auth.end_session, sess.id, now_ms=T0, reason="logout")
    with pytest.raises(repo_auth.SessionInactive):
        ledger.write_sync(repo_auth.force_seat, session_id=sess.id, now_ms=T0 + 1, lease_ms=LEASE)
    assert audit_rows(ledger, action="seat.force") == []


def test_expire_clears_only_a_lapsed_lease_and_reports_who_lost_it(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    assert ledger.write_sync(repo_auth.expire_seat, now_ms=T0) is None  # 没人持有
    acquire(ledger, sess, op, now_ms=T0)
    assert ledger.write_sync(repo_auth.expire_seat, now_ms=T0 + LEASE - 1) is None  # 还没到期
    assert ledger.write_sync(repo_auth.expire_seat, now_ms=T0 + LEASE) == sess.id
    assert ledger.write_sync(repo_auth.expire_seat, now_ms=T0 + LEASE + 1) is None  # 已清掉，不重复
    (row,) = audit_rows(ledger, action="seat.expired")
    assert row.operator == "system" and row.before["holder_session_id"] == sess.id


def test_expire_also_clears_a_seat_held_by_a_revoked_session(ledger):
    op = operator(ledger)
    sess = open_session(ledger, op)
    acquire(ledger, sess, op, now_ms=T0)
    ledger.write_sync(repo_auth.end_session, sess.id, now_ms=T0 + 1, reason="idle")
    assert ledger.write_sync(repo_auth.expire_seat, now_ms=T0 + 2) is None  # end_session 已经放掉了
    # 直接把行改回去，模拟「会话被撤销时席位没放掉」的残留
    ledger.write_sync(_force_row, sess.id, T0 + LEASE * 10)
    assert ledger.write_sync(repo_auth.expire_seat, now_ms=T0 + 3) == sess.id


def _force_row(session, session_id, lease_until_ms):
    from dmr_kernel.ledger.models import Seat

    row = session.get(Seat, 1)
    row.session_id, row.lease_until_ms, row.acquired_ms = session_id, lease_until_ms, T0
    session.flush()


async def test_concurrent_acquires_are_serialized_so_exactly_one_wins(ledger):
    a, b, c = operator(ledger, "alice"), operator(ledger, "bob"), operator(ledger, "carol")
    sessions = [(open_session(ledger, a, "a"), a), (open_session(ledger, b, "b"), b), (open_session(ledger, c, "c"), c)]
    results = await asyncio.gather(*[
        ledger.write(repo_auth.acquire_seat, session_id=s.id, now_ms=T0, lease_ms=LEASE)
        for s, o in sessions])
    winners = [r for r in results if r.ok]
    assert len(winners) == 1
    assert all(r.state.holder_session_id == winners[0].state.holder_session_id for r in results)


def test_seat_table_stays_a_single_row(ledger, raw):
    a, b = operator(ledger, "alice"), operator(ledger, "bob")
    sa, sb = open_session(ledger, a, "a"), open_session(ledger, b, "b")
    acquire(ledger, sa, a)
    ledger.write_sync(repo_auth.force_seat, session_id=sb.id, now_ms=T0 + 1, lease_ms=LEASE)
    ledger.write_sync(repo_auth.release_seat, session_id=sb.id, now_ms=T0 + 2)
    assert raw.query("SELECT id FROM seat") == [(1,)]
