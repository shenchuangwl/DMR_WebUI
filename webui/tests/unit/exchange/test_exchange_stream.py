"""StandinAccountStream 的状态机与判活（脚本化传输，不连替身）。

时间全部由 ManualClock 推进：续期、静默判活、退避重连、稳定窗口都在 ``check_liveness()`` 里按注入时钟评估，
测试推进时钟后直接调它。读循环只负责收帧发事件。对真实替身行为的验证见 tests/integration/test_stream_standin.py。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from decimal import Decimal
from typing import Any

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import (
    EventBus,
    ListenKeyExpired,
    StreamAccountReceived,
    StreamAlgoReceived,
    StreamOrderReceived,
    StreamStatusChanged,
)
from dmr_kernel.core.types import StreamAccount, StreamAlgo, StreamOrder
from dmr_kernel.exchange.adapter import AccountStream
from dmr_kernel.exchange.transport import Response, TransportConnectionError, TransportTimeout
from dmr_kernel.exchange.stream import StandinAccountStream, parse_frame
from dmr_kernel.exchange.standin import DecodeError
from tests.support.inproc import T0_MS, default_config

LISTEN_KEY_PATH = "/api/private/listen_key"
WS_PATH = "/ws/stream"
SECRET_PREFIX = "KEYSECRET"

ORDER = {"symbol": "BTCUSDT", "client_order_id": "DKA1-1-1-o", "order_id": "9001", "side": "BUY",
         "position_side": "LONG", "type": "MARKET", "qty": "0.010", "filled_qty": "0.010", "avg_price": "50000",
         "status": "FILLED", "raw_status": "FILLED", "close_position": False, "reduce_only": False,
         "update_time_ms": T0_MS}
TRADE = {"trade_id": "5001", "order_id": "9001", "symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
         "qty": "0.010", "price": "50000", "commission": "0.2", "commission_asset": "USDT", "realized_pnl": "0",
         "time_ms": T0_MS, "maker": False}
ALGO = {"symbol": "BTCUSDT", "client_algo_id": "DKA1-0-1-s", "algo_id": "7001", "side": "SELL",
        "position_side": "LONG", "type": "STOP_MARKET", "trigger_price": "40000", "working_type": "MARK_PRICE",
        "close_position": True, "qty": None, "status": "NEW", "actual_order_id": None, "create_time_ms": T0_MS,
        "update_time_ms": T0_MS}
POSITION = {"symbol": "BTCUSDT", "position_side": "LONG", "qty": "0.010", "entry_price": "50000",
            "mark_price": "50000", "liquidation_price": "47500", "unrealized_pnl": "0", "leverage": 20,
            "margin_mode": "cross", "update_time_ms": T0_MS}

ORDER_FRAME = {"type": "order_update", "event_time_ms": T0_MS, "execution": "TRADE", "order": ORDER, "fill": TRADE}
ALGO_FRAME = {"type": "algo_update", "event_time_ms": T0_MS, "algo": ALGO}
ACCOUNT_FRAME = {"type": "account_update", "event_time_ms": T0_MS, "reason": "ORDER", "positions": [POSITION],
                 "balances": [{"asset": "USDT", "wallet_balance": "1000"}]}
HEARTBEAT = {"type": "heartbeat", "server_time_ms": T0_MS}
EXPIRED = {"type": "listen_key_expired", "event_time_ms": T0_MS}


# ================================================================ 脚本化传输


class FakeWs:
    """可编程的数据流连接：``push`` 放一帧，``peer_close`` 模拟对端关闭，推 Exception 实例则 ``receive`` 抛出它。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.close_calls = 0

    def push(self, frame: Any) -> None:
        self.queue.put_nowait(frame if isinstance(frame, (str, Exception)) else json.dumps(frame))

    def peer_close(self) -> None:
        self.queue.put_nowait(None)

    async def receive(self, timeout_s: float | None) -> str | None:
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.close_calls += 1
        self.queue.put_nowait(None)


class StuckWs:
    """``close()`` 唤不醒读者的连接：读循环只能靠被取消才能结束（模拟对端挂死、关闭没有回音）。"""

    def __init__(self) -> None:
        self.close_calls = 0

    async def receive(self, timeout_s: float | None) -> str | None:
        await asyncio.Event().wait()
        return None

    async def close(self) -> None:
        self.close_calls += 1


