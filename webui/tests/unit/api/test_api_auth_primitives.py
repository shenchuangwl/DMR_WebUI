"""登录的基础零件（dmr_kernel.api.auth）：口令哈希（标准库 scrypt）、会话令牌哈希、本机令牌文件、按用户名的失败限速。

全部是纯函数或只依赖手动时钟，不碰账本；真正的登录流程在 test_api_auth_service.py。
"""
from __future__ import annotations

import hashlib
import os
import stat

import pytest

from dmr_kernel.api import auth
from dmr_kernel.core.clock import ManualClock

FAST = auth.ScryptParams(n=2**4, r=1, p=1)


# ---------------------------------------------------------------- 口令哈希


def test_hash_has_scrypt_format_with_parameters_and_salt():
    stored = auth.hash_password("correct horse", FAST)
    kind, n, r, p, salt, digest = stored.split("$")
    assert (kind, n, r, p) == ("scrypt", "16", "1", "1")
    assert len(bytes.fromhex(salt)) == auth.SALT_BYTES
    assert len(bytes.fromhex(digest)) == auth.DIGEST_BYTES
    assert "correct horse" not in stored


def test_hash_is_salted_so_the_same_password_hashes_differently():
    first, second = auth.hash_password("same password", FAST), auth.hash_password("same password", FAST)
    assert first != second
    assert auth.verify_password("same password", first) and auth.verify_password("same password", second)


def test_hash_is_the_stdlib_scrypt_of_the_normalized_password():
    stored = auth.hash_password("pässword-123", FAST)
    _, n, r, p, salt, digest = stored.split("$")
    expected = hashlib.scrypt("pässword-123".encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r),
                              p=int(p), dklen=len(bytes.fromhex(digest)))
    assert bytes.fromhex(digest) == expected


def test_verify_accepts_the_right_password_and_rejects_everything_else():
    stored = auth.hash_password("right-password", FAST)
    assert auth.verify_password("right-password", stored) is True
    assert auth.verify_password("right-password ", stored) is False
    assert auth.verify_password("Right-password", stored) is False
    assert auth.verify_password("", stored) is False


def test_verify_reads_the_parameters_from_the_stored_string():
    weak = auth.hash_password("pw-for-weak-params", auth.ScryptParams(n=2**4, r=1, p=1))
    other = auth.hash_password("pw-for-other-params", auth.ScryptParams(n=2**5, r=2, p=2))
    assert auth.verify_password("pw-for-weak-params", weak)
    assert auth.verify_password("pw-for-other-params", other)
    assert not auth.verify_password("pw-for-other-params", weak)


def test_password_is_nfkc_normalized_before_hashing():
    stored = auth.hash_password("password123", FAST)
    assert auth.verify_password("ｐａｓｓｗｏｒｄ１２３", stored)  # 全角输入与半角等价
    assert auth.verify_password("口令-ｍｉｘ", auth.hash_password("口令-mix", FAST))


def test_chinese_passwords_work():
    stored = auth.hash_password("中文口令一二三四", FAST)
    assert auth.verify_password("中文口令一二三四", stored)
    assert not auth.verify_password("中文口令一二三五", stored)


@pytest.mark.parametrize("stored", [
    "",
    "plain-text",
    "scrypt",
    "scrypt$16$1$1$00",
    "scrypt$16$1$1$zz$yy",
    "bcrypt$16$1$1$" + "00" * 16 + "$" + "00" * 32,
    "scrypt$abc$1$1$" + "00" * 16 + "$" + "00" * 32,
    "scrypt$15$1$1$" + "00" * 16 + "$" + "00" * 32,        # n 不是 2 的幂
    "scrypt$16$1$1$" + "00" * 16,                          # 少一段
    "scrypt$16$1$1$" + "00" * 16 + "$" + "00" * 32 + "$x",  # 多一段
    "scrypt$%d$8$1$" % (2**30) + "00" * 16 + "$" + "00" * 32,  # n 大到会耗尽内存
    "scrypt$16$1$1$" + "00" * 2 + "$" + "00" * 32,        # 盐太短
    "scrypt$16$1$1$" + "00" * 16 + "$" + "00" * 2,         # 摘要太短
])
def test_verify_rejects_malformed_or_hostile_hash_strings_without_raising(stored):
    assert auth.verify_password("anything", stored) is False


