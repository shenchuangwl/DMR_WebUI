"""登录页与访问控制的冒烟测试（NiceGUI 用户模拟 + 假内核，不连网）。"""
from __future__ import annotations

from console_kit import GOOD_TOKEN, FakeKernelClient, lost
from dmr_console.client import KernelError
from ui_kit import PASSWORD, running_console


async def test_the_login_page_shows_the_form_and_marks_the_standin_environment():
    async with running_console() as h:
        await h.open_login()
        for marker in ("login-user", "login-password", "login-submit", "login-error"):
            assert h.has(marker), marker
        assert h.enabled("login-submit")
        await h.wait_until(lambda: h.has("banner-standin"), "替身环境横幅")
        assert h.text("banner-text-standin") == "替身环境 · 非真实交易"
        assert not h.has("banner-lost")


async def test_wrong_credentials_show_an_error_and_clear_the_password_field():
    async with running_console() as h:
        await h.open_login()
        h.user.find(marker="login-user").type("admin")
        h.user.find(marker="login-password").type("wrong")
        h.user.find(marker="login-submit").click()
        await h.wait_text("login-error", "用户名或口令错误")
        assert h.element("login-password").value == ""
        assert h.current_path() == "/login"
        assert len(h.console.service.sessions) == 0


async def test_good_credentials_open_the_overview_with_the_status_bar():
    async with running_console() as h:
        await h.login()
        assert h.current_path() == "/"
        assert h.text("operator-name") == "操作员：admin"
        assert h.text("banner-text-standin") == "替身环境 · 非真实交易"
        assert len(h.console.service.sessions) == 1
        assert h.api.calls_of("login") == [("admin", PASSWORD)]


async def test_pressing_enter_in_the_password_field_submits():
    async with running_console() as h:
        await h.open_login()
        h.user.find(marker="login-user").type("admin")
        h.user.find(marker="login-password").type(PASSWORD).trigger("keydown.enter")
        await h.wait_until(lambda: h.has("chip-gate"), "回车提交后进入总览")


async def test_the_login_button_is_disabled_while_the_kernel_is_unreachable_and_the_lost_message_is_shown():
    api = FakeKernelClient()
    api.set("health", lost("connect"))
    async with running_console(api) as h:
        await h.open_login()
        await h.wait_until(lambda: h.has("banner-lost"), "失联横幅")
        assert h.text("banner-text-lost") == "与内核失联：请改用命令行应急入口，或直接在 Binance 处置"
        await h.wait_until(lambda: not h.enabled("login-submit"), "登录按钮禁用")
        h.user.find(marker="login-submit").click()  # 禁用的按钮点不动
        assert h.api.calls_of("login") == []
        api.heal()
        await h.wait_until(lambda: h.enabled("login-submit") and not h.has("banner-lost"), "内核恢复后按钮可用、横幅消失")


async def test_a_login_attempt_that_cannot_reach_the_kernel_says_so_and_disables_the_button():
    api = FakeKernelClient()
    api.set("login", lost("timeout"))
    async with running_console(api) as h:
        await h.open_login()
        h.user.find(marker="login-user").type("admin")
        h.user.find(marker="login-password").type(PASSWORD)
        h.user.find(marker="login-submit").click()
        await h.wait_text("login-error", "与内核失联：请改用命令行应急入口，或直接在 Binance 处置")
        assert len(h.console.service.sessions) == 0


async def test_rate_limiting_is_explained():
    api = FakeKernelClient()
    api.set("login", KernelError(429, "rate_limited", "失败次数过多", {"retry_after_s": 42}))
    async with running_console(api) as h:
        await h.open_login()
        h.user.find(marker="login-user").type("admin")
        h.user.find(marker="login-password").type("x")
        h.user.find(marker="login-submit").click()
        await h.wait_until(lambda: "42" in h.text("login-error"), "提示等待秒数")


async def test_empty_fields_are_reported_without_calling_the_kernel():
    async with running_console() as h:
        await h.open_login()
        h.user.find(marker="login-submit").click()
        await h.wait_until(lambda: "用户名" in h.text("login-error"), "空字段提示")
        assert h.api.calls_of("login") == []


async def test_every_page_sends_a_visitor_without_a_session_to_the_login_page():
    async with running_console() as h:
        for path in ("/", "/positions", "/oplog"):
            await h.user.open(path)
            await h.wait_until(lambda: h.has("login-submit"), f"{path} 跳到登录页")
            assert not h.has("chip-gate")


async def test_a_browser_that_is_already_logged_in_skips_the_login_page():
    async with running_console() as h:
        await h.login()
        await h.user.open("/login")
        await h.wait_until(lambda: h.has("chip-gate"), "已登录时 /login 直接进总览")


async def test_the_session_expiring_in_the_kernel_sends_the_operator_back_to_login():
    async with running_console() as h:
        await h.login()
        h.api.valid_tokens.discard(GOOD_TOKEN)
        h.advance(60)
        await h.wait_until(lambda: h.has("login-submit"), "令牌失效后回到登录页")
        await h.wait_until(lambda: "过期" in h.text("login-notice"), "显示会话已过期")
        assert len(h.console.service.sessions) == 0


async def test_logging_out_revokes_the_token_and_the_pages_close_again():
    async with running_console() as h:
        await h.login()
        h.user.find(marker="btn-logout").click()
        await h.wait_until(lambda: h.has("login-submit"), "退出后回到登录页")
        assert h.api.calls_of("logout") == [(GOOD_TOKEN,)]
        assert len(h.console.service.sessions) == 0
        await h.wait_until(lambda: "已退出" in h.text("login-notice"), "显示已退出")
        await h.user.open("/")
        await h.wait_until(lambda: h.has("login-submit"), "退出后再访问 / 仍要登录")
