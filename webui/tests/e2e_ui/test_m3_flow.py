"""M3 部分：设置接管清单 -> 切到并启动模型 2 -> 开发页「模拟手动下单」开清单内的仓 -> 10 秒内被接管且系统保护 -> 部分平仓 50% -> 平掉此仓
-> 清单外开仓进入待确认 -> 忽略。截图 docs/acceptance/m3-1*.png。

依赖 M3 的内核（models/m2_takeover、ops/takeover_ops）与控制台（设置页、持仓页接管按钮）；并行任务合并后本组已全部通过，不再标 xfail。
接管类弹窗与设置页的元素 ``.mark()`` 不进 DOM，这里按可见文字定位：确认按钮「确认接管」「确认忽略」，设置页以配置键（takeover_symbols）所在的行定位输入框，
同一行的保存按钮是「保存」。文案与实现不一致时按实现改这里。
"""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import expect

from conftest import OPERATOR  # noqa: F401  (保证先加载 conftest)
from deploy_kit import wait_until

pytestmark = pytest.mark.ui

IN_LIST = "BTCUSDT"
OUT_OF_LIST = "ETHUSDT"
TAKEOVER_SECONDS = 10.0


def positions_by(deployment, **want) -> list[dict]:
    return [p for p in deployment.positions() if all(p.get(k) == v for k, v in want.items())]


def external_by(deployment, **want) -> list[dict]:
    return [p for p in deployment.external() if all(p.get(k) == v for k, v in want.items())]


def wait_for(predicate, what, timeout=20.0):
    wait_until(predicate, timeout, interval=0.3, what=what)


def ensure_seat(page) -> None:
    expect(page.get_by_role("button", name=re.compile("申请席位|释放席位")).first).to_be_visible()  # 等状态取到
    button = page.get_by_role("button", name="申请席位")
    if button.is_visible():
        button.click()
    expect(page.get_by_role("button", name="释放席位")).to_be_visible()


def manual_open(page, symbol: str, qty: str, shot_name: str | None = None) -> None:
    page.get_by_role("link", name="替身开发", exact=True).click()
    expect(page.get_by_text("模拟手动下单")).to_be_visible()
    # 开发页有好几个「合约」框（测试造仓、信号注入、模拟手动下单……）：按卡片标题定位到「模拟手动下单」这一张卡片再找
    card = page.get_by_text("模拟手动下单").locator("xpath=ancestor::*[.//button[normalize-space()='开仓']][1]")
    card.get_by_label("合约").fill(symbol)
    card.get_by_label("数量", exact=True).fill(qty)
    card.get_by_role("button", name="开仓", exact=True).click()
    expect(page.get_by_text(f"手动开仓 {symbol} LONG 数量 {qty} 成功")).to_be_visible()


def test_set_takeover_list_in_settings(deployment, page, shot):
    ensure_seat(page)
    page.get_by_role("link", name="设置", exact=True).click()
    key = page.get_by_text("takeover_symbols", exact=True)  # 每项的输入框没有标签：以配置键定位所在的行
    expect(key).to_be_visible()
    row = key.locator("xpath=ancestor::*[.//button[normalize-space()='保存']][1]")
    row.locator("input").fill(IN_LIST)
    row.get_by_role("button", name="保存").click()
    expect(page.get_by_text("已保存")).to_be_visible()
    shot("m3-10-settings")
    status, body = deployment.kernel_get("/config")
    assert status == 200
    items = body if isinstance(body, list) else body.get("items", body.get("config", []))
    assert [i["value"] for i in items if i["key"] == "takeover_symbols"] == [[IN_LIST]]


def test_switch_to_model_2_and_start(deployment, page, shot):
    ensure_seat(page)
    page.get_by_role("link", name="总览", exact=True).click()
    page.get_by_role("button", name="切换模型").click()
    page.get_by_role("button", name="模型 2 · 接管手动订单").click()
    page.get_by_role("button", name="保留原规则继续托管").click()
    page.get_by_label("口令").fill(deployment.password)
    page.get_by_role("button", name="确认切换").click()
    expect(page.get_by_text("已切换到")).to_be_visible()
    page.get_by_role("button", name="关闭", exact=True).click()
    page.get_by_role("button", name="启动", exact=True).click()
    page.get_by_role("button", name="确认启动").click()
    expect(page.get_by_text("已启动")).to_be_visible()
    page.get_by_role("button", name="关闭", exact=True).click()
    expect(page.get_by_text("模型 2 · 接管手动订单").first).to_be_visible()
    expect(page.get_by_text("运行中").first).to_be_visible()
    shot("m3-11-model2-running")


