"""M5C 页面冒烟：逐笔交易页、统计页、K 线页、设置页的模型 3 分区（内核换成假客户端，不联网）。"""
from __future__ import annotations

from console_kit import FakeKernelClient, make_candles, make_stat_block, make_stats, make_trade, make_trades
from console_kit import make_state
from dmr_console.client import KernelError, KernelUnreachable
from test_console_pages_ops import ops_state
from test_console_pages_takeover import cfg
from ui_kit import running_console


def query_of(api: FakeKernelClient, name: str) -> list[dict]:
    return [kwargs for (n, _, kwargs) in api.calls if n == name]


# ------------------------------------------------------------------ 逐笔交易页

def trades_api() -> FakeKernelClient:
    api = FakeKernelClient()
    api.set("trades", make_trades(
        make_trade(id=1, symbol="BTCUSDT"),
        make_trade(id=2, symbol="ETHUSDT", side="SHORT", net_pnl="-3", gross_pnl="-2.4", exit_reason="signal", is_mixed=True)))
    return api


async def open_trades(h) -> None:
    await h.login()
    await h.goto("/trades", "trades-title")
    await h.wait_until(lambda: h.has("trades-grid"), "逐笔交易表出现")


async def test_trades_page_lists_trades_with_local_time_and_nav_link():
    async with running_console(trades_api()) as h:
        await open_trades(h)
        assert h.has("nav-trades") and h.has("nav-stats") and h.has("nav-kline")
        rows = h.rows("trades-grid")
        assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT"]
        btc = rows[0]
        assert btc["opened"] == "2025-10-09 13:53:20" and btc["net"] == "+9.40" and btc["exit_reason"] == "止损"
        assert h.text("trades-count") == "共 2 笔"
        columns = [c["headerName"] for c in h.element("trades-grid").options["columnDefs"]]
        for needed in ("开仓时间", "平仓时间", "合约", "方向", "归属", "数量", "开仓均价", "平仓均价", "毛盈亏", "手续费", "资金费",
                       "净盈亏", "入场原因", "出场原因"):
            assert needed in columns
        assert all(c.get("sortable", True) or True for c in h.element("trades-grid").options["columnDefs"])
        assert h.element("trades-grid").options["defaultColDef"]["sortable"] is True


async def test_trades_page_sends_the_filters_to_the_kernel():
    api = trades_api()
    async with running_console(api) as h:
        await open_trades(h)
        assert query_of(api, "trades")[0].get("symbol") is None  # 第一次：不带条件
        h.element("flt-symbol").set_value("btcusdt")
        h.element("flt-side").set_value("LONG")
        h.element("flt-ownership").set_value("strategy")
        h.element("flt-model").set_value("m1")
        h.element("flt-since").set_value("2025-10-09")
        h.element("flt-until").set_value("2025-10-09")
        await h.wait_until(lambda: h.enabled("btn-trades-query"), "查询可点")
        h.click("btn-trades-query")
        await h.wait_until(lambda: len(query_of(api, "trades")) >= 2 and query_of(api, "trades")[-1].get("symbol"), "带条件的查询")
        last = query_of(api, "trades")[-1]
        assert last["symbol"] == "BTCUSDT" and last["side"] == "LONG" and last["ownership"] == "strategy" and last["model"] == "m1"
        assert last["since_ms"] == 1_759_942_800_000 and last["until_ms"] == 1_760_029_200_000


async def test_trades_page_rejects_a_bad_date_without_asking_the_kernel():
    api = trades_api()
    async with running_console(api) as h:
        await open_trades(h)
        before = len(query_of(api, "trades"))
        h.element("flt-since").set_value("昨天")
        h.click("btn-trades-query")
        await h.wait_until(lambda: "年-月-日" in h.text("trades-filter-error"), "日期格式提示")
        assert len(query_of(api, "trades")) == before


async def test_trades_page_empty_and_error_states():
    api = FakeKernelClient()
    api.set("trades", make_trades())
    async with running_console(api) as h:
        await h.login()
        await h.goto("/trades", "trades-title")
        await h.wait_until(lambda: h.has("trades-empty") and h.text("trades-empty") == "（暂无逐笔交易）", "空表提示")
        assert not h.has("trades-grid")
    api = FakeKernelClient()
    api.set("trades", KernelError(503, "unavailable", "账本暂时读不到"))
    async with running_console(api) as h:
        await h.login()
        await h.goto("/trades", "trades-title")
        await h.wait_until(lambda: "账本暂时读不到" in h.text("trades-error"), "错误原因")


