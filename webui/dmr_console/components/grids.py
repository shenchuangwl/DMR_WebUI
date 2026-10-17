"""AG Grid 的选项（纯函数，不碰 NiceGUI）。

带 ``:`` 前缀的键，NiceGUI 会把它的值当 JS 在浏览器里执行：这里的 JS 全是常量，**绝不拼进任何内核数据**；
单元格一律按文本渲染（不用 cellRenderer/html_columns），所以内核给的文字再怪也不会被当成 HTML。
"""
from __future__ import annotations

from typing import Any, Iterable

GET_ROW_CLASS_JS = "params => (params.data && params.data.row_class) || ''"
PNL_CELL_CLASS_JS = "params => params.data && params.data.pnl_sign > 0 ? 'dmr-pos' : (params.data && params.data.pnl_sign < 0 ? 'dmr-neg' : '')"
DANGER_CELL_CLASS_JS = "params => params.data && params.data.danger ? 'dmr-neg' : ''"
def _numeric_comparator_js(field: str) -> str:
    return f"(a, b, nodeA, nodeB) => nodeA.data.{field} - nodeB.data.{field}"  # field 只来自本文件的常量


TONE_CELL_CLASS_JS = ("params => params.data && params.data.tone === 'danger' ? 'dmr-neg' : "
                      "(params.data && params.data.tone === 'warn' ? 'dmr-warn-text' : '')")


def _col(field: str, header: str, width: int, **extra: Any) -> dict[str, Any]:
    return {"field": field, "headerName": header, "width": width, **extra}


_SYMBOL = _col("symbol", "合约", 120, pinned="left")
_SIDE = _col("side", "方向", 95)
_STATUS = _col("status", "状态", 100)
_OWNERSHIP = _col("ownership", "归属", 85)
_QTY = _col("qty", "数量", 105)
_AVG = _col("avg_price", "开仓均价", 115)
_MARK = _col("mark_price", "标记价", 115)
_PNL = _col("pnl", "浮动盈亏", 105, **{":cellClass": PNL_CELL_CLASS_JS})
_LIQ = _col("liquidation", "强平价", 110)
_PROTECTION = _col("protection", "保护", 100, **{":cellClass": DANGER_CELL_CLASS_JS})
_STOP = _col("stop", "止损价", 160)
_EXIT = _col("exit", "出场规则", 140)
_CONSISTENT = _col("consistent", "本地与交易所数量", 170)
_UNCONFIRMED = _col("unconfirmed", "未确认订单", 105)
_CLOSING = _col("closing", "平仓中", 80)
_OPENED = _col("opened", "开仓时间", 200)
_LEVERAGE = _col("leverage", "杠杆", 95)

REGISTERED_COLUMNS = [_SYMBOL, _SIDE, _STATUS, _OWNERSHIP, _QTY, _AVG, _MARK, _PNL, _LIQ, _PROTECTION, _STOP, _EXIT,
                      _CONSISTENT, _UNCONFIRMED, _CLOSING, _OPENED, _LEVERAGE]
# 待确认与外部：另外显示「交易所侧有没有止损类条件单」；它们没有系统保护/出场规则，所以不放那两列
PENDING_COLUMNS = [_SYMBOL, _SIDE, _STATUS, _QTY, _AVG, _MARK, _PNL, _LIQ,
                   _col("pending_reason", "待确认原因", 190), _col("exchange_stop", "交易所止损类条件单", 150),
                   _col("note", "来源备注", 200), _col("flags", "标记", 130),
                   _CONSISTENT, _UNCONFIRMED, _OPENED, _LEVERAGE]
EXTERNAL_COLUMNS = [c for c in PENDING_COLUMNS if c["field"] != "pending_reason"]


def position_columns(group_key: str) -> list[dict[str, Any]]:
    table = {"pending": PENDING_COLUMNS, "external": EXTERNAL_COLUMNS}.get(group_key, REGISTERED_COLUMNS)
    return [dict(column) for column in table]


