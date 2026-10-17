"""选币名单页：当日成员（合约、方向、分数、首次入选时间、是否已持仓、被避让的原因）、名单是否有效与有效期、最近拉取时间、被拒收的成员与原因。

数据只来自内核的 ``GET /watchlist``（内核自己拉取名单并做校验；避让原因由内核按模型 1 的预检逻辑算出，这里照显示，不另算）。
内核没有这个接口或暂时读不到时，页面上写明原因，外壳照常工作。
"""
from __future__ import annotations

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console.app import TITLE, ConsoleUi
from dmr_console.components.gridslot import GridSlot
from dmr_console.components.grids import rejected_grid_options, watchlist_grid_options
from dmr_console.pages.shell import Shell, enter

PARTS = ("state",)


def register(console: ConsoleUi) -> None:
    @ui.page("/watchlist", title=f"选币名单 · {TITLE}")
    def watchlist_page():
        shell = enter(console, page="watchlist", parts=PARTS)
        if isinstance(shell, RedirectResponse):
            return shell
        build(shell)
        shell.start()


def build(shell: Shell) -> None:
    controller = shell.controller
    config = shell.console.config
    rendered = {"watchlist": -1}

    async def refresh() -> None:
        await controller.refresh(PARTS)
        await controller.refresh_watchlist()
        await shell._tick()  # noqa: SLF001 - 同一个外壳，拉完立刻重画

    async def poll() -> None:
        await controller.refresh_watchlist_if_due(max_age_s=config.poll_interval_s * 0.8)

    with shell.body:
        with ui.row().classes("items-center gap-3"):
            ui.label("选币名单").classes("dmr-h2")
            shell.utility_button("刷新", refresh, "btn-refresh")
        error = ui.label("").classes("dmr-part-errors").mark("watchlist-error")
        with ui.column().classes("gap-0"):
            status = ui.label("").mark("watchlist-status")
            count = ui.label("").classes("dmr-hint").mark("watchlist-count")
            valid_until = ui.label("").classes("dmr-hint").mark("watchlist-valid-until")
            date = ui.label("").classes("dmr-hint").mark("watchlist-date")
            fetched = ui.label("").classes("dmr-hint").mark("watchlist-fetched")
        members_slot = GridSlot("watchlist-grid", "watchlist-empty", watchlist_grid_options, empty_text="（名单里没有成员）")
        ui.label("被拒收的成员").classes("dmr-h2")
        rejected_slot = GridSlot("rejected-grid", "rejected-empty", rejected_grid_options, empty_text="（无）")

    def update() -> None:
        summary = controller.watchlist_summary()
        status.set_text(summary["status"])
        status.classes(replace={"ok": "dmr-pos", "warn": "dmr-warn-text"}.get(summary["tone"], ""))
        count.set_text(summary["count"])
        valid_until.set_text(f"有效期至：{summary['valid_until']}")
        date.set_text(f"业务日：{summary['date']}")
        fetched.set_text(f"最近拉取：{summary['fetched']}")
        error.set_text(f"名单：{controller.errors['watchlist']}" if "watchlist" in controller.errors else "")
        if rendered["watchlist"] != controller.versions["watchlist"]:
            rendered["watchlist"] = controller.versions["watchlist"]
            loaded = controller.loaded("watchlist")
            members_slot.show(controller.watchlist_rows(), loaded=loaded)
            rejected_slot.show(controller.rejected_rows(), loaded=loaded)

    shell.add_poller(poll)
    shell.add_updater(update)
    ui.timer(0.01, controller.refresh_watchlist, once=True)