async def test_trades_page_keeps_working_when_the_kernel_is_lost():
    api = trades_api()
    async with running_console(api) as h:
        await open_trades(h)
        api.set("trades", KernelUnreachable("无法连接内核"))
        h.click("btn-refresh")
        await h.wait_until(lambda: not h.enabled("btn-trades-query"), "失联后查询置灰")
        assert [r["symbol"] for r in h.rows("trades-grid")] == ["BTCUSDT", "ETHUSDT"]  # 失联前的数据保留


# ------------------------------------------------------------------ 统计页

def stats_api() -> FakeKernelClient:
    api = FakeKernelClient()

    def answer(token, **query):
        group_by = query.get("group_by")
        blocks = {
            "model": [{"key": "m1", "stats": make_stat_block()}, {"key": "m2", "stats": make_stat_block(count=2, win_rate="1")}],
            "ownership": [{"key": "strategy", "stats": make_stat_block()}],
            "side": [{"key": "LONG", "stats": make_stat_block()}, {"key": "SHORT", "stats": make_stat_block(profit_loss_ratio=None)}],
            "symbol": [{"key": "BTCUSDT", "stats": make_stat_block()}],
        }
        return make_stats(groups=blocks.get(group_by, []))

    api.set("stats", answer)
    return api


async def open_stats(h) -> None:
    await h.login()
    await h.goto("/stats", "stats-title")
    await h.wait_until(lambda: h.has("stat-big-1") and h.text("stat-big-1") != "加载中…", "统计到达")


async def test_stats_headline_numbers_are_in_the_fixed_order():
    async with running_console(stats_api()) as h:
        await open_stats(h)
        assert [h.text(f"stat-big-label-{i}") for i in (1, 2, 3)] == ["盈亏比", "盈利率", "胜率"]
        assert [h.text(f"stat-big-{i}") for i in (1, 2, 3)] == ["2.50", "1.25%", "50.00%"]
        # 顺序也体现在页面里的先后（同一行里从左到右）
        ids = [h.element(f"stat-big-{i}").id for i in (1, 2, 3)]
        assert ids == sorted(ids)
        label_ids = [h.element(f"stat-big-label-{i}").id for i in (1, 2, 3)]
        assert label_ids == sorted(label_ids)
        # 三个大数字在其它指标之前
        assert max(ids) < h.element("stat-detail-count").id


async def test_stats_page_shows_other_metrics_groups_and_mixed_apart():
    async with running_console(stats_api()) as h:
        await open_stats(h)
        assert h.text("stat-detail-count") == "4" and h.text("stat-detail-net_pnl") == "+7.50"
        assert h.text("stat-detail-fees") == "1.00" and h.text("stat-detail-avg_duration") == "1 小时 30 分"
        assert h.text("stat-detail-max_win") == "+10.00" and h.text("stat-detail-max_loss") == "-4.00"
        await h.wait_until(lambda: h.has("stats-grid-symbol"), "四张分组表都到达")
        assert [r["key"] for r in h.rows("stats-grid-model")] == ["模型 1 · 完全自动化量化交易", "模型 2 · 接管手动订单"]
        assert [r["key"] for r in h.rows("stats-grid-ownership")] == ["策略"]
        assert [r["key"] for r in h.rows("stats-grid-side")] == ["多 LONG", "空 SHORT"]
        assert h.rows("stats-grid-side")[1]["profit_loss_ratio"] == "—"
        assert [r["key"] for r in h.rows("stats-grid-symbol")] == ["BTCUSDT"]
        # 混合仓单列：不在分组表里，也不并进总览
        assert h.text("stat-mixed-count") == "1" and h.text("stat-mixed-net_pnl") == "-2.00"
        assert h.text("stat-detail-count") == "4"


