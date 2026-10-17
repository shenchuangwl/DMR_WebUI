"""替身交易所的 HTTP/WebSocket 外壳（FastAPI）。协议全部在 ``dispatcher``：这里只做传输。

- 任何 ``GET/POST/PUT/DELETE`` 请求原样交给 ``Dispatcher.handle``，把它返回的 ``(status, body, headers)`` 编成 JSON 响应。
- ``Disconnect``（``order_timeout`` 的 drop 故障）：只发响应头、不发响应体就结束，服务器随即断开连接，
  客户端读不到完整响应（ASGI 无法在发响应头之前断开，这是能做到的最接近「连接被断开」的表现）。
- ``/ws/stream?listen_key=``：``Dispatcher.subscribe``；被拒绝时在握手阶段以 1008 关闭；之后把订阅的帧逐条以文本 JSON 发出。
  服务端断开（listenKey 失效 1000、断线故障或停机 1001）时关闭连接；客户端断开时撤销订阅。
- ``background=True``（缺省）时随应用启停 ``Dispatcher.start()/stop()``；测试用 ``background=False`` 自己驱动。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator

import anyio
from fastapi import FastAPI, Request, WebSocket
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from .dispatcher import Dispatcher, Disconnect
from .stream import StreamClosed, StreamRefused, Subscription


def _dumps(body: Any) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


class _DroppedResponse(Response):
    """只发响应头（不带 content-length）就结束：服务器发现响应不完整，直接关闭连接。"""

    def __init__(self, headers: dict[str, str]):
        super().__init__(content=None, status_code=200)
        self._drop_headers = [(b"content-type", b"application/json")] + [
            (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": self._drop_headers})


async def _pump(ws: WebSocket, sub: Subscription) -> None:
    """把订阅的帧转发给客户端，同时盯着客户端是否断开；任一方结束就收尾。

    两个循环放在 anyio 的任务组里（而不是裸的 asyncio 任务），这样外部取消（服务器关停、测试客户端退出时取消会话）
    会按 anyio 的取消范围正常传递并被吸收，不会以 CancelledError 漏出去；撤销订阅放在 finally 里，任何结局都执行。
    """
    reason: str | None = None

    async def forward(scope: anyio.CancelScope) -> None:
        nonlocal reason
        try:
            while True:
                frame = await sub.get()
                await ws.send_text(_dumps(frame))
        except StreamClosed as exc:
            reason = str(exc) or "closed"
        scope.cancel()

    async def watch(scope: anyio.CancelScope) -> None:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
        scope.cancel()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(forward, tg.cancel_scope)
            tg.start_soon(watch, tg.cancel_scope)
    finally:
        sub.close("connection_closed")
    if reason is not None:
        with suppress(Exception):
            await ws.close(code=1000 if reason == "listen_key_expired" else 1001)


def create_app(disp: Dispatcher, *, background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if background:
            await disp.start()
        try:
            yield
        finally:
            if background:
                await disp.stop()

    app = FastAPI(title="DMR 替身交易所（只用于开发与验收）", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    @app.websocket("/ws/stream")
    async def stream(ws: WebSocket) -> None:
        try:
            sub = disp.subscribe(ws.query_params.get("listen_key", ""))
        except StreamRefused:
            await ws.close(code=1008)
            return
        await ws.accept()
        await _pump(ws, sub)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False)
    async def http(request: Request, path: str) -> Response:
        raw = await request.body()
        try:
            status, body, headers = await disp.handle(request.method, "/" + path, dict(request.query_params),
                                                      raw or None, dict(request.headers))
        except Disconnect:
            return _DroppedResponse({"X-MBX-USED-WEIGHT-1M": str(disp.used_weight())})
        return Response(content=_dumps(body), status_code=status, headers=headers, media_type="application/json")

    return app
