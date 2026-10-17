"""常驻状态条：内核状态 JSON → 状态条字段（纯函数）。报告 4.10 的字段一个不少；缺数据显示「未知」而不是消失；坏数据不抛异常。"""
from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from console_kit import START_MS, make_state
from dmr_console.link import LinkStatus
from dmr_console.state import (HEARTBEAT_STALE_MS, LOST_MESSAGE, STANDIN_BANNER, STATUS_BAR_FIELDS, StatusBar,
                               build_status_bar)

UTC7 = timezone(timedelta(hours=7))
NOW = START_MS
OK = LinkStatus(lost=False, last_ok_ms=NOW)
LOST = LinkStatus(lost=True, reason="无法连接内核（ConnectError）", kind="connect", lost_since_ms=NOW - 20_000,
                  last_ok_ms=NOW - 25_000, failures=4)


def bar(state="default", link: LinkStatus = OK, *, now_ms: int = NOW, fetched_ms="default", env_hint=None, **over) -> StatusBar:
    if state == "default":
        state = make_state(**over)
    if fetched_ms == "default":
        fetched_ms = NOW - 1_000 if state is not None else None
    return build_status_bar(state, link, now_ms=now_ms, fetched_ms=fetched_ms, tz=UTC7, env_hint=env_hint)


# ------------------------------------------------------------------ 字段齐全

def test_the_report_fields_are_all_present_with_real_data():
    b = bar()
    assert tuple(c.key for c in b.chips) == STATUS_BAR_FIELDS
    assert b.has_data and not b.stale
    for field in ("model", "gate", "suppressions", "op_locks", "env", "account", "unprotected", "external", "list", "run_mode",
                  "stream", "reconcile", "position_mode", "restrictions", "margin_symbols", "seat"):
        assert field in STATUS_BAR_FIELDS


def test_the_report_fields_are_all_present_even_before_any_data_arrives():
    b = bar(None)
    assert tuple(c.key for c in b.chips) == STATUS_BAR_FIELDS
    assert not b.has_data
    assert all(c.tone == "unknown" for c in b.chips)
    assert b.updated_text == "尚无数据"


@pytest.mark.parametrize("junk", [None, 5, "x", [], [1]])
@pytest.mark.parametrize("section", ["model", "allowed", "account", "counts", "list", "stream", "reconcile", "position_mode",
                                     "restrictions", "risk", "limits", "seat", "me", "heartbeat_ms", "time_ms", "env"])
def test_garbage_in_any_section_never_raises_and_never_drops_a_field(section, junk):
    state = make_state()
    state[section] = junk
    b = build_status_bar(state, OK, now_ms=NOW, fetched_ms=NOW, tz=UTC7)
    assert tuple(c.key for c in b.chips) == STATUS_BAR_FIELDS


def test_empty_and_unknown_shapes_are_survived():
    assert tuple(c.key for c in bar({}).chips) == STATUS_BAR_FIELDS
    extra = make_state()
    extra["something_new"] = {"a": 1}
    extra["model"]["new_field"] = 1
    assert tuple(c.key for c in bar(extra).chips) == STATUS_BAR_FIELDS


def test_chip_lookup_by_key():
    b = bar()
    assert b.chip("model").label == "生效模型"
    with pytest.raises(KeyError):
        b.chip("nope")


# ------------------------------------------------------------------ 环境与横幅

def test_standin_is_marked_prominently_with_the_exact_wording():
    b = bar()
    banner = b.banner("standin")
    assert banner is not None and banner.text == "替身环境 · 非真实交易" == STANDIN_BANNER
    assert banner.tone == "warn"
    assert b.chip("env").text == "替身（standin）" and b.chip("env").tone == "warn"


def test_no_standin_banner_in_real_environments():
    b = bar(env="testnet", standin=False)
    assert b.banner("standin") is None
    assert b.chip("env").text == "测试网" and b.chip("env").tone == "info"


def test_live_environment_gets_a_danger_banner():
    b = bar(env="mainnet_live", standin=False)
    assert b.banner("standin") is None
    live = b.banner("live")
    assert live is not None and live.tone == "danger" and "主网实盘" in live.text
    assert b.chip("env").tone == "danger"


def test_shadow_environment_label():
    assert bar(env="mainnet_shadow", standin=False).chip("env").text == "主网只读影子"


def test_standin_flag_wins_even_if_env_text_is_missing():
    state = make_state()
    del state["env"]
    assert bar(state).banner("standin") is not None


