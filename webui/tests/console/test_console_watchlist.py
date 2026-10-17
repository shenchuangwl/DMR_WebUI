"""T3.5：名单页、信号注入表单、状态条的名单字段、内核 allowed 表含接管类五个键时控制台照内核说的。"""
from __future__ import annotations

import httpx

from console_kit import FakeKernel, GOOD_TOKEN, START_MS, FakeKernelClient, make_state, make_watchlist
from test_console_client import make_client
from test_console_statusbar import NOW, OK, UTC7, bar
from ui_kit import running_console


# ------------------------------------------------------------------ 客户端

async def test_client_gets_watchlist_without_params():
    kernel = FakeKernel()
    kernel.reply("GET", "/watchlist", make_watchlist())
    data = await make_client(kernel).watchlist(GOOD_TOKEN)
    assert data["members"][0]["symbol"] == "BTCUSDT"
    (request,) = kernel.requests_to("GET", "/watchlist")
    assert not request.url.query and request.headers["authorization"] == f"Bearer {GOOD_TOKEN}"


# ------------------------------------------------------------------ 视图（纯函数）

def test_watchlist_rows_show_held_and_avoid_reason():
    from dmr_console.views import watchlist_rows

    rows = watchlist_rows(make_watchlist(), tz=UTC7)
    assert [r["symbol"] for r in rows] == ["BTCUSDT", "1000PEPEUSDT"]
    btc, pepe = rows
    assert btc["side"] == "多 LONG" and btc["score"] == "8.5" and btc["held"] == "已持仓" and btc["avoid"] == "无"
    assert btc["first_selected"] == "2025-10-09 13:53:20" and btc["row_class"] == ""
    assert pepe["side"] == "空 SHORT" and pepe["held"] == "否" and pepe["avoid"] == "外部仓：该合约已有手动持仓"
    assert pepe["row_class"] == "dmr-row-warn"


def test_watchlist_rows_survive_bad_data():
    from dmr_console.views import rejected_rows, watchlist_rows

    assert watchlist_rows(None, tz=UTC7) == [] and rejected_rows(None) == []
    rows = watchlist_rows({"members": [None, 5, {"symbol": "X", "direction": 3, "score": None, "first_selected_ms": None}]}, tz=UTC7)
    assert len(rows) == 1 and rows[0]["symbol"] == "X" and rows[0]["first_selected"] == "—"
    assert rejected_rows({"rejected": [{"code": "a", "reason": "b"}, "junk"]}) == [{"code": "a", "reason": "b"}]


def test_watchlist_summary():
    from dmr_console.views import watchlist_summary

    ok = watchlist_summary(make_watchlist(), now_ms=NOW, tz=UTC7)
    assert ok["status"] == "有效" and ok["tone"] == "ok" and ok["count"] == "成员 2 个"
    assert "1 小时" in ok["valid_until"] and ok["fetched"] == "3 秒前" and ok["date"] == "2025-10-09"
    bad = watchlist_summary(make_watchlist(valid=False, reason="HTTP 503", members=[]), now_ms=NOW, tz=UTC7)
    assert bad["status"] == "无效：HTTP 503" and bad["tone"] == "warn" and bad["count"] == "成员 0 个"
    empty_ok = watchlist_summary(make_watchlist(members=[]), now_ms=NOW, tz=UTC7)
    assert empty_ok["status"] == "有效" and empty_ok["count"] == "成员 0 个"
    none = watchlist_summary(None, now_ms=NOW, tz=UTC7)
    assert none["status"] == "尚无数据"


def test_inject_payload_is_exactly_the_plan_message():
    from dmr_console import operations as O

    assert O.inject_signal_payload("op1", "BTCUSDT", "up", "bullish") == {
        "op_id": "op1", "symbol": "BTCUSDT", "short_cross": "up", "long_trend": "bullish"}
    assert set(O.SHORT_CROSS_LABELS) == {"up", "down"}
    assert set(O.LONG_TREND_LABELS) == {"bullish", "bearish", "neutral", "missing"}


