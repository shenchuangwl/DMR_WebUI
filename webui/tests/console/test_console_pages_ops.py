"""统一操作的页面冒烟（NiceGUI 用户模拟 + 假内核客户端）：每个弹窗 打开 → 填写 → 发出正确的请求。"""
from __future__ import annotations

import copy

from console_kit import FakeKernelClient, lost, make_position, make_state
from ui_kit import running_console

from dmr_console.client import KernelError
from dmr_console.state import SEAT_REASON

ALL_OPS = ("start", "stop", "switch", "switch_confirm", "switch_cancel", "partial_close", "close_one", "close_all", "abort")


def ops_state(*, seat="mine", **over):
    allowed = {k: {"allowed": True, "reason": "", "needs_live_read": False, "note": ""} for k in ALL_OPS}
    seats = {
        "mine": {"holder_session_id": "sess-1", "holder_operator": "admin", "lease_until_ms": None, "mine": True},
        "free": {"holder_session_id": None, "holder_operator": None, "lease_until_ms": None, "mine": False},
        "other": {"holder_session_id": "sess-9", "holder_operator": "bob", "lease_until_ms": 1, "mine": False},
    }
    return make_state(seat=seats[seat], allowed=allowed, **over)


def api_with(state=None, positions=None):
    api = FakeKernelClient()
    api.set("state", state or ops_state())
    api.set("positions", positions if positions is not None else [make_position()])
    return api


async def open_dialog(h, button, dialog_marker):
    await h.wait_until(lambda: h.enabled(button), f"{button} 可点")
    h.click(button)
    await h.wait_until(lambda: h.has(dialog_marker), f"弹窗 {dialog_marker}")


# ------------------------------------------------------------------ 按钮与置灰

async def test_buttons_follow_the_allowed_table_and_the_seat_and_show_the_reason():
    state = ops_state()
    state["allowed"]["stop"] = {"allowed": False, "reason": "开仓闸已关闭", "needs_live_read": False, "note": ""}
    async with running_console(api_with(state)) as h:
        await h.login()
        await h.wait_until(lambda: h.enabled("btn-start"), "启动可点")
        assert h.enabled("btn-close_all") and h.enabled("btn-partial_close") and h.enabled("btn-switch")
        assert not h.enabled("btn-stop") and h.text("reason-stop") == "开仓闸已关闭"
        assert h.text("reason-start") == ""
        assert not h.has("readonly-notice")


async def test_without_the_seat_everything_is_greyed_with_the_seat_reason():
    async with running_console(api_with(ops_state(seat="free"))) as h:
        await h.login()
        for key in ("start", "stop", "partial_close", "close_all", "switch"):
            assert not h.enabled(f"btn-{key}") and h.text(f"reason-{key}") == SEAT_REASON


async def test_the_four_unified_operations_keep_their_names_and_order():
    async with running_console(api_with()) as h:
        await h.login()
        names = [h.element(f"btn-{k}").text for k in ("start", "stop", "partial_close", "close_all", "switch")]
        assert names == ["启动", "停止", "部分平仓", "一键全部平仓", "切换模型"]
        assert h.text("meaning-start") == "打开开仓闸，按信号自动开仓"


# ------------------------------------------------------------------ 启动 / 停止

async def test_start_sends_the_model_version_and_shows_the_self_checks():
    api = api_with()
    api.set("POST /ops/start", {"op_id": "x", "status": "succeeded",
                                "result": {"gate": "running", "model": "m1", "checks": [{"name": "对冲模式", "ok": True, "detail": ""}]}})
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-start", "dlg-start")
        h.click("start-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/start"), "发出启动请求")
        body = api.posts_to("/ops/start")[0]
        assert body["model_version"] == 12 and body["op_id"].startswith("c-") and "password" not in body
        await h.wait_until(lambda: "已启动" in h.text("start-message"), "成功提示")
        assert "对冲模式" in h.text("start-checks")


