"""core.configdefs：运行期配置项的定义、默认值、取值范围与校验（计划报告 4.12）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core import configdefs as cd
from dmr_kernel.core.types import Environment

REPORT_412_KEYS = """
trade_notional_usdt leverage margin_mode max_symbols max_symbols_degraded max_orders_per_symbol_10m
daily_loss_limit max_drawdown daily_trade_limit init_stop_min_pct init_stop_max_pct stop_trigger_price_type
protect_target_s protect_retry_window_s protect_retries stop_gap_target_s stop_gap_max_s
timeframe_short timeframe_long correction_signal cooldown_bars intent_ttl_s order_unknown_grace_s
list_poll_s list_member_ttl_h require_direction_match list_max_new_per_poll list_invalid_alert_min
takeover_symbols takeover_min_notional takeover_max_notional takeover_signal_exit pending_remind_min
kline_delay_s kline_ready_deadline_s kline_missing_alert_periods
stream_silence_s stream_recover_stable_s listenkey_stale_min reconcile_mismatch_stop_s
reconcile_positions_s reconcile_orders_s clock_skew_max_ms weight_soft_limit
partial_close_pause_s close_one_timeout_s close_all_timeout_s close_all_wait_open_s shutdown_drain_s
switch_settle_s switch_preview_ttl_s reauth_window_s seat_lease_s session_idle_min
heartbeat_s heartbeat_alert_after_s
""".split()


def test_every_report_412_key_is_defined():
    missing = [k for k in REPORT_412_KEYS if k not in cd.DEFINITIONS]
    assert missing == []


def test_report_suggested_defaults():
    d = cd.DEFINITIONS
    assert d["max_symbols"].default == 30 and d["max_symbols"].max == 35
    assert d["max_symbols_degraded"].default == 10
    assert d["max_orders_per_symbol_10m"].default == 25
    assert (d["protect_target_s"].default, d["protect_retry_window_s"].default, d["protect_retries"].default) == (5, 30, 3)
    assert (d["stop_gap_target_s"].default, d["stop_gap_max_s"].default) == (3, 30)
    assert (d["timeframe_short"].default, d["timeframe_long"].default) == ("15m", "1h")
    assert d["correction_signal"].default is False
    assert d["stop_trigger_price_type"].default == "CONTRACT_PRICE"
    assert (d["reconcile_positions_s"].default, d["reconcile_orders_s"].default) == (10, 60)
    assert (d["close_one_timeout_s"].default, d["close_all_timeout_s"].default) == (15, 120)
    assert (d["switch_settle_s"].default, d["switch_preview_ttl_s"].default) == (10, 60)
    assert (d["reauth_window_s"].default, d["seat_lease_s"].default, d["session_idle_min"].default) == (300, 60, 30)
    assert d["takeover_symbols"].default == []
    assert d["require_direction_match"].default is True


def test_user_decided_items_have_no_general_default_but_have_standin_values():
    # 「待你定」的项：通用默认为空（实盘不可启动），替身环境用本轮给定的取值
    expected_standin = {
        "trade_notional_usdt": Decimal("10"), "leverage": 2, "margin_mode": "isolated",
        "daily_loss_limit": Decimal("50"), "max_drawdown": Decimal("10"), "daily_trade_limit": 50,
        "init_stop_min_pct": Decimal("1"), "init_stop_max_pct": Decimal("5"),
        "takeover_min_notional": Decimal("5"), "takeover_max_notional": Decimal("200"),
    }
    for key, value in expected_standin.items():
        assert cd.DEFINITIONS[key].default is None, key
        assert cd.DEFINITIONS[key].required_for_live, key
        assert cd.initial_values(Environment.STANDIN)[key] == value, key
    assert cd.initial_values(Environment.TESTNET)["trade_notional_usdt"] is None


def test_all_defaults_satisfy_their_own_ranges():
    for env in (Environment.STANDIN, Environment.TESTNET):
        for key, value in cd.initial_values(env).items():
            if value is not None:
                assert cd.validate_value(key, value) == value, key


@pytest.mark.parametrize("key,bad", [
    ("max_symbols", 36), ("max_symbols", 0), ("leverage", 0), ("margin_mode", "portfolio"),
    ("stop_trigger_price_type", "INDEX"), ("timeframe_short", "7m"), ("protect_retries", -1),
])
def test_out_of_range_values_are_rejected(key, bad):
    with pytest.raises(cd.ConfigError):
        cd.validate_value(key, bad)


def test_decimal_values_are_parsed_from_strings():
    assert cd.validate_value("trade_notional_usdt", "12.5") == Decimal("12.5")


def test_takeover_symbols_are_normalized_raw_codes():
    assert cd.validate_value("takeover_symbols", ["btcusdt", " ETHUSDT ", "BTCUSDT"]) == ["BTCUSDT", "ETHUSDT"]
    with pytest.raises(cd.ConfigError):
        cd.validate_value("takeover_symbols", ["BTC/USDT"])


@pytest.mark.parametrize("a,b,lev,ok", [
    (Decimal("1"), Decimal("5"), 2, True),
    (Decimal("0.3"), Decimal("5"), 2, True),
    (Decimal("0.2"), Decimal("5"), 2, False),     # A 低于 0.3% 的建议下限
    (Decimal("5"), Decimal("5"), 2, False),       # 必须 A < B
    (Decimal("0"), Decimal("5"), 2, False),       # 必须 0 < A
    (Decimal("1"), Decimal("60"), 2, False),      # B 必须小于估算的强平距离（2 倍约 49.5%）
    (Decimal("1"), Decimal("9"), 10, True),
    (Decimal("1"), Decimal("10"), 10, False),     # 10 倍强平距离约 9.5%
])
def test_stop_band_validation(a, b, lev, ok):
    problems = cd.check_stop_band(a, b, lev)
    assert (problems == []) is ok, problems


def test_cross_field_checks():
    vals = cd.initial_values(Environment.STANDIN)
    assert cd.cross_field_problems(vals) == []
    bad = dict(vals, takeover_min_notional=Decimal("300"))
    assert cd.cross_field_problems(bad)
    bad = dict(vals, max_symbols_degraded=31)
    assert cd.cross_field_problems(bad)


def test_live_start_requires_user_decided_values():
    vals = cd.initial_values(Environment.TESTNET)
    missing = cd.missing_for_live(vals)
    assert "init_stop_min_pct" in missing and "trade_notional_usdt" in missing
    assert cd.missing_for_live(cd.initial_values(Environment.STANDIN)) == []


def test_design_branch_switches_default_to_conservative_choice():
    # 由测试网实测决定的分支：两种都实现，默认取保守的一种（docs/实施记录.md）
    d = cd.DEFINITIONS
    assert d["ex_stop_update_mode"].default == "gap"            # 实测 5：不假设可并存两张全平型止损
    assert d["ex_resend_client_id"].default == "new"            # 实测 13：不假设交易所会拒绝重复编号
    assert d["ex_qty_stop_protect_rule"].default == "equal_only"  # 实测 3
    assert d["ex_close_min_notional_exempt"].default is False   # 实测 2


def test_model3_reserved_config_is_all_off_and_locked():
    d = cd.DEFINITIONS
    assert d["m3_ai_tier"].default == "off" and d["m3_exec_tier"].default == "off"
    assert d["m3_form"].default == "strategy_exec_ai_veto"
    assert d["m3_ai_tier"].requires_deploy_switch and d["m3_exec_tier"].requires_deploy_switch


def test_every_definition_documents_apply_mode_and_effect_on_positions():
    for key, defn in cd.DEFINITIONS.items():
        assert defn.apply in cd.APPLY_MODES, key
        assert isinstance(defn.affects_existing, bool), key
        assert defn.label, key


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-Infinity", Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_numbers_are_config_errors(bad):
    with pytest.raises(cd.ConfigError):
        cd.validate_value("trade_notional_usdt", bad)
