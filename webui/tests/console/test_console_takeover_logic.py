"""接管类操作与设置页的纯逻辑：报文（编号类字段是整数）、哪些仓有哪些按钮、按钮启用规则、配置项整理与输入解析。"""
from __future__ import annotations

import pytest
from console_kit import make_position, make_state

from dmr_console import configview as C
from dmr_console import operations as O
from dmr_console.link import LinkStatus
from dmr_console.state import position_action_states

OK_LINK = LinkStatus()


def seat_state(active="m2", **over):
    return make_state(model={"active": active}, seat={"mine": True, "holder_session_id": "s", "holder_operator": "admin",
                                                      "lease_until_ms": None}, **over)


# ------------------------------------------------------------------ 报文

def test_position_op_payload_has_integer_ids():
    assert O.position_op_payload("c-1", 7, 3) == {"op_id": "c-1", "position_id": 7, "position_version": 3}


def test_cancel_takeover_payload_carries_the_mode_and_rejects_unknown_modes():
    assert O.cancel_takeover_payload("c-1", 7, 3, "handover") == {
        "op_id": "c-1", "position_id": 7, "position_version": 3, "mode": "handover"}
    assert O.cancel_takeover_payload("c-1", 7, 3, "cancel_stop")["mode"] == "cancel_stop"
    with pytest.raises(ValueError):
        O.cancel_takeover_payload("c-1", 7, 3, "other")


def test_paths():
    assert O.TAKEOVER_PATHS == {"takeover": "/ops/takeover", "ignore": "/ops/ignore", "restore": "/ops/restore",
                                "cancel_takeover": "/ops/cancel_takeover", "hand_stop_to_system": "/ops/hand_stop_to_system"}


# ------------------------------------------------------------------ 哪些仓有哪些按钮

def test_pending_positions_get_takeover_and_ignore_only():
    pos = make_position(status="pending", ownership="none", pending_reason="out_of_scope")
    assert O.position_actions(pos) == ("takeover", "ignore")


def test_origin_to_verify_adds_restore():
    pos = make_position(status="pending", ownership="none", pending_reason="origin_to_verify")
    assert O.position_actions(pos) == ("takeover", "ignore", "restore")


def test_takeover_and_mixed_positions_can_cancel_takeover():
    assert O.position_actions(make_position(status="managed", ownership="takeover")) == ("cancel_takeover",)
    assert O.position_actions(make_position(status="managed", ownership="strategy", is_mixed=True)) == ("cancel_takeover",)
    assert O.position_actions(make_position(status="taking_over", ownership="takeover")) == ("cancel_takeover",)


def test_external_protection_on_a_takeover_position_adds_hand_stop():
    pos = make_position(status="managed", ownership="takeover", protection_state="external")
    assert O.position_actions(pos) == ("cancel_takeover", "hand_stop_to_system")
    mixed = make_position(status="managed", ownership="strategy", is_mixed=True, protection_state="external")
    assert O.position_actions(mixed) == ("cancel_takeover",)


@pytest.mark.parametrize("pos", [
    make_position(status="managed", ownership="strategy"),
    make_position(status="ignored", ownership="none"),
    make_position(status="closed", ownership="takeover"),
    make_position(status="closing", ownership="strategy"),
    "not a dict", None,
])
def test_other_positions_have_no_takeover_buttons(pos):
    assert O.position_actions(pos) == ()


# ------------------------------------------------------------------ 按钮启用

def test_takeover_is_enabled_only_while_model_2_is_active_and_seat_held():
    states = position_action_states(seat_state("m2"), OK_LINK, operations_enabled=True)
    assert states["takeover"].enabled and states["ignore"].enabled and states["cancel_takeover"].enabled
    other = position_action_states(seat_state("m1"), OK_LINK, operations_enabled=True)
    assert not other["takeover"].enabled and "模型 2" in other["takeover"].reason
    assert other["ignore"].enabled and other["restore"].enabled  # 忽略与恢复托管任何模型下都可用


def test_without_the_seat_or_when_lost_everything_is_disabled_with_a_reason():
    state = make_state(model={"active": "m2"})
    states = position_action_states(state, OK_LINK, operations_enabled=True)
    assert all(not s.enabled and s.reason for s in states.values())
    lost = position_action_states(seat_state(), LinkStatus(lost=True, reason="无法连接内核"), operations_enabled=True)
    assert all(not s.enabled for s in lost.values())
    assert all(not s.enabled for s in position_action_states(None, OK_LINK, operations_enabled=True).values())