async def test_a_failed_self_check_is_listed_with_the_kernel_reason():
    api = api_with()
    api.set("POST /ops/start", KernelError(409, "not_allowed", "自检未通过", {"checks": [{"name": "对账无差异", "ok": False, "detail": "有 1 处差异"}]}))
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-start", "dlg-start")
        h.click("start-confirm")
        await h.wait_until(lambda: "自检未通过" in h.text("start-message"), "拒绝原因")
        assert "未通过" in h.text("start-checks") and "有 1 处差异" in h.text("start-checks")


async def test_retrying_after_losing_the_kernel_reuses_the_same_op_id():
    api = api_with()
    api.set("POST /ops/start", lost("timeout"))
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-start", "dlg-start")
        h.click("start-confirm")
        await h.wait_until(lambda: len(api.posts_to("/ops/start")) == 1, "第一次请求")
        await h.wait_until(lambda: "超时" in h.text("start-message"), "结果未知的提示")
        api.script.pop("POST /ops/start")
        await h.wait_until(lambda: h.enabled("start-confirm"), "确认键恢复")
        h.click("start-confirm")
        await h.wait_until(lambda: len(api.posts_to("/ops/start")) == 2, "重试")
        first, second = api.posts_to("/ops/start")
        assert first["op_id"] == second["op_id"]


async def test_stop_with_close_all_sends_the_password():
    api = api_with()
    api.set("POST /ops/stop", {"op_id": "x", "status": "running", "result": {"gate": "stopped", "close_all_op_id": "ca-1"}})
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-stop", "dlg-stop")
        h.click("stop-close-all")
        h.type("stop-password", "pw-admin")
        h.click("stop-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/stop"), "发出停止请求")
        body = api.posts_to("/ops/stop")[0]
        assert body["close_all"] is True and body["password"] == "pw-admin" and body["model_version"] == 12
        await h.wait_until(lambda: "ca-1" in h.text("stop-message"), "显示另起的一键全平操作号")


async def test_plain_stop_sends_no_password_and_no_close_all():
    api = api_with()
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-stop", "dlg-stop")
        h.click("stop-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/stop"), "发出停止请求")
        assert set(api.posts_to("/ops/stop")[0]) == {"op_id", "model_version"}


# ------------------------------------------------------------------ 部分平仓

PREVIEW = {"position_id": 1, "position_version": 3, "live_qty": "0.010", "close_qty": "0.005", "remaining_qty": "0.005",
           "remaining_notional": "300", "min_notional": "100", "mark_price": "60100", "warnings": []}


async def test_a_row_partial_close_goes_preview_then_confirm():
    api = api_with()
    api.set("POST /ops/partial_close/preview", {"op_id": None, "status": "succeeded", "result": PREVIEW})
    async with running_console(api) as h:
        await h.login()
        await h.goto("/positions", "group-strategy")
        await open_dialog(h, "btn-partial-1", "dlg-partial")
        assert not h.enabled("partial-confirm")  # 预览之前不能确认
        h.click("partial-ratio-50")
        await h.wait_until(lambda: "0.005" in h.text("partial-preview"), "预览结果")
        assert api.posts_to("/ops/partial_close/preview") == [{"position_id": 1, "ratio": 0.5}]
        await h.wait_until(lambda: h.enabled("partial-confirm"), "可确认")
        h.click("partial-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/partial_close/confirm"), "发出确认")
        body = api.posts_to("/ops/partial_close/confirm")[0]
        assert {k: v for k, v in body.items() if k != "op_id"} == {
            "position_id": 1, "position_version": 3, "preview_qty": "0.010", "close_qty": "0.005"}
        await h.wait_until(lambda: "已提交" in h.text("partial-message"), "成功提示")


