"""统一操作的纯函数层：按钮可用性与提示、报文构造、op_id 复用、席位续租、恢复按钮、预览整理。"""
from __future__ import annotations

import re

import pytest
from console_kit import make_position, make_state

from dmr_console import operations as O
from dmr_console.link import LinkStatus
from dmr_console.state import SEAT_REASON, row_action_states

OK = LinkStatus(lost=False)
LOST = LinkStatus(lost=True)
OP_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def mine(**over):
    return make_state(seat={"holder_session_id": "s1", "holder_operator": "admin", "lease_until_ms": None, "mine": True}, **over)


# ------------------------------------------------------------------ op_id

def test_a_new_op_id_is_valid_for_the_kernel_and_unique():
    ids = {O.new_op_id() for _ in range(200)}
    assert len(ids) == 200 and all(OP_ID.fullmatch(i) for i in ids)


def test_the_same_click_reuses_its_op_id_until_it_is_settled():
    book = O.OpIds()
    first = book.get("close_all")
    assert book.get("close_all") == first  # 重试
    assert book.get("stop") != first  # 别的操作不共用
    book.settle("close_all")
    assert book.get("close_all") != first  # 新的一次点击


def test_settling_an_unknown_slot_is_harmless():
    O.OpIds().settle("nothing")


# ------------------------------------------------------------------ 席位

def test_seat_actions_follow_who_holds_the_seat():
    assert O.seat_actions(make_state()) == ("acquire",)
    assert O.seat_actions(mine()) == ("release",)
    other = make_state(seat={"holder_session_id": "s2", "holder_operator": "bob", "lease_until_ms": 5, "mine": False})
    assert O.seat_actions(other) == ("force",)
    assert O.seat_actions(None) == ()


def test_the_seat_is_renewed_every_half_lease():
    state = mine()
    state["seat"]["lease_until_ms"] = 1_000_000 + 60_000
    assert O.renew_interval_s(state, now_ms=1_000_000) == 30.0
    assert O.renew_interval_s(make_state(), now_ms=1_000_000) is None  # 没持有就不续
    assert O.renew_interval_s(None, now_ms=0) is None
    state["seat"]["lease_until_ms"] = 1_000_000 + 1_000
    assert O.renew_interval_s(state, now_ms=1_000_000) == O.MIN_RENEW_S  # 下限，避免敲得太密


def test_the_lease_length_is_remembered_so_renewal_keeps_half_of_the_full_lease():
    tracker = O.LeaseTracker()
    state = mine()
    state["seat"]["lease_until_ms"] = 100_000 + 60_000
    tracker.observe(state, now_ms=100_000)
    state["seat"]["lease_until_ms"] = 100_000 + 20_000  # 已用掉一部分，租约总长仍是 60 秒
    tracker.observe(state, now_ms=140_000)
    assert tracker.interval_s(state, now_ms=140_000) == 30.0


# ------------------------------------------------------------------ 恢复

def test_the_recover_button_only_shows_while_the_kernel_is_recovering():
    assert not O.needs_recover(make_state())
    assert O.needs_recover(make_state(model={"suppressions": [{"code": "recovering", "label": "恢复中"}]}))
    assert not O.needs_recover(make_state(model={"suppressions": [{"code": "clock_skew", "label": "x"}]}))
    assert not O.needs_recover(None)


# ------------------------------------------------------------------ 版本

def test_versions_are_read_from_state_and_position():
    assert O.model_version(make_state()) == 12
    assert O.model_version(None) is None
    assert O.model_version({"model": {"version": "x"}}) is None
    assert O.position_version(make_position(version=7)) == 7
    assert O.position_version({}) is None


# ------------------------------------------------------------------ 报文