def test_the_kernel_allowed_table_wins_when_it_has_an_entry():
    state = seat_state("m2")
    state["allowed"]["takeover"] = {"allowed": False, "reason": "模型 2 未运行", "needs_live_read": False, "note": ""}
    state["allowed"]["ignore"] = {"allowed": True, "reason": "", "needs_live_read": False, "note": ""}
    states = position_action_states(state, OK_LINK, operations_enabled=True)
    assert not states["takeover"].enabled and states["takeover"].reason == "模型 2 未运行"
    assert states["ignore"].enabled


def test_read_only_console_disables_them():
    states = position_action_states(seat_state(), OK_LINK, operations_enabled=False)
    assert all(not s.enabled for s in states.values())


# ------------------------------------------------------------------ 设置页

def item(key, group="接管", value=None, **over):
    base = {"key": key, "group": group, "label": key, "value": value, "default": value, "min": None, "max": None,
            "choices": [], "unit": "", "apply": "立即生效", "affects_existing": False, "required_for_live": False,
            "requires_deploy_switch": False, "note": ""}
    base.update(over)
    return base


def test_groups_keep_the_kernel_order_and_describe_each_row():
    items = [item("a", "资金", "10", min="1", max="100", unit="USDT", default="5", affects_existing=True),
             item("takeover_symbols", "接管", ["BTCUSDT", "ETHUSDT"]),
             item("b", "资金", 5, apply="下一次信号起")]
    groups = C.config_groups(items)
    assert [g.name for g in groups] == ["资金", "接管"]
    a = groups[0].rows[0]
    assert (a.value_text, a.default_text, a.range_text, a.unit) == ("10", "5", "1 – 100", "USDT")
    assert a.affects_text == "影响已有持仓" and a.editable and a.kind == "decimal"
    assert groups[0].rows[1].kind == "int" and groups[0].rows[1].affects_text == "不影响已有持仓"
    sym = groups[1].rows[0]
    assert sym.kind == "symbols" and sym.value_text == "BTCUSDT, ETHUSDT" and sym.input_text == "BTCUSDT, ETHUSDT"


def test_kinds_are_inferred_and_model_3_is_read_only_with_a_note():
    rows = {r.key: r for g in C.config_groups([
        item("flag", value=True), item("mode", value="cross", choices=["cross", "isolated"]),
        item("m3_form", "模型3", "x", choices=["x", "y"], requires_deploy_switch=True),
        item("m3_max_symbols", "模型3", 0)]) for r in g.rows}
    assert rows["flag"].kind == "bool" and rows["flag"].value_text == "是"
    assert rows["mode"].kind == "choice" and rows["mode"].choices == ("cross", "isolated")
    assert not rows["m3_form"].editable and "部署级开关" in rows["m3_form"].readonly_note
    assert not rows["m3_max_symbols"].editable and "部署级开关" in rows["m3_max_symbols"].readonly_note


def test_empty_symbol_list_shows_empty_and_bad_items_are_ignored():
    rows = C.config_groups([item("takeover_symbols", value=[]), "junk", None, {"no": "key"}])[0].rows
    assert len(rows) == 1 and rows[0].value_text == "（空）" and rows[0].input_text == ""


def test_parse_symbols_accepts_commas_spaces_and_fullwidth_commas():
    row = C.config_groups([item("takeover_symbols", value=[])])[0].rows[0]
    assert C.parse_input(row, "btcusdt, ETHUSDT  solusdt，bnbusdt") == (["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"], "")
    assert C.parse_input(row, "") == ([], "")


def test_parse_other_kinds():
    rows = {r.key: r for r in C.config_groups([
        item("i", value=1), item("d", value="1.5"), item("b", value=False), item("c", value="x", choices=["x", "y"])])[0].rows}
    assert C.parse_input(rows["i"], " 12 ") == (12, "")
    assert C.parse_input(rows["i"], "1.5")[1] and C.parse_input(rows["i"], "")[1]
    assert C.parse_input(rows["d"], " 2.50 ") == ("2.50", "")
    assert C.parse_input(rows["d"], "abc")[1] and C.parse_input(rows["d"], "NaN")[1] and C.parse_input(rows["d"], "")[1]
    assert C.parse_input(rows["b"], True) == (True, "")
    assert C.parse_input(rows["c"], "y") == ("y", "") and C.parse_input(rows["c"], "z")[1]
