"""持仓页与操作日志页的冒烟测试：按归属分组、待确认与外部单列、无保护标红、翻页、坏数据不崩。"""
from __future__ import annotations

from console_kit import FakeKernelClient, lost, make_alert, make_oplog_item, make_position
from ui_kit import running_console


def seeded_api() -> FakeKernelClient:
    api = FakeKernelClient()
    api.set("positions", [
        make_position(id=1, symbol="BTCUSDT", ownership="strategy"),
        make_position(id=2, symbol="ETHUSDT", ownership="takeover", exit_ruleset="takeover"),
        make_position(id=3, symbol="SOLUSDT", ownership="strategy", is_mixed=True),
        make_position(id=4, symbol="ADAUSDT", ownership="strategy", side="SHORT", protection_state="none", stop_price=None,
                      has_stop_on_exchange=False),
    ])
    api.set("positions_external", [
        make_position(id=10, symbol="XRPUSDT", status="pending", ownership="none", pending_reason="out_of_scope",
                      has_stop_on_exchange=False, origin_note="手动开的"),
        make_position(id=11, symbol="DOGEUSDT", status="ignored", ownership="none", has_stop_on_exchange=True),
    ])
    return api


async def open_positions(h) -> None:
    await h.login()
    await h.goto("/positions", "group-strategy")
    await h.wait_until(lambda: h.text("group-title-strategy").endswith("（2）"), "持仓数据到达")


# ------------------------------------------------------------------ 持仓

async def test_positions_are_grouped_by_ownership_with_pending_and_external_listed_apart():
    async with running_console(seeded_api()) as h:
        await open_positions(h)
        assert h.text("group-title-strategy") == "策略仓（2）"
        assert [r["symbol"] for r in h.rows("grid-strategy")] == ["ADAUSDT", "BTCUSDT"]
        assert h.text("group-title-mixed") == "混合仓（1）" and [r["symbol"] for r in h.rows("grid-mixed")] == ["SOLUSDT"]
        assert h.text("group-title-takeover") == "接管仓（1）" and [r["symbol"] for r in h.rows("grid-takeover")] == ["ETHUSDT"]
        assert h.text("group-title-pending") == "待确认仓（1）"
        assert h.text("group-title-external") == "外部仓（已忽略）（1）"
        pending = h.rows("grid-pending")[0]
        assert pending["symbol"] == "XRPUSDT" and pending["pending_reason"] == "不在接管清单"
        assert pending["exchange_stop"] == "无" and pending["note"] == "手动开的"
        external = h.rows("grid-external")[0]
        assert external["symbol"] == "DOGEUSDT" and external["exchange_stop"] == "有"
        # 外部与待确认不混进登记仓的表
        registered_symbols = {r["symbol"] for key in ("strategy", "mixed", "takeover") for r in h.rows(f"grid-{key}")}
        assert "XRPUSDT" not in registered_symbols and "DOGEUSDT" not in registered_symbols


async def test_groups_without_positions_show_an_explicit_empty_line_after_the_data_arrived():
    async with running_console(seeded_api()) as h:
        await open_positions(h)
        assert not h.has("grid-ai")  # 空表不显示
        assert h.text("empty-ai") == "（无）" and h.text("group-title-ai") == "AI 仓（0）"


async def test_a_row_shows_what_the_report_asks_for():
    async with running_console(seeded_api()) as h:
        await open_positions(h)
        btc = next(r for r in h.rows("grid-strategy") if r["symbol"] == "BTCUSDT")
        assert btc["side"] == "多 LONG" and btc["qty"] == "0.010" and btc["avg_price"] == "60000" and btc["pnl"] == "+1.00"
        assert btc["ownership"] == "策略" and btc["protection"] == "系统保护" and btc["stop"] == "58000"
        assert btc["liquidation"] == "30000" and btc["consistent"] == "一致" and btc["unconfirmed"] == "无"
        assert btc["closing"] == "否" and btc["opened"] == "2025-10-09 14:53:20"  # 本机时区（+07）


async def test_an_unprotected_registered_position_is_highlighted_red_and_listed_as_such():
    async with running_console(seeded_api()) as h:
        await open_positions(h)
        ada = next(r for r in h.rows("grid-strategy") if r["symbol"] == "ADAUSDT")
        assert ada["protection"] == "无保护" and ada["row_class"] == "dmr-row-danger"
        btc = next(r for r in h.rows("grid-strategy") if r["symbol"] == "BTCUSDT")
        assert btc["row_class"] == ""


