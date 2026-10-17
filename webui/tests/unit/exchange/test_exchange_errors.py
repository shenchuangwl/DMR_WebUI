"""exchange.errors：把交易所的失败响应归类成 RejectKind（docs/设计约定.md 7.2 末尾的映射表）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import RejectKind
from dmr_kernel.exchange import errors
from dmr_kernel.exchange.errors import AuthConfigError, classify, parse_error_body

# 400 + 错误码 -> 拒绝类别（设计约定 7.2 末尾）
BUSINESS_CODES = [
    (-2021, RejectKind.IMMEDIATE_TRIGGER),
    (-4130, RejectKind.DUAL_CLOSE_POSITION),
    (-2022, RejectKind.REDUCE_REJECTED),
    (-4164, RejectKind.MIN_NOTIONAL),
    (-2019, RejectKind.INSUFFICIENT_MARGIN),
    (-4116, RejectKind.DUPLICATE_CLIENT_ID),
    (-2011, RejectKind.ORDER_NOT_FOUND),
    (-2013, RejectKind.ORDER_NOT_FOUND),
    (-4045, RejectKind.STOP_LIMIT_REACHED),
    (-1003, RejectKind.RATE_LIMITED),
    (-4047, RejectKind.HAS_POSITION_OR_ORDERS),
    (-4048, RejectKind.HAS_POSITION_OR_ORDERS),
    (-1102, RejectKind.INVALID_PARAMS),
    (-1111, RejectKind.INVALID_PARAMS),
    (-4015, RejectKind.INVALID_PARAMS),
]


@pytest.mark.parametrize("code,kind", BUSINESS_CODES)
def test_http_400_codes_map_to_reject_kinds(code, kind):
    assert classify(400, code, "任意文字") is kind


def test_every_documented_code_is_covered_by_the_table():
    # 设计约定列出的 14 个错误码（-2011/-2013 算两个）都有映射
    assert len({c for c, _ in BUSINESS_CODES}) == 15


def test_unlisted_business_error_is_other_not_unknown():
    # -2010（替身 algo_create_fail 的默认码）没有专门类别：被拒绝，但原因归 OTHER，不是「状态未知」
    assert classify(400, -2010, "NEW_ORDER_REJECTED") is RejectKind.OTHER
    assert classify(400, None, "没有代码的 400") is RejectKind.OTHER
    assert classify(400, -9999, "") is RejectKind.OTHER


@pytest.mark.parametrize("status,code", [
    (503, -1007), (503, None), (500, -1001), (500, None), (502, None), (504, None), (599, -2010),
])
def test_server_side_failures_mean_execution_status_unknown(status, code):
    assert classify(status, code, "Timeout waiting for response from backend server.") is None


@pytest.mark.parametrize("code", [-1007, -1001])
def test_unknown_codes_are_unknown_whatever_the_http_status(code):
    # 交易所偶尔在 400 或 200 里带这两个码：状态仍然未知，不能当成被拒绝
    assert classify(400, code, "x") is None
    assert classify(200, code, "x") is None


@pytest.mark.parametrize("status", [429, 418])
def test_http_429_and_418_are_rate_limited(status):
    assert classify(status, -1003, "Too many requests") is RejectKind.RATE_LIMITED
    assert classify(status, None, "") is RejectKind.RATE_LIMITED


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_raise_config_error_instead_of_returning_a_kind(status):
    with pytest.raises(AuthConfigError):
        classify(status, -2015, "令牌无效")
    with pytest.raises(AuthConfigError):
        classify(status, None, "")


def test_auth_config_error_is_not_a_reject_kind_nor_a_transport_error():
    from dmr_kernel.exchange.transport import TransportError

    assert not issubclass(AuthConfigError, TransportError)
    assert issubclass(AuthConfigError, Exception)


def test_auth_error_message_carries_status_and_server_text():
    with pytest.raises(AuthConfigError) as info:
        classify(401, -2015, "令牌无效：这个接口只认 kernel 令牌")
    text = str(info.value)
    assert "401" in text and "令牌无效" in text


# ---------------------------------------------------------------- 响应体里取代码与文字


def test_parse_error_body_reads_binance_style_json():
    assert parse_error_body({"code": -2013, "msg": "订单不存在"}) == (-2013, "订单不存在")


def test_parse_error_body_tolerates_odd_bodies():
    assert parse_error_body(None) == (None, "")
    assert parse_error_body("<html>Bad Gateway</html>") == (None, "<html>Bad Gateway</html>")
    assert parse_error_body([1, 2]) == (None, "[1, 2]")
    assert parse_error_body({"msg": "只有文字"}) == (None, "只有文字")
    # code 是字符串形式的整数也认；其它形式当作没有
    assert parse_error_body({"code": "-1003", "msg": "x"}) == (-1003, "x")
    assert parse_error_body({"code": "abc", "msg": "x"}) == (None, "x")
    assert parse_error_body({"code": True, "msg": "x"}) == (None, "x")


def test_parse_error_body_truncates_huge_text():
    code, msg = parse_error_body("x" * 5000)
    assert code is None and len(msg) <= 300


def test_module_has_no_ccxt_or_network_imports():
    # 错误归类是纯函数：不依赖传输或 ccxt
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(errors))
    imported = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not imported & {"ccxt", "aiohttp", "requests", "httpx"}


def test_error_text_is_truncated_so_a_whole_html_page_cannot_flood_reasons_and_logs():
    code, text = parse_error_body({"code": -1, "msg": "x" * 5000})
    assert code == -1 and text == "x" * 300
    assert parse_error_body("y" * 5000) == (None, "y" * 300)
    assert parse_error_body(["z"] * 5000)[1] == str(["z"] * 5000)[:300]
