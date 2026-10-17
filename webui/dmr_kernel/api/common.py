"""控制接口各部分共用的小类型：登录主体、登录结果、席位变化事件。"""
from __future__ import annotations

from dataclasses import dataclass, field

from dmr_kernel.core.events import Event

__all__ = ["CLI_OPERATOR", "KERNEL_VERSION", "LoginResult", "Principal", "SeatChanged"]

CLI_OPERATOR = "cli"
KERNEL_VERSION = "0.1.0"  # 与 pyproject.toml 的 version 一致（有测试核对）


@dataclass(frozen=True)
class Principal:
    """一次请求的主体：``session`` 是登录会话（占用席位、可重新校验口令），``cli`` 是本机令牌（不占席位）。"""

    kind: str  # "session" | "cli"
    operator: str
    session_id: int | None = None
    operator_id: int | None = None
    role: str | None = None
    reauth_ms: int | None = None  # 会话上最近一次口令校验的时间（认证那一刻读到的）

    @property
    def is_session(self) -> bool:
        return self.kind == "session"

    @classmethod
    def cli(cls) -> "Principal":
        return cls(kind="cli", operator=CLI_OPERATOR)


@dataclass(frozen=True)
class LoginResult:
    token: str = field(repr=False)  # 令牌不进 repr：对象被日志、断言信息带出来也不泄露
    operator: str
    session_id: int
    idle_timeout_min: int


@dataclass(frozen=True)
class SeatChanged(Event):
    """操作席位换了人或空出来了。本地事件（api 内部）：事件流把它映射成 ``state`` 帧，让其它控制台刷新席位显示。
    cause：acquire | release | force | expired | logout。"""

    holder_session_id: int | None
    cause: str
    at_ms: int
