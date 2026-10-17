"""内核客户端：报文解析、错误信封、失联与超时判定（假传输，不联网）。"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from console_kit import GOOD_TOKEN, FakeKernel, envelope, make_alert, make_oplog_item, make_position, make_state
from dmr_console.client import KernelClient, KernelError, KernelUnreachable, LoginResult

BASE = "http://127.0.0.1:18200/api/v1"


def make_client(kernel: FakeKernel, **kw) -> KernelClient:
    return KernelClient(BASE, transport=kernel.transport(), **kw)


# ------------------------------------------------------------------ 正常路径

async def test_health_needs_no_token_and_sends_none():
    kernel = FakeKernel()
    kernel.reply("GET", "/health", {"status": "ok", "env": "standin", "version": "0.1.0", "time_ms": 1})
    client = make_client(kernel)
    assert (await client.health())["env"] == "standin"
    assert "authorization" not in kernel.requests[0].headers


async def test_login_posts_credentials_and_returns_a_result_whose_repr_hides_the_token():
    kernel = FakeKernel()
    kernel.reply("POST", "/auth/login", {"token": GOOD_TOKEN, "operator": "admin", "session_id": "s1", "idle_timeout_min": 30})
    result = await make_client(kernel).login("admin", "pw-admin")
    assert isinstance(result, LoginResult)
    assert (result.token, result.operator, result.session_id, result.idle_timeout_min) == (GOOD_TOKEN, "admin", "s1", 30)
    assert GOOD_TOKEN not in repr(result)
    sent = kernel.requests[0]
    assert sent.method == "POST" and sent.url.path == "/api/v1/auth/login"
    assert json.loads(sent.content) == {"username": "admin", "password": "pw-admin"}
    assert "authorization" not in sent.headers


async def test_login_accepts_the_integer_session_id_the_real_kernel_returns():
    """真实内核的 session_id 是整数（账本里的会话行号）；控制台统一转成字符串保存。"""
    kernel = FakeKernel()
    kernel.reply("POST", "/auth/login", {"token": GOOD_TOKEN, "operator": "admin", "session_id": 2, "idle_timeout_min": 30})
    result = await make_client(kernel).login("admin", "pw-admin")
    assert result.session_id == "2"


@pytest.mark.parametrize("body", [{}, {"token": ""}, {"token": 5, "operator": "a", "session_id": "s"},
                                  {"token": "t", "session_id": "s"}, {"token": "t", "operator": "a", "session_id": True},
                                  {"token": "t", "operator": "a", "session_id": None}, ["x"]])
async def test_login_response_missing_fields_is_a_protocol_error(body):
    kernel = FakeKernel()
    kernel.reply("POST", "/auth/login", body)
    with pytest.raises(KernelUnreachable) as info:
        await make_client(kernel).login("admin", "pw")
    assert info.value.kind == "protocol"


@pytest.mark.parametrize("path,call,body", [
    ("/state", lambda c, t: c.state(t), make_state()),
    ("/auth/me", lambda c, t: c.me(t), {"operator": "admin"}),
    ("/config", lambda c, t: c.config(t), {"items": []}),
    ("/orders", lambda c, t: c.orders(t), {"system": [], "exchange_open": [], "exchange_algo": [], "snapshot_ms": 1}),
])
async def test_authenticated_calls_send_the_bearer_token(path, call, body):
    kernel = FakeKernel()
    kernel.reply("GET", path, body)
    await call(make_client(kernel), GOOD_TOKEN)
    assert kernel.requests[0].headers["authorization"] == f"Bearer {GOOD_TOKEN}"
    assert kernel.requests[0].headers["accept"].startswith("application/json")


async def test_state_returns_the_json_object():
    kernel = FakeKernel()
    kernel.reply("GET", "/state", make_state())
    assert (await make_client(kernel).state(GOOD_TOKEN))["model"]["active"] == "m1"


async def test_positions_unwrap_items_and_external_uses_its_own_path():
    kernel = FakeKernel()
    kernel.reply("GET", "/positions", {"items": [make_position(id=1)]})
    kernel.reply("GET", "/positions/external", {"items": [make_position(id=2, status="pending")]})
    client = make_client(kernel)
    assert [p["id"] for p in await client.positions(GOOD_TOKEN)] == [1]
    assert [p["id"] for p in await client.positions_external(GOOD_TOKEN)] == [2]


async def test_orders_returns_the_whole_object():
    kernel = FakeKernel()
    body = {"system": [], "exchange_open": [], "exchange_algo": [], "snapshot_ms": 7}
    kernel.reply("GET", "/orders", body)
    assert await make_client(kernel).orders(GOOD_TOKEN) == body


async def test_oplog_passes_limit_and_before_id_only_when_given():
    kernel = FakeKernel()
    kernel.reply("GET", "/oplog", {"items": [make_oplog_item(id=9)]})
    client = make_client(kernel)
    await client.oplog(GOOD_TOKEN)
    await client.oplog(GOOD_TOKEN, limit=20, before_id=9)
    first, second = kernel.requests
    assert dict(first.url.params) == {"limit": "100"}
    assert dict(second.url.params) == {"limit": "20", "before_id": "9"}


async def test_alerts_pass_limit():
    kernel = FakeKernel()
    kernel.reply("GET", "/alerts", {"items": [make_alert()]})
    items = await make_client(kernel).alerts(GOOD_TOKEN, limit=5)
    assert items[0]["kind"] == "stream_gap"
    assert dict(kernel.requests[0].url.params) == {"limit": "5"}


async def test_logout_posts_with_the_token():
    kernel = FakeKernel()
    kernel.reply("POST", "/auth/logout", {})
    await make_client(kernel).logout(GOOD_TOKEN)
    assert kernel.requests[0].method == "POST"
    assert kernel.requests[0].headers["authorization"] == f"Bearer {GOOD_TOKEN}"


async def test_logout_of_an_already_expired_session_is_not_an_error():
    kernel = FakeKernel()
    kernel.error("POST", "/auth/logout", 401, "unauthorized")
    await make_client(kernel).logout(GOOD_TOKEN)  # 不抛异常：登出的目的（令牌作废）已经达到
    assert [(r.method, r.url.path) for r in kernel.requests] == [("POST", "/api/v1/auth/logout")]


async def test_logout_while_the_kernel_is_gone_still_reports_unreachable():
    kernel = FakeKernel()
    kernel.raise_on("POST", "/auth/logout", httpx.ConnectError("refused"))
    with pytest.raises(KernelUnreachable):
        await make_client(kernel).logout(GOOD_TOKEN)


# ------------------------------------------------------------------ 内核回了错误信封：内核活着

async def test_error_envelope_becomes_kernel_error_with_code_message_and_extras():
    kernel = FakeKernel()
    kernel.error("POST", "/auth/login", 429, "rate_limited", "尝试过多", retry_after_s=42)
    with pytest.raises(KernelError) as info:
        await make_client(kernel).login("admin", "x")
    err = info.value
    assert (err.status, err.code, err.message) == (429, "rate_limited", "尝试过多")
    assert err.extra == {"retry_after_s": 42}
    assert err.retry_after_s == 42


async def test_bad_credentials_and_unauthorized_are_told_apart():
    kernel = FakeKernel()
    kernel.error("POST", "/auth/login", 401, "bad_credentials", "用户名或口令错误")
    kernel.error("GET", "/state", 401, "unauthorized", "会话已过期")
    client = make_client(kernel)
    with pytest.raises(KernelError) as bad:
        await client.login("admin", "x")
    with pytest.raises(KernelError) as expired:
        await client.state(GOOD_TOKEN)
    assert bad.value.code == "bad_credentials" and not bad.value.is_session_invalid
    assert expired.value.code == "unauthorized" and expired.value.is_session_invalid


@pytest.mark.parametrize("status,code", [(409, "seat_taken"), (501, "not_implemented"), (503, "unavailable"),
                                         (404, "not_found"), (422, "invalid"), (403, "reauth_required")])
async def test_other_documented_codes_surface_unchanged(status, code):
    kernel = FakeKernel()
    kernel.error("GET", "/state", status, code, "中文原因")
    with pytest.raises(KernelError) as info:
        await make_client(kernel).state(GOOD_TOKEN)
    assert (info.value.status, info.value.code, info.value.message) == (status, code, "中文原因")


async def test_a_4xx_without_the_envelope_is_still_a_live_kernel():
    kernel = FakeKernel()
    kernel.reply("GET", "/state", {"detail": "Not Found"}, 404)
    with pytest.raises(KernelError) as info:
        await make_client(kernel).state(GOOD_TOKEN)
    assert info.value.status == 404 and info.value.code == "http_404"


# ------------------------------------------------------------------ 失联：连不上、超时、响应无法解析

@pytest.mark.parametrize("exc,kind", [
    (httpx.ConnectError("refused"), "connect"),
    (httpx.ConnectTimeout("slow"), "timeout"),
    (httpx.ReadTimeout("slow"), "timeout"),
    (httpx.PoolTimeout("busy"), "timeout"),
    (httpx.RemoteProtocolError("closed"), "connect"),
    (httpx.ReadError("reset"), "connect"),
    (httpx.WriteError("broken"), "connect"),
    (ConnectionResetError("reset"), "connect"),
    (OSError("unreachable"), "connect"),
])
async def test_transport_failures_are_unreachable(exc, kind):
    kernel = FakeKernel()
    kernel.raise_on("GET", "/state", exc)
    with pytest.raises(KernelUnreachable) as info:
        await make_client(kernel).state(GOOD_TOKEN)
    assert info.value.kind == kind
    assert info.value.reason


async def test_a_hanging_kernel_times_out_quickly_and_is_unreachable():
    kernel = FakeKernel()
    kernel.hang("GET", "/state")
    client = make_client(kernel, timeout_s=0.05)
    started = time.monotonic()
    with pytest.raises(KernelUnreachable) as info:
        await client.state(GOOD_TOKEN)
    assert info.value.kind == "timeout"
    assert time.monotonic() - started < 2.0


async def test_the_timeout_is_a_total_budget_per_request_not_per_byte():
    kernel = FakeKernel()
    kernel.hang("POST", "/auth/login")
    with pytest.raises(KernelUnreachable) as info:
        await make_client(kernel, timeout_s=0.05).login("admin", "pw")
    assert info.value.kind == "timeout"


@pytest.mark.parametrize("raw", [b"not json", b"", b"<html>proxy</html>", b'{"a":'])
async def test_a_200_with_unparsable_body_is_unreachable_protocol(raw):
    kernel = FakeKernel()
    kernel.reply("GET", "/state", raw=raw)
    with pytest.raises(KernelUnreachable) as info:
        await make_client(kernel).state(GOOD_TOKEN)
    assert info.value.kind == "protocol"


@pytest.mark.parametrize("call", ["state", "positions", "positions_external", "orders", "oplog", "alerts", "config"])
@pytest.mark.parametrize("body", [[1, 2], "text", 5, None])
async def test_wrong_json_shape_is_protocol_error(call, body):
    kernel = FakeKernel()
    for path in ("/state", "/positions", "/positions/external", "/orders", "/oplog", "/alerts", "/config"):
        kernel.reply("GET", path, body)
    with pytest.raises(KernelUnreachable) as info:
        await getattr(make_client(kernel), call)(GOOD_TOKEN)
    assert info.value.kind == "protocol"


@pytest.mark.parametrize("call", ["positions", "positions_external", "oplog", "alerts", "config"])
async def test_list_endpoints_require_items_to_be_a_list(call):
    kernel = FakeKernel()
    for path in ("/positions", "/positions/external", "/oplog", "/alerts", "/config"):
        kernel.reply("GET", path, {"items": "nope"})
    with pytest.raises(KernelUnreachable):
        await getattr(make_client(kernel), call)(GOOD_TOKEN)


@pytest.mark.parametrize("status", [500, 502, 504])
async def test_5xx_without_the_envelope_counts_as_lost(status):
    kernel = FakeKernel()
    kernel.reply("GET", "/state", raw=b"Internal Server Error", status=status)
    with pytest.raises(KernelUnreachable) as info:
        await make_client(kernel).state(GOOD_TOKEN)
    assert info.value.kind == "protocol" and str(status) in info.value.reason


async def test_a_5xx_with_the_envelope_is_a_live_kernel_answering_badly():
    kernel = FakeKernel()
    kernel.error("GET", "/state", 503, "unavailable", "账本暂时读不到")
    with pytest.raises(KernelError) as info:
        await make_client(kernel).state(GOOD_TOKEN)
    assert info.value.code == "unavailable"


async def test_redirects_are_not_followed_and_count_as_a_bad_response():
    kernel = FakeKernel()
    kernel.reply("GET", "/state", raw=b"", status=302, headers={"location": "http://evil.example/"})
    with pytest.raises(KernelUnreachable) as info:
        await make_client(kernel).state(GOOD_TOKEN)
    assert info.value.kind == "protocol"
    assert len(kernel.requests) == 1


# ------------------------------------------------------------------ 安全

async def test_token_and_password_never_appear_in_errors():
    kernel = FakeKernel()
    kernel.raise_on("GET", "/state", httpx.ConnectError("refused"))
    kernel.reply("POST", "/auth/login", raw=b"junk")
    client = make_client(kernel)
    with pytest.raises(KernelUnreachable) as one:
        await client.state(GOOD_TOKEN)
    with pytest.raises(KernelUnreachable) as two:
        await client.login("admin", "pw-secret-zzz")
    for exc in (one.value, two.value):
        text = f"{exc!s} {exc!r} {exc.reason}"
        assert GOOD_TOKEN not in text and "pw-secret-zzz" not in text


def test_client_ignores_proxy_environment_and_does_not_follow_redirects():
    client = KernelClient(BASE)
    assert client.http.trust_env is False
    assert client.http.follow_redirects is False


async def test_aclose_closes_the_http_client():
    client = KernelClient(BASE)
    await client.aclose()
    assert client.http.is_closed


def test_base_url_must_be_loopback():
    with pytest.raises(ValueError, match="回环"):
        KernelClient("http://192.168.1.2:18200/api/v1")
    with pytest.raises(ValueError, match="回环"):
        KernelClient("http://example.com/api/v1")
    with pytest.raises(ValueError):
        KernelClient("https://127.0.0.1:18200/api/v1")


async def test_concurrent_requests_do_not_interfere():
    import asyncio
    kernel = FakeKernel()
    kernel.reply("GET", "/state", make_state())
    kernel.reply("GET", "/positions", {"items": []})
    client = make_client(kernel)
    state, positions = await asyncio.gather(client.state(GOOD_TOKEN), client.positions(GOOD_TOKEN))
    assert state["env"] == "standin" and positions == []


# ------------------------------------------------------------------ 操作类请求

async def test_post_sends_the_token_and_json_body_and_returns_the_envelope():
    kernel = FakeKernel()
    kernel.reply("POST", "/ops/stop", {"op_id": "a", "status": "succeeded", "result": {"gate": "stopped"}})
    out = await make_client(kernel).post(GOOD_TOKEN, "/ops/stop", {"op_id": "a", "model_version": 3})
    assert out["result"] == {"gate": "stopped"}
    sent = kernel.requests[0]
    assert sent.method == "POST" and sent.url.path == "/api/v1/ops/stop" and sent.headers["authorization"] == f"Bearer {GOOD_TOKEN}"
    assert json.loads(sent.content) == {"op_id": "a", "model_version": 3}


async def test_post_turns_a_refusal_into_a_kernel_error_with_the_extra_fields():
    kernel = FakeKernel()
    kernel.error("POST", "/ops/start", 409, "not_allowed", "自检不过", checks=[{"name": "x", "ok": False}])
    with pytest.raises(KernelError) as info:
        await make_client(kernel).post(GOOD_TOKEN, "/ops/start", {"op_id": "a"})
    assert info.value.code == "not_allowed" and info.value.extra["checks"][0]["name"] == "x"


async def test_post_reports_a_lost_kernel_as_unreachable_so_the_caller_can_retry_with_the_same_op_id():
    kernel = FakeKernel()
    kernel.raise_on("POST", "/ops/stop", httpx.ConnectError("boom"))
    with pytest.raises(KernelUnreachable):
        await make_client(kernel).post(GOOD_TOKEN, "/ops/stop", {"op_id": "a"})


async def test_operation_reads_one_operation_record():
    kernel = FakeKernel()
    kernel.reply("GET", "/operations/ca-1", {"op_id": "ca-1", "status": "running", "per_position": None})
    assert (await make_client(kernel).operation(GOOD_TOKEN, "ca-1"))["status"] == "running"


async def test_put_config_sends_the_op_id_and_value_to_the_key_path_with_the_token():
    import json
    kernel = FakeKernel()
    result = {"key": "takeover_symbols", "old": [], "new": ["BTCUSDT"], "applies": "只影响之后新出现的仓", "affects_existing": False}
    kernel.reply("PUT", "/config/takeover_symbols", result)
    got = await make_client(kernel).put_config(GOOD_TOKEN, "takeover_symbols", {"op_id": "c-1", "value": ["BTCUSDT"]})
    assert got == result
    request = kernel.requests[0]
    assert request.method == "PUT" and request.headers["authorization"] == f"Bearer {GOOD_TOKEN}"
    assert json.loads(request.content) == {"op_id": "c-1", "value": ["BTCUSDT"]}


async def test_put_config_refusal_is_a_kernel_error_and_the_key_is_url_quoted():
    kernel = FakeKernel()
    kernel.error("PUT", "/config/a/b", 422, "invalid", "取值冲突")
    with pytest.raises(KernelError) as info:
        await make_client(kernel).put_config(GOOD_TOKEN, "a/b", {"op_id": "c-1", "value": 1})
    assert info.value.code == "invalid" and info.value.message == "取值冲突"
    assert b"%2F" in kernel.requests[0].url.raw_path
