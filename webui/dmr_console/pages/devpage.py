"""替身开发工具页（只在 env=standin 显示）：测试造仓、模拟手动下单、故障开关、价格覆盖。

- 测试造仓走内核的 ``/dev/test_open``（要席位）；其余直接调替身交易所（令牌 ``var/standin/manual.token``，地址 ``DMR_STANDIN_URL``）。
- 页面始终醒目标注「替身环境 · 非真实交易」；环境不是替身时只显示一句说明，不放任何工具。
- 每个操作在控制台日志留一行，页面下方也列出最近的操作。
"""
from __future__ import annotations

import logging
from collections import deque

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console import operations as O
from dmr_console.app import TITLE, ConsoleUi
from dmr_console.devtools import (ALGO_KINDS, DIRECTIONS, FAULT_KEYS, FAULT_LABELS, ORDER_ACTIONS, StandinError)
from dmr_console.pages.shell import Shell, enter
from dmr_console.state import STANDIN_BANNER

log = logging.getLogger(__name__)

PARTS = ("state",)
LOG_LINES = 30
DEFAULT_SYMBOL = "BTCUSDT"
UNAVAILABLE = "开发工具只在替身环境可用"
NO_DEV = "没有配置替身交易所客户端"


def register(console: ConsoleUi) -> None:
    @ui.page("/dev", title=f"替身开发 · {TITLE}")
    def dev_page():
        shell = enter(console, page="dev", parts=PARTS)
        if isinstance(shell, RedirectResponse):
            return shell
        build(shell)
        shell.start()


