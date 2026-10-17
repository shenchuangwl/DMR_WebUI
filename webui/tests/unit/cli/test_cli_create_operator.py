"""命令行 ``create-operator``：随机生成口令，先以 0600 独占创建口令文件，再凭本机令牌调 ``POST /api/v1/operators``；
失败就删掉口令文件。终端上只打印口令文件的路径，**绝不打印口令**（也不打印令牌）。

默认口令文件 ``<var>/operators/<名>.password``（目录 0700）；``--password-file`` 可另指路径。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from cli_kit import UNREACHABLE, error_response, json_response, transport
from dmr_kernel.cli.client import HttpResponse, KernelUnreachable
from dmr_kernel.cli.main import EXIT_FAILED, EXIT_OK, EXIT_UNAVAILABLE, EXIT_USAGE

CREATED = json_response(201, {"id": 1, "username": "carol", "role": "admin", "created_ms": 1})
URLSAFE = re.compile(r"[A-Za-z0-9_-]+")


def read_password(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n") and text.count("\n") == 1
    return text.strip()


def create(rig, *extra: str, username: str = "carol", fake=None, **kw):
    return rig.run(["create-operator", "--username", username, *extra], fake if fake is not None else transport(CREATED),
                   **kw)


# ====================================================================== 成功


def test_the_password_file_is_created_with_mode_0600_in_a_0700_directory(rig):
    result = create(rig)
    assert result.code == EXIT_OK
    path = rig.password_path("carol")
    assert path.is_file() and rig.mode_of(path) == 0o600
    assert rig.mode_of(rig.operators_dir) == 0o700


def test_the_default_location_is_var_operators_name_dot_password(rig):
    create(rig, username="dave.x-1_y")
    assert (rig.var / "operators" / "dave.x-1_y.password").is_file()


def test_the_password_is_long_random_and_url_safe(rig):
    create(rig)
    password = read_password(rig.password_path("carol"))
    assert len(password) >= 32 and URLSAFE.fullmatch(password)
    assert len(set(password)) >= 12  # 不是 aaaa… 之类


def test_every_run_draws_a_new_password(rig):
    seen = set()
    for index in range(5):
        create(rig, username=f"user{index}")
        seen.add(read_password(rig.password_path(f"user{index}")))
    assert len(seen) == 5


def test_the_request_carries_the_name_and_the_same_password_that_was_saved(rig):
    fake = transport(CREATED)
    create(rig, fake=fake)
    (sent,) = fake.sent
    assert (sent.method, sent.path) == ("POST", "/api/v1/operators")
    assert sent.headers["Authorization"] == f"Bearer {rig.token}"
    assert sent.json() == {"username": "carol", "password": read_password(rig.password_path("carol"))}


def test_the_file_exists_before_the_request_is_made(rig):
    """先落盘后创建：万一内核已创建、答复丢了，口令也不会丢。"""
    seen = {}

    def peek(sent):
        seen["exists"] = rig.password_path("carol").exists()
        seen["mode"] = rig.mode_of(rig.password_path("carol"))
        seen["password"] = rig.password_path("carol").read_text().strip()
        return CREATED

    create(rig, fake=transport(peek))
    assert seen["exists"] and seen["mode"] == 0o600
    assert seen["password"] == read_password(rig.password_path("carol"))


def test_the_terminal_shows_the_path_and_never_the_password_or_the_token(rig):
    result = create(rig)
    password = read_password(rig.password_path("carol"))
    assert str(rig.password_path("carol")) in result.out and "carol" in result.out and "已创建" in result.out
    assert password not in result.text and rig.token not in result.text
    assert result.err == ""


def test_a_custom_password_file_is_used_and_its_missing_directory_made_private(rig, tmp_path):
    target = tmp_path / "secrets" / "nested" / "carol.pw"
    result = create(rig, "--password-file", str(target))
    assert result.code == EXIT_OK and str(target) in result.out
    assert rig.mode_of(target) == 0o600
    assert rig.mode_of(target.parent) == 0o700 and rig.mode_of(target.parent.parent) == 0o700
    assert not rig.operators_dir.exists()  # 没用默认位置


def test_an_existing_directory_of_a_custom_path_keeps_its_mode(rig, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    create(rig, "--password-file", str(shared / "carol.pw"))
    assert rig.mode_of(shared) == 0o755  # 别人的目录不动


def test_a_loose_default_directory_is_tightened(rig):
    rig.operators_dir.mkdir(parents=True, mode=0o755)
    rig.operators_dir.chmod(0o755)
    create(rig)
    assert rig.mode_of(rig.operators_dir) == 0o700


def test_a_strict_umask_cannot_loosen_or_break_the_modes(rig):
    rig.write_token()  # 令牌文件在 umask 收紧之前就绪（测试台自己建目录也受 umask 影响）
    old = os.umask(0o277)
    try:
        result = create(rig)
    finally:
        os.umask(old)
    assert result.code == EXIT_OK
    assert rig.mode_of(rig.password_path("carol")) == 0o600 and rig.mode_of(rig.operators_dir) == 0o700


def test_a_relative_password_file_is_resolved_against_the_working_directory(rig, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = create(rig, "--password-file", "rel.pw")
    assert (tmp_path / "rel.pw").is_file() and str(tmp_path / "rel.pw") in result.out


def test_a_relative_run_directory_still_gives_an_absolute_path_on_the_terminal(rig, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    token = tmp_path / "relvar" / "kernel" / "local.token"
    token.parent.mkdir(parents=True)
    token.write_text(rig.token + "\n")
    result = create(rig, env={"DMR_VAR_DIR": "relvar"})
    expected = tmp_path / "relvar" / "operators" / "carol.password"
    assert result.code == EXIT_OK and expected.is_file() and str(expected) in result.out


# ====================================================================== 不覆盖、不跟随


def test_an_existing_password_file_is_never_overwritten(rig):
    rig.operators_dir.mkdir(parents=True, mode=0o700)
    existing = rig.password_path("carol")
    existing.write_text("old-password\n")
    fake = transport()
    result = create(rig, fake=fake)
    assert result.code == EXIT_FAILED and "已存在" in result.err and str(existing) in result.err
    assert existing.read_text() == "old-password\n"
    assert fake.sent == []  # 没去找内核


def test_a_symlink_in_the_way_is_not_followed(rig, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    rig.operators_dir.mkdir(parents=True, mode=0o700)
    rig.password_path("carol").symlink_to(victim)
    fake = transport()
    result = create(rig, fake=fake)
    assert result.code == EXIT_FAILED and victim.read_text() == "precious" and fake.sent == []


def test_a_dangling_symlink_in_the_way_is_not_followed_either(rig, tmp_path):
    target = tmp_path / "does-not-exist-yet"
    rig.operators_dir.mkdir(parents=True, mode=0o700)
    rig.password_path("carol").symlink_to(target)
    result = create(rig, fake=transport())
    assert result.code == EXIT_FAILED and not target.exists()


def test_a_directory_at_the_password_path_is_refused(rig, tmp_path):
    folder = tmp_path / "is-a-dir"
    folder.mkdir()
    result = create(rig, "--password-file", str(folder), fake=transport())
    assert result.code == EXIT_FAILED and "已存在" in result.err


def test_an_unwritable_location_is_explained_and_nothing_is_sent(rig, tmp_path):
    blocker = tmp_path / "plain-file"
    blocker.write_text("x")
    fake = transport()
    result = create(rig, "--password-file", str(blocker / "carol.pw"), fake=fake)
    assert result.code == EXIT_FAILED and "无法创建口令文件" in result.err and fake.sent == []


# ====================================================================== 失败：删掉口令文件

REFUSALS = [
    error_response(409, "operator_exists", "用户名已存在：carol"),
    error_response(403, "forbidden", "只有本机命令行可以创建操作员"),
    error_response(401, "unauthorized", "需要登录"),
    error_response(422, "invalid", "用户名只能是 1 到 32 位的字母、数字、下划线、点或连字符"),
    error_response(500, "internal_error", "内核内部错误，详情见内核日志"),
    HttpResponse(502, b"<html>bad gateway</html>"),
]


@pytest.mark.parametrize("response", REFUSALS, ids=lambda r: str(r.status))
def test_a_refusal_deletes_the_password_file_and_says_why(rig, response):
    fake = transport(response)
    result = create(rig, fake=fake)
    assert result.code == EXIT_FAILED
    assert not rig.password_path("carol").exists()
    assert rig.operators_dir.is_dir()  # 目录留着
    assert "创建失败" in result.err and "口令文件已删除" in result.err and str(response.status) in result.err
    assert len(fake.sent) == 1
    password = fake.sent[0].json()["password"]
    assert password not in result.text and rig.token not in result.text


def test_a_taken_name_repeats_the_kernels_reason(rig):
    result = create(rig, fake=transport(REFUSALS[0]))
    assert "用户名已存在：carol" in result.err


def test_a_rejected_token_points_at_the_token_file(rig):
    result = create(rig, fake=transport(REFUSALS[2]))
    assert "本机令牌" in result.err and "local.token" in result.err


def test_a_custom_password_file_is_also_deleted_on_failure(rig, tmp_path):
    target = tmp_path / "out" / "carol.pw"
    create(rig, "--password-file", str(target), fake=transport(REFUSALS[0]))
    assert not target.exists()


def test_with_the_kernel_down_the_file_is_deleted_and_the_hint_is_chinese(rig):
    result = create(rig, fake=transport(UNREACHABLE("connect")))
    assert result.code == EXIT_UNAVAILABLE
    assert not rig.password_path("carol").exists()
    assert "无法连接内核" in result.err and "127.0.0.1:18200" in result.err and "应急" in result.err
    assert "T1.7" in result.err and "口令文件已删除" in result.err
    assert result.out == ""


def test_an_uncertain_outcome_keeps_the_file_so_the_password_is_not_lost(rig):
    """请求发出之后没拿到完整的答复：内核可能已经建了这个操作员，口令文件不能删。"""
    fake = transport(KernelUnreachable("io", "等待超时"))
    result = create(rig, fake=fake)
    assert result.code == EXIT_UNAVAILABLE
    path = rig.password_path("carol")
    assert path.exists() and read_password(path) == fake.sent[0].json()["password"]
    assert "不确定" in result.err and "口令文件已保留" in result.err and str(path) in result.err
    assert read_password(path) not in result.text


def test_retrying_after_an_uncertain_outcome_does_not_clobber_the_kept_file(rig):
    create(rig, fake=transport(KernelUnreachable("io", "等待超时")))
    kept = read_password(rig.password_path("carol"))
    result = create(rig, fake=transport())
    assert result.code == EXIT_FAILED and "已存在" in result.err
    assert read_password(rig.password_path("carol")) == kept


def test_a_keyboard_interrupt_during_the_request_keeps_the_file_like_any_uncertain_outcome(rig):
    """Ctrl-C 可能打在请求已经送达之后：此时删掉口令文件，会让一个已存在的用户名永远没人知道口令。"""
    fake = transport(KeyboardInterrupt())
    result = create(rig, fake=fake)
    path = rig.password_path("carol")
    assert result.code == 130 and "已中断" in result.err
    assert path.exists() and read_password(path) == fake.sent[0].json()["password"]
    assert "口令文件已保留" in result.err and str(path) in result.err and "不确定" in result.err
    assert read_password(path) not in result.text and rig.token not in result.text


def test_an_unexpected_error_deletes_the_file_and_propagates(rig):
    with pytest.raises(RuntimeError, match="boom"):
        create(rig, fake=transport(RuntimeError("boom")))
    assert not rig.password_path("carol").exists()


def test_if_the_file_cannot_be_deleted_the_user_is_told_to_do_it(rig, monkeypatch):
    real_unlink = os.unlink

    def refuse(path, *args, **kwargs):
        if str(path).endswith(".password"):
            raise PermissionError("nope")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse)
    result = create(rig, fake=transport(REFUSALS[0]))
    assert result.code == EXIT_FAILED and "无法删除" in result.err and str(rig.password_path("carol")) in result.err
    assert "口令文件已删除" not in result.err


# ====================================================================== 创建之前就拒绝


@pytest.mark.parametrize("username", ["", " ", "a b", "x" * 33, "名字", "a/b", "../x", "tab\tname", "semi;colon"])
def test_a_bad_username_is_a_usage_error_and_creates_nothing(rig, username):
    fake = transport()
    result = create(rig, username=username, fake=fake)
    assert result.code == EXIT_USAGE and "用户名只能是" in result.err
    assert fake.sent == [] and not rig.operators_dir.exists()


def test_the_username_rule_is_the_kernels_rule():
    from dmr_kernel.api.auth import USERNAME_RE
    from dmr_kernel.cli import commands

    assert commands.USERNAME_RE.pattern == USERNAME_RE.pattern


def test_the_generated_password_satisfies_the_kernels_password_rule():
    from dmr_kernel.api.auth import PASSWORD_MAX_LEN, PASSWORD_MIN_LEN
    from dmr_kernel.cli import commands

    password = commands.new_password()
    assert PASSWORD_MIN_LEN <= len(password) <= PASSWORD_MAX_LEN


def test_a_missing_token_stops_before_anything_is_created(rig):
    fake = transport()
    result = create(rig, fake=fake, with_token=False)
    assert result.code == EXIT_UNAVAILABLE and "找不到本机令牌文件" in result.err
    assert fake.sent == [] and not rig.operators_dir.exists()


def test_the_username_option_is_required(rig, capsys):
    result = rig.run(["create-operator"], transport())
    assert result.code == EXIT_USAGE
    assert "--username" in capsys.readouterr().err


def test_a_password_option_is_explained_not_just_rejected(rig, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = rig.run(["create-operator", "--username", "carol", "--password", "hunter2hunter2"], transport())
    shown = capsys.readouterr()
    assert result.code == EXIT_USAGE and "用法错误" in shown.err and "不能在命令行里指定" in shown.err
    assert not rig.operators_dir.exists()
    rig.run(["create-operator", "--username", "carol", "--passphrase", "x"], transport())  # 别的未知选项：不带这条提示
    assert "不能在命令行里指定" not in capsys.readouterr().err
    rig.run(["create-operator", "--username", "carol", "--password-file"], transport())  # 合法选项缺了值：也不带
    assert "不能在命令行里指定" not in capsys.readouterr().err


def test_there_is_no_way_to_pass_the_password_on_the_command_line(rig, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 万一缩写被当成 --password-file，也只会在临时目录里留下文件
    for flag in ("--password", "--passphrase"):
        result = rig.run(["create-operator", "--username", "carol", flag, "hunter2hunter2"], transport())
        assert result.code == EXIT_USAGE
    assert "hunter2hunter2" not in capsys.readouterr().out
    assert list(tmp_path.glob("hunter*")) == []  # --password 不能被当成 --password-file 的缩写