def test_env_hint_from_health_marks_the_login_page_before_any_state_exists():
    assert bar(None, env_hint="standin").banner("standin") is not None
    assert bar(None, env_hint="testnet").banner("standin") is None
    assert bar(None).banner("standin") is None


# ------------------------------------------------------------------ 失联

def test_lost_contact_shows_the_exact_emergency_wording_and_marks_data_stale():
    b = bar(link=LOST)
    banner = b.banner("lost")
    assert banner is not None and banner.text == LOST_MESSAGE
    assert LOST_MESSAGE == "与内核失联：请改用命令行应急入口，或直接在 Binance 处置"
    assert banner.tone == "danger"
    assert "无法连接内核" in banner.detail and "20 秒" in banner.detail
    assert b.stale and b.has_data
    assert b.updated_text.startswith("失联前的数据")


def test_lost_contact_keeps_the_standin_banner_from_the_last_known_state():
    b = bar(link=LOST)
    assert b.banner("standin") is not None and b.banner("lost") is not None


def test_lost_contact_without_any_data():
    b = bar(None, link=LOST)
    assert b.banner("lost") is not None and not b.has_data and not b.stale


def test_connected_has_no_lost_banner_and_reports_the_update_time():
    b = bar(fetched_ms=NOW - 3_000)
    assert b.banner("lost") is None
    assert b.updated_text == "更新于 15:53:17"
    assert not b.stale


# ------------------------------------------------------------------ 逐项

@pytest.mark.parametrize("active,text", [
    ("m1", "模型 1 · 完全自动化量化交易"), ("m2", "模型 2 · 接管手动订单"), ("m3", "模型 3 · AI（预留）"), ("m9", "m9"),
])
def test_model(active, text):
    assert bar(model={"active": active}).chip("model").text == text


def test_gate_wording_matches_the_report():
    assert bar(counts={"managed": 3}).chip("gate").text == "已停止开仓 · 托管中 3 个"
    assert bar(model={"gate": "running"}, counts={"managed": 0}).chip("gate").text == "运行中 · 托管中 0 个"
    assert bar(model={"gate": "weird"}).chip("gate").text.startswith("weird · 托管中")


def test_gate_tone_and_detail():
    stopped = bar(counts={"taking_over": 1, "closing": 2}).chip("gate")
    assert stopped.tone == "info" and "接管中 1" in stopped.detail and "平仓中 2" in stopped.detail
    assert "模型状态版本 12" in stopped.detail
    assert bar(model={"gate": "running"}).chip("gate").tone == "ok"


def test_suppressions():
    none = bar().chip("suppressions")
    assert (none.text, none.tone) == ("无", "ok")
    some = bar(model={"suppressions": [{"code": "recovering", "label": "恢复中"},
                                       {"code": "reconcile_mismatch", "label": "对账不一致"}]}).chip("suppressions")
    assert some.text == "恢复中、对账不一致" and some.tone == "warn"


def test_suppression_label_falls_back_to_the_local_table_then_the_code():
    c = bar(model={"suppressions": [{"code": "risk_tripped"}, {"code": "brand_new", "label": ""}]}).chip("suppressions")
    assert c.text == "风控触发（账户级）、brand_new"


def test_op_locks_show_owner_and_remaining_time():
    c = bar(model={"op_locks": [{"code": "switch_preview", "label": "切换预览中", "owner": "admin",
                                 "expires_ms": NOW + 125_000}]}).chip("op_locks")
    assert c.tone == "warn"
    assert c.text == "切换预览中"
    assert "admin" in c.detail and "2 分 5 秒" in c.detail
    none = bar().chip("op_locks")
    assert (none.text, none.tone) == ("无", "ok")


def test_account_formats_money_and_flags_unknown():
    c = bar().chip("account")
    assert c.text == "1,000.00 / 900.00 USDT" and c.tone == "ok"
    assert "钱包余额 1,000.00" in c.detail and "2 秒前" in c.detail
    unknown = bar(account={"known": False, "equity": None, "available": None}).chip("account")
    assert (unknown.text, unknown.tone) == ("未知", "unknown")


def test_account_snapshot_that_is_too_old_is_a_warning():
    c = bar(account={"at_ms": NOW - 5 * 60_000}).chip("account")
    assert c.tone == "warn" and "5 分钟前" in c.detail


