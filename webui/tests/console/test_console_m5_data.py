"""M5C 的纯逻辑与客户端：逐笔交易行、统计数字（盈亏比、盈利率、胜率的顺序）、K 线图选项、查询参数。"""
from __future__ import annotations

from console_kit import (GOOD_TOKEN, START_MS, FakeKernel, make_candles, make_stat_block, make_stats, make_trade, make_trades)
from test_console_client import make_client
from test_console_statusbar import UTC7


# ------------------------------------------------------------------ 客户端

async def test_client_trades_sends_only_the_filters_that_are_set():
    kernel = FakeKernel()
    kernel.reply("GET", "/trades", make_trades(make_trade()))
    data = await make_client(kernel).trades(GOOD_TOKEN, symbol="BTCUSDT", side="LONG", since_ms=5, limit=50, order="desc")
    assert data["total"] == 1 and data["items"][0]["symbol"] == "BTCUSDT"
    (request,) = kernel.requests_to("GET", "/trades")
    assert dict(request.url.params) == {"symbol": "BTCUSDT", "side": "LONG", "since_ms": "5", "limit": "50", "order": "desc"}
    assert request.headers["authorization"] == f"Bearer {GOOD_TOKEN}"


async def test_client_stats_and_candles_pass_their_params():
    kernel = FakeKernel()
    kernel.reply("GET", "/stats", make_stats())
    kernel.reply("GET", "/candles", make_candles())
    client = make_client(kernel)
    assert (await client.stats(GOOD_TOKEN, group_by="model", until_ms=9))["overall"]["count"] == 4
    assert dict(kernel.requests_to("GET", "/stats")[0].url.params) == {"group_by": "model", "until_ms": "9"}
    assert len((await client.candles(GOOD_TOKEN, symbol="BTCUSDT", tf="15m", limit=100))["candles"]) == 6
    assert dict(kernel.requests_to("GET", "/candles")[0].url.params) == {"symbol": "BTCUSDT", "tf": "15m", "limit": "100"}


async def test_client_rejects_a_reply_that_is_not_an_object():
    import pytest
    from dmr_console.client import KernelUnreachable

    kernel = FakeKernel()
    kernel.reply("GET", "/trades", [1, 2])
    with pytest.raises(KernelUnreachable):
        await make_client(kernel).trades(GOOD_TOKEN)


# ------------------------------------------------------------------ 逐笔交易行

def test_trade_row_shows_what_the_plan_asks_for():
    from dmr_console.tradeviews import trade_rows

    (row,) = trade_rows(make_trades(make_trade()), tz=UTC7)
    assert row["opened"] == "2025-10-09 13:53:20" and row["closed"] == "2025-10-09 14:53:20"  # 本机时区（+07）
    assert row["symbol"] == "BTCUSDT" and row["side"] == "多 LONG" and row["ownership"] == "策略" and row["mixed"] == ""
    assert row["qty"] == "0.010" and row["open_avg"] == "60000" and row["close_avg"] == "61000"
    assert row["gross"] == "+10.00" and row["fees"] == "0.50" and row["funding"] == "0.10" and row["net"] == "+9.40"
    assert row["entry_reason"] == "名单做多 BTCUSDT（分数 8.5）" and row["exit_reason"] == "止损"
    assert row["duration"] == "1 小时" and row["return_pct"] == "1.57%" and row["pnl_sign"] == 1


def test_trade_rows_mark_mixed_estimated_open_time_and_losses():
    from dmr_console.tradeviews import trade_rows

    rows = trade_rows(make_trades(make_trade(id=2, is_mixed=True, open_time_estimated=True, net_pnl="-3", side="SHORT",
                                              exit_reason="close_all", model=None)), tz=UTC7)
    row = rows[0]
    assert row["mixed"] == "混合仓" and row["opened"].startswith("约 ") and row["net"] == "-3.00" and row["pnl_sign"] == -1
    assert row["side"] == "空 SHORT" and row["exit_reason"] == "一键全平" and row["model"] == "—"


