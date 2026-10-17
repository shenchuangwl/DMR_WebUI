"""故障开关与行为开关（docs/设计约定.md 7.5）：键名、取值、默认值严格照两张表；校验、计数用完自动恢复、复位。"""
from __future__ import annotations

import pytest

from dmr_standin.faults import BEHAVIOR_CHOICES, FAULT_DEFAULTS, SwitchError, Switches
from standin_kit import FakeClock

# 7.5 行为开关表：第一个取值是默认（表里的粗体）
BEHAVIOR_TABLE = {
    "close_qty_exceeds": ("reject", "truncate"),
    "close_min_notional_exempt": (False, True),
    "qty_stop_after_shrink": ("reject", "close_remaining"),
    "conditional_limit": ("per_symbol_10", "account_200"),
    "dual_close_position_stop": ("reject_4130", "allow"),
    "stop_crossed_on_create": ("reject_2021", "trigger_immediately"),
    "push_algo_events": (False, True),
    "triggered_order_link": ("unavailable", "available"),
    "duplicate_client_id_after_fill": ("accept", "reject"),
    "fill_event_order": ("account_first", "order_first"),
    "close_position_stop_autocancel_on_flat": (False, True),
    "stream_available": (True, False),
}

# 7.5 故障开关表的字段
FAULT_FIELDS = {
    "order_timeout": {"enabled", "mode", "scope", "count", "hang_s"},
    "algo_create_fail": {"enabled", "mode", "code", "count"},
    "position_query_fail": {"enabled", "mode", "count"},
    "partial_fill": {"enabled", "ratio", "remainder", "delay_s", "count"},
    "stream_drop": {"enabled", "mode", "duration_s"},
    "rate_limit": {"enabled", "http", "scope", "count"},
    "listen_key_keepalive_fail": {"enabled"},
}


def test_behavior_table_keys_values_and_defaults_match_design():
    assert BEHAVIOR_CHOICES == BEHAVIOR_TABLE
    sw = Switches(FakeClock())
    behaviors = sw.to_json()["behaviors"]
    assert behaviors == {k: v[0] for k, v in BEHAVIOR_TABLE.items()}


def test_fault_table_keys_fields_and_conservative_defaults():
    assert {k: set(v) for k, v in FAULT_DEFAULTS.items()} == FAULT_FIELDS
    faults = Switches(FakeClock()).to_json()["faults"]
    assert set(faults) == set(FAULT_FIELDS)
    assert all(f["enabled"] is False for f in faults.values())
    assert faults["order_timeout"]["mode"] == "accept_then_hang" and faults["order_timeout"]["scope"] == "market"
    assert faults["algo_create_fail"]["mode"] == "reject" and faults["algo_create_fail"]["code"] == -2010
    assert faults["position_query_fail"]["mode"] == "error"
    assert faults["partial_fill"]["remainder"] == "later"
    assert faults["stream_drop"]["mode"] == "close"
    assert faults["rate_limit"]["http"] == 429
    for key in ("order_timeout", "algo_create_fail", "position_query_fail", "partial_fill", "rate_limit"):
        assert faults[key]["count"] is None  # 空表示直到关闭


def test_update_partial_grouped_and_flat_forms():
    sw = Switches(FakeClock())
    sw.update({"faults": {"order_timeout": {"enabled": True, "mode": "drop"}},
               "behaviors": {"push_algo_events": True}})
    sw.update({"close_qty_exceeds": "truncate", "rate_limit": {"enabled": True, "http": 418}})
    js = sw.to_json()
    assert js["faults"]["order_timeout"] == {**FAULT_DEFAULTS["order_timeout"], "enabled": True, "mode": "drop"}
    assert js["faults"]["rate_limit"]["http"] == 418 and js["faults"]["rate_limit"]["scope"] == "reads"
    assert js["behaviors"]["push_algo_events"] is True
    assert js["behaviors"]["close_qty_exceeds"] == "truncate"
    assert sw.behavior("close_qty_exceeds") == "truncate"