def reader_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name() == "account-stream-reader" and not t.done()]


class ScriptedTransport:
    """POST/PUT listen_key 与 ws_connect 的结果可以排队编排；没排队时给正常的默认结果。"""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.post_script: deque[Any] = deque()
        self.put_script: deque[Any] = deque()
        self.ws_script: deque[Any] = deque()
        self.requests: list[tuple[str, str, dict | None, dict | None, bool, float]] = []
        self.post_times: list[int] = []
        self.put_times: list[int] = []
        self.ws_connects: list[tuple[str, dict, float]] = []
        self.conns: list[FakeWs] = []
        self.gate: asyncio.Event | None = None  # 设了就让 POST 卡住，直到 gate.set()
        self.put_gate: asyncio.Event | None = None  # 设了就让 PUT 卡住，直到 put_gate.set()
        self._keys = 0

    async def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                      auth: bool = True, timeout_s: float) -> Response:
        assert path == LISTEN_KEY_PATH, f"数据流不该请求 {path}"
        self.requests.append((method, path, params, body, auth, timeout_s))
        if method == "POST":
            self.post_times.append(self.clock.now_ms())
            if self.gate is not None:
                await self.gate.wait()
            script = self.post_script
        else:
            self.put_times.append(self.clock.now_ms())
            if self.put_gate is not None:
                await self.put_gate.wait()
            script = self.put_script
        if script:
            item = script.popleft()
            if isinstance(item, Exception):
                raise item
            return item
        if method == "POST":
            self._keys += 1
            return Response(200, {"listen_key": f"{SECRET_PREFIX}{self._keys}", "expires_ms": self.clock.now_ms() + 3_600_000}, {})
        return Response(200, {"expires_ms": self.clock.now_ms() + 3_600_000}, {})

    async def ws_connect(self, path: str, *, params: dict, timeout_s: float) -> FakeWs:
        self.ws_connects.append((path, dict(params), timeout_s))
        if self.ws_script:
            item = self.ws_script.popleft()
            if isinstance(item, Exception):
                raise item
            self.conns.append(item)
            return item
        ws = FakeWs()
        self.conns.append(ws)
        return ws

    async def close(self) -> None:
        return None

    @property
    def posts(self) -> int:
        return len([r for r in self.requests if r[0] == "POST"])

    @property
    def puts(self) -> int:
        return len([r for r in self.requests if r[0] == "PUT"])

    @property
    def ws(self) -> FakeWs:
        return self.conns[-1]


class Rig:
    def __init__(self, *, keepalive_s: float = 1200.0, backoff: tuple[float, ...] = (1, 2, 5, 10, 30),
                 timeout_s: float = 3.0, **config: Any) -> None:
        self.clock = ManualClock(T0_MS)
        self.config = default_config(**config)
        self.bus = EventBus()
        self.transport = ScriptedTransport(self.clock)
        self.stream = StandinAccountStream(self.transport, bus=self.bus, clock=self.clock,
                                           config_get=self.config.__getitem__, keepalive_s=keepalive_s,
                                           reconnect_backoff_s=backoff, request_timeout_s=timeout_s)
        self.timeout_s = timeout_s
        self.events: list[Any] = []
        self.bus.subscribe(None, self.events.append, name="rig")

    async def start(self) -> None:
        await self.bus.start()
        await self.stream.start()
        await self.settle()

    async def settle(self) -> None:
        for _ in range(6):
            await asyncio.sleep(0)
        await self.bus.drain(2.0)

    async def advance(self, seconds: float, *, beat: bool = False) -> None:
        """推进时钟（可先让当前连接来一帧心跳），再做一次判活。"""
        self.clock.advance(round(seconds * 1000))
        if beat and self.transport.conns:
            self.transport.ws.push(HEARTBEAT)
            await self.settle()
        await self.stream.check_liveness()
        await self.settle()

    def states(self) -> list[str]:
        return [e.state for e in self.events if isinstance(e, StreamStatusChanged)]

    def status(self, state: str) -> list[StreamStatusChanged]:
        return [e for e in self.events if isinstance(e, StreamStatusChanged) and e.state == state]

    def of(self, cls: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, cls)]


# ================================================================ 帧解析


