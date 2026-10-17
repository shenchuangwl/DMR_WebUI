"""M5 部分：平掉 M4 留下的策略仓得到一笔逐笔交易 -> 逐笔交易页、统计页（盈亏比、盈利率、胜率顺序）、K 线页（蜡烛与标记）
-> 设置页模型 3 分区 -> 切到模型 3 点启动被内核拒绝并写明原因。截图 docs/acceptance/m5-*.png。

"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from conftest import OPERATOR  # noqa: F401
from deploy_kit import wait_until

pytestmark = pytest.mark.ui

SYMBOL = "SOLUSDT"  # M4 开的策略仓


def wait_for(predicate, what, timeout=20.0):
    wait_until(predicate, timeout, interval=0.3, what=what)


def ensure_seat(page) -> None:
    expect(page.get_by_role("button", name=re.compile("申请席位|释放席位")).first).to_be_visible()
    button = page.get_by_role("button", name="申请席位")
    if button.is_visible():
        button.click()
    expect(page.get_by_role("button", name="释放席位")).to_be_visible()


def live_rows(deployment) -> list[dict]:
    return [p for p in deployment.positions() if p.get("symbol") == SYMBOL and p.get("status") in ("managed", "closing")]


def test_close_strategy_position_makes_a_trade(deployment, page, shot):
    ensure_seat(page)
    page.get_by_role("link", name="持仓", exact=True).click()
    qty = live_rows(deployment)[0]["qty"]
    expect(page.get_by_text(f"{SYMBOL} 多 LONG · 数量 {qty}").first).to_be_visible()
    page.get_by_role("button", name="平掉此仓").click()
    page.get_by_role("button", name="确认平掉此仓").click()
    expect(page.get_by_text("已提交平仓")).to_be_visible()
    wait_for(lambda: not live_rows(deployment), "策略仓清零")
    page.get_by_role("button", name="关闭", exact=True).click()


def test_trades_page_has_data(deployment, page, shot):
    status, body = deployment.kernel_get("/trades")
    assert status == 200 and body["total"] >= 1, body
    assert body["items"][0]["symbol"] == SYMBOL
    page.get_by_role("link", name="逐笔交易", exact=True).click()
    expect(page.get_by_text(SYMBOL).first).to_be_visible(timeout=20000)
    for column in ("净盈亏", "手续费", "资金费", "入场原因", "出场原因"):
        expect(page.get_by_text(column).first).to_be_visible()
    shot("m5-01-trades")


def test_stats_page_order_and_data(deployment, page, shot):
    status, body = deployment.kernel_get("/stats")
    assert status == 200 and "overall" in body and body["overall"], body
    page.get_by_role("link", name="统计", exact=True).click()
    for label in ("盈亏比", "盈利率", "胜率"):
        expect(page.get_by_text(label).first).to_be_visible(timeout=20000)
    assert page.locator(".dmr-big-label").all_inner_texts() == ["盈亏比", "盈利率", "胜率"], "顶部三个大数字应依次为盈亏比、盈利率、胜率"
    text = page.locator("body").inner_text()
    assert text.index("盈亏比") < text.index("盈利率") < text.index("胜率")
    expect(page.get_by_text("混合仓").first).to_be_visible()
    shot("m5-02-stats")


def test_candles_page_has_chart_and_markers(deployment, page, shot):
    status, body = deployment.kernel_get(f"/candles?symbol={SYMBOL}&tf=15m&limit=100")
    assert status == 200 and body["candles"], body
    assert body["trades"], "应有进出场标记"
    page.get_by_role("link", name="K 线", exact=True).click()
    page.get_by_label("合约").fill(SYMBOL)  # 页面默认 BTCUSDT，策略仓开在 SOLUSDT
    page.get_by_role("button", name="查看").click()
    expect(page.locator("canvas").first).to_be_visible(timeout=20000)
    expect(page.get_by_text(re.compile("开仓标记 [1-9]"))).to_be_visible(timeout=20000)
    shot("m5-03-candles")


def test_settings_model3_section(deployment, page, shot):
    page.get_by_role("link", name="设置", exact=True).click()
    expect(page.get_by_text("m3_").first).to_be_visible(timeout=20000)
    expect(page.get_by_text("改档需要部署级开关").first).to_be_visible()
    shot("m5-04-settings-m3")
    status, body = deployment.kernel_get("/state")
    assert status == 200
    m3 = body.get("model3")
    assert isinstance(m3, dict) and {"form", "ai_tier", "exec_tier", "master_switch", "deploy_switch"} <= set(m3), "/state.model3 应给出模型 3 的形态与档位"


def test_model3_start_is_rejected_with_reason(deployment, page, shot):
    page.get_by_role("link", name="总览", exact=True).click()
    page.get_by_role("button", name="切换模型").click()
    page.get_by_role("button", name="模型 3 · AI（预留）").click()
    page.get_by_role("button", name="保留原规则继续托管").click()
    page.get_by_label("口令").fill(deployment.password)
    page.get_by_role("button", name="确认切换").click()
    expect(page.get_by_text(re.compile("已切换到|切换失败"))).to_be_visible()
    page.get_by_role("button", name="关闭", exact=True).click()
    expect(page.get_by_text("切换预览中")).to_have_count(0, timeout=20000)  # 等状态刷新
    start = page.get_by_role("button", name="启动", exact=True)
    expect(start).to_be_enabled()  # 模型 3 的启动按钮保持可点，由内核拒绝并写明原因
    start.click()
    page.get_by_role("button", name="确认启动").click()
    expect(page.get_by_text(re.compile("模型 3 本期只预留|模型 3.*不能启动")).first).to_be_visible(timeout=20000)
    assert "已启动：开仓闸已打开" not in page.locator("body").inner_text()
    shot("m5-05-model3-start-rejected")
    status, body = deployment.kernel_get("/state")
    assert status == 200 and "running" not in str(body.get("gate", body.get("gate_state", ""))), body
