"""命令行（docs/设计约定.md 7.1）：参数默认值；--host 只允许回环地址。不启动任何服务。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from dmr_standin.__main__ import WEBUI_ROOT, parse_args


def test_defaults():
    a = parse_args([])
    assert a.host == "127.0.0.1" and a.port == 18220
    assert a.state_dir == WEBUI_ROOT / "var" / "standin"
    assert a.feed == "auto" and a.equity == Decimal("1000") and a.persist is True
    assert a.replay_dir == WEBUI_ROOT.parent / "bit" / "data"
    assert a.selfcheck_feed is False


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_hosts_are_allowed(host):
    assert parse_args(["--host", host]).host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "10.0.0.1", "example.com", ""])
def test_non_loopback_hosts_are_refused(host, capsys):
    with pytest.raises(SystemExit) as e:
        parse_args(["--host", host])
    assert e.value.code == 2
    assert "回环" in capsys.readouterr().err


def test_options_are_parsed(tmp_path):
    a = parse_args(["--port", "0", "--state-dir", str(tmp_path), "--feed", "replay", "--equity", "250.5",
                    "--no-persist", "--replay-dir", str(tmp_path / "data"), "--selfcheck-feed"])
    assert a.port == 0 and a.state_dir == tmp_path and a.feed == "replay"
    assert a.equity == Decimal("250.5") and a.persist is False and a.replay_dir == tmp_path / "data"
    assert a.selfcheck_feed is True


def test_relative_paths_resolve_against_webui_root():
    a = parse_args(["--state-dir", "var/other"])
    assert a.state_dir == WEBUI_ROOT / "var" / "other"
    assert isinstance(a.state_dir, Path)


@pytest.mark.parametrize("argv", [["--feed", "live"], ["--equity", "-5"], ["--equity", "abc"], ["--port", "70000"]])
def test_invalid_options_are_refused(argv):
    with pytest.raises(SystemExit) as e:
        parse_args(argv)
    assert e.value.code == 2