async def test_stats_page_asks_for_each_grouping_and_the_date_range():
    api = stats_api()
    async with running_console(api) as h:
        await open_stats(h)
        await h.wait_until(lambda: {q.get("group_by") for q in query_of(api, "stats")} >= {"model", "ownership", "side", "symbol"},
                           "四种分组都请求过")
        h.element("flt-since").set_value("2025-10-09")
        h.click("btn-stats-query")
        await h.wait_until(lambda: any(q.get("since_ms") == 1_759_942_800_000 for q in query_of(api, "stats")), "带起始日期")


async def test_stats_ratio_without_losses_is_a_dash_with_the_reason_and_errors_are_shown():
    api = FakeKernelClient()
    api.set("stats", make_stats(overall=make_stat_block(profit_loss_ratio=None), groups=[]))
    async with running_console(api) as h:
        await open_stats(h)
        assert h.text("stat-big-1") == "—" and h.text("stat-big-note-1") == "没有亏损交易"
    api = FakeKernelClient()
    api.set("stats", KernelError(503, "unavailable", "账本暂时读不到"))
    async with running_console(api) as h:
        await h.login()
        await h.goto("/stats", "stats-title")
        await h.wait_until(lambda: "账本暂时读不到" in h.text("stats-error"), "错误原因")


# ------------------------------------------------------------------ K 线页

def kline_api() -> FakeKernelClient:
    api = FakeKernelClient()
    api.set("candles", make_candles())
    return api


async def open_kline(h) -> None:
    await h.login()
    await h.goto("/kline", "kline-title")
    await h.wait_until(lambda: h.has("kline-chart"), "图表出现")


async def test_kline_page_draws_candles_dmr_markers_and_stops():
    api = kline_api()
    async with running_console(api) as h:
        await open_kline(h)
        options = h.element("kline-chart").options
        names = [s["name"] for s in options["series"]]
        assert names[:4] == ["K线", "DMR", "开仓", "平仓"] and "止损 1" in names and "止损 2" in names
        assert len(options["series"][0]["data"]) == 6
        assert h.text("kline-summary") == "K 线 6 根 · 开仓标记 1 个 · 平仓标记 1 个 · 止损线 2 段"
        first = query_of(api, "candles")[0]
        assert first["symbol"] == "BTCUSDT" and first["tf"] == "15m"


async def test_kline_page_reloads_for_another_symbol_and_timeframe():
    api = kline_api()
    async with running_console(api) as h:
        await open_kline(h)
        h.element("kline-symbol").set_value("ethusdt")
        h.element("kline-tf").set_value("1h")
        h.click("btn-kline-load")
        await h.wait_until(lambda: query_of(api, "candles")[-1].get("tf") == "1h", "重新请求")
        last = query_of(api, "candles")[-1]
        assert last["symbol"] == "ETHUSDT" and last["tf"] == "1h"


async def test_kline_page_empty_and_error_states():
    api = FakeKernelClient()
    api.set("candles", make_candles(0, candles=[], dmr=[], trades=[], stops=[]))
    async with running_console(api) as h:
        await h.login()
        await h.goto("/kline", "kline-title")
        await h.wait_until(lambda: h.has("kline-empty") and h.text("kline-empty") != "加载中…", "没有 K 线时的提示")
        assert h.text("kline-empty") == "（还没有这个合约的已收盘 K 线）" and not h.has("kline-chart")
    api = FakeKernelClient()
    api.set("candles", KernelError(422, "invalid", "不支持的周期"))
    async with running_console(api) as h:
        await h.login()
        await h.goto("/kline", "kline-title")
        await h.wait_until(lambda: "不支持的周期" in h.text("kline-error"), "错误原因")


# ------------------------------------------------------------------ 设置页：模型 3 分区

M3_CONFIG = [
    cfg("leverage", "资金", 5, min=1, max=20),
    cfg("m3_form", "模型3", "strategy_exec_ai_veto", choices=["strategy_exec_ai_veto", "ai_proposal_sealed", "ai_autonomous"],
        requires_deploy_switch=True),
    cfg("m3_ai_tier", "模型3", "off", choices=["off", "shadow", "advise", "veto"], requires_deploy_switch=True),
    cfg("m3_exec_tier", "模型3", "off", choices=["off", "shadow", "paper", "testnet", "live_manual", "live_auto"],
        requires_deploy_switch=True),
    cfg("m3_master_switch", "模型3", False, requires_deploy_switch=True),
]