async def test_custom_percentage_is_validated_and_sent_as_a_ratio():
    api = api_with()
    api.set("POST /ops/partial_close/preview", {"op_id": None, "status": "succeeded", "result": PREVIEW})
    async with running_console(api) as h:
        await h.login()
        await h.goto("/positions", "group-strategy")
        await open_dialog(h, "btn-partial-1", "dlg-partial")
        h.type("partial-custom", "150")
        h.click("partial-custom-go")
        await h.wait_until(lambda: "大于 0 小于 100" in h.text("partial-message"), "越界提示")
        assert api.posts_to("/ops/partial_close/preview") == []
        h.element("partial-custom").set_value("30")
        h.click("partial-custom-go")
        await h.wait_until(lambda: api.posts_to("/ops/partial_close/preview"), "发出预览")
        assert api.posts_to("/ops/partial_close/preview")[0]["ratio"] == 0.3


async def test_a_stale_confirm_asks_for_a_new_preview_and_blocks_confirming_again():
    api = api_with()
    api.set("POST /ops/partial_close/preview", {"op_id": None, "status": "succeeded", "result": PREVIEW})
    api.set("POST /ops/partial_close/confirm", KernelError(409, "stale_version", "持仓已变化"))
    async with running_console(api) as h:
        await h.login()
        await h.goto("/positions", "group-strategy")
        await open_dialog(h, "btn-partial-1", "dlg-partial")
        h.click("partial-ratio-25")
        await h.wait_until(lambda: h.enabled("partial-confirm"), "可确认")
        h.click("partial-confirm")
        await h.wait_until(lambda: "重新预览" in h.text("partial-message"), "要求重新预览")
        await h.wait_until(lambda: not h.enabled("partial-confirm"), "确认键禁用")


async def test_the_overview_partial_close_picks_a_managed_position_first():
    api = api_with(positions=[make_position(id=1), make_position(id=2, symbol="ETHUSDT", status="pending", ownership="none")])
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-partial_close", "dlg-pick-partial")
        assert h.has("pick-partial-1") and not h.has("pick-partial-2")
        h.click("pick-partial-1")
        await h.wait_until(lambda: h.has("dlg-partial"), "进入部分平仓弹窗")


# ------------------------------------------------------------------ 平掉此仓

async def test_close_one_needs_a_single_confirmation_and_sends_the_position_version():
    api = api_with()
    async with running_console(api) as h:
        await h.login()
        await h.goto("/positions", "group-strategy")
        await open_dialog(h, "btn-closeone-1", "dlg-close-one")
        assert not h.has("closeone-password")  # 不需要口令
        h.click("closeone-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/close_one"), "发出平仓")
        body = api.posts_to("/ops/close_one")[0]
        assert {k: v for k, v in body.items() if k != "op_id"} == {"position_id": 1, "position_version": 3}
        await h.wait_until(lambda: "已提交" in h.text("closeone-message"), "成功提示")


async def test_row_buttons_are_greyed_by_the_allowed_table():
    state = ops_state()
    state["allowed"]["close_one"] = {"allowed": False, "reason": "没有托管持仓", "needs_live_read": False, "note": ""}
    async with running_console(api_with(state)) as h:
        await h.login()
        await h.goto("/positions", "group-strategy")
        await h.wait_until(lambda: h.enabled("btn-partial-1"), "部分平仓可点")
        assert not h.enabled("btn-closeone-1")
        assert "没有托管持仓" in h.text("rowops-reason")


# ------------------------------------------------------------------ 一键全平

CA_PREVIEW = {"scope": "all_managed", "model_version": 12,
              "included": [{"id": 1, "symbol": "BTCUSDT", "side": "LONG", "qty": "0.010"}],
              "excluded": [{"position": {"id": 5, "symbol": "ETHUSDT", "side": "SHORT", "qty": "1"}, "reason": "外部持仓永不包含"}],
              "mixed_included": []}