def test_defaults_are_strong_and_work_within_the_openssl_memory_limit():
    params = auth.DEFAULT_SCRYPT
    assert params.n >= 2**15 and params.r >= 8 and params.p >= 1
    stored = auth.hash_password("default-params-password", params)  # 若没设 maxmem，OpenSSL 默认 32MiB 会拒绝
    assert stored.startswith(f"scrypt${params.n}${params.r}${params.p}$")
    assert auth.verify_password("default-params-password", stored)
    assert not auth.verify_password("default-params-passworD", stored)


@pytest.mark.parametrize("bad", ["", "x" * (auth.PASSWORD_MAX_LEN + 1)])
def test_hash_refuses_empty_and_overlong_passwords(bad):
    with pytest.raises(ValueError):
        auth.hash_password(bad, FAST)


def test_verify_returns_false_for_overlong_passwords_instead_of_hashing_them():
    stored = auth.hash_password("short-enough", FAST)
    assert auth.verify_password("x" * (auth.PASSWORD_MAX_LEN + 1), stored) is False


@pytest.mark.parametrize("params", [
    auth.ScryptParams(n=3, r=1, p=1), auth.ScryptParams(n=2**4, r=0, p=1), auth.ScryptParams(n=2**4, r=1, p=0),
    auth.ScryptParams(n=2**4, r=1, p=1, salt_len=4), auth.ScryptParams(n=2**30, r=8, p=1),
])
def test_hash_refuses_invalid_parameters(params):
    with pytest.raises(ValueError):
        auth.hash_password("whatever-password", params)


# ---------------------------------------------------------------- 令牌


