"""exchange.transport：响应类型、错误层次、HttpTransport（用假的 aiohttp 会话，不开任何套接字）。"""
from __future__ import annotations

import asyncio
import dataclasses

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from dmr_kernel.exchange import transport as tp
from dmr_kernel.exchange.transport import (
    HttpTransport,
    Response,
    TransportConnectionError,
    TransportError,
    TransportTimeout,
)

# ---------------------------------------------------------------- 类型与错误层次


def test_response_is_a_frozen_value_object():
    r = Response(200, {"a": 1}, {"X-MBX-USED-WEIGHT-1M": "3"})
    assert (r.status, r.body, r.headers["X-MBX-USED-WEIGHT-1M"]) == (200, {"a": 1}, "3")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.status = 500  # type: ignore[misc]


def test_error_hierarchy_separates_timeout_from_connection_loss():
    assert issubclass(TransportTimeout, TransportError)
    assert issubclass(TransportConnectionError, TransportError)
    assert not issubclass(TransportTimeout, TransportConnectionError)
    assert not issubclass(TransportConnectionError, TransportTimeout)


# ---------------------------------------------------------------- 假的 aiohttp 会话


class FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None, read_error=None, delay=0.0):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self._read_error = read_error
        self._delay = delay

    async def read(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._read_error is not None:
            raise self._read_error
        return self._body


class _Ctx:
    def __init__(self, session, call):
        self._session = session
        self._call = call

    async def __aenter__(self):
        return await self._session._enter(self._call)

    async def __aexit__(self, *exc):
        self._session.exited += 1
        return False


class FakeSession:
    def __init__(self, response=None, *, error=None, hang=False, ws=None, ws_error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.hang = hang
        self.calls: list[dict] = []
        self.ws_calls: list[dict] = []
        self.exited = 0
        self.closed = False
        self.close_calls = 0
        self._ws = ws
        self._ws_error = ws_error

    def request(self, method, url, **kwargs):
        call = {"method": method, "url": url, **kwargs}
        self.calls.append(call)
        return _Ctx(self, call)

    async def _enter(self, call):
        if self.hang:
            await asyncio.sleep(3600)
        if self.error is not None:
            raise self.error
        return self.response

    async def ws_connect(self, url, **kwargs):
        self.ws_calls.append({"url": url, **kwargs})
        if self.hang:
            await asyncio.sleep(3600)
        if self._ws_error is not None:
            raise self._ws_error
        return self._ws

    async def close(self):
        self.closed = True
        self.close_calls += 1


def make_transport(session, *, token="tok", base_url="http://127.0.0.1:18220"):
    return HttpTransport(base_url, token=token, session_factory=lambda: session)


# ---------------------------------------------------------------- 请求


async def test_request_sends_the_token_only_when_auth_is_true():
    s = FakeSession(FakeResponse(200, b'{"ok": true}', {"X-MBX-USED-WEIGHT-1M": "7"}))
    t = make_transport(s)
    r = await t.request("GET", "/api/private/account", timeout_s=1.0)
    assert r.status == 200 and r.body == {"ok": True} and r.headers["X-MBX-USED-WEIGHT-1M"] == "7"
    assert s.calls[0]["headers"] == {"X-Standin-Key": "tok"}
    assert s.calls[0]["url"] == "http://127.0.0.1:18220/api/private/account"
    await t.request("GET", "/api/public/time", auth=False, timeout_s=1.0)
    assert "X-Standin-Key" not in (s.calls[1]["headers"] or {})


async def test_request_without_a_token_sends_no_key_header_even_when_auth_is_true():
    s = FakeSession()
    t = make_transport(s, token=None)
    await t.request("GET", "/api/private/account", timeout_s=1.0)
    assert "X-Standin-Key" not in (s.calls[0]["headers"] or {})


async def test_request_passes_method_params_and_json_body():
    s = FakeSession()
    t = make_transport(s)
    await t.request("POST", "/api/private/order", body={"symbol": "BTCUSDT", "qty": "0.01"}, timeout_s=1.0)
    await t.request("GET", "/api/private/trades", params={"symbol": "BTCUSDT", "limit": 5, "end_ms": None},
                    timeout_s=1.0)
    assert s.calls[0]["method"] == "POST" and s.calls[0]["json"] == {"symbol": "BTCUSDT", "qty": "0.01"}
    # 查询参数一律转成字符串，空值不发
    assert dict(s.calls[1]["params"]) == {"symbol": "BTCUSDT", "limit": "5"}
    assert s.calls[1]["json"] is None


async def test_base_url_trailing_slash_is_tolerated():
    s = FakeSession()
    t = make_transport(s, base_url="http://127.0.0.1:18220/")
    await t.request("GET", "/api/public/time", auth=False, timeout_s=1.0)
    assert s.calls[0]["url"] == "http://127.0.0.1:18220/api/public/time"


async def test_every_request_carries_an_aiohttp_total_timeout_and_an_outer_one():
    s = FakeSession()
    t = make_transport(s)
    await t.request("GET", "/api/public/time", auth=False, timeout_s=2.5)
    timeout = s.calls[0]["timeout"]
    assert isinstance(timeout, aiohttp.ClientTimeout) and timeout.total == 2.5


async def test_outer_wait_for_turns_a_stuck_request_into_transport_timeout():
    s = FakeSession(hang=True)
    t = make_transport(s)
    with pytest.raises(TransportTimeout):
        await t.request("GET", "/api/private/positions", timeout_s=0.05)


async def test_slow_body_read_also_times_out():
    s = FakeSession(FakeResponse(200, b"{}", delay=5))
    with pytest.raises(TransportTimeout):
        await make_transport(s).request("GET", "/api/private/account", timeout_s=0.05)


async def test_aiohttp_timeouts_map_to_transport_timeout_not_connection_error():
    # ServerTimeoutError 同时是 ClientError 与 TimeoutError：必须按超时处理
    for exc in (aiohttp.ServerTimeoutError("x"), aiohttp.ConnectionTimeoutError("x"), asyncio.TimeoutError()):
        with pytest.raises(TransportTimeout):
            await make_transport(FakeSession(error=exc)).request("GET", "/x", timeout_s=1.0)


async def test_connection_problems_map_to_transport_connection_error():
    errors = [
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientPayloadError("Response payload is not completed"),
        aiohttp.ClientConnectionError("boom"),
        ConnectionResetError("reset"),
        OSError("network down"),
    ]
    for exc in errors:
        with pytest.raises(TransportConnectionError):
            await make_transport(FakeSession(error=exc)).request("GET", "/x", timeout_s=1.0)
    # 响应体读到一半断开（替身 order_timeout=drop 在真实 HTTP 下的表现）
    s = FakeSession(FakeResponse(200, b"", read_error=aiohttp.ClientPayloadError("not completed")))
    with pytest.raises(TransportConnectionError):
        await make_transport(s).request("POST", "/api/private/order", body={}, timeout_s=1.0)


async def test_cancellation_is_not_swallowed():
    s = FakeSession(hang=True)
    task = asyncio.ensure_future(make_transport(s).request("GET", "/x", timeout_s=30))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_error_status_codes_are_returned_not_raised():
    s = FakeSession(FakeResponse(400, b'{"code": -2013, "msg": "x"}'))
    r = await make_transport(s).request("GET", "/api/private/order", timeout_s=1.0)
    assert r.status == 400 and r.body == {"code": -2013, "msg": "x"}


async def test_non_json_and_empty_bodies():
    r = await make_transport(FakeSession(FakeResponse(502, b"<html>Bad Gateway</html>"))).request("GET", "/x", timeout_s=1)
    assert r.status == 502 and r.body == "<html>Bad Gateway</html>"
    r = await make_transport(FakeSession(FakeResponse(200, b""))).request("GET", "/x", timeout_s=1)
    assert r.body is None
    r = await make_transport(FakeSession(FakeResponse(200, '[1,"中文"]'.encode()))).request("GET", "/x", timeout_s=1)
    assert r.body == [1, "中文"]


async def test_the_session_is_created_lazily_and_reused():
    made = []

    def factory():
        s = FakeSession()
        made.append(s)
        return s

    t = HttpTransport("http://127.0.0.1:18220", token="t", session_factory=factory)
    assert made == []
    await t.request("GET", "/a", timeout_s=1)
    await t.request("GET", "/b", timeout_s=1)
    assert len(made) == 1 and len(made[0].calls) == 2


async def test_close_closes_the_session_and_is_idempotent():
    s = FakeSession()
    t = make_transport(s)
    await t.close()  # 还没有会话也不出错
    await t.request("GET", "/a", timeout_s=1)
    await t.close()
    await t.close()
    assert s.closed and s.close_calls == 1  # 重复关闭不会再碰底层会话
    await t.request("GET", "/a", timeout_s=1)  # 关闭之后再用：重新建会话（这里工厂返回同一个假会话）
    assert len(s.calls) == 2


# ---------------------------------------------------------------- WebSocket


class FakeWs:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.closed = False
        self.receive_timeouts: list = []

    async def receive(self, timeout=None):
        self.receive_timeouts.append(timeout)
        if not self.messages:
            # 与真实的 aiohttp 一致：给了时限就在时限到时抛 TimeoutError
            if timeout is None:
                await asyncio.sleep(3600)
            await asyncio.sleep(timeout)
            raise asyncio.TimeoutError()
        item = self.messages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True


class Msg:
    def __init__(self, type_, data=None):
        self.type = type_
        self.data = data


async def test_ws_connect_builds_ws_url_and_returns_a_connection():
    ws = FakeWs([Msg(aiohttp.WSMsgType.TEXT, '{"type":"heartbeat"}')])
    s = FakeSession(ws=ws)
    t = make_transport(s)
    conn = await t.ws_connect("/ws/stream", params={"listen_key": "abc"}, timeout_s=1.0)
    assert s.ws_calls[0]["url"] == "ws://127.0.0.1:18220/ws/stream"
    assert dict(s.ws_calls[0]["params"]) == {"listen_key": "abc"}
    assert await conn.receive(5.0) == '{"type":"heartbeat"}'


async def test_ws_connect_uses_wss_for_https():
    s = FakeSession(ws=FakeWs())
    await make_transport(s, base_url="https://example.invalid:443").ws_connect("/ws/stream", params={}, timeout_s=1)
    assert s.ws_calls[0]["url"].startswith("wss://")


async def test_ws_connect_failures_map_to_transport_errors():
    # 握手被拒绝（替身在握手阶段以 1008 关闭 -> HTTP 层面是握手错误）
    url = URL("http://127.0.0.1:18220/ws/stream")
    info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    refused = aiohttp.WSServerHandshakeError(info, (), status=403, message="Invalid response status")
    with pytest.raises(TransportConnectionError):
        await make_transport(FakeSession(ws_error=refused)).ws_connect("/ws/stream", params={}, timeout_s=1)
    with pytest.raises(TransportConnectionError):
        await make_transport(FakeSession(ws_error=OSError("down"))).ws_connect("/ws/stream", params={}, timeout_s=1)
    with pytest.raises(TransportTimeout):
        await make_transport(FakeSession(hang=True)).ws_connect("/ws/stream", params={}, timeout_s=0.05)


async def test_ws_connect_error_text_never_contains_the_listen_key():
    # aiohttp 的握手错误文字带完整网址（含 ?listen_key=...）；listenKey 是凭据，不能出现在我们抛出的错误文字里
    url = URL("http://127.0.0.1:18220/ws/stream?listen_key=SECRETKEY123&x=1")
    info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    refused = aiohttp.WSServerHandshakeError(info, (), status=403, message="Invalid response status")
    assert "SECRETKEY123" in str(refused)  # 前提：原始错误确实带着它
    with pytest.raises(TransportConnectionError) as caught:
        await make_transport(FakeSession(ws_error=refused)).ws_connect("/ws/stream", params={"listen_key": "SECRETKEY123"},
                                                                        timeout_s=1)
    assert "SECRETKEY123" not in str(caught.value)
    assert "403" in str(caught.value)  # 其余信息保留
    with pytest.raises(TransportConnectionError) as caught:
        await make_transport(FakeSession(ws_error=OSError("bad url ws://h/ws/stream?listen_key=SECRETKEY123"))).ws_connect(
            "/ws/stream", params={"listen_key": "SECRETKEY123"}, timeout_s=1)
    assert "SECRETKEY123" not in str(caught.value)


async def test_ws_receive_text_close_and_errors():
    ws = FakeWs([
        Msg(aiohttp.WSMsgType.PONG), Msg(aiohttp.WSMsgType.BINARY, b"x"),  # 忽略
        Msg(aiohttp.WSMsgType.TEXT, "hello"),
        Msg(aiohttp.WSMsgType.CLOSE), Msg(aiohttp.WSMsgType.CLOSED),
    ])
    conn = await make_transport(FakeSession(ws=ws)).ws_connect("/ws/stream", params={}, timeout_s=1)
    assert await conn.receive(None) == "hello"
    assert await conn.receive(None) is None  # 对端关闭
    assert await conn.receive(None) is None


async def test_ws_receive_timeout_and_transport_failure():
    ws = FakeWs()
    conn = await make_transport(FakeSession(ws=ws)).ws_connect("/ws/stream", params={}, timeout_s=1)
    with pytest.raises(TransportTimeout):
        await conn.receive(0.05)
    ws2 = FakeWs([Msg(aiohttp.WSMsgType.ERROR, RuntimeError("x"))])
    conn2 = await make_transport(FakeSession(ws=ws2)).ws_connect("/ws/stream", params={}, timeout_s=1)
    with pytest.raises(TransportConnectionError):
        await conn2.receive(None)
    ws3 = FakeWs([aiohttp.ClientConnectionError("reset")])
    conn3 = await make_transport(FakeSession(ws=ws3)).ws_connect("/ws/stream", params={}, timeout_s=1)
    with pytest.raises(TransportConnectionError):
        await conn3.receive(None)


async def test_ws_close_closes_the_underlying_socket():
    ws = FakeWs()
    conn = await make_transport(FakeSession(ws=ws)).ws_connect("/ws/stream", params={}, timeout_s=1)
    await conn.close()
    assert ws.closed
    await conn.close()  # 重复关闭不出错


# ---------------------------------------------------------------- 真实的 aiohttp（不连接任何东西）


async def test_real_aiohttp_connection_failure_maps_to_transport_connection_error():
    # 禁网包装下 AF_INET 套接字根本建不出来；不在包装下是回环上没有人监听。两种情况都是「连接失败」。
    t = HttpTransport("http://127.0.0.1:9", token="t")
    try:
        with pytest.raises(TransportConnectionError):
            await t.request("GET", "/api/public/time", auth=False, timeout_s=2.0)
        with pytest.raises(TransportConnectionError):
            await t.ws_connect("/ws/stream", params={"listen_key": "x"}, timeout_s=2.0)
    finally:
        await t.close()


def test_transport_module_exports_the_protocols():
    assert hasattr(tp, "Transport") and hasattr(tp, "WsConnection")
