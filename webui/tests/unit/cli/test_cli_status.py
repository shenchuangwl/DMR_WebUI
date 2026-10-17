"""命令行 ``status``：凭本机令牌调 ``GET /api/v1/state``，把要点用中文打印出来。

内核地址取 settings（默认 127.0.0.1:18200）、令牌取 ``<var>/kernel/local.token``；内核不可用时给出中文提示
（内核不可用时的应急平仓路径在 M2 的 T1.7 做）。终端上永远不出现令牌。
"""
from __future__ import annotations

from datetime import timezone
from io import StringIO

import pytest

from cli_kit import UNREACHABLE, error_response, json_response, transport
from dmr_kernel.cli.client import HttpResponse, KernelUnreachable
from dmr_kernel.cli.main import EXIT_FAILED, EXIT_OK, EXIT_UNAVAILABLE, EXIT_USAGE

NOW = 1_760_000_000_000
STATE = {
    "env": "standin", "standin": True, "time_ms": NOW, "heartbeat_ms": NOW - 3_000, "kernel_version": "0.1.0",
    "model": {"active": "m1", "gate": "stopped", "version": 4, "suppressions": [], "op_locks": [],
              "run_mode": {"code": "normal", "label": "正常"}},
    "account": {"known": True, "equity": "1000", "available": "900", "wallet": "950", "at_ms": NOW},
    "counts": {"managed": 2, "unprotected": 0, "taking_over": 0, "closing": 0, "pending": 0, "external": 1},
    "seat": {"holder_session_id": None, "holder_operator": None, "lease_until_ms": None, "mine": False},
    "me": {"operator": "cli", "principal": "cli", "reauth_valid_until_ms": None},
}


def test_status_prints_the_key_points_in_chinese(rig):
    fake = transport(json_response(200, STATE))
    result = rig.run(["status"], fake)
    assert result.code == EXIT_OK == 0
    for fragment in ("替身环境 · 非真实交易", "生效模型：模型 1", "已停止开仓", "托管中 2", "权益：1000", "当前身份：cli"):
        assert fragment in result.out
    assert result.err == ""


def test_status_asks_for_state_with_the_local_token_and_nothing_else(rig):
    fake = transport(json_response(200, STATE))
    rig.run(["status"], fake)
    (sent,) = fake.sent
    assert (sent.method, sent.path, sent.body) == ("GET", "/api/v1/state", None)
    assert sent.headers["Authorization"] == f"Bearer {rig.token}"


def test_the_token_file_is_read_without_its_newline_and_whitespace(rig):
    rig.write_token(raw=f"  {rig.token} \n\n")
    fake = transport(json_response(200, STATE))
    rig.run(["status"], fake)
    assert fake.sent[0].headers["Authorization"] == f"Bearer {rig.token}"


def test_the_kernel_address_comes_from_the_settings(rig):
    fake = transport(json_response(200, STATE))
    rig.run(["status"], fake, env={"DMR_KERNEL_PORT": "19999", "DMR_KERNEL_HOST": "127.0.0.2"})
    settings = rig.settings_seen[0]
    assert (settings.kernel_host, settings.kernel_port) == ("127.0.0.2", 19999)
    assert settings.local_token_path == rig.token_path


def test_without_an_injected_transport_the_http_transport_is_aimed_at_the_settings_address(rig, monkeypatch):
    """不注入传输时用真实的 HttpTransport，地址取 settings；这里换掉它的类，不开套接字。"""
    import dmr_kernel.cli.main as main_module

    fake = transport(json_response(200, STATE))
    aimed = []
    monkeypatch.setattr(main_module, "HttpTransport", lambda host, port: aimed.append((host, port)) or fake)
    rig.write_token()
    out, err = StringIO(), StringIO()
    code = main_module.main(["status"], env={**rig.env, "DMR_KERNEL_PORT": "19999"}, stdout=out, stderr=err,
                            tz=timezone.utc)
    assert code == EXIT_OK and aimed == [("127.0.0.1", 19999)] and "内核状态" in out.getvalue()
    assert fake.sent[0].headers["Authorization"] == f"Bearer {rig.token}"


def test_status_never_prints_the_token(rig):
    for step in (json_response(200, STATE), error_response(401, "unauthorized", "需要登录"),
                 KernelUnreachable("connect", "连接被拒绝"), HttpResponse(500, b"boom")):
        result = rig.run(["status"], transport(step))
        assert rig.token not in result.text


def test_status_with_the_kernel_down_gives_a_chinese_hint(rig):
    result = rig.run(["status"], transport(UNREACHABLE("connect")))
    assert result.code == EXIT_UNAVAILABLE == 3
    assert result.out == ""
    assert "无法连接内核" in result.err and "127.0.0.1:18200" in result.err and "连接被拒绝" in result.err
    assert "应急" in result.err and "T1.7" in result.err  # 应急路径还没做：告诉操作者现在能怎么办
    assert "Binance" in result.err


def test_status_when_the_kernel_answers_halfway_says_so(rig):
    result = rig.run(["status"], transport(UNREACHABLE("io")))
    assert result.code == EXIT_UNAVAILABLE
    assert "没有给出完整的答复" in result.err


