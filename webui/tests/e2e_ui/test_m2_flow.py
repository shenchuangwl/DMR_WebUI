"""M2 部分：登录、总览、申请席位、开发页测试造仓、部分平仓 50%、平掉此仓、一键全平两档。截图 docs/acceptance/m3-*.png。"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from conftest import OPERATOR
from deploy_kit import wait_until

pytestmark = pytest.mark.ui


def test_login_and_overview(deployment, page, shot):
    page.goto(deployment.console + "/")
    expect(page).to_have_url(f"{deployment.console}/login")
    shot("m3-00-login")
    page.get_by_label("用户名").fill(OPERATOR)
    page.get_by_label("口令").fill(deployment.password)
    page.get_by_role("button", name="登录").click()
    expect(page.get_by_text(f"操作员：{OPERATOR}")).to_be_visible()
    expect(page.get_by_text("替身环境 · 非真实交易").first).to_be_visible()
    for name in ("启动", "停止", "部分平仓", "一键全部平仓"):
        expect(page.get_by_role("button", name=name, exact=True)).to_be_visible()
    shot("m3-01-overview")


def test_acquire_seat(deployment, page, shot):
    page.get_by_role("button", name="申请席位").click()
    expect(page.get_by_role("button", name="释放席位")).to_be_visible()
    expect(page.get_by_role("button", name="启动", exact=True)).to_be_enabled()


def test_dev_test_open(deployment, page, shot):
    page.get_by_role("link", name="替身开发", exact=True).click()
    expect(page.get_by_text("测试造仓（走内核")).to_be_visible()
    page.get_by_label("名义金额（USDT）").fill("2000")  # 数量要够大，50% 才不会被步长取整成 0
    page.get_by_role("button", name="测试造仓 · 多 LONG").click()
    expect(page.get_by_text(re.compile("测试造仓 BTCUSDT LONG 2000：已完成，意图状态 \\w+"))).to_be_visible()
    shot("m3-02-dev")


def managed_rows(deployment) -> list[dict]:
    return [p for p in deployment.positions() if p.get("status") == "managed"]


def wait_for(predicate, what, timeout=20.0):
    wait_until(predicate, timeout, interval=0.3, what=what)


def test_partial_close_half(deployment, page, shot):
    wait_for(lambda: len(managed_rows(deployment)) == 1, "内核出现 1 个托管仓")
    before = float(managed_rows(deployment)[0]["qty"])
    page.get_by_role("link", name="持仓", exact=True).click()
    expect(page.get_by_text("BTCUSDT 多 LONG · 数量")).to_be_visible()
    page.get_by_role("button", name="部分平仓", exact=True).click()
    page.get_by_role("button", name="50%", exact=True).click()
    expect(page.get_by_text("实际平仓数量")).to_be_visible()
    shot("m3-03-partial-preview")
    page.get_by_role("button", name="确认部分平仓").click()
    expect(page.get_by_text("已提交部分平仓")).to_be_visible()
    wait_for(lambda: abs(float(managed_rows(deployment)[0]["qty"]) - before / 2) <= before * 0.1, "数量约剩一半")
    page.get_by_role("button", name="关闭", exact=True).click()
    shot("m3-04-after-partial")


def test_close_one(deployment, page, shot):
    qty = managed_rows(deployment)[0]["qty"]
    expect(page.get_by_text(f"BTCUSDT 多 LONG · 数量 {qty}")).to_be_visible()  # 等页面拿到最新持仓版本，否则会被内核以版本过期拒绝
    page.get_by_role("button", name="平掉此仓").click()
    page.get_by_role("button", name="确认平掉此仓").click()
    expect(page.get_by_text("已提交平仓")).to_be_visible()
    wait_for(lambda: not managed_rows(deployment), "托管仓清零")
    page.get_by_role("button", name="关闭", exact=True).click()
    expect(page.get_by_text("（没有托管持仓）")).to_be_visible()
    shot("m3-05-after-close-one")


def open_test_position(deployment, page, side_text="测试造仓 · 多 LONG", notional="2000"):
    page.get_by_role("link", name="替身开发", exact=True).click()
    expect(page.get_by_text("测试造仓（走内核")).to_be_visible()
    page.get_by_label("名义金额（USDT）").fill(notional)
    page.get_by_role("button", name=side_text).click()
    expect(page.get_by_text(re.compile(f"测试造仓 BTCUSDT {'LONG' if 'LONG' in side_text else 'SHORT'} {notional}：已完成，意图状态 \\w+")).first).to_be_visible()
    wait_for(lambda: len(managed_rows(deployment)) == 1, "内核出现 1 个托管仓")


@pytest.mark.parametrize("scope,shot_name", [("当前模型归属的持仓", "m3-06-close-all-model"), ("全部托管持仓", "m3-07-close-all-managed")])
def test_close_all_two_scopes(deployment, page, shot, scope, shot_name):
    open_test_position(deployment, page)
    page.get_by_role("link", name="总览", exact=True).click()
    page.get_by_role("button", name="一键全部平仓", exact=True).click()
    page.get_by_role("button", name=scope).click()
    expect(page.get_by_text("将平掉（1）")).to_be_visible()
    page.get_by_label("口令").fill(deployment.password)
    page.get_by_role("button", name="确认全部平仓").click()
    expect(page.get_by_text("完成：")).to_be_visible()
    wait_for(lambda: not managed_rows(deployment), "托管仓清零")
    shot(shot_name)
    page.get_by_role("button", name="关闭", exact=True).click()
