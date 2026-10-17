"""持仓页的接管类操作与设置页（NiceGUI 用户模拟 + 假内核客户端）。"""
from __future__ import annotations

from console_kit import make_position
from test_console_pages_ops import ops_state
from ui_kit import running_console

from console_kit import FakeKernelClient
from dmr_console.client import KernelError, KernelUnreachable


def pending(pid=21, **over):
    base = dict(id=pid, symbol="DOGEUSDT", status="pending", ownership="none", pending_reason="out_of_scope",
                protection_state="none", version=4)
    return make_position(**{**base, **over})


def taken(pid=31, **over):
    base = dict(id=pid, status="managed", ownership="takeover", exit_ruleset="takeover", version=6)
    return make_position(**{**base, **over})


def api_for(*, external=(), positions=(), state=None):
    api = FakeKernelClient()
    api.set("state", state or ops_state(model={"active": "m2"}))
    api.set("positions", list(positions))
    api.set("positions_external", list(external))
    return api


async def open_positions(h):
    await h.login()
    await h.goto("/positions", "group-strategy")


async def open_dialog(h, button, dialog):
    await h.wait_until(lambda: h.has(button) and h.enabled(button), f"{button} 可点")
    h.click(button)
    await h.wait_until(lambda: h.has(dialog), f"弹窗 {dialog}")


# ------------------------------------------------------------------ 待确认仓

async def test_pending_position_shows_takeover_and_ignore_but_not_restore():
    async with running_console(api_for(external=[pending()])) as h:
        await open_positions(h)
        await h.wait_until(lambda: h.has("btn-takeover-21"), "接管按钮")
        assert h.element("btn-takeover-21").text == "接管" and h.element("btn-ignore-21").text == "忽略"
        assert not h.has("btn-restore-21")
        assert h.enabled("btn-takeover-21") and h.enabled("btn-ignore-21")


async def test_origin_to_verify_adds_the_restore_button():
    async with running_console(api_for(external=[pending(pending_reason="origin_to_verify")])) as h:
        await open_positions(h)
        await h.wait_until(lambda: h.has("btn-restore-21"), "恢复托管按钮")
        assert h.element("btn-restore-21").text == "恢复托管"


async def test_takeover_is_greyed_with_the_reason_when_model_2_is_not_active_but_ignore_still_works():
    api = api_for(external=[pending()], state=ops_state(model={"active": "m1"}))
    async with running_console(api) as h:
        await open_positions(h)
        await h.wait_until(lambda: h.has("btn-takeover-21"), "按钮")
        assert not h.enabled("btn-takeover-21") and h.enabled("btn-ignore-21")
        assert "模型 2" in h.text("takeover-reason-21")


async def test_without_the_seat_the_buttons_are_greyed():
    async with running_console(api_for(external=[pending()], state=ops_state(seat="free", model={"active": "m2"}))) as h:
        await open_positions(h)
        await h.wait_until(lambda: h.has("btn-takeover-21"), "按钮")
        assert not h.enabled("btn-takeover-21") and not h.enabled("btn-ignore-21")


async def test_takeover_sends_integer_ids_and_shows_the_result():
    api = api_for(external=[pending()])
    api.set("POST /ops/takeover", {"op_id": "x", "status": "succeeded", "result": {
        "position_id": 21, "status": "managed", "ownership": "takeover", "protection_state": "system"}})
    async with running_console(api) as h:
        await open_positions(h)
        await open_dialog(h, "btn-takeover-21", "dlg-takeover")
        h.click("takeover-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/takeover"), "发出请求")
        body = api.posts_to("/ops/takeover")[0]
        assert body["position_id"] == 21 and body["position_version"] == 4 and body["op_id"].startswith("c-")
        assert set(body) == {"op_id", "position_id", "position_version"}
        await h.wait_until(lambda: "托管中" in h.text("takeover-message") and "系统保护" in h.text("takeover-message"), "结果文字")


async def test_a_refusal_shows_the_kernels_chinese_reason():
    api = api_for(external=[pending()])
    api.set("POST /ops/takeover", KernelError(409, "not_allowed", "模型 2 未运行"))
    async with running_console(api) as h:
        await open_positions(h)
        await open_dialog(h, "btn-takeover-21", "dlg-takeover")
        h.click("takeover-confirm")
        await h.wait_until(lambda: "模型 2 未运行" in h.text("takeover-message"), "拒绝原因")


async def test_stale_version_tells_the_operator_to_retry():
    api = api_for(external=[pending()])
    api.set("POST /ops/ignore", KernelError(409, "stale_version", "持仓已变化"))
    async with running_console(api) as h:
        await open_positions(h)
        await open_dialog(h, "btn-ignore-21", "dlg-ignore")
        h.click("ignore-confirm")
        await h.wait_until(lambda: "持仓已变化" in h.text("ignore-message"), "过期提示")