def test_trade_rows_survive_bad_data_and_unknown_codes():
    from dmr_console.tradeviews import trade_rows

    assert trade_rows(None, tz=UTC7) == [] and trade_rows({"items": "x"}, tz=UTC7) == []
    rows = trade_rows({"items": [None, 5, {"symbol": "X", "exit_reason": "brand_new", "net_pnl": None, "open_ms": None}]}, tz=UTC7)
    assert len(rows) == 1 and rows[0]["exit_reason"] == "brand_new" and rows[0]["net"] == "—" and rows[0]["opened"] == "—"
    assert rows[0]["pnl_sign"] == 0


# ------------------------------------------------------------------ 查询条件

def test_trade_query_turns_form_values_into_kernel_params():
    from dmr_console.tradeviews import trade_query

    params, problem = trade_query(symbol=" btcusdt ", ownership="strategy", model="", side="LONG", since="2025-10-09",
                                  until="2025-10-09", tz=UTC7)
    assert problem == ""
    # 本机日期按本地时区换成毫秒：起始是当天 00:00，结束是次日 00:00（不含）
    assert params == {"symbol": "BTCUSDT", "ownership": "strategy", "side": "LONG",
                      "since_ms": 1_759_942_800_000, "until_ms": 1_760_029_200_000}


def test_trade_query_ignores_blanks_and_reports_bad_dates():
    from dmr_console.tradeviews import trade_query

    assert trade_query(symbol="", ownership=None, model=None, side=None, since="", until="  ", tz=UTC7) == ({}, "")
    params, problem = trade_query(symbol="", ownership=None, model=None, side=None, since="昨天", until="", tz=UTC7)
    assert problem == "开始日期需要写成 年-月-日（例如 2025-10-09）"
    _, problem = trade_query(symbol="", ownership=None, model=None, side=None, since="2025-10-10", until="2025-10-09", tz=UTC7)
    assert problem == "开始日期不能晚于结束日期"


# ------------------------------------------------------------------ 统计数字

def test_headline_order_is_profit_loss_ratio_then_profit_rate_then_win_rate():
    from dmr_console.tradeviews import HEADLINE_KEYS, stats_headline

    assert HEADLINE_KEYS == ("profit_loss_ratio", "profit_rate", "win_rate")
    head = stats_headline(make_stat_block())
    assert [h["label"] for h in head] == ["盈亏比", "盈利率", "胜率"]
    assert [h["value"] for h in head] == ["2.50", "1.25%", "50.00%"]


def test_headline_ratio_without_losing_trades_is_null_and_says_why():
    from dmr_console.tradeviews import stats_headline

    head = stats_headline(make_stat_block(profit_loss_ratio=None))
    assert head[0]["value"] == "—" and head[0]["note"] == "没有亏损交易"
    assert stats_headline(make_stat_block(profit_loss_ratio="0"))[0]["value"] == "0.00"
    assert stats_headline(make_stat_block(count=0, profit_loss_ratio=None, profit_rate=None, win_rate=None))[0]["note"] == "还没有交易"


def test_stats_details_list_the_other_metrics():
    from dmr_console.tradeviews import stats_details

    details = {d["key"]: d["value"] for d in stats_details(make_stat_block())}
    assert details == {"count": "4", "net_pnl": "+7.50", "gross_pnl": "+9.00", "fees": "1.00", "funding": "0.50",
                       "avg_duration": "1 小时 30 分", "max_win": "+10.00", "max_loss": "-4.00"}


