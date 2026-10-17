"""统计页：顶部三个大数字依次为盈亏比、盈利率、胜率（顺序固定），下面是其它指标；按模型、归属、方向、合约分组的表；混合仓单列。
统计都在内核算（净盈亏已扣手续费和资金费），页面只格式化。
"""
from __future__ import annotations

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console import tradeviews as T
from dmr_console.app import TITLE, ConsoleUi
from dmr_console.components.gridslot import GridSlot
from dmr_console.components.grids import stats_group_grid_options
from dmr_console.pages.shell import Shell, enter

PARTS = ("state",)
DETAIL_KEYS = tuple(key for key, _ in T.DETAIL_LABELS)


def register(console: ConsoleUi) -> None:
    @ui.page("/stats", title=f"统计 · {TITLE}")
    def stats_page():
        shell = enter(console, page="stats", parts=PARTS)
        if isinstance(shell, RedirectResponse):
            return shell
        build(shell)
        shell.start()


def _detail_block(prefix: str) -> dict[str, ui.label]:
    values: dict[str, ui.label] = {}
    with ui.element("div").classes("dmr-detail-grid"):
        for key, label in T.DETAIL_LABELS:
            with ui.column().classes("dmr-detail"):
                ui.label(label).classes("dmr-hint")
                values[key] = ui.label("加载中…").classes("dmr-detail-value").mark(f"{prefix}-{key}")
    return values


def build(shell: Shell) -> None:
    controller = shell.controller
    config = shell.console.config
    rendered = {"stats": -1}
    group_slots: dict[str, GridSlot] = {}

    async def query() -> None:
        params, problem = T.trade_query(symbol="", ownership=None, model=None, side=None, since=since.value, until=until.value,
                                        tz=shell.console.tz)
        filter_error.set_text(problem)
        if problem:
            return
        await controller.refresh_stats({k: v for k, v in params.items() if k in ("since_ms", "until_ms")})
        await shell._tick()  # noqa: SLF001 - 同一个外壳，拉完立刻重画

    async def refresh() -> None:
        await controller.refresh(PARTS)
        await controller.refresh_stats()
        await shell._tick()  # noqa: SLF001

    async def poll() -> None:
        await controller.refresh_stats_if_due(max_age_s=config.poll_interval_s * 0.8)

    with shell.body:
        with ui.row().classes("items-center gap-3"):
            ui.label("统计").classes("dmr-h2").mark("stats-title")
            shell.utility_button("刷新", refresh, "btn-refresh")
        with ui.row().classes("items-end gap-2"):
            since = ui.input("开始日期（年-月-日）").props("dense clearable").mark("flt-since")
            until = ui.input("结束日期（年-月-日）").props("dense clearable").mark("flt-until")
            shell.utility_button("查询", query, "btn-stats-query")
        filter_error = ui.label("").classes("dmr-reason").mark("stats-filter-error")
        error = ui.label("").classes("dmr-part-errors").mark("stats-error")
        ui.label("净盈亏已扣除手续费和资金费；盈亏比 = 盈利交易平均净盈亏 ÷ |亏损交易平均净盈亏|；盈利率 = 净盈亏合计 ÷ 开仓名义金额合计。").classes(
            "dmr-hint")
        big_values: list[ui.label] = []
        big_notes: list[ui.label] = []
        with ui.element("div").classes("dmr-big-row").mark("stats-headline"):
            for number, key in enumerate(T.HEADLINE_KEYS, start=1):  # 顺序：盈亏比、盈利率、胜率
                with ui.column().classes("dmr-big"):
                    ui.label(T.HEADLINE_LABELS[key]).classes("dmr-big-label").mark(f"stat-big-label-{number}")
                    big_values.append(ui.label("加载中…").classes("dmr-big-value").mark(f"stat-big-{number}"))
                    big_notes.append(ui.label("").classes("dmr-hint").mark(f"stat-big-note-{number}"))
        details = _detail_block("stat-detail")
        for group_by in T.GROUP_BY_KEYS:
            ui.label(T.GROUP_TITLES[group_by]).classes("dmr-h2").mark(f"stats-group-title-{group_by}")
            header = T.GROUP_TITLES[group_by].removeprefix("按")
            group_slots[group_by] = GridSlot(f"stats-grid-{group_by}", f"stats-empty-{group_by}",
                                             lambda rows, header=header: stats_group_grid_options(header, rows), empty_text="（无）")
        ui.label("混合仓（单列，不并入上面的任何分组）").classes("dmr-h2").mark("stats-mixed-title")
        mixed_headline = ui.label("").classes("dmr-hint").mark("stat-mixed-headline")
        mixed = _detail_block("stat-mixed")

    def update() -> None:
        error.set_text(f"统计：{controller.errors['stats']}" if "stats" in controller.errors else "")
        if rendered["stats"] == controller.versions["stats"]:
            return
        rendered["stats"] = controller.versions["stats"]
        data = controller.stats if isinstance(controller.stats, dict) else {}
        loaded = controller.loaded("stats")
        for label, note, item in zip(big_values, big_notes, T.stats_headline(data.get("overall") if loaded else None)):
            label.set_text(item["value"] if loaded else "加载中…")
            note.set_text(item["note"] if loaded else "")
        for target, block in ((details, data.get("overall")), (mixed, data.get("mixed"))):
            for item in T.stats_details(block):
                target[item["key"]].set_text(item["value"])
        mixed_headline.set_text(" · ".join(f"{h['label']} {h['value']}" for h in T.stats_headline(data.get("mixed"))) if loaded else "")
        groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
        for group_by, slot in group_slots.items():
            slot.show(T.stats_group_rows(groups.get(group_by), group_by), loaded=loaded)

    shell.add_poller(poll)
    shell.add_updater(update)
    ui.timer(0.01, lambda: controller.refresh_stats({}), once=True)