# ------------------------------------------------------------------ 状态条

def test_list_chip_shows_validity_count_and_expiry_clock():
    chip = bar(list={"known": True, "valid": True, "member_count": 0, "valid_until_ms": NOW + 3_600_000, "reason": ""}).chip("list")
    assert chip.text.startswith("有效（成员 0 个") and chip.tone == "ok"
    assert chip.detail.startswith("有效期至 2025-10-09 ")


# ------------------------------------------------------------------ allowed 里的接管类五个键

def test_kernel_allowed_with_five_takeover_keys_is_followed():
    from dmr_console.link import LinkStatus
    from dmr_console.state import position_action_states

    state = make_state(model={"active": "m1"}, seat={"mine": True})
    for key in ("takeover", "ignore", "restore", "cancel_takeover", "hand_stop_to_system"):
        state["allowed"][key] = {"allowed": key != "takeover", "reason": "接管只在模型 2 生效时可用（内核）" if key == "takeover" else "",
                                 "needs_live_read": False, "note": ""}
    states = position_action_states(state, LinkStatus(lost=False, last_ok_ms=NOW), operations_enabled=True)
    assert not states["takeover"].enabled and states["takeover"].reason == "接管只在模型 2 生效时可用（内核）"
    assert all(states[k].enabled for k in ("ignore", "restore", "cancel_takeover", "hand_stop_to_system"))
    # 内核说可以就可以，哪怕控制台自己的兜底规则会因为不是模型 2 而拒绝
    state["allowed"]["takeover"] = {"allowed": True, "reason": "", "needs_live_read": False, "note": ""}
    assert position_action_states(state, LinkStatus(lost=False, last_ok_ms=NOW), operations_enabled=True)["takeover"].enabled


# ------------------------------------------------------------------ 页面

async def test_watchlist_page_shows_members_validity_and_rejected():
    api = FakeKernelClient()
    async with running_console(api) as h:
        await h.login()
        await h.wait_until(lambda: h.has("nav-watchlist"), "导航里有名单页")
        await h.goto("/watchlist", "watchlist-status")
        await h.wait_until(lambda: h.text("watchlist-status") == "有效", "名单到达")
        assert h.text("watchlist-count") == "成员 2 个" and h.text("watchlist-date") == "业务日：2025-10-09"
        assert "1 小时" in h.text("watchlist-valid-until")
        rows = h.rows("watchlist-grid")
        assert [r["symbol"] for r in rows] == ["BTCUSDT", "1000PEPEUSDT"]
        assert rows[1]["avoid"] == "外部仓：该合约已有手动持仓" and rows[0]["held"] == "已持仓"
        assert h.rows("rejected-grid") == [{"code": "某某币", "reason": "交易所里查不到这个合约"}]
        assert len(api.calls_of("watchlist")) >= 1


async def test_watchlist_page_invalid_and_empty():
    api = FakeKernelClient()
    api.set("watchlist", make_watchlist(valid=False, reason="HTTP 503", members=[], rejected=[], valid_until_ms=None))
    async with running_console(api) as h:
        await h.login()
        await h.goto("/watchlist", "watchlist-status")
        await h.wait_until(lambda: h.text("watchlist-status") == "无效：HTTP 503", "无效原因显示")
        assert h.has("watchlist-empty") and not h.has("watchlist-grid")
        assert h.has("rejected-empty")


async def test_watchlist_page_keeps_working_when_the_kernel_has_no_such_endpoint():
    from dmr_console.client import KernelError

    api = FakeKernelClient()
    api.set("watchlist", KernelError(404, "not_found", "没有这个接口"))
    async with running_console(api) as h:
        await h.login()
        await h.goto("/watchlist", "watchlist-status")
        await h.wait_until(lambda: "没有这个接口" in h.text("watchlist-error"), "错误显示在页面上")
        assert h.has("chip-gate")  # 外壳照常