def test_manual_open_in_list_is_taken_over_within_10s(deployment, page, shot):
    started = time.monotonic()
    manual_open(page, IN_LIST, "0.002")
    page.get_by_role("link", name="持仓", exact=True).click()
    expect(page.get_by_text("接管仓（1）")).to_be_visible(timeout=TAKEOVER_SECONDS * 1000)
    elapsed = time.monotonic() - started
    print(f"接管耗时 {elapsed:.1f} 秒")
    assert elapsed <= TAKEOVER_SECONDS + 3, f"接管耗时 {elapsed:.1f} 秒"  # 页面轮询自身有几秒延迟，内核侧另由内核测试卡 10 秒
    rows = positions_by(deployment, symbol=IN_LIST, ownership="takeover")
    assert len(rows) == 1
    wait_for(lambda: positions_by(deployment, symbol=IN_LIST, ownership="takeover")[0].get("protection_state") == "system", "保护状态为系统保护")
    expect(page.get_by_text("系统保护").first).to_be_visible()
    shot("m3-12-taken-over")


def test_partial_close_half_of_taken_over(deployment, page, shot):
    before = float(positions_by(deployment, symbol=IN_LIST, ownership="takeover")[0]["qty"])
    expect(page.get_by_text(f"{IN_LIST} 多 LONG · 数量 {positions_by(deployment, symbol=IN_LIST)[0]['qty']}").first).to_be_visible()
    page.get_by_role("button", name="部分平仓", exact=True).click()
    page.get_by_role("button", name="50%", exact=True).click()
    expect(page.get_by_text("实际平仓数量")).to_be_visible()
    page.get_by_role("button", name="确认部分平仓").click()
    expect(page.get_by_text("已提交部分平仓")).to_be_visible()
    wait_for(lambda: abs(float(positions_by(deployment, symbol=IN_LIST, ownership="takeover")[0]["qty"]) - before / 2) <= before * 0.1, "数量约剩一半")
    page.get_by_role("button", name="关闭", exact=True).click()
    shot("m3-13-taken-over-partial")


def test_close_taken_over_position(deployment, page, shot):
    qty = positions_by(deployment, symbol=IN_LIST, ownership="takeover")[0]["qty"]
    expect(page.get_by_text(f"{IN_LIST} 多 LONG · 数量 {qty}").first).to_be_visible()
    page.get_by_role("button", name="平掉此仓").click()
    page.get_by_role("button", name="确认平掉此仓").click()
    expect(page.get_by_text("已提交平仓")).to_be_visible()
    wait_for(lambda: not [p for p in positions_by(deployment, symbol=IN_LIST) if p.get("status") in ("managed", "closing")], "接管仓清零")
    page.get_by_role("button", name="关闭", exact=True).click()
    shot("m3-14-taken-over-closed")


def test_out_of_list_position_goes_pending_then_ignore(deployment, page, shot):
    manual_open(page, OUT_OF_LIST, "0.05")
    page.get_by_role("link", name="持仓", exact=True).click()
    expect(page.get_by_text("待确认仓（1）")).to_be_visible(timeout=TAKEOVER_SECONDS * 1000)
    expect(page.get_by_text("不在接管清单").first).to_be_visible()
    wait_for(lambda: external_by(deployment, symbol=OUT_OF_LIST, status="pending"), "内核里是待确认")
    shot("m3-15-pending")
    page.get_by_role("button", name="忽略", exact=True).click()
    page.get_by_role("button", name="确认忽略").click()
    expect(page.get_by_text("外部仓（已忽略）（1）")).to_be_visible()
    wait_for(lambda: external_by(deployment, symbol=OUT_OF_LIST, status="ignored"), "内核里是外部（已忽略）")
    shot("m3-16-ignored")
