"""常驻状态条的界面部分：横幅（失联、替身环境）放页眉里始终可见；字段卡片按 ``STATUS_BAR_FIELDS`` 排成网格。

字段内容全部来自 ``state.build_status_bar``（纯函数、已有单元测试）；这里只负责把它原地更新到元素上：
文字、色调、悬停提示都用 set_text / classes 就地改，不重建元素。
"""
from __future__ import annotations

from dataclasses import dataclass

from nicegui import ui

from dmr_console.components.theme import tone_class
from dmr_console.state import CHIP_LABELS, STATUS_BAR_FIELDS, Banner, StatusBar


@dataclass
class _ChipView:
    box: ui.element
    value: ui.label
    tip: ui.tooltip
    tone: str = "unknown"


class StatusBarView:
    def __init__(self) -> None:
        self._banner_box: ui.element | None = None
        self._banner_signature: tuple = ()
        self._banner_details: dict[str, ui.label] = {}
        self._chips: dict[str, _ChipView] = {}
        self._updated: ui.label | None = None

    # ------------------------------------------------------------ 构建

    def build_banners(self) -> None:
        self._banner_box = ui.column().classes("dmr-banners gap-0").mark("banners")

    def build_chips(self) -> None:
        with ui.column().classes("w-full gap-1").mark("statusbar"):
            with ui.element("div").classes("dmr-chips"):
                for key in STATUS_BAR_FIELDS:
                    self._chips[key] = self._build_chip(key)
            self._updated = ui.label("").classes("dmr-updated").mark("status-updated")

    @staticmethod
    def _build_chip(key: str) -> _ChipView:
        with ui.element("div").classes(f"dmr-chip {tone_class('unknown')}").mark(f"chip-box-{key}") as box:
            ui.label(CHIP_LABELS[key]).classes("dmr-chip-label")
            value = ui.label("未知").classes("dmr-chip-value").mark(f"chip-{key}")
            tip = ui.tooltip(CHIP_LABELS[key])
        return _ChipView(box=box, value=value, tip=tip)

    # ------------------------------------------------------------ 更新

    def update(self, bar: StatusBar) -> None:
        self.update_banners(bar.banners)
        for chip in bar.chips:
            view = self._chips.get(chip.key)
            if view is None:
                continue
            view.value.set_text(chip.text)
            if chip.tone != view.tone:
                view.box.classes(remove=tone_class(view.tone), add=tone_class(chip.tone))
                view.tone = chip.tone
            view.tip.set_text(chip.detail or chip.label)
        if self._updated is not None:
            self._updated.set_text(bar.updated_text)

    def update_banners(self, banners: tuple[Banner, ...]) -> None:
        """横幅集合（哪几条、什么文字）变了才重建；详情文字（如「已失联 20 秒」）每次就地更新。"""
        if self._banner_box is None:
            return
        signature = tuple((b.key, b.text, b.tone) for b in banners)
        if signature != self._banner_signature:
            self._banner_box.clear()
            self._banner_details = {}
            with self._banner_box:
                for banner in banners:
                    with ui.column().classes(f"dmr-banner dmr-banner-{banner.tone} gap-0").mark(f"banner-{banner.key}"):
                        ui.label(banner.text).classes("dmr-banner-text").mark(f"banner-text-{banner.key}")
                        self._banner_details[banner.key] = ui.label(banner.detail).classes("dmr-banner-detail").mark(
                            f"banner-detail-{banner.key}")
            self._banner_signature = signature
        for banner in banners:
            label = self._banner_details.get(banner.key)
            if label is not None:
                label.set_text(banner.detail)
                label.set_visibility(bool(banner.detail))
