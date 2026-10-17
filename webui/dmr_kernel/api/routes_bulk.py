"""一键全平与模型切换的控制接口（M2B；报文约定见 docs/plans/M2-统一操作-测试先行计划.md 第 2 节）。

``router`` 由 M2A 在 ``app.py`` 里挂上（带 ``/api/v1`` 前缀）。路径：
``POST /ops/close_all/preview | /ops/close_all/confirm | /ops/abort | /ops/switch/preview | /ops/switch/confirm | /ops/switch/cancel``。

服务从 ``app.state.bulk_ops``（``ops.switch.BulkOps``：一键全平与切换两个服务，装配层 ``core.scheduler`` 放进去）和
``app.state.services``（登录与席位）取；没有 ``bulk_ops``（没有装配）答 501 ``not_implemented``，不看请求体。
路由只做：鉴权 → 把登录主体转成 ``ops.common.Caller``（``routes_ops.caller_of``：查席位、带上最近一次口令校验的时间）→ 调 ops
→ 把 ``OpRejected`` 转成接口错误。口令校验函数用 ``routes_ops.verifier_of``。席位、口令、版本、操作号去重等统一步骤全在 ops 里（``ops.common.admit``）。请求带的口令只用来当场校验，不往下传。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from dmr_kernel.api import errors
from dmr_kernel.api.common import Principal
from dmr_kernel.api.deps import ok, require_principal
from dmr_kernel.api.routes_ops import caller_of, verifier_of
from dmr_kernel.api.views import position_view
from dmr_kernel.ops.common import OpRejected
from dmr_kernel.ops.switch import BulkOps

__all__ = ["router"]


def require_bulk(request: Request) -> None:
    """依赖：一键全平与切换没有装配（没有 ``app.state.bulk_ops``）就答 501，不看请求体。"""
    if getattr(request.app.state, "bulk_ops", None) is None:
        raise errors.not_implemented("操作类接口在 M2 实现（一键全平与切换尚未装配）")


router = APIRouter(dependencies=[Depends(require_principal), Depends(require_bulk)])


class CloseAllPreviewBody(BaseModel):
    scope: str


class CloseAllConfirmBody(BaseModel):
    op_id: str
    model_version: int
    scope: str
    password: str | None = None


class AbortBody(BaseModel):
    op_id: str
    target_op_id: str


class SwitchPreviewBody(BaseModel):
    op_id: str
    target: Any = None


class SwitchConfirmBody(BaseModel):
    op_id: str
    model_version: int
    mode: Any = None
    password: str | None = None


class SwitchCancelBody(BaseModel):
    op_id: str


def _bulk(request: Request) -> BulkOps:
    return request.app.state.bulk_ops


def _as_api_error(exc: OpRejected) -> errors.ApiError:
    return errors.ApiError(exc.status, exc.code, exc.message, **exc.extra)


def _position(rec: Any) -> dict[str, Any]:
    return position_view(rec, quote=None, unconfirmed=False, stop_on_exchange=None)


def _envelope(op_id: str, status: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"op_id": op_id, "status": status, "result": result}


# ---------------------------------------------------------------- 一键全平


@router.post("/ops/close_all/preview")
async def close_all_preview(body: CloseAllPreviewBody, request: Request):
    try:
        preview = await _bulk(request).close_all.preview(body.scope)
    except OpRejected as exc:
        raise _as_api_error(exc) from None
    return ok({
        "scope": preview["scope"], "model_version": preview["model_version"],
        "included": [_position(rec) for rec in preview["included"]],
        "excluded": [{"position": _position(item["position"]), "reason": item["reason"]}
                     for item in preview["excluded"]],
        "mixed_included": [_position(rec) for rec in preview["mixed_included"]]})


@router.post("/ops/close_all/confirm")
async def close_all_confirm(body: CloseAllConfirmBody, request: Request,
                            principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    try:
        record, _ = await _bulk(request).close_all.start(
            caller, op_id=body.op_id, scope=body.scope, model_version=body.model_version, password=body.password,
            verify=verifier_of(request, principal))
    except OpRejected as exc:
        raise _as_api_error(exc) from None
    if record.status.value in ("accepted", "running"):
        return ok(_envelope(record.op_id, "running", {"status": "running"}))
    result = record.result if isinstance(record.result, dict) else {}
    return ok(_envelope(record.op_id, record.status.value, result))


@router.post("/ops/abort")
async def abort(body: AbortBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    try:
        result = await _bulk(request).close_all.abort(caller, op_id=body.op_id, target_op_id=body.target_op_id)
    except OpRejected as exc:
        raise _as_api_error(exc) from None
    return ok(_envelope(body.op_id, "succeeded", result))


# ---------------------------------------------------------------- 模型切换


@router.post("/ops/switch/preview")
async def switch_preview(body: SwitchPreviewBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    try:
        result = await _bulk(request).switch.preview(caller, op_id=body.op_id, target=body.target)
    except OpRejected as exc:
        raise _as_api_error(exc) from None
    return ok(_envelope(body.op_id, "succeeded", result))


@router.post("/ops/switch/confirm")
async def switch_confirm(body: SwitchConfirmBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    try:
        record, payload = await _bulk(request).switch.confirm(
            caller, op_id=body.op_id, model_version=body.model_version, mode=body.mode, password=body.password,
            verify=verifier_of(request, principal))
    except OpRejected as exc:
        raise _as_api_error(exc) from None
    return ok(_envelope(record.op_id, payload["status"], payload))


@router.post("/ops/switch/cancel")
async def switch_cancel(body: SwitchCancelBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    try:
        result = await _bulk(request).switch.cancel(caller, op_id=body.op_id)
    except OpRejected as exc:
        raise _as_api_error(exc) from None
    return ok(_envelope(body.op_id, "succeeded", result))
