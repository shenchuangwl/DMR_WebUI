"""管理路由（设计约定第 10 节；报告 4.9「首个操作员」）：``POST /operators {username, password}``。

- 只允许本机令牌（主体 ``cli``）：会话令牌一律 403 ``forbidden``，不管持不持有席位、是不是管理员。这个检查是依赖，排在请求体校验之前。
- 成功：201 ``{id, username, role, created_ms}``；口令用 ``api.auth.hash_password`` 的 scrypt 哈希存，不返回、不写日志、不进审计。
  操作员的角色目前只有一种（admin），请求里写别的角色会被忽略。
- 用户名已存在 409 ``operator_exists``；用户名或口令不合规 422 ``invalid``（中文原因，不回显提交的内容）。
- 审计：``operator.create``，操作人是 ``cli``（由 ``repo_auth.create_operator`` 在建号的同一事务里写）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from dmr_kernel.api import errors
from dmr_kernel.api.auth import PASSWORD_MAX_LEN
from dmr_kernel.api.common import Principal
from dmr_kernel.api.deps import ok, require_principal, services_of
from dmr_kernel.ledger import repo_auth

__all__ = ["require_cli", "router"]

router = APIRouter()


class OperatorBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LEN)


async def require_cli(principal: Principal = Depends(require_principal)) -> Principal:
    """只有本机令牌（命令行）可以过；任何会话令牌都是 403 ``forbidden``。"""
    if principal.is_session:
        raise errors.forbidden("只有本机命令行（凭本机令牌）可以创建操作员："
                               "请在内核所在的机器上运行 python -m dmr_kernel.cli create-operator")
    return principal


@router.post("/operators", status_code=201)
async def create_operator(body: OperatorBody, request: Request, principal: Principal = Depends(require_cli)):
    try:
        record = await services_of(request).auth.create_operator(body.username, body.password, role="admin",
                                                                 created_by=principal.operator)
    except repo_auth.OperatorExists:
        raise errors.operator_exists(body.username) from None
    except ValueError as exc:  # 用户名、口令不合规：原因是固定的中文，不含提交的内容
        raise errors.invalid(str(exc)) from None
    return ok({"id": record.id, "username": record.username, "role": record.role, "created_ms": record.created_ms},
              status=201)
