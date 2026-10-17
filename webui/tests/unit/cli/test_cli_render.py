"""cli.render：把 ``GET /state`` 的报文变成中文要点（纯函数）。

报文形状见 docs/plans/M1-控制接口只读部分.md 3.1。命令行是应急工具：数据缺了、形状怪了都不能抛异常，缺的一律显示「未知」。
"""
from __future__ import annotations

import copy
from datetime import timedelta, timezone

import pytest

from dmr_kernel.cli.render import format_state

UTC = timezone.utc
NOW = 1_760_000_000_000  # 2025-10-09 08:53:20 UTC

STATE = {
    "env": "standin", "standin": True, "time_ms": NOW, "heartbeat_ms": NOW - 2_000, "kernel_version": "0.1.0",
    "model": {"active": "m1", "gate": "stopped", "version": 12,
              "suppressions": [{"code": "recovering", "label": "恢复中"}],
              "op_locks": [{"code": "switch_preview", "label": "切换预览中", "owner": "alice", "expires_ms": NOW + 30_000}],
              "run_mode": {"code": "normal", "label": "正常"}},
    "allowed": {"start": {"allowed": False, "reason": "…", "needs_live_read": False, "note": ""}},
    "button_meanings": {"start": "…"},
    "account": {"known": True, "equity": "1000.5", "available": "900", "wallet": "950", "at_ms": NOW - 5_000},
    "counts": {"managed": 3, "unprotected": 1, "taking_over": 0, "closing": 2, "pending": 4, "external": 5},
    "list": {"known": False, "valid": False, "reason": "名单同步在 M4 接入", "member_count": 0, "valid_until_ms": None},
    "stream": {"known": True, "state": "connected", "last_frame_ms": NOW - 1_000},
    "reconcile": {"positions": {"known": True, "last_ms": NOW - 10_000, "status": "ok"},
                  "orders": {"known": False, "last_ms": None, "status": None, "reason": "挂单对账还没跑过"}},
    "position_mode": {"known": True, "dual_side": True},
    "restrictions": {"known": True, "restricted": False},
    "risk": {"account_tripped": False, "model_tripped": False, "reasons": [], "daily_loss_remaining": "50",
             "drawdown_remaining_pct": "10.00", "trades_remaining": 48},
    "limits": {"used": 3, "reserved": 1, "cap": 30, "stale": False},
    "seat": {"holder_session_id": 7, "holder_operator": "alice", "lease_until_ms": NOW + 40_000, "mine": False},
    "me": {"operator": "cli", "principal": "cli", "reauth_valid_until_ms": None},
}


def render(state, **kw) -> str:
    return "\n".join(format_state(state, tz=UTC, **kw))


def line(text: str, label: str) -> str:
    matches = [ln for ln in text.splitlines() if ln.startswith(label + "：")]
    assert len(matches) == 1, (label, matches)
    return matches[0]


def test_a_full_state_shows_every_key_point_in_chinese():
    text = render(STATE)
    assert "替身环境 · 非真实交易" in line(text, "环境")
    assert "0.1.0" in line(text, "内核") and "2025-10-09 08:53:20" in line(text, "内核") and "2 秒前" in line(text, "内核")
    assert "模型 1" in line(text, "生效模型")
    gate = line(text, "开仓闸")
    assert "已停止开仓" in gate and "托管中 3 个" in gate and "12" in gate
    assert "恢复中" in line(text, "抑制原因")
    lock = line(text, "操作锁")
    assert "切换预览中" in lock and "alice" in lock
    assert "正常" in line(text, "运行方式")
    account = line(text, "权益")
    assert "1000.5" in account and "900" in account and "USDT" in account
    counts = line(text, "持仓")
    for fragment in ("托管中 3", "无保护 1", "接管中 0", "平仓中 2", "待确认 4", "外部 5"):
        assert fragment in counts
    assert "已用 3" in line(text, "合约数") and "预留 1" in line(text, "合约数") and "30" in line(text, "合约数")
    assert "名单同步在 M4 接入" in line(text, "选币名单")
    assert "已连接" in line(text, "数据流")
    reconcile = line(text, "最近对账")
    assert "一致" in reconcile and "挂单对账还没跑过" in reconcile
    assert "对冲" in line(text, "持仓模式")
    assert "无" in line(text, "交易所限制")
    assert "未触发" in line(text, "风控")
    seat = line(text, "操作席位")
    assert "alice" in seat and "40 秒" in seat
    assert "cli" in line(text, "当前身份")


def test_no_suppressions_no_locks_and_a_free_seat_read_as_none():
    state = copy.deepcopy(STATE)
    state["model"].update(suppressions=[], op_locks=[], gate="running")
    state["seat"] = {"holder_session_id": None, "holder_operator": None, "lease_until_ms": None, "mine": False}
    text = render(state)
    assert line(text, "抑制原因").endswith("无") and line(text, "操作锁").endswith("无")
    assert "运行中" in line(text, "开仓闸")
    assert "无人持有" in line(text, "操作席位")


@pytest.mark.parametrize("env, shown", [("testnet", "测试网"), ("mainnet_shadow", "主网只读影子"),
                                         ("mainnet_live", "主网实盘"), ("somewhere_new", "somewhere_new")])