async def test_close_all_previews_each_scope_then_confirms_with_a_password_and_shows_per_position_results():
    api = api_with()
    api.set("POST /ops/close_all/preview", lambda body: {"op_id": None, "status": "succeeded", "result": {**CA_PREVIEW, "scope": body["scope"]}})
    api.set("POST /ops/close_all/confirm", {"op_id": "x", "status": "running", "result": {"status": "running"}})
    done = {"op_id": "x", "status": "succeeded", "per_position": [
        {"position_id": 1, "symbol": "BTCUSDT", "side": "LONG", "result": "closed", "detail": ""}]}
    api.set("GET /operations", done)
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-close_all", "dlg-close-all")
        assert not h.enabled("ca-confirm")
        h.click("ca-scope-model")
        await h.wait_until(lambda: api.posts_to("/ops/close_all/preview") == [{"scope": "model"}], "第一档预览")
        h.click("ca-scope-all_managed")
        await h.wait_until(lambda: len(api.posts_to("/ops/close_all/preview")) == 2, "第二档预览")
        assert api.posts_to("/ops/close_all/preview")[1] == {"scope": "all_managed"}
        await h.wait_until(lambda: "BTCUSDT" in h.text("ca-included"), "包含列表")
        assert "外部持仓永不包含" in h.text("ca-excluded")
        h.type("ca-password", "pw-admin")
        await h.wait_until(lambda: h.enabled("ca-confirm"), "可确认")
        h.click("ca-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/close_all/confirm"), "发出确认")
        body = api.posts_to("/ops/close_all/confirm")[0]
        assert {k: v for k, v in body.items() if k != "op_id"} == {"model_version": 12, "scope": "all_managed", "password": "pw-admin"}
        await h.wait_until(lambda: h.rows_of_table("ca-results"), "逐仓结果")
        assert h.rows_of_table("ca-results")[0]["result"] == "已平"
        assert "完成" in h.text("ca-progress")


async def test_close_all_can_be_aborted_while_running():
    api = api_with()
    api.set("POST /ops/close_all/preview", {"op_id": None, "status": "succeeded", "result": CA_PREVIEW})
    api.set("POST /ops/close_all/confirm", {"op_id": "x", "status": "running", "result": {"status": "running"}})
    api.set("GET /operations", {"op_id": "x", "status": "running", "per_position": []})
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-close_all", "dlg-close-all")
        h.click("ca-scope-all_managed")
        await h.wait_until(lambda: h.enabled("ca-password") if h.has("ca-password") else False, "口令框")
        h.type("ca-password", "pw-admin")
        await h.wait_until(lambda: h.enabled("ca-confirm"), "可确认")
        h.click("ca-confirm")
        await h.wait_until(lambda: h.enabled("ca-abort"), "可中止")
        h.click("ca-abort")
        await h.wait_until(lambda: api.posts_to("/ops/abort"), "发出中止")
        body = api.posts_to("/ops/abort")[0]
        confirm = api.posts_to("/ops/close_all/confirm")[0]
        assert body["target_op_id"] == confirm["op_id"] and body["op_id"] != confirm["op_id"]


async def test_close_all_confirm_needs_a_password():
    api = api_with()
    api.set("POST /ops/close_all/preview", {"op_id": None, "status": "succeeded", "result": CA_PREVIEW})
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-close_all", "dlg-close-all")
        h.click("ca-scope-model")
        await h.wait_until(lambda: "BTCUSDT" in h.text("ca-included"), "包含列表")
        assert not h.enabled("ca-confirm")  # 口令为空


# ------------------------------------------------------------------ 模型切换

SW_PREVIEW = {"model_version": 12, "current": {"active": "m1", "gate": "stopped"}, "counts_by_ownership": {"strategy": 2},
              "open_orders": [], "expires_ms": 1, "notes": ["切到模型 3：不开仓，也不接管新仓"]}