def test_payloads_follow_the_wire_contract():
    assert O.start_payload("a", 12) == {"op_id": "a", "model_version": 12}
    assert O.start_payload("a", 12, password="pw") == {"op_id": "a", "model_version": 12, "password": "pw"}
    assert O.stop_payload("a", 12) == {"op_id": "a", "model_version": 12}
    assert O.stop_payload("a", 12, close_all=True, password="pw") == {
        "op_id": "a", "model_version": 12, "close_all": True, "password": "pw"}
    assert O.partial_preview_payload(7, ratio=0.5) == {"position_id": 7, "ratio": 0.5}
    assert O.partial_preview_payload(7, qty="0.003") == {"position_id": 7, "qty": "0.003"}
    preview = {"position_id": 7, "position_version": 3, "live_qty": "0.010", "close_qty": "0.005"}
    assert O.partial_confirm_payload("a", preview) == {
        "op_id": "a", "position_id": 7, "position_version": 3, "preview_qty": "0.010", "close_qty": "0.005"}
    assert O.close_one_payload("a", 7, 3) == {"op_id": "a", "position_id": 7, "position_version": 3}
    assert O.close_all_preview_payload("model") == {"scope": "model"}
    assert O.close_all_confirm_payload("a", 12, "all_managed", "pw") == {
        "op_id": "a", "model_version": 12, "scope": "all_managed", "password": "pw"}
    assert O.abort_payload("a", "b") == {"op_id": "a", "target_op_id": "b"}
    assert O.switch_preview_payload("a", "m2") == {"op_id": "a", "target": "m2"}
    assert O.switch_confirm_payload("a", 12, "keep", "pw") == {"op_id": "a", "model_version": 12, "mode": "keep", "password": "pw"}
    assert O.switch_cancel_payload("a") == {"op_id": "a"}
    assert O.recover_payload("a") == {"op_id": "a"}
    assert O.test_open_payload("a", "BTCUSDT", "LONG", "100") == {"op_id": "a", "symbol": "BTCUSDT", "side": "LONG", "notional": "100"}


def test_an_empty_password_is_not_sent():
    assert "password" not in O.start_payload("a", 1, password="")
    assert "password" not in O.close_all_confirm_payload("a", 1, "model", None)


def test_the_only_choices_are_the_documented_ones():
    assert O.PARTIAL_RATIOS == (0.25, 0.5, 0.75)
    assert O.CLOSE_ALL_SCOPES == ("model", "all_managed")
    assert O.SWITCH_MODES == ("keep", "close_all_first")
    assert O.SWITCH_TARGETS == ("m1", "m2", "m3")


@pytest.mark.parametrize("text,expected", [("30", 0.3), (" 12.5 ", 0.125), ("99", 0.99), ("1", 0.01)])
def test_a_custom_percentage_becomes_a_ratio(text, expected):
    assert O.parse_custom_percent(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "0", "100", "-5", "abc", "nan", "inf", "101", "1e3"])
def test_bad_custom_percentages_are_rejected(text):
    assert O.parse_custom_percent(text) is None


# ------------------------------------------------------------------ 结果解读

def test_the_result_is_unwrapped_from_the_envelope():
    assert O.result_of({"op_id": "a", "status": "running", "result": {"x": 1}}) == {"x": 1}
    assert O.result_of({"x": 1}) == {"x": 1}
    assert O.result_of(None) == {}
    assert O.status_of({"status": "running"}) == "running"
    assert O.status_of({}) == ""


def test_error_text_prefers_the_kernel_message_and_hints_at_the_next_step():
    assert "重新预览" in O.error_text("stale_version", "版本已变")
    assert "版本已变" in O.error_text("stale_version", "版本已变")
    assert O.error_text("reauth_required", "").startswith("需要重新输入口令")
    assert O.error_text("seat_required", "").startswith("需要先取得操作席位")
    assert O.error_text("whatever", "原文") == "原文"
    assert O.error_text("whatever", "") == "内核拒绝了请求（whatever）"


def test_start_check_rows_list_every_self_check():
    rows = O.check_rows({"checks": [{"name": "对冲模式", "ok": True, "detail": ""}, {"name": "对账", "ok": False, "detail": "有差异"}]})
    assert rows == [("通过", "对冲模式", ""), ("未通过", "对账", "有差异")]
    assert O.check_rows({}) == []


