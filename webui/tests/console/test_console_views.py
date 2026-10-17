"""页面状态逻辑（纯函数）：按钮启用/禁用、持仓按归属分组、操作日志与告警行。"""
from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from console_kit import START_MS, make_alert, make_oplog_item, make_position, make_state
from dmr_console.link import LinkStatus
from dmr_console.state import (LOST_MESSAGE, OPERATION_ORDER, READ_ONLY_REASON, SEAT_REASON, button_states, utility_enabled,
                               allowed_rows)
from dmr_console.views import (POSITION_GROUP_KEYS, alert_rows, group_positions, merge_oplog, oplog_rows)

UTC7 = timezone(timedelta(hours=7))
NOW = START_MS
OK = LinkStatus(lost=False, last_ok_ms=NOW)
LOST = LinkStatus(lost=True, reason="请求内核超时", kind="timeout", lost_since_ms=NOW, last_ok_ms=NOW - 9_000, failures=2)


def allowed_everything() -> dict:
    return make_state(
        model={"gate": "running"},
        allowed={k: {"allowed": True, "reason": "", "needs_live_read": False, "note": ""}
                 for k in ("start", "stop", "switch", "switch_confirm", "switch_cancel", "partial_close", "close_one",
                           "close_all", "abort")},
        seat={"holder_session_id": "s1", "holder_operator": "admin", "lease_until_ms": NOW + 30_000, "mine": True},
    )


# ------------------------------------------------------------------ 按钮

def test_the_four_unified_buttons_and_model_switch_exist_in_a_fixed_order():
    assert OPERATION_ORDER == ("start", "stop", "partial_close", "close_all", "switch")
    states = button_states(make_state(), OK)
    assert tuple(states) == OPERATION_ORDER
    assert [states[k].label for k in OPERATION_ORDER] == ["启动", "停止", "部分平仓", "一键全部平仓", "切换模型"]


def test_buttons_carry_the_meaning_for_the_current_model():
    states = button_states(make_state(button_meanings={"start": "开始自动接管清单内的手动仓"}), OK)
    assert states["start"].meaning == "开始自动接管清单内的手动仓"
    assert states["close_all"].meaning == "默认平策略仓"
    assert states["switch"].meaning  # 切换没有按模型区分的含义，给一句通用的话


def test_this_version_is_read_only_so_every_operation_button_is_disabled_even_when_the_kernel_allows_it():
    states = button_states(allowed_everything(), OK)
    assert all(not s.enabled for s in states.values())
    assert all(s.reason == READ_ONLY_REASON for s in states.values())
    assert all(s.kernel_allowed is True for s in states.values())


def test_when_operations_are_enabled_the_kernel_allowed_table_decides():
    states = button_states(allowed_everything(), OK, operations_enabled=True)
    assert all(s.enabled and s.reason == "" for s in states.values())
    state = allowed_everything()
    state["allowed"]["stop"] = {"allowed": False, "reason": "开仓闸已关闭", "needs_live_read": False, "note": ""}
    states = button_states(state, OK, operations_enabled=True)
    assert not states["stop"].enabled and states["stop"].reason == "开仓闸已关闭"
    assert states["start"].enabled


def test_buttons_are_disabled_when_the_kernel_is_lost_even_if_everything_was_allowed():
    for enabled_flag in (False, True):
        states = button_states(allowed_everything(), LOST, operations_enabled=enabled_flag)
        assert all(not s.enabled for s in states.values())
        assert all(s.reason == LOST_MESSAGE for s in states.values())


def test_buttons_are_disabled_before_any_state_has_arrived():
    for flag in (False, True):
        states = button_states(None, OK, operations_enabled=flag)
        assert all(not s.enabled and s.reason for s in states.values())
    assert button_states(None, OK, operations_enabled=True)["start"].reason == "尚未取得内核状态"


def test_buttons_need_the_operation_seat_when_operations_are_enabled():
    state = allowed_everything()
    state["seat"] = {"holder_session_id": None, "holder_operator": None, "lease_until_ms": None, "mine": False}
    states = button_states(state, OK, operations_enabled=True)
    assert all(not s.enabled and s.reason == SEAT_REASON for s in states.values())


