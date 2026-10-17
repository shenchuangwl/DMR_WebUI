"""路由共用的依赖与小工具：取服务、鉴权、查询参数解析、JSON 出口、请求编号。"""
from __future__ import annotations

import re
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from dmr_kernel.api import errors
from dmr_kernel.api.auth import bearer_token
from dmr_kernel.api.common import Principal
from dmr_kernel.api.context import ApiContext, Services
from dmr_kernel.api.views import jsonable

__all__ = ["client_info", "ctx_of", "int_query", "ok", "request_id_of", "require_principal", "services_of"]

_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
MAX_CLIENT_INFO = 200


def services_of(request: Request) -> Services:
    return request.app.state.services


def ctx_of(request: Request) -> ApiContext:
    return request.app.state.ctx


async def require_principal(request: Request) -> Principal:
    """鉴权依赖：``Authorization: Bearer <令牌>``（会话令牌或本机令牌）。无效一律 401 ``unauthorized``。"""
    return await services_of(request).auth.authenticate(bearer_token(request.headers.get("authorization")))


def ok(data: Any, status: int = 200) -> JSONResponse:
    """JSON 出口：金额一律字符串、枚举取值（见 views.jsonable）。"""
    return JSONResponse(jsonable(data), status_code=status)


def int_query(name: str, raw: str | None, *, default: int | None, minimum: int, maximum: int) -> int | None:
    """解析整数查询参数。没给或给空串（约定里的 ``before_id=``）取默认；不是整数或越界 422 ``invalid``。"""
    if raw is None or raw == "":
        return default
    try:
        value = int(raw, 10) if raw.lstrip("+-").isdigit() else None
    except ValueError:
        value = None
    if value is None or not minimum <= value <= maximum:
        raise errors.invalid(f"{name} 必须是 {minimum} 到 {maximum} 之间的整数")
    return value


def request_id_of(request: Request) -> str | None:
    """客户端给的 ``X-Request-Id``（只收简单的字母数字和 ._:-，最长 64），用于操作日志里的请求编号。"""
    value = request.headers.get("x-request-id")
    return value if value is not None and _REQUEST_ID.fullmatch(value) else None


def client_info(request: Request) -> str:
    host = request.client.host if request.client is not None else "-"
    agent = request.headers.get("user-agent", "")
    return f"{host} {agent}".strip()[:MAX_CLIENT_INFO]