def test_parse_frame_returns_typed_records():
    assert isinstance(parse_frame(json.dumps(ORDER_FRAME)), StreamOrder)
    assert isinstance(parse_frame(json.dumps(ALGO_FRAME)), StreamAlgo)
    account = parse_frame(json.dumps(ACCOUNT_FRAME))
    assert isinstance(account, StreamAccount) and account.balances == (("USDT", Decimal("1000")),)


def test_parse_frame_heartbeat_and_expired_are_distinguishable():
    from dmr_kernel.exchange.stream import Heartbeat, ListenKeyExpiredFrame

    assert isinstance(parse_frame(json.dumps(HEARTBEAT)), Heartbeat)
    assert isinstance(parse_frame(json.dumps(EXPIRED)), ListenKeyExpiredFrame)


@pytest.mark.parametrize("text", [
    "not json", "", "[1, 2]", '"text"', "null", "42", "{}", '{"type": "mystery"}', '{"type": 5}',
    json.dumps({"type": "order_update", "event_time_ms": 1}),
    json.dumps({**ACCOUNT_FRAME, "positions": "x"}),
])
def test_parse_frame_rejects_garbage_with_decode_error(text):
    with pytest.raises(DecodeError):
        parse_frame(text)


# ================================================================ 启动与停止


async def test_initial_state_is_stopped_and_start_connects_with_a_listen_key():
    r = Rig()
    assert r.stream.state == "stopped" and isinstance(r.stream, AccountStream)
    assert r.stream.last_frame_ms is None and r.stream.last_keepalive_ok_ms is None
    await r.start()
    assert r.stream.state == "connected"
    method, path, params, body, auth, timeout_s = r.transport.requests[0]
    assert (method, path, auth, timeout_s) == ("POST", LISTEN_KEY_PATH, True, 3.0)
    path, params, timeout_s = r.transport.ws_connects[0]
    assert (path, params, timeout_s) == (WS_PATH, {"listen_key": f"{SECRET_PREFIX}1"}, 3.0)
    assert r.stream.last_keepalive_ok_ms == T0_MS
    assert r.stream.last_frame_ms is None
    assert [(e.state, e.at_ms) for e in r.of(StreamStatusChanged)] == [("connected", T0_MS)]
    assert r.status("connected")[0].reason


async def test_state_is_connecting_while_the_first_connection_is_made():
    r = Rig()
    r.transport.gate = asyncio.Event()
    await r.bus.start()
    task = asyncio.create_task(r.stream.start())
    await r.settle()
    assert r.stream.state == "connecting"
    r.transport.gate.set()
    await task
    assert r.stream.state == "connected"


async def test_start_twice_opens_only_one_connection():
    r = Rig()
    await r.start()
    await r.stream.start()
    await r.settle()
    assert r.transport.posts == 1 and len(r.transport.ws_connects) == 1
    assert r.states() == ["connected"]


async def test_stop_closes_the_connection_publishes_stopped_once_and_is_idempotent():
    r = Rig()
    await r.start()
    ws = r.transport.ws
    await r.stream.stop()
    await r.stream.stop()
    await r.settle()
    assert r.stream.state == "stopped" and ws.close_calls >= 1
    assert r.states() == ["connected", "stopped"]
    ws.push(ORDER_FRAME)  # 停止后到达的帧不再处理
    await r.settle()
    assert r.of(StreamOrderReceived) == []
    await r.advance(3600)  # 停止后判活什么也不做
    assert r.transport.posts == 1 and r.states() == ["connected", "stopped"]


async def test_stop_without_start_publishes_nothing():
    r = Rig()
    await r.bus.start()
    await r.stream.stop()
    await r.settle()
    assert r.stream.state == "stopped" and r.events == []


async def test_stream_can_be_restarted_after_stop():
    r = Rig()
    await r.start()
    await r.stream.stop()
    await r.stream.start()
    await r.settle()
    assert r.stream.state == "connected" and r.transport.posts == 2
    r.transport.ws.push(ORDER_FRAME)
    await r.settle()
    assert len(r.of(StreamOrderReceived)) == 1
    assert r.states() == ["connected", "stopped", "connected"]


async def test_stop_while_the_first_connection_is_in_flight_leaves_nothing_open():
    r = Rig()
    r.transport.gate = asyncio.Event()
    await r.bus.start()
    task = asyncio.create_task(r.stream.start())
    await r.settle()
    await r.stream.stop()
    r.transport.gate.set()
    await task
    await r.settle()
    assert r.stream.state == "stopped"
    assert "connected" not in r.states()
    assert all(ws.close_calls >= 1 for ws in r.transport.conns)  # 即使建起来了也被关掉