def test_a_missing_or_malformed_allowed_entry_means_not_allowed():
    state = allowed_everything()
    state["allowed"] = {"start": {}, "stop": None, "switch": "yes"}
    states = button_states(state, OK, operations_enabled=True)
    assert all(not s.enabled for s in states.values())
    assert states["start"].kernel_allowed is False
    assert states["close_all"].kernel_allowed is None and states["close_all"].reason


@pytest.mark.parametrize("junk", [None, 5, "x", []])
def test_garbage_state_sections_do_not_crash_button_logic(junk):
    state = allowed_everything()
    state["allowed"] = junk
    state["button_meanings"] = junk
    state["seat"] = junk
    states = button_states(state, OK, operations_enabled=True)
    assert tuple(states) == OPERATION_ORDER and all(not s.enabled for s in states.values())


def test_utility_buttons_follow_the_link_only():
    assert utility_enabled(OK) is True
    assert utility_enabled(LOST) is False


def test_allowed_rows_list_every_operation_with_the_kernel_reason():
    rows = allowed_rows(make_state())
    by_key = {r["key"]: r for r in rows}
    assert by_key["start"]["text"] == "可执行" and by_key["start"]["reason"] == ""
    assert by_key["stop"]["text"] == "不可执行" and by_key["stop"]["reason"] == "开仓闸已关闭"
    assert [r["key"] for r in rows][:5] == ["start", "stop", "partial_close", "close_all", "switch"]
    assert allowed_rows(None) == []
    assert allowed_rows({"allowed": "junk"}) == []


# ------------------------------------------------------------------ 持仓分组

def ids(group) -> list[int]:
    return [r["id"] for r in group.rows]


def test_registered_positions_are_grouped_by_ownership_in_a_fixed_order():
    registered = [
        make_position(id=1, ownership="strategy"),
        make_position(id=2, ownership="takeover", symbol="ETHUSDT"),
        make_position(id=3, ownership="strategy", is_mixed=True, symbol="SOLUSDT"),
        make_position(id=4, ownership="ai", symbol="XRPUSDT"),
        make_position(id=5, ownership="mystery", symbol="ADAUSDT"),
    ]
    groups = group_positions(registered, [], tz=UTC7)
    assert tuple(g.key for g in groups) == POSITION_GROUP_KEYS == ("strategy", "mixed", "takeover", "ai", "other", "pending", "external")
    assert {g.key: ids(g) for g in groups} == {"strategy": [1], "mixed": [3], "takeover": [2], "ai": [4], "other": [5],
                                              "pending": [], "external": []}
    assert groups[0].title == "策略仓" and groups[1].title == "混合仓" and groups[2].title == "接管仓"


def test_pending_and_external_positions_are_listed_apart_from_registered_ones():
    external = [make_position(id=10, status="pending", ownership="none", pending_reason="out_of_scope", has_stop_on_exchange=False),
                make_position(id=11, status="ignored", ownership="none", symbol="ETHUSDT", has_stop_on_exchange=True)]
    groups = {g.key: g for g in group_positions([make_position(id=1)], external, tz=UTC7)}
    assert ids(groups["strategy"]) == [1]
    assert ids(groups["pending"]) == [10] and ids(groups["external"]) == [11]
    pending = groups["pending"].rows[0]
    assert pending["pending_reason"] == "不在接管清单" and pending["exchange_stop"] == "无"
    assert groups["external"].rows[0]["exchange_stop"] == "有"
    assert "外部" in groups["external"].title and "待确认" in groups["pending"].title


def test_closed_positions_are_not_shown_and_status_overrides_ownership_for_pending():
    registered = [make_position(id=1, status="closed"), make_position(id=2, status="pending", ownership="none")]
    groups = {g.key: g for g in group_positions(registered, [], tz=UTC7)}
    assert ids(groups["strategy"]) == [] and ids(groups["pending"]) == [2] and ids(groups["other"]) == []


