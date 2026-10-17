"""逐笔交易页：一笔 = 仓位从零变非零到再次归零。表格（AG Grid，可排序）列出开平时间（本地时间）、合约、方向、归属、数量、开平均价、
毛盈亏、手续费、资金费、净盈亏、入场原因、出场原因；可按合约、归属、模型、方向、时间范围筛选（筛选在内核做，页面只发参数）。
"""
from __future__ import annotations

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console import labels as L
from dmr_console import tradeviews as T
from dmr_console.app import TITLE, ConsoleUi
from dmr_console.components.gridslot import GridSlot
from dmr_console.components.grids import trade_grid_options
from dmr_console.pages.shell import Shell, enter

PARTS = ("state",)
ALL = {"": "全部"}
OWNERSHIP_OPTIONS = {**ALL, "strategy": "策略", "takeover": "接管", "ai": "AI"}
MODEL_OPTIONS = {**ALL, **L.TRADE_MODEL_LABELS}
SIDE_OPTIONS = {**ALL, "LONG": "多 LONG", "SHORT": "空 SHORT"}


def register(console: ConsoleUi) -> None:
    @ui.page("/trades", title=f"逐笔交易 · {TITLE}")
    def trades_page():
        shell = enter(console, page="trades", parts=PARTS)
        if isinstance(shell, RedirectResponse):
            return shell
        build(shell)
        shell.start()


def build(shell: Shell) -> None:
    controller = shell.controller
    tz = shell.console.tz
    config = shell.console.config
    rendered = {"trades": -1}

    async def query() -> None:
        params, problem = T.trade_query(symbol=symbol.value, ownership=ownership.value, model=model.value, side=side.value,
                                        since=since.value, until=until.value, tz=tz)
        filter_error.set_text(problem)
        if problem:
            return
        await controller.refresh_trades(params)
        await shell._tick()  # noqa: SLF001 - 同一个外壳，拉完立刻重画

    async def refresh() -> None:
        await controller.refresh(PARTS)
        await controller.refresh_trades()
        await shell._tick()  # noqa: SLF001

    async def poll() -> None:
        await controller.refresh_trades_if_due(max_age_s=config.poll_interval_s * 0.8)

    with shell.body:
        with ui.row().classes("items-center gap-3"):
            ui.label("逐笔交易").classes("dmr-h2").mark("trades-title")
            shell.utility_button("刷新", refresh, "btn-refresh")
            count = ui.label("").classes("dmr-hint").mark("trades-count")
        with ui.row().classes("items-end gap-2").mark("trades-filters"):
            symbol = ui.input("合约").props("dense clearable").mark("flt-symbol")
            ownership = ui.select(OWNERSHIP_OPTIONS, value="", label="归属").props("dense").mark("flt-ownership")
            model = ui.select(MODEL_OPTIONS, value="", label="模型").props("dense").mark("flt-model")
            side = ui.select(SIDE_OPTIONS, value="", label="方向").props("dense").mark("flt-side")
            since = ui.input("开始日期（年-月-日）").props("dense clearable").mark("flt-since")
            until = ui.input("结束日期（年-月-日）").props("dense clearable").mark("flt-until")
            shell.utility_button("查询", query, "btn-trades-query")
        filter_error = ui.label("").classes("dmr-reason").mark("trades-filter-error")
        error = ui.label("").classes("dmr-part-errors").mark("trades-error")
        slot = GridSlot("trades-grid", "trades-empty", trade_grid_options, empty_text="（暂无逐笔交易）")

    def update() -> None:
        error.set_text(f"逐笔交易：{controller.errors['trades']}" if "trades" in controller.errors else "")
        if rendered["trades"] == controller.versions["trades"]:
            return
        rendered["trades"] = controller.versions["trades"]
        rows = T.trade_rows(controller.trades, tz=tz)
        total = controller.trades.get("total") if isinstance(controller.trades, dict) else None
        if isinstance(total, int) and not isinstance(total, bool) and total > len(rows):
            count.set_text(f"共 {total} 笔，显示 {len(rows)} 笔")
        else:
            count.set_text(f"共 {len(rows)} 笔" if controller.loaded("trades") else "")
        slot.show(rows, loaded=controller.loaded("trades"))

    shell.add_poller(poll)
    shell.add_updater(update)
    ui.timer(0.01, lambda: controller.refresh_trades({}), once=True)  # 进页面从「无筛选」开始（上次的筛选不能悄悄留着）