async def test_start_never_raises_when_the_exchange_refuses_the_stream():
    r = Rig()
    r.transport.ws_script.append(TransportConnectionError("数据流被拒绝"))
    await r.start()
    assert r.stream.state == "interrupted"
    assert r.states() == ["interrupted"] and "拒绝" in r.status("interrupted")[0].reason


# ================================================================ 帧 -> 事件


async def test_business_frames_become_bus_events_in_order():
    r = Rig()
    await r.start()
    ws = r.transport.ws
    for frame in (ACCOUNT_FRAME, ORDER_FRAME, ALGO_FRAME):
        ws.push(frame)
    await r.settle()
    kinds = [type(e) for e in r.events if not isinstance(e, StreamStatusChanged)]
    assert kinds == [StreamAccountReceived, StreamOrderReceived, StreamAlgoReceived]
    order = r.of(StreamOrderReceived)[0].payload
    assert order.execution == "TRADE" and order.order.exchange_order_id == "9001" and order.fill.trade_id == "5001"
    assert r.of(StreamAlgoReceived)[0].payload.algo.algo_id == "7001"
    assert r.of(StreamAccountReceived)[0].payload.positions[0].qty == Decimal("0.010")


async def test_heartbeat_is_only_liveness_and_publishes_nothing():
    r = Rig()
    await r.start()
    r.clock.advance(10_000)
    r.transport.ws.push(HEARTBEAT)
    await r.settle()
    assert r.stream.last_frame_ms == T0_MS + 10_000
    assert [type(e) for e in r.events] == [StreamStatusChanged]


async def test_garbage_frames_are_dropped_counted_and_still_count_as_alive():
    r = Rig()
    await r.start()
    ws = r.transport.ws
    r.clock.advance(5_000)
    for junk in ("not json", "[1,2]", json.dumps({"type": "mystery"}), json.dumps({"type": "order_update"}),
                 json.dumps({**ORDER_FRAME, "order": {**ORDER, "qty": "abc"}})):
        ws.push(junk)
    await r.settle()
    assert r.stream.dropped_frames == 5
    assert r.stream.state == "connected" and r.stream.last_frame_ms == T0_MS + 5_000
    assert [type(e) for e in r.events] == [StreamStatusChanged]  # 没有任何业务事件
    ws.push(ORDER_FRAME)  # 坏帧之后好帧照常处理
    await r.settle()
    assert len(r.of(StreamOrderReceived)) == 1 and r.stream.dropped_frames == 5


async def test_a_handler_error_in_the_bus_does_not_break_the_stream():
    r = Rig()

    def boom(event):
        raise RuntimeError("订阅者出错")

    r.bus.subscribe(StreamOrderReceived, boom, name="boom")
    await r.start()
    r.transport.ws.push(ORDER_FRAME)
    r.transport.ws.push(ORDER_FRAME)
    await r.settle()
    assert r.stream.state == "connected" and len(r.of(StreamOrderReceived)) == 2


# ================================================================ 中断与重连


async def test_peer_close_interrupts_and_reconnect_waits_for_the_first_backoff():
    r = Rig()
    await r.start()
    old = r.transport.ws
    old.peer_close()
    await r.settle()
    assert r.stream.state == "interrupted"
    assert r.states() == ["connected", "interrupted"] and r.status("interrupted")[0].reason
    await r.advance(0.9)
    assert r.transport.posts == 1 and r.stream.state == "interrupted"
    await r.advance(0.1)
    assert r.transport.posts == 2 and r.stream.state == "connected"
    assert r.states() == ["connected", "interrupted", "connected"]
    assert r.transport.ws is not old and old.close_calls >= 1


async def test_read_error_interrupts_with_the_reason():
    r = Rig()
    await r.start()
    r.transport.ws.push(TransportConnectionError("connection reset"))
    await r.settle()
    assert r.stream.state == "interrupted"
    assert "connection reset" in r.status("interrupted")[0].reason


