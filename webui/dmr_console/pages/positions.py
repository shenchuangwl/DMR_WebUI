"""持仓页：按归属分组（策略仓、混合仓、接管仓、AI 仓、其他），待确认与外部（已忽略）的仓单列。

每组一张表；没有仓的组只显示一行「（无）」。数据没取到之前显示「加载中…」，取到之后才显示真实的空。
托管中的持仓每行有「部分平仓」「平掉此仓」；待确认仓有「接管」「忽略」（来源待核对的还有「恢复托管」），
接管仓与混合仓有「取消托管」，外部保护的接管仓有「交给系统管理止损」。置灰只是提示，是否允许以内核为准。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console.app import TITLE, ConsoleUi
from dmr_console.components.gridslot import GridSlot
from dmr_console.components.grids import position_grid_options
from dmr_console.pages.shell import Shell, enter
from dmr_console import labels as L
from dmr_console.components.opdialogs import OpDialogs
from dmr_console.state import position_action_states, row_action_states
from dmr_console import operations as O
from dmr_console.views import GROUP_HINTS, GROUP_TITLES, POSITION_GROUP_KEYS

PARTS = ("state", "positions", "external")


@dataclass
class _GroupView:
    title: ui.label
    slot: GridSlot


def register(console: ConsoleUi) -> None:
    @ui.page("/positions", title=f"持仓 · {TITLE}")
    def positions_page():
        shell = enter(console, page="positions", parts=PARTS)
        if isinstance(shell, RedirectResponse):
            return shell
        build(shell)
        shell.start()


def build(shell: Shell) -> None:
    controller = shell.controller
    dialogs = OpDialogs(shell)
    row_buttons: dict[str, list[ui.button]] = {"partial_close": [], "close_one": []}
    rows_rendered = {"positions": -1}
    takeover_rendered = {"positions": -1, "external": -1}
    takeover_buttons: list[tuple[str, ui.button]] = []
    reason_labels: dict = {}
    views: dict[str, _GroupView] = {}
    rendered = {"positions": -1, "external": -1}

    with shell.body:
        with ui.row().classes("items-center gap-3"):
            ui.label("持仓").classes("dmr-h2")
            shell.utility_button("刷新", shell.refresh_now, "btn-refresh")
        with ui.column().classes("w-full gap-1").mark("rowops"):
            ui.label("持仓操作").classes("dmr-h2")
            rowops_reason = ui.label("").classes("dmr-reason").mark("rowops-reason")
            rowops = ui.column().classes("w-full gap-1")
        with ui.column().classes("w-full gap-1").mark("takeoverops"):
            ui.label("待确认与接管仓操作").classes("dmr-h2")
            ui.label("接管、忽略、恢复托管、取消托管、交给系统管理止损；置灰时旁边写明原因，最终由内核判定。").classes("dmr-hint")
            takeover_rows = ui.column().classes("w-full gap-1")
        for key in POSITION_GROUP_KEYS:
            with ui.column().classes("w-full gap-1").mark(f"group-{key}"):
                title = ui.label(GROUP_TITLES[key]).classes("dmr-h2").mark(f"group-title-{key}")
                ui.label(GROUP_HINTS[key]).classes("dmr-hint")
                slot = GridSlot(f"grid-{key}", f"empty-{key}", lambda rows, key=key: position_grid_options(key, rows))
                views[key] = _GroupView(title, slot)

    def rebuild_rows() -> None:
        rowops.clear()
        row_buttons["partial_close"], row_buttons["close_one"] = [], []
        managed = [p for p in (controller.positions or []) if isinstance(p, dict) and p.get("status") == "managed"]
        with rowops:
            if not managed:
                ui.label("（没有托管持仓）" if controller.loaded("positions") else "加载中…").classes("dmr-empty").mark("rowops-empty")
            for pos in managed:
                with ui.row().classes("items-center gap-2").mark(f"rowop-{pos.get('id')}"):
                    ui.label(f"{pos.get('symbol', '')} {L.label_of(L.SIDE_LABELS, pos.get('side'))} · 数量 {pos.get('qty', '')}").classes("dmr-rowop-name")
                    partial = ui.button("部分平仓", on_click=lambda pos=pos: dialogs.open_partial(pos)).props("dense no-caps").mark(
                        f"btn-partial-{pos.get('id')}")
                    one = ui.button("平掉此仓", on_click=lambda pos=pos: dialogs.open_close_one(pos)).props("dense no-caps color=negative").mark(
                        f"btn-closeone-{pos.get('id')}")
                    row_buttons["partial_close"].append(partial)
                    row_buttons["close_one"].append(one)

    def rebuild_takeover_rows() -> None:
        takeover_rows.clear()
        takeover_buttons.clear()
        reason_labels.clear()
        items = [p for p in list(controller.positions or []) + list(controller.external or []) if O.position_actions(p)]
        with takeover_rows:
            if not items:
                ui.label("（没有待确认或接管的持仓）" if controller.loaded("positions") and controller.loaded("external") else "加载中…").classes(
                    "dmr-empty").mark("takeoverops-empty")
            for pos in items:
                pid = pos.get("id")
                with ui.row().classes("items-center gap-2").mark(f"rowop-t-{pid}"):
                    reason = pos.get("pending_reason")
                    suffix = f" · {L.label_of(L.PENDING_REASON_LABELS, reason)}" if pos.get("status") == "pending" and reason else ""
                    ui.label(f"{pos.get('symbol', '')} {L.label_of(L.SIDE_LABELS, pos.get('side'))} · 数量 {pos.get('qty', '')}"
                             f" · {L.label_of(L.POSITION_STATUS_LABELS, pos.get('status'))}{suffix}").classes("dmr-rowop-name")
                    keys = O.position_actions(pos)
                    for key in keys:
                        marker = {"hand_stop_to_system": "handstop", "cancel_takeover": "canceltakeover"}.get(key, key)
                        button = ui.button(L.OPERATION_LABELS[key], on_click=lambda pos=pos, key=key: open_op(key, pos)).props(
                            "dense no-caps").mark(f"btn-{marker}-{pid}")
                        takeover_buttons.append((key, button))
                    reason_labels[pid] = (ui.label("").classes("dmr-reason").mark(f"takeover-reason-{pid}"), keys)

    def open_op(key: str, pos: dict) -> None:
        if key == "cancel_takeover":
            dialogs.open_cancel_takeover(pos)
        else:
            dialogs.open_position_op(key, pos)

    def update_row_states() -> None:
        tstates = position_action_states(controller.state, controller.link.status, operations_enabled=controller.operations_enabled)
        for key, button in takeover_buttons:
            button.enabled = tstates[key].enabled
        for label, keys in reason_labels.values():
            label.set_text("；".join(f"{tstates[k].label}：{tstates[k].reason}" for k in keys if not tstates[k].enabled))
        states = row_action_states(controller.state, controller.link.status, operations_enabled=controller.operations_enabled)
        for key, buttons in row_buttons.items():
            for button in buttons:
                button.enabled = states[key].enabled
        reasons = [f"{s.label}：{s.reason}" for s in states.values() if not s.enabled]
        rowops_reason.set_text("；".join(reasons))

    def update() -> None:
        if rows_rendered["positions"] != controller.versions["positions"]:
            rows_rendered["positions"] = controller.versions["positions"]
            rebuild_rows()
        if (takeover_rendered["positions"], takeover_rendered["external"]) != (controller.versions["positions"], controller.versions["external"]):
            takeover_rendered.update(positions=controller.versions["positions"], external=controller.versions["external"])
            rebuild_takeover_rows()
        update_row_states()
        if (rendered["positions"], rendered["external"]) == (controller.versions["positions"], controller.versions["external"]):
            return
        rendered.update(positions=controller.versions["positions"], external=controller.versions["external"])
        loaded = controller.loaded("positions") and controller.loaded("external")
        for group in controller.position_groups():
            view = views[group.key]
            view.title.set_text(f"{group.title}（{len(group.rows)}）" if loaded else group.title)
            view.slot.show(list(group.rows), loaded=loaded)

    shell.add_updater(update)
