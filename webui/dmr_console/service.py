"""控制台服务（不含界面）：登录/退出、按浏览器会话取「实时数据控制器」。

浏览器只有一个签名 Cookie 里的随机编号（``app.storage.browser['id']``）；内核令牌存在 ``SessionStore``（服务端内存），
控制器按这个编号一一对应。口令只在 ``login`` 的参数里短暂出现，不存、不记、不进返回值。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any, Callable

from dmr_console.client import KernelApi, KernelError, KernelUnreachable
from dmr_console.clock import Clock
from dmr_console.live import DEFAULT_ALERTS_LIMIT, DEFAULT_OPLOG_PAGE, DEFAULT_STREAM_GRACE_S, LiveController
from dmr_console.session import ConsoleSession, SessionStore
from dmr_console.state import LOST_MESSAGE

log = logging.getLogger(__name__)

EMPTY_MESSAGE = "请输入用户名和口令"
BAD_CREDENTIALS_MESSAGE = "用户名或口令错误"
NO_BROWSER_MESSAGE = "浏览器会话无效，请刷新页面后重试"
INTERNAL_MESSAGE = "登录时出现内部错误，请查看控制台日志"
UNCONFIRMED_LOGOUT_MESSAGE = "已退出；内核暂时没有确认，该会话会在闲置后自动过期"


@dataclass(frozen=True)
class LoginOutcome:
    ok: bool
    code: str  # ok / empty / no_browser / bad_credentials / rate_limited / unreachable / error / 内核的其它错误代码
    message: str = ""  # 给操作员看的中文
    detail: str = ""  # 失联时的具体原因
    retry_after_s: float | None = None
    unreachable: bool = False


@dataclass(frozen=True)
class LogoutOutcome:
    confirmed: bool  # 内核是否确认撤销了令牌
    message: str = ""


def _kernel_refusal(exc: KernelError) -> LoginOutcome:
    if exc.code == "bad_credentials":
        return LoginOutcome(False, "bad_credentials", BAD_CREDENTIALS_MESSAGE)
    if exc.code == "rate_limited":
        wait = exc.retry_after_s
        message = "登录失败次数过多，请稍后再试" if wait is None else f"登录失败次数过多，请 {math.ceil(wait)} 秒后再试"
        return LoginOutcome(False, "rate_limited", message, retry_after_s=wait)
    return LoginOutcome(False, exc.code, exc.message or f"登录被内核拒绝（{exc.code}）")


class ConsoleService:
    def __init__(self, *, api: KernelApi, clock: Clock, sessions: SessionStore, tz: tzinfo | None = None,
                 stream_factory: Callable[[LiveController], Any] | None = None,
                 stream_grace_s: float = DEFAULT_STREAM_GRACE_S, oplog_page: int = DEFAULT_OPLOG_PAGE,
                 alerts_limit: int = DEFAULT_ALERTS_LIMIT) -> None:
        self.api = api
        self.clock = clock
        self.sessions = sessions
        self.tz = tz
        self._stream_factory = stream_factory
        self._stream_grace_s = stream_grace_s
        self._oplog_page = oplog_page
        self._alerts_limit = alerts_limit
        self._controllers: dict[str, LiveController] = {}

    # ------------------------------------------------------------ 登录与退出

    async def login(self, browser_id: str, username: str, password: str) -> LoginOutcome:
        if not isinstance(browser_id, str) or not browser_id:
            return LoginOutcome(False, "no_browser", NO_BROWSER_MESSAGE)
        username = (username or "").strip()
        if not username or not password:
            return LoginOutcome(False, "empty", EMPTY_MESSAGE)
        try:
            result = await self.api.login(username, password)
        except KernelUnreachable as exc:
            return LoginOutcome(False, "unreachable", LOST_MESSAGE, detail=exc.reason, unreachable=True)
        except KernelError as exc:
            return _kernel_refusal(exc)
        except Exception:  # noqa: BLE001 - 程序错误：记栈（栈里没有口令），界面只说内部错误
            log.exception("登录时出现未预期的错误")
            return LoginOutcome(False, "error", INTERNAL_MESSAGE)
        self._sweep()
        previous = self._controllers.pop(browser_id, None)
        if previous is not None:
            previous.close_nowait()
        self.sessions.open(browser_id, result)
        log.info("操作员 %s 登录控制台", result.operator)
        return LoginOutcome(True, "ok")

    async def logout(self, browser_id: str | None) -> LogoutOutcome:
        session = self.sessions.drop(browser_id)
        await self._close_controller(browser_id)
        if session is None:
            return LogoutOutcome(True)
        try:
            await self.api.logout(session.token)
        except KernelUnreachable:
            return LogoutOutcome(False, UNCONFIRMED_LOGOUT_MESSAGE)
        except KernelError:
            return LogoutOutcome(False, UNCONFIRMED_LOGOUT_MESSAGE)
        except Exception:  # noqa: BLE001
            log.exception("退出登录时出现未预期的错误")
            return LogoutOutcome(False, UNCONFIRMED_LOGOUT_MESSAGE)
        log.info("操作员 %s 退出控制台", session.operator)
        return LogoutOutcome(True)

    async def end_session(self, browser_id: str | None) -> None:
        """令牌已被内核判定失效：只在本地忘掉它，不再去敲内核。"""
        self.sessions.drop(browser_id)
        await self._close_controller(browser_id)

    # ------------------------------------------------------------ 取用

    def session_for(self, browser_id: str | None) -> ConsoleSession | None:
        return self.sessions.get(browser_id)

    def controller_for(self, browser_id: str | None) -> LiveController | None:
        session = self.sessions.get(browser_id)
        if session is None:
            if isinstance(browser_id, str):
                stale = self._controllers.pop(browser_id, None)
                if stale is not None:
                    stale.close_nowait()
            return None
        controller = self._controllers.get(session.browser_id)
        if controller is None or controller.closed or controller.session is not session:
            if controller is not None:
                controller.close_nowait()
            controller = LiveController(self.api, session, self.clock, tz=self.tz, oplog_page=self._oplog_page,
                                        alerts_limit=self._alerts_limit, stream_factory=self._stream_factory,
                                        stream_grace_s=self._stream_grace_s, operations_enabled=True)
            self._controllers[session.browser_id] = controller
        return controller

    async def aclose(self) -> None:
        controllers, self._controllers = list(self._controllers.values()), {}
        for controller in controllers:
            await controller.aclose()

    # ------------------------------------------------------------ 内部

    async def _close_controller(self, browser_id: str | None) -> None:
        if isinstance(browser_id, str):
            controller = self._controllers.pop(browser_id, None)
            if controller is not None:
                await controller.aclose()

    def _sweep(self) -> None:
        """会话已过期/被挤掉的控制器顺手收掉。"""
        live = set(self.sessions.browser_ids())
        for browser_id in [b for b in self._controllers if b not in live]:
            self._controllers.pop(browser_id).close_nowait()
