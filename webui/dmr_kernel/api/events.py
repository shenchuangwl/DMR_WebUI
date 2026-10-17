"""事件流（第 7 层 api）：``WS /api/v1/events?token=<令牌>``。

服务端只推、不收：把内核事件映射成「刷新提示」JSON 文本帧，控制台收到后重新拉对应的只读接口
（docs/plans/M1-控制接口只读部分.md 第 4 节）。每帧都有 ``at_ms``。

- 握手：令牌无效（缺、错、撤销、过期、操作员停用）时在 accept 之前关闭，关闭码 4401（真实服务器上表现为握手被拒）。
  会话令牌与本机令牌都可以；握手不算会话活动（``touch=False``），连着事件流不会让闲置的会话续命。
- ``EventHub`` 订阅总线，给每个连接一个有界队列；队列满了说明消费者太慢，清空积压、只留一个关闭信号并判出局
  （1013），控制台自行重连并全量刷新，所以不丢状态；已出局的连接不再收帧。
- 每 ``ws_ping_s`` 秒（默认 15）发一次 ``ping``，同一时刻重新核对令牌：退出登录、闲置过期、停用后事件流以 4401 关闭。
- 本地事件 ``SeatChanged`` 映射成 ``state`` 帧，其它控制台据此刷新席位显示。
- 每个连接的写循环与读循环放在 anyio 任务组里，不用裸的 asyncio 任务：服务器关停、starlette TestClient 退出会话
  （先发 disconnect，紧接着取消会话外面的 anyio 取消范围）这类外部取消会沿取消范围正常传递并被外层吸收，不会以
  CancelledError 漏出去；任一循环结束（含出错）就取消任务组，收尾放在 finally（先撤销登记，再尽力关闭）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, Final

import anyio
from starlette.websockets import WebSocket, WebSocketDisconnect

from dmr_kernel.api.common import Principal, SeatChanged
from dmr_kernel.api.errors import ApiError
from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import (
    AlertRaised,
    ConfigChanged,
    Event,
    EventBus,
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
    Subscription,
)

log = logging.getLogger(__name__)

__all__ = ["CLOSE", "CLOSE_AUTH", "CLOSE_OVERLOAD", "Connection", "EventHub", "events_endpoint"]

CLOSE_AUTH = 4401  # 令牌无效
CLOSE_OVERLOAD = 1013  # 消费者太慢，请稍后重连
CLOSE: Final = object()  # 放进队列的关闭信号

_STATE_EVENTS = (RiskStateChanged, StreamStatusChanged, ReconcileCompleted, ConfigChanged, SeatChanged)
_POSITION_EVENTS = (PositionRegistered, PositionChanged, PositionClosed, ProtectionChanged)
_EVENTS = (ModelStateChanged, OrderStateChanged, IntentStateChanged, OperationProgress,
           AlertRaised) + _STATE_EVENTS + _POSITION_EVENTS


class Connection:
    def __init__(self, principal: Principal, max_queue: int) -> None:
        self.principal = principal
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_queue)
        self.overflowed = False


class EventHub:
    def __init__(self, bus: EventBus, clock: Clock, *, max_queue: int = 1000) -> None:
        if max_queue < 2:
            raise ValueError("max_queue 至少为 2（要留一个位置给关闭信号）")
        self._bus = bus
        self._clock = clock
        self._max_queue = max_queue
        self._sub: Subscription | None = None
        self._conns: list[Connection] = []

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """订阅总线（重复调用无害）。在事件循环里调用。"""
        if self._sub is None:
            self._sub = self._bus.subscribe(_EVENTS, self._on_event, name="api-events")

    def stop(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None

    # ------------------------------------------------------------ 连接

    @property
    def connection_count(self) -> int:
        return len(self._conns)

    def connect(self, principal: Principal) -> Connection:
        conn = Connection(principal, self._max_queue)
        self._conns.append(conn)
        return conn

    def disconnect(self, conn: Connection) -> None:
        if conn in self._conns:
            self._conns.remove(conn)

    # ------------------------------------------------------------ 帧

    def ping_frame(self) -> dict[str, Any]:
        return {"type": "ping", "at_ms": self._clock.now_ms()}

    def frame_for(self, event: Event) -> dict[str, Any] | None:
        """事件 -> 刷新提示帧；不是刷新提示的事件返回 None。"""
        at_ms = getattr(event, "at_ms", 0) or self._clock.now_ms()
        if isinstance(event, ModelStateChanged):
            return {"type": "state", "version": event.version, "at_ms": at_ms}
        if isinstance(event, _STATE_EVENTS):
            return {"type": "state", "version": None, "at_ms": at_ms}
        if isinstance(event, _POSITION_EVENTS):
            return {"type": "position", "position_id": event.position_id, "at_ms": at_ms}
        if isinstance(event, OrderStateChanged):
            return {"type": "order", "client_order_id": event.client_order_id, "intent_id": event.intent_id,
                    "at_ms": at_ms}
        if isinstance(event, IntentStateChanged):
            return {"type": "order", "client_order_id": None, "intent_id": event.intent_id, "at_ms": at_ms}
        if isinstance(event, OperationProgress):
            return {"type": "operation", "op_id": event.op_id, "status": event.status, "at_ms": at_ms}
        if isinstance(event, AlertRaised):
            return {"type": "alert", "level": event.level.value, "kind": event.kind, "message": event.message,
                    "at_ms": at_ms}
        return None

    def _on_event(self, event: Event) -> None:
        frame = self.frame_for(event)
        if frame is None:
            return
        for conn in list(self._conns):
            if conn.overflowed:
                continue
            try:
                conn.queue.put_nowait(frame)
            except asyncio.QueueFull:
                conn.overflowed = True
                while not conn.queue.empty():
                    conn.queue.get_nowait()
                conn.queue.put_nowait(CLOSE)
                log.warning("事件流消费者太慢，已断开（操作员 %s）", conn.principal.operator)


# ---------------------------------------------------------------- WebSocket 端点


def _text(frame: dict[str, Any]) -> str:
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


# 对端已经走了（或传输层断开、starlette 因连接状态不对抛 RuntimeError 的子类）是连接的正常结局；其它异常是程序错误
_CONNECTION_ENDED = (WebSocketDisconnect, OSError, RuntimeError)


def _loop_ended(loop_name: str, exc: Exception) -> None:
    if isinstance(exc, _CONNECTION_ENDED):
        log.debug("事件流%s循环结束：%r", loop_name, exc)
    else:
        log.error("事件流%s循环意外结束（这条连接断开，控制台会重连）", loop_name, exc_info=exc)


async def _writer(ws: WebSocket, conn: Connection, hub: EventHub, auth: Any, token: str, ping_s: float,
                  scope: anyio.CancelScope) -> None:
    """写循环：从连接的队列取帧发给控制台，每 ``ping_s`` 秒 ping 一次并核对令牌。结束（关闭、出错）就取消任务组。"""
    next_ping = anyio.current_time() + ping_s
    try:
        while True:
            now = anyio.current_time()
            if now >= next_ping:  # 事件再密也要按时 ping
                next_ping = now + ping_s
                try:
                    await auth.authenticate(token, touch=False)  # 退出、过期、停用：事件流跟着关
                except ApiError:
                    await ws.close(code=CLOSE_AUTH)
                    return
                except Exception:
                    log.exception("事件流核对令牌时出错（稍后重试）")
                item: Any = hub.ping_frame()
            else:
                with anyio.move_on_after(next_ping - now) as waiting:
                    item = await conn.queue.get()
                if waiting.cancelled_caught:  # 到点了：回到循环开头去 ping
                    continue
            if item is CLOSE:
                await ws.close(code=CLOSE_OVERLOAD)
                return
            await ws.send_text(_text(item))
    except Exception as exc:  # 这条连接到此为止（原因按类别记日志）
        _loop_ended("写", exc)
    finally:
        scope.cancel()


async def _reader(ws: WebSocket, scope: anyio.CancelScope) -> None:
    """读循环：服务端只推不收，客户端发来的内容一律忽略，只为及时发现对端断开。结束就取消任务组。"""
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
    except Exception as exc:
        _loop_ended("读", exc)
    finally:
        scope.cancel()


async def events_endpoint(websocket: WebSocket) -> None:
    services = websocket.app.state.services
    ctx = websocket.app.state.ctx
    token = websocket.query_params.get("token")
    try:
        principal = await services.auth.authenticate(token, touch=False)
    except ApiError:
        await websocket.close(code=CLOSE_AUTH)
        return
    conn = services.hub.connect(principal)  # 先登记再 accept：客户端握手一成功，之后的事件就不会漏
    try:
        await websocket.accept()
        async with anyio.create_task_group() as group:
            group.start_soon(_writer, websocket, conn, services.hub, services.auth, token, ctx.ws_ping_s,
                             group.cancel_scope)
            group.start_soon(_reader, websocket, group.cancel_scope)
    finally:
        services.hub.disconnect(conn)  # 同步撤销登记放在最前：之后的 await 再被外部取消打断也不会漏
        with suppress(Exception):
            await websocket.close()
