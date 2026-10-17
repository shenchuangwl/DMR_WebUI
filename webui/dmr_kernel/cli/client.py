"""命令行的 HTTP 客户端（第 7 层 cli）：只经控制接口与内核说话，不打开数据库，只用标准库（不给内核环境添依赖）。

- ``KernelClient(transport, token)``：路径相对 ``/api/v1``；带本机令牌的 ``Authorization: Bearer`` 头；JSON 进 JSON 出。
  内核答复了错误（4xx/5xx）抛 ``KernelError(status, code, message)``，``message`` 是内核给的中文原因；连不上或中途出错抛
  ``KernelUnreachable``。令牌只出现在请求头里：不进 repr、不进任何错误文字。
- ``KernelUnreachable.kind``：``connect`` = 连接没建立（请求肯定没到内核）；``io`` = 连接建立之后出错或超时（请求可能已被处理）。
  需要「做没做成」的调用（创建操作员）靠它区分「肯定失败」与「结果不确定」。
- ``Transport`` 是个协议：真实的是 ``HttpTransport``（``http.client``）；测试里换成假的，不用开套接字。
"""
from __future__ import annotations

import http.client
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "API_PREFIX", "CONNECT_TIMEOUT_S", "HttpResponse", "HttpTransport", "KernelClient", "KernelError",
    "KernelUnreachable", "MAX_BODY", "REQUEST_TIMEOUT_S", "Transport",
]

API_PREFIX = "/api/v1"
CONNECT_TIMEOUT_S = 3.0  # 本机回环：连不上就是没在听，不用等
REQUEST_TIMEOUT_S = 30.0  # 等答复（创建操作员要算一次 scrypt，约零点几秒）
MAX_BODY = 4 * 1024 * 1024  # 答复体上限；/state 只有几十 KB
MAX_MESSAGE = 500  # 内核报文里的 message 最长显示多少字


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    def request(self, method: str, path: str, *, headers: Mapping[str, str],
                body: bytes | None = None) -> HttpResponse:
        """发一个请求，返回答复（不论状态码）；连不上或中途出错抛 ``KernelUnreachable``。"""
        ...


class KernelUnreachable(Exception):
    KINDS = ("connect", "io")

    def __init__(self, kind: str, detail: str) -> None:
        if kind not in self.KINDS:
            raise ValueError(f"kind 只能是 {self.KINDS}：{kind!r}")
        super().__init__(detail)
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class KernelError(Exception):
    """内核答复了错误。``message`` 是内核给的中文原因（认不出报文时是「内核答复了 HTTP nnn」）。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _describe(exc: BaseException) -> str:
    """连接与读写出错的中文说明。不带异常原文（原文里可能有地址、请求头一类的东西）。"""
    if isinstance(exc, ConnectionRefusedError):
        return "连接被拒绝"
    if isinstance(exc, TimeoutError):
        return "等待超时"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return "连接被中断"
    if isinstance(exc, http.client.HTTPException):
        return "内核的答复不是合法的 HTTP"
    if isinstance(exc, OSError):
        return f"网络错误（{exc.strerror or type(exc).__name__}）"
    return type(exc).__name__


class HttpTransport:
    """``http.client`` 的薄封装：每个请求一条连接，先用短超时连接，连上之后改用长超时等答复。"""

    def __init__(self, host: str, port: int, *, connect_timeout_s: float = CONNECT_TIMEOUT_S,
                 timeout_s: float = REQUEST_TIMEOUT_S) -> None:
        self._host = host
        self._port = port
        self._connect_timeout_s = connect_timeout_s
        self._timeout_s = timeout_s

    def request(self, method: str, path: str, *, headers: Mapping[str, str],
                body: bytes | None = None) -> HttpResponse:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._connect_timeout_s)
        try:
            try:
                conn.connect()
            except (OSError, http.client.HTTPException) as exc:
                raise KernelUnreachable("connect", _describe(exc)) from None
            conn.sock.settimeout(self._timeout_s)
            try:
                conn.request(method, path, body=body, headers=dict(headers))
                reply = conn.getresponse()
                data = reply.read(MAX_BODY + 1)
            except (OSError, http.client.HTTPException) as exc:
                raise KernelUnreachable("io", _describe(exc)) from None
            if len(data) > MAX_BODY:
                raise KernelUnreachable("io", "内核的答复过大")
            return HttpResponse(reply.status, data)
        finally:
            conn.close()


def _parse(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _kernel_error(status: int, data: Any) -> KernelError:
    if isinstance(data, Mapping) and isinstance(data.get("code"), str) and isinstance(data.get("message"), str):
        message = data["message"]
        if len(message) > MAX_MESSAGE:
            message = message[:MAX_MESSAGE] + "…"
        return KernelError(status, data["code"], message)
    return KernelError(status, "unknown", f"内核答复了 HTTP {status}")


class KernelClient:
    def __init__(self, transport: Transport, token: str) -> None:
        self._transport = transport
        self._token = token

    def __repr__(self) -> str:
        return "KernelClient(...)"  # 令牌不进 repr

    def get(self, path: str) -> dict[str, Any]:
        """GET；答复必须是 JSON 对象，否则按「没拿到完整的答复」处理。"""
        _, data = self._call("GET", path, None)
        if not isinstance(data, dict):
            raise KernelUnreachable("io", "内核的答复不是 JSON 对象")
        return data

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        """POST；成功（2xx）就返回解析出的 JSON（读不懂就是 None：状态码已经说明内核处理了）。"""
        return self._call("POST", path, payload)[1]

    def _call(self, method: str, path: str, payload: Mapping[str, Any] | None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = self._transport.request(method, API_PREFIX + path, headers=headers, body=body)
        data = _parse(response.body)
        if 200 <= response.status < 300:
            return response.status, data
        raise _kernel_error(response.status, data)
