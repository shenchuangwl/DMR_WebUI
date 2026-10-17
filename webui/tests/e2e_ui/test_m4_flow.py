"""M4 部分：停掉模型 2 并切到模型 1 -> 名单页显示回放的假名单（DMR_LIST_URL 指向测试内起的回环假名单服务）-> 启动
-> 开发页注入信号后开仓并挂止损 -> 停止后再注入不再开仓。截图 docs/acceptance/m4-*.png。

接在 test_m3_flow 之后运行（共用同一套部署，M3 结束时模型 2 在运行）。文案与实现不一致时按实现改这里。
"""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import expect

from conftest import OPERATOR  # noqa: F401
from deploy_kit import wait_until

pytestmark = pytest.mark.ui

SYMBOL = "SOLUSDT"      # 假名单里 LONG（BTCUSDT 在前面几组测试里平过仓，还在冷却期）
SHORT_SYMBOL = "BNBUSDT"  # 假名单里 SHORT


def strategy_rows(deployment, symbol: str) -> list[dict]:
    return [p for p in deployment.positions() if p.get("symbol") == symbol and p.get("ownership") == "strategy"
            and p.get("status") in ("managed", "closing")]


def wait_for(predicate, what, timeout=20.0):
    wait_until(predicate, timeout, interval=0.3, what=what)


def ensure_seat(page) -> None:
    expect(page.get_by_role("button", name=re.compile("申请席位|释放席位")).first).to_be_visible()
    button = page.get_by_role("button", name="申请席位")
    if button.is_visible():
        button.click()
    expect(page.get_by_role("button", name="释放席位")).to_be_visible()


def inject(page, symbol: str, cross: str, trend: str) -> None:
    page.get_by_role("link", name="替身开发", exact=True).click()
    expect(page.get_by_text("信号注入（走内核")).to_be_visible()
    card = page.get_by_text("信号注入（走内核").locator("xpath=ancestor::*[.//button[normalize-space()='注入信号']][1]")
    card.get_by_label("合约").fill(symbol)
    card.get_by_role("button", name=cross, exact=True).click()
    card.get_by_role("button", name=trend, exact=True).click()
    card.get_by_role("button", name="注入信号", exact=True).click()


def test_stop_model2_and_switch_to_model1(deployment, page, shot):
    ensure_seat(page)
    close = page.get_by_role("button", name="关闭", exact=True)  # M3 最后的弹窗可能还开着
    if close.count() and close.first.is_visible():
        close.first.click()
    page.get_by_role("link", name="总览", exact=True).click()
    stop = page.get_by_role("button", name="停止", exact=True)
    expect(stop).to_be_enabled()
    stop.click()
    page.get_by_role("button", name="确认停止").click()
    expect(page.get_by_text("已停止：开仓闸已关闭")).to_be_visible()
    page.get_by_role("button", name="关闭", exact=True).click()
    page.get_by_role("button", name="切换模型").click()
    page.get_by_role("button", name="模型 1 · 完全自动化量化交易").click()
    page.get_by_role("button", name="保留原规则继续托管").click()
    page.get_by_label("口令").fill(deployment.password)
    page.get_by_role("button", name="确认切换").click()
    expect(page.get_by_text("已切换到")).to_be_visible()
    page.get_by_role("button", name="关闭", exact=True).click()
    expect(page.get_by_text("模型 1 · 完全自动化量化交易").first).to_be_visible()


def test_watchlist_page_shows_replayed_list(deployment, page, shot):
    page.get_by_role("link", name="选币名单", exact=True).click()
    expect(page.get_by_text("选币名单").first).to_be_visible()
    expect(page.get_by_text("BTCUSDT").first).to_be_visible(timeout=20000)
    expect(page.get_by_text(SHORT_SYMBOL).first).to_be_visible()
    expect(page.get_by_text("有效期至：").first).to_be_visible()
    assert deployment.fake_list.hits >= 1, "内核应已拉过测试内的假名单"
    shot("m4-01-watchlist")


def test_start_model1(deployment, page, shot):
    page.get_by_role("link", name="总览", exact=True).click()
    page.get_by_role("button", name="启动", exact=True).click()
    page.get_by_role("button", name="确认启动").click()
    expect(page.get_by_text("已启动")).to_be_visible()
    shot("m4-02-started")
    page.get_by_role("button", name="关闭", exact=True).click()
    expect(page.get_by_text("运行中").first).to_be_visible()


def test_inject_signal_opens_with_stop(deployment, page, shot):
    inject(page, SYMBOL, "上穿", "看多")
    expect(page.get_by_text(re.compile(f"信号注入 {SYMBOL} 短周期上穿 长周期看多：已完成，意图状态 \\w+"))).to_be_visible(timeout=30000)
    shot("m4-03-injected")
    wait_for(lambda: strategy_rows(deployment, SYMBOL), "出现策略仓", 30)
    pos = strategy_rows(deployment, SYMBOL)[0]
    assert pos["side"] == "LONG"
    wait_for(lambda: strategy_rows(deployment, SYMBOL)[0].get("protection_state") == "system", "止损保护单已挂（系统保护）", 30)
    page.get_by_role("link", name="持仓", exact=True).click()
    expect(page.get_by_text(re.compile(f"{SYMBOL} 多 LONG")).first).to_be_visible()
    expect(page.get_by_text("系统保护").first).to_be_visible()
    shot("m4-04-position-with-stop")


def test_stop_then_no_more_opens(deployment, page, shot):
    page.get_by_role("link", name="总览", exact=True).click()
    page.get_by_role("button", name="停止", exact=True).click()
    page.get_by_role("button", name="确认停止").click()
    expect(page.get_by_text("已停止：开仓闸已关闭")).to_be_visible()
    page.get_by_role("button", name="关闭", exact=True).click()
    inject(page, SHORT_SYMBOL, "下穿", "看空")
    expect(page.get_by_text(re.compile(f"信号注入 {SHORT_SYMBOL} 短周期下穿 长周期看空："))).to_be_visible(timeout=30000)
    time.sleep(3)
    assert not strategy_rows(deployment, SHORT_SYMBOL), "停止后不应再开仓"
    shot("m4-05-stopped-no-open")


def test_oplog_has_m4_operations(deployment, page, shot):
    page.get_by_role("link", name="操作日志", exact=True).click()
    expect(page.get_by_text(re.compile("inject_signal|信号注入")).first).to_be_visible(timeout=20000)
    shot("m4-06-oplog")