async def test_failed_attempts_back_off_1_2_5_10_30_30_and_publish_interrupted_only_once():
    r = Rig()
    await r.start()
    r.transport.ws_script.extend([TransportConnectionError("拒绝")] * 6)
    r.transport.ws.peer_close()
    await r.settle()
    base = r.clock.now_ms()
    # 每次失败之后到下一次尝试的间隔：1、2、5、10、30、30，然后成功
    for wait, posts_after in ((1, 2), (2, 3), (5, 4), (10, 5), (30, 6), (30, 7)):
        await r.advance(wait - 0.001)
        assert r.transport.posts == posts_after - 1, f"{wait} 秒的间隔未到就重试了"
        await r.advance(0.001)
        assert r.transport.posts == posts_after, f"{wait} 秒到了没有重试"
        assert r.stream.state == "interrupted"
    assert r.clock.now_ms() - base == (1 + 2 + 5 + 10 + 30 + 30) * 1000
    await r.advance(30)
    assert r.stream.state == "connected" and r.transport.posts == 8
    assert r.states() == ["connected", "interrupted", "connected"]


async def test_backoff_restarts_from_the_first_step_after_a_successful_reconnect():
    r = Rig()
    await r.start()
    r.transport.ws_script.extend([TransportConnectionError("拒绝")] * 2)
    r.transport.ws.peer_close()
    await r.settle()
    await r.advance(1)  # 失败 1
    await r.advance(2)  # 失败 2
    await r.advance(5)  # 成功
    assert r.stream.state == "connected"
    posts = r.transport.posts
    r.transport.ws.peer_close()
    await r.settle()
    await r.advance(1)
    assert r.transport.posts == posts + 1 and r.stream.state == "connected"


async def test_first_connection_failure_retries_and_then_recovers():
    r = Rig()
    r.transport.ws_script.append(TransportConnectionError("拒绝"))
    await r.start()
    assert r.stream.state == "interrupted"
    await r.advance(1)
    assert r.stream.state == "connected"
    assert r.states() == ["interrupted", "connected"]
    await r.advance(60, beat=True)
    assert r.states() == ["interrupted", "connected", "recovered_stable"]


@pytest.mark.parametrize("script, expect", [
    (Response(401, {"code": -2015, "msg": "Invalid key"}, {}), "401"),
    (Response(403, {"code": -1, "msg": "forbidden"}, {}), "403"),
    (Response(503, {"code": -1007, "msg": "Timeout waiting for response"}, {}), "503"),
    (Response(429, {"code": -1003, "msg": "Too many requests"}, {"Retry-After": "3"}), "429"),
    (Response(200, {"expires_ms": 1}, {}), "listen_key"),
    (Response(200, {"listen_key": "", "expires_ms": 1}, {}), "listen_key"),
    (Response(200, {"listen_key": 123, "expires_ms": 1}, {}), "listen_key"),
    (Response(200, "<html>bad gateway</html>", {}), ""),
    (TransportTimeout("请求超时"), "超时"),
    (TransportConnectionError("连接被拒绝"), "连接"),
])
async def test_listen_key_failures_make_the_stream_interrupted_without_raising(script, expect):
    r = Rig()
    r.transport.post_script.append(script)
    await r.start()  # 不抛
    assert r.stream.state == "interrupted" and r.states() == ["interrupted"]
    assert expect in r.status("interrupted")[0].reason
    assert r.transport.ws_connects == []  # 没有 listenKey 就不连
    await r.advance(1)  # 下一次尝试正常 -> 恢复
    assert r.stream.state == "connected"


async def test_auth_failure_is_logged_as_a_configuration_error(caplog):
    r = Rig()
    r.transport.post_script.append(Response(401, {"code": -2015, "msg": "Invalid key"}, {}))
    with caplog.at_level(logging.WARNING):
        await r.start()
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


async def test_old_connection_frames_are_ignored_after_it_has_been_replaced():
    r = Rig()
    await r.start()
    old = r.transport.ws
    old.peer_close()
    await r.settle()
    await r.advance(1)
    assert r.stream.state == "connected" and r.transport.ws is not old
    old.push(ORDER_FRAME)
    await r.settle()
    assert r.of(StreamOrderReceived) == []
    r.transport.ws.push(ORDER_FRAME)
    await r.settle()
    assert len(r.of(StreamOrderReceived)) == 1


async def test_concurrent_liveness_checks_make_a_single_reconnect_attempt():
    r = Rig()
    await r.start()
    r.transport.ws.peer_close()
    await r.settle()
    r.clock.advance(1000)
    r.transport.gate = asyncio.Event()
    first = asyncio.create_task(r.stream.check_liveness())
    await r.settle()
    second = asyncio.create_task(r.stream.check_liveness())
    await r.settle()
    assert r.transport.posts == 2  # 启动一次 + 这一轮一次
    r.transport.gate.set()
    await asyncio.gather(first, second)
    await r.settle()
    assert r.transport.posts == 2 and r.stream.state == "connected"
    assert r.states() == ["connected", "interrupted", "connected"]