def test_rows_are_sorted_by_symbol_then_side_then_id():
    registered = [make_position(id=3, symbol="BTCUSDT", side="SHORT"), make_position(id=2, symbol="BTCUSDT", side="LONG"),
                  make_position(id=1, symbol="ADAUSDT", side="SHORT")]
    assert ids(group_positions(registered, [], tz=UTC7)[0]) == [1, 2, 3]


def test_a_row_shows_what_the_report_asks_for():
    position = make_position(unrealized_pnl="-2.5", mark_price="59900", liquidation_price="30000.5", stop_price="58000",
                             exit_ruleset="strategy_no_reverse", open_time_estimated=True, open_time_ms=NOW, leverage=5,
                             margin_mode="cross")
    row = group_positions([position], [], tz=UTC7)[0].rows[0]
    assert row["symbol"] == "BTCUSDT" and row["side"] == "多 LONG"
    assert row["status"] == "托管中" and row["ownership"] == "策略"
    assert (row["qty"], row["avg_price"], row["mark_price"]) == ("0.010", "60000", "59900")
    assert row["pnl"] == "-2.50" and row["pnl_sign"] == -1
    assert row["liquidation"] == "30000.5"
    assert row["protection"] == "系统保护" and row["stop"] == "58000"
    assert row["exit"] == "策略规则（不反手）"
    assert row["consistent"] == "一致" and row["unconfirmed"] == "无" and row["closing"] == "否"
    assert row["opened"] == "2025-10-09 15:53:20（估计）"
    assert row["leverage"] == "5x 全仓"
    assert not row["danger"] and not row["warn"]


def test_short_side_and_missing_mark_price():
    row = group_positions([make_position(side="SHORT", mark_price=None, unrealized_pnl=None)], [], tz=UTC7)[0].rows[0]
    assert row["side"] == "空 SHORT" and row["mark_price"] == "—" and row["pnl"] == "—" and row["pnl_sign"] == 0


def test_an_unprotected_registered_position_is_flagged_danger():
    row = group_positions([make_position(protection_state="none", stop_price=None, has_stop_on_exchange=False)], [], tz=UTC7)[0].rows[0]
    assert row["protection"] == "无保护" and row["stop"] == "—" and row["danger"] is True


def test_external_protection_is_shown_and_not_danger():
    row = group_positions([make_position(protection_state="external", stop_note="外部止损单")], [], tz=UTC7)[0].rows[0]
    assert row["protection"] == "外部保护" and "外部止损单" in row["stop"] and not row["danger"]


def test_local_vs_exchange_quantity_mismatch_and_unconfirmed_orders_are_warnings():
    mismatch = group_positions([make_position(consistent=False, exchange_qty="0.020")], [], tz=UTC7)[0].rows[0]
    assert mismatch["consistent"] == "不一致（交易所 0.020）" and mismatch["warn"] is True and not mismatch["danger"]
    pending_orders = group_positions([make_position(has_unconfirmed_orders=True)], [], tz=UTC7)[0].rows[0]
    assert pending_orders["unconfirmed"] == "有" and pending_orders["warn"] is True
    unknown = group_positions([make_position(consistent=None)], [], tz=UTC7)[0].rows[0]
    assert unknown["consistent"] == "未知"


def test_closing_flag():
    row = group_positions([make_position(status="closing", closing=True)], [], tz=UTC7)[0].rows[0]
    assert row["status"] == "平仓中" and row["closing"] == "是"


def test_pending_flags_and_origin_note_are_kept():
    external = [make_position(status="pending", ownership="none", pending_reason="unknown_origin", pending_flags=["follow_up", "x"],
                              origin_note="来源不明")]
    row = {g.key: g for g in group_positions([], external, tz=UTC7)}["pending"].rows[0]
    assert row["pending_reason"] == "来源不明" and row["note"] == "来源不明"
    assert "follow_up" in row["flags"]


