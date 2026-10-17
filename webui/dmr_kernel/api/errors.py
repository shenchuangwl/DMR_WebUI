"""控制接口的错误：HTTP 4xx/5xx，体 ``{"code": "<英文代码>", "message": "<中文原因>", ...附加字段}``
（报文约定见 docs/plans/M1-控制接口只读部分.md 第 1 节）。

代码与状态码：unauthorized 401、bad_credentials 401、rate_limited 429、reauth_required 403、forbidden 403、
seat_required 409、seat_taken 409、stale_version 409、not_allowed 409、operator_exists 409、not_found 404、
invalid 422、not_implemented 501、unavailable 503。
``forbidden``（身份没问题但这件事不许这种身份做，例如会话令牌创建操作员）与 ``operator_exists``（用户名已被占用）是
管理路由（``POST /operators``）的错误，报文约定第 1 节的代码表之外。
内部未预料的错误是 500 ``internal_error``（报文约定之外，只在程序有缺陷时出现，不带任何内部细节）。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "ApiError", "bad_credentials", "forbidden", "internal_error", "invalid", "not_allowed", "not_found",
    "not_implemented", "operator_exists", "rate_limited", "reauth_required", "seat_required", "seat_taken",
    "stale_version", "unauthorized", "unavailable",
]


class ApiError(Exception):
    """路由与服务抛出的可预期错误；由应用统一转成 JSON。``extra`` 里的字段并入响应体顶层。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def unauthorized(message: str = "需要登录：请在 Authorization 头里带上有效的 Bearer 令牌") -> ApiError:
    return ApiError(401, "unauthorized", message)


def bad_credentials(message: str = "用户名或口令错误") -> ApiError:
    return ApiError(401, "bad_credentials", message)


def rate_limited(retry_after_s: int) -> ApiError:
    return ApiError(429, "rate_limited", f"失败次数过多，请 {retry_after_s} 秒后再试", retry_after_s=retry_after_s)


def reauth_required(message: str = "需要重新输入口令") -> ApiError:
    return ApiError(403, "reauth_required", message)


def forbidden(message: str) -> ApiError:
    return ApiError(403, "forbidden", message)


def seat_required(message: str = "需要持有操作席位：请先申请席位") -> ApiError:
    return ApiError(409, "seat_required", message)


def seat_taken(holder_operator: str | None, lease_until_ms: int | None) -> ApiError:
    who = holder_operator or "另一个会话"
    return ApiError(409, "seat_taken", f"操作席位正由 {who} 持有", holder_operator=holder_operator,
                    lease_until_ms=lease_until_ms)


def stale_version(message: str = "状态版本已过期，请刷新后重试") -> ApiError:
    return ApiError(409, "stale_version", message)


def not_allowed(message: str) -> ApiError:
    return ApiError(409, "not_allowed", message)


def operator_exists(username: str) -> ApiError:
    return ApiError(409, "operator_exists", f"用户名已存在：{username}")


def not_found(message: str = "不存在") -> ApiError:
    return ApiError(404, "not_found", message)


def invalid(message: str) -> ApiError:
    return ApiError(422, "invalid", message)


def not_implemented(message: str = "M2 实现") -> ApiError:
    return ApiError(501, "not_implemented", message)


def unavailable(message: str) -> ApiError:
    return ApiError(503, "unavailable", message)


def internal_error() -> ApiError:
    return ApiError(500, "internal_error", "内核内部错误，详情见内核日志")
