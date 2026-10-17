"""会话签名密钥：首次启动随机生成，写进 var/console/session.secret（0600）；之后重启沿用。"""
from __future__ import annotations

import os
import stat

import pytest

from dmr_console.secret import SECRET_MIN_CHARS, SecretError, load_or_create_secret


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_first_start_generates_a_random_secret_with_mode_0600(tmp_path):
    path = tmp_path / "var" / "console" / "session.secret"
    secret = load_or_create_secret(path)
    assert len(secret) >= SECRET_MIN_CHARS
    assert path.read_text(encoding="utf-8").strip() == secret
    assert mode_of(path) == 0o600
    assert mode_of(path.parent) == 0o700


def test_restart_reuses_the_same_secret_so_browser_cookies_stay_valid(tmp_path):
    path = tmp_path / "console" / "session.secret"
    first = load_or_create_secret(path)
    assert load_or_create_secret(path) == first


def test_two_fresh_installs_get_different_secrets(tmp_path):
    a = load_or_create_secret(tmp_path / "a" / "session.secret")
    b = load_or_create_secret(tmp_path / "b" / "session.secret")
    assert a != b


def test_loose_permissions_are_tightened(tmp_path):
    path = tmp_path / "session.secret"
    secret = load_or_create_secret(path)
    os.chmod(path, 0o644)
    assert load_or_create_secret(path) == secret
    assert mode_of(path) == 0o600


def test_truncated_or_empty_secret_is_replaced(tmp_path):
    path = tmp_path / "session.secret"
    path.write_text("short\n", encoding="utf-8")
    os.chmod(path, 0o600)
    secret = load_or_create_secret(path)
    assert secret != "short" and len(secret) >= SECRET_MIN_CHARS
    assert path.read_text(encoding="utf-8").strip() == secret
    assert mode_of(path) == 0o600


def test_stale_temp_file_from_an_interrupted_run_does_not_block_replacement(tmp_path):
    path = tmp_path / "session.secret"
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o600)
    (tmp_path / "session.secret.tmp").write_text("junk", encoding="utf-8")
    secret = load_or_create_secret(path)
    assert path.read_text(encoding="utf-8").strip() == secret
    assert not (tmp_path / "session.secret.tmp").exists()


def test_symlink_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("x" * 64, encoding="utf-8")
    link = tmp_path / "session.secret"
    link.symlink_to(target)
    with pytest.raises(SecretError):
        load_or_create_secret(link)


def test_directory_in_the_way_is_an_error_not_a_crash(tmp_path):
    path = tmp_path / "session.secret"
    path.mkdir()
    with pytest.raises(SecretError):
        load_or_create_secret(path)


def test_secret_text_is_url_safe_ascii(tmp_path):
    secret = load_or_create_secret(tmp_path / "s")
    assert secret.isascii() and secret.strip() == secret and " " not in secret