# ================================================================ 判活：静默


async def test_silence_for_stream_silence_s_interrupts_the_stream():
    r = Rig()
    await r.start()
    await r.advance(89.999)
    assert r.stream.state == "connected"
    await r.advance(0.001)
    assert r.stream.state == "interrupted"
    reason = r.status("interrupted")[0].reason
    assert "90" in reason and "帧" in reason
    assert r.transport.ws.close_calls >= 1


async def test_any_frame_resets_the_silence_timer_even_a_heartbeat():
    r = Rig()
    await r.start()
    await r.advance(60, beat=True)
    await r.advance(89, beat=False)
    assert r.stream.state == "connected"
    await r.advance(1)
    assert r.stream.state == "interrupted"


async def test_frames_that_arrive_just_before_the_check_are_counted():
    """判活之前先让读循环处理完已到达的帧：不能因为帧还躺在队列里就误判静默。"""
    r = Rig()
    await r.start()
    r.clock.advance(120_000)
    r.transport.ws.push(HEARTBEAT)  # 帧已到，但读循环还没来得及处理
    await r.stream.check_liveness()
    assert r.stream.state == "connected"


async def test_silence_threshold_is_read_from_the_config_each_time():
    r = Rig()
    await r.start()
    r.config["stream_silence_s"] = 30
    await r.advance(29)
    assert r.stream.state == "connected"
    await r.advance(1)
    assert r.stream.state == "interrupted"


# ================================================================ 判活：listenKey 续期


async def test_keepalive_is_sent_every_keepalive_s_with_the_listen_key():
    r = Rig()
    await r.start()
    await r.advance(1199, beat=True)
    assert r.transport.puts == 0
    await r.advance(1, beat=True)
    assert r.transport.puts == 1
    method, path, params, body, auth, timeout_s = [q for q in r.transport.requests if q[0] == "PUT"][0]
    assert (path, body, auth, timeout_s) == (LISTEN_KEY_PATH, {"listen_key": f"{SECRET_PREFIX}1"}, True, 3.0)
    assert r.stream.last_keepalive_ok_ms == T0_MS + 1_200_000
    await r.advance(1199, beat=True)
    assert r.transport.puts == 1
    await r.advance(1, beat=True)
    assert r.transport.puts == 2 and r.stream.last_keepalive_ok_ms == T0_MS + 2_400_000
    assert r.stream.state == "connected"


async def test_failed_keepalive_is_retried_every_minute_and_interrupts_after_listenkey_stale_min():
    r = Rig()
    await r.start()
    r.transport.put_script.extend([Response(400, {"code": -1125, "msg": "listenKey 续期失败"}, {})] * 100)
    for _ in range(49):  # 到 49 分钟：一直失败，但还没超过 50 分钟
        await r.advance(60, beat=True)
    assert r.stream.state == "connected"
    assert r.stream.last_keepalive_ok_ms == T0_MS  # 一次都没成功
    assert r.transport.put_times[0] == T0_MS + 1_200_000
    assert r.transport.put_times[1] == T0_MS + 1_260_000  # 失败后每分钟重试
    await r.advance(60, beat=True)  # 刚好 50 分钟
    assert r.stream.state == "interrupted"
    reason = r.status("interrupted")[0].reason
    assert "listenKey" in reason and "50" in reason


async def test_stale_threshold_is_read_from_the_config_each_time():
    r = Rig(listenkey_stale_min=5)
    await r.start()
    r.config["listenkey_stale_min"] = 10
    await r.advance(9 * 60, beat=True)
    assert r.stream.state == "connected"
    await r.advance(60, beat=True)
    assert r.stream.state == "interrupted"


async def test_keepalive_with_a_short_interval_succeeds_and_keeps_the_stream_connected():
    r = Rig(keepalive_s=100.0)
    await r.start()
    for _ in range(10):
        await r.advance(100, beat=True)
    assert r.transport.puts == 10 and r.stream.state == "connected"
    assert r.stream.last_keepalive_ok_ms == r.clock.now_ms()


