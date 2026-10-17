"""总览页：常驻状态条（外壳里）、四个统一操作 + 模型切换（按 /state.allowed 与席位置灰，下方写明当前模型下的含义与禁用原因）、
「重新恢复」（内核恢复中才出现）、状态与可执行操作表、最近告警。置灰只是提示，内核强制。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console import labels as L
from dmr_console.app import TITLE, ConsoleUi
from dmr_console.components.gridslot import GridSlot
from dmr_console.components.grids import alert_grid_options
from dmr_console.pages.shell import Shell, enter
from dmr_console import operations as O
from dmr_console.components.opdialogs import OpDialogs
from dmr_console.state import OPERATION_ORDER, allowed_rows

PARTS = ("state", "alerts", "positions")  # 部分平仓的选仓弹窗要用持仓列表
RECENT_ALERTS = 8
ALLOWED_COLUMNS = [
    {"name": "label", "label": "操作", "field": "label", "align": "left"},
    {"name": "text", "label": "当前", "field": "text", "align": "left"},
    {"name": "reason", "label": "原因 / 说明", "field": "reason", "align": "left"},
]


@dataclass
class _OpView:
    button: ui.button
    meaning: ui.label
    reason: ui.label


def _allowed_table_rows(state: object) -> list[dict]:
    return [{"key": r["key"], "label": r["label"], "text": r["text"], "reason": " ".join(x for x in (r["reason"], r["note"]) if x) or "—"}
            for r in allowed_rows(state)]


def register(console: ConsoleUi) -> None:
    @ui.page("/", title=f"总览 · {TITLE}")
    def overview_page():
        shell = enter(console, page="overview", parts=PARTS)
        if isinstance(shell, RedirectResponse):
            return shell
        build(shell)
        shell.start()


def build(shell: Shell) -> None:
    controller = shell.controller
    dialogs = OpDialogs(shell)
    openers = {"start": dialogs.open_start, "stop": dialogs.open_stop, "partial_close": dialogs.open_pick_partial,
               "close_all": dialogs.open_close_all, "switch": dialogs.open_switch}
    ops: dict[str, _OpView] = {}
    rendered = {"state": -1, "alerts": -1}

    with shell.body:
        with ui.row().classes("items-center gap-3"):
            ui.label("统一操作").classes("dmr-h2")
            shell.utility_button("刷新", shell.refresh_now, "btn-refresh")
            recover = ui.button("重新恢复", on_click=lambda: do_recover()).props("no-caps color=warning").mark("btn-recover")
            recover.set_visibility(False)
            recover_message = ui.label("").classes("dmr-reason").mark("recover-message")
        with ui.row().classes("dmr-ops w-full"):
            for key in OPERATION_ORDER:
                with ui.card().classes("dmr-op").props("flat bordered"):
                    button = ui.button(L.OPERATION_LABELS[key], on_click=openers[key]).props("no-caps").mark(f"btn-{key}")
                    button.disable()
                    meaning = ui.label("").classes("dmr-meaning").mark(f"meaning-{key}")
                    reason = ui.label("").classes("dmr-reason").mark(f"reason-{key}")
                    ops[key] = _OpView(button, meaning, reason)

        ui.label("状态与可执行操作").classes("dmr-h2")
        allowed_table = ui.table(columns=ALLOWED_COLUMNS, rows=[], row_key="key").props("dense flat bordered").classes("w-full").mark(
            "allowed-table")
        ui.label("最近告警").classes("dmr-h2")
        alerts = GridSlot("alerts-grid", "alerts-empty", alert_grid_options, empty_text="（暂无告警）")

    async def do_recover() -> None:
        outcome = await controller.actions.call("recover", "/ops/recover", O.recover_payload)
        recover_message.set_text("已请求重新恢复" if outcome.ok else outcome.text)

    def update() -> None:
        show_recover = O.needs_recover(controller.state)
        recover.set_visibility(show_recover)
        recover.enabled = show_recover and not controller.link.status.lost and O.seat_actions(controller.state) == ("release",)
        for key, state in controller.buttons().items():
            view = ops[key]
            view.button.enabled = state.enabled
            view.meaning.set_text(state.meaning or "—")
            view.reason.set_text("" if state.enabled else state.reason)
        if rendered["state"] != controller.versions["state"]:
            rendered["state"] = controller.versions["state"]
            allowed_table.rows = _allowed_table_rows(controller.state)
            allowed_table.update()
        if rendered["alerts"] != controller.versions["alerts"]:
            rendered["alerts"] = controller.versions["alerts"]
            alerts.show(controller.alert_rows()[:RECENT_ALERTS], loaded=controller.loaded("alerts"))

    shell.add_updater(update)