async def test_watchlist_page_refreshes_when_the_kernel_changes():
    api = FakeKernelClient()
    async with running_console(api) as h:
        await h.login()
        await h.goto("/watchlist", "watchlist-status")
        await h.wait_until(lambda: len(h.rows("watchlist-grid")) == 2, "两个成员")
        api.set("watchlist", make_watchlist(members=[make_watchlist()["members"][0]]))
        h.advance(10)
        await h.wait_until(lambda: len(h.rows("watchlist-grid")) == 1, "名单变化后重画")


async def test_other_pages_do_not_fetch_the_watchlist():
    api = FakeKernelClient()
    async with running_console(api) as h:
        await h.login()
        await h.goto("/positions", "group-strategy")
        h.advance(10)
        await h.wait_until(lambda: len(api.calls_of("positions")) >= 2, "轮询发生")
        assert api.calls_of("watchlist") == []


# ------------------------------------------------------------------ 信号注入表单

def make_dev(tmp_path):
    from dmr_console.devtools import StandinDev

    token = tmp_path / "standin" / "manual.token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("tok", encoding="utf-8")
    return StandinDev("http://127.0.0.1:18220", token,
                      transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"faults": {}})))


def seat_state():
    return make_state(seat={"holder_session_id": "sess-1", "holder_operator": "admin", "lease_until_ms": START_MS + 60_000, "mine": True})


async def test_inject_signal_form_posts_the_plan_message(tmp_path):
    api = FakeKernelClient()
    api.set("state", seat_state())
    api.set("POST /dev/inject_signal", {"op_id": "x", "status": "succeeded", "result": {"intent_id": "i9", "state": "SUBMITTED"}})
    async with running_console(api, dev=make_dev(tmp_path)) as h:
        await h.login()
        await h.goto("/dev", "dev-banner")
        await h.wait_until(lambda: h.has("dev-inject-symbol") and h.enabled("dev-inject-submit"), "注入表单")
        h.element("dev-inject-symbol").set_value("ETHUSDT")
        h.click("dev-inject-cross-down")
        h.click("dev-inject-trend-bearish")
        h.click("dev-inject-submit")
        await h.wait_until(lambda: api.posts_to("/dev/inject_signal"), "发出注入")
        (body,) = api.posts_to("/dev/inject_signal")
        assert body["symbol"] == "ETHUSDT" and body["short_cross"] == "down" and body["long_trend"] == "bearish"
        assert set(body) == {"op_id", "symbol", "short_cross", "long_trend"} and body["op_id"]
        await h.wait_until(lambda: "信号注入" in h.text("dev-log") and "ETHUSDT" in h.text("dev-log"), "页面留一行")


async def test_inject_signal_defaults_and_rejection_text(tmp_path):
    from dmr_console.client import KernelError

    api = FakeKernelClient()
    api.set("state", seat_state())
    api.set("POST /dev/inject_signal", KernelError(409, "not_allowed", "模型 1 未生效"))
    async with running_console(api, dev=make_dev(tmp_path)) as h:
        await h.login()
        await h.goto("/dev", "dev-banner")
        await h.wait_until(lambda: h.has("dev-inject-submit") and h.enabled("dev-inject-submit"), "注入按钮")
        h.click("dev-inject-submit")
        await h.wait_until(lambda: api.posts_to("/dev/inject_signal"), "发出注入")
        (body,) = api.posts_to("/dev/inject_signal")
        assert body["symbol"] == "BTCUSDT" and body["short_cross"] == "up" and body["long_trend"] == "bullish"
        await h.wait_until(lambda: "模型 1 未生效" in h.text("dev-log"), "拒绝原因显示")


async def test_inject_button_is_disabled_without_the_seat(tmp_path):
    api = FakeKernelClient()
    async with running_console(api, dev=make_dev(tmp_path)) as h:
        await h.login()
        await h.goto("/dev", "dev-banner")
        await h.wait_until(lambda: h.has("dev-inject-submit"), "注入表单")
        assert not h.enabled("dev-inject-submit")