def test_session_tokens_are_long_random_and_urlsafe():
    tokens = {auth.new_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        assert len(token) >= 43  # 32 字节随机数的 urlsafe 编码
        assert all(c.isalnum() or c in "-_" for c in token)


def test_token_hash_is_sha256_hex_and_never_the_token():
    token = auth.new_token()
    digest = auth.hash_token(token)
    assert digest == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert len(digest) == 64 and token not in digest
    assert auth.hash_token(token) == digest and auth.hash_token(token + "x") != digest


def test_bearer_token_parsing():
    assert auth.bearer_token("Bearer abc.def") == "abc.def"
    assert auth.bearer_token("bearer   abc") == "abc"
    assert auth.bearer_token("BEARER abc") == "abc"
    for header in (None, "", "Bearer", "Bearer ", "Basic abc", "abc", "Bearer a b"):
        assert auth.bearer_token(header) is None


# ---------------------------------------------------------------- 本机令牌文件


def test_local_token_file_is_0600_and_holds_exactly_the_returned_token(tmp_path):
    path = tmp_path / "var" / "kernel" / "local.token"
    token = auth.write_local_token(path)
    assert path.read_text(encoding="utf-8").strip() == token
    assert len(token) >= 43
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_local_token_parent_directory_is_created_private(tmp_path):
    path = tmp_path / "a" / "b" / "local.token"
    auth.write_local_token(path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_local_token_is_regenerated_on_every_start_and_permissions_are_tightened(tmp_path):
    path = tmp_path / "local.token"
    path.write_text("old-token\n", encoding="utf-8")
    os.chmod(path, 0o644)
    token = auth.write_local_token(path)
    assert token != "old-token" and path.read_text(encoding="utf-8").strip() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.write_local_token(path) != token


def test_local_token_write_replaces_a_symlink_instead_of_following_it(tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("do not touch", encoding="utf-8")
    path = tmp_path / "local.token"
    path.symlink_to(target)
    token = auth.write_local_token(path)
    assert target.read_text(encoding="utf-8") == "do not touch"
    assert not path.is_symlink() and path.read_text(encoding="utf-8").strip() == token


def test_local_token_write_leaves_no_temporary_files(tmp_path):
    auth.write_local_token(tmp_path / "local.token")
    assert [p.name for p in tmp_path.iterdir()] == ["local.token"]


# ---------------------------------------------------------------- 失败限速


def make_limiter(**kwargs):
    clock = ManualClock(1_000_000)
    return clock, auth.LoginLimiter(clock, **kwargs)


def test_limiter_lets_attempts_through_until_the_failure_limit_is_reached():
    _, limiter = make_limiter(max_failures=3, lock_s=60)
    assert limiter.retry_after_ms("alice") == 0
    assert limiter.record_failure("alice") == 0
    assert limiter.record_failure("alice") == 0
    assert limiter.retry_after_ms("alice") == 0


def test_limiter_locks_on_the_failure_that_reaches_the_limit_and_reports_the_wait():
    clock, limiter = make_limiter(max_failures=3, lock_s=60)
    limiter.record_failure("alice")
    limiter.record_failure("alice")
    assert limiter.record_failure("alice") == 60_000
    assert limiter.retry_after_ms("alice") == 60_000
    clock.advance(20_000)
    assert limiter.retry_after_ms("alice") == 40_000


def test_limiter_unlocks_after_the_lock_period():
    clock, limiter = make_limiter(max_failures=2, lock_s=60)
    limiter.record_failure("alice")
    limiter.record_failure("alice")
    clock.advance(59_999)
    assert limiter.retry_after_ms("alice") == 1
    clock.advance(1)
    assert limiter.retry_after_ms("alice") == 0


def test_after_a_lock_expires_one_more_failure_locks_again():
    clock, limiter = make_limiter(max_failures=2, lock_s=60)
    limiter.record_failure("alice")
    limiter.record_failure("alice")
    clock.advance(60_000)
    assert limiter.record_failure("alice") == 60_000


def test_asking_while_locked_does_not_extend_the_lock():
    clock, limiter = make_limiter(max_failures=1, lock_s=60)
    limiter.record_failure("alice")
    for _ in range(5):
        clock.advance(5_000)
        limiter.retry_after_ms("alice")
    assert limiter.retry_after_ms("alice") == 35_000


def test_success_clears_the_failure_count():
    _, limiter = make_limiter(max_failures=3, lock_s=60)
    limiter.record_failure("alice")
    limiter.record_failure("alice")
    limiter.record_success("alice")
    assert limiter.record_failure("alice") == 0
    assert limiter.record_failure("alice") == 0
    assert limiter.record_failure("alice") == 60_000


def test_limits_are_per_username():
    _, limiter = make_limiter(max_failures=2, lock_s=60)
    limiter.record_failure("alice")
    limiter.record_failure("alice")
    assert limiter.retry_after_ms("alice") > 0
    assert limiter.retry_after_ms("bob") == 0
    assert limiter.record_failure("bob") == 0


def test_old_failures_are_forgotten_after_the_window():
    clock, limiter = make_limiter(max_failures=3, lock_s=60, window_s=100)
    limiter.record_failure("alice")
    limiter.record_failure("alice")
    clock.advance(100_001)
    assert limiter.record_failure("alice") == 0  # 只算这一次，不是第三次
    assert limiter.record_failure("alice") == 0
    assert limiter.record_failure("alice") == 60_000


def test_limiter_memory_is_bounded_even_when_every_attempt_uses_a_new_username():
    clock, limiter = make_limiter(max_failures=3, lock_s=60, window_s=100, max_entries=50)
    for i in range(500):
        limiter.record_failure(f"user{i}")
        clock.advance(10)
    assert limiter.size() <= 50


def test_a_locked_username_survives_the_flood_of_other_usernames():
    clock, limiter = make_limiter(max_failures=2, lock_s=600, window_s=100, max_entries=20)
    limiter.record_failure("victim")
    limiter.record_failure("victim")
    for i in range(200):
        limiter.record_failure(f"noise{i}")  # 各自只失败一次，不会被锁
        clock.advance(1)
    assert limiter.size() <= 20
    assert limiter.retry_after_ms("victim") > 0


def test_username_key_is_stripped_but_otherwise_exact():
    _, limiter = make_limiter(max_failures=1, lock_s=60)
    limiter.record_failure(" alice ")
    assert limiter.retry_after_ms("alice") > 0
    assert limiter.retry_after_ms("Alice") == 0


def test_limiter_rejects_nonsense_settings():
    clock = ManualClock(0)
    for kwargs in ({"max_failures": 0}, {"lock_s": 0}, {"window_s": 0}, {"max_entries": 0}):
        with pytest.raises(ValueError):
            auth.LoginLimiter(clock, **kwargs)