async def test_switch_goes_target_then_preview_then_mode_and_password_then_progress():
    api = api_with()
    api.set("POST /ops/switch/preview", {"op_id": "x", "status": "succeeded", "result": SW_PREVIEW})
    api.set("POST /ops/switch/confirm", {"op_id": "x", "status": "succeeded",
                                         "result": {"status": "succeeded", "active_model": "m3", "gate": "stopped", "detail": ""}})
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-switch", "dlg-switch")
        assert not h.has("sw-confirm")
        h.click("sw-target-m3")
        await h.wait_until(lambda: api.posts_to("/ops/switch/preview"), "发出预览")
        assert api.posts_to("/ops/switch/preview")[0]["target"] == "m3"
        await h.wait_until(lambda: "策略 2" in h.text("sw-summary"), "预览摘要")
        assert "不开仓" in h.text("sw-summary")
        assert not h.enabled("sw-confirm")  # 还没选方式、没填口令
        h.click("sw-mode-close_all_first")
        h.type("sw-password", "pw-admin")
        await h.wait_until(lambda: h.enabled("sw-confirm"), "可确认")
        h.click("sw-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/switch/confirm"), "发出确认")
        body = api.posts_to("/ops/switch/confirm")[0]
        assert {k: v for k, v in body.items() if k != "op_id"} == {"model_version": 12, "mode": "close_all_first", "password": "pw-admin"}
        await h.wait_until(lambda: "已切换" in h.text("sw-message"), "成功提示")
        assert "m3" in h.text("sw-message") or "模型 3" in h.text("sw-message")


async def test_a_switch_that_cannot_settle_stays_on_the_old_model_and_says_so():
    api = api_with()
    api.set("POST /ops/switch/preview", {"op_id": "x", "status": "succeeded", "result": SW_PREVIEW})
    api.set("POST /ops/switch/confirm", {"op_id": "x", "status": "succeeded",
                                         "result": {"status": "failed", "reason": "10 秒内没有停稳", "active_model": "m1"}})
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-switch", "dlg-switch")
        h.click("sw-target-m2")
        await h.wait_until(lambda: h.has("sw-mode-keep"), "预览出来")
        h.click("sw-mode-keep")
        h.type("sw-password", "pw")
        await h.wait_until(lambda: h.enabled("sw-confirm"), "可确认")
        h.click("sw-confirm")
        await h.wait_until(lambda: "10 秒内没有停稳" in h.text("sw-message"), "失败原因")
        assert "m1" in h.text("sw-message") or "模型 1" in h.text("sw-message")


async def test_cancelling_a_switch_preview_tells_the_kernel():
    api = api_with()
    api.set("POST /ops/switch/preview", {"op_id": "x", "status": "succeeded", "result": SW_PREVIEW})
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-switch", "dlg-switch")
        h.click("sw-target-m2")
        await h.wait_until(lambda: h.has("sw-cancel"), "预览出来")
        h.click("sw-cancel")
        await h.wait_until(lambda: api.posts_to("/ops/switch/cancel"), "发出取消")
        await h.wait_until(lambda: not h.has("dlg-switch"), "弹窗关闭")


# ------------------------------------------------------------------ 席位

async def test_acquire_release_and_force_the_seat():
    api = api_with(ops_state(seat="free"))
    async with running_console(api) as h:
        await h.login()
        await h.wait_until(lambda: h.has("btn-seat-acquire"), "申请席位按钮")
        assert not h.has("btn-seat-release") and not h.has("btn-seat-force")
        h.click("btn-seat-acquire")
        await h.wait_until(lambda: [p for p, _ in api.posts] == ["/seat/acquire"], "发出申请")
        api.set("state", ops_state(seat="mine"))
        h.advance(60)
        await h.wait_until(lambda: h.has("btn-seat-release"), "释放席位按钮")
        h.click("btn-seat-release")
        await h.wait_until(lambda: [p for p, _ in api.posts][-1] == "/seat/release", "发出释放")
        api.set("state", ops_state(seat="other"))
        h.advance(60)
        await h.wait_until(lambda: h.has("btn-seat-force"), "强制接管按钮")
        h.click("btn-seat-force")
        await h.wait_until(lambda: h.has("seat-password"), "口令框")
        assert not h.enabled("seat-force-confirm")
        h.type("seat-password", "pw-admin")
        await h.wait_until(lambda: h.enabled("seat-force-confirm"), "可确认")
        h.click("seat-force-confirm")
        await h.wait_until(lambda: api.posts[-1] == ("/seat/force", {"password": "pw-admin"}), "发出强制接管")


