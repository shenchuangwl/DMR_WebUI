"""名单接口的 HTTP 客户端（本包唯一导入 aiohttp 的地方）。

一个 ``ClientSession`` 长连接复用；每次请求有总时限；不带任何查询参数；不跟随重定向（3xx 按非 200 返回）。
状态码与正文分开交给调用方：非 200 与非 JSON 正文都不抛异常（返回 ``(状态码, None)``）；
连接失败、超时抛 ``ListFetchError``（文字不带完整网址之外的敏感信息，名单接口本身无鉴权）。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import aiohttp


class ListFetchError(Exception):
    """接口不可达（连接被拒、超时、读取中断）。"""


class AiohttpListClient:
    def __init__(self, *, connect_timeout_s: float = 5.0, session_factory: Callable[[], Any] | None = None):
        """``session_factory`` 只给测试用（测试进程不能开套接字），缺省建真正的 aiohttp 会话。"""
        self._connect_timeout_s = connect_timeout_s
        self._session_factory = session_factory
        self._session: Any = None

    def _get_session(self) -> Any:
        if self._session is None or self._session.closed:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=None, connect=self._connect_timeout_s))
        return self._session

    async def get_json(self, url: str, *, timeout_s: float) -> tuple[int, Any]:
        session = self._get_session()
        timeout = aiohttp.ClientTimeout(total=timeout_s, connect=min(self._connect_timeout_s, timeout_s))

        async def send() -> tuple[int, Any]:
            async with session.get(url, timeout=timeout, allow_redirects=False) as resp:
                raw = await resp.read()
                if resp.status != 200:
                    return resp.status, None
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, None

        try:
            return await asyncio.wait_for(send(), timeout_s)
        except TimeoutError as exc:
            raise ListFetchError(f"名单接口超过 {timeout_s:g} 秒没有响应") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise ListFetchError(f"名单接口连接失败：{type(exc).__name__}") from None

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()
