"""页面状态逻辑（纯函数，不碰 NiceGUI）：内核状态 JSON → 常驻状态条字段；按钮启用/禁用。

报告 4.10 的常驻状态条字段一个不少：数据暂时没有（内核给 ``known: false`` 或字段缺失）时显示「未知」，
不会让字段消失；内核给了意料之外的形状（None、数字、字符串……）也不抛异常。

时间：``now_ms`` 是控制台的当前时刻（所有「多久以前」都用它算，和内核在同一台机器上）；显示用的时区由调用方给。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import tzinfo
from decimal import Decimal
from typing import Any, Mapping

from dmr_console import labels as L
from dmr_console.fmt import (DASH, fmt_age, fmt_clock, fmt_money, fmt_number, fmt_pct, fmt_remaining, fmt_span, fmt_time, sign_of)
from dmr_console.link import LinkStatus
from dmr_console.operations import TAKEOVER_OPS

LOST_MESSAGE = "与内核失联：请改用命令行应急入口，或直接在 Binance 处置"
STANDIN_BANNER = "替身环境 · 非真实交易"
LIVE_BANNER = "主网实盘：操作会动真实资金"
READ_ONLY_REASON = "本版本只读：操作按钮在后续版本开放"
NO_STATE_REASON = "尚未取得内核状态"
SEAT_REASON = "需要先取得操作席位"
NO_ANSWER_REASON = "内核没有给出这个操作的可执行状态"

HEARTBEAT_STALE_MS = 90_000
ACCOUNT_STALE_MS = 60_000
LOW_SYMBOL_MARGIN = 3
LOW_COUNT_MARGIN = 3
LOW_RATIO = Decimal("0.2")

CHIP_LABELS = {
    "model": "生效模型",
    "gate": "开仓闸 · 托管",
    "suppressions": "抑制原因",
    "op_locks": "操作锁",
    "env": "环境",
    "account": "权益 / 可用保证金",
    "unprotected": "无保护持仓",
    "external": "外部 / 待确认",
    "list": "选币名单",
    "run_mode": "运行方式",
    "stream": "数据流",
    "reconcile": "最近对账",
    "position_mode": "持仓模式",
    "restrictions": "交易所限制",
    "margin_symbols": "合约数余量",
    "margin_daily_loss": "日亏损余量",
    "margin_drawdown": "回撤余量",
    "margin_trades": "开仓次数余量",
    "margin_model_loss": "模型日亏损余量",
    "risk": "风控状态",
    "seat": "操作席位",
    "heartbeat": "内核心跳",
}
STATUS_BAR_FIELDS = tuple(CHIP_LABELS)

OPERATION_ORDER = ("start", "stop", "partial_close", "close_all", "switch")
SWITCH_MEANING = "切换生效模型：先预览，再确认"


# ------------------------------------------------------------------ 取值帮手（坏数据一律当作没有）

def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001 - 任何解析失败都当作没有
        return None
    return number if number.is_finite() else None


def _age(now_ms: int, then: Any) -> str:
    ms = _int(then)
    return DASH if ms is None else fmt_age(now_ms - ms)


# ------------------------------------------------------------------ 数据结构

@dataclass(frozen=True)
class Chip:
    key: str
    label: str
    text: str
    tone: str = "info"  # ok / info / warn / danger / unknown
    detail: str = ""


@dataclass(frozen=True)
class Banner:
    key: str  # lost / standin / live
    text: str
    tone: str  # danger / warn
    detail: str = ""


@dataclass(frozen=True)
class StatusBar:
    banners: tuple[Banner, ...]
    chips: tuple[Chip, ...]
    has_data: bool
    stale: bool
    updated_text: str

    def chip(self, key: str) -> Chip:
        for chip in self.chips:
            if chip.key == key:
                return chip
        raise KeyError(key)

    def banner(self, key: str) -> Banner | None:
        return next((b for b in self.banners if b.key == key), None)


def _chip(key: str, text: str, tone: str = "info", detail: str = "") -> Chip:
    return Chip(key=key, label=CHIP_LABELS[key], text=text, tone=tone, detail=detail)


def _unknown(key: str, detail: str = "") -> Chip:
    return _chip(key, "未知", "unknown", detail)


# ------------------------------------------------------------------ 逐项

def _model_chips(state: Mapping, now_ms: int) -> list[Chip]:
    model = state.get("model")
    if not isinstance(model, Mapping):
        return [_unknown("model"), _unknown("gate"), _unknown("suppressions"), _unknown("op_locks"), _unknown("run_mode")]
    active = model.get("active")
    chips = [_chip("model", L.label_of(L.MODEL_LABELS, active), "info") if active is not None else _unknown("model")]

    counts = _map(state.get("counts"))
    managed = _int(counts.get("managed"))
    gate = model.get("gate")
    if gate is None:
        chips.append(_unknown("gate"))
    else:
        detail = [f"模型状态版本 {model['version']}"] if _int(model.get("version")) is not None else []
        for name, key in (("接管中", "taking_over"), ("平仓中", "closing")):
            if _int(counts.get(key)) is not None:
                detail.append(f"{name} {counts[key]}")
        chips.append(_chip("gate", f"{L.label_of(L.GATE_LABELS, gate)} · 托管中 {DASH if managed is None else managed} 个",
                           "ok" if gate == "running" else "info", " · ".join(detail)))

    suppressions = model.get("suppressions")
    if isinstance(suppressions, list):
        names = [_item_label(item, L.SUPPRESSION_LABELS) for item in suppressions]
        chips.append(_chip("suppressions", "、".join(names) if names else "无", "warn" if names else "ok"))
    else:
        chips.append(_unknown("suppressions"))

    locks = model.get("op_locks")
    if isinstance(locks, list):
        names = [_item_label(item, L.OP_LOCK_LABELS) for item in locks]
        details = []
        for item in locks:
            item = _map(item)
            parts = [_item_label(item, L.OP_LOCK_LABELS)]
            if _str(item.get("owner")):
                parts.append(f"持有者 {item['owner']}")
            if _int(item.get("expires_ms")) is not None:
                parts.append(f"{fmt_remaining(item['expires_ms'] - now_ms)}后到期")
            details.append("，".join(parts))
        chips.append(_chip("op_locks", "、".join(names) if names else "无", "warn" if names else "ok", "；".join(details)))
    else:
        chips.append(_unknown("op_locks"))

    run_mode = _map(model.get("run_mode"))
    code = run_mode.get("code")
    if code is None:
        chips.append(_unknown("run_mode"))
    else:
        label = _str(run_mode.get("label")) or L.label_of(L.RUN_MODE_LABELS, code)
        chips.append(_chip("run_mode", label, "ok" if code == "normal" else "warn"))
    return chips


def _item_label(item: Any, table: Mapping[str, str]) -> str:
    item = _map(item)
    return _str(item.get("label")) or L.label_of(table, item.get("code"))


def _env_chip(state: Mapping, env_hint: str | None) -> Chip:
    env = _str(state.get("env")) or (env_hint if state.get("standin") is not True else "standin")
    if env is None and state.get("standin") is True:
        env = "standin"
    if env is None:
        return _unknown("env")
    tone = {"standin": "warn", "mainnet_live": "danger"}.get(env, "info")
    return _chip("env", L.label_of(L.ENV_LABELS, env), tone)


def _account_chip(state: Mapping, now_ms: int) -> Chip:
    account = state.get("account")
    if not isinstance(account, Mapping) or account.get("known") is False:
        return _unknown("account", _str(_map(account).get("reason")) or "")
    equity, available = account.get("equity"), account.get("available")
    if equity is None and available is None:
        return _unknown("account")
    at_ms = _int(account.get("at_ms"))
    detail = f"钱包余额 {fmt_money(account.get('wallet'))} USDT"
    stale = False
    if at_ms is not None:
        detail += f" · {fmt_age(now_ms - at_ms)}"
        stale = now_ms - at_ms > ACCOUNT_STALE_MS
    return _chip("account", f"{fmt_money(equity)} / {fmt_money(available)} USDT", "warn" if stale else "ok", detail)


def _count_chips(state: Mapping) -> list[Chip]:
    counts = state.get("counts")
    if not isinstance(counts, Mapping):
        return [_unknown("unprotected"), _unknown("external")]
    unprotected = _int(counts.get("unprotected"))
    first = (_unknown("unprotected") if unprotected is None else
             _chip("unprotected", str(unprotected), "danger" if unprotected > 0 else "ok", "只统计登记仓（接管中、托管中、平仓中）"))
    external, pending = _int(counts.get("external")), _int(counts.get("pending"))
    if external is None and pending is None:
        return [first, _unknown("external")]
    text = f"外部 {DASH if external is None else external} · 待确认 {DASH if pending is None else pending}"
    return [first, _chip("external", text, "warn" if (pending or 0) > 0 else "info")]


def _list_chip(state: Mapping, now_ms: int, tz: tzinfo | None = None) -> Chip:
    info = state.get("list")
    if not isinstance(info, Mapping):
        return _unknown("list")
    reason = _str(info.get("reason")) or ""
    if info.get("known") is False:
        return _chip("list", f"未接入：{reason}" if reason else "未接入", "unknown")
    if info.get("valid") is True:
        parts = [f"成员 {info.get('member_count', DASH)} 个"]
        until = _int(info.get("valid_until_ms"))
        if until is not None:
            parts.append(f"剩余 {fmt_remaining(until - now_ms)}")
        detail = f"有效期至 {fmt_time(until, tz)}" if until is not None else ""
        return _chip("list", f"有效（{'，'.join(parts)}）", "ok", detail)
    if info.get("valid") is False:
        return _chip("list", f"无效：{reason}" if reason else "无效", "warn")
    return _unknown("list")


def _stream_chip(state: Mapping, now_ms: int) -> Chip:
    stream = state.get("stream")
    if not isinstance(stream, Mapping) or stream.get("known") is False or stream.get("state") is None:
        return _unknown("stream")
    code = stream["state"]
    text = L.label_of(L.STREAM_STATE_LABELS, code)
    if _int(stream.get("last_frame_ms")) is not None:
        text += f" · 最后一帧 {fmt_age(now_ms - stream['last_frame_ms'])}"
    tone = "ok" if code in L.STREAM_OK_STATES else "danger" if code in L.STREAM_BAD_STATES else "warn"
    return _chip("stream", text, tone)


def _reconcile_chip(state: Mapping, now_ms: int) -> Chip:
    rec = state.get("reconcile")
    if not isinstance(rec, Mapping):
        return _unknown("reconcile")
    parts: list[str] = []
    statuses: list[Any] = []
    for name, key in (("持仓", "positions"), ("挂单", "orders")):
        side = _map(rec.get(key))
        last, status = _int(side.get("last_ms")), side.get("status")
        statuses.append(status)
        when = "尚未对账" if last is None else fmt_age(now_ms - last)
        parts.append(f"{name} {when}（{L.label_of(L.RECONCILE_STATUS_LABELS, status) or '未知'}）")
    if any(s in L.RECONCILE_BAD_STATUSES for s in statuses):
        tone = "danger"
    elif all(s in L.RECONCILE_OK_STATUSES for s in statuses):
        tone = "ok"
    else:
        tone = "warn"
    return _chip("reconcile", "· ".join(parts), tone)


def _mode_chips(state: Mapping) -> list[Chip]:
    mode = state.get("position_mode")
    if not isinstance(mode, Mapping) or mode.get("known") is False or not isinstance(mode.get("dual_side"), bool):
        first = _unknown("position_mode")
    elif mode["dual_side"]:
        first = _chip("position_mode", "双向持仓（对冲）", "ok")
    else:
        first = _chip("position_mode", "单向持仓", "danger", "系统要求对冲模式（双向持仓）")
    restr = state.get("restrictions")
    if not isinstance(restr, Mapping) or restr.get("known") is False or not isinstance(restr.get("restricted"), bool):
        second = _unknown("restrictions")
    elif restr["restricted"]:
        second = _chip("restrictions", "有限制", "danger")
    else:
        second = _chip("restrictions", "无", "ok")
    return [first, second]


def _ratio_low(remaining: Decimal, limit: Decimal | None) -> bool:
    return limit is not None and limit > 0 and remaining / limit < LOW_RATIO


def _margin_symbols(state: Mapping) -> Chip:
    limits = state.get("limits")
    if not isinstance(limits, Mapping):
        return _unknown("margin_symbols")
    cap, used = _int(limits.get("cap")), _int(limits.get("used"))
    if cap is None or used is None:
        return _unknown("margin_symbols")
    reserved = _int(limits.get("reserved")) or 0
    remaining = max(cap - used - reserved, 0)
    stale = limits.get("stale") is True
    tone = "danger" if remaining == 0 else "warn" if stale or remaining <= LOW_SYMBOL_MARGIN else "ok"
    return _chip("margin_symbols", f"余 {remaining} 个（已占 {used} + 预占 {reserved} / 上限 {cap}）", tone,
                 "持仓/挂单快照过期，按未知处理，不放行新开仓" if stale else "")


def _risk_chips(state: Mapping) -> list[Chip]:
    risk = state.get("risk")
    if not isinstance(risk, Mapping):
        return [_unknown(k) for k in ("margin_daily_loss", "margin_drawdown", "margin_trades", "margin_model_loss", "risk")]
    chips: list[Chip] = []

    remaining, limit = _dec(risk.get("daily_loss_remaining")), _dec(risk.get("daily_loss_limit"))
    if remaining is None:
        chips.append(_unknown("margin_daily_loss"))
    else:
        text = f"余 {fmt_money(remaining)} USDT" + (f"（限额 {fmt_money(limit)}）" if limit is not None else "")
        danger = risk.get("daily_loss_tripped") is True or remaining <= 0
        chips.append(_chip("margin_daily_loss", text, "danger" if danger else "warn" if _ratio_low(remaining, limit) else "ok"))

    remaining, limit = _dec(risk.get("drawdown_remaining_pct")), _dec(risk.get("max_drawdown_pct"))
    if remaining is None:
        chips.append(_unknown("margin_drawdown"))
    else:
        text = f"余 {fmt_pct(remaining)}" + (f"（上限 {fmt_pct(limit)}）" if limit is not None else "")
        danger = risk.get("drawdown_tripped") is True or remaining <= 0
        chips.append(_chip("margin_drawdown", text, "danger" if danger else "warn" if _ratio_low(remaining, limit) else "ok"))

    trades = _int(risk.get("trades_remaining"))
    if trades is None:
        chips.append(_unknown("margin_trades"))
    else:
        chips.append(_chip("margin_trades", f"余 {trades} 次", "danger" if trades <= 0 else "warn" if trades <= LOW_COUNT_MARGIN else "ok"))

    remaining = _dec(risk.get("model_loss_remaining"))
    if remaining is None:
        chips.append(_unknown("margin_model_loss"))
    else:
        danger = risk.get("model_tripped") is True or remaining <= 0
        chips.append(_chip("margin_model_loss", f"余 {fmt_money(remaining)} USDT", "danger" if danger else "ok"))

    tripped = any(risk.get(k) is True for k in ("account_tripped", "daily_loss_tripped", "drawdown_tripped", "model_tripped"))
    reasons = [str(r) for r in risk.get("reasons", []) if r] if isinstance(risk.get("reasons"), list) else []
    missing = [str(k) for k in risk.get("limits_missing", [])] if isinstance(risk.get("limits_missing"), list) else []
    if tripped:
        chips.append(_chip("risk", "已触发：" + "；".join(reasons) if reasons else "已触发", "danger"))
    elif missing:
        chips.append(_chip("risk", "未配置限额：" + "、".join(missing), "warn"))
    elif risk.get("stale") is True:
        chips.append(_chip("risk", "权益快照过期，按未知处理", "warn"))
    elif risk.get("loaded") is False:
        chips.append(_chip("risk", "风控状态尚未加载", "warn"))
    else:
        chips.append(_chip("risk", "正常", "ok"))
    return chips


def _seat_chip(state: Mapping, now_ms: int) -> Chip:
    seat = state.get("seat")
    if not isinstance(seat, Mapping):
        return _unknown("seat")
    lease = _int(seat.get("lease_until_ms"))
    left = f"（剩余 {fmt_remaining(lease - now_ms)}）" if lease is not None else ""
    if seat.get("mine") is True:
        return _chip("seat", f"本会话持有{left}", "ok")
    holder = _str(seat.get("holder_operator")) or _str(seat.get("holder_session_id"))
    if holder:
        return _chip("seat", f"{holder} 持有{left}", "warn", "没有席位的会话只读")
    return _chip("seat", "无人持有", "info")


def _heartbeat_chip(state: Mapping, now_ms: int) -> Chip:
    beat = _int(state.get("heartbeat_ms"))
    if beat is None:
        return _unknown("heartbeat")
    age = now_ms - beat
    version = _str(state.get("kernel_version"))
    detail = f"内核版本 {version}" if version else ""
    if age > HEARTBEAT_STALE_MS:
        detail = (detail + "；" if detail else "") + f"内核心跳超过 {HEARTBEAT_STALE_MS // 1000} 秒未更新"
        return _chip("heartbeat", fmt_age(age), "danger", detail)
    return _chip("heartbeat", fmt_age(age), "ok", detail)


# ------------------------------------------------------------------ 总装

def build_status_bar(state: Any, link: LinkStatus, *, now_ms: int, fetched_ms: int | None, tz: tzinfo | None = None,
                     env_hint: str | None = None) -> StatusBar:
    """常驻状态条。``state`` 是最近一次 GET /state 的 JSON（还没有就 None）；失联时仍显示失联前的最后一份，并标注。"""
    has_data = isinstance(state, Mapping)
    data: Mapping[str, Any] = state if isinstance(state, Mapping) else {}
    stale = link.lost and has_data

    chips: dict[str, Chip] = {}
    model_chips = _model_chips(data, now_ms)
    chips.update({c.key: c for c in model_chips})
    chips["env"] = _env_chip(data, env_hint) if has_data else _unknown("env")
    chips["account"] = _account_chip(data, now_ms)
    for chip in _count_chips(data):
        chips[chip.key] = chip
    chips["list"] = _list_chip(data, now_ms, tz)
    chips["stream"] = _stream_chip(data, now_ms)
    chips["reconcile"] = _reconcile_chip(data, now_ms)
    for chip in _mode_chips(data):
        chips[chip.key] = chip
    chips["margin_symbols"] = _margin_symbols(data)
    for chip in _risk_chips(data):
        chips[chip.key] = chip
    chips["seat"] = _seat_chip(data, now_ms)
    chips["heartbeat"] = _heartbeat_chip(data, now_ms)

    banners: list[Banner] = []
    if link.lost:
        detail = link.reason
        if link.lost_since_ms is not None:
            detail += f"；已失联 {fmt_span(now_ms - link.lost_since_ms)}"
        banners.append(Banner("lost", LOST_MESSAGE, "danger", detail))
    env = _str(data.get("env")) or env_hint
    if data.get("standin") is True or env == "standin":
        banners.append(Banner("standin", STANDIN_BANNER, "warn"))
    elif env == "mainnet_live":
        banners.append(Banner("live", LIVE_BANNER, "danger"))

    if not has_data:
        updated = "尚无数据"
    elif stale:
        updated = "失联前的数据" + (f"（更新于 {fmt_clock(fetched_ms, tz)}）" if fetched_ms is not None else "")
    else:
        updated = f"更新于 {fmt_clock(fetched_ms, tz)}" if fetched_ms is not None else DASH
    return StatusBar(banners=tuple(banners), chips=tuple(chips[key] for key in STATUS_BAR_FIELDS), has_data=has_data,
                     stale=stale, updated_text=updated)


# ------------------------------------------------------------------ 按钮

@dataclass(frozen=True)
class ButtonState:
    key: str
    label: str
    enabled: bool
    reason: str  # 禁用的原因（中文）；启用时为空
    meaning: str  # 在当前模型下的含义
    kernel_allowed: bool | None  # 内核 allowed 表怎么说；None = 没说
    kernel_reason: str = ""


def _button_state(key: str, data: Mapping | None, link: LinkStatus, operations_enabled: bool) -> ButtonState:
    allowed = _map(data.get("allowed")) if data else {}
    meanings = _map(data.get("button_meanings")) if data else {}
    seat_mine = _map(data.get("seat")).get("mine") is True if data else False
    entry = allowed.get(key)
    kernel_allowed: bool | None
    if isinstance(entry, Mapping):
        kernel_allowed = entry.get("allowed") is True
        kernel_reason = _str(entry.get("reason")) or ""
    else:
        kernel_allowed, kernel_reason = None, ""
    meaning = _str(meanings.get(key)) or (SWITCH_MEANING if key == "switch" else "")
    if link.lost:
        reason = LOST_MESSAGE
    elif data is None:
        reason = NO_STATE_REASON
    elif not operations_enabled:
        reason = READ_ONLY_REASON
    elif kernel_allowed is None:
        reason = NO_ANSWER_REASON
    elif not kernel_allowed and not (key == "start" and _map(data.get("model")).get("active") == "m3"):
        reason = kernel_reason or "内核不允许当前执行这个操作"
    elif not seat_mine:
        reason = SEAT_REASON
    else:
        reason = ""
    return ButtonState(key=key, label=L.OPERATION_LABELS[key], enabled=reason == "", reason=reason, meaning=meaning,
                       kernel_allowed=kernel_allowed, kernel_reason=kernel_reason)


def button_states(state: Any, link: LinkStatus, *, operations_enabled: bool = False) -> dict[str, ButtonState]:
    """四个统一操作 + 模型切换的按钮状态。

    优先级：失联 → 还没有状态 → 本版本只读（operations_enabled=False）→ 内核说不可执行 → 没有操作席位。
    任何一条命中都禁用；失联时无论内核之前怎么说都禁用。
    """
    data = state if isinstance(state, Mapping) else None
    return {key: _button_state(key, data, link, operations_enabled) for key in OPERATION_ORDER}


ROW_OPERATIONS = ("partial_close", "close_one")


def row_action_states(state: Any, link: LinkStatus, *, operations_enabled: bool = False) -> dict[str, ButtonState]:
    """持仓页每行的「部分平仓」「平掉此仓」：规则与统一操作按钮相同（置灰只是提示，内核强制）。"""
    data = state if isinstance(state, Mapping) else None
    return {key: _button_state(key, data, link, operations_enabled) for key in ROW_OPERATIONS}


TAKEOVER_NEEDS_M2 = "接管只在模型 2 生效时可用（先切换到模型 2）"


def position_action_states(state: Any, link: LinkStatus, *, operations_enabled: bool = False) -> dict[str, ButtonState]:
    """持仓页接管类按钮（接管、忽略、恢复托管、取消托管、交给系统管理止损）。
    内核的可执行表里有这个操作就照它说的；没有时只做显示层的提示：失联、没状态、只读、没席位，另加「接管」需要模型 2 生效。
    置灰只是提示，内核强制。"""
    data = state if isinstance(state, Mapping) else None
    allowed = _map(data.get("allowed")) if data else {}
    active = _map(data.get("model")).get("active") if data else None
    out: dict[str, ButtonState] = {}
    for key in TAKEOVER_OPS:
        entry = allowed.get(key)
        if isinstance(entry, Mapping):
            out[key] = _button_state(key, data, link, operations_enabled)
            continue
        base = _button_state(key, data, link, operations_enabled)
        if base.reason == NO_ANSWER_REASON:  # 内核没有这个键：改按显示层规则
            seat_mine = _map(data.get("seat")).get("mine") is True if data else False
            reason = SEAT_REASON if not seat_mine else (TAKEOVER_NEEDS_M2 if key == "takeover" and active != "m2" else "")
            base = ButtonState(key=key, label=base.label, enabled=reason == "", reason=reason, meaning="", kernel_allowed=None)
        out[key] = base
    return out


def utility_enabled(link: LinkStatus) -> bool:
    """刷新、退出登录、加载更多这类不改变内核状态的按钮：只看连通性。失联时同样禁用。"""
    return not link.lost


def allowed_rows(state: Any) -> list[dict[str, str]]:
    """「状态与可执行操作」表：每个操作能不能执行、为什么。统一操作排在前面，其余按内核给的顺序。"""
    allowed = _map(state.get("allowed")) if isinstance(state, Mapping) else {}
    keys = [k for k in OPERATION_ORDER if k in allowed] + [k for k in allowed if k not in OPERATION_ORDER]
    rows = []
    for key in keys:
        entry = _map(allowed[key])
        ok = entry.get("allowed") is True
        rows.append({
            "key": str(key), "label": L.OPERATION_LABELS.get(key, str(key)), "text": "可执行" if ok else "不可执行",
            "reason": _str(entry.get("reason")) or "", "note": _str(entry.get("note")) or "", "allowed": ok,
        })
    return rows


__all__ = [
    "Banner", "ButtonState", "Chip", "LOST_MESSAGE", "STANDIN_BANNER", "StatusBar", "STATUS_BAR_FIELDS", "allowed_rows",
    "build_status_bar", "button_states", "position_action_states", "row_action_states", "ROW_OPERATIONS", "fmt_number", "sign_of", "utility_enabled",
]