async def test_a_held_seat_is_renewed_on_half_the_lease():
    api = api_with()
    async with running_console(api) as h:
        await h.login()
        start = h.clock.now_ms()
        state = ops_state()
        state["seat"]["lease_until_ms"] = start + 60_000
        api.set("state", state)
        h.advance(1)
        await h.wait_until(lambda: h.console.service.controller_for(h.browser_id()).state["seat"]["lease_until_ms"] is not None, "读到租约")
        await h.wait_until(lambda: True)
        h.advance(40)
        await h.wait_until(lambda: any(p == "/seat/renew" for p, _ in api.posts), "自动续租")


# ------------------------------------------------------------------ 重新恢复

async def test_the_recover_button_shows_only_while_recovering_and_posts_recover():
    api = api_with()
    async with running_console(api) as h:
        await h.login()
        assert not h.has("btn-recover")
        api.set("state", ops_state(model={"suppressions": [{"code": "recovering", "label": "恢复中"}]}))
        h.advance(60)
        await h.wait_until(lambda: h.has("btn-recover"), "出现重新恢复按钮")
        await h.wait_until(lambda: h.enabled("btn-recover"), "可点")
        h.click("btn-recover")
        await h.wait_until(lambda: api.posts_to("/ops/recover"), "发出恢复")
        assert api.posts_to("/ops/recover")[0]["op_id"].startswith("c-")


# ------------------------------------------------------------------ 失联

async def test_when_the_kernel_is_lost_every_operation_and_dialog_button_is_disabled():
    api = api_with()
    async with running_console(api) as h:
        await h.login()
        await h.goto("/positions", "group-strategy")
        await open_dialog(h, "btn-partial-1", "dlg-partial")
        api.fail_all(lost("timeout"))
        h.advance(60)
        await h.wait_until(lambda: h.has("banner-lost"), "失联横幅")
        from nicegui import ui
        await h.wait_until(lambda: not any(b.enabled for b in h.user.find(ui.button).elements if b.text not in ("关闭",)),
                           "除「关闭」外全部禁用")


# ------------------------------------------------------------------ 替身开发工具页

def make_dev(tmp_path, seen):
    import httpx

    from dmr_console.devtools import StandinDev

    token = tmp_path / "manual.token"
    token.write_text("tok-manual\n", encoding="utf-8")

    def handler(request: httpx.Request):
        import json as _json
        seen.append((request.method, request.url.path, _json.loads(request.content) if request.content else None,
                     request.headers.get("x-standin-key")))
        if request.url.path == "/dev/faults":
            return httpx.Response(200, json={"faults": {"partial_fill": {"enabled": True}, "rate_limit": {"enabled": False}}, "behaviors": {}})
        return httpx.Response(200, json={"status": "FILLED"})

    return StandinDev("http://127.0.0.1:18220", token, transport=httpx.MockTransport(handler))


async def test_the_dev_page_is_for_the_standin_only_and_is_loudly_marked(tmp_path):
    seen = []
    async with running_console(api_with(), dev=make_dev(tmp_path, seen)) as h:
        await h.login()
        await h.wait_until(lambda: h.has("nav-dev"), "替身环境里有开发页入口")
        await h.goto("/dev", "dev-banner")
        assert h.text("dev-banner") == "替身环境 · 非真实交易"
        await h.wait_until(lambda: h.has("dev-content"), "工具区")


async def test_outside_the_standin_the_dev_page_offers_no_tools(tmp_path):
    api = api_with(ops_state(env="testnet", standin=False))
    async with running_console(api, dev=make_dev(tmp_path, [])) as h:
        await h.login()
        assert not h.has("nav-dev")
        await h.goto("/dev", "dev-unavailable")
        assert not h.has("dev-content") and not h.has("dev-order-open")


