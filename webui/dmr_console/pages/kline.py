"""K 线页：选合约与周期（15m / 1h）；ECharts 蜡烛图叠加 DMR 副图、进出场标记、止损线。数据来自内核的 ``GET /candles``。"""
from __future__ import annotations

from fastapi.responses import RedirectResponse
from nicegui import ui

from dmr_console import tradeviews as T
from dmr_console.app import TITLE, ConsoleUi
from dmr_console.pages.shell import Shell, enter

PARTS = ("state",)
TIMEFRAMES = {"15m": "15 分钟", "1h": "1 小时"}
EMPTY_TEXT = "（还没有这个合约的已收盘 K 线）"


def register(console: ConsoleUi) -> None:
    @ui.page("/kline", title=f"K 线 · {TITLE}")
    def kline_page():
        shell = enter(console, page="kline", parts=PARTS)
        if isinstance(shell, RedirectResponse):
            return shell
        build(shell)
        shell.start()


def build(shell: Shell) -> None:
    controller = shell.controller
    tz = shell.console.tz
    config = shell.console.config
    rendered = {"candles": -1}
    remembered = controller.candles_query

    async def load() -> None:
        text = symbol.value.strip().upper() if isinstance(symbol.value, str) else ""
        input_error.set_text("" if text else "请输入合约代码，例如 BTCUSDT")
        if not text:
            return
        await controller.refresh_candles({**remembered, "symbol": text, "tf": timeframe.value})
        await shell._tick()  # noqa: SLF001 - 同一个外壳，拉完立刻重画

    async def refresh() -> None:
        await controller.refresh(PARTS)
        await controller.refresh_candles()
        await shell._tick()  # noqa: SLF001

    async def poll() -> None:
        await controller.refresh_candles_if_due(max_age_s=config.poll_interval_s * 0.8)

    with shell.body:
        with ui.row().classes("items-center gap-3"):
            ui.label("K 线").classes("dmr-h2").mark("kline-title")
            shell.utility_button("刷新", refresh, "btn-refresh")
        with ui.row().classes("items-end gap-2"):
            symbol = ui.input("合约", value=str(remembered.get("symbol", "BTCUSDT"))).props("dense").mark("kline-symbol")
            timeframe = ui.select(TIMEFRAMES, value=remembered.get("tf") if remembered.get("tf") in TIMEFRAMES else "15m",
                                  label="周期").props("dense").mark("kline-tf")
            shell.utility_button("查看", load, "btn-kline-load")
        input_error = ui.label("").classes("dmr-reason").mark("kline-input-error")
        error = ui.label("").classes("dmr-part-errors").mark("kline-error")
        summary = ui.label("").classes("dmr-hint").mark("kline-summary")
        holder = ui.column().classes("w-full gap-1")

    def draw() -> None:
        holder.clear()
        loaded = controller.loaded("candles")
        option = T.kline_option(controller.candles, tz=tz)
        with holder:
            if loaded and option["xAxis"][0]["data"]:
                ui.echart(option).classes("w-full").style("height: 620px").mark("kline-chart")
            else:
                ui.label(EMPTY_TEXT if loaded else "加载中…").classes("dmr-empty").mark("kline-empty")
        summary.set_text(T.kline_summary(controller.candles) if loaded else "")

    def update() -> None:
        error.set_text(f"K 线：{controller.errors['candles']}" if "candles" in controller.errors else "")
        if rendered["candles"] != controller.versions["candles"]:
            rendered["candles"] = controller.versions["candles"]
            draw()

    shell.add_poller(poll)
    shell.add_updater(update)
    draw()
    ui.timer(0.01, controller.refresh_candles, once=True)