@pytest.mark.parametrize("patch", [
    {"behaviors": {"no_such_switch": True}},
    {"no_such_switch": True},
    {"behaviors": {"close_qty_exceeds": "explode"}},
    {"behaviors": {"push_algo_events": "true"}},  # 布尔开关只认 JSON 布尔
    {"behaviors": {"push_algo_events": 1}},
    {"faults": {"order_timeout": {"mode": "slow"}}},
    {"faults": {"order_timeout": {"unknown_field": 1}}},
    {"faults": {"order_timeout": {"enabled": "yes"}}},
    {"faults": {"order_timeout": {"count": 0}}},
    {"faults": {"order_timeout": {"hang_s": -1}}},
    {"faults": {"partial_fill": {"ratio": "1.5"}}},
    {"faults": {"partial_fill": {"ratio": "0"}}},
    {"faults": {"rate_limit": {"http": 500}}},
    {"faults": {"rate_limit": {"scope": "market"}}},
    {"faults": {"stream_drop": {"duration_s": 0}}},
    {"faults": {"algo_create_fail": {"code": "x"}}},
    {"faults": "nope"},
])
def test_invalid_patches_are_rejected_without_partial_effect(patch):
    sw = Switches(FakeClock())
    before = sw.to_json()
    with pytest.raises(SwitchError):
        sw.update({**patch, "fill_event_order": "order_first"})  # 合法的一项也不能生效（整体校验后才应用）
    assert sw.to_json() == before


def test_count_consumed_then_fault_recovers_by_itself():
    sw = Switches(FakeClock())
    sw.update({"faults": {"algo_create_fail": {"enabled": True, "count": 2}}})
    assert sw.consume("algo_create_fail") is not None
    assert sw.fault("algo_create_fail")["count"] == 1
    assert sw.consume("algo_create_fail") is not None
    assert sw.fault("algo_create_fail")["enabled"] is False
    assert sw.consume("algo_create_fail") is None


def test_count_none_lasts_until_disabled_and_predicate_does_not_consume():
    sw = Switches(FakeClock())
    sw.update({"faults": {"order_timeout": {"enabled": True, "scope": "algo", "count": 1}}})
    assert sw.consume("order_timeout", lambda c: c["scope"] in ("market", "all")) is None
    assert sw.fault("order_timeout")["count"] == 1  # 不匹配的请求不计数
    sw.update({"faults": {"rate_limit": {"enabled": True}}})
    for _ in range(50):
        assert sw.consume("rate_limit") is not None
    sw.update({"faults": {"rate_limit": {"enabled": False}}})
    assert sw.consume("rate_limit") is None


def test_stream_drop_window_ends_after_duration():
    clock = FakeClock()
    sw = Switches(clock)
    assert sw.stream_drop() is None
    sw.update({"faults": {"stream_drop": {"enabled": True, "mode": "silent", "duration_s": 30}}})
    active = sw.stream_drop()
    assert active == ("silent", clock() + 30_000)
    clock.advance(29_999)
    assert sw.stream_drop() is not None
    clock.advance(1)
    assert sw.stream_drop() is None
    assert sw.fault("stream_drop")["enabled"] is False


def test_reset_restores_both_tables_and_bumps_version():
    sw = Switches(FakeClock())
    v0 = sw.version
    sw.update({"stream_available": False, "partial_fill": {"enabled": True, "ratio": "0.3"}})
    assert sw.version > v0
    sw.reset()
    fresh = Switches(FakeClock()).to_json()
    assert sw.to_json() == fresh


def test_behaviors_round_trip_for_persistence_but_faults_do_not():
    sw = Switches(FakeClock())
    sw.update({"triggered_order_link": "available", "order_timeout": {"enabled": True}})
    restored = Switches(FakeClock())
    restored.load_behaviors(sw.behaviors())
    assert restored.behavior("triggered_order_link") == "available"
    assert restored.fault("order_timeout")["enabled"] is False