async def test_test_open_goes_to_the_kernel_and_leaves_a_log_line(tmp_path):
    api = api_with()
    api.set("POST /dev/test_open", {"op_id": "x", "status": "succeeded", "result": {"intent_id": "i1", "state": "SUBMITTED"}})
    async with running_console(api, dev=make_dev(tmp_path, [])) as h:
        await h.login()
        await h.goto("/dev", "dev-banner")
        await h.wait_until(lambda: h.has("dev-test-open-LONG") and h.enabled("dev-test-open-LONG"), "造仓按钮")
        h.click("dev-test-open-LONG")
        await h.wait_until(lambda: api.posts_to("/dev/test_open"), "发出造仓")
        body = api.posts_to("/dev/test_open")[0]
        assert {k: v for k, v in body.items() if k != "op_id"} == {"symbol": "BTCUSDT", "side": "LONG", "notional": "100"}
        await h.wait_until(lambda: "测试造仓" in h.text("dev-log"), "页面日志有一行")


async def test_manual_orders_go_straight_to_the_standin_with_the_manual_token(tmp_path):
    seen = []
    async with running_console(api_with(), dev=make_dev(tmp_path, seen)) as h:
        await h.login()
        await h.goto("/dev", "dev-banner")
        await h.wait_until(lambda: h.has("dev-order-open"), "手动下单按钮")
        h.click("dev-dir-SHORT")
        h.click("dev-order-open")
        await h.wait_until(lambda: any(p == "/dev/manual/order" for _, p, _, _ in seen), "开仓请求")
        h.click("dev-order-reduce")
        h.type("dev-m-trigger", "61000")
        h.click("dev-algo-stop")
        await h.wait_until(lambda: any(p == "/dev/manual/algo_order" for _, p, _, _ in seen), "止损请求")
        orders = [(b, k) for _, p, b, k in seen if p == "/dev/manual/order"]
        assert orders[0] == ({"symbol": "BTCUSDT", "side": "SELL", "position_side": "SHORT", "qty": "0.01"}, "tok-manual")
        assert orders[1][0]["side"] == "BUY"
        algo = [b for _, p, b, _ in seen if p == "/dev/manual/algo_order"][0]
        assert algo["type"] == "STOP_MARKET" and algo["side"] == "BUY" and algo["trigger_price"] == "61000" and algo["close_position"] is True
        assert "手动开仓" in h.text("dev-log")


async def test_fault_switches_are_read_toggled_and_reset(tmp_path):
    seen = []
    async with running_console(api_with(), dev=make_dev(tmp_path, seen)) as h:
        await h.login()
        await h.goto("/dev", "dev-banner")
        await h.wait_until(lambda: any(p == "/dev/faults" and m == "GET" for m, p, _, _ in seen), "读取开关")
        await h.wait_until(lambda: h.element("dev-fault-partial_fill").value is True, "按替身现状显示")
        seen.clear()
        h.click("dev-fault-rate_limit")
        await h.wait_until(lambda: any(m == "PUT" for m, _, _, _ in seen), "改开关")
        assert ("PUT", "/dev/faults", {"faults": {"rate_limit": {"enabled": True}}}, "tok-manual") in seen
        h.click("dev-faults-reset")
        await h.wait_until(lambda: any(p == "/dev/faults/reset" for _, p, _, _ in seen), "复位")


async def test_price_override_sets_by_price_by_percent_and_clears(tmp_path):
    seen = []
    async with running_console(api_with(), dev=make_dev(tmp_path, seen)) as h:
        await h.login()
        await h.goto("/dev", "dev-banner")
        await h.wait_until(lambda: h.has("dev-price-set"), "价格覆盖按钮")
        h.type("dev-p-price", "61000")
        h.click("dev-price-set")
        h.type("dev-p-pct", "-3")
        h.click("dev-price-pct")
        h.click("dev-price-clear")
        await h.wait_until(lambda: sum(p == "/dev/price_override" for _, p, _, _ in seen) == 3, "三次价格覆盖")
        bodies = [b for _, p, b, _ in seen if p == "/dev/price_override"]
        assert bodies == [{"symbol": "BTCUSDT", "price": "61000"}, {"symbol": "BTCUSDT", "pct": "-3"}, {"symbol": "BTCUSDT", "clear": True}]


