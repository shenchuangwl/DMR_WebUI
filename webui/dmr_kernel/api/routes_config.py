"""配置修改路由（设计约定第 10 节）：``PUT /config/{key} {op_id, value}``。

- 鉴权：会话令牌或本机令牌。会话必须持有未到期的操作席位（否则 409 ``seat_required``）；本机令牌（主体 ``cli``）不占席位。
  席位检查是依赖，所以排在请求体校验之前（没席位的人不用知道请求体哪里写错了）。
- 成功：200 ``{key, old, new, applies, affects_existing}``；``applies`` 是何时生效的中文说明；金额是字符串。
- 校验失败：422 ``invalid``，``message`` 是 ``ConfigError`` 的原文（含「部署级开关没开不能改模型 3 档位」「A、B 取值冲突」）。
- 同一个 ``op_id`` 只执行一次，重复提交返回第一次的结果（含第一次的拒绝）；``op_id`` 是 1 到 64 位的字母、数字或 ``. _ : -``。
- 实现在 ``ops.config_ops``；这里只做鉴权、席位与报文。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from dmr_kernel.api import errors
from dmr_kernel.api.common import Principal
from dmr_kernel.api.deps import ctx_of, ok, require_principal, services_of
from dmr_kernel.core.configdefs import ConfigError
from dmr_kernel.ops.config_ops import ConfigOps
from dmr_kernel.ops.operations import Operations, check_op_id

__all__ = ["config_ops_of", "require_seat", "router"]

router = APIRouter()


class ConfigBody(BaseModel):
    op_id: str
    value: Any


async def require_seat(request: Request, principal: Principal = Depends(require_principal)) -> Principal:
    """改变状态的接口用的席位检查：本机令牌不占席位，直接放行；会话必须持有未到期的操作席位，否则 409 ``seat_required``。
    只在受理请求时校验，已受理的操作不因席位之后的变化而中断。"""
    if principal.is_session and not (await services_of(request).seat.view(principal))["mine"]:
        raise errors.seat_required()
    return principal


def config_ops_of(request: Request) -> ConfigOps:
    """应用里唯一的 ``ConfigOps``（第一次用到时建；它那把串行锁只有在全应用共用一个时才起作用）。"""
    state = request.app.state
    config_ops = getattr(state, "config_ops", None)
    if config_ops is None:
        ctx = ctx_of(request)
        config_ops = state.config_ops = ConfigOps(Operations(ctx.ledger, ctx.clock, ctx.bus), ctx.config)
    return config_ops


@router.put("/config/{key}")
async def put_config(key: str, body: ConfigBody, request: Request, principal: Principal = Depends(require_seat)):
    try:
        op_id = check_op_id(body.op_id)
    except ValueError as exc:
        raise errors.invalid(str(exc)) from None
    try:
        result = await config_ops_of(request).set_config(op_id, key, body.value, operator=principal.operator,
                                                         session_id=principal.session_id)
    except ConfigError as exc:
        raise errors.invalid(str(exc)) from None
    return ok(result)
