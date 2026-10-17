"""事件流：WS /events?token=<会话令牌>。

三部分：EventHub（事件 -> 刷新提示帧的映射、每个连接一个有界队列、慢消费者被断开）的单元测试；
经 starlette TestClient 的端到端测试（握手鉴权、帧内容、ping、会话失效后被关闭）；
外部取消与连接出错的收尾（TestClient 退出会话、服务器关停时端点必须被外层 anyio 取消范围吸收，连接登记必须撤销）。
报文约定：docs/plans/M1-控制接口只读部分.md 第 4 节。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import anyio
import pytest
from starlette.websockets import WebSocketDisconnect

from api_kit import BASE, T0
from dmr_kernel.api.common import Principal, SeatChanged
from dmr_kernel.api.events import CLOSE, EventHub
from dmr_kernel.core.events import (
    AccountSnapshotTaken,
    AlertRaised,
    CandlesReady,
    ConfigChanged,
    IntentStateChanged,
    ModelStateChanged,
    OperationProgress,
    OrderStateChanged,
    PositionChanged,
    PositionClosed,
    PositionRegistered,
    ProtectionChanged,
    ReconcileCompleted,
    RiskStateChanged,
    StreamStatusChanged,
)
from dmr_kernel.core.types import AlertLevel, PositionSide

LONG = PositionSide.LONG
CLI = Principal.cli()

# (事件, 期望的帧)
MAPPED = [
    (ModelStateChanged(version=7, at_ms=T0 + 1), {"type": "state", "version": 7, "at_ms": T0 + 1}),
    (RiskStateChanged(account_tripped=True, model_tripped=False, reasons=("日亏损",), at_ms=T0 + 2),
     {"type": "state", "version": None, "at_ms": T0 + 2}),
    (StreamStatusChanged(state="interrupted", reason="静默", at_ms=T0 + 3),
     {"type": "state", "version": None, "at_ms": T0 + 3}),
    (ReconcileCompleted(kind="positions", status="ok", at_ms=T0 + 4),
     {"type": "state", "version": None, "at_ms": T0 + 4}),
    (ConfigChanged(key="leverage", at_ms=T0 + 5), {"type": "state", "version": None, "at_ms": T0 + 5}),
    (SeatChanged(holder_session_id=3, cause="acquire", at_ms=T0 + 6),
     {"type": "state", "version": None, "at_ms": T0 + 6}),
    (PositionRegistered(position_id=11, symbol="BTCUSDT", side=LONG, ownership="strategy", status="managed",
                        at_ms=T0 + 7), {"type": "position", "position_id": 11, "at_ms": T0 + 7}),
    (PositionChanged(position_id=12, symbol="BTCUSDT", side=LONG, cause="fill", version=4, at_ms=T0 + 8),
     {"type": "position", "position_id": 12, "at_ms": T0 + 8}),
    (PositionClosed(position_id=13, symbol="BTCUSDT", side=LONG, exit_reason="stop", at_ms=T0 + 9),
     {"type": "position", "position_id": 13, "at_ms": T0 + 9}),
    (ProtectionChanged(position_id=14, state="system", stop_price=Decimal("58000"), at_ms=T0 + 10),
     {"type": "position", "position_id": 14, "at_ms": T0 + 10}),
    (OrderStateChanged(client_order_id="DKA-1-5-o", state="ACKNOWLEDGED", intent_id=5, symbol="BTCUSDT",
                       position_side=LONG, at_ms=T0 + 11),
     {"type": "order", "client_order_id": "DKA-1-5-o", "intent_id": 5, "at_ms": T0 + 11}),
    (OrderStateChanged(client_order_id="DK0-0-1-c", state="PENDING_SEND", intent_id=None, symbol="BTCUSDT",
                       position_side=LONG, at_ms=T0 + 12),
     {"type": "order", "client_order_id": "DK0-0-1-c", "intent_id": None, "at_ms": T0 + 12}),
    (IntentStateChanged(intent_id=6, state="ACTIVE", action="open", symbol="BTCUSDT", side=LONG, at_ms=T0 + 13),
     {"type": "order", "client_order_id": None, "intent_id": 6, "at_ms": T0 + 13}),
    (OperationProgress(op_id="op-9", op_type="close_all", status="running", detail={"done": 1}, at_ms=T0 + 14),
     {"type": "operation", "op_id": "op-9", "status": "running", "at_ms": T0 + 14}),
    (AlertRaised(alert_id=21, level=AlertLevel.CRITICAL, kind="risk", message="风控触发", at_ms=T0 + 15),
     {"type": "alert", "level": "critical", "kind": "risk", "message": "风控触发", "at_ms": T0 + 15}),
]
UNMAPPED = [
    CandlesReady(timeframe="15m", bar_open_ms=T0, symbols=("BTCUSDT",), at_ms=T0),
    AccountSnapshotTaken(equity=Decimal("1"), available=Decimal("1"), wallet=Decimal("1"), at_ms=T0),
]


# ---------------------------------------------------------------- 映射

@pytest.mark.parametrize("event, frame", MAPPED, ids=[type(e).__name__ + str(i) for i, (e, _) in enumerate(MAPPED)])
async def test_each_event_maps_to_the_agreed_frame(bus, clock, event, frame):
    hub = EventHub(bus, clock)
    assert hub.frame_for(event) == frame


@pytest.mark.parametrize("event", UNMAPPED, ids=lambda e: type(e).__name__)
async def test_events_that_are_not_refresh_hints_produce_no_frame(bus, clock, event):
    assert EventHub(bus, clock).frame_for(event) is None


async def test_an_event_without_a_time_gets_the_clock_time(bus, clock):
    event = OperationProgress(op_id="op-1", op_type="start", status="accepted")  # at_ms 默认 0
    assert EventHub(bus, clock).frame_for(event)["at_ms"] == T0


async def test_ping_frame(bus, clock):
    clock.advance(1_234)
    assert EventHub(bus, clock).ping_frame() == {"type": "ping", "at_ms": T0 + 1_234}


# ---------------------------------------------------------------- 连接与队列

async def test_a_connection_receives_frames_in_publication_order(bus, clock):
    hub = EventHub(bus, clock)
    hub.start()
    await bus.start()
    conn = hub.connect(CLI)
    for event, _ in MAPPED[:4]:
        bus.publish(event)
    bus.publish(UNMAPPED[0])  # 不产生帧
    await bus.drain()
    got = [conn.queue.get_nowait() for _ in range(conn.queue.qsize())]
    assert got == [frame for _, frame in MAPPED[:4]]
    await bus.stop()


async def test_every_connection_gets_every_frame_and_a_closed_one_gets_none(bus, clock):
    hub = EventHub(bus, clock)
    hub.start()
    await bus.start()
    first, second = hub.connect(CLI), hub.connect(CLI)
    assert hub.connection_count == 2
    hub.disconnect(first)
    hub.disconnect(first)  # 重复断开无害
    assert hub.connection_count == 1
    bus.publish(MAPPED[0][0])
    await bus.drain()
    assert first.queue.qsize() == 0 and second.queue.qsize() == 1
    await bus.stop()


async def test_a_slow_consumer_is_cut_off_instead_of_piling_up_memory(bus, clock):
    hub = EventHub(bus, clock, max_queue=3)
    hub.start()
    await bus.start()
    slow, healthy = hub.connect(CLI), hub.connect(CLI)
    for event, _ in MAPPED[:3]:
        bus.publish(event)
    await bus.drain()
    while not healthy.queue.empty():
        healthy.queue.get_nowait()  # 健康的连接一直在取
    bus.publish(MAPPED[3][0])  # 慢的队列已满：这一条放不进去
    bus.publish(MAPPED[4][0])
    await bus.drain()
    assert slow.overflowed is True and healthy.overflowed is False
    assert slow.queue.get_nowait() is CLOSE and slow.queue.empty()  # 积压清掉，只剩一个关闭信号
    assert [healthy.queue.get_nowait() for _ in range(2)] == [MAPPED[3][1], MAPPED[4][1]]
    bus.publish(MAPPED[5][0])
    await bus.drain()
    assert slow.queue.empty()  # 已被判出局的不再收帧
    await bus.stop()


async def test_events_before_start_and_after_stop_are_not_delivered(bus, clock):
    hub = EventHub(bus, clock)
    await bus.start()
    conn = hub.connect(CLI)
    bus.publish(MAPPED[0][0])
    await bus.drain()
    assert conn.queue.empty()
    hub.start()
    hub.start()  # 重复 start 无害
    bus.publish(MAPPED[0][0])
    await bus.drain()
    assert conn.queue.qsize() == 1
    hub.stop()
    hub.stop()
    bus.publish(MAPPED[0][0])
    await bus.drain()
    assert conn.queue.qsize() == 1
    await bus.stop()


# ---------------------------------------------------------------- 端到端：握手

def ws_url(token: str | None) -> str:
    return f"{BASE}/events" + ("" if token is None else f"?token={token}")


class Feed:
    """带超时的取帧（TestClient 的 receive 会一直阻塞）。"""

    def __init__(self, ws) -> None:
        self.ws = ws
        self._pool = ThreadPoolExecutor(max_workers=1)

    def next(self, timeout: float = 5.0) -> dict:
        return self._pool.submit(self.ws.receive_json).result(timeout)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def connect(kit):
    feeds: list[Feed] = []
    managers = []

    def open_feed(token: str):
        manager = kit.client.websocket_connect(ws_url(token))
        managers.append(manager)
        feed = Feed(manager.__enter__())
        feeds.append(feed)
        return feed

    yield open_feed
    for manager in reversed(managers):
        manager.__exit__(None, None, None)
    for feed in feeds:
        feed.close()


@pytest.mark.parametrize("token", [None, "", "wrong-token", "A" * 600])
def test_the_handshake_is_refused_with_4401_for_a_missing_or_invalid_token(kit, token):
    with pytest.raises(WebSocketDisconnect) as refused:
        with kit.client.websocket_connect(ws_url(token)):
            pass
    assert refused.value.code == 4401


def test_a_revoked_session_token_is_refused_at_the_handshake(kit):
    sess = kit.login()
    sess.post("/auth/logout")
    with pytest.raises(WebSocketDisconnect) as refused:
        with kit.client.websocket_connect(ws_url(sess.token)):
            pass
    assert refused.value.code == 4401


def test_a_token_in_the_authorization_header_is_not_enough_for_the_websocket(kit):
    sess = kit.login()
    with pytest.raises(WebSocketDisconnect) as refused:
        with kit.client.websocket_connect(BASE + "/events", headers=sess.headers):
            pass
    assert refused.value.code == 4401


def test_a_session_token_and_the_local_token_are_both_accepted(kit, connect):
    connect(kit.login().token)
    connect(kit.local_token)
    assert kit.services.hub.connection_count == 2


# ---------------------------------------------------------------- 端到端：帧

def test_frames_are_json_text_with_the_agreed_fields(kit, connect):
    feed = connect(kit.login().token)
    kit.publish(ModelStateChanged(version=9, at_ms=T0 + 5))
    assert feed.next() == {"type": "state", "version": 9, "at_ms": T0 + 5}


def test_frames_are_sent_as_text_not_binary(kit):
    sess = kit.login()
    with kit.client.websocket_connect(ws_url(sess.token)) as ws:
        kit.publish(ConfigChanged(key="leverage", at_ms=T0))
        text = ws.receive_text()
    assert json.loads(text) == {"type": "state", "version": None, "at_ms": T0}


def test_unmapped_events_are_skipped_and_the_order_is_kept(kit, connect):
    feed = connect(kit.login().token)
    kit.publish(UNMAPPED[0], MAPPED[6][0], UNMAPPED[1], MAPPED[10][0], MAPPED[13][0])
    assert [feed.next() for _ in range(3)] == [MAPPED[6][1], MAPPED[10][1], MAPPED[13][1]]


def test_two_consoles_both_get_the_frames_and_one_leaving_does_not_matter(kit, connect):
    first, second = connect(kit.login("alice").token), connect(kit.login("bob").token)
    kit.publish(MAPPED[7][0])
    assert first.next() == MAPPED[7][1] and second.next() == MAPPED[7][1]
    first.ws.close()
    kit.publish(MAPPED[8][0])
    assert second.next() == MAPPED[8][1]


def test_a_seat_change_by_one_console_reaches_the_other_as_a_state_frame(kit, connect):
    alice, bob = kit.login("alice"), kit.login("bob")
    feed = connect(bob.token)
    alice.post("/seat/acquire")
    frame = feed.next()
    assert frame["type"] == "state" and frame["at_ms"] == T0
    alice.post("/auth/logout")  # 退出放席位：再来一帧
    assert feed.next()["type"] == "state"


def test_messages_from_the_client_are_ignored(kit, connect):
    feed = connect(kit.login().token)
    feed.ws.send_text("hello")
    feed.ws.send_json({"type": "subscribe"})
    kit.publish(MAPPED[0][0])
    assert feed.next() == MAPPED[0][1]


def test_the_hub_forgets_a_console_that_disconnected(kit):
    sess = kit.login()
    with kit.client.websocket_connect(ws_url(sess.token)):
        assert kit.services.hub.connection_count == 1
    assert kit.services.hub.connection_count == 0  # 退出 with 时会话任务已完整收尾，不用等


# ---------------------------------------------------------------- 端到端：ping 与会话失效

def test_a_ping_arrives_on_schedule(open_kit):
    with open_kit(ws_ping_s=0.05) as kit:
        sess = kit.login()
        with kit.client.websocket_connect(ws_url(sess.token)) as ws:
            feed = Feed(ws)
            assert feed.next() == {"type": "ping", "at_ms": T0}
            assert feed.next()["type"] == "ping"
            feed.close()


def test_pings_keep_coming_while_events_flow(open_kit):
    with open_kit(ws_ping_s=0.05) as kit:
        sess = kit.login()
        with kit.client.websocket_connect(ws_url(sess.token)) as ws:
            feed = Feed(ws)
            kit.publish(MAPPED[0][0])
            kinds = {feed.next()["type"] for _ in range(4)}
            assert kinds == {"state", "ping"}
            feed.close()


def drain_until_closed(ws, limit: int = 400) -> WebSocketDisconnect:
    with pytest.raises(WebSocketDisconnect) as closed:
        for _ in range(limit):
            ws.receive_json()
    return closed.value


def test_logging_out_closes_the_event_stream_with_4401(open_kit):
    with open_kit(ws_ping_s=0.05) as kit:
        sess = kit.login()
        with kit.client.websocket_connect(ws_url(sess.token)) as ws:
            sess.post("/auth/logout")
            assert drain_until_closed(ws).code == 4401


def test_an_idle_expired_session_loses_its_event_stream_too(open_kit):
    with open_kit(ws_ping_s=0.05) as kit:
        sess = kit.login()
        with kit.client.websocket_connect(ws_url(sess.token)) as ws:
            kit.clock.advance(31 * 60_000)  # 连着事件流不算活动：会话照样闲置过期
            assert drain_until_closed(ws).code == 4401


def test_the_local_token_stream_survives_session_churn(open_kit):
    with open_kit(ws_ping_s=0.05) as kit:
        sess = kit.login()
        with kit.client.websocket_connect(ws_url(kit.local_token)) as ws:
            sess.post("/auth/logout")
            feed = Feed(ws)
            kinds = [feed.next()["type"] for _ in range(4)]
            assert set(kinds) == {"ping"}
            feed.close()


# ---------------------------------------------------------------- 外部取消与连接出错的收尾

class Socket:
    """直接对着 ASGI 应用说话的最小 WebSocket 对端，跑在应用自己的事件循环里（用 ``kit.run`` 交给那个循环）。

    starlette 的 TestClient 退出会话时先发 disconnect，紧接着取消会话外面的 anyio 取消范围；服务器关停则是没有
    disconnect 的取消。这里让「断开」与「取消」之间隔几个事件循环轮次，把端点收尾的每一步都碰一遍。
    """

    def __init__(self, app, token: str, *, break_sends: bool = False, break_receives: bool = False,
                 fail_receives_with: Exception | None = None) -> None:
        path = f"{BASE}/events"
        self.app = app
        self.scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "ws",
                      "path": path, "raw_path": path.encode(), "root_path": "", "headers": [],
                      "query_string": f"token={token}".encode(), "client": ("testclient", 50000),
                      "server": ("testserver", 80), "subprotocols": [], "state": {}}
        self.break_sends, self.break_receives = break_sends, break_receives
        self.fail_receives_with = fail_receives_with  # 握手之后的接收抛出这个异常（模拟程序错误）
        self.outer = anyio.CancelScope()  # 相当于 TestClient 会话外面被取消的那个范围
        self.escaped: BaseException | None = None  # 没被外层范围吸收、漏出来的取消或异常
        self.handler_returned = asyncio.Event()  # 端点的处理函数自己返回了（没靠外部取消）
        self.task: asyncio.Task | None = None
        self._receives = 0
        self._to_app_tx, self._to_app_rx = anyio.create_memory_object_stream[dict](math.inf)
        self._from_app_tx, self._from_app_rx = anyio.create_memory_object_stream[dict](math.inf)

    async def _receive(self) -> dict:
        self._receives += 1
        if self._receives > 1:  # 第一次是握手的 connect
            if self.break_receives:
                raise ConnectionResetError("对端已断开")
            if self.fail_receives_with is not None:
                raise self.fail_receives_with
        return await self._to_app_rx.receive()

    async def _send(self, message: dict) -> None:
        if self.break_sends and message["type"] == "websocket.send":
            raise ConnectionResetError("对端已断开")
        await self._from_app_tx.send(message)

    async def _session(self) -> None:
        try:
            with self._to_app_tx, self._to_app_rx, self._from_app_tx, self._from_app_rx, self.outer:
                await self.app(self.scope, self._receive, self._send)
                self.handler_returned.set()
                await anyio.sleep_forever()
        except BaseException as exc:  # noqa: BLE001 - 漏出外层范围的取消或异常，交给测试断言
            self.escaped = exc

    async def open(self) -> dict:
        """发 connect，返回应用回的第一条消息（accept 或 close）。"""
        self.task = asyncio.ensure_future(self._session())
        self._to_app_tx.send_nowait({"type": "websocket.connect"})
        return await self.next_message()

    async def next_message(self) -> dict:
        return await asyncio.wait_for(self._from_app_rx.receive(), 5)

    async def messages_until_close(self) -> list[dict]:
        got: list[dict] = []
        while not got or got[-1]["type"] != "websocket.close":
            got.append(await self.next_message())
        return got

    def disconnect(self) -> None:
        self._to_app_tx.send_nowait({"type": "websocket.disconnect", "code": 1000})

    async def finish(self) -> None:
        """取消外层范围，等会话任务结束。"""
        self.outer.cancel()
        assert self.task is not None
        await asyncio.wait_for(self.task, 5)


async def teardown_problems(kit, token: str, *, disconnect_first: bool) -> list[str]:
    """让外部取消落在端点收尾的每一个事件循环轮次上，返回发现的问题（空表示都干净）。"""
    hub, problems = kit.services.hub, []
    for delay in range(16):
        before = asyncio.all_tasks()
        sock = Socket(kit.app, token)
        accepted = await sock.open()
        if accepted["type"] != "websocket.accept" or hub.connection_count != 1:
            problems.append(f"间隔 {delay}：握手没成功（{accepted}，登记 {hub.connection_count} 个连接）")
            await sock.finish()
            continue
        if disconnect_first:
            sock.disconnect()
        for _ in range(delay):
            await asyncio.sleep(0)
        await sock.finish()
        stray = [t for t in asyncio.all_tasks() - before - {asyncio.current_task()} if not t.done()]
        if sock.escaped is not None:
            problems.append(f"间隔 {delay}：{sock.escaped!r} 漏出了外层取消范围")
        if hub.connection_count:
            problems.append(f"间隔 {delay}：事件中心里还登记着 {hub.connection_count} 个连接")
        if stray:
            problems.append(f"间隔 {delay}：遗留了没结束的任务 {stray}")
    return problems


@pytest.mark.parametrize("disconnect_first", [True, False], ids=["client-leaves-then-cancel", "cancel-while-connected"])
def test_an_outside_cancellation_at_any_moment_is_absorbed_and_frees_the_connection(kit, disconnect_first):
    token = kit.login().token
    assert kit.run(teardown_problems(kit, token, disconnect_first=disconnect_first)) == []


@pytest.mark.parametrize("breaks", ["sends", "receives"])
def test_a_broken_connection_ends_the_stream_by_itself_quietly_and_frees_the_connection(kit, breaks, caplog):
    token = kit.login().token

    async def go() -> Socket:
        sock = Socket(kit.app, token, break_sends=breaks == "sends", break_receives=breaks == "receives")
        assert (await sock.open())["type"] == "websocket.accept"
        if breaks == "sends":
            kit.bus.publish(MAPPED[0][0])  # 这一帧写不出去
            await kit.bus.drain()
        await asyncio.wait_for(sock.handler_returned.wait(), 5)  # 没人取消：处理函数自己结束
        await sock.finish()
        return sock

    with caplog.at_level(logging.DEBUG, logger="dmr_kernel.api.events"):
        sock = kit.run(go())
    assert sock.escaped is None
    assert kit.services.hub.connection_count == 0
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []  # 对端走了是常事，不是错误


@pytest.mark.parametrize("fails", ["writer", "reader"])
def test_an_unexpected_error_ends_the_stream_and_is_logged_with_its_traceback(kit, caplog, fails):
    token = kit.login().token

    async def go() -> Socket:
        sock = Socket(kit.app, token, fail_receives_with=ValueError("bug") if fails == "reader" else None)
        assert (await sock.open())["type"] == "websocket.accept"
        if fails == "writer":  # 帧里有 Decimal：JSON 序列化会出错，这是程序错误而不是连接问题
            kit.bus.publish(ModelStateChanged(version=Decimal("1"), at_ms=T0))
            await kit.bus.drain()
        await asyncio.wait_for(sock.handler_returned.wait(), 5)
        await sock.finish()
        return sock

    with caplog.at_level(logging.DEBUG, logger="dmr_kernel.api.events"):
        sock = kit.run(go())
    assert sock.escaped is None and kit.services.hub.connection_count == 0
    [record] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert record.name == "dmr_kernel.api.events"
    assert record.exc_info is not None and record.exc_info[0] is (TypeError if fails == "writer" else ValueError)


def test_a_stream_closed_by_the_server_for_a_dead_session_ends_by_itself(open_kit):
    with open_kit(ws_ping_s=0.05) as kit:
        token = kit.login().token

        async def go():
            sock = Socket(kit.app, token)
            assert (await sock.open())["type"] == "websocket.accept"
            kit.clock.advance(31 * 60_000)  # 闲置过期：下一次 ping 时复核不再通过
            closing = (await sock.messages_until_close())[-1]
            await asyncio.wait_for(sock.handler_returned.wait(), 5)
            await sock.finish()
            return sock, closing

        sock, closing = kit.run(go())
        assert closing["code"] == 4401
        assert sock.escaped is None and kit.services.hub.connection_count == 0


def test_a_consumer_that_cannot_keep_up_is_closed_with_1013_and_the_stream_ends(open_kit):
    with open_kit(ws_max_queue=3) as kit:
        token = kit.login().token

        async def go():
            sock = Socket(kit.app, token)
            assert (await sock.open())["type"] == "websocket.accept"
            for _ in range(10):  # 队列只有 3 格，一口气来 10 条必然挤爆
                kit.bus.publish(MAPPED[0][0])
            await kit.bus.drain()
            got = await sock.messages_until_close()
            await asyncio.wait_for(sock.handler_returned.wait(), 5)
            await sock.finish()
            return sock, got

        sock, got = kit.run(go())
        assert got[-1]["code"] == 1013
        assert all(json.loads(m["text"]) == MAPPED[0][1] for m in got[:-1])  # 关闭之前发出去的都是完整的帧
        assert sock.escaped is None and kit.services.hub.connection_count == 0


def test_a_hiccup_while_rechecking_the_token_does_not_end_the_stream(open_kit, monkeypatch):
    with open_kit(ws_ping_s=0.05) as kit:
        sess = kit.login()
        real, calls = kit.services.auth.authenticate, []

        async def flaky(token, **kw):
            calls.append(token)
            if len(calls) == 2:  # 第一次是握手，第二次是第一次 ping 时的复核
                raise RuntimeError("账本暂时读不了")
            return await real(token, **kw)

        monkeypatch.setattr(kit.services.auth, "authenticate", flaky)
        with kit.client.websocket_connect(ws_url(sess.token)) as ws:
            feed = Feed(ws)
            assert {feed.next()["type"] for _ in range(4)} == {"ping"}  # 出错那一次照样发了 ping，后面的复核照常通过
            assert len(calls) >= 4
            feed.close()