async def test_ignore_and_restore_use_their_own_paths():
    api = api_for(external=[pending(pending_reason="origin_to_verify")])
    async with running_console(api) as h:
        await open_positions(h)
        await open_dialog(h, "btn-ignore-21", "dlg-ignore")
        h.click("ignore-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/ignore"), "忽略")
        await open_dialog(h, "btn-restore-21", "dlg-restore")
        h.click("restore-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/restore"), "恢复")
        assert api.posts_to("/ops/restore")[0]["position_id"] == 21


# ------------------------------------------------------------------ 接管仓

async def test_takeover_position_has_cancel_takeover_and_hand_stop_only_when_externally_protected():
    api = api_for(positions=[taken(31), taken(32, protection_state="external", symbol="ETHUSDT"),
                             make_position(id=33, ownership="strategy")])
    async with running_console(api) as h:
        await open_positions(h)
        await h.wait_until(lambda: h.has("btn-canceltakeover-31"), "取消托管")
        assert h.has("btn-canceltakeover-32") and not h.has("btn-canceltakeover-33")
        assert h.has("btn-handstop-32") and not h.has("btn-handstop-31")
        assert h.element("btn-handstop-32").text == "交给系统管理止损"


async def test_cancel_takeover_defaults_to_handover_and_can_choose_cancel_stop():
    api = api_for(positions=[taken(31)])
    async with running_console(api) as h:
        await open_positions(h)
        await open_dialog(h, "btn-canceltakeover-31", "dlg-cancel-takeover")
        h.click("cancel-takeover-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/cancel_takeover"), "取消托管")
        first = api.posts_to("/ops/cancel_takeover")[0]
        assert first["mode"] == "handover" and first["position_id"] == 31 and first["position_version"] == 6
        await h.wait_until(lambda: h.has("cancel-takeover-confirm") and h.enabled("cancel-takeover-confirm"), "再点")
        h.element("cancel-takeover-mode").set_value("cancel_stop")
        h.click("cancel-takeover-confirm")
        await h.wait_until(lambda: len(api.posts_to("/ops/cancel_takeover")) == 2, "第二次")
        assert api.posts_to("/ops/cancel_takeover")[1]["mode"] == "cancel_stop"


async def test_hand_stop_to_system_posts_the_standard_body():
    api = api_for(positions=[taken(32, protection_state="external")])
    async with running_console(api) as h:
        await open_positions(h)
        await open_dialog(h, "btn-handstop-32", "dlg-hand-stop")
        h.click("hand-stop-confirm")
        await h.wait_until(lambda: api.posts_to("/ops/hand_stop_to_system"), "发出请求")
        assert api.posts_to("/ops/hand_stop_to_system")[0] == {
            "op_id": api.posts_to("/ops/hand_stop_to_system")[0]["op_id"], "position_id": 32, "position_version": 6}


async def test_lost_kernel_disables_the_takeover_buttons():
    api = api_for(external=[pending()])
    async with running_console(api) as h:
        await open_positions(h)
        await h.wait_until(lambda: h.enabled("btn-ignore-21"), "可点")
        api.fail_all(KernelUnreachable("无法连接内核"))
        h.advance(60)
        await h.wait_until(lambda: not h.enabled("btn-ignore-21"), "失联后置灰")


# ------------------------------------------------------------------ 设置页

def cfg(key, group, value, **over):
    base = {"key": key, "group": group, "label": f"标签-{key}", "value": value, "default": value, "min": None, "max": None,
            "choices": [], "unit": "", "apply": "立即生效", "affects_existing": False, "required_for_live": False,
            "requires_deploy_switch": False, "note": ""}
    base.update(over)
    return base


CONFIG = [
    cfg("takeover_symbols", "接管", [], apply="只影响之后新出现的仓"),
    cfg("takeover_min_notional", "接管", "5", min="0", max="1000000", unit="USDT"),
    cfg("takeover_signal_exit", "接管", False),
    cfg("margin_mode", "资金", "cross", choices=["cross", "isolated"], affects_existing=True),
    cfg("leverage", "资金", 5, min=1, max=20),
    cfg("m3_form", "模型3", "strategy_exec_ai_veto", choices=["strategy_exec_ai_veto", "ai_autonomous"], requires_deploy_switch=True),
]


def settings_api(seat="mine"):
    api = FakeKernelClient()
    api.set("state", ops_state(seat=seat, model={"active": "m2"}))
    api.set("config", CONFIG)
    return api


async def open_settings(h):
    await h.login()
    await h.goto("/settings", "settings-title")
    await h.wait_until(lambda: h.has("cfg-input-takeover_symbols"), "配置项出现")