def test_status_with_a_rejected_token_explains_where_the_token_comes_from(rig):
    result = rig.run(["status"], transport(error_response(401, "unauthorized", "需要登录")))
    assert result.code == EXIT_FAILED == 1
    assert "本机令牌" in result.err and "local.token" in result.err and "重启" in result.err


@pytest.mark.parametrize("response", [error_response(500, "internal_error", "内核内部错误，详情见内核日志"),
                                      error_response(503, "unavailable", "数据源暂时读不到"),
                                      HttpResponse(502, b"<html>bad gateway</html>")])
def test_status_shows_what_the_kernel_said_when_it_refuses(rig, response):
    result = rig.run(["status"], transport(response))
    assert result.code == EXIT_FAILED
    assert result.out == "" and "内核" in result.err and str(response.status) in result.err


def test_status_with_an_unreadable_state_still_exits_cleanly(rig):
    result = rig.run(["status"], transport(HttpResponse(200, b"this is not json")))
    assert result.code == EXIT_UNAVAILABLE and "没有给出完整的答复" in result.err


def test_status_with_a_state_of_the_wrong_shape_does_not_crash(rig):
    result = rig.run(["status"], transport(json_response(200, {"model": None})))
    assert result.code == EXIT_OK and "未知" in result.out


def test_status_does_not_send_anything_besides_one_get(rig):
    fake = transport(json_response(200, STATE))
    rig.run(["status"], fake)
    assert len(fake.sent) == 1 and not fake.script


# ---------------------------------------------------------------- 本机令牌文件有问题


def test_a_missing_token_file_means_the_kernel_never_ran_here(rig):
    result = rig.run(["status"], transport(), with_token=False)
    assert result.code == EXIT_UNAVAILABLE
    assert "找不到本机令牌文件" in result.err and str(rig.token_path) in result.err
    assert "无法连接内核" not in result.err and rig.settings_seen == []  # 没有令牌就不去连


def test_an_empty_token_file_is_refused(rig):
    rig.write_token(raw="\n")
    result = rig.run(["status"], transport())
    assert result.code == EXIT_FAILED and "本机令牌文件是空的" in result.err


def test_an_unreadable_token_file_is_explained(rig):
    path = rig.write_token()
    path.chmod(0o000)
    try:
        import os

        if os.access(path, os.R_OK):  # 以 root 身份运行时读得了：没法测这一条
            pytest.skip("当前用户无视文件权限")
        result = rig.run(["status"], transport())
        assert result.code == EXIT_FAILED and "无权读取" in result.err and rig.token not in result.text
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("raw", ["abc def\n", "line-one\nline-two\n", "tök-en\n", "abc\x00def\n", "Bearer abc\n"])
def test_a_token_file_that_is_not_one_visible_ascii_word_is_refused_without_echoing_it(rig, raw):
    """放进请求头会出错（甚至把令牌带进错误信息）：读到这样的内容就在发请求之前拒绝。"""
    rig.write_token(raw=raw)
    fake = transport()
    result = rig.run(["status"], fake)
    assert result.code == EXIT_FAILED and "不是合法的令牌" in result.err and str(rig.token_path) in result.err
    assert fake.sent == [] and rig.settings_seen == []
    assert all(word not in result.text for word in raw.split())


def test_a_token_file_that_is_a_directory_is_refused(rig):
    rig.token_path.mkdir(parents=True)
    result = rig.run(["status"], transport(), with_token=False)
    assert result.code == EXIT_FAILED and "本机令牌文件" in result.err


# ---------------------------------------------------------------- 启动配置与用法


def test_by_default_the_command_line_and_the_kernel_look_at_the_same_run_directory():
    """缺省的运行目录（webui/var）两边必须是同一个，否则命令行找不到内核写的本机令牌。"""
    from dmr_kernel.__main__ import WEBUI_ROOT as kernel_root
    from dmr_kernel.cli.main import WEBUI_ROOT as cli_root
    from dmr_kernel.settings import load_settings

    assert cli_root == kernel_root
    assert load_settings({}, webui_root=cli_root).local_token_path == kernel_root / "var" / "kernel" / "local.token"


def test_a_bad_environment_is_a_usage_error_before_any_request(rig):
    result = rig.run(["status"], transport(), env={"DMR_KERNEL_HOST": "10.1.2.3"})
    assert result.code == EXIT_USAGE == 2
    assert "启动配置错误" in result.err and "回环" in result.err


def test_no_command_is_a_usage_error(rig, capsys):
    result = rig.run([], transport())
    assert result.code == EXIT_USAGE


def test_an_unknown_command_is_a_usage_error(rig, capsys):
    result = rig.run(["launch-missiles"], transport())
    assert result.code == EXIT_USAGE
    assert "launch-missiles" in capsys.readouterr().err


def test_help_lists_both_commands_in_chinese(rig, capsys):
    result = rig.run(["--help"], transport())
    assert result.code == EXIT_OK
    shown = capsys.readouterr().out
    assert "status" in shown and "create-operator" in shown and "内核" in shown


def test_keyboard_interrupt_ends_with_130_and_a_short_note(rig):
    result = rig.run(["status"], transport(KeyboardInterrupt()))
    assert result.code == 130 and "已中断" in result.err
