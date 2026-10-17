"""cli.client：命令行的 HTTP 客户端。

``KernelClient`` 把「路径 + 载荷」变成带本机令牌的请求，把内核的错误报文变成 ``KernelError``，把连不上、中途出错变成
``KernelUnreachable``（区分「请求肯定没到内核」与「可能已被处理」）。``HttpTransport`` 是标准库 ``http.client`` 的薄封装：
这里用假的连接类测它（测试环境里一个套接字都不开）。
"""
from __future__ import annotations

import http.client
import json

import pytest

from cli_kit import FakeTransport, error_response, json_response, transport
from dmr_kernel.cli import client as client_mod
from dmr_kernel.cli.client import (
    API_PREFIX,
    HttpResponse,
    HttpTransport,
    KernelClient,
    KernelError,
    KernelUnreachable,
)

TOKEN = "T" * 43


# ====================================================================== KernelClient


def test_get_sends_the_bearer_token_to_the_api_prefix_and_parses_json():
    fake = transport(json_response(200, {"env": "standin"}))
    assert KernelClient(fake, TOKEN).get("/state") == {"env": "standin"}
    (sent,) = fake.sent
    assert (sent.method, sent.path, sent.body) == ("GET", "/api/v1/state", None)
    assert API_PREFIX == "/api/v1"
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["Accept"] == "application/json"
    assert "Content-Type" not in sent.headers


def test_post_sends_a_utf8_json_body():
    fake = transport(json_response(201, {"id": 1}))
    result = KernelClient(fake, TOKEN).post("/operators", {"username": "carol", "password": "p w", "note": "中文"})
    assert result == {"id": 1}
    (sent,) = fake.sent
    assert (sent.method, sent.path) == ("POST", "/api/v1/operators")
    assert sent.headers["Content-Type"] == "application/json"
    assert sent.json() == {"username": "carol", "password": "p w", "note": "中文"}
    assert "中文".encode("utf-8") in sent.body  # 中文原样，不转义


def test_a_kernel_error_becomes_kernel_error_with_status_code_and_message():
    fake = transport(error_response(409, "operator_exists", "用户名已存在：carol"))
    with pytest.raises(KernelError) as caught:
        KernelClient(fake, TOKEN).post("/operators", {})
    error = caught.value
    assert (error.status, error.code, error.message) == (409, "operator_exists", "用户名已存在：carol")
    assert "operator_exists" in str(error) and "用户名已存在" in str(error)


@pytest.mark.parametrize("response", [
    HttpResponse(502, b"<html>Bad Gateway</html>"), HttpResponse(500, b""), HttpResponse(500, b"[1, 2]"),
    HttpResponse(400, json.dumps({"code": 5, "message": ["x"]}).encode()),
    HttpResponse(404, json.dumps({"other": "shape"}).encode()), HttpResponse(500, b"\xff\xfe not utf8")])
def test_an_error_without_the_usual_envelope_still_gives_a_usable_kernel_error(response):
    with pytest.raises(KernelError) as caught:
        KernelClient(transport(response), TOKEN).get("/state")
    error = caught.value
    assert error.status == response.status and error.code == "unknown"
    assert str(response.status) in error.message and error.message  # 「内核答复了 HTTP 502」之类


def test_a_huge_kernel_message_is_cut_short():
    fake = transport(error_response(500, "internal_error", "x" * 10_000))
    with pytest.raises(KernelError) as caught:
        KernelClient(fake, TOKEN).get("/state")
    assert len(caught.value.message) <= 600


def test_get_wants_a_json_object_back():
    for body in (b"", b"not json", b"[1]", b'"text"'):
        with pytest.raises(KernelUnreachable) as caught:
            KernelClient(transport(HttpResponse(200, body)), TOKEN).get("/state")
        assert caught.value.kind == "io"


def test_post_accepts_a_success_without_a_readable_body():
    """创建操作员只看状态码：内核已经处理了，答复体读不懂不能算失败。"""
    assert KernelClient(transport(HttpResponse(201, b"")), TOKEN).post("/operators", {}) is None
    assert KernelClient(transport(HttpResponse(200, b"junk")), TOKEN).post("/operators", {}) is None


def test_unreachable_passes_through_from_the_transport():
    boom = KernelUnreachable("connect", "连接被拒绝")
    with pytest.raises(KernelUnreachable) as caught:
        KernelClient(transport(boom), TOKEN).get("/state")
    assert caught.value is boom and caught.value.kind == "connect" and "拒绝" in str(caught.value)


def test_the_token_is_not_part_of_the_clients_repr_or_of_any_error_text():
    client = KernelClient(FakeTransport([error_response(401, "unauthorized", "需要登录")]), TOKEN)
    assert TOKEN not in repr(client) and TOKEN not in str(client)
    with pytest.raises(KernelError) as caught:
        client.get("/state")
    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)