async def test_the_positions_page_follows_the_kernel_when_positions_change():
    api = seeded_api()
    async with running_console(api) as h:
        await open_positions(h)
        api.set("positions", [make_position(id=1, symbol="BTCUSDT")])
        h.advance(60)
        await h.wait_until(lambda: h.text("group-title-strategy") == "策略仓（1）", "持仓减少后表格跟着变")
        assert h.text("group-title-mixed") == "混合仓（0）"


async def test_garbage_from_the_kernel_does_not_break_the_positions_page():
    api = FakeKernelClient()
    api.set("positions", [None, 5, "x", {}, {"id": "weird", "symbol": None, "qty": {"a": 1}}, make_position(id=1)])
    api.set("positions_external", [[], None])
    async with running_console(api) as h:
        await h.login()
        await h.goto("/positions", "group-strategy")
        await h.wait_until(lambda: h.text("group-title-strategy").endswith("（1）"), "页面仍能画出能画的行")


async def test_positions_stay_visible_but_stale_while_the_kernel_is_lost():
    async with running_console(seeded_api()) as h:
        await open_positions(h)
        h.api.fail_all(lost("timeout"))
        h.advance(60)
        await h.wait_until(lambda: h.has("banner-lost"), "失联横幅")
        assert [r["symbol"] for r in h.rows("grid-strategy")] == ["ADAUSDT", "BTCUSDT"]


# ------------------------------------------------------------------ 操作日志

def paged_api() -> FakeKernelClient:
    api = FakeKernelClient()
    pages = {None: [make_oplog_item(id=i, action=f"op-{i}") for i in (10, 9, 8)],
             8: [make_oplog_item(id=i, action=f"op-{i}") for i in (7, 6)]}
    api.set("oplog", lambda token, limit=3, before_id=None: pages.get(before_id, []))
    api.set("alerts", [make_alert(id=2, level="critical", message="风控触发"), make_alert(id=1, message="数据流中断")])
    return api


async def open_oplog(h) -> None:
    await h.login()
    await h.goto("/oplog", "btn-load-more")
    await h.wait_until(lambda: h.text("oplog-count").startswith("已载入"), "操作日志数据到达")


async def test_the_operation_log_is_listed_newest_first_with_operator_and_result():
    async with running_console(paged_api(), oplog_page=3) as h:
        await open_oplog(h)
        assert [r["id"] for r in h.rows("oplog-grid")] == [10, 9, 8]
        row = h.rows("oplog-grid")[0]
        assert (row["operator"], row["action"], row["result"], row["request_id"]) == ("admin", "op-10", "ok", "req-10")
        assert row["time"].startswith("2025-10-09 15:5")  # 本机时区（+07）
        assert h.text("oplog-count") == "已载入 3 条"


async def test_load_more_fetches_the_next_older_page_and_then_disables_itself_at_the_end():
    api = paged_api()
    async with running_console(api, oplog_page=3) as h:
        await open_oplog(h)
        await h.wait_until(lambda: h.enabled("btn-load-more"), "加载更多可点")
        h.user.find(marker="btn-load-more").click()
        await h.wait_until(lambda: [r["id"] for r in h.rows("oplog-grid")] == [10, 9, 8, 7, 6], "翻到更旧的一页")
        assert {"limit": 3, "before_id": 8} in [kw for (name, _, kw) in api.calls if name == "oplog"]
        await h.wait_until(lambda: not h.enabled("btn-load-more"), "到头后禁用")
        assert "已到最早一条" in h.text("oplog-count")


async def test_load_more_is_disabled_while_the_kernel_is_lost():
    async with running_console(paged_api(), oplog_page=3) as h:
        await open_oplog(h)
        await h.wait_until(lambda: h.enabled("btn-load-more"), "加载更多可点")
        h.api.fail_all(lost("connect"))
        h.advance(60)
        await h.wait_until(lambda: h.has("banner-lost"), "失联横幅")
        await h.wait_until(lambda: not h.enabled("btn-load-more") and not h.enabled("btn-refresh"), "失联时按钮禁用")


async def test_alerts_are_listed_below_the_log_with_their_level():
    async with running_console(paged_api(), oplog_page=3) as h:
        await open_oplog(h)
        await h.wait_until(lambda: len(h.rows("alerts-grid")) == 2, "告警表")
        assert [(r["level"], r["message"]) for r in h.rows("alerts-grid")] == [("严重", "风控触发"), ("警告", "数据流中断")]


async def test_an_empty_log_says_so_instead_of_staying_on_loading():
    async with running_console(FakeKernelClient()) as h:
        await open_oplog(h)
        await h.wait_until(lambda: h.text("oplog-empty") == "（暂无操作记录）", "空日志的提示")
        await h.wait_until(lambda: h.text("alerts-empty") == "（暂无告警）", "空告警的提示")
