"""页面冒烟测试的帮手：在 NiceGUI 的用户模拟里跑真实的页面代码（无真实浏览器、无网络），内核换成 FakeKernelClient。

``nicegui.testing.user_simulation`` 是异步上下文管理器，不需要 pytest-asyncio 与 ``user`` 夹具；
页面经 ASGI 内存传输访问，在禁网包装下可用。

注意：NiceGUI 的重置会在每个用户模拟结束时把「定义页面函数的模块」从 sys.modules 里弹掉，
所以 ``dmr_console.app`` 等一律在函数里延迟导入，不在测试文件顶层导入。
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Any

import httpx
from console_kit import FakeKernelClient, ManualClock

UTC7 = timezone(timedelta(hours=7))
PASSWORD = "pw-admin"
NO_DATA_TEXT = "尚无数据"  # 状态条在第一次拉取完成之前的「更新时间」文字


def _routed_navigate_class():
    """延迟导入 NiceGUI（见模块说明）：造出「按调用页面所属的浏览器分派」的 navigate 类。"""
    from nicegui import ui
    from nicegui.testing.user_navigate import UserNavigate

    class RoutedNavigate(UserNavigate):
        """NiceGUI 的用户模拟把全局 ``ui.navigate`` 绑成「最近被访问过属性的那个 User」，而且连这个 User 的某个旧页面客户端
        迟到发出的 run_javascript（例如登录页输入框的 updateValue）也会触发这次改绑：两个浏览器同时在线时，页面里的
        ``ui.navigate.to`` 就可能落到另一个浏览器头上。这里改成按「调用它的页面属于哪个浏览器」分派，与改绑无关。"""

        def __init__(self, user: Any, browsers: list) -> None:
            super().__init__(user)
            self._browsers = browsers

        def to(self, target: Any, new_tab: bool = False) -> None:
            owner = self.user
            with contextlib.suppress(RuntimeError):  # 不在任何页面上下文里（后台任务）：留给自己
                current = ui.context.client
                owner = next((b for b in self._browsers if b.client is current), self.user)
            if owner is self.user:
                super().to(target, new_tab)
            else:
                owner.navigate.to(target, new_tab)

    return RoutedNavigate


@dataclass
class Harness:
    user: Any  # nicegui.testing.user.User
    console: Any  # dmr_console.app.ConsoleUi
    api: FakeKernelClient
    clock: ManualClock
    streams: list = field(default_factory=list)
    browsers: list = field(default_factory=list)  # 本场景里所有浏览器（User），含自己
    extra_clients: list = field(default_factory=list)  # 额外浏览器的 HTTP 客户端，场景结束时统一关闭

    # ------------------------------------------------------------ 多个浏览器
    def new_browser(self) -> "Harness":
        """再开一个互不相干的浏览器（独立 Cookie），与本场景共用同一个控制台与内核替身。"""
        from nicegui import core
        from nicegui.testing.user import User

        http = httpx.AsyncClient(transport=httpx.ASGITransport(core.app), base_url="http://test")
        self.extra_clients.append(http)
        user = User(http)
        self.browsers.append(user)
        user.navigate = _routed_navigate_class()(user, self.browsers)
        return Harness(user=user, console=self.console, api=self.api, clock=self.clock, streams=self.streams,
                       browsers=self.browsers, extra_clients=self.extra_clients)

    # ------------------------------------------------------------ 查找
    def elements(self, marker: str) -> list:
        return sorted(self.user.find(marker=marker).elements, key=lambda e: e.id)

    def element(self, marker: str):
        found = self.elements(marker)
        assert len(found) == 1, f"期望恰好一个 {marker!r}，找到 {len(found)} 个"
        return found[0]

    def click(self, marker: str) -> None:
        self.user.find(marker=marker).click()

    def type(self, marker: str, text: str) -> None:
        self.user.find(marker=marker).type(text)

    def visible(self, marker: str) -> bool:
        return self.element(marker).visible

    def has(self, marker: str) -> bool:
        try:
            return bool(self.user.find(marker=marker).elements)
        except AssertionError:
            return False

    def text(self, marker: str) -> str:
        return self.element(marker).text

    def enabled(self, marker: str) -> bool:
        return self.element(marker).enabled

    def rows_of_table(self, marker: str) -> list[dict]:
        return self.element(marker).rows

    def browser_id(self) -> str | None:
        ids = self.console.service.sessions.browser_ids()
        return ids[0] if ids else None

    def rows(self, marker: str) -> list[dict]:
        return self.element(marker).options["rowData"]

    # ------------------------------------------------------------ 等待
    async def wait_until(self, predicate: Callable[[], bool], what: str = "条件", timeout_s: float = 10.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        last_error: Exception | None = None
        while True:
            try:
                if predicate():
                    return
            except (AssertionError, KeyError, IndexError) as exc:  # 页面还没搭好
                last_error = exc
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"等待超时：{what}" + (f"（最后一次错误：{last_error!r}）" if last_error else ""))
            await asyncio.sleep(0.02)

    async def wait_text(self, marker: str, expected: str, timeout_s: float = 10.0) -> None:
        await self.wait_until(lambda: self.text(marker) == expected, f"{marker} 的文字变成 {expected!r}", timeout_s)

    def advance(self, seconds: float) -> None:
        """推进手动时钟，让「到期才拉取」的轮询真的去拉。"""
        self.clock.advance(seconds)

    # ------------------------------------------------------------ 动作
    async def open_login(self) -> None:
        await self.user.open("/login")
        await self.wait_until(lambda: self.has("login-submit"), "登录页出现")

    async def login(self, username: str = "admin", password: str = PASSWORD, *, expect_overview: bool = True,
                    wait_data: bool = True) -> None:
        """登录并等到总览页搭好；``wait_data`` 还要等第一次拉取完成（否则紧接着注入的故障会和第一次拉取抢先后）。"""
        await self.open_login()
        self.user.find(marker="login-user").type(username)
        self.user.find(marker="login-password").type(password)
        self.user.find(marker="login-submit").click()
        if expect_overview:
            await self.wait_until(lambda: self.has("chip-gate"), "登录后出现总览的状态条")
            if wait_data:
                await self.wait_until(lambda: self.text("status-updated") != NO_DATA_TEXT, "第一次拉取完成")

    async def goto(self, path: str, marker: str) -> None:
        await self.user.open(path)
        await self.wait_until(lambda: self.has(marker), f"{path} 出现 {marker}")

    def current_path(self) -> str:
        return self.user.back_history[-1] if self.user.back_history else ""


@contextlib.asynccontextmanager
async def running_console(api: FakeKernelClient | None = None, *, poll_s: float = 0.05, tick_s: float = 0.03,
                          with_streams: bool = False, oplog_page: int = 100, dev: Any = None) -> AsyncIterator[Harness]:
    from nicegui.testing.user_simulation import user_simulation

    api = api or FakeKernelClient()
    clock = ManualClock()
    async with user_simulation() as user:
        from dmr_console.app import ConsoleUi, UiConfig, register_pages
        from dmr_console.live import HealthProbe
        from dmr_console.service import ConsoleService
        from dmr_console.session import SessionStore

        streams: list = []

        class FakeStream:
            def __init__(self, controller) -> None:
                self.controller = controller
                self.cancelled = False
                streams.append(self)

            async def run(self) -> None:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

        service = ConsoleService(api=api, clock=clock, sessions=SessionStore(clock), tz=UTC7,
                                 stream_factory=FakeStream if with_streams else None, stream_grace_s=0, oplog_page=oplog_page)
        console = ConsoleUi(service, HealthProbe(api, clock), clock, UTC7, UiConfig(poll_interval_s=poll_s, tick_s=tick_s), dev=dev)
        register_pages(console)
        browsers = [user]
        user.navigate = _routed_navigate_class()(user, browsers)
        harness = Harness(user=user, console=console, api=api, clock=clock, streams=streams, browsers=browsers)
        try:
            yield harness
        finally:
            for http in harness.extra_clients:
                await http.aclose()
            await service.aclose()
