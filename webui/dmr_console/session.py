"""控制台服务端的会话存储：内核会话令牌只放在这里（进程内存），按浏览器会话区分。

键是 ``app.storage.browser['id']``（NiceGUI 在签名 Cookie 里放的随机编号）；浏览器只拿到这个编号，拿不到令牌。
不写文件（尤其不写 .nicegui 的存储文件），不进 repr / 日志。控制台重启后令牌即丢失，需要重新登录；内核不受影响。

本地也按内核的闲置时长（登录响应里的 ``idle_timeout_min``）过期：页面在线时轮询会不断取用，所以只有真闲置才会过期。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dmr_console.client import LoginResult
from dmr_console.clock import Clock

DEFAULT_IDLE_MIN = 30.0
DEFAULT_MAX_SESSIONS = 64


@dataclass
class ConsoleSession:
    browser_id: str
    token: str = field(repr=False)
    operator: str
    session_id: str
    idle_timeout_min: float
    opened_ms: int
    last_seen_ms: int

    @property
    def idle_timeout_ms(self) -> int:
        return int(self.idle_timeout_min * 60_000)


class SessionStore:
    def __init__(self, clock: Clock, *, max_sessions: int = DEFAULT_MAX_SESSIONS,
                 default_idle_min: float = DEFAULT_IDLE_MIN) -> None:
        self._clock = clock
        self._max = max(1, max_sessions)
        self._default_idle_min = default_idle_min
        self._sessions: dict[str, ConsoleSession] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, browser_id: object) -> bool:
        return isinstance(browser_id, str) and self._live(browser_id) is not None

    def browser_ids(self) -> list[str]:
        self._prune()
        return list(self._sessions)

    def open(self, browser_id: str, login: LoginResult) -> ConsoleSession:
        """登录成功后存下令牌；同一浏览器再次登录会替换旧会话。"""
        if not isinstance(browser_id, str) or not browser_id:
            raise ValueError("需要浏览器会话编号")
        self._prune()
        now = self._clock.now_ms()
        idle = login.idle_timeout_min if login.idle_timeout_min and login.idle_timeout_min > 0 else self._default_idle_min
        session = ConsoleSession(browser_id=browser_id, token=login.token, operator=login.operator,
                                 session_id=login.session_id, idle_timeout_min=float(idle), opened_ms=now, last_seen_ms=now)
        self._sessions.pop(browser_id, None)
        while len(self._sessions) >= self._max:
            oldest = min(self._sessions.values(), key=lambda s: s.last_seen_ms)
            del self._sessions[oldest.browser_id]
        self._sessions[browser_id] = session
        return session

    def get(self, browser_id: str | None) -> ConsoleSession | None:
        """取会话；取用算一次活动。过期的会话在这里被丢弃。"""
        if not isinstance(browser_id, str):
            return None
        session = self._live(browser_id)
        if session is not None:
            session.last_seen_ms = self._clock.now_ms()
        return session

    def touch(self, browser_id: str | None) -> None:
        self.get(browser_id)

    def drop(self, browser_id: str | None) -> ConsoleSession | None:
        if not isinstance(browser_id, str):
            return None
        return self._sessions.pop(browser_id, None)

    # ------------------------------------------------------------ 内部

    def _expired(self, session: ConsoleSession, now_ms: int) -> bool:
        return now_ms - session.last_seen_ms > session.idle_timeout_ms

    def _live(self, browser_id: str) -> ConsoleSession | None:
        session = self._sessions.get(browser_id)
        if session is not None and self._expired(session, self._clock.now_ms()):
            del self._sessions[browser_id]
            return None
        return session

    def _prune(self) -> None:
        now = self._clock.now_ms()
        for browser_id in [k for k, s in self._sessions.items() if self._expired(s, now)]:
            del self._sessions[browser_id]
