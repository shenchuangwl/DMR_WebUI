"""会话与席位的路由：``/auth/*``、``/seat/*``（报文约定第 2 节）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from dmr_kernel.api.auth import PASSWORD_MAX_LEN
from dmr_kernel.api.common import Principal
from dmr_kernel.api.deps import client_info, ok, request_id_of, require_principal, services_of

__all__ = ["authed", "public"]

public = APIRouter()  # 不需要令牌：登录
authed = APIRouter(dependencies=[Depends(require_principal)])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LEN)


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LEN)


@public.post("/auth/login")
async def login(body: LoginBody, request: Request):
    result = await services_of(request).auth.login(body.username, body.password, client_info=client_info(request))
    return ok({"token": result.token, "operator": result.operator, "session_id": result.session_id,
               "idle_timeout_min": result.idle_timeout_min})


@authed.post("/auth/logout")
async def logout(request: Request, principal: Principal = Depends(require_principal)):
    await services_of(request).auth.logout(principal)
    return ok({})


@authed.post("/auth/reauth")
async def reauth(body: PasswordBody, request: Request, principal: Principal = Depends(require_principal)):
    until = await services_of(request).auth.reauth(principal, body.password)
    return ok({"reauth_valid_until_ms": until})


@authed.get("/auth/me")
async def me(request: Request, principal: Principal = Depends(require_principal)):
    services = services_of(request)
    return ok({"operator": principal.operator, "session_id": principal.session_id, "principal": principal.kind,
               "reauth_valid_until_ms": services.auth.reauth_valid_until(principal),
               "seat": await services.seat.view(principal)})


@authed.post("/seat/acquire")
async def seat_acquire(request: Request, principal: Principal = Depends(require_principal)):
    return ok(await services_of(request).seat.acquire(principal))


@authed.post("/seat/renew")
async def seat_renew(request: Request, principal: Principal = Depends(require_principal)):
    return ok(await services_of(request).seat.renew(principal))


@authed.post("/seat/release")
async def seat_release(request: Request, principal: Principal = Depends(require_principal)):
    return ok(await services_of(request).seat.release(principal))


@authed.post("/seat/force")
async def seat_force(body: PasswordBody, request: Request, principal: Principal = Depends(require_principal)):
    """强制接管：口令当场校验（失败 401，与登录共用限速），成功即刷新重新校验时间，再换人并留痕。"""
    services = services_of(request)
    await services.auth.reauth(principal, body.password, via="seat_force")
    return ok(await services.seat.force(principal, request_id=request_id_of(request)))