def test_other_environments_are_named_and_only_the_standin_gets_the_banner(env, shown):
    state = copy.deepcopy(STATE)
    state.update(env=env, standin=False)
    text = render(state)
    assert shown in line(text, "环境") and "替身环境" not in text


def test_the_live_environment_is_called_out():
    state = copy.deepcopy(STATE)
    state.update(env="mainnet_live", standin=False)
    assert "真实资金" in line(render(state), "环境")


@pytest.mark.parametrize("gate, model, run_mode", [("running", "m2", "polling_degraded"), ("stopped", "m3", "normal")])
def test_models_gates_and_run_modes_have_chinese_names(gate, model, run_mode):
    state = copy.deepcopy(STATE)
    state["model"].update(gate=gate, active=model, run_mode={"code": run_mode})
    text = render(state)
    assert {"m2": "模型 2", "m3": "模型 3"}[model] in line(text, "生效模型")
    assert ("运行中" if gate == "running" else "已停止开仓") in line(text, "开仓闸")
    assert ("仅轮询降级" if run_mode == "polling_degraded" else "正常") in line(text, "运行方式")


def test_a_tripped_risk_gate_shows_why():
    state = copy.deepcopy(STATE)
    state["risk"].update(account_tripped=True, reasons=["日亏损已触发：亏损 60 ≥ 上限 50"], daily_loss_remaining="0")
    risk = line(render(state), "风控")
    assert "已触发" in risk and "日亏损已触发" in risk


def test_unprotected_positions_are_called_out():
    state = copy.deepcopy(STATE)
    state["counts"]["unprotected"] = 2
    assert "无保护 2" in line(render(state), "持仓") and "需要处理" in line(render(state), "持仓")
    state["counts"]["unprotected"] = 0
    assert "需要处理" not in line(render(state), "持仓")


def test_the_heartbeat_and_the_clock_use_the_kernels_own_time():
    state = copy.deepcopy(STATE)
    state["heartbeat_ms"] = NOW - 125_000
    assert "2 分钟前" in line(render(state), "内核")
    state["heartbeat_ms"] = None
    assert "心跳未知" in line(render(state), "内核")


def test_times_are_shown_in_the_requested_zone():
    plus7 = timezone(timedelta(hours=7))
    text = "\n".join(format_state(STATE, tz=plus7))
    assert "2025-10-09 15:53:20" in line(text, "内核") and "+0700" in line(text, "内核")


def test_known_false_pieces_say_unknown_and_why():
    state = copy.deepcopy(STATE)
    state["account"] = {"known": False, "equity": None, "available": None, "wallet": None, "at_ms": None,
                        "reason": "还没有收到账户快照"}
    state["stream"] = {"known": False, "state": None, "last_frame_ms": None, "reason": "还没有收到数据流状态"}
    state["position_mode"] = {"known": False, "dual_side": None, "reason": "还没有读过持仓模式"}
    state["restrictions"] = {"known": False, "restricted": None, "reason": "还没有读过交易所限制"}
    text = render(state)
    for label, why in (("权益", "还没有收到账户快照"), ("数据流", "还没有收到数据流状态"),
                       ("持仓模式", "还没有读过持仓模式"), ("交易所限制", "还没有读过交易所限制")):
        assert "未知" in line(text, label) and why in line(text, label)


def test_a_restricted_exchange_and_a_one_way_mode_are_shown():
    state = copy.deepcopy(STATE)
    state["restrictions"] = {"known": True, "restricted": True}
    state["position_mode"] = {"known": True, "dual_side": False}
    text = render(state)
    assert "受限" in line(text, "交易所限制") and "单向" in line(text, "持仓模式")


@pytest.mark.parametrize("state", [None, 5, "text", [1, 2], {}, {"model": None}, {"model": [], "counts": 3}])
def test_garbage_or_empty_state_never_raises_and_says_unknown(state):
    lines = format_state(state, tz=UTC)
    assert lines and all(isinstance(ln, str) for ln in lines)
    assert "未知" in "\n".join(lines) or "无法识别" in "\n".join(lines)


def test_every_field_can_be_the_wrong_type_without_an_exception():
    for key in STATE:
        for junk in (None, 7, "x", [], {"known": True}, True):
            state = copy.deepcopy(STATE)
            state[key] = junk
            assert format_state(state, tz=UTC)
    for key in STATE["model"]:
        for junk in (None, 7, "x", [], {}):
            state = copy.deepcopy(STATE)
            state["model"][key] = junk
            assert format_state(state, tz=UTC)


def test_unknown_codes_are_shown_as_they_are_rather_than_swallowed():
    state = copy.deepcopy(STATE)
    state["model"]["active"] = "m9"
    state["model"]["gate"] = "ajar"
    state["stream"]["state"] = "wobbling"
    text = render(state)
    assert "m9" in line(text, "生效模型") and "ajar" in line(text, "开仓闸") and "wobbling" in line(text, "数据流")


def test_the_output_is_plain_text_with_no_raw_json_braces():
    text = render(STATE)
    assert "{" not in text and "}" not in text and "None" not in text