async def test_keepalive_transport_errors_are_a_failed_renewal_not_a_crash():
    r = Rig(keepalive_s=100.0)
    await r.start()
    r.transport.put_script.extend([TransportTimeout("超时"), TransportConnectionError("断开"),
                                   Response(503, {"code": -1007, "msg": "未知"}, {})])
    await r.advance(100, beat=True)
    await r.advance(60, beat=True)
    await r.advance(60, beat=True)
    assert r.transport.puts == 3 and r.stream.state == "connected"
    assert r.stream.last_keepalive_ok_ms == T0_MS
    await r.advance(60, beat=True)  # 第四次成功
    assert r.transport.puts == 4 and r.stream.last_keepalive_ok_ms == r.clock.now_ms()


async def test_a_listen_key_is_never_written_to_the_log(caplog):
    r = Rig(keepalive_s=100.0)
    with caplog.at_level(logging.DEBUG):
        r.transport.post_script.append(Response(503, {"code": -1007, "msg": "x"}, {}))
        await r.start()
        await r.advance(1)
        r.transport.put_script.append(Response(400, {"code": -1125, "msg": "x"}, {}))
        await r.advance(100, beat=True)
        r.transport.ws.push(json.dumps({"type": "garbage", "listen_key": f"{SECRET_PREFIX}1"}))
        r.transport.ws.push(EXPIRED)
        await r.settle()
        await r.stream.stop()
    assert caplog.records
    assert not any(SECRET_PREFIX in rec.getMessage() for rec in caplog.records)
    assert not any(SECRET_PREFIX in e.reason for e in r.of(StreamStatusChanged))


async def test_concurrent_liveness_checks_send_a_single_keepalive():
    r = Rig(keepalive_s=100.0)
    await r.start()
    r.transport.put_gate = asyncio.Event()
    r.clock.advance(100_000)
    r.transport.ws.push(HEARTBEAT)
    await r.settle()
    first = asyncio.create_task(r.stream.check_liveness())
    await r.settle()
    second = asyncio.create_task(r.stream.check_liveness())
    await r.settle()
    assert r.transport.puts == 1  # 第一份还卡在 PUT 上，第二份直接返回
    r.transport.put_gate.set()
    await asyncio.gather(first, second)
    assert r.transport.puts == 1 and r.stream.state == "connected"


async def test_stop_cancels_a_reader_stuck_on_a_connection_that_ignores_close():
    r = Rig()
    stuck = StuckWs()
    r.transport.ws_script.append(stuck)
    await r.start()
    assert len(reader_tasks()) == 1
    await r.stream.stop()
    assert reader_tasks() == [] and stuck.close_calls == 1


async def test_an_interruption_cancels_the_old_reader_and_only_one_reader_runs_after_the_reconnect():
    r = Rig()
    stuck = StuckWs()
    r.transport.ws_script.append(stuck)
    await r.start()
    await r.advance(90)  # 静默 -> 中断
    assert r.stream.state == "interrupted" and reader_tasks() == [] and stuck.close_calls == 1
    await r.advance(1)  # 重连成功
    assert r.stream.state == "connected" and len(reader_tasks()) == 1


async def test_a_listen_key_inside_a_transport_error_text_is_masked_in_the_reason_and_the_log(caplog):
    r = Rig()
    r.transport.ws_script.append(TransportConnectionError(
        f"handshake failed: ws://127.0.0.1:18220/ws/stream?listen_key={SECRET_PREFIX}1&x=1"))
    with caplog.at_level(logging.DEBUG):
        await r.start()
    reason = r.status("interrupted")[0].reason
    assert SECRET_PREFIX not in reason and "***" in reason and "handshake failed" in reason
    assert caplog.records and not any(SECRET_PREFIX in rec.getMessage() for rec in caplog.records)
    r.transport.ws_script.append(TransportConnectionError(f"bad request for {SECRET_PREFIX}2"))  # 不带 listen_key= 前缀
    await r.advance(1)
    assert r.stream.state == "interrupted"
    assert not any(SECRET_PREFIX in rec.getMessage() for rec in caplog.records)
    assert r.stream.last_error and SECRET_PREFIX not in r.stream.last_error


# ================================================================ 恢复稳定