def test_stats_group_rows_are_labelled_and_survive_bad_data():
    from dmr_console.tradeviews import stats_group_rows

    rows = stats_group_rows([{"key": "m1", "stats": make_stat_block()}, {"key": "m2", "stats": make_stat_block(win_rate="1")}],
                            "model")
    assert [r["key"] for r in rows] == ["模型 1 · 完全自动化量化交易", "模型 2 · 接管手动订单"]
    assert rows[0]["profit_loss_ratio"] == "2.50" and rows[0]["profit_rate"] == "1.25%" and rows[0]["win_rate"] == "50.00%"
    assert rows[1]["win_rate"] == "100.00%" and rows[0]["count"] == "4" and rows[0]["net_pnl"] == "+7.50"
    assert [r["key"] for r in stats_group_rows([{"key": "LONG", "stats": make_stat_block()}], "side")] == ["多 LONG"]
    assert [r["key"] for r in stats_group_rows([{"key": "strategy", "stats": make_stat_block()}], "ownership")] == ["策略"]
    assert [r["key"] for r in stats_group_rows([{"key": "ETHUSDT", "stats": make_stat_block()}], "symbol")] == ["ETHUSDT"]
    assert stats_group_rows(None, "model") == []
    bad = stats_group_rows([None, {"key": "x"}, {"stats": {}}, "junk"], "symbol")
    assert [r["key"] for r in bad] == ["x", "—"] and bad[0]["win_rate"] == "—" and bad[0]["count"] == "—"


# ------------------------------------------------------------------ K 线图

def test_kline_option_has_candles_dmr_markers_and_stop_lines():
    from dmr_console.tradeviews import kline_option, kline_summary

    data = make_candles()
    option = kline_option(data, tz=UTC7)
    series = {s["name"]: s for s in option["series"]}
    assert set(series) >= {"K线", "DMR", "开仓", "平仓", "止损 1", "止损 2"}
    candle = series["K线"]
    assert candle["type"] == "candlestick" and len(candle["data"]) == 6
    assert candle["data"][0] == [100.0, 102.0, 95.0, 105.0]  # ECharts 次序：开、收、低、高
    categories = option["xAxis"][0]["data"]
    assert len(categories) == 6 and categories[0] == "2025-10-09 14:23"  # 本机时区
    dmr = series["DMR"]
    assert dmr["type"] == "line" and dmr["xAxisIndex"] == 1 and dmr["yAxisIndex"] == 1
    assert dmr["data"][:2] == [None, None] and dmr["data"][2] == 0.2
    entry, exit_ = series["开仓"], series["平仓"]
    assert entry["data"] == [[categories[1], 101.0]] and exit_["data"] == [[categories[4], 104.0]]
    assert series["止损 1"]["data"] == [[categories[1], 98.0], [categories[3], 98.0]]
    assert series["止损 2"]["data"] == [[categories[3], 100.0], [categories[4], 100.0]]
    assert kline_summary(data) == "K 线 6 根 · 开仓标记 1 个 · 平仓标记 1 个 · 止损线 2 段"


def test_kline_option_marks_short_trades_and_open_trades_without_close():
    from dmr_console.tradeviews import kline_option

    data = make_candles(trades=[{"position_id": 2, "side": "SHORT", "open_ms": START_MS - 5 * 900_000, "open_price": "100",
                                 "close_ms": None, "close_price": None, "exit_reason": None}], stops=[])
    series = {s["name"]: s for s in kline_option(data, tz=UTC7)["series"]}
    assert len(series["开仓"]["data"]) == 1 and series["平仓"]["data"] == []
    assert not any(name.startswith("止损") for name in series)


def test_kline_option_survives_empty_and_broken_data():
    from dmr_console.tradeviews import kline_option, kline_summary

    for bad in (None, {}, {"candles": "x"}, {"candles": [None, [1], ["a", 1, 2, 3, 4, 5]], "dmr": 5, "trades": [None], "stops": [3]}):
        option = kline_option(bad, tz=UTC7)
        assert option["series"][0]["data"] == [] or isinstance(option["series"][0]["data"], list)
    assert kline_summary(None) == "还没有 K 线数据"
