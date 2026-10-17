"""登录页：用户名、口令 → 内核登录接口；令牌只存服务端，浏览器拿到的只是签名 Cookie 里的随机编号。

内核失联（连不上、超时）时登录按钮禁用，并显示统一的失联提示；替身环境在这里同样醒目标注。
"""
from __future__ import annotations

import logging

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console.app import TITLE, ConsoleUi
from dmr_console.components.statusbar import StatusBarView
from dmr_console.components.theme import STYLE_TAG
from dmr_console.pages.shell import LOGIN_MESSAGES
from dmr_console.state import build_status_bar, utility_enabled

log = logging.getLogger(__name__)


def register(console: ConsoleUi) -> None:
    @ui.page("/login", title=f"登录 · {TITLE}")
    def login_page(m: str | None = None):
        browser_id = console.browser_id()
        if console.service.session_for(browser_id) is not None:
            return RedirectResponse("/")  # 已经登录：直接进总览
        probe = console.probe
        view = StatusBarView()
        state = {"busy": False}

        ui.add_head_html(STYLE_TAG)
        with ui.column().classes("w-full gap-0"):
            view.build_banners()
        with ui.column().classes("absolute-center items-stretch dmr-login gap-3"):
            ui.label(TITLE).classes("dmr-title text-h5")
            notice = ui.label(LOGIN_MESSAGES.get(m or "", "")).classes("dmr-hint").mark("login-notice")
            username = ui.input("用户名").props("outlined dense autofocus autocomplete=username").mark("login-user")
            password = ui.input("口令", password=True, password_toggle_button=True).props(
                "outlined dense autocomplete=current-password").mark("login-password")
            error = ui.label("").classes("dmr-login-error").mark("login-error")
            button = ui.button("登录").props("no-caps").mark("login-submit")

        def refresh_view() -> None:
            bar = build_status_bar(None, probe.link.status, now_ms=console.clock.now_ms(), fetched_ms=None, tz=console.tz,
                                   env_hint=probe.env)
            view.update_banners(bar.banners)
            button.enabled = utility_enabled(probe.link.status) and not state["busy"]

        async def submit() -> None:
            if state["busy"]:
                return
            state["busy"] = True
            refresh_view()
            try:
                outcome = await console.service.login(browser_id or "", username.value or "", password.value or "")
            finally:
                state["busy"] = False
                password.value = ""  # 口令不在页面上多留一刻
            if outcome.ok:
                error.set_text("")
                ui.navigate.to("/")
                return
            if outcome.unreachable:
                probe.link.record_failure(outcome.detail or outcome.message, "connect")
            error.set_text(outcome.message)
            refresh_view()

        async def probe_kernel() -> None:
            await probe.check()
            refresh_view()

        button.on_click(submit)
        password.on("keydown.enter", submit)
        username.on("keydown.enter", submit)
        refresh_view()
        ui.timer(console.config.poll_interval_s, probe_kernel)