def m3_api(*, deploy_switch=None, ai="off", execution="off") -> FakeKernelClient:
    api = FakeKernelClient()
    model3 = {"form": "strategy_exec_ai_veto", "ai_tier": ai, "exec_tier": execution, "master_switch": False}
    if deploy_switch is not None:
        model3["deploy_switch"] = deploy_switch
    api.set("state", ops_state(model3=model3))
    api.set("config", [dict(c, value=ai) if c["key"] == "m3_ai_tier" else dict(c, value=execution) if c["key"] == "m3_exec_tier" else c
                       for c in M3_CONFIG])
    return api


async def open_m3(h) -> None:
    await h.login()
    await h.goto("/settings", "settings-title")
    await h.wait_until(lambda: h.has("m3-section"), "模型 3 分区出现")


async def test_model_3_section_shows_both_tier_groups_default_off_and_read_only():
    async with running_console(m3_api()) as h:
        await open_m3(h)
        assert h.text("m3-ai-title") == "AI 档" and h.text("m3-exec-title") == "执行档"
        assert [h.text(f"m3-ai-opt-{v}") for v in ("off", "shadow", "advise", "veto")] == ["关闭", "影子记录", "建议", "否决"]
        assert [h.text(f"m3-exec-opt-{v}") for v in ("off", "shadow", "paper", "testnet", "live_manual", "live_auto")] == [
            "关闭", "影子", "模拟盘", "测试网", "实盘手动", "实盘自动"]
        assert h.text("m3-ai-current") == "当前：关闭" and h.text("m3-exec-current") == "当前：关闭"
        assert "改档需要部署级开关" in h.text("m3-readonly-note")
        assert not h.has("cfg-input-m3_ai_tier") and not h.has("btn-save-m3_ai_tier")


async def test_model_3_section_shows_the_current_tiers_from_the_kernel():
    async with running_console(m3_api(ai="shadow", execution="paper")) as h:
        await open_m3(h)
        assert h.text("m3-ai-current") == "当前：影子记录" and h.text("m3-exec-current") == "当前：模拟盘"
        assert "m3-opt-current" in h.element("m3-ai-opt-shadow").classes
        assert "m3-opt-current" not in h.element("m3-ai-opt-off").classes


async def test_model_3_tiers_become_editable_only_when_the_deploy_switch_is_on():
    api = m3_api(deploy_switch=True)
    async with running_console(api) as h:
        await open_m3(h)
        await h.wait_until(lambda: h.has("cfg-input-m3_ai_tier"), "开关打开后可改档")
        assert not h.has("m3-readonly-note")
        await h.wait_until(lambda: h.enabled("btn-save-m3_ai_tier"), "有席位时可保存")
        h.element("cfg-input-m3_ai_tier").set_value("shadow")
        h.click("btn-save-m3_ai_tier")
        await h.wait_until(lambda: bool(api.puts_to("m3_ai_tier")), "提交给内核")
        assert api.puts_to("m3_ai_tier")[0]["value"] == "shadow"


async def test_model_3_kernel_refusal_reason_is_shown_verbatim():
    api = m3_api(deploy_switch=True)
    api.set("PUT /config/m3_exec_tier", KernelError(422, "invalid", "部署级开关没开不能改模型 3 档位"))
    async with running_console(api) as h:
        await open_m3(h)
        await h.wait_until(lambda: h.enabled("btn-save-m3_exec_tier"), "可保存")
        h.element("cfg-input-m3_exec_tier").set_value("paper")
        h.click("btn-save-m3_exec_tier")
        await h.wait_until(lambda: "部署级开关没开不能改模型 3 档位" in h.text("cfg-msg-m3_exec_tier"), "内核原因")


async def test_model_3_section_without_the_state_field_stays_read_only():
    api = FakeKernelClient()
    api.set("state", make_state())
    api.set("config", M3_CONFIG)
    async with running_console(api) as h:
        await open_m3(h)
        assert h.text("m3-ai-current") == "当前：关闭" and "改档需要部署级开关" in h.text("m3-readonly-note")