def build(shell: Shell) -> None:
    controller = shell.controller
    console = shell.console
    lines: deque[str] = deque(maxlen=LOG_LINES)
    state = {"direction": "LONG", "faults_loaded": False}
    inject = {"short_cross": "up", "long_trend": "bullish"}
    switches: dict[str, ui.switch] = {}

    with shell.body:
        ui.label(STANDIN_BANNER).classes("dmr-standin-tag").mark("dev-banner")
        unavailable = ui.label(UNAVAILABLE).classes("dmr-empty").mark("dev-unavailable")
        content = ui.column().classes("w-full gap-3").mark("dev-content")
        content.set_visibility(False)

    def note(text: str) -> None:
        log.info("开发工具 %s", text)
        lines.appendleft(text)
        log_label.set_text("\n".join(lines))

    async def dev_call(what: str, call) -> dict | None:
        if console.dev is None:
            note(f"{what} 失败：{NO_DEV}")
            return None
        try:
            result = await call(console.dev)
        except StandinError as exc:
            note(f"{what} 失败：{exc}")
            return None
        except ValueError as exc:
            note(f"{what} 参数不合法：{exc}")
            return None
        note(f"{what} 成功")
        return result

    with content:
        with ui.card().props("flat bordered").classes("w-full"):
            ui.label("测试造仓（走内核 /dev/test_open，需要操作席位）").classes("dmr-h2")
            symbol = ui.input("合约", value=DEFAULT_SYMBOL).props("dense").mark("dev-symbol")
            notional = ui.input("名义金额（USDT）", value="100").props("dense").mark("dev-notional")
            with ui.row():
                for side in DIRECTIONS:
                    async def test_open(side=side) -> None:
                        outcome = await controller.actions.call(
                            "dev.test_open", "/dev/test_open", lambda op: O.test_open_payload(op, symbol.value.strip(), side, notional.value.strip()))
                        note(f"测试造仓 {symbol.value.strip()} {side} {notional.value.strip()}："
                             + (O.intent_result_text(outcome.status, outcome.result) if outcome.ok else outcome.text))

                    shell.utility_button(f"测试造仓 · {DIRECTIONS[side]}", test_open, f"dev-test-open-{side}")

        with ui.card().props("flat bordered").classes("w-full"):
            ui.label("信号注入（走内核 /dev/inject_signal，需要操作席位）").classes("dmr-h2")
            ui.label("构造一次当根已收盘 K 线的信号，走与自然信号完全相同的预检与提交路径；不改任何 K 线数据。").classes("dmr-hint")
            i_symbol = ui.input("合约", value=DEFAULT_SYMBOL).props("dense").mark("dev-inject-symbol")
            ui.label("短周期").classes("dmr-hint")
            cross_label = ui.label("").mark("dev-inject-cross-label")
            with ui.row():
                for cross, name in O.SHORT_CROSS_LABELS.items():
                    ui.button(name, on_click=lambda c=cross: set_inject("short_cross", c)).props("dense no-caps flat").mark(f"dev-inject-cross-{cross}")
            ui.label("长周期趋势").classes("dmr-hint")
            trend_label = ui.label("").mark("dev-inject-trend-label")
            with ui.row():
                for trend, name in O.LONG_TREND_LABELS.items():
                    ui.button(name, on_click=lambda t=trend: set_inject("long_trend", t)).props("dense no-caps flat").mark(f"dev-inject-trend-{trend}")

            def set_inject(key: str, value: str) -> None:
                inject[key] = value
                cross_label.set_text(f"短周期：{O.SHORT_CROSS_LABELS[inject['short_cross']]}")
                trend_label.set_text(f"长周期趋势：{O.LONG_TREND_LABELS[inject['long_trend']]}")

            set_inject("short_cross", "up")

            async def inject_signal() -> None:
                name = i_symbol.value.strip()
                cross, trend = inject["short_cross"], inject["long_trend"]
                outcome = await controller.actions.call(
                    "dev.inject_signal", "/dev/inject_signal", lambda op: O.inject_signal_payload(op, name, cross, trend))
                note(f"信号注入 {name} 短周期{O.SHORT_CROSS_LABELS[cross]} 长周期{O.LONG_TREND_LABELS[trend]}："
                     + (O.intent_result_text(outcome.status, outcome.result) if outcome.ok else outcome.text))

            def has_seat() -> bool:
                seat = controller.state.get("seat") if isinstance(controller.state, dict) else None
                return isinstance(seat, dict) and seat.get("mine") is True

            shell.utility_button("注入信号", inject_signal, "dev-inject-submit", extra_enabled=has_seat)

        with ui.card().props("flat bordered").classes("w-full"):
            ui.label("模拟手动下单（直接调替身交易所，绕过内核）").classes("dmr-h2")
            m_symbol = ui.input("合约", value=DEFAULT_SYMBOL).props("dense").mark("dev-m-symbol")
            qty = ui.input("数量", value="0.01").props("dense").mark("dev-m-qty")
            trigger = ui.input("触发价（止损 / 止盈）").props("dense").mark("dev-m-trigger")
            direction_label = ui.label("").mark("dev-dir-label")

            def set_direction(direction: str) -> None:
                state["direction"] = direction
                direction_label.set_text(f"持仓方向：{DIRECTIONS[direction]}")

            set_direction("LONG")
            with ui.row():
                for direction, name in DIRECTIONS.items():
                    ui.button(name, on_click=lambda d=direction: set_direction(d)).props("dense no-caps flat").mark(f"dev-dir-{direction}")
            with ui.row():
                for action, name in ORDER_ACTIONS.items():
                    async def order(action=action, name=name) -> None:
                        d = state["direction"]
                        await dev_call(f"手动{name} {m_symbol.value.strip()} {d} 数量 {qty.value.strip()}",
                                       lambda dev: dev.manual_order(action, m_symbol.value.strip(), d, qty.value.strip()))

                    ui.button(name, on_click=order).props("no-caps").mark(f"dev-order-{action}")
                for kind, name in ALGO_KINDS.items():
                    async def algo(kind=kind, name=name) -> None:
                        d = state["direction"]
                        await dev_call(f"手动挂{name} {m_symbol.value.strip()} {d} 触发价 {trigger.value.strip()}",
                                       lambda dev: dev.manual_algo(kind, m_symbol.value.strip(), d, trigger.value.strip()))

                    ui.button(f"挂{name}", on_click=algo).props("no-caps").mark(f"dev-algo-{kind}")

        with ui.card().props("flat bordered").classes("w-full"):
            ui.label("故障开关（替身 /dev/faults）").classes("dmr-h2")

            def show_faults(data: dict | None) -> None:
                faults = data.get("faults") if isinstance(data, dict) else None
                if not isinstance(faults, dict):
                    return
                for key, switch in switches.items():
                    cfg = faults.get(key)
                    if isinstance(cfg, dict):
                        switch.set_value(cfg.get("enabled") is True)

            async def refresh_faults() -> None:
                show_faults(await dev_call("读取故障开关", lambda dev: dev.faults()))

            async def reset_faults() -> None:
                show_faults(await dev_call("故障开关全部复位", lambda dev: dev.reset_faults()))

            for key in FAULT_KEYS:
                async def toggle(event, key=key) -> None:
                    if state.get("syncing"):
                        return
                    await dev_call(f"故障开关 {FAULT_LABELS[key]} {'打开' if event.value else '关闭'}", lambda dev: dev.set_fault(key, bool(event.value)))

                switches[key] = ui.switch(FAULT_LABELS[key], on_change=toggle).mark(f"dev-fault-{key}")
            with ui.row():
                ui.button("读取当前开关", on_click=refresh_faults).props("dense no-caps flat").mark("dev-faults-refresh")
                ui.button("全部复位", on_click=reset_faults).props("dense no-caps flat").mark("dev-faults-reset")

        with ui.card().props("flat bordered").classes("w-full"):
            ui.label("价格覆盖（替身 /dev/price_override）").classes("dmr-h2")
            p_symbol = ui.input("合约", value=DEFAULT_SYMBOL).props("dense").mark("dev-p-symbol")
            p_price = ui.input("价格").props("dense").mark("dev-p-price")
            p_pct = ui.input("或涨跌百分比（如 -3）").props("dense").mark("dev-p-pct")

            async def set_price() -> None:
                await dev_call(f"价格覆盖 {p_symbol.value.strip()} 价格 {p_price.value.strip()}",
                               lambda dev: dev.price_override(p_symbol.value.strip(), price=p_price.value.strip()))

            async def set_pct() -> None:
                await dev_call(f"价格覆盖 {p_symbol.value.strip()} 涨跌 {p_pct.value.strip()}%",
                               lambda dev: dev.price_override(p_symbol.value.strip(), pct=p_pct.value.strip()))

            async def clear_price() -> None:
                await dev_call(f"清除价格覆盖 {p_symbol.value.strip()}", lambda dev: dev.price_override(p_symbol.value.strip(), clear=True))

            with ui.row():
                ui.button("按价格覆盖", on_click=set_price).props("no-caps").mark("dev-price-set")
                ui.button("按涨跌覆盖", on_click=set_pct).props("no-caps").mark("dev-price-pct")
                ui.button("清除覆盖", on_click=clear_price).props("no-caps flat").mark("dev-price-clear")

        ui.label("最近的开发操作").classes("dmr-h2")
        log_label = ui.label("").style("white-space: pre-line").classes("dmr-hint").mark("dev-log")

    def update() -> None:
        standin = controller.env_hint == "standin"
        content.set_visibility(standin)
        unavailable.set_visibility(not standin)
        if standin and not state["faults_loaded"]:
            state["faults_loaded"] = True
            ui.timer(0.01, refresh_faults, once=True)

    shell.add_updater(update)
