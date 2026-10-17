"""操作日志页：每次操作的时间、操作人、结果（来自内核的审计日志，按编号倒序）；可翻更旧的一页。下方是告警。

「加载更多」和「刷新」一样不改变内核状态，但失联时同样禁用；翻到头也禁用。
"""
from __future__ import annotations

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console.app import TITLE, ConsoleUi
from dmr_console.components.gridslot import GridSlot
from dmr_console.components.grids import alert_grid_options, oplog_grid_options
from dmr_console.pages.shell import Shell, enter

PARTS = ("state", "oplog", "alerts")


def register(console: ConsoleUi) -> None:
    @ui.page("/oplog", title=f"操作日志 · {TITLE}")
    def oplog_page():
        shell = enter(console, page="oplog", parts=PARTS)
        if isinstance(shell, RedirectResponse):
            return shell
        build(shell)
        shell.start()


def build(shell: Shell) -> None:
    controller = shell.controller
    rendered = {"oplog": -1, "alerts": -1}

    async def load_more() -> None:
        await controller.load_more_oplog()
        await shell._tick()  # noqa: SLF001 - 同一个外壳，翻页后立刻重画

    with shell.body:
        with ui.row().classes("items-center gap-3"):
            ui.label("操作日志").classes("dmr-h2")
            shell.utility_button("刷新", shell.refresh_now, "btn-refresh")
            shell.utility_button("加载更多", load_more, "btn-load-more",
                                 extra_enabled=lambda: controller.loaded("oplog") and not controller.oplog_exhausted)
            count = ui.label("").classes("dmr-hint").mark("oplog-count")
        log_slot = GridSlot("oplog-grid", "oplog-empty", oplog_grid_options, empty_text="（暂无操作记录）")
        ui.label("告警").classes("dmr-h2")
        alerts_slot = GridSlot("alerts-grid", "alerts-empty", alert_grid_options, empty_text="（暂无告警）")

    def update() -> None:
        if rendered["oplog"] != controller.versions["oplog"]:
            rendered["oplog"] = controller.versions["oplog"]
            log_slot.show(controller.oplog_rows(), loaded=controller.loaded("oplog"))
        if controller.loaded("oplog"):
            count.set_text(f"已载入 {len(controller.oplog)} 条" + ("，已到最早一条" if controller.oplog_exhausted else ""))
        if rendered["alerts"] != controller.versions["alerts"]:
            rendered["alerts"] = controller.versions["alerts"]
            alerts_slot.show(controller.alert_rows(), loaded=controller.loaded("alerts"))

    shell.add_updater(update)