@pytest.mark.parametrize("junk", [None, 5, "x", [], {}])
def test_garbage_positions_never_crash_the_grouping(junk):
    groups = group_positions([junk, {}, {"id": "x", "symbol": None}], [junk], tz=UTC7)
    assert tuple(g.key for g in groups) == POSITION_GROUP_KEYS
    assert group_positions(None, None, tz=UTC7)[0].rows == ()


def test_registered_empty_lists_give_empty_groups():
    groups = group_positions([], [], tz=UTC7)
    assert all(g.rows == () for g in groups) and len(groups) == 7


# ------------------------------------------------------------------ 操作日志

def test_oplog_rows_are_formatted_for_display():
    items = [make_oplog_item(id=7, at_ms=NOW, operator="admin", action="config.set", target="max_symbols", result="ok",
                             request_id="r-7", before={"value": 30}, after="31")]
    row = oplog_rows(items, tz=UTC7)[0]
    assert row["id"] == 7 and row["time"] == "2025-10-09 15:53:20"
    assert (row["operator"], row["action"], row["target"], row["result"], row["request_id"]) == \
           ("admin", "config.set", "max_symbols", "ok", "r-7")
    assert row["before"] == '{"value":30}' and row["after"] == "31"


def test_long_before_after_are_truncated_for_the_cell_but_kept_in_full_for_the_tooltip():
    long_text = "x" * 200
    row = oplog_rows([make_oplog_item(before=long_text, after=None)], tz=UTC7)[0]
    assert len(row["before"]) <= 81 and row["before"].endswith("…")
    assert row["before_full"] == long_text
    assert row["after"] == "—" and row["after_full"] == ""


def test_oplog_none_fields_become_dashes_and_junk_rows_survive():
    row = oplog_rows([make_oplog_item(operator=None, target=None, result=None, request_id=None)], tz=UTC7)[0]
    assert row["operator"] == "—" and row["target"] == "—" and row["result"] == "—" and row["request_id"] == "—"
    assert len(oplog_rows([None, 5, "x", {}], tz=UTC7)) == 1  # 只有空字典算一行；非字典被忽略
    assert oplog_rows(None, tz=UTC7) == []


def test_merge_oplog_appends_older_pages_without_duplicates_and_keeps_newest_first():
    page1 = [make_oplog_item(id=i) for i in (10, 9, 8)]
    page2 = [make_oplog_item(id=i) for i in (8, 7, 6)]
    merged = merge_oplog(page1, page2)
    assert [i["id"] for i in merged] == [10, 9, 8, 7, 6]
    # 刷新最新页时新记录排到前面
    newest = merge_oplog(merged, [make_oplog_item(id=12), make_oplog_item(id=11), make_oplog_item(id=10)])
    assert [i["id"] for i in newest] == [12, 11, 10, 9, 8, 7, 6]


def test_merge_oplog_keeps_the_newer_copy_of_a_duplicate_id():
    old = [make_oplog_item(id=5, result="pending")]
    new = [make_oplog_item(id=5, result="ok")]
    assert merge_oplog(old, new)[0]["result"] == "ok"


def test_merge_oplog_respects_a_cap():
    many = [make_oplog_item(id=i) for i in range(500, 0, -1)]
    assert len(merge_oplog([], many, cap=200)) == 200
    assert merge_oplog([], many, cap=200)[0]["id"] == 500


# ------------------------------------------------------------------ 告警

def test_alert_rows():
    rows = alert_rows([make_alert(id=1, level="critical", kind="risk_tripped", message="风控触发", delivered=False),
                       make_alert(id=2, level="info", delivered=True), make_alert(id=3, level="odd", delivered=None)], tz=UTC7)
    assert rows[0]["level"] == "严重" and rows[0]["tone"] == "danger" and rows[0]["delivered"] == "未送达"
    assert rows[1]["level"] == "提示" and rows[1]["tone"] == "info" and rows[1]["delivered"] == "已送达"
    assert rows[2]["level"] == "odd" and rows[2]["delivered"] == "—"
    assert rows[0]["time"] == "2025-10-09 15:52:50"
    assert alert_rows(None, tz=UTC7) == []