async def test_settings_lists_every_item_by_group_with_default_range_unit_and_apply_mode():
    async with running_console(settings_api()) as h:
        await open_settings(h)
        for item in CONFIG:
            assert h.has(f"cfg-row-{item['key']}")
        assert "接管" in h.text("cfg-group-title-0") and "资金" in h.text("cfg-group-title-1")
        meta = h.text("cfg-meta-takeover_min_notional")
        assert "默认 5" in meta and "0 – 1000000" in meta and "USDT" in meta and "立即生效" in meta
        assert "影响已有持仓" in h.text("cfg-meta-margin_mode")
        assert "只影响之后新出现的仓" in h.text("cfg-meta-takeover_symbols")


async def test_settings_nav_link_exists():
    async with running_console(settings_api()) as h:
        await h.login()
        assert h.has("nav-settings")


async def test_writing_the_takeover_list_sends_a_symbol_list_and_shows_what_applies():
    api = settings_api()
    api.set("PUT /config/takeover_symbols", {"key": "takeover_symbols", "old": [], "new": ["BTCUSDT", "ETHUSDT"],
                                              "applies": "只影响之后新出现的仓", "affects_existing": False})
    async with running_console(api) as h:
        await open_settings(h)
        await h.wait_until(lambda: h.enabled("btn-save-takeover_symbols"), "可保存")
        h.element("cfg-input-takeover_symbols").set_value("btcusdt, ETHUSDT")
        h.click("btn-save-takeover_symbols")
        await h.wait_until(lambda: api.puts_to("takeover_symbols"), "发出请求")
        body = api.puts_to("takeover_symbols")[0]
        assert body["value"] == ["BTCUSDT", "ETHUSDT"] and body["op_id"].startswith("c-")
        await h.wait_until(lambda: "已保存" in h.text("cfg-msg-takeover_symbols"), "保存提示")
        assert "只影响之后新出现的仓" in h.text("cfg-msg-takeover_symbols")
        assert h.text("cfg-current-takeover_symbols") == "BTCUSDT, ETHUSDT"


async def test_bool_choice_and_int_items_send_typed_values():
    api = settings_api()
    async with running_console(api) as h:
        await open_settings(h)
        await h.wait_until(lambda: h.enabled("btn-save-leverage"), "可保存")
        h.element("cfg-input-takeover_signal_exit").set_value(True)
        h.click("btn-save-takeover_signal_exit")
        h.element("cfg-input-margin_mode").set_value("isolated")
        h.click("btn-save-margin_mode")
        h.element("cfg-input-leverage").set_value("8")
        h.click("btn-save-leverage")
        await h.wait_until(lambda: api.puts_to("leverage") and api.puts_to("margin_mode") and api.puts_to("takeover_signal_exit"), "三个请求")
        assert api.puts_to("takeover_signal_exit")[0]["value"] is True
        assert api.puts_to("margin_mode")[0]["value"] == "isolated"
        assert api.puts_to("leverage")[0]["value"] == 8 and isinstance(api.puts_to("leverage")[0]["value"], int)


async def test_a_malformed_value_is_stopped_before_the_kernel_and_a_kernel_refusal_is_shown_verbatim():
    api = settings_api()
    api.set("PUT /config/leverage", KernelError(422, "invalid", "杠杆 不能大于 20x"))
    async with running_console(api) as h:
        await open_settings(h)
        await h.wait_until(lambda: h.enabled("btn-save-leverage"), "可保存")
        h.element("cfg-input-leverage").set_value("abc")
        h.click("btn-save-leverage")
        await h.wait_until(lambda: "整数" in h.text("cfg-msg-leverage"), "形状错误")
        assert not api.puts_to("leverage")
        h.element("cfg-input-leverage").set_value("99")
        h.click("btn-save-leverage")
        await h.wait_until(lambda: "杠杆 不能大于 20x" in h.text("cfg-msg-leverage"), "内核原因")


async def test_model_3_items_are_read_only_with_the_deploy_switch_note():
    async with running_console(settings_api()) as h:
        await open_settings(h)
        assert not h.has("btn-save-m3_form")
        assert "部署级开关" in h.text("cfg-readonly-m3_form")


async def test_without_the_seat_saving_is_greyed_with_the_seat_reason():
    async with running_console(settings_api(seat="free")) as h:
        await open_settings(h)
        await h.wait_until(lambda: not h.enabled("btn-save-leverage"), "置灰")
        assert "席位" in h.text("settings-reason")


async def test_a_failed_config_load_shows_the_reason_and_no_rows():
    api = settings_api()
    api.set("config", KernelUnreachable("无法连接内核"))
    async with running_console(api) as h:
        await h.login()
        await h.goto("/settings", "settings-title")
        await h.wait_until(lambda: "无法连接内核" in h.text("settings-error"), "错误提示")
        assert not h.has("cfg-row-leverage")
