"""登录服务（api.auth.AuthService）：建操作员、登录、认证、闲置过期、退出、重新校验口令、按用户名限速。

真实的账本与手动时钟；口令哈希用最小的 scrypt 参数。配置用一个字典顶替（session_idle_min、reauth_window_s、seat_lease_s）。
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from api_kit import FAST, PASSWORD, T0, AuthRig, default_config
from dmr_kernel.api import auth as auth_mod
from dmr_kernel.api.common import CLI_OPERATOR
from dmr_kernel.api.errors import ApiError
from dmr_kernel.ledger import repo_audit, repo_auth

MIN = 60_000


def disk_bytes(ledger) -> bytes:
    """账本主文件加上还没并回去的 WAL 文件的全部字节。"""
    data = b""
    for suffix in ("", "-wal"):
        path = Path(str(ledger.path) + suffix)
        if path.exists():
            data += path.read_bytes()
    return data


def audit(ledger, **filters):
    return ledger.read_sync(repo_audit.recent, limit=100, **filters)


async def fails(coro) -> ApiError:
    with pytest.raises(ApiError) as caught:
        await coro
    return caught.value


# ---------------------------------------------------------------- 建操作员


async def test_create_operator_stores_a_verifiable_scrypt_hash_and_audits(rig, ledger):
    rec = await rig.auth.create_operator("alice", PASSWORD, role="admin", created_by="cli")
    assert rec.username == "alice" and rec.role == "admin" and rec.created_ms == T0 and not rec.disabled
    assert rec.password_hash.startswith("scrypt$16$1$1$") and PASSWORD not in rec.password_hash
    assert auth_mod.verify_password(PASSWORD, rec.password_hash)
    (row,) = audit(ledger, action="operator.create")
    assert row.operator == "cli" and row.target == "operator:alice"


@pytest.mark.parametrize("username", ["", "a b", "a/b", "名字", "x" * 33, "alice\n", "a$b"])
async def test_create_operator_rejects_unsafe_usernames(rig, username):
    with pytest.raises(ValueError):
        await rig.auth.create_operator(username, PASSWORD)


async def test_create_operator_enforces_the_password_policy(rig):
    with pytest.raises(ValueError):
        await rig.auth.create_operator("alice", "short")
    with pytest.raises(ValueError):
        await rig.auth.create_operator("alice", "x" * (auth_mod.PASSWORD_MAX_LEN + 1))
    await rig.auth.create_operator("alice", "x" * auth_mod.PASSWORD_MIN_LEN)  # 恰好够长


async def test_create_operator_refuses_duplicates_and_odd_roles(rig):
    await rig.operator("alice")
    with pytest.raises(repo_auth.OperatorExists):
        await rig.operator("alice")
    with pytest.raises(ValueError):
        await rig.auth.create_operator("bob", PASSWORD, role="")
    with pytest.raises(ValueError):
        await rig.auth.create_operator("bob", PASSWORD, role="Not Valid!")


# ---------------------------------------------------------------- 登录


async def test_login_returns_token_operator_session_and_idle_timeout(rig):
    await rig.operator("alice")
    result = await rig.auth.login("alice", PASSWORD, client_info="127.0.0.1 pytest")
    assert len(result.token) >= 43 and result.operator == "alice" and result.session_id >= 1
    assert result.idle_timeout_min == 30


async def test_login_idle_timeout_follows_the_configuration(rig):
    await rig.operator("alice")
    rig.config["session_idle_min"] = 5
    assert (await rig.auth.login("alice", PASSWORD)).idle_timeout_min == 5


async def test_login_stores_only_the_token_hash_never_the_token_or_the_password(rig, ledger, raw):
    await rig.operator("alice")
    result = await rig.auth.login("alice", PASSWORD, client_info="client")
    ((stored,),) = raw.query("SELECT token_hash FROM session")
    assert stored == hashlib.sha256(result.token.encode()).hexdigest() != result.token
    data = disk_bytes(ledger)
    assert result.token.encode() not in data
    assert PASSWORD.encode() not in data
    assert stored.encode() in data


async def test_each_login_is_a_new_session_with_its_own_token(rig, raw):
    await rig.operator("alice")
    first, second = await rig.auth.login("alice", PASSWORD), await rig.auth.login("alice", PASSWORD)
    assert first.token != second.token and first.session_id != second.session_id
    assert raw.query("SELECT COUNT(*) FROM session") == [(2,)]
    assert (await rig.auth.authenticate(first.token)).session_id == first.session_id
    assert (await rig.auth.authenticate(second.token)).session_id == second.session_id


async def test_login_does_not_count_as_a_reauth(rig, raw):
    await rig.operator("alice")
    await rig.auth.login("alice", PASSWORD)
    assert raw.query("SELECT reauth_ms FROM session") == [(None,)]


async def test_wrong_password_and_unknown_user_look_the_same(rig):
    await rig.operator("alice")
    wrong = await fails(rig.auth.login("alice", "not-the-password"))
    unknown = await fails(rig.auth.login("nobody", PASSWORD))
    assert (wrong.status, wrong.code, wrong.message) == (401, "bad_credentials", "用户名或口令错误")
    assert (unknown.status, unknown.code, unknown.message) == (wrong.status, wrong.code, wrong.message)


async def test_username_matching_is_exact(rig):
    await rig.operator("alice")
    assert (await fails(rig.auth.login("Alice", PASSWORD))).code == "bad_credentials"
    assert (await fails(rig.auth.login("alice ", PASSWORD))).code == "bad_credentials"


async def test_disabled_operator_cannot_log_in(rig, raw):
    await rig.operator("alice")
    raw.exec("UPDATE operator SET disabled = 1 WHERE username = 'alice'")
    assert (await fails(rig.auth.login("alice", PASSWORD))).code == "bad_credentials"


async def test_unknown_user_still_pays_for_a_password_verification(rig, monkeypatch):
    calls = []
    real = auth_mod.verify_password

    def counting(password, stored):
        calls.append(stored)
        return real(password, stored)

    monkeypatch.setattr(auth_mod, "verify_password", counting)
    await fails(rig.auth.login("nobody", PASSWORD))
    assert len(calls) == 1  # 用一个假哈希校验一次：耗时与真用户相近，猜不出用户名是否存在


async def test_failed_logins_are_audited_for_existing_operators_only(rig, ledger):
    await rig.operator("alice")
    await fails(rig.auth.login("alice", "wrong-password"))
    await fails(rig.auth.login("nobody", "wrong-password"))
    (row,) = audit(ledger, action="auth.login_failed")
    assert row.operator == "alice" and row.result == "bad_credentials"
    assert "wrong-password" not in repr(row)


async def test_successful_login_is_audited_with_the_session(rig, ledger):
    await rig.operator("alice")
    result = await rig.auth.login("alice", PASSWORD)
    (row,) = audit(ledger, action="auth.login")
    assert row.operator == "alice" and row.target == f"session:{result.session_id}" and row.result == "ok"


# ---------------------------------------------------------------- 限速


async def test_five_failures_lock_the_username_and_the_sixth_attempt_gets_429(rig):
    await rig.operator("alice")
    for _ in range(5):
        assert (await fails(rig.auth.login("alice", "wrong-password"))).code == "bad_credentials"
    locked = await fails(rig.auth.login("alice", "wrong-password"))
    assert (locked.status, locked.code) == (429, "rate_limited")
    assert locked.extra["retry_after_s"] == 60


async def test_a_locked_username_is_refused_even_with_the_right_password_until_the_lock_expires(rig, clock, raw):
    await rig.operator("alice")
    for _ in range(5):
        await fails(rig.auth.login("alice", "wrong-password"))
    assert (await fails(rig.auth.login("alice", PASSWORD))).code == "rate_limited"
    assert raw.query("SELECT COUNT(*) FROM session") == [(0,)]  # 锁期内口令对也不发会话
    clock.advance(30_000)
    assert (await fails(rig.auth.login("alice", PASSWORD))).extra["retry_after_s"] == 30
    clock.advance(30_000)
    assert (await rig.auth.login("alice", PASSWORD)).operator == "alice"


async def test_retry_after_is_rounded_up_to_whole_seconds(rig, clock):
    await rig.operator("alice")
    for _ in range(5):
        await fails(rig.auth.login("alice", "wrong-password"))
    clock.advance(59_500)
    assert (await fails(rig.auth.login("alice", PASSWORD))).extra["retry_after_s"] == 1


async def test_failures_are_counted_per_username(rig):
    await rig.operator("alice")
    await rig.operator("bob")
    for _ in range(5):
        await fails(rig.auth.login("alice", "wrong-password"))
    assert (await fails(rig.auth.login("alice", PASSWORD))).code == "rate_limited"
    assert (await rig.auth.login("bob", PASSWORD)).operator == "bob"


async def test_unknown_usernames_are_limited_too_so_the_limit_reveals_nothing(rig):
    for _ in range(5):
        assert (await fails(rig.auth.login("nobody", "wrong-password"))).code == "bad_credentials"
    assert (await fails(rig.auth.login("nobody", "wrong-password"))).code == "rate_limited"


async def test_success_resets_the_failure_count(rig):
    await rig.operator("alice")
    for _ in range(4):
        await fails(rig.auth.login("alice", "wrong-password"))
    await rig.auth.login("alice", PASSWORD)
    for _ in range(4):
        await fails(rig.auth.login("alice", "wrong-password"))
    assert (await rig.auth.login("alice", PASSWORD)).operator == "alice"  # 第 5 次失败还没出现


async def test_the_locking_failure_is_audited_once_and_locked_attempts_are_not_logged(rig, ledger):
    await rig.operator("alice")
    for _ in range(8):
        await fails(rig.auth.login("alice", "wrong-password"))
    assert len(audit(ledger, action="auth.login_failed")) == 5
    (row,) = audit(ledger, action="auth.login_locked")
    assert row.operator == "alice" and row.after == {"retry_after_s": 60}


async def test_reauth_failures_share_the_login_limit(rig):
    await rig.operator("alice")
    principal = await rig.principal()
    for _ in range(5):
        assert (await fails(rig.auth.reauth(principal, "wrong-password"))).code == "bad_credentials"
    assert (await fails(rig.auth.reauth(principal, PASSWORD))).code == "rate_limited"
    assert (await fails(rig.auth.login("alice", PASSWORD))).code == "rate_limited"


async def test_limiter_can_be_injected(ledger, clock, bus):
    limiter = auth_mod.LoginLimiter(clock, max_failures=2, lock_s=10)
    rig = AuthRig.make(ledger, clock, bus, limiter=limiter)
    await rig.operator("alice")
    await fails(rig.auth.login("alice", "wrong-password"))
    await fails(rig.auth.login("alice", "wrong-password"))
    assert (await fails(rig.auth.login("alice", PASSWORD))).extra["retry_after_s"] == 10


async def test_parallel_wrong_guesses_cannot_outrun_the_lock(rig):
    await rig.operator("alice")
    results = await asyncio.gather(*(rig.auth.login("alice", "wrong-password") for _ in range(20)),
                                   return_exceptions=True)
    codes = sorted(error.code for error in results)
    assert codes == ["bad_credentials"] * 5 + ["rate_limited"] * 15  # 并发的错误猜测也只有 5 次机会


async def test_password_checks_run_in_threads_with_a_bounded_concurrency(rig, monkeypatch):
    import threading
    import time

    for i in range(10):
        await rig.operator(f"user{i}")
    lock, state = threading.Lock(), {"now": 0, "peak": 0}
    real = auth_mod.verify_password

    def slow(password, stored):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        try:
            return real(password, stored)
        finally:
            with lock:
                state["now"] -= 1

    monkeypatch.setattr(auth_mod, "verify_password", slow)
    logins = await asyncio.gather(*(rig.auth.login(f"user{i}", PASSWORD) for i in range(10)))
    assert len(logins) == 10
    assert 2 <= state["peak"] <= auth_mod.MAX_CONCURRENT_HASHES  # 确实并行了，但有上限


async def test_a_short_or_empty_local_token_is_refused_at_construction(ledger, clock, bus):
    for token in ("", "short", "x" * 31):
        with pytest.raises(ValueError):
            auth_mod.AuthService(ledger, clock, bus, config_get=default_config().__getitem__, local_token=token)
    auth_mod.AuthService(ledger, clock, bus, config_get=default_config().__getitem__, local_token="x" * 32)


# ---------------------------------------------------------------- 认证


async def test_the_local_token_authenticates_as_cli_without_touching_the_ledger(rig, raw):
    principal = await rig.auth.authenticate(rig.local_token)
    assert principal.kind == "cli" and principal.operator == CLI_OPERATOR and not principal.is_session
    assert principal.session_id is None and principal.operator_id is None and principal.reauth_ms is None
    assert raw.query("SELECT COUNT(*) FROM session") == [(0,)]


@pytest.mark.parametrize("token", ["", "x", "not-a-token", "A" * 43, "A" * 513, "令牌" * 20, None, 12345])
async def test_unknown_tokens_are_unauthorized(rig, token):
    error = await fails(rig.auth.authenticate(token))
    assert (error.status, error.code) == (401, "unauthorized")


async def test_a_session_token_authenticates_as_the_session(rig):
    operator = await rig.operator("alice", role="admin")
    result = await rig.login()
    principal = await rig.auth.authenticate(result.token)
    assert principal.kind == "session" and principal.is_session
    assert (principal.operator, principal.session_id, principal.operator_id, principal.role) == (
        "alice", result.session_id, operator.id, "admin")
    assert principal.reauth_ms is None


async def test_the_token_hash_is_not_a_valid_token(rig, raw):
    await rig.operator("alice")
    await rig.login()
    ((stored,),) = raw.query("SELECT token_hash FROM session")
    assert (await fails(rig.auth.authenticate(stored))).code == "unauthorized"


async def test_idle_session_expires_just_after_the_configured_minutes(rig, clock):
    await rig.operator("alice")
    token = (await rig.login()).token
    clock.advance(30 * MIN)
    await rig.auth.authenticate(token, touch=False)  # 恰好 30 分钟还有效
    clock.advance(1)
    error = await fails(rig.auth.authenticate(token))
    assert (error.status, error.code) == (401, "unauthorized") and "过期" in error.message


async def test_activity_keeps_the_session_alive(rig, clock):
    await rig.operator("alice")
    token = (await rig.login()).token
    clock.advance(29 * MIN)
    await rig.auth.authenticate(token)
    clock.advance(29 * MIN)
    await rig.auth.authenticate(token)  # 距上次活动 29 分钟，距登录 58 分钟
    clock.advance(30 * MIN + 1)
    assert (await fails(rig.auth.authenticate(token))).code == "unauthorized"


async def test_an_expired_session_stays_dead_even_if_the_timeout_is_raised_later(rig, clock, raw):
    await rig.operator("alice")
    token = (await rig.login()).token
    clock.advance(31 * MIN)
    await fails(rig.auth.authenticate(token))
    assert raw.query("SELECT revoked FROM session") == [(1,)]
    rig.config["session_idle_min"] = 1440
    assert (await fails(rig.auth.authenticate(token))).code == "unauthorized"


async def test_the_timeout_is_read_from_the_configuration_each_time(rig, clock):
    await rig.operator("alice")
    token = (await rig.login()).token
    rig.config["session_idle_min"] = 1
    clock.advance(2 * MIN)
    assert (await fails(rig.auth.authenticate(token))).code == "unauthorized"


async def test_activity_is_persisted_at_most_every_thirty_seconds(rig, clock, raw):
    await rig.operator("alice")
    token = (await rig.login()).token
    clock.advance(10_000)
    await rig.auth.authenticate(token)
    assert raw.query("SELECT last_active_ms FROM session") == [(T0,)]  # 太近，不写库
    clock.advance(20_000)
    await rig.auth.authenticate(token)
    assert raw.query("SELECT last_active_ms FROM session") == [(T0 + 30_000,)]


async def test_unwritten_activity_still_counts_towards_idleness(rig, clock, raw):
    rig.config["session_idle_min"] = 1
    await rig.operator("alice")
    token = (await rig.login()).token
    clock.advance(20_000)
    await rig.auth.authenticate(token)  # 内存里记下活动，库里还是登录时间
    clock.advance(50_000)  # 距登录 70 秒，距上次活动 50 秒
    await rig.auth.authenticate(token)
    clock.advance(61_000)
    assert (await fails(rig.auth.authenticate(token))).code == "unauthorized"


async def test_authenticate_without_touch_leaves_activity_alone(rig, clock, raw):
    await rig.operator("alice")
    token = (await rig.login()).token
    clock.advance(25 * MIN)
    await rig.auth.authenticate(token, touch=False)
    assert raw.query("SELECT last_active_ms FROM session") == [(T0,)]
    clock.advance(6 * MIN)  # 距登录 31 分钟：touch=False 没有续命
    assert (await fails(rig.auth.authenticate(token))).code == "unauthorized"


async def test_idle_expiry_frees_the_seat_the_session_held_and_announces_it(rig, clock, raw):
    rig.config["seat_lease_s"] = 600
    rig.config["session_idle_min"] = 1
    await rig.operator("alice")
    result = await rig.login()
    principal = await rig.auth.authenticate(result.token)
    await rig.seat.acquire(principal)
    clock.advance(2 * MIN)  # 租约还有 8 分钟，但会话已闲置过期
    await fails(rig.auth.authenticate(result.token))
    assert (await rig.seat.view(await rig.principal("bob")))["holder_session_id"] is None
    assert raw.query("SELECT session_id FROM seat") == [(None,)]
    assert [(e.cause, e.holder_session_id) for e in await rig.settle()] == [
        ("acquire", principal.session_id), ("expired", None)]


async def test_a_disabled_operators_existing_session_stops_working(rig, raw):
    await rig.operator("alice")
    token = (await rig.login()).token
    raw.exec("UPDATE operator SET disabled = 1 WHERE username = 'alice'")
    assert (await fails(rig.auth.authenticate(token))).code == "unauthorized"


async def test_reauth_belongs_to_the_session_not_the_operator(rig, clock):
    await rig.operator("alice")
    first, second = await rig.login(), await rig.login()
    principal = await rig.auth.authenticate(first.token)
    clock.advance(1_000)
    await rig.auth.reauth(principal, PASSWORD)
    assert (await rig.auth.authenticate(first.token)).reauth_ms == T0 + 1_000
    assert (await rig.auth.authenticate(second.token)).reauth_ms is None  # 另一个会话不受影响


# ---------------------------------------------------------------- 退出


async def test_logout_revokes_the_session(rig):
    await rig.operator("alice")
    result = await rig.login()
    principal = await rig.auth.authenticate(result.token)
    assert await rig.auth.logout(principal) is False  # 没持有席位
    assert (await fails(rig.auth.authenticate(result.token))).code == "unauthorized"


async def test_logout_audits_the_session_end(rig, ledger):
    await rig.operator("alice")
    principal = await rig.principal()
    await rig.auth.logout(principal)
    (row,) = audit(ledger, action="auth.logout")
    assert row.operator == "alice" and row.after == {"reason": "logout"}


async def test_logout_releases_the_seat_it_holds_and_announces_it(rig):
    principal = await rig.principal()
    await rig.seat.acquire(principal)
    assert await rig.auth.logout(principal) is True
    assert (await rig.seat.view(await rig.principal("bob")))["holder_session_id"] is None
    causes = [(e.cause, e.holder_session_id) for e in await rig.settle()]
    assert causes == [("acquire", principal.session_id), ("logout", None)]


async def test_logging_out_twice_is_harmless(rig):
    principal = await rig.principal()
    await rig.auth.logout(principal)
    assert await rig.auth.logout(principal) is False


async def test_the_cli_token_has_no_session_to_log_out(rig):
    error = await fails(rig.auth.logout(await rig.auth.authenticate(rig.local_token)))
    assert (error.status, error.code) == (409, "not_allowed")


# ---------------------------------------------------------------- 重新校验口令


async def test_reauth_records_the_time_and_returns_the_window_end(rig, clock, raw, ledger):
    await rig.operator("alice")
    result = await rig.login()
    principal = await rig.auth.authenticate(result.token)
    clock.advance(5_000)
    until = await rig.auth.reauth(principal, PASSWORD)
    assert until == T0 + 5_000 + 300_000
    assert raw.query("SELECT reauth_ms FROM session") == [(T0 + 5_000,)]
    fresh = await rig.auth.authenticate(result.token)
    assert fresh.reauth_ms == T0 + 5_000 and rig.auth.reauth_valid_until(fresh) == until
    (row,) = audit(ledger, action="auth.reauth")
    assert row.operator == "alice" and row.after == {"via": "reauth"}


async def test_reauth_window_boundaries_and_configuration(rig, clock):
    await rig.operator("alice")
    result = await rig.login()
    principal = await rig.auth.authenticate(result.token)
    assert rig.auth.reauth_valid_until(principal) is None  # 只登录不算
    await rig.auth.reauth(principal, PASSWORD)
    fresh = await rig.auth.authenticate(result.token)
    until = rig.auth.reauth_valid_until(fresh)
    assert until == T0 + 300_000
    clock.advance(299_999)
    assert rig.auth.reauth_valid_until(fresh) == until
    clock.advance(1)
    assert rig.auth.reauth_valid_until(fresh) is None  # 恰好到点就失效
    rig.config["reauth_window_s"] = 600
    assert rig.auth.reauth_valid_until(fresh) == T0 + 600_000  # 窗口每次都取自配置


async def test_wrong_password_on_reauth_is_refused_and_recorded(rig, raw, ledger):
    principal = await rig.principal()
    error = await fails(rig.auth.reauth(principal, "wrong-password"))
    assert (error.status, error.code) == (401, "bad_credentials")
    assert raw.query("SELECT reauth_ms FROM session") == [(None,)]
    (row,) = audit(ledger, action="auth.reauth_failed")
    assert row.operator == "alice" and row.result == "bad_credentials"


async def test_the_cli_token_cannot_reauth(rig):
    error = await fails(rig.auth.reauth(await rig.auth.authenticate(rig.local_token), PASSWORD))
    assert (error.status, error.code) == (409, "not_allowed")


async def test_require_recent_reauth(rig, clock):
    await rig.operator("alice")
    result = await rig.login()
    principal = await rig.auth.authenticate(result.token)
    error = None
    try:
        rig.auth.require_recent_reauth(principal)
    except ApiError as exc:
        error = exc
    assert error is not None and (error.status, error.code) == (403, "reauth_required")
    await rig.auth.reauth(principal, PASSWORD)
    fresh = await rig.auth.authenticate(result.token)
    rig.auth.require_recent_reauth(fresh)
    clock.advance(300_000)
    with pytest.raises(ApiError) as caught:
        rig.auth.require_recent_reauth(fresh)
    assert caught.value.code == "reauth_required"


async def test_the_cli_principal_never_needs_reauth(rig):
    rig.auth.require_recent_reauth(await rig.auth.authenticate(rig.local_token))


async def test_scrypt_parameters_are_injectable_and_used_for_new_hashes(rig):
    assert FAST.n == 16
    rec = await rig.operator("alice")
    assert rec.password_hash.split("$")[1:4] == ["16", "1", "1"]
