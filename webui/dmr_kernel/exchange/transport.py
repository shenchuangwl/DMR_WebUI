"""适配层与替身交易所之间的传输（第 1 层）。

``Transport`` 是适配层看到的唯一「线路」接口：一个请求方法与一个数据流连接方法。真实实现 ``HttpTransport`` 走回环 HTTP 与
WebSocket（aiohttp）；测试用 ``tests/support/inproc.py`` 里的进程内实现，直接调替身交易所的分发器，不开任何套接字。

约定：
- ``request`` 只在拿到完整响应时返回 ``Response``（HTTP 4xx/5xx 也是 ``Response``，由适配层按状态与错误码归类）；
  超时抛 ``TransportTimeout``，连接失败或响应中途断开抛 ``TransportConnectionError``。两者执行状态都「未知」。
- 查询参数的值一律转成字符串，值为 ``None`` 的不发。
- 认证头 ``X-Standin-Key``：``auth=True`` 且构造时给了令牌才带。令牌是替身交易所自己生成的（``var/standin/kernel.token``），
  由调用方读出后传入；它不是 DMR 的任何密钥。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

import aiohttp

log = logging.getLogger(__name__)

AUTH_HEADER = "X-Standin-Key"

# aiohttp 的错误文字会带完整网址；数据流的网址里有 listenKey（凭据），错误文字不能把它带出去
_SECRET_QUERY = re.compile(r"((?:listen_?key|signature|api_?key)=)[^&\s'\"]+", re.IGNORECASE)


def _redact(text: str) -> str:
    return _SECRET_QUERY.sub(r"\1***", text)


@dataclass(frozen=True)
class Response:
    status: int
    body: Any  # 已解析的 JSON；空体为 None；解析不了的保留原文字符串
    headers: Mapping[str, str]


class TransportError(Exception):
    """传输层失败：请求有没有被对方处理是未知的。"""


class TransportTimeout(TransportError):
    """超过时限没有（完整的）响应。"""


class TransportConnectionError(TransportError):
    """连接失败、被对端断开，或响应读到一半中断。"""


@runtime_checkable
class WsConnection(Protocol):
    async def receive(self, timeout_s: float | None) -> str | None:
        """下一条文本消息；对端已关闭返回 None；超过时限抛 ``TransportTimeout``。"""

    async def close(self) -> None: ...


@runtime_checkable
class Transport(Protocol):
    async def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                      auth: bool = True, timeout_s: float) -> Response: ...

    async def ws_connect(self, path: str, *, params: dict, timeout_s: float) -> WsConnection: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------- aiohttp 实现


def _query(params: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not params:
        return None
    return {str(k): str(v) for k, v in params.items() if v is not None}


def _decode_body(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


class _AiohttpWs:
    def __init__(self, ws: Any):
        self._ws = ws

    async def receive(self, timeout_s: float | None) -> str | None:
        try:
            while True:
                msg = await self._ws.receive(timeout_s)
                kind = msg.type
                if kind == aiohttp.WSMsgType.TEXT:
                    return msg.data
                if kind in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    return None
                if kind == aiohttp.WSMsgType.ERROR:
                    raise TransportConnectionError(f"数据流连接出错：{_redact(repr(msg.data))}")
                # 二进制、ping/pong 等：忽略，继续等文本消息
        except TimeoutError as exc:
            raise TransportTimeout(f"数据流 {timeout_s} 秒内没有消息") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise TransportConnectionError(f"数据流读取失败：{type(exc).__name__}: {_redact(str(exc))}") from None

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception as exc:  # 关闭时的错误只记录
            log.debug("关闭数据流连接时出错：%s", exc)


class HttpTransport:
    """aiohttp：一个 ``ClientSession`` 长连接复用；每个请求既有 ``ClientTimeout(total)`` 又有外层 ``asyncio.wait_for``。

    会话在第一次使用时才创建（要在事件循环里）。``session_factory`` 只给测试用，缺省建真正的 aiohttp 会话。
    """

    def __init__(self, base_url: str, *, token: str | None, connect_timeout_s: float = 5.0,
                 session_factory: Callable[[], Any] | None = None):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._connect_timeout_s = connect_timeout_s
        self._session_factory = session_factory
        self._session: Any = None

    def _get_session(self) -> Any:
        if self._session is None:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                # 会话级不设总时限（数据流连接会长时间保持）；每个请求自己带时限
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=None, connect=self._connect_timeout_s))
        return self._session

    def _ws_url(self, path: str) -> str:
        base = self._base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return base + path

    async def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                      auth: bool = True, timeout_s: float) -> Response:
        session = self._get_session()
        headers: dict[str, str] = {}
        if auth and self._token:
            headers[AUTH_HEADER] = self._token
        timeout = aiohttp.ClientTimeout(total=timeout_s, connect=min(self._connect_timeout_s, timeout_s))

        async def send() -> Response:
            async with session.request(method, self._base_url + path, params=_query(params), json=body,
                                       headers=headers, timeout=timeout) as resp:
                raw = await resp.read()
                return Response(resp.status, _decode_body(raw), dict(resp.headers))

        try:
            return await asyncio.wait_for(send(), timeout_s)
        except TimeoutError as exc:  # asyncio.TimeoutError 与 aiohttp 的各种超时都是它的子类或别名
            raise TransportTimeout(f"{method} {path} 超过 {timeout_s} 秒没有响应") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise TransportConnectionError(f"{method} {path} 连接失败：{type(exc).__name__}: {_redact(str(exc))}") from None

    async def ws_connect(self, path: str, *, params: dict, timeout_s: float) -> WsConnection:
        session = self._get_session()
        try:
            ws = await asyncio.wait_for(session.ws_connect(self._ws_url(path), params=_query(params)), timeout_s)
        except TimeoutError as exc:
            raise TransportTimeout(f"连接数据流 {path} 超过 {timeout_s} 秒") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise TransportConnectionError(f"连接数据流 {path} 失败：{type(exc).__name__}: {_redact(str(exc))}") from None
        return _AiohttpWs(ws)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()