async def test_recovered_stable_follows_a_reconnect_after_the_stable_window_only():
    r = Rig()
    await r.start()
    await r.advance(120, beat=True)
    assert r.states() == ["connected"]  # 首次连接不算「恢复」
    r.transport.ws.peer_close()
    await r.settle()
    await r.advance(1)
    assert r.states() == ["connected", "interrupted", "connected"]
    await r.advance(59.999, beat=True)
    assert r.states()[-1] == "connected"
    await r.advance(0.001, beat=True)
    assert r.states() == ["connected", "interrupted", "connected", "recovered_stable"]
    await r.advance(600, beat=True)
    assert r.states().count("recovered_stable") == 1  # 只发一次


async def test_an_interruption_before_the_window_ends_restarts_the_stable_window():
    r = Rig()
    await r.start()
    r.transport.ws.peer_close()
    await r.settle()
    await r.advance(1)  # 重连成功
    await r.advance(30, beat=True)
    r.transport.ws.peer_close()
    await r.settle()
    await r.advance(1)  # 再次重连成功，稳定窗口重新计
    await r.advance(59, beat=True)
    assert "recovered_stable" not in r.states()
    await r.advance(1, beat=True)
    assert r.states() == ["connected", "interrupted", "connected", "interrupted", "connected", "recovered_stable"]


async def test_stable_window_is_read_from_the_config():
    r = Rig(stream_recover_stable_s=5)
    await r.start()
    r.transport.ws.peer_close()
    await r.settle()
    await r.advance(1)
    r.config["stream_recover_stable_s"] = 20
    await r.advance(19, beat=True)
    assert "recovered_stable" not in r.states()
    await r.advance(1, beat=True)
    assert r.states()[-1] == "recovered_stable"


# ================================================================ listenKey 失效

async def test_listen_key_expired_frame_publishes_the_event_and_rebuilds_at_once():
    r = Rig()
    await r.start()
    old = r.transport.ws
    old.push(EXPIRED)
    await r.settle()  # 不调判活：读循环立刻重建
    assert [e.at_ms for e in r.of(ListenKeyExpired)] == [T0_MS]
    assert r.states() == ["connected", "interrupted", "connected"]
    assert "listenKey" in r.status("interrupted")[0].reason
    assert r.transport.posts == 2 and len(r.transport.ws_connects) == 2
    assert r.transport.ws_connects[1][1] == {"listen_key": f"{SECRET_PREFIX}2"}  # 用的是新建的 key
    assert r.transport.ws is not old and r.stream.state == "connected"
    r.transport.ws.push(ORDER_FRAME)  # 新连接上的帧照常处理
    await r.settle()
    assert len(r.of(StreamOrderReceived)) == 1


async def test_rebuild_after_expiry_that_fails_keeps_retrying_with_backoff():
    r = Rig()
    await r.start()
    r.transport.post_script.append(Response(503, {"code": -1007, "msg": "x"}, {}))
    r.transport.ws.push(EXPIRED)
    await r.settle()
    assert r.stream.state == "interrupted" and r.states() == ["connected", "interrupted"]
    await r.advance(1)
    assert r.stream.state == "connected" and r.states() == ["connected", "interrupted", "connected"]


# ================================================================ items()


async def take(it):
    return await asyncio.wait_for(anext(it), 1)


async def test_items_yields_typed_records_and_skips_heartbeats():
    r = Rig()
    await r.start()
    it = r.stream.items()
    first = asyncio.create_task(take(it))
    await r.settle()
    for frame in (HEARTBEAT, ORDER_FRAME, ALGO_FRAME, ACCOUNT_FRAME):
        r.transport.ws.push(frame)
    await r.settle()
    got = [await first, await take(it), await take(it)]
    assert [type(x) for x in got] == [StreamOrder, StreamAlgo, StreamAccount]
    assert got[0].order.exchange_order_id == "9001"


async def test_two_consumers_each_get_every_item_and_stop_ends_them():
    r = Rig()
    await r.start()
    a, b = r.stream.items(), r.stream.items()
    ta, tb = asyncio.create_task(take(a)), asyncio.create_task(take(b))
    await r.settle()
    r.transport.ws.push(ORDER_FRAME)
    await r.settle()
    assert isinstance(await ta, StreamOrder) and isinstance(await tb, StreamOrder)
    waiting = asyncio.create_task(take(a))
    await r.settle()
    await r.stream.stop()
    with pytest.raises(StopAsyncIteration):
        await waiting
    with pytest.raises(StopAsyncIteration):
        await take(b)
