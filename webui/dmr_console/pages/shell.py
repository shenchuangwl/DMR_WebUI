"""已登录页面共用的外壳：页眉（导航、操作员、退出）、常驻状态条、定时拉取与界面刷新、失联时按钮禁用。

两个定时器，各干各的：

- 拉取（``poll_interval_s``）：``controller.refresh_if_due`` 向内核要数据；失联、超时都在控制器里判定，这里不处理异常。
- 刷新（``tick_s``）：只读控制器里的缓存，把状态条、按钮、表格原地更新；不请求内核，所以内核卡住时「已失联 N 秒」照样走。

表格只在对应数据的版本号变了才重画；按钮的启用状态每个刷新周期都按最新的链路状态重算。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Union

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console.app import LOGIN_PATH, TITLE, ConsoleUi
from dmr_console.components.statusbar import StatusBarView
from dmr_console.components.theme import STYLE_TAG
from dmr_console.live import LiveController
from dmr_console.session import ConsoleSession
from dmr_console import operations as O
from dmr_console.state import utility_enabled

log = logging.getLogger(__name__)

NAV = (("overview", "总览", "/"), ("positions", "持仓", "/positions"),
       ("trades", "逐笔交易", "/trades"), ("stats", "统计", "/stats"), ("kline", "K 线", "/kline"), ("oplog", "操作日志", "/oplog"),
       ("watchlist", "选币名单", "/watchlist"), ("settings", "设置", "/settings"))
PART_NAMES = {"state": "总状态", "positions": "登记持仓", "external": "待确认与外部持仓", "oplog": "操作日志", "alerts": "告警"}
LOGIN_MESSAGES = {
    "expired": "会话已过期或已失效，请重新登录",
    "bye": "已退出登录",
    "bye_unconfirmed": "已退出；内核暂时没有确认，该会话会在闲置后自动过期",
}


class Shell:
    def __init__(self, console: ConsoleUi, browser_id: str, session: ConsoleSession, controller: LiveController, *, page: str,
                 parts: tuple[str, ...]) -> None:
        self.console = console
        self.browser_id = browser_id
        self.session = session
        self.controller = controller
        self.page = page
        self.parts = parts
        self.statusbar = StatusBarView()
        self.body: ui.element
        self._utility: list[tuple[ui.button, Callable[[], bool]]] = []
        self._updaters: list[Callable[[], None]] = []
        self._pollers: list[Callable[[], Awaitable[object]]] = []
        self._errors: ui.label | None = None
        self._leaving = False
        self.actions = controller.actions
        self._seat_buttons: dict[str, ui.button] = {}
        self._seat_message: ui.label | None = None
        self._dev_link: ui.link | None = None

    # ------------------------------------------------------------ 构建

    def build(self) -> None:
        ui.add_head_html(STYLE_TAG)
        with ui.header(elevated=False).classes("dmr-header"):
            with ui.column().classes("w-full gap-0"):
                with ui.row().classes("w-full items-center no-wrap dmr-topbar"):
                    ui.label(TITLE).classes("dmr-title")
                    for key, text, href in NAV:
                        active = " dmr-nav-active" if key == self.page else ""
                        ui.link(text, href).classes(f"dmr-nav{active}").mark(f"nav-{key}")
                    self._dev_link = ui.link("替身开发", "/dev").classes("dmr-nav").mark("nav-dev")
                    self._dev_link.set_visibility(False)
                    ui.space()
                    ui.label(f"操作员：{self.session.operator}").classes("dmr-operator").mark("operator-name")
                    self.utility_button("退出登录", self._logout, "btn-logout")
                self.statusbar.build_banners()
        with ui.column().classes("dmr-main w-full"):
            self.statusbar.build_chips()
            self._build_seat()
            self._errors = ui.label("").classes("dmr-part-errors").mark("part-errors")
            self.body = ui.column().classes("w-full gap-3")

    def utility_button(self, text: str, handler: Callable[[], Awaitable[None] | None], marker: str, *,
                       extra_enabled: Callable[[], bool] | None = None) -> ui.button:
        """不改变内核状态的按钮（刷新、退出、加载更多）：失联时同样禁用。"""
        button = ui.button(text, on_click=handler).props("flat dense no-caps").mark(marker)
        self._utility.append((button, extra_enabled or (lambda: True)))
        return button

    def forget_buttons(self, buttons: list[ui.button]) -> None:
        """弹窗关闭时，把它里面的按钮从「失联即禁用」的名单里去掉（元素已删除，不能再更新）。"""
        gone = {id(b) for b in buttons}
        self._utility = [(b, f) for (b, f) in self._utility if id(b) not in gone]

    def _build_seat(self) -> None:
        with ui.row().classes("items-center gap-2").mark("seat-bar"):
            ui.label("操作席位").classes("dmr-hint")
            for action, text in (("acquire", "申请席位"), ("release", "释放席位"), ("force", "强制接管")):
                handler = self._seat_force_dialog if action == "force" else (lambda a=action: self._seat_call(a))
                button = self.utility_button(text, handler, f"btn-seat-{action}")
                button.set_visibility(False)
                self._seat_buttons[action] = button
            self._seat_message = ui.label("").classes("dmr-hint").mark("seat-message")

    async def _seat_call(self, action: str, password: str | None = None) -> bool:
        outcome = await self.actions.seat(action, password=password)
        if self._seat_message is not None:
            self._seat_message.set_text("" if outcome.ok else outcome.text)
        await self._tick()
        return outcome.ok

    async def _seat_force_dialog(self) -> None:
        with self.body:
            dialog = ui.dialog().props("persistent")
        mine: list[ui.button] = []

        def close() -> None:
            self.forget_buttons(mine)
            dialog.close()
            dialog.delete()

        async def confirm() -> None:
            if await self._seat_call("force", password=password.value):
                close()
            else:
                message.set_text(self._seat_message.text if self._seat_message else "")

        with dialog, ui.card().classes("dmr-dialog").mark("dlg-seat-force"):
            ui.label("强制接管席位").classes("dmr-h2")
            ui.label("对方会话将失去席位并变为只读；需要重新输入口令，操作会写入操作日志。").classes("dmr-hint")
            password = ui.input("口令", password=True).props("autocomplete=off").mark("seat-password")
            message = ui.label("").classes("dmr-reason").mark("seat-force-message")
            with ui.row():
                confirm_button = self.utility_button("确认接管", confirm, "seat-force-confirm",
                                                     extra_enabled=lambda: bool(password.value))
                mine.append(confirm_button)
                ui.button("取消", on_click=close).props("flat no-caps").mark("seat-force-cancel")
        dialog.open()

    def add_updater(self, updater: Callable[[], None]) -> None:
        self._updaters.append(updater)

    def add_poller(self, poller: Callable[[], Awaitable[object]]) -> None:
        """页面自己的定时拉取（比如名单页的 /watchlist）：与统一的拉取同一个节奏。"""
        self._pollers.append(poller)

    def start(self) -> None:
        """页面搭好之后调用：登记在线页面、启动拉取与刷新两个定时器。"""
        self.controller.attach()
        ui.context.client.on_delete(self.controller.detach)
        ui.timer(self.console.config.poll_interval_s, self._poll)
        ui.timer(self.console.config.tick_s, self._tick)

    # ------------------------------------------------------------ 定时

    async def _poll(self) -> None:
        if self._leaving:
            return
        await self.controller.refresh_if_due(self.parts, max_age_s=self.console.config.poll_interval_s * 0.8)
        for poller in self._pollers:
            await poller()
        await self._tick()

    async def _tick(self) -> None:
        if self._leaving:
            return
        controller = self.controller
        if controller.session_invalid or controller.closed:
            await self._leave_expired()
            return
        self.statusbar.update(controller.status_bar())
        link_ok = utility_enabled(controller.link.status)
        for button, extra in self._utility:
            button.enabled = link_ok and extra()
        self._update_seat()
        if self._dev_link is not None:
            self._dev_link.set_visibility(controller.env_hint == "standin")
        if controller.state is not None and utility_enabled(controller.link.status):
            await self.actions.renew_if_due()  # 持有席位时按租约的一半续租
        if self._errors is not None:
            self._errors.set_text("；".join(f"{PART_NAMES.get(p, p)}：{controller.errors[p]}" for p in self.parts if p in controller.errors))
        for updater in self._updaters:
            try:
                updater()
            except Exception:  # noqa: BLE001 - 一个区块画错了不能拖垮整页的刷新
                log.exception("页面更新出错")

    def _update_seat(self) -> None:
        wanted = O.seat_actions(self.controller.state)
        for action, button in self._seat_buttons.items():
            button.set_visibility(action in wanted)

    async def refresh_now(self) -> None:
        await self.controller.refresh(self.parts)
        await self._tick()

    # ------------------------------------------------------------ 离开

    async def _leave_expired(self) -> None:
        self._leaving = True
        service = self.console.service
        if self.controller.session_invalid and service.session_for(self.browser_id) is self.session:
            await service.end_session(self.browser_id)  # 令牌已死：只在本地忘掉
        ui.navigate.to(f"{LOGIN_PATH}?m=expired")

    async def _logout(self) -> None:
        if self._leaving:
            return
        self._leaving = True
        outcome = await self.console.service.logout(self.browser_id)
        ui.navigate.to(f"{LOGIN_PATH}?m={'bye' if outcome.confirmed else 'bye_unconfirmed'}")


def enter(console: ConsoleUi, *, page: str, parts: tuple[str, ...]) -> Union[Shell, RedirectResponse]:
    """每个已登录页面的入口：没有有效会话就送去登录页；否则搭好外壳。"""
    browser_id = console.browser_id()
    session = console.service.session_for(browser_id)
    controller = console.service.controller_for(browser_id) if session is not None else None
    if browser_id is None or session is None or controller is None:
        return RedirectResponse(LOGIN_PATH)
    shell = Shell(console, browser_id, session, controller, page=page, parts=parts)
    shell.build()
    return shell
