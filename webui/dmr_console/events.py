"""内核事件流（``WS /events?token=``）的客户端。

事件帧只是「刷新提示」（docs/plans/M1-控制接口只读部分.md 第 4 节）：收到后重新拉对应的只读接口，帧里的内容不当作数据。
断线后自行重连（逐级退避、封顶），**每次连上都全量刷新一次**——断线期间错过的帧靠这次刷新补上；
定时轮询是兜底，事件流断了页面也会更新，只是慢一点。

令牌在地址的查询串里：**绝不记录地址或异常文字**（aiohttp 的握手异常文字里带完整地址）；日志只写异常类型名。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

from dmr_console.settings import is_loopback_host

log = logging.getLogger(__name__)

PARTS = ("state", "positions", "external", "oplog", "alerts")

# 事件类型 → 该重新拉取的只读部分。状态条里的计数、席位等都在 state 里，所以几乎每种都带 state。
FRAME_PARTS: dict[str, frozenset[str]] = {
    "state": frozenset({"state"}),
    "position": frozenset({"state", "positions", "external"}),
    "order": frozenset({"state", "positions"}),
    "operation": frozenset({"state", "oplog"}),
    "alert": frozenset({"state", "alerts"}),
}

DEFAULT_BACKOFF_S = (0.5, 1.0, 2.0, 5.0, 10.0)
HEARTBEAT_S = 20.0  # aiohttp 的 WS 心跳：对端不回 pong 就判定连接已死
RECEIVE_TIMEOUT_S = 60.0  # 服务端每 15 秒一个 ping 帧；60 秒什么都没收到就重连
CONNECT_TIMEOUT_S = 5.0

FrameHandler = Callable[[dict], Awaitable[None]]
Connector = Callable[[str], AbstractAsyncContextManager[AsyncIterator[str]]]


def refresh_parts_for_frame(frame: Any) -> frozenset[str]:
    """一帧该触发哪些部分的重新拉取。ping 与非对象帧什么都不刷；不认识的类型宁可全刷。"""
    if not isinstance(frame, dict):
        return frozenset()
    kind = frame.get("type")
    if kind == "ping":
        return frozenset()
    if isinstance(kind, str) and kind in FRAME_PARTS:
        return FRAME_PARTS[kind]
    return frozenset(PARTS)


def build_events_url(base_url: str, token: str) -> str:
    """``http://127.0.0.1:18200/api/v1`` + 令牌 → ``ws://127.0.0.1:18200/api/v1/events?token=…``。只允许回环上的 http。"""
    parts = urlsplit(base_url)
    if parts.scheme != "http" or not is_loopback_host(parts.hostname or ""):
        raise ValueError("事件流地址只允许回环上的 http 基址（令牌不出本机）")
    return f"ws://{parts.netloc}{parts.path.rstrip('/')}/events?token={quote(token, safe='')}"


def _parse(text: Any) -> dict | None:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@asynccontextmanager
async def aiohttp_connect(url: str, *, heartbeat_s: float = HEARTBEAT_S, receive_timeout_s: float = RECEIVE_TIMEOUT_S,
                          connect_timeout_s: float = CONNECT_TIMEOUT_S,
                          session_factory: Callable[..., Any] = aiohttp.ClientSession) -> AsyncIterator[AsyncIterator[str]]:
    """真实连接器：产出文本帧的异步迭代器。服务端关闭就正常结束；出错抛异常；退出时关闭连接与会话。"""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=connect_timeout_s)
    async with session_factory(timeout=timeout, trust_env=False) as session:
        async with session.ws_connect(url, heartbeat=heartbeat_s, receive_timeout=receive_timeout_s) as ws:
            async def frames() -> AsyncIterator[str]:
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        yield message.data
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError("websocket error")
                    # 二进制等其它类型：协议里没有，忽略

            yield frames()


class EventStream:
    def __init__(self, url_factory: Callable[[], str], connect: Connector, *, on_frame: FrameHandler,
                 on_connected: Callable[[], Awaitable[None]],
                 on_disconnected: Callable[[str], Awaitable[None]] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 backoff_s: Sequence[float] = DEFAULT_BACKOFF_S) -> None:
        if not backoff_s:
            raise ValueError("backoff_s 不能为空")
        self._url_factory = url_factory
        self._connect = connect
        self._on_frame = on_frame
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._sleep = sleep
        self._backoff = tuple(backoff_s)

    async def run(self) -> None:
        """一直连接、收帧、断线重连，直到所在任务被取消。"""
        attempt = 0
        while True:
            reason = "closed"
            try:
                async with self._connect(self._url_factory()) as frames:
                    log.info("事件流已连接")
                    await self._call(self._on_connected)
                    async for text in frames:
                        attempt = 0  # 收到过帧说明这次连接是健康的：退避归零
                        frame = _parse(text)
                        if frame is not None:
                            await self._call(self._on_frame, frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何连接问题都只是「断了」；只记类型，绝不记文字（含令牌）
                reason = type(exc).__name__
                status = getattr(exc, "status", None)
                if isinstance(status, int) and not isinstance(status, bool):
                    reason += f"({status})"
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            log.info("事件流断开（%s），%.1f 秒后重连", reason, delay)
            if self._on_disconnected is not None:
                await self._call(self._on_disconnected, reason)
            await self._sleep(delay)

    @staticmethod
    async def _call(handler: Callable[..., Awaitable[None]], *args: Any) -> None:
        try:
            await handler(*args)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 回调的错不能拖垮事件流；这是我们自己的代码，值得留完整的栈
            log.exception("事件流回调出错")


def stream_factory_for(base_url: str, *, connect: Connector = aiohttp_connect, **stream_kwargs: Any) -> Callable[[Any], EventStream]:
    """给 ``LiveController`` 用的事件流工厂：地址每次连接时按控制器**当前**的令牌现拼，帧与「连上」交给控制器处理。"""
    def make(controller: Any) -> EventStream:
        return EventStream(lambda: build_events_url(base_url, controller.session.token), connect,
                           on_frame=controller.handle_frame, on_connected=controller.handle_connected, **stream_kwargs)

    return make