async def test_a_standin_failure_is_shown_in_the_log_not_raised(tmp_path):
    import httpx

    from dmr_console.devtools import StandinDev

    token = tmp_path / "manual.token"
    token.write_text("t", encoding="utf-8")
    dev = StandinDev("http://127.0.0.1:18220", token, transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"code": -1102, "msg": "缺少参数 qty"})))
    async with running_console(api_with(), dev=dev) as h:
        await h.login()
        await h.goto("/dev", "dev-banner")
        await h.wait_until(lambda: h.has("dev-order-open"), "按钮")
        h.click("dev-order-open")
        await h.wait_until(lambda: "缺少参数 qty" in h.text("dev-log"), "失败原因进日志")


# ------------------------------------------------------------------ M5：模型 3 的启动按钮 / 开发页如实显示

def m3_state(*, start_allowed: bool, active: str = "m3"):
    state = ops_state()
    state["model"] = dict(state["model"], active=active)
    state["allowed"]["start"] = {"allowed": start_allowed, "reason": "模型 3 本期只预留，不能启动", "needs_live_read": False, "note": ""}
    return state


async def test_model3_start_stays_clickable_and_shows_the_kernel_refusal():
    api = api_with(m3_state(start_allowed=False))
    api.set("POST /ops/start", KernelError(409, "not_allowed", "模型 3 本期只预留，不能启动"))
    async with running_console(api) as h:
        await h.login()
        await open_dialog(h, "btn-start", "dlg-start")  # 模型 3 即使 allowed=false 也可点
        h.click("start-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/start"), "发出启动请求")
        await h.wait_until(lambda: "模型 3 本期只预留" in h.text("start-message"), "内核拒绝原因醒目显示")
        assert "已启动" not in h.text("start-message")


async def test_other_models_start_is_still_greyed_when_the_kernel_says_no():
    async with running_console(api_with(m3_state(start_allowed=False, active="m2"))) as h:
        await h.login()
        await h.wait_until(lambda: h.has("btn-start") and h.enabled("btn-stop"), "页面就绪")
        assert not h.enabled("btn-start") and "不能启动" in h.text("reason-start")


async def test_model3_start_is_still_greyed_without_the_seat():
    state = m3_state(start_allowed=False)
    state["seat"] = {"holder_session_id": None, "holder_operator": None, "lease_until_ms": None, "mine": False}
    async with running_console(api_with(state)) as h:
        await h.login()
        await h.wait_until(lambda: h.has("btn-start"), "页面就绪")
        assert not h.enabled("btn-start")


async def test_dev_test_open_reports_the_kernel_result_not_a_blanket_accepted(tmp_path):
    api = api_with()
    api.set("POST /dev/test_open", {"op_id": "x", "status": "failed",
                                    "result": {"intent_id": "i1", "state": "REJECTED", "reason": "合约不在清单"}})
    async with running_console(api, dev=make_dev(tmp_path, [])) as h:
        await h.login()
        await h.goto("/dev", "dev-banner")
        await h.wait_until(lambda: h.has("dev-test-open-LONG") and h.enabled("dev-test-open-LONG"), "造仓按钮")
        h.click("dev-test-open-LONG")
        await h.wait_until(lambda: "REJECTED" in h.text("dev-log"), "按内核返回显示意图状态")
        log = h.text("dev-log")
        assert "合约不在清单" in log and "已受理" not in log


async def test_dev_test_open_accepted_shows_the_intent_state(tmp_path):
    api = api_with()
    api.set("POST /dev/test_open", {"op_id": "x", "status": "succeeded", "result": {"intent_id": "i1", "state": "COMPLETED"}})
    async with running_console(api, dev=make_dev(tmp_path, [])) as h:
        await h.login()
        await h.goto("/dev", "dev-banner")
        await h.wait_until(lambda: h.has("dev-test-open-LONG") and h.enabled("dev-test-open-LONG"), "造仓按钮")
        h.click("dev-test-open-LONG")
        await h.wait_until(lambda: "COMPLETED" in h.text("dev-log"), "意图状态")
