"""交易所失败响应的归类（第 1 层，纯函数，不做任何 I/O）。

``classify`` 把「HTTP 状态 + 错误码 + 文字」归成 ``RejectKind``；返回 ``None`` 表示执行状态未知
（503、-1007、-1001、其它 5xx）：写请求必须先回查，读请求当作 ``Unknown``，绝不能当成「被拒绝」或「空」。
映射表见 docs/设计约定.md 7.2 末尾：

- 400 + -2021 会立即触发、-4130 同向已有全平型止盈止损、-2022 无仓可平或超出持仓、-4164 名义金额不足、-2019 保证金不足、
  -4116 编号重复、-2011/-2013 订单不存在、-4045 条件单数量到上限、-1003 限频、-4047/-4048 有挂单或持仓时不能改保证金模式、
  -1102/-1111/-4015 参数错误；
- 429/418 -> RATE_LIMITED；
- 401/403 -> 抛 ``AuthConfigError``（令牌错误或缺失，是配置问题，不是交易所拒绝）；
- 其它 4xx 与没有专门类别的错误码 -> OTHER。
"""
from __future__ import annotations

from typing import Any

from dmr_kernel.core.types import RejectKind

# 错误文字最多保留的长度（响应体可能是一整页 HTML）
_MAX_TEXT = 300

_CODE_KINDS: dict[int, RejectKind] = {
    -2021: RejectKind.IMMEDIATE_TRIGGER,
    -4130: RejectKind.DUAL_CLOSE_POSITION,
    -2022: RejectKind.REDUCE_REJECTED,
    -4164: RejectKind.MIN_NOTIONAL,
    -2019: RejectKind.INSUFFICIENT_MARGIN,
    -4116: RejectKind.DUPLICATE_CLIENT_ID,
    -2011: RejectKind.ORDER_NOT_FOUND,
    -2013: RejectKind.ORDER_NOT_FOUND,
    -4045: RejectKind.STOP_LIMIT_REACHED,
    -1003: RejectKind.RATE_LIMITED,
    -4047: RejectKind.HAS_POSITION_OR_ORDERS,
    -4048: RejectKind.HAS_POSITION_OR_ORDERS,
    -1102: RejectKind.INVALID_PARAMS,
    -1111: RejectKind.INVALID_PARAMS,
    -4015: RejectKind.INVALID_PARAMS,
}

# 交易所自己说「状态未知」的错误码：-1007 等待后端响应超时、-1001 内部断开
UNKNOWN_CODES = frozenset({-1007, -1001})


class AuthConfigError(Exception):
    """令牌错误或缺失（HTTP 401/403）。属于配置问题，不是交易所拒绝：由装配层或操作员处理，不能当成「订单被拒」。"""


def parse_error_body(body: Any) -> tuple[int | None, str]:
    """从失败响应体取出 ``(错误码, 文字)``。体是 ``{"code", "msg"}`` 时直接取；其它形式（HTML、空）只保留文字。"""
    if isinstance(body, dict):
        raw = body.get("code")
        code: int | None = None
        if type(raw) is int:
            code = raw
        elif isinstance(raw, str):
            try:
                code = int(raw.strip())
            except ValueError:
                code = None
        msg = body.get("msg")
        return code, ("" if msg is None else str(msg))[:_MAX_TEXT]
    if body is None:
        return None, ""
    return None, str(body)[:_MAX_TEXT]


def classify(http_status: int, code: int | None, msg: str) -> RejectKind | None:
    """归类失败响应。``None`` = 执行状态未知（503、-1007、-1001、5xx）；401/403 抛 ``AuthConfigError``。"""
    if http_status in (401, 403):
        raise AuthConfigError(f"认证失败（HTTP {http_status}，代码 {code}）：{msg}")
    if code in UNKNOWN_CODES or http_status >= 500:
        return None
    if http_status in (418, 429):
        return RejectKind.RATE_LIMITED
    if code is not None and code in _CODE_KINDS:
        return _CODE_KINDS[code]
    return RejectKind.OTHER
