"""把 ``GET /state`` 的报文变成中文要点（第 7 层 cli，纯函数）。

报文形状见 docs/plans/M1-控制接口只读部分.md 3.1。命令行是应急工具：数据缺了、形状怪了都不抛异常，缺的一律显示「未知」
（并尽量带上内核给的原因）；表里没有的新代码原样显示，不吞掉也不猜。这里不导入控制台包（两个包互不导入），名称表各写一份。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, tzinfo
from typing import Any

__all__ = ["format_state"]

UNKNOWN = "未知"
STANDIN_BANNER = "替身环境 · 非真实交易"
LIVE_BANNER = "操作会动真实资金"

ENV_LABELS = {"standin": "替身（standin）", "testnet": "测试网", "mainnet_shadow": "主网只读影子",
              "mainnet_live": "主网实盘"}
MODEL_LABELS = {"m1": "模型 1 · 完全自动化量化交易", "m2": "模型 2 · 接管手动订单", "m3": "模型 3 · AI（预留）"}
GATE_LABELS = {"stopped": "已停止开仓", "running": "运行中"}
SUPPRESSION_LABELS = {
    "stream_switching": "数据流切换中", "reconcile_unknown": "对账未知", "reconcile_mismatch": "对账不一致",
    "exchange_restricted": "交易所限制", "risk_tripped": "风控触发（账户级）", "unprotected": "存在无保护持仓",
    "recovering": "恢复中", "mutex_anomaly": "互斥异常", "clock_skew": "时钟偏差"}
OP_LOCK_LABELS = {"switch_preview": "切换预览中", "switching": "切换中", "bulk_closing": "批量平仓中"}
RUN_MODE_LABELS = {"normal": "正常", "polling_degraded": "仅轮询降级"}
STREAM_LABELS = {"connected": "已连接", "connecting": "连接中", "reconnecting": "重连中", "disconnected": "已断开",
                 "stale": "数据停滞", "switching": "切换中"}
RECONCILE_LABELS = {"ok": "一致", "mismatch": "不一致", "unknown": "未知", "never": "未对账", "running": "进行中"}
COUNT_FIELDS = (("托管中", "managed"), ("无保护", "unprotected"), ("接管中", "taking_over"), ("平仓中", "closing"),
                ("待确认", "pending"), ("外部", "external"))


# ---------------------------------------------------------------- 取值帮手（坏数据一律当作没有）


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _num(value: Any) -> str | None:
    """金额、数量：报文里是字符串；整数也认。"""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, str) and value else (str(value) if isinstance(value, int) else None)


def _label(table: Mapping[str, str], code: Any) -> str | None:
    text = _str(code)
    return None if text is None else table.get(text, text)


def _span(ms: int) -> str:
    seconds = max(ms, 0) // 1000
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    return f"{seconds // 3600} 小时"


def _age(now_ms: int | None, then: Any) -> str | None:
    ms = _int(then)
    return None if now_ms is None or ms is None else f"{_span(now_ms - ms)}前"


def _clock(ms: int, tz: tzinfo | None) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000, tz=tz).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %z")
    except (OverflowError, OSError, ValueError):
        return UNKNOWN


def _reason(info: Mapping[str, Any]) -> str:
    reason = _str(info.get("reason"))
    return f"（{reason}）" if reason else ""


def _unknown(info: Any) -> str:
    return UNKNOWN + _reason(_map(info))


# ---------------------------------------------------------------- 各行


def _env_line(state: Mapping[str, Any]) -> str:
    env = _str(state.get("env"))
    standin = state.get("standin") is True or env == "standin"
    if env is None and not standin:
        return f"环境：{UNKNOWN}"
    parts = [ENV_LABELS.get(env, env) if env else ENV_LABELS["standin"]]
    if standin:
        parts.append(STANDIN_BANNER)
    elif env == "mainnet_live":
        parts.append(LIVE_BANNER)
    return "环境：" + " · ".join(parts)


def _kernel_line(state: Mapping[str, Any], now: int | None, tz: tzinfo | None) -> str:
    version = _str(state.get("kernel_version")) or UNKNOWN
    clock = UNKNOWN if now is None else _clock(now, tz)
    beat = _age(now, state.get("heartbeat_ms"))
    return f"内核：版本 {version} · 时间 {clock} · " + (f"心跳 {beat}" if beat else "心跳未知")


def _names(items: Any, table: Mapping[str, str], now: int | None, *, detail: bool = False) -> str:
    """抑制原因、操作锁：``[{code, label, ...}]`` -> 「、」连起来；空的是「无」；不是列表是「未知」。"""
    if not isinstance(items, list):
        return UNKNOWN
    names = []
    for item in items:
        info = _map(item)
        name = _str(info.get("label")) or _label(table, info.get("code")) or (item if isinstance(item, str) else UNKNOWN)
        if detail:
            extras = []
            if _str(info.get("owner")):
                extras.append(info["owner"])
            expires = _int(info.get("expires_ms"))
            if expires is not None and now is not None:
                extras.append(f"{_span(expires - now)}后到期" if expires > now else "已到期")
            if extras:
                name += f"（{'，'.join(extras)}）"
        names.append(name)
    return "、".join(names) if names else "无"


def _model_lines(state: Mapping[str, Any], now: int | None) -> list[str]:
    model = state.get("model")
    if not isinstance(model, Mapping):
        return [f"{label}：{UNKNOWN}" for label in ("生效模型", "开仓闸", "抑制原因", "操作锁", "运行方式")]
    active = _label(MODEL_LABELS, model.get("active")) or UNKNOWN
    gate_parts = [_label(GATE_LABELS, model.get("gate")) or UNKNOWN]
    managed = _int(_map(state.get("counts")).get("managed"))
    if managed is not None:
        gate_parts.append(f"托管中 {managed} 个")
    if _int(model.get("version")) is not None:
        gate_parts.append(f"模型状态版本 {model['version']}")
    run_info = _map(model.get("run_mode"))
    run_mode = _str(run_info.get("label")) or _label(RUN_MODE_LABELS, run_info.get("code")) or UNKNOWN
    return [f"生效模型：{active}", "开仓闸：" + " · ".join(gate_parts),
            "抑制原因：" + _names(model.get("suppressions"), SUPPRESSION_LABELS, now),
            "操作锁：" + _names(model.get("op_locks"), OP_LOCK_LABELS, now, detail=True), f"运行方式：{run_mode}"]


def _account_line(state: Mapping[str, Any], now: int | None) -> str:
    account = _map(state.get("account"))
    equity, available = _num(account.get("equity")), _num(account.get("available"))
    if account.get("known") is False or (equity is None and available is None):
        return "权益：" + _unknown(account)
    parts = [f"{equity or UNKNOWN} USDT", f"可用保证金 {available or UNKNOWN} USDT"]
    if _num(account.get("wallet")):
        parts.append(f"钱包余额 {_num(account.get('wallet'))}")
    age = _age(now, account.get("at_ms"))
    if age:
        parts.append(age)
    return "权益：" + " · ".join(parts)


def _counts_line(state: Mapping[str, Any]) -> str:
    counts = state.get("counts")
    if not isinstance(counts, Mapping):
        return f"持仓：{UNKNOWN}"
    parts = []
    for name, key in COUNT_FIELDS:
        value = _int(counts.get(key))
        text = f"{name} {UNKNOWN if value is None else value}"
        if key == "unprotected" and value:
            text += "（需要处理）"
        parts.append(text)
    return "持仓：" + " · ".join(parts)


def _limits_line(state: Mapping[str, Any]) -> str:
    limits = _map(state.get("limits"))
    used, reserved, cap = _int(limits.get("used")), _int(limits.get("reserved")), _int(limits.get("cap"))
    if used is None or reserved is None or cap is None:
        return f"合约数：{UNKNOWN}"
    return f"合约数：已用 {used} + 预留 {reserved} / 上限 {cap}" + ("（读数已过期）" if limits.get("stale") is True else "")


def _list_line(state: Mapping[str, Any]) -> str:
    info = _map(state.get("list"))
    reason = _str(info.get("reason"))
    if info.get("known") is False:
        return "选币名单：未接入" + (f"（{reason}）" if reason else "")
    if info.get("valid") is True:
        count = _int(info.get("member_count"))
        return "选币名单：有效" + (f"（成员 {count} 个）" if count is not None else "")
    if info.get("valid") is False:
        return "选币名单：无效" + (f"：{reason}" if reason else "")
    return f"选币名单：{UNKNOWN}"


def _stream_line(state: Mapping[str, Any], now: int | None) -> str:
    stream = _map(state.get("stream"))
    label = _label(STREAM_LABELS, stream.get("state"))
    if stream.get("known") is False or label is None:
        return "数据流：" + _unknown(stream)
    age = _age(now, stream.get("last_frame_ms"))
    return f"数据流：{label}" + (f"（最近一帧 {age}）" if age else "")


def _reconcile_part(name: str, info: Any, now: int | None) -> str:
    info = _map(info)
    label = _label(RECONCILE_LABELS, info.get("status"))
    if info.get("known") is False or label is None:
        return f"{name} {_unknown(info)}"
    age = _age(now, info.get("last_ms"))
    return f"{name} {label}" + (f"（{age}）" if age else "")


def _reconcile_line(state: Mapping[str, Any], now: int | None) -> str:
    reconcile = _map(state.get("reconcile"))
    return ("最近对账：" + _reconcile_part("持仓", reconcile.get("positions"), now) + "；"
            + _reconcile_part("挂单", reconcile.get("orders"), now))


def _flag_line(state: Mapping[str, Any], key: str, field: str, label: str, shown: Mapping[bool, str]) -> str:
    info = _map(state.get(key))
    value = info.get(field)
    if info.get("known") is False or not isinstance(value, bool):
        return f"{label}：" + _unknown(info)
    return f"{label}：{shown[value]}"


def _risk_line(state: Mapping[str, Any]) -> str:
    risk = state.get("risk")
    if not isinstance(risk, Mapping):
        return f"风控：{UNKNOWN}"
    if risk.get("account_tripped") is True or risk.get("model_tripped") is True:
        reasons = [r for r in (risk.get("reasons") if isinstance(risk.get("reasons"), list) else []) if _str(r)]
        return "风控：已触发（" + ("；".join(reasons) if reasons else "原因没有记录") + "）"
    margins = []
    if _num(risk.get("daily_loss_remaining")):
        margins.append(f"日亏损余量 {_num(risk.get('daily_loss_remaining'))}")
    if _num(risk.get("drawdown_remaining_pct")):
        margins.append(f"回撤余量 {_num(risk.get('drawdown_remaining_pct'))}%")
    if _int(risk.get("trades_remaining")) is not None:
        margins.append(f"开仓次数余量 {risk['trades_remaining']}")
    return "风控：" + " · ".join(["未触发", *margins])


def _seat_line(state: Mapping[str, Any], now: int | None) -> str:
    seat = state.get("seat")
    if not isinstance(seat, Mapping):
        return f"操作席位：{UNKNOWN}"
    holder = _str(seat.get("holder_operator"))
    if holder is None:
        return "操作席位：无人持有"
    lease = _int(seat.get("lease_until_ms"))
    left = f"，租约还剩 {_span(lease - now)}" if lease is not None and now is not None and lease > now else ""
    return f"操作席位：{holder} 持有{left}"


def _me_line(state: Mapping[str, Any]) -> str:
    me = _map(state.get("me"))
    who = _str(me.get("operator")) or UNKNOWN
    kind = {"cli": "本机令牌", "session": "登录会话"}.get(me.get("principal"), "")
    return f"当前身份：{who}" + (f"（{kind}）" if kind else "")


def format_state(state: Any, *, tz: tzinfo | None = None) -> list[str]:
    """``GET /state`` 的 JSON -> 一行一项的中文要点。``tz`` 是显示用的时区（缺省本机）。"""
    if not isinstance(state, Mapping):
        return ["内核状态", "内核返回了无法识别的状态（不是 JSON 对象）"]
    now = _int(state.get("time_ms"))
    return [
        "内核状态",
        _env_line(state),
        _kernel_line(state, now, tz),
        *_model_lines(state, now),
        _account_line(state, now),
        _counts_line(state),
        _limits_line(state),
        _list_line(state),
        _stream_line(state, now),
        _reconcile_line(state, now),
        _flag_line(state, "position_mode", "dual_side", "持仓模式", {True: "对冲（双向）", False: "单向（非对冲）"}),
        _flag_line(state, "restrictions", "restricted", "交易所限制", {True: "受限", False: "无"}),
        _risk_line(state),
        _seat_line(state, now),
        _me_line(state),
    ]
