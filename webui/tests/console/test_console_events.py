"""事件流（WS /events）：帧只是「刷新提示」；断线自行重连（退避），每次连上都全量刷新一次；令牌不进日志。"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import aiohttp
import pytest

from dmr_console.events import (DEFAULT_BACKOFF_S, PARTS, EventStream, aiohttp_connect, build_events_url,
                                refresh_parts_for_frame, stream_factory_for)

TOKEN = "tok-WS-secret-9999"
URL = f"ws://127.0.0.1:18200/api/v1/events?token={TOKEN}"


# ------------------------------------------------------------------ 帧 → 要重新拉取的部分

@pytest.mark.parametrize("frame, parts", [
    ({"type": "state", "version": 3, "at_ms": 1}, {"state"}),
    ({"type": "position", "position_id": 7, "at_ms": 1}, {"state", "positions", "external"}),
    ({"type": "order", "client_order_id": "DK1-1-a-o", "at_ms": 1}, {"state", "positions"}),
    ({"type": "operation", "op_id": "op-1", "status": "done", "at_ms": 1}, {"state", "oplog"}),
    ({"type": "alert", "level": "critical", "kind": "x", "message": "m", "at_ms": 1}, {"state", "alerts"}),
])
def test_each_frame_type_refreshes_the_readonly_parts_it_can_change(frame, parts):
    assert refresh_parts_for_frame(frame) == frozenset(parts)
    assert refresh_parts_for_frame(frame) <= frozenset(PARTS)


def test_ping_refreshes_nothing():
    assert refresh_parts_for_frame({"type": "ping", "at_ms": 1}) == frozenset()


def test_unknown_or_untyped_frames_refresh_everything_rather_than_nothing():
    assert refresh_parts_for_frame({"type": "brand_new"}) == frozenset(PARTS)
    assert refresh_parts_for_frame({"no_type": 1}) == frozenset(PARTS)
    assert refresh_parts_for_frame({"type": 5}) == frozenset(PARTS)


@pytest.mark.parametrize("junk", [None, 5, "state", [], [{"type": "state"}]])
def test_non_object_frames_are_ignored(junk):
    assert refresh_parts_for_frame(junk) == frozenset()


# ------------------------------------------------------------------ 地址

def test_events_url_swaps_the_scheme_and_quotes_the_token():
    assert build_events_url("http://127.0.0.1:18200/api/v1", "abc") == "ws://127.0.0.1:18200/api/v1/events?token=abc"
    assert build_events_url("http://127.0.0.1:18200/api/v1/", "a b/c+d=") == \
           "ws://127.0.0.1:18200/api/v1/events?token=a%20b%2Fc%2Bd%3D"
    assert build_events_url("http://[::1]:18200/api/v1", "t") == "ws://[::1]:18200/api/v1/events?token=t"


@pytest.mark.parametrize("base", ["http://example.com:18200/api/v1", "http://10.0.0.5:18200/api/v1", "https://127.0.0.1:18200/api/v1",
                                  "ftp://127.0.0.1/x", "127.0.0.1:18200"])
def test_events_url_refuses_anything_but_plain_http_on_loopback(base):
    with pytest.raises(ValueError):
        build_events_url(base, "t")


# ------------------------------------------------------------------ 重连循环

class Scripted:
    """连接器：每次连接按脚本给出一批帧（或抛错）；脚本用完就取消，让 run() 结束。"""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.urls: list[str] = []
        self.opened = 0

    def __call__(self, url: str):
        return self._open(url)

    @asynccontextmanager
    async def _open(self, url: str):
        self.urls.append(url)
        if not self.steps:
            raise asyncio.CancelledError
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        self.opened += 1

        async def frames():
            for item in step:
                if isinstance(item, BaseException):
                    raise item
                yield item

        yield frames()


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.delays: list[float] = []

    async def on_frame(self, frame: dict) -> None:
        self.events.append(("frame", frame))

    async def on_connected(self) -> None:
        self.events.append(("connected",))

    async def on_disconnected(self, reason: str) -> None:
        self.events.append(("disconnected", reason))

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)

    def stream(self, connector, url_factory=lambda: URL, **kw) -> EventStream:
        return EventStream(url_factory, connector, on_frame=self.on_frame, on_connected=self.on_connected,
                           on_disconnected=self.on_disconnected, sleep=self.sleep, **kw)

    def frames(self) -> list[dict]:
        return [e[1] for e in self.events if e[0] == "frame"]


def frame(kind: str = "state", **kw) -> str:
    return json.dumps({"type": kind, "at_ms": 1, **kw})


async def test_frames_are_delivered_in_order_and_junk_is_dropped():
    rec = Recorder()
    connector = Scripted([frame("state", version=1), "not json", json.dumps([1, 2]), '"text"', "null", frame("ping"), ""])
    with pytest.raises(asyncio.CancelledError):
        await rec.stream(connector).run()
    assert [f["type"] for f in rec.frames()] == ["state", "ping"]


async def test_on_connected_fires_for_every_successful_connection_before_its_frames():
    rec = Recorder()
    connector = Scripted([frame("state")], [frame("alert")])
    with pytest.raises(asyncio.CancelledError):
        await rec.stream(connector).run()
    kinds = [e[0] if e[0] != "frame" else e[1]["type"] for e in rec.events]
    assert kinds == ["connected", "state", "disconnected", "connected", "alert", "disconnected"]


async def test_a_closed_connection_is_reopened_after_a_short_pause():
    rec = Recorder()
    connector = Scripted([frame("state")], [frame("state")])
    with pytest.raises(asyncio.CancelledError):
        await rec.stream(connector).run()
    assert connector.opened == 2 and rec.delays == [DEFAULT_BACKOFF_S[0], DEFAULT_BACKOFF_S[0]]


async def test_failed_connects_back_off_up_to_a_cap_and_a_delivered_frame_resets_the_backoff():
    rec = Recorder()
    err = lambda: OSError("refused")  # noqa: E731
    connector = Scripted(err(), err(), err(), [frame("state")], err(), err(), err(), err(), err(), err(), err())
    with pytest.raises(asyncio.CancelledError):
        await rec.stream(connector).run()
    b = DEFAULT_BACKOFF_S
    assert rec.delays[:3] == [b[0], b[1], b[2]]
    assert rec.delays[3] == b[0]  # 收到过帧：退避归零
    assert rec.delays[4:] == [b[1], b[2], b[3], b[4], b[4], b[4], b[4]]  # 之后逐级增加，封顶
    assert max(rec.delays) == DEFAULT_BACKOFF_S[-1]


async def test_a_connection_that_dies_without_any_frame_does_not_reset_the_backoff():
    rec = Recorder()
    connector = Scripted([], [], [])
    with pytest.raises(asyncio.CancelledError):
        await rec.stream(connector, backoff_s=(1.0, 2.0, 4.0)).run()
    assert rec.delays == [1.0, 2.0, 4.0]


async def test_disconnect_reason_is_the_error_type_not_its_text():
    rec = Recorder()
    connector = Scripted(OSError(f"cannot reach {URL}"), [frame("state"), ConnectionResetError(f"reset {URL}")])
    with pytest.raises(asyncio.CancelledError):
        await rec.stream(connector).run()
    reasons = [e[1] for e in rec.events if e[0] == "disconnected"]
    assert reasons == ["OSError", "ConnectionResetError"]
    assert all(TOKEN not in r for r in reasons)


async def test_the_token_never_reaches_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    rec = Recorder()
    connector = Scripted(OSError(f"cannot reach {URL}"), aiohttp.ClientError(f"boom {URL}"),
                         [frame("state"), RuntimeError(f"bad {URL}")])
    with pytest.raises(asyncio.CancelledError):
        await rec.stream(connector).run()
    assert TOKEN not in caplog.text
    assert caplog.records  # 确实记了日志，只是不含令牌


async def test_url_is_built_again_for_every_attempt_so_a_new_token_is_picked_up():
    rec = Recorder()
    counter = iter(range(100))
    connector = Scripted(OSError("x"), [], OSError("x"))
    with pytest.raises(asyncio.CancelledError):
        await rec.stream(connector, url_factory=lambda: f"ws://127.0.0.1:1/e?token=t{next(counter)}").run()
    assert connector.urls[:3] == ["ws://127.0.0.1:1/e?token=t0", "ws://127.0.0.1:1/e?token=t1", "ws://127.0.0.1:1/e?token=t2"]


async def test_a_failing_frame_handler_does_not_stop_the_stream():
    calls = []

    async def on_frame(f: dict) -> None:
        calls.append(f["type"])
        if f["type"] == "state":
            raise RuntimeError("handler bug")

    rec = Recorder()
    stream = EventStream(lambda: URL, Scripted([frame("state"), frame("position")]), on_frame=on_frame,
                         on_connected=rec.on_connected, sleep=rec.sleep)
    with pytest.raises(asyncio.CancelledError):
        await stream.run()
    assert calls == ["state", "position"]


async def test_a_failing_connected_handler_does_not_stop_the_stream():
    seen = []

    async def on_connected() -> None:
        raise RuntimeError("refresh bug")

    async def on_frame(f: dict) -> None:
        seen.append(f["type"])

    stream = EventStream(lambda: URL, Scripted([frame("state")]), on_frame=on_frame, on_connected=on_connected,
                         sleep=Recorder().sleep)
    with pytest.raises(asyncio.CancelledError):
        await stream.run()
    assert seen == ["state"]


async def test_cancelling_the_task_ends_the_stream_without_reconnecting():
    gate = asyncio.Event()
    opened = []

    @asynccontextmanager
    async def connector(url: str):
        opened.append(url)

        async def frames():
            await gate.wait()  # 一直等，直到被取消
            yield "never"

        yield frames()

    rec = Recorder()
    task = asyncio.create_task(rec.stream(connector).run())
    for _ in range(50):
        await asyncio.sleep(0)
        if opened:
            break
    assert opened == [URL]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() and len(opened) == 1 and rec.delays == []


# ------------------------------------------------------------------ aiohttp 连接器（用假会话，不连网）

class FakeWS:
    def __init__(self, messages) -> None:
        self._messages = list(messages)
        self.closed_called = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration  # 与 aiohttp 一致：收到关闭帧就结束迭代
        return self._messages.pop(0)

    async def close(self) -> None:
        self.closed_called = True


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, ws: FakeWS | None = None, error: BaseException | None = None, **kwargs) -> None:
        self.ws, self.error, self.kwargs = ws, error, kwargs
        self.connect_url = None
        self.connect_kwargs = {}
        self.closed = False
        FakeSession.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    def ws_connect(self, url, **kwargs):
        self.connect_url, self.connect_kwargs = url, kwargs
        session = self

        class Manager:
            async def __aenter__(self_inner):
                if session.error is not None:
                    raise session.error
                return session.ws

            async def __aexit__(self_inner, *exc):
                await session.ws.close()

        return Manager()


def text(data: str) -> aiohttp.WSMessage:
    return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, data, None)


async def collect(url: str, ws: FakeWS | None, error: BaseException | None = None) -> tuple[list[str], FakeSession]:
    FakeSession.instances.clear()
    out: list[str] = []

    def factory(**kwargs):
        return FakeSession(ws=ws, error=error, **kwargs)

    async with aiohttp_connect(url, session_factory=factory) as frames:
        async for item in frames:
            out.append(item)
    return out, FakeSession.instances[0]


async def test_aiohttp_connector_yields_text_frames_and_skips_binary():
    ws = FakeWS([text("a"), aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, b"x", None), text("b")])
    out, session = await collect(URL, ws)
    assert out == ["a", "b"]
    assert session.connect_url == URL and session.closed and ws.closed_called


async def test_aiohttp_connector_uses_heartbeat_receive_timeout_and_ignores_proxy_environment():
    _, session = await collect(URL, FakeWS([]))
    assert session.kwargs.get("trust_env") is False
    assert session.connect_kwargs.get("heartbeat", 0) > 0
    assert 0 < session.connect_kwargs.get("receive_timeout", 0)
    assert session.connect_kwargs.get("receive_timeout") >= 30  # 服务端每 15 秒一个 ping 帧：至少容忍丢两个


async def test_aiohttp_connector_turns_an_error_message_into_an_exception():
    ws = FakeWS([text("a"), aiohttp.WSMessage(aiohttp.WSMsgType.ERROR, RuntimeError("x"), None)])
    FakeSession.instances.clear()
    out = []
    with pytest.raises(ConnectionError):
        async with aiohttp_connect(URL, session_factory=lambda **kw: FakeSession(ws=ws, **kw)) as frames:
            async for item in frames:
                out.append(item)
    assert out == ["a"] and FakeSession.instances[0].closed


async def test_aiohttp_connector_propagates_connect_failures_and_still_closes_the_session():
    FakeSession.instances.clear()
    with pytest.raises(OSError):
        async with aiohttp_connect(URL, session_factory=lambda **kw: FakeSession(error=OSError("refused"), **kw)):
            raise AssertionError("不应连上")
    assert FakeSession.instances[0].closed


async def test_stream_factory_wires_the_controller_handlers_and_builds_the_url_from_the_current_token():
    from types import SimpleNamespace

    got: list = []

    async def handle_frame(f: dict) -> None:
        got.append(("frame", f["type"]))

    async def handle_connected() -> None:
        got.append(("connected",))

    controller = SimpleNamespace(session=SimpleNamespace(token="old-token"), handle_frame=handle_frame,
                                 handle_connected=handle_connected)
    connector = Scripted([frame("state")], [])
    stream = stream_factory_for("http://127.0.0.1:18200/api/v1", connect=connector, sleep=Recorder().sleep)(controller)
    controller.session.token = "new-token"  # 令牌在建流之后换了：连接时用的是最新的
    with pytest.raises(asyncio.CancelledError):
        await stream.run()
    assert got[:2] == [("connected",), ("frame", "state")]
    assert connector.urls[0] == "ws://127.0.0.1:18200/api/v1/events?token=new-token"