def test_unprotected_is_red_only_when_there_are_any():
    zero = bar().chip("unprotected")
    assert (zero.text, zero.tone) == ("0", "ok")
    some = bar(counts={"unprotected": 2}).chip("unprotected")
    assert (some.text, some.tone) == ("2", "danger")
    assert "登记仓" in some.detail


def test_external_and_pending_counts():
    c = bar(counts={"external": 2, "pending": 1}).chip("external")
    assert c.text == "外部 2 · 待确认 1" and c.tone == "warn"
    quiet = bar(counts={"external": 3, "pending": 0}).chip("external")
    assert quiet.text == "外部 3 · 待确认 0" and quiet.tone == "info"


def test_list_three_states():
    unwired = bar().chip("list")
    assert unwired.text == "未接入：名单同步在 M4 接入" and unwired.tone == "unknown"
    valid = bar(list={"known": True, "valid": True, "member_count": 7, "valid_until_ms": NOW + 3_600_000, "reason": ""}).chip("list")
    assert valid.text.startswith("有效（成员 7 个") and valid.tone == "ok" and "1 小时" in valid.text
    invalid = bar(list={"known": True, "valid": False, "reason": "已过期", "member_count": 0}).chip("list")
    assert invalid.text == "无效：已过期" and invalid.tone == "warn"


def test_run_mode():
    normal = bar().chip("run_mode")
    assert (normal.text, normal.tone) == ("正常", "ok")
    degraded = bar(model={"run_mode": {"code": "polling_degraded", "label": "仅轮询降级"}}).chip("run_mode")
    assert (degraded.text, degraded.tone) == ("仅轮询降级", "warn")
    unlabeled = make_state()
    unlabeled["model"]["run_mode"] = {"code": "polling_degraded"}  # 整体替换：make_state 的深度合并会留下基准里的 label
    no_label = bar(unlabeled).chip("run_mode")
    assert (no_label.text, no_label.tone) == ("仅轮询降级", "warn")


def test_stream():
    ok = bar().chip("stream")
    assert ok.text == "已连接 · 最后一帧 1 秒前" and ok.tone == "ok"
    bad = bar(stream={"state": "disconnected", "last_frame_ms": NOW - 90_000}).chip("stream")
    assert bad.tone == "danger" and bad.text.startswith("已断开")
    unknown = bar(stream={"known": False, "state": None, "last_frame_ms": None}).chip("stream")
    assert (unknown.text, unknown.tone) == ("未知", "unknown")
    odd = bar(stream={"state": "brand_new"}).chip("stream")
    assert odd.text.startswith("brand_new") and odd.tone == "warn"


def test_reconcile_shows_both_sides_and_flags_mismatch():
    ok = bar().chip("reconcile")
    assert ok.text == "持仓 4 秒前（一致）· 挂单 4 秒前（一致）" and ok.tone == "ok"
    mismatch = bar(reconcile={"positions": {"status": "mismatch"}}).chip("reconcile")
    assert mismatch.tone == "danger" and "不一致" in mismatch.text
    never = bar(reconcile={"positions": {"last_ms": None, "status": "unknown"}, "orders": {"last_ms": None, "status": "unknown"}}).chip("reconcile")
    assert never.text == "持仓 尚未对账（未知）· 挂单 尚未对账（未知）" and never.tone == "warn"


def test_position_mode():
    assert (bar().chip("position_mode").text, bar().chip("position_mode").tone) == ("双向持仓（对冲）", "ok")
    single = bar(position_mode={"dual_side": False}).chip("position_mode")
    assert single.text == "单向持仓" and single.tone == "danger"
    unknown = bar(position_mode={"known": False}).chip("position_mode")
    assert (unknown.text, unknown.tone) == ("未知", "unknown")


def test_restrictions():
    assert (bar().chip("restrictions").text, bar().chip("restrictions").tone) == ("无", "ok")
    restricted = bar(restrictions={"restricted": True}).chip("restrictions")
    assert restricted.text == "有限制" and restricted.tone == "danger"
    assert bar(restrictions={"known": False}).chip("restrictions").tone == "unknown"


def test_seat():
    none = bar().chip("seat")
    assert (none.text, none.tone) == ("无人持有", "info")
    mine = bar(seat={"holder_session_id": "s1", "holder_operator": "admin", "lease_until_ms": NOW + 30_000, "mine": True}).chip("seat")
    assert mine.text == "本会话持有（剩余 30 秒）" and mine.tone == "ok"
    other = bar(seat={"holder_session_id": "s2", "holder_operator": "bob", "lease_until_ms": NOW + 60_000, "mine": False}).chip("seat")
    assert other.text == "bob 持有（剩余 1 分钟）" and other.tone == "warn"


