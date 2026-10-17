"""控制接口应用（第 7 层 api）：``create_app(ctx) -> FastAPI``，基址 ``/api/v1``（装配层按 ``127.0.0.1:18200`` 起 uvicorn）。

- 除 ``GET /health`` 外都要令牌（会话令牌或本机令牌）；不存在的路径也先鉴权再 404，不泄露哪些路径存在。
- 错误一律 ``{"code", "message", ...附加字段}``（报文约定第 1 节）；请求校验失败 422 ``invalid``，不回显提交的内容；
  意外错误 500 ``internal_error``，不带内部细节（详情只进内核日志）。
- 不提供 /docs、/redoc、/openapi.json（它们不需要令牌，也没有必要）。
- 事件流订阅与实时缓存在应用启动时（lifespan）接上总线，关停时撤下；``app.state.services`` 给装配层取
  ``seat.expire_due``（席位到期清理的定时任务）、``live.note_position_mode``/``note_restrictions`` 用。
- 关停第一步（报告 4.4「停止接收新指令」）：装配层把 ``app.state.accepting`` 置假，此后一切改变状态的 HTTP 请求
  （GET/HEAD/OPTIONS 以外）在鉴权之前就返回 503 ``unavailable``；读请求和事件流照常。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from dmr_kernel.api import (
    errors,
    events,
    routes_admin,
    routes_auth,
    routes_bulk,
    routes_config,
    routes_dev,
    routes_ops,
    routes_read,
)
from dmr_kernel.api.context import ApiContext, Services, build_services
from dmr_kernel.api.deps import require_principal
from dmr_kernel.api.errors import ApiError
from dmr_kernel.ops.common import OpRejected

log = logging.getLogger(__name__)

__all__ = ["BASE", "ApiContext", "Services", "create_app"]

BASE = "/api/v1"
_FIELD_PROBLEMS = {
    "missing": "缺少这一项",
    "string_too_short": "不能为空",
    "string_too_long": "太长",
    "string_type": "必须是文本",
    "json_invalid": "请求体不是有效的 JSON",
    "model_attributes_type": "请求体必须是 JSON 对象",
    "dict_type": "请求体必须是 JSON 对象",
}


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class _Accepting:
    """纯 ASGI 中间件：``app.state.accepting`` 为假时拒绝改变状态的请求（503 ``unavailable``，不泄露路径是否存在）。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if (scope["type"] == "http" and scope["method"] not in _READ_METHODS
                and not getattr(scope["app"].state, "accepting", True)):
            error = errors.unavailable("内核正在关停，暂不受理改变状态的请求")
            await JSONResponse(error.body(), status_code=error.status)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _headers_for(error: ApiError) -> dict[str, str]:
    if error.code == "unauthorized":
        return {"WWW-Authenticate": "Bearer"}
    if error.code == "rate_limited":
        return {"Retry-After": str(error.extra["retry_after_s"])}
    return {}


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status, headers=_headers_for(exc))


async def _op_rejected(request: Request, exc: OpRejected) -> JSONResponse:
    """操作被拒绝：状态码、代码、原因，附加字段（例如自检的 ``checks``）并入顶层。"""
    return JSONResponse(exc.body(), status_code=exc.status)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """只说哪一项、什么问题，不回显提交的内容（里面可能有口令）。"""
    problems = []
    for item in exc.errors()[:5]:
        place = ".".join(str(part) for part in item.get("loc", ()) if part not in ("body", "query", "path", "header"))
        problems.append(f"{place or '请求'}：{_FIELD_PROBLEMS.get(item.get('type', ''), '取值不合法')}")
    return JSONResponse(errors.invalid("请求参数无效：" + "；".join(problems)).body(), status_code=422)


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        error = errors.not_found("没有这个接口")
    else:
        error = ApiError(exc.status_code, "invalid", "请求不被接受")
    return JSONResponse(error.body(), status_code=error.status, headers=getattr(exc, "headers", None))


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("控制接口处理 %s %s 时出现未预料的错误", request.method, request.url.path)
    error = errors.internal_error()
    return JSONResponse(error.body(), status_code=error.status)


def create_app(ctx: ApiContext, services: Services | None = None) -> FastAPI:
    services = services if services is not None else build_services(ctx)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services.live.start()
        services.hub.start()
        try:
            yield
        finally:
            services.hub.stop()
            services.live.stop()

    app = FastAPI(title="DMR 内核控制接口", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx
    app.state.services = services
    app.state.accepting = True
    app.add_middleware(_Accepting)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(OpRejected, _op_rejected)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected)

    app.include_router(routes_read.public, prefix=BASE)
    app.include_router(routes_auth.public, prefix=BASE)
    app.include_router(routes_auth.authed, prefix=BASE)
    app.include_router(routes_read.authed, prefix=BASE)
    # 配置修改与操作员创建要先于 routes_ops 挂上：路径匹配先到先得，routes_ops 里 PUT /config/{key} 的 501 占位要被盖住
    app.include_router(routes_config.router, prefix=BASE)
    app.include_router(routes_admin.router, prefix=BASE)
    # M2 的操作入口：一键全平与切换（routes_bulk）、启停与单仓操作（routes_ops.real）；占位只剩尚未实现的路径
    app.include_router(routes_bulk.router, prefix=BASE)
    app.include_router(routes_ops.real, prefix=BASE)
    app.include_router(routes_ops.ops, prefix=BASE)
    if ctx.settings.dev_tools_enabled:
        app.include_router(routes_dev.router, prefix=BASE)
        app.include_router(routes_ops.dev, prefix=BASE)
    app.add_api_websocket_route(BASE + "/events", events.events_endpoint)

    async def no_such_endpoint(rest: str):
        raise errors.not_found("没有这个接口（或不支持这种请求方法）")

    # 兜底：基址下所有没匹配上的请求先鉴权（没令牌 401）再 404
    app.add_api_route(BASE + "/{rest:path}", no_such_endpoint, methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                      dependencies=[Depends(require_principal)])
    return app