def test_close_all_preview_is_split_into_included_and_excluded_lines():
    pos = lambda pid, sym, side: {"id": pid, "symbol": sym, "side": side, "qty": "1", "ownership": "strategy"}  # noqa: E731
    view = O.close_all_view({"scope": "model", "model_version": 12, "included": [pos(1, "BTCUSDT", "LONG")],
                             "excluded": [{"position": pos(2, "ETHUSDT", "SHORT"), "reason": "外部持仓"}],
                             "mixed_included": [pos(1, "BTCUSDT", "LONG")]})
    assert view.included == ["BTCUSDT 多 LONG · 数量 1"]
    assert view.excluded == ["ETHUSDT 空 SHORT · 数量 1：外部持仓"]
    assert view.mixed == ["BTCUSDT 多 LONG · 数量 1"]
    assert view.model_version == 12
    empty = O.close_all_view({})
    assert empty.included == [] and empty.excluded == [] and empty.mixed == []


def test_per_position_results_are_labelled_in_chinese():
    rows = O.per_position_rows([{"position_id": 1, "symbol": "BTCUSDT", "side": "LONG", "result": "closed", "detail": ""},
                                {"position_id": 2, "symbol": "ETHUSDT", "side": "SHORT", "result": "weird", "detail": "x"}])
    assert rows[0]["result"] == "已平" and rows[0]["symbol"] == "BTCUSDT"
    assert rows[1]["result"] == "weird" and rows[1]["detail"] == "x"
    assert O.per_position_rows(None) == []


def test_switch_preview_is_summarised_for_the_dialog():
    view = O.switch_view({"model_version": 12, "current": {"active": "m1", "gate": "running"},
                          "counts_by_ownership": {"strategy": 2, "takeover": 1}, "open_orders": [{"id": 1}],
                          "expires_ms": 5, "notes": ["切到模型 3：不开仓，也不接管新仓"]}, target="m3")
    assert view.model_version == 12 and view.target == "m3"
    assert "策略 2" in view.counts_text and "接管 1" in view.counts_text
    assert view.open_orders == 1 and view.notes == ["切到模型 3：不开仓，也不接管新仓"]


# ------------------------------------------------------------------ 持仓行按钮

def test_row_actions_follow_the_allowed_table_the_seat_and_the_link():
    state = mine()
    state["allowed"]["partial_close"] = {"allowed": True, "reason": "", "needs_live_read": False, "note": ""}
    state["allowed"]["close_one"] = {"allowed": True, "reason": "", "needs_live_read": False, "note": ""}
    rows = row_action_states(state, OK, operations_enabled=True)
    assert rows["partial_close"].enabled and rows["close_one"].enabled
    state["allowed"]["close_one"] = {"allowed": False, "reason": "没有托管持仓", "needs_live_read": False, "note": ""}
    rows = row_action_states(state, OK, operations_enabled=True)
    assert not rows["close_one"].enabled and rows["close_one"].reason == "没有托管持仓"
    assert row_action_states(make_state(), OK, operations_enabled=True)["partial_close"].reason in ("没有托管持仓", SEAT_REASON)
    assert not row_action_states(state, LOST, operations_enabled=True)["partial_close"].enabled


def test_intent_result_text_follows_the_kernel_answer():
    assert "REJECTED" in O.intent_result_text("failed", {"state": "REJECTED", "reason": "影子记录：只留痕不下单"})
    assert "影子记录：只留痕不下单" in O.intent_result_text("failed", {"state": "REJECTED", "reason": "影子记录：只留痕不下单"})
    assert "已受理" not in O.intent_result_text("failed", {"state": "REJECTED", "reason": "x"})
    ok = O.intent_result_text("succeeded", {"state": "COMPLETED"})
    assert "COMPLETED" in ok and "被拒绝" not in ok
    not_submitted = O.intent_result_text("failed", {"signal": "none", "submitted": False, "reason": "不构成信号"})
    assert "未提交" in not_submitted and "不构成信号" in not_submitted
    assert "unknown" in O.intent_result_text("unknown", {"state": "ACTIVE"}).lower() or "结果未知" in O.intent_result_text("unknown", {"state": "ACTIVE"})
    assert O.intent_result_text("", {}) != ""
