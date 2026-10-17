"""「有数据才出现」的表格位。

数据还没到：只显示「加载中…」；到了但是空：显示空提示（不画一张只有表头的空表）；有行：整体重建一张 AG Grid。
重建而不是显示/隐藏：AG Grid 在隐藏的容器里初始化会量不到宽度，而它本来每次更新也是整体重建的。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui


class GridSlot:
    def __init__(self, grid_marker: str, empty_marker: str, make_options: Callable[[list[dict[str, Any]]], dict[str, Any]], *,
                 empty_text: str = "（无）") -> None:
        self._grid_marker = grid_marker
        self._empty_marker = empty_marker
        self._make_options = make_options
        self._empty_text = empty_text
        self._holder = ui.column().classes("w-full gap-1")
        self.show([], loaded=False)

    def show(self, rows: list[dict[str, Any]], *, loaded: bool) -> None:
        self._holder.clear()
        with self._holder:
            if rows:
                ui.aggrid(self._make_options(rows), auto_size_columns=False).mark(self._grid_marker)
            else:
                ui.label(self._empty_text if loaded else "加载中…").classes("dmr-empty").mark(self._empty_marker)
