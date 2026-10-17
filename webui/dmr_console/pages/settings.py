"""设置页（M5 配置页的基础版）：``GET /config`` 的全部项按分组列出，可编辑项逐项保存（``PUT /config/{key}``，需要操作席位）。

每项显示当前值、默认值、取值范围、单位、生效方式、是否影响已有持仓。模型 3 的项只读并注明「改档需要部署级开关」。
这里只检查输入的形状（整数、数字、合约代码拆分）；范围、冲突与开关一律由内核校验，内核给的中文原因原样显示。
"""
from __future__ import annotations

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console import configview as C
from dmr_console import labels as L
from dmr_console.app import TITLE, ConsoleUi
from dmr_console.fmt import DASH
from dmr_console.pages.shell import Shell, enter
from dmr_console.state import NO_STATE_REASON, READ_ONLY_REASON, SEAT_REASON

PARTS = ("state",)
RETRY_MS = 5_000


def register(console: ConsoleUi) -> None:
    @ui.page("/settings", title=f"设置 · {TITLE}")
    def settings_page():
        shell = enter(console, page="settings", parts=PARTS)
        if isinstance(shell, RedirectResponse):
            return shell
        build(shell)
        shell.start()


def _meta(row: C.ConfigRow) -> str:
    parts = [f"默认 {row.default_text}"]
    if row.range_text != DASH:
        parts.append(f"范围 {row.range_text}")
    if row.unit:
        parts.append(f"单位 {row.unit}")
    parts.append(f"生效：{row.apply_text}")
    parts.append(row.affects_text)
    if row.required_for_live:
        parts.append("实盘必须配置")
    return " · ".join(parts)


def build(shell: Shell) -> None:
    controller = shell.controller
    actions = shell.actions
    state = {"loading": False, "loaded": False, "retry_ms": 0, "switch": False}
    saving: set[str] = set()
    buttons: list = []

    with shell.body:
        with ui.row().classes("items-center gap-3"):
            ui.label("设置").classes("dmr-h2").mark("settings-title")
            shell.utility_button("刷新", lambda: load(), "btn-settings-refresh")
        ui.label("改动需要操作席位；每项的「生效」列说明何时生效、是否影响已有持仓。范围与冲突由内核校验。").classes("dmr-hint")
        reason = ui.label("").classes("dmr-reason").mark("settings-reason")
        error = ui.label("").classes("dmr-reason").mark("settings-error")
        container = ui.column().classes("w-full gap-3")

    def can_edit_reason() -> str:
        data = controller.state
        if data is None:
            return NO_STATE_REASON
        if not controller.operations_enabled:
            return READ_ONLY_REASON
        seat = data.get("seat") if isinstance(data, dict) else None
        return "" if isinstance(seat, dict) and seat.get("mine") is True else SEAT_REASON

    def build_m3_section(group: C.ConfigGroup, switch: bool) -> None:
        """模型 3 分区：AI 档与执行档两组档位（默认全关）；部署级开关关着时只读并注明原因。"""
        by_key = {row.key: row for row in group.rows}
        with ui.column().classes("w-full gap-2").mark("m3-section"):
            for prefix, key, title, labels in (("ai", "m3_ai_tier", "AI 档", L.M3_AI_TIER_LABELS),
                                              ("exec", "m3_exec_tier", "执行档", L.M3_EXEC_TIER_LABELS)):
                row = by_key.get(key)
                if row is None:
                    continue
                choices = row.choices or tuple(labels)
                with ui.column().classes("w-full gap-1"):
                    ui.label(title).classes("dmr-rowop-name").mark(f"m3-{prefix}-title")
                    ui.label(f"当前：{L.label_of(labels, row.value)}").classes("dmr-current").mark(f"m3-{prefix}-current")
                    with ui.row().classes("items-center gap-2"):
                        for choice in choices:
                            current = " m3-opt-current" if choice == row.value else ""
                            ui.label(L.label_of(labels, choice)).classes(f"m3-opt{current}").mark(f"m3-{prefix}-opt-{choice}")
            if not switch:
                ui.label(C.READONLY_NOTE + "（默认全关，本期模型 3 只预留，不能启动）").classes("dmr-hint").mark("m3-readonly-note")

    def build_row(row: C.ConfigRow) -> None:
        key = row.key
        with ui.column().classes("w-full gap-0").mark(f"cfg-row-{key}"):
            with ui.row().classes("items-center gap-2"):
                ui.label(row.label).classes("dmr-rowop-name")
                ui.label(key).classes("dmr-hint")
            current = ui.label(row.value_text).classes("dmr-current").mark(f"cfg-current-{key}")
            ui.label(_meta(row)).classes("dmr-hint").mark(f"cfg-meta-{key}")
            if row.note:
                ui.label(row.note).classes("dmr-hint")
            message = ui.label("").classes("dmr-reason").mark(f"cfg-msg-{key}")
            if not row.editable:
                ui.label(row.readonly_note).classes("dmr-hint").mark(f"cfg-readonly-{key}")
                return
            with ui.row().classes("items-center gap-2"):
                if row.kind == "bool":
                    field = ui.switch(value=bool(row.value))
                elif row.kind == "choice":
                    field = ui.select(list(row.choices), value=row.value if row.value in row.choices else None).props("dense")
                else:
                    field = ui.input(value=row.input_text).props("dense")
                field.mark(f"cfg-input-{key}")

                async def save(row=row, field=field, current=current, message=message) -> None:
                    if row.key in saving:
                        return
                    value, problem = C.parse_input(row, field.value)
                    if problem:
                        message.set_text(f"{row.label}：{problem}")
                        return
                    saving.add(row.key)
                    try:
                        outcome = await actions.set_config(row.key, value)
                    finally:
                        saving.discard(row.key)
                    if outcome.ok:
                        current.set_text(C.format_value(outcome.result.get("new", value)))
                        applies = outcome.result.get("applies")
                        message.set_text("已保存" + (f"：{applies}" if isinstance(applies, str) and applies else ""))
                    else:
                        message.set_text(outcome.text)

                buttons.append(shell.utility_button("保存", save, f"btn-save-{key}",
                                                     extra_enabled=lambda: can_edit_reason() == ""))

    async def load() -> None:
        if state["loading"]:
            return
        state["loading"] = True
        try:
            items, problem = await actions.fetch_config()
        finally:
            state["loading"] = False
        error.set_text(f"配置读取失败：{problem}" if items is None else "")
        if items is None:
            state["retry_ms"] = shell.console.clock.now_ms() + RETRY_MS  # 失败后隔几秒再试，不每秒敲内核
            return
        shell.forget_buttons(buttons)
        buttons.clear()
        container.clear()
        switch = C.deploy_switch_on(controller.state)
        state["switch"] = switch
        with container:
            for index, group in enumerate(C.config_groups(items, deploy_switch=switch)):
                ui.label(f"{group.name}（{len(group.rows)}）").classes("dmr-h2").mark(f"cfg-group-title-{index}")
                if group.name in C.READONLY_GROUPS:
                    build_m3_section(group, switch)
                for row in group.rows:
                    build_row(row)
        state["loaded"] = True

    def update() -> None:
        reason.set_text(can_edit_reason())
        if state["loaded"] and C.deploy_switch_on(controller.state) != state["switch"]:
            state["loaded"] = False  # 部署级开关变了：按新的可改状态重画
        if (not state["loaded"] and not state["loading"] and not controller.link.status.lost
                and shell.console.clock.now_ms() >= state["retry_ms"]):
            import asyncio
            asyncio.ensure_future(load())

    shell.add_updater(update)
