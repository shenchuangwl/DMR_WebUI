"""控制接口的小零件：错误对象与代码表、内核版本号、日志里抹掉令牌的过滤器、应用骨架。"""
from __future__ import annotations

import io
import logging
import tomllib

import pytest

from dmr_kernel.api import errors, logsafe
from dmr_kernel.api.common import CLI_OPERATOR, KERNEL_VERSION, LoginResult, Principal


# ---------------------------------------------------------------- 错误

@pytest.mark.parametrize("error, status, code", [
    (errors.unauthorized(), 401, "unauthorized"),
    (errors.bad_credentials(), 401, "bad_credentials"),
    (errors.rate_limited(30), 429, "rate_limited"),
    (errors.reauth_required(), 403, "reauth_required"),
    (errors.seat_required(), 409, "seat_required"),
    (errors.seat_taken("alice", 5), 409, "seat_taken"),
    (errors.stale_version(), 409, "stale_version"),
    (errors.not_allowed("不行"), 409, "not_allowed"),
    (errors.not_found(), 404, "not_found"),
    (errors.invalid("不对"), 422, "invalid"),
    (errors.not_implemented(), 501, "not_implemented"),
    (errors.unavailable("读不到"), 503, "unavailable"),
    (errors.internal_error(), 500, "internal_error"),
])
def test_error_constructors_use_the_agreed_status_and_code(error, status, code):
    assert (error.status, error.code) == (status, code)
    body = error.body()
    assert body["code"] == code and isinstance(body["message"], str) and body["message"]


def test_error_bodies_carry_their_extra_fields_at_the_top_level():
    assert errors.rate_limited(42).body()["retry_after_s"] == 42
    taken = errors.seat_taken("alice", 1234).body()
    assert taken["holder_operator"] == "alice" and taken["lease_until_ms"] == 1234
    assert set(errors.not_found().body()) == {"code", "message"}


def test_not_implemented_says_which_milestone_by_default():
    assert "M2" in errors.not_implemented().message


# ---------------------------------------------------------------- 主体与版本

def test_the_cli_principal_is_not_a_session():
    cli = Principal.cli()
    assert (cli.kind, cli.operator, cli.is_session) == ("cli", CLI_OPERATOR, False)
    assert cli.session_id is None and cli.reauth_ms is None


def test_a_login_result_never_shows_its_token_in_repr():
    result = LoginResult(token="SECRET-TOKEN-VALUE", operator="alice", session_id=3, idle_timeout_min=30)
    assert "SECRET-TOKEN-VALUE" not in repr(result) and "alice" in repr(result)


def test_the_kernel_version_matches_pyproject(webui_root):
    data = tomllib.loads((webui_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert KERNEL_VERSION == data["project"]["version"]


# ---------------------------------------------------------------- 日志里抹掉令牌

@pytest.mark.parametrize("text, expected", [
    ('WebSocket /api/v1/events?token=abc_DEF-123 [accepted]', 'WebSocket /api/v1/events?token=*** [accepted]'),
    ('GET /api/v1/events?x=1&token=abc&y=2 HTTP/1.1', 'GET /api/v1/events?x=1&token=***&y=2 HTTP/1.1'),
    ('"GET /e?TOKEN=abc HTTP/1.1" 200', '"GET /e?TOKEN=*** HTTP/1.1" 200'),
    ('a token=one and token=two', 'a token=*** and token=***'),
    ('Authorization: Bearer abc.def-ghi_jkl', 'Authorization: Bearer ***'),
    ('authorization: bearer abc', 'authorization: bearer ***'),
    ('POST /login?password=hunter2&u=a', 'POST /login?password=***&u=a'),
    ('no secrets here', 'no secrets here'),
    ('tokens=3 and mytoken=abc and token_count=4', 'tokens=3 and mytoken=abc and token_count=4'),
])
def test_scrub_hides_tokens_and_passwords_but_nothing_else(text, expected):
    assert logsafe.scrub(text) == expected


def test_the_filter_scrubs_the_formatted_message_including_its_arguments():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test_api_common.scrub")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logsafe.install(logger.name)
        logsafe.install(logger.name)  # 重复安装无害
        assert sum(isinstance(f, logsafe.TokenScrubFilter) for f in logger.filters) == 1
        logger.info('%s - "WebSocket %s" [accepted]', "127.0.0.1:5000", "/api/v1/events?token=SECRETTOKEN")
        logger.info("plain message")
    finally:
        logger.removeHandler(handler)
    assert "SECRETTOKEN" not in stream.getvalue()
    assert stream.getvalue().splitlines() == [
        '127.0.0.1:5000 - "WebSocket /api/v1/events?token=***" [accepted]', "plain message"]


def test_the_filter_never_drops_a_record_and_survives_bad_format_arguments():
    flt = logsafe.TokenScrubFilter()
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s %s", ("only-one",), None)
    assert flt.filter(record) is True  # 格式错误的记录也放行（不能因为日志出错丢日志）
