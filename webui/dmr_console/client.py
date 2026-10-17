"""内核控制接口的异步客户端（只经 HTTP 调内核，不导入 dmr_kernel）。

报文约定见 docs/plans/M1-控制接口只读部分.md。三类结果：

- 成功：返回解析后的 JSON（列表接口返回 ``items``）。
- ``KernelError``：内核回了错误信封 ``{"code","message"}``（也包括没有信封的 4xx）。内核是活着的。
- ``KernelUnreachable``：连不上、超时、响应无法解析、5xx 但没有信封、被重定向。控制台据此判定「与内核失联」，
  失联时全部按钮禁用。``kind`` 取 connect / timeout / protocol。

令牌与口令只放进请求，不进异常文字、日志或 repr。每次请求有总时限（``timeout_s``），不依赖传输自己的超时。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from dmr_console.settings import is_loopback_host

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 3.0
DEFAULT_CONNECT_TIMEOUT_S = 1.0

_KIND_TEXT = {"connect": "无法连接内核", "timeout": "请求内核超时", "protocol": "内核响应无法解析"}


class KernelUnreachable(Exception):
    """与内核失联。``reason`` 是给人看的中文原因，不含令牌或口令。"""

    def __init__(self, reason: str, kind: str = "connect") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


class KernelError(Exception):
    """内核回了错误：HTTP 状态、英文代码、中文原因，以及信封里的其它字段（如 retry_after_s）。"""

    def __init__(self, status: int, code: str, message: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.extra = dict(extra or {})

    @property
    def is_session_invalid(self) -> bool:
        """会话令牌已失效（过期或被撤销）：需要重新登录。登录口令错误（bad_credentials）不算。"""
        return self.code == "unauthorized"

    @property
    def retry_after_s(self) -> int | float | None:
        value = self.extra.get("retry_after_s")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class LoginResult:
    token: str = field(repr=False)
    operator: str
    session_id: str
    idle_timeout_min: float | int | None = None


class KernelApi(Protocol):
    """页面与控制器依赖的接口；测试用同形的假客户端替换。"""

    async def health(self) -> dict: ...
    async def login(self, username: str, password: str) -> LoginResult: ...
    async def logout(self, token: str) -> None: ...
    async def me(self, token: str) -> dict: ...
    async def state(self, token: str) -> dict: ...
    async def positions(self, token: str) -> list[dict]: ...
    async def positions_external(self, token: str) -> list[dict]: ...
    async def orders(self, token: str) -> dict: ...
    async def oplog(self, token: str, *, limit: int = 100, before_id: int | None = None) -> list[dict]: ...
    async def alerts(self, token: str, *, limit: int = 100) -> list[dict]: ...
    async def config(self, token: str) -> list[dict]: ...
    async def watchlist(self, token: str) -> dict: ...
    async def trades(self, token: str, **query: Any) -> dict: ...
    async def stats(self, token: str, **query: Any) -> dict: ...
    async def candles(self, token: str, **query: Any) -> dict: ...
    async def post(self, token: str, path: str, body: Any = None) -> dict: ...
    async def put_config(self, token: str, key: str, body: Any) -> dict: ...
    async def operation(self, token: str, op_id: str) -> dict: ...
    async def aclose(self) -> None: ...


def _unreachable(kind: str, detail: str = "") -> KernelUnreachable:
    text = _KIND_TEXT[kind] + (f"（{detail}）" if detail else "")
    return KernelUnreachable(text, kind)


def _classify(exc: BaseException) -> KernelUnreachable:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return _unreachable("timeout")
    return _unreachable("connect", type(exc).__name__)


class KernelClient:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S, connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "http" or not is_loopback_host(parts.hostname or ""):
            raise ValueError(f"内核地址只允许回环上的 http，拒绝 {base_url!r}（令牌不出本机）")
        self._timeout_s = timeout_s
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            transport=transport,
            timeout=httpx.Timeout(timeout_s, connect=min(connect_timeout_s, timeout_s)),
            headers={"Accept": "application/json"},
            follow_redirects=False,
            trust_env=False,  # 不走环境里的代理：令牌不经任何中间人
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ 底层

    async def _call(self, method: str, path: str, *, token: str | None = None, params: dict[str, Any] | None = None,
                    body: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        try:
            response = await asyncio.wait_for(
                self._http.request(method, path.lstrip("/"), headers=headers, params=params, json=body),
                timeout=self._timeout_s,
            )
        except (TimeoutError, httpx.HTTPError, OSError) as exc:
            raise _classify(exc) from None
        log.debug("kernel %s %s -> %s", method, path, response.status_code)
        return self._interpret(response)

    @staticmethod
    def _interpret(response: httpx.Response) -> Any:
        status = response.status_code
        try:
            payload = json.loads(response.content)
        except ValueError:
            payload = None
            parsed = False
        else:
            parsed = True
        if 200 <= status < 300:
            if not parsed:
                raise _unreachable("protocol", f"HTTP {status}，正文不是 JSON")
            return payload
        if status >= 400:
            if parsed and isinstance(payload, dict) and isinstance(payload.get("code"), str):
                extra = {k: v for k, v in payload.items() if k not in ("code", "message")}
                message = payload.get("message")
                raise KernelError(status, payload["code"], message if isinstance(message, str) else "", extra)
            if status < 500:
                detail = payload.get("detail") if parsed and isinstance(payload, dict) else None
                raise KernelError(status, f"http_{status}", detail if isinstance(detail, str) else f"内核返回 HTTP {status}")
        raise _unreachable("protocol", f"HTTP {status}")

    @staticmethod
    def _object(payload: Any, what: str) -> dict:
        if not isinstance(payload, dict):
            raise _unreachable("protocol", f"{what}不是 JSON 对象")
        return payload

    @classmethod
    def _items(cls, payload: Any, what: str) -> list[dict]:
        items = cls._object(payload, what).get("items")
        if not isinstance(items, list):
            raise _unreachable("protocol", f"{what}缺少 items 列表")
        return items

    # ------------------------------------------------------------ 会话

    async def health(self) -> dict:
        return self._object(await self._call("GET", "/health"), "health")

    async def login(self, username: str, password: str) -> LoginResult:
        payload = self._object(await self._call("POST", "/auth/login", body={"username": username, "password": password}),
                               "登录响应")
        token, operator, session_id = payload.get("token"), payload.get("operator"), payload.get("session_id")
        # 内核的会话号是整数（会话表的行号）；也接受字符串，统一按字符串保存。布尔值不算。
        if isinstance(session_id, int) and not isinstance(session_id, bool):
            session_id = str(session_id)
        if not (isinstance(token, str) and token and isinstance(operator, str) and isinstance(session_id, str) and session_id):
            raise _unreachable("protocol", "登录响应缺少字段")
        idle = payload.get("idle_timeout_min")
        return LoginResult(token=token, operator=operator, session_id=session_id,
                           idle_timeout_min=idle if isinstance(idle, (int, float)) and not isinstance(idle, bool) else None)

    async def logout(self, token: str) -> None:
        try:
            await self._call("POST", "/auth/logout", token=token)
        except KernelError as exc:
            if not exc.is_session_invalid:  # 会话本来就已失效：登出的目的已经达到
                raise

    async def me(self, token: str) -> dict:
        return self._object(await self._call("GET", "/auth/me", token=token), "me")

    # ------------------------------------------------------------ 只读

    async def state(self, token: str) -> dict:
        return self._object(await self._call("GET", "/state", token=token), "state")

    async def positions(self, token: str) -> list[dict]:
        return self._items(await self._call("GET", "/positions", token=token), "positions")

    async def positions_external(self, token: str) -> list[dict]:
        return self._items(await self._call("GET", "/positions/external", token=token), "positions/external")

    async def orders(self, token: str) -> dict:
        return self._object(await self._call("GET", "/orders", token=token), "orders")

    async def oplog(self, token: str, *, limit: int = 100, before_id: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if before_id is not None:
            params["before_id"] = before_id
        return self._items(await self._call("GET", "/oplog", token=token, params=params), "oplog")

    async def alerts(self, token: str, *, limit: int = 100) -> list[dict]:
        return self._items(await self._call("GET", "/alerts", token=token, params={"limit": limit}), "alerts")

    async def config(self, token: str) -> list[dict]:
        return self._items(await self._call("GET", "/config", token=token), "config")

    async def watchlist(self, token: str) -> dict:
        """``GET /watchlist``：选币名单的只读视图（内核自己拉取并校验名单，控制台不碰名单接口）。"""
        return self._object(await self._call("GET", "/watchlist", token=token), "watchlist")

    @staticmethod
    def _params(query: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in query.items() if v is not None and v != ""}

    async def trades(self, token: str, **query: Any) -> dict:
        """``GET /trades``：筛选条件（symbol ownership model side since_ms until_ms limit order）只发设了值的。返回 ``{"items","total"}``。"""
        return self._object(await self._call("GET", "/trades", token=token, params=self._params(query)), "trades")

    async def stats(self, token: str, **query: Any) -> dict:
        """``GET /stats``（group_by since_ms until_ms）。返回 ``{"overall","groups","mixed"}``。"""
        return self._object(await self._call("GET", "/stats", token=token, params=self._params(query)), "stats")

    async def candles(self, token: str, **query: Any) -> dict:
        """``GET /candles``（symbol tf limit）。返回 ``{"candles","dmr","trades","stops"}``。"""
        return self._object(await self._call("GET", "/candles", token=token, params=self._params(query)), "candles")

    # ------------------------------------------------------------ 操作（改变状态；报文约定见 M2 计划第 2 节）

    async def post(self, token: str, path: str, body: Any = None) -> dict:
        """操作类与席位接口的 POST。返回内核给的 JSON 对象（成功信封 ``{"op_id","status","result"}`` 或席位报文）。"""
        return self._object(await self._call("POST", path, token=token, body=body), f"{path} 响应")

    async def put_config(self, token: str, key: str, body: Any) -> dict:
        """``PUT /config/{key}``，``body`` 是 ``{"op_id","value"}``。返回 ``{key, old, new, applies, affects_existing}``。"""
        return self._object(await self._call("PUT", f"/config/{quote(key, safe='')}", token=token, body=body), "config 响应")

    async def operation(self, token: str, op_id: str) -> dict:
        return self._object(await self._call("GET", f"/operations/{quote(op_id, safe='')}", token=token), "operations")
