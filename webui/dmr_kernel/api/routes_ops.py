"""操作类接口（设计约定第 10 节，M2 计划第 2 节）。

- ``real``：M2A 已实现的入口——``POST /ops/start``、``/ops/stop``、``/ops/partial_close/preview``、``/ops/partial_close/confirm``、
  ``/ops/close_one``、``/ops/recover``。先鉴权（没有令牌 401），再看操作服务有没有装配（``ApiContext.ops_wiring`` 为空答 501，
  不看请求体），然后才解析请求体并交给 ``ops``。校验、席位、口令、版本、去重都在 ``ops.common.admit`` 里；这里只转报文：
  ``OpRejected`` 由应用的异常处理转成 4xx/5xx JSON（``{"code","message",...附加字段}``，例如自检不过时带 ``checks``）。
- ``ops``：接管类操作入口（M3：``POST /ops/takeover``、``/ops/ignore``、``/ops/restore``、``/ops/cancel_takeover``、
  ``/ops/hand_stop_to_system``）。接管操作服务（``ops.takeover_ops.TakeoverOps``）由装配层放进 ``app.state.takeover_ops``；
  没有装配时先鉴权、再答 501 ``not_implemented``，不看请求体。已实现的 ``real`` 里的路径不在这里。
  开发接口的造仓见 ``routes_dev``，``dev`` 里只剩注入信号的占位。
- 共用小工具：``build_ops_services(ctx)`` 按 ``ApiContext`` 建一套操作服务（装配层 ``core.scheduler`` 在构造时就建好、放进
  ``app.state.ops_services``，并让一键全平与切换共用它的 ``deps``）；
  ``ops_services_of(request)`` 取应用里唯一的一套操作服务（``OpsServices``：``deps`` 与各入口对象，锁才起作用；没有预建就现建），
  ``caller_of(request, principal)`` 把登录主体转成 ``ops.common.Caller``（席位在这里查），``verifier_of(request, principal)``
  给重新输入口令用的校验函数。M2B 的 ``routes_bulk`` 可直接用这三个。

配置修改 ``PUT /config/{key}`` 已经实现（``routes_config``），这里没有它的占位：以前的 501 占位被先挂上的 ``routes_config``
遮蔽，删掉之后挂载顺序也就不再影响它。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from dmr_kernel.api import errors
from dmr_kernel.api.common import Principal
from dmr_kernel.api.deps import ctx_of, ok, require_principal, services_of
from dmr_kernel.ops.close_one import CloseOne
from dmr_kernel.ops.common import Caller, OpsDeps
from dmr_kernel.ops.operations import Operations
from dmr_kernel.ops.partial_close import PartialClose
from dmr_kernel.ops.startstop import StartStop
from dmr_kernel.ops.test_entry import TestEntry

__all__ = ["OpsServices", "build_ops_services", "caller_of", "dev", "ops", "ops_services_of", "real", "require_wired",
           "takeover_ops_of", "verifier_of"]

# 接管类入口（``ops`` 路由器，M3 实现）。已实现的（``real``、``routes_bulk``、``routes_dev``）不再留被遮蔽的占位，挂载顺序也就不影响它们
OPS_PATHS = ("/ops/takeover", "/ops/ignore", "/ops/restore", "/ops/cancel_takeover", "/ops/hand_stop_to_system")
DEV_PATHS = ("/dev/inject_signal",)

ops = APIRouter(dependencies=[Depends(require_principal)])
dev = APIRouter(dependencies=[Depends(require_principal)])


async def _not_implemented():
    raise errors.not_implemented("操作类接口在 M2 实现")


def takeover_ops_of(request: Request) -> Any:
    """应用里唯一的一套接管操作服务（装配层放进 ``app.state.takeover_ops``）；没有装配答 501，不看请求体。"""
    built = getattr(request.app.state, "takeover_ops", None)
    if built is None:
        raise errors.not_implemented("操作类接口在 M2 实现（接管操作服务尚未装配）")
    return built


for _path in DEV_PATHS:
    dev.add_api_route(_path, _not_implemented, methods=["POST"])


# ---------------------------------------------------------------- 操作服务与主体转换


@dataclass
class OpsServices:
    deps: OpsDeps
    startstop: StartStop
    partial_close: PartialClose
    close_one: CloseOne
    test_entry: TestEntry


def require_wired(request: Request) -> None:
    """依赖：操作服务没有装配（``ApiContext.ops_wiring`` 为空）就答 501，不看请求体。"""
    if getattr(ctx_of(request), "ops_wiring", None) is None:
        raise errors.not_implemented("操作类接口在 M2 实现（操作服务尚未装配）")


def build_ops_services(ctx: Any, *, operations: Operations | None = None) -> OpsServices:
    """按 ``ApiContext`` 建一套操作服务（``ctx.ops_wiring`` 必须已接好）。``operations`` 缺省按账本新建。"""
    wiring = ctx.ops_wiring
    deps = OpsDeps(
        clock=ctx.clock, modelstate=ctx.modelstate, registry=ctx.registry,
        operations=operations if operations is not None else Operations(ctx.ledger, ctx.clock, ctx.bus),
        config_get=ctx.config.get, models=ctx.models,
        coordinator=wiring.coordinator, adapter=wiring.adapter, rules=wiring.rules, orders=wiring.orders,
        is_live=ctx.settings.is_live, list_valid=wiring.list_valid, legacy_check=wiring.legacy_check,
        recover=wiring.recover, last_recovery=wiring.last_recovery)
    return OpsServices(deps=deps, startstop=StartStop(deps), partial_close=PartialClose(deps),
                       close_one=CloseOne(deps), test_entry=TestEntry(deps))


def ops_services_of(request: Request) -> OpsServices:
    """应用里唯一的一套操作服务（装配层预建的；没有就第一次用到时建）。"""
    state = request.app.state
    built = getattr(state, "ops_services", None)
    if built is None:
        built = state.ops_services = build_ops_services(ctx_of(request))
    return built


async def caller_of(request: Request, principal: Principal) -> Caller:
    """登录主体 → 操作主体；会话是否持有未到期的席位在这里查（本机令牌不占席位）。"""
    holds = principal.is_session and bool((await services_of(request).seat.view(principal))["mine"])
    return Caller(operator=principal.operator, session_id=principal.session_id, is_session=principal.is_session,
                  holds_seat=holds, reauth_ms=principal.reauth_ms)


def verifier_of(request: Request, principal: Principal) -> Callable[[str], Awaitable[Any]]:
    """重新输入口令的校验函数：口令错误抛 401 ``bad_credentials``。"""
    auth = services_of(request).auth

    async def verify(password: str) -> None:
        await auth.reauth(principal, password)

    return verify


# ---------------------------------------------------------------- 已实现的入口（M2A）

real = APIRouter(dependencies=[Depends(require_principal), Depends(require_wired)])


class StartBody(BaseModel):
    op_id: str
    model_version: int
    password: str | None = None


class StopBody(StartBody):
    close_all: bool = False


class PreviewBody(BaseModel):
    position_id: int
    ratio: Any = None
    qty: Any = None


class ConfirmBody(BaseModel):
    op_id: str
    position_id: int
    position_version: int
    preview_qty: Any
    close_qty: Any


class CloseOneBody(BaseModel):
    op_id: str
    position_id: int
    position_version: int


class RecoverBody(BaseModel):
    op_id: str


@real.post("/ops/start")
async def start(body: StartBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    return ok(await ops_services_of(request).startstop.start(
        caller, op_id=body.op_id, model_version=body.model_version, password=body.password,
        verify=verifier_of(request, principal)))


@real.post("/ops/stop")
async def stop(body: StopBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    return ok(await ops_services_of(request).startstop.stop(
        caller, op_id=body.op_id, model_version=body.model_version, close_all=body.close_all, password=body.password,
        verify=verifier_of(request, principal)))


@real.post("/ops/partial_close/preview")
async def partial_close_preview(body: PreviewBody, request: Request,
                                principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    return ok(await ops_services_of(request).partial_close.preview(
        caller, position_id=body.position_id, ratio=body.ratio, qty=body.qty))


@real.post("/ops/partial_close/confirm")
async def partial_close_confirm(body: ConfirmBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    return ok(await ops_services_of(request).partial_close.confirm(
        caller, op_id=body.op_id, position_id=body.position_id, position_version=body.position_version,
        preview_qty=body.preview_qty, close_qty=body.close_qty))


@real.post("/ops/close_one")
async def close_one(body: CloseOneBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    return ok(await ops_services_of(request).close_one.confirm(
        caller, op_id=body.op_id, position_id=body.position_id, position_version=body.position_version))


@real.post("/ops/recover")
async def recover(body: RecoverBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    return ok(await ops_services_of(request).startstop.recover(caller, op_id=body.op_id))


# ---------------------------------------------------------------- 接管类入口（M3）


class TakeoverBody(BaseModel):
    op_id: str
    position_id: int
    position_version: int


class CancelTakeoverBody(TakeoverBody):
    mode: str = "handover"  # handover：保留止损并移交 | cancel_stop：撤掉系统止损


def _takeover_route(name: str, summary: str):
    async def handler(body: TakeoverBody, request: Request, principal: Principal = Depends(require_principal)):
        caller = await caller_of(request, principal)
        return ok(await getattr(request.app.state.takeover_ops, name)(
            caller, op_id=body.op_id, position_id=body.position_id, position_version=body.position_version))

    handler.__name__ = f"takeover_{name}"
    handler.__doc__ = summary
    return handler


async def _require_takeover_wired(request: Request) -> None:
    takeover_ops_of(request)


for _name, _path in (("takeover", "/ops/takeover"), ("ignore", "/ops/ignore"), ("restore", "/ops/restore"),
                     ("hand_stop_to_system", "/ops/hand_stop_to_system")):
    ops.add_api_route(_path, _takeover_route(_name, _path), methods=["POST"],
                      dependencies=[Depends(_require_takeover_wired)])


@ops.post("/ops/cancel_takeover", dependencies=[Depends(_require_takeover_wired)])
async def cancel_takeover(body: CancelTakeoverBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    return ok(await request.app.state.takeover_ops.cancel_takeover(
        caller, op_id=body.op_id, position_id=body.position_id, position_version=body.position_version, mode=body.mode))
