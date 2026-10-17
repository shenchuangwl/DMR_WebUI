"""AG Grid 选项（纯函数）：列定义的字段必须真的存在于行里；带 ':' 前缀的键是会在浏览器里执行的 JS，必须是常量，绝不含内核数据。"""
from __future__ import annotations

import json
from datetime import timedelta, timezone

import pytest

from console_kit import make_alert, make_oplog_item, make_position
from dmr_console.components.grids import (alert_grid_options, oplog_grid_options, position_columns, position_grid_options,
                                          row_class_for)
from dmr_console.views import POSITION_GROUP_KEYS, alert_rows, group_positions, oplog_rows

UTC7 = timezone(timedelta(hours=7))


def sample_groups():
    registered = [make_position(id=1), make_position(id=2, ownership="takeover", symbol="ETHUSDT"),
                  make_position(id=3, is_mixed=True, symbol="SOLUSDT"), make_position(id=4, ownership="ai", symbol="XRPUSDT"),
                  make_position(id=5, ownership="mystery", symbol="ADAUSDT")]
    external = [make_position(id=10, status="pending", ownership="none"), make_position(id=11, status="ignored", ownership="none")]
    return {g.key: g for g in group_positions(registered, external, tz=UTC7)}


@pytest.mark.parametrize("group", POSITION_GROUP_KEYS)
def test_every_position_column_reads_a_field_that_exists_in_the_rows(group):
    row = sample_groups()[group].rows[0]
    columns = position_columns(group)
    assert columns
    fields = [c["field"] for c in columns]
    assert len(fields) == len(set(fields))
    for column in columns:
        assert column["field"] in row, column
        assert column["headerName"] and any("一" <= ch <= "鿿" for ch in column["headerName"])
        assert column["width"] > 0


def test_the_report_fields_are_all_columns_for_registered_positions():
    fields = {c["field"] for c in position_columns("strategy")}
    # 合约、方向、数量、开仓均价、浮动盈亏；归属；保护状态和止损价；强平价；数量是否一致；未确认订单；是否平仓中；开仓时间（含估计）
    for needed in ("symbol", "side", "qty", "avg_price", "pnl", "ownership", "protection", "stop", "liquidation", "consistent",
                   "unconfirmed", "closing", "opened"):
        assert needed in fields


def test_pending_and_external_groups_also_show_the_exchange_side_stop_order():
    for group in ("pending", "external"):
        fields = {c["field"] for c in position_columns(group)}
        assert "exchange_stop" in fields
    assert "pending_reason" in {c["field"] for c in position_columns("pending")}
    assert "exchange_stop" not in {c["field"] for c in position_columns("strategy")}


def test_unknown_group_falls_back_to_the_registered_columns():
    assert [c["field"] for c in position_columns("whatever")] == [c["field"] for c in position_columns("strategy")]


def test_position_grid_options_carry_rows_with_a_row_class():
    rows = list(sample_groups()["strategy"].rows)
    options = position_grid_options("strategy", rows)
    assert [r["id"] for r in options["rowData"]] == [r["id"] for r in rows]
    assert options["columnDefs"] == position_columns("strategy")
    assert options["domLayout"] == "autoHeight" and options["suppressFieldDotNotation"] is True
    assert ":getRowId" not in options  # 表格每次整体重建，不需要行编号；内核给了重复编号也不会让 AG Grid 报错
    assert all("row_class" in r for r in options["rowData"])
    assert rows[0].get("row_class") is None  # 不改动传入的行


def test_row_class_marks_danger_before_warn():
    assert row_class_for({"danger": True, "warn": True}) == "dmr-row-danger"
    assert row_class_for({"danger": False, "warn": True}) == "dmr-row-warn"
    assert row_class_for({"danger": False, "warn": False}) == ""
    assert row_class_for({}) == ""


def test_an_unprotected_position_row_is_highlighted_in_the_grid():
    unprotected = make_position(protection_state="none", stop_price=None, has_stop_on_exchange=False)
    rows = group_positions([unprotected], [], tz=UTC7)[0].rows
    assert position_grid_options("strategy", list(rows))["rowData"][0]["row_class"] == "dmr-row-danger"


def test_oplog_columns_exist_in_oplog_rows_and_tooltips_show_the_full_text():
    rows = oplog_rows([make_oplog_item(before={"a": 1}, after="x" * 200)], tz=UTC7)
    options = oplog_grid_options(rows)
    row = rows[0]
    for column in options["columnDefs"]:
        assert column["field"] in row
        if "tooltipField" in column:
            assert column["tooltipField"] in row
    fields = [c["field"] for c in options["columnDefs"]]
    for needed in ("time", "operator", "action", "target", "result", "request_id", "before", "after"):
        assert needed in fields
    assert options["rowData"] == rows


def test_alert_columns_exist_in_alert_rows():
    rows = alert_rows([make_alert()], tz=UTC7)
    options = alert_grid_options(rows)
    for column in options["columnDefs"]:
        assert column["field"] in rows[0]
    assert {"time", "level", "kind", "message", "delivered"} <= {c["field"] for c in options["columnDefs"]}


def _js_values(options: dict) -> dict:
    found = {}

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.startswith(":"):
                    found[f"{path}/{key}"] = value
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(options)
    return found


def test_javascript_strings_never_contain_kernel_data():
    evil = "'; alert(1); //</script>"
    benign_rows = list(sample_groups()["strategy"].rows)
    evil_position = make_position(symbol=evil, stop_note=evil, origin_note=evil)
    evil_rows = list(group_positions([evil_position], [], tz=UTC7)[0].rows)
    benign = position_grid_options("strategy", benign_rows)
    hostile = position_grid_options("strategy", evil_rows)
    assert _js_values(benign) == _js_values(hostile)
    assert _js_values(benign)  # 确实有 JS 键
    for text in _js_values(hostile).values():
        assert evil not in text
    # 恶意文字只出现在数据里（AG Grid 按文本渲染，不会当 HTML 执行）
    assert evil in json.dumps(hostile["rowData"], ensure_ascii=False)
    hostile_oplog = oplog_grid_options(oplog_rows([make_oplog_item(target=evil, after=evil)], tz=UTC7))
    assert all(evil not in v for v in _js_values(hostile_oplog).values())
    hostile_alerts = alert_grid_options(alert_rows([make_alert(message=evil)], tz=UTC7))
    assert all(evil not in v for v in _js_values(hostile_alerts).values())


def test_no_column_renders_html():
    for group in POSITION_GROUP_KEYS:
        for column in position_columns(group):
            assert "cellRenderer" not in column
    for options in (oplog_grid_options([]), alert_grid_options([])):
        assert all("cellRenderer" not in c for c in options["columnDefs"])