@pytest.mark.parametrize("kind", ["connect", "io"])
def test_unreachable_has_only_the_two_known_kinds(kind):
    assert KernelUnreachable(kind, "x").kind == kind
    with pytest.raises(ValueError):
        KernelUnreachable("whatever", "x")


# ====================================================================== HttpTransport（假的连接类）


class FakeSock:
    def __init__(self):
        self.timeouts: list[float] = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeReply:
    def __init__(self, status: int, body: bytes):
        self.status, self._body = status, body

    def read(self, amt=None):
        return self._body if amt is None else self._body[:amt]


class FakeConnection:
    """替换 http.client.HTTPConnection：按类属性 ``plan`` 决定哪一步出错。"""

    plan: dict = {}
    made: list["FakeConnection"] = []

    def __init__(self, host, port=None, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.closed = False
        self.calls: list = []
        FakeConnection.made.append(self)

    def connect(self):
        self._maybe("connect")
        self.sock = FakeSock()

    def request(self, method, url, body=None, headers=None):
        self.calls.append((method, url, body, dict(headers or {})))
        self._maybe("request")

    def getresponse(self):
        self._maybe("getresponse")
        return FakeReply(*self.plan.get("reply", (200, b"{}")))

    def close(self):
        self.closed = True

    def _maybe(self, step):
        error = self.plan.get(step)
        if error is not None:
            raise error


@pytest.fixture
def fake_http(monkeypatch):
    FakeConnection.plan = {}
    FakeConnection.made = []
    monkeypatch.setattr(client_mod.http.client, "HTTPConnection", FakeConnection)
    return FakeConnection


def send(**kwargs):
    return HttpTransport("127.0.0.1", 18200, **kwargs).request(
        "POST", "/api/v1/operators", headers={"Authorization": "Bearer abc", "Content-Type": "application/json"},
        body=b'{"a": 1}')


def test_the_transport_connects_with_a_short_timeout_then_waits_longer_for_the_answer(fake_http):
    fake_http.plan = {"reply": (201, b'{"id": 1}')}
    response = send(connect_timeout_s=2.5, timeout_s=40.0)
    assert response == HttpResponse(201, b'{"id": 1}')
    (conn,) = fake_http.made
    assert (conn.host, conn.port, conn.timeout) == ("127.0.0.1", 18200, 2.5)
    assert conn.sock.timeouts == [40.0]
    assert conn.calls == [("POST", "/api/v1/operators", b'{"a": 1}',
                           {"Authorization": "Bearer abc", "Content-Type": "application/json"})]
    assert conn.closed


@pytest.mark.parametrize("error, fragment", [
    (ConnectionRefusedError(), "拒绝"), (TimeoutError(), "超时"), (OSError(101, "Network is unreachable"), "unreachable"),
    (OSError(), "OSError")])
def test_a_connection_that_cannot_be_made_is_unreachable_of_kind_connect(fake_http, error, fragment):
    fake_http.plan = {"connect": error}
    with pytest.raises(KernelUnreachable) as caught:
        send()
    assert caught.value.kind == "connect" and fragment in str(caught.value)
    assert fake_http.made[0].closed and not fake_http.made[0].calls  # 一个请求都没发出去


@pytest.mark.parametrize("step", ["request", "getresponse"])
@pytest.mark.parametrize("error, fragment", [
    (TimeoutError(), "超时"), (ConnectionResetError(), "中断"), (BrokenPipeError(), "中断"),
    (http.client.RemoteDisconnected("x"), "中断"), (http.client.BadStatusLine("x"), "HTTP"),
    (http.client.IncompleteRead(b"x"), "HTTP"), (OSError(5, "Input/output error"), "Input/output")])
def test_a_failure_after_connecting_is_unreachable_of_kind_io(fake_http, step, error, fragment):
    fake_http.plan = {step: error}
    with pytest.raises(KernelUnreachable) as caught:
        send()
    assert caught.value.kind == "io" and fragment in str(caught.value)
    assert fake_http.made[0].closed


def test_an_oversized_answer_is_refused_as_io(fake_http):
    fake_http.plan = {"reply": (200, b"x" * (client_mod.MAX_BODY + 10))}
    with pytest.raises(KernelUnreachable) as caught:
        send()
    assert caught.value.kind == "io" and "过大" in str(caught.value)
    assert fake_http.made[0].closed


def test_the_transport_never_puts_the_request_headers_into_error_text(fake_http):
    fake_http.plan = {"getresponse": TimeoutError("Bearer abc secret")}
    with pytest.raises(KernelUnreachable) as caught:
        send()
    assert "abc" not in str(caught.value) and "secret" not in str(caught.value)