def row_class_for(row: dict[str, Any]) -> str:
    if row.get("danger"):
        return "dmr-row-danger"
    if row.get("warn"):
        return "dmr-row-warn"
    return ""


def _options(columns: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "columnDefs": columns,
        "rowData": rows,
        "defaultColDef": {"sortable": True, "resizable": True},
        "domLayout": "autoHeight",
        "suppressFieldDotNotation": True,
        "enableCellTextSelection": True,
        "animateRows": False,
        "tooltipShowDelay": 300,
        ":getRowClass": GET_ROW_CLASS_JS,
    }


def position_grid_options(group_key: str, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    prepared = [{**row, "row_class": row_class_for(row)} for row in rows]
    return _options(position_columns(group_key), prepared)


def oplog_grid_options(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    columns = [
        _col("id", "编号", 85),
        _col("time", "时间", 190),
        _col("operator", "操作人", 110),
        _col("action", "操作", 170),
        _col("target", "对象", 170),
        _col("result", "结果", 110),
        _col("request_id", "请求编号", 180),
        _col("before", "操作前", 260, tooltipField="before_full"),
        _col("after", "操作后", 260, tooltipField="after_full"),
    ]
    return _options(columns, list(rows))


def alert_grid_options(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    columns = [
        _col("time", "时间", 190),
        _col("level", "级别", 90, **{":cellClass": TONE_CELL_CLASS_JS}),
        _col("kind", "类型", 170),
        _col("message", "内容", 520),
        _col("delivered", "外部推送", 100),
    ]
    return _options(columns, list(rows))


def watchlist_grid_options(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    columns = [
        _col("symbol", "合约", 150, pinned="left"),
        _col("side", "方向", 100),
        _col("score", "分数", 90),
        _col("first_selected", "首次入选时间", 190),
        _col("held", "是否已持仓", 110),
        _col("avoid", "避让原因", 420),
    ]
    return _options(columns, list(rows))


def rejected_grid_options(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    columns = [_col("code", "原始代码", 200), _col("reason", "被拒收的原因", 520)]
    return _options(columns, list(rows))


def trade_grid_options(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    columns = [
        _col("opened", "开仓时间", 190),
        _col("closed", "平仓时间", 190),
        _SYMBOL,
        _SIDE,
        _OWNERSHIP,
        _col("mixed", "混合仓", 90),
        _col("model", "模型", 90),
        _col("qty", "数量", 105),
        _col("open_avg", "开仓均价", 115),
        _col("close_avg", "平仓均价", 115),
        _col("gross", "毛盈亏", 105, **{":cellClass": PNL_CELL_CLASS_JS, ":comparator": _numeric_comparator_js("gross_num")}),
        _col("fees", "手续费", 95),
        _col("funding", "资金费", 95),
        _col("net", "净盈亏", 105, **{":cellClass": PNL_CELL_CLASS_JS, ":comparator": _numeric_comparator_js("net_num")}),
        _col("return_pct", "收益率", 95),
        _col("duration", "持仓时长", 110),
        _col("entry_reason", "入场原因", 320, tooltipField="entry_reason"),
        _col("exit_reason", "出场原因", 120),
    ]
    options = _options([dict(c) for c in columns], list(rows))
    options["suppressColumnVirtualisation"] = True  # 列不多，全部渲染（窄屏横向滚动也能看到入场原因等列）
    return options


def stats_group_grid_options(first_header: str, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    columns = [
        _col("key", first_header, 230, pinned="left"),
        _col("profit_loss_ratio", "盈亏比", 100),
        _col("profit_rate", "盈利率", 105),
        _col("win_rate", "胜率", 100),
        _col("count", "笔数", 85),
        _col("net_pnl", "净盈亏", 110, **{":cellClass": PNL_CELL_CLASS_JS}),
        _col("gross_pnl", "毛盈亏", 110),
        _col("fees", "手续费", 100),
        _col("funding", "资金费", 100),
    ]
    return _options(columns, list(rows))