def test_heartbeat_fresh_and_stale():
    fresh = bar().chip("heartbeat")
    assert fresh.text == "5 秒前" and fresh.tone == "ok" and "0.1.0" in fresh.detail
    stale = bar(heartbeat_ms=NOW - HEARTBEAT_STALE_MS - 1_000).chip("heartbeat")
    assert stale.tone == "danger" and stale.text == "1 分钟前" and "90 秒" in stale.detail
    missing = bar(heartbeat_ms=None).chip("heartbeat")
    assert (missing.text, missing.tone) == ("未知", "unknown")


# ------------------------------------------------------------------ 距风控上限的余量

def test_margin_symbols_counts_used_plus_reserved_against_the_cap():
    c = bar(limits={"used": 20, "reserved": 3, "cap": 30}).chip("margin_symbols")
    assert c.text == "余 7 个（已占 20 + 预占 3 / 上限 30）" and c.tone == "ok"
    low = bar(limits={"used": 28, "reserved": 0, "cap": 30}).chip("margin_symbols")
    assert low.tone == "warn"
    full = bar(limits={"used": 25, "reserved": 5, "cap": 30}).chip("margin_symbols")
    assert full.text.startswith("余 0 个") and full.tone == "danger"
    over = bar(limits={"used": 35, "reserved": 0, "cap": 30}).chip("margin_symbols")
    assert over.text.startswith("余 0 个") and over.tone == "danger"


def test_margin_symbols_stale_snapshot_is_flagged_not_trusted():
    c = bar(limits={"stale": True}).chip("margin_symbols")
    assert c.tone == "warn" and "快照过期" in c.detail
    assert bar(limits={"cap": None}).chip("margin_symbols").tone == "unknown"


def test_risk_margins_from_the_risk_gate_status():
    b = bar()
    assert b.chip("margin_daily_loss").text == "余 50.00 USDT（限额 50.00）" and b.chip("margin_daily_loss").tone == "ok"
    assert b.chip("margin_drawdown").text == "余 10.00%（上限 10.00%）"
    assert b.chip("margin_trades").text == "余 50 次"
    assert b.chip("margin_model_loss").text == "余 50.00 USDT"
    assert b.chip("risk").text == "正常" and b.chip("risk").tone == "ok"


def test_tripped_risk_limits_are_danger_and_say_why():
    b = bar(risk={"account_tripped": True, "daily_loss_tripped": True, "daily_loss_remaining": "0",
                  "reasons": ["日亏损 60 已超限额 50"]})
    assert b.chip("margin_daily_loss").tone == "danger"
    assert b.chip("risk").tone == "danger" and "日亏损 60 已超限额 50" in b.chip("risk").text
    dd = bar(risk={"drawdown_tripped": True, "account_tripped": True, "drawdown_remaining_pct": "0.00"})
    assert dd.chip("margin_drawdown").tone == "danger"
    model = bar(risk={"model_tripped": True, "reasons": ["模型 1 当日开仓次数已达上限"]})
    assert model.chip("risk").tone == "danger"


def test_missing_risk_limits_are_unknown_with_the_config_keys_named():
    b = bar(risk={"daily_loss_limit": None, "daily_loss_remaining": None, "trades_remaining": None,
                  "limits_missing": ["daily_loss_limit", "daily_trade_limit"]})
    assert b.chip("margin_daily_loss").tone == "unknown" and b.chip("margin_daily_loss").text == "未知"
    assert b.chip("margin_trades").tone == "unknown"
    assert b.chip("risk").tone == "warn" and "daily_loss_limit" in b.chip("risk").text


def test_stale_risk_snapshot_is_a_warning():
    assert bar(risk={"stale": True}).chip("risk").tone == "warn"


def test_zero_remaining_trades_is_danger():
    assert bar(risk={"trades_remaining": 0}).chip("margin_trades").tone == "danger"
    assert bar(risk={"trades_remaining": 3}).chip("margin_trades").tone == "warn"


# ------------------------------------------------------------------ 不可变

def test_status_bar_is_immutable():
    b = bar()
    with pytest.raises(Exception):
        b.stale = True  # type: ignore[misc]
    with pytest.raises(Exception):
        b.chips[0].text = "x"  # type: ignore[misc]
