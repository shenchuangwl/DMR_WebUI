"""总览页与常驻状态条的冒烟测试：字段齐全、替身标注、只读、失联时全部按钮禁用并提示、令牌不出服务端、多浏览器互相隔离。"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from console_kit import GOOD_TOKEN, FakeKernelClient, lost, make_alert, make_state
from dmr_console.client import KernelError, LoginResult
from dmr_console.state import LOST_MESSAGE, OPERATION_ORDER, STATUS_BAR_FIELDS
from ui_kit import PASSWORD, running_console

BUTTON_MARKERS = [f"btn-{key}" for key in OPERATION_ORDER]


def all_buttons(h):
    from nicegui import ui

    return list(h.user.find(ui.button).elements)


# ------------------------------------------------------------------ 状态条

async def test_every_status_bar_field_is_on_screen_with_the_kernel_data():
    async with running_console() as h:
        await h.login()
        for key in STATUS_BAR_FIELDS:
            assert h.has(f"chip-{key}"), key
            assert h.text(f"chip-{key}"), key
        assert h.text("chip-gate") == "已停止开仓 · 托管中 0 个"
        assert h.text("chip-model") == "模型 1 · 完全自动化量化交易"
        assert h.text("chip-env") == "替身（standin）"
        assert h.text("chip-account") == "1,000.00 / 900.00 USDT"
        assert h.text("chip-unprotected") == "0"
        assert h.text("chip-external") == "外部 0 · 待确认 0"
        assert h.text("chip-seat") == "无人持有"
        assert h.text("chip-stream") == "已连接 · 最后一帧 1 秒前"
        assert h.text("chip-position_mode") == "双向持仓（对冲）"
        assert h.text("chip-run_mode") == "正常"


async def test_the_standin_environment_is_marked_prominently_on_every_page():
    async with running_console() as h:
        await h.login()
        for path, marker in (("/", "chip-gate"), ("/positions", "group-strategy"), ("/oplog", "btn-load-more")):
            await h.goto(path, marker)
            await h.wait_until(lambda: h.has("banner-standin"), f"{path} 的替身横幅")
            assert h.text("banner-text-standin") == "替身环境 · 非真实交易"


async def test_unprotected_positions_are_shown_in_the_danger_tone_and_external_ones_in_warning():
    api = FakeKernelClient()
    api.set("state", make_state(counts={"managed": 3, "unprotected": 2, "pending": 1, "external": 4}))
    async with running_console(api) as h:
        await h.login()
        assert h.text("chip-unprotected") == "2"
        assert "dmr-tone-danger" in h.element("chip-box-unprotected").classes
        assert h.text("chip-gate") == "已停止开仓 · 托管中 3 个"
        assert "dmr-tone-warn" in h.element("chip-box-external").classes


async def test_polling_picks_up_new_kernel_state_without_any_user_action():
    api = FakeKernelClient()
    async with running_console(api) as h:
        await h.login()
        api.set("state", make_state(model={"gate": "running"}, counts={"managed": 5}))
        h.advance(60)
        await h.wait_text("chip-gate", "运行中 · 托管中 5 个")


# ------------------------------------------------------------------ 只读的操作按钮

async def test_the_four_operations_and_the_model_switch_are_listed_with_their_meaning_and_greyed_without_the_seat():
    from dmr_console.state import SEAT_REASON

    async with running_console() as h:
        await h.login()
        assert not h.has("readonly-notice")  # 操作已开放，不再有「只读」提示
        for key in OPERATION_ORDER:
            assert h.has(f"btn-{key}"), key
            assert not h.enabled(f"btn-{key}"), key  # 默认状态下没有席位：置灰并说明原因
        for key in ("start", "switch"):  # 内核说可执行，只差席位
            assert h.text(f"reason-{key}") == SEAT_REASON
        assert h.text("reason-stop") == "开仓闸已关闭"
        assert h.text("meaning-start") == "打开开仓闸，按信号自动开仓"
        assert h.text("meaning-close_all") == "默认平策略仓"
        assert [h.element(f"btn-{key}").text for key in OPERATION_ORDER] == ["启动", "停止", "部分平仓", "一键全部平仓", "切换模型"]


async def test_the_allowed_operations_table_shows_why_each_one_is_not_available():
    async with running_console() as h:
        await h.login()
        await h.wait_until(lambda: len(h.element("allowed-table").rows) >= 5, "可执行操作表")
        rows = {r["key"]: r for r in h.element("allowed-table").rows}
        assert rows["start"]["text"] == "可执行" and rows["stop"]["text"] == "不可执行" and rows["stop"]["reason"] == "开仓闸已关闭"


async def test_recent_alerts_are_listed_on_the_overview():
    api = FakeKernelClient()
    api.set("alerts", [make_alert(id=2, level="critical", message="风控触发"), make_alert(id=1)])
    async with running_console(api) as h:
        await h.login()
        await h.wait_until(lambda: len(h.rows("alerts-grid")) == 2, "告警表有两行")
        assert h.rows("alerts-grid")[0]["message"] == "风控触发" and h.rows("alerts-grid")[0]["level"] == "严重"


# ------------------------------------------------------------------ 失联

async def test_when_the_kernel_is_lost_every_button_is_disabled_and_the_banner_says_what_to_do():
    async with running_console() as h:
        await h.login()
        assert any(b.enabled for b in all_buttons(h))  # 正常时刷新、退出是可点的
        h.api.fail_all(lost("timeout"))
        h.advance(60)
        await h.wait_until(lambda: h.has("banner-lost"), "失联横幅")
        assert h.text("banner-text-lost") == "与内核失联：请改用命令行应急入口，或直接在 Binance 处置"
        await h.wait_until(lambda: not any(b.enabled for b in all_buttons(h)), "全部按钮禁用")
        assert len(all_buttons(h)) >= len(BUTTON_MARKERS) + 2  # 五个操作按钮 + 刷新 + 退出
        for key in OPERATION_ORDER:
            assert h.text(f"reason-{key}") == LOST_MESSAGE
        assert h.text("banner-detail-lost")  # 具体原因与失联时长


async def test_data_from_before_the_loss_stays_on_screen_and_is_labelled():
    async with running_console() as h:
        await h.login()
        h.api.fail_all(lost("connect"))
        h.advance(60)
        await h.wait_until(lambda: h.has("banner-lost"), "失联横幅")
        assert h.text("chip-account") == "1,000.00 / 900.00 USDT"
        await h.wait_until(lambda: h.text("status-updated").startswith("失联前的数据"), "标注失联前的数据")
        assert h.has("banner-standin")


async def test_recovery_removes_the_banner_and_re_enables_only_the_read_only_utilities():
    async with running_console() as h:
        await h.login()
        h.api.fail_all(lost("timeout"))
        h.advance(60)
        await h.wait_until(lambda: h.has("banner-lost"), "失联横幅")
        h.api.heal()
        h.advance(60)
        await h.wait_until(lambda: not h.has("banner-lost"), "横幅消失")
        await h.wait_until(lambda: h.enabled("btn-refresh") and h.enabled("btn-logout"), "刷新与退出恢复")
        assert all(not h.enabled(m) for m in BUTTON_MARKERS)  # 没有席位：操作按钮仍禁用


async def test_the_lost_banner_and_disabled_buttons_follow_the_operator_to_the_other_pages():
    async with running_console() as h:
        await h.login()
        h.api.fail_all(lost("protocol"))
        h.advance(60)
        await h.wait_until(lambda: h.has("banner-lost"), "总览失联横幅")
        for path, marker in (("/positions", "group-strategy"), ("/oplog", "btn-load-more")):
            await h.goto(path, marker)
            await h.wait_until(lambda: h.has("banner-lost"), f"{path} 的失联横幅")
            await h.wait_until(lambda: not any(b.enabled for b in all_buttons(h)), f"{path} 的按钮全部禁用")


async def test_a_kernel_refusal_for_one_part_is_shown_without_calling_it_a_loss():
    api = FakeKernelClient()
    async with running_console(api) as h:
        await h.login()
        await h.goto("/positions", "group-strategy")
        api.set("positions", KernelError(503, "unavailable", "持仓快照暂时读不到"))
        h.advance(60)
        await h.wait_until(lambda: "持仓快照暂时读不到" in h.text("part-errors"), "分部分的错误提示")
        assert not h.has("banner-lost") and h.enabled("btn-refresh")


# ------------------------------------------------------------------ 事件流只在有页面时运行

async def test_the_event_stream_runs_while_a_page_is_open_and_stops_after_logout():
    async with running_console(with_streams=True) as h:
        await h.login()
        await h.wait_until(lambda: len(h.streams) == 1, "事件流启动")
        assert not h.streams[0].cancelled
        await h.goto("/positions", "group-strategy")
        assert len(h.streams) == 1  # 换页不重启
        h.user.find(marker="btn-logout").click()
        await h.wait_until(lambda: h.streams[0].cancelled, "退出后事件流停止")


# ------------------------------------------------------------------ 令牌与多浏览器

def _two_account_api() -> FakeKernelClient:
    api = FakeKernelClient(valid_tokens={"tok-admin", "tok-bob"})

    def login(username: str, password: str) -> LoginResult:
        if {"admin": "pw-admin", "bob": "pw-bob"}.get(username) != password:
            raise KernelError(401, "bad_credentials", "用户名或口令错误")
        return LoginResult(token=f"tok-{username}", operator=username, session_id=f"sess-{username}", idle_timeout_min=30)

    api.set("login", login)
    return api


async def test_two_browsers_hold_separate_sessions_and_logging_one_out_leaves_the_other():
    api = _two_account_api()
    async with running_console(api) as h:
        await h.login("admin", "pw-admin")
        assert h.text("operator-name") == "操作员：admin"
        second = h.new_browser()
        await second.login("bob", "pw-bob")
        assert second.text("operator-name") == "操作员：bob"
        sessions = h.console.service.sessions
        assert sorted(sessions.get(b).token for b in sessions.browser_ids()) == ["tok-admin", "tok-bob"]
        second.user.find(marker="btn-logout").click()
        await second.wait_until(lambda: second.has("login-submit"), "第二个浏览器退出")
        assert [sessions.get(b).token for b in sessions.browser_ids()] == ["tok-admin"]
        assert "tok-bob" not in api.valid_tokens and "tok-admin" in api.valid_tokens
        before = len(api.calls_of("state"))
        h.advance(60)
        await h.wait_until(lambda: any(args[0] == "tok-admin" for args in api.calls_of("state")[before:]), "第一个浏览器仍在拉取")
        assert h.has("chip-gate") and h.text("operator-name") == "操作员：admin"


async def test_a_fresh_browser_has_no_session_even_while_another_one_is_logged_in():
    async with running_console() as h:
        await h.login()
        guest = h.new_browser()
        await guest.user.open("/")
        await guest.wait_until(lambda: guest.has("login-submit"), "陌生浏览器被送去登录")
        assert not guest.has("chip-gate")


async def test_the_kernel_token_never_reaches_the_browser_or_the_nicegui_storage():
    from nicegui import app

    async with running_console() as h:
        await h.login()
        await h.goto("/positions", "group-strategy")
        await h.goto("/oplog", "btn-load-more")
        await h.goto("/", "chip-gate")
        h.advance(60)
        await h.wait_until(lambda: len(h.api.calls_of("state")) >= 2, "已轮询过")

        # 1. 页面元素（属性、文字、类、样式、标记）里没有令牌
        for element in h.user.client.elements.values():
            blob = json.dumps([element._props, getattr(element, "text", ""), list(element._classes), dict(element._style),
                               element._markers], default=str, ensure_ascii=False)
            assert GOOD_TOKEN not in blob, element
        # 2. 页面 HTML 里没有令牌
        for path in ("/login", "/", "/positions", "/oplog"):
            response = await h.user.http_client.get(path, follow_redirects=True)
            assert GOOD_TOKEN not in response.text
        # 3. Cookie 里只有浏览器会话编号（已签名），没有令牌
        cookies = list(h.user.http_client.cookies.jar)
        assert cookies  # 确实有会话 Cookie，下面的检查不是空转
        for cookie in cookies:
            assert GOOD_TOKEN not in cookie.value
            payload = cookie.value.split(".")[0]
            decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", "replace")
            assert GOOD_TOKEN not in decoded
        # 4. 令牌只在服务端会话存储里；NiceGUI 的存储（general/user）是空的，也没有落盘
        assert GOOD_TOKEN in [s.token for s in (h.console.service.sessions.get(b) for b in h.console.service.sessions.browser_ids())]
        assert not dict(app.storage.general)
        assert all(not dict(user) for user in app.storage._users.values())
        assert not (Path.cwd() / ".nicegui").exists()
