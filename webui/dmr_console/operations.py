"""统一操作的纯逻辑（不碰 NiceGUI、不碰网络）：操作号、报文构造、席位续租节奏、恢复按钮、预览与结果的整理。

报文约定见 docs/plans/M2-统一操作-测试先行计划.md 第 2 节。界面只负责把这里算好的东西画出来、把点击转成这里的报文。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from dmr_console import labels as L

PARTIAL_RATIOS = (0.25, 0.5, 0.75)
CLOSE_ALL_SCOPES = ("model", "all_managed")
SWITCH_MODES = ("keep", "close_all_first")
SWITCH_TARGETS = ("m1", "m2", "m3")
SCOPE_LABELS = {"model": "当前模型归属的持仓", "all_managed": "全部托管持仓"}
MODE_LABELS = {"keep": "保留原规则继续托管", "close_all_first": "先全部平仓再切换"}
MIN_RENEW_S = 2.0

PER_POSITION_LABELS = {
    "closed": "已平", "closed_externally": "已由外部平掉", "failed": "失败", "unknown": "未知", "skipped": "已跳过",
    "aborted": "已中止", "timeout": "超时",
}
COUNT_LABELS = {"strategy": "策略", "takeover": "接管", "ai": "AI", "none": "无归属"}

_ERROR_HINTS = {
    "stale_version": "请重新预览后再确认",
    "reauth_required": "",
    "seat_required": "",
    "not_allowed": "",
    "seat_taken": "可用「强制接管」（需口令）",
    "bad_credentials": "口令错误，请重新输入",
}
_ERROR_PREFIX = {"reauth_required": "需要重新输入口令", "seat_required": "需要先取得操作席位"}


# ------------------------------------------------------------------ 操作号

def new_op_id() -> str:
    """客户端生成的操作号：1–64 位 [A-Za-z0-9._:-]。"""
    return "c-" + uuid.uuid4().hex


class OpIds:
    """每个「槽」（一个按钮 / 一个弹窗的一次点击）一个操作号；重试同一次点击复用它，``settle`` 之后下一次点击才换新的。"""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}

    def get(self, slot: str) -> str:
        return self._ids.setdefault(slot, new_op_id())

    def settle(self, slot: str) -> None:
        self._ids.pop(slot, None)


# ------------------------------------------------------------------ 取值

def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def model_version(state: Any) -> int | None:
    return _int(_map(_map(state).get("model")).get("version"))


def position_version(position: Any) -> int | None:
    return _int(_map(position).get("version"))


def needs_recover(state: Any) -> bool:
    """内核处于「恢复中」（抑制原因里有 recovering）：显示「重新恢复」按钮。"""
    items = _map(state).get("model")
    items = _map(items).get("suppressions")
    return isinstance(items, list) and any(_map(i).get("code") == "recovering" or i == "recovering" for i in items)


# ------------------------------------------------------------------ 席位

def seat_actions(state: Any) -> tuple[str, ...]:
    """当前能做的席位动作：我持有 → 释放；无人持有 → 申请；别人持有 → 强制接管。"""
    if not isinstance(state, Mapping) or not isinstance(state.get("seat"), Mapping):
        return ()
    seat = state["seat"]
    if seat.get("mine") is True:
        return ("release",)
    if seat.get("holder_session_id") or seat.get("holder_operator"):
        return ("force",)
    return ("acquire",)


def renew_interval_s(state: Any, *, now_ms: int) -> float | None:
    """持有席位时多久续一次：当前剩余租约的一半（有下限）。没持有返回 None。"""
    seat = _map(_map(state).get("seat"))
    lease = _int(seat.get("lease_until_ms"))
    if seat.get("mine") is not True or lease is None:
        return None
    return max(MIN_RENEW_S, (lease - now_ms) / 2000)


class LeaseTracker:
    """记住整条租约的长度（`seat_lease_s`，从刚申请/续租时剩余的最大值推得），续租间隔取它的一半。"""

    def __init__(self) -> None:
        self._lease_ms = 0

    def observe(self, state: Any, *, now_ms: int) -> None:
        seat = _map(_map(state).get("seat"))
        lease = _int(seat.get("lease_until_ms"))
        if seat.get("mine") is not True or lease is None:
            self._lease_ms = 0
            return
        self._lease_ms = max(self._lease_ms, lease - now_ms)

    def interval_s(self, state: Any, *, now_ms: int) -> float | None:
        base = renew_interval_s(state, now_ms=now_ms)
        if base is None:
            return None
        return max(MIN_RENEW_S, self._lease_ms / 2000, base)


# ------------------------------------------------------------------ 报文

def _with_password(body: dict, password: str | None) -> dict:
    if password:
        body["password"] = password
    return body


def start_payload(op_id: str, model_version: int | None, *, password: str | None = None) -> dict:
    return _with_password({"op_id": op_id, "model_version": model_version}, password)


def stop_payload(op_id: str, model_version: int | None, *, close_all: bool = False, password: str | None = None) -> dict:
    body: dict[str, Any] = {"op_id": op_id, "model_version": model_version}
    if close_all:
        body["close_all"] = True
    return _with_password(body, password)


def partial_preview_payload(position_id: int, *, ratio: float | None = None, qty: str | None = None) -> dict:
    body: dict[str, Any] = {"position_id": position_id}
    if qty is not None:
        body["qty"] = qty
    else:
        body["ratio"] = ratio
    return body


def partial_confirm_payload(op_id: str, preview: Mapping[str, Any]) -> dict:
    return {"op_id": op_id, "position_id": preview.get("position_id"), "position_version": preview.get("position_version"),
            "preview_qty": preview.get("live_qty"), "close_qty": preview.get("close_qty")}


def close_one_payload(op_id: str, position_id: int, version: int | None) -> dict:
    return {"op_id": op_id, "position_id": position_id, "position_version": version}


def position_op_payload(op_id: str, position_id: int, version: int | None) -> dict:
    """接管类操作的统一报文：编号、版本都是整数。"""
    return {"op_id": op_id, "position_id": position_id, "position_version": version}


CANCEL_TAKEOVER_MODES = ("handover", "cancel_stop")
CANCEL_MODE_LABELS = {"handover": "保留止损并移交（转为外部，已忽略）", "cancel_stop": "撤掉系统止损"}
TAKEOVER_PATHS = {"takeover": "/ops/takeover", "ignore": "/ops/ignore", "restore": "/ops/restore",
                  "cancel_takeover": "/ops/cancel_takeover", "hand_stop_to_system": "/ops/hand_stop_to_system"}
TAKEOVER_OPS = tuple(TAKEOVER_PATHS)


def cancel_takeover_payload(op_id: str, position_id: int, version: int | None, mode: str) -> dict:
    if mode not in CANCEL_TAKEOVER_MODES:
        raise ValueError(f"未知的取消托管方式：{mode!r}")
    return {**position_op_payload(op_id, position_id, version), "mode": mode}


def position_actions(position: Any) -> tuple[str, ...]:
    """这个仓在持仓页上有哪些接管类按钮（只按仓自己的字段决定显示；能不能点由 /state 与内核决定）。"""
    p = _map(position)
    status, ownership = p.get("status"), p.get("ownership")
    if status == "pending":
        return ("takeover", "ignore") + (("restore",) if p.get("pending_reason") == "origin_to_verify" else ())
    if status in ("taking_over", "managed", "closing") and (ownership == "takeover" or p.get("is_mixed") is True):
        external = ownership == "takeover" and p.get("is_mixed") is not True and p.get("protection_state") == "external"
        return ("cancel_takeover",) + (("hand_stop_to_system",) if external else ())
    return ()


def close_all_preview_payload(scope: str) -> dict:
    return {"scope": scope}


def close_all_confirm_payload(op_id: str, model_version: int | None, scope: str, password: str | None) -> dict:
    return _with_password({"op_id": op_id, "model_version": model_version, "scope": scope}, password)


def abort_payload(op_id: str, target_op_id: str) -> dict:
    return {"op_id": op_id, "target_op_id": target_op_id}


def switch_preview_payload(op_id: str, target: str) -> dict:
    return {"op_id": op_id, "target": target}


def switch_confirm_payload(op_id: str, model_version: int | None, mode: str, password: str | None) -> dict:
    return _with_password({"op_id": op_id, "model_version": model_version, "mode": mode}, password)


def switch_cancel_payload(op_id: str) -> dict:
    return {"op_id": op_id}


def recover_payload(op_id: str) -> dict:
    return {"op_id": op_id}


def test_open_payload(op_id: str, symbol: str, side: str, notional: str) -> dict:
    return {"op_id": op_id, "symbol": symbol, "side": side, "notional": notional}


SHORT_CROSS_LABELS = {"up": "上穿", "down": "下穿"}
LONG_TREND_LABELS = {"bullish": "看多", "bearish": "看空", "neutral": "中性", "missing": "缺失"}


def inject_signal_payload(op_id: str, symbol: str, short_cross: str, long_trend: str) -> dict:
    """信号注入（只在替身环境）：``POST /dev/inject_signal``。"""
    return {"op_id": op_id, "symbol": symbol, "short_cross": short_cross, "long_trend": long_trend}


_PERCENT = re.compile(r"\d+(?:\.\d+)?")


def parse_custom_percent(text: Any) -> float | None:
    """自定义百分比（0–100，不含两端）→ 0–1 的比例；不合法返回 None。"""
    if not isinstance(text, str) or not _PERCENT.fullmatch(text.strip()):
        return None
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    if not Decimal(0) < value < Decimal(100):
        return None
    return float(value / 100)


# ------------------------------------------------------------------ 结果解读

def result_of(payload: Any) -> dict:
    """成功响应 ``{"op_id","status","result"}`` 里的 result；没有信封就当整个报文是结果。"""
    if not isinstance(payload, Mapping):
        return {}
    inner = payload.get("result")
    return dict(inner) if isinstance(inner, Mapping) else dict(payload)


def status_of(payload: Any) -> str:
    status = _map(payload).get("status")
    return status if isinstance(status, str) else ""


def error_text(code: str, message: str) -> str:
    prefix = _ERROR_PREFIX.get(code, "")
    hint = _ERROR_HINTS.get(code, "")
    if prefix:
        return prefix + (f"：{message}" if message else "")
    if not message:
        return f"内核拒绝了请求（{code}）"
    return f"{message}（{hint}）" if hint else message


def check_rows(result: Any) -> list[tuple[str, str, str]]:
    checks = _map(result).get("checks")
    if not isinstance(checks, list):
        return []
    return [("通过" if _map(c).get("ok") is True else "未通过", str(_map(c).get("name", "")), str(_map(c).get("detail", "") or ""))
            for c in checks]


def _position_line(position: Any) -> str:
    p = _map(position)
    side = L.label_of(L.SIDE_LABELS, p.get("side"))
    return f"{p.get('symbol', '')} {side} · 数量 {p.get('qty', '')}"


@dataclass(frozen=True)
class CloseAllView:
    model_version: int | None
    included: list[str]
    excluded: list[str]
    mixed: list[str]


def close_all_view(result: Any) -> CloseAllView:
    r = _map(result)
    included = r.get("included") if isinstance(r.get("included"), list) else []
    excluded = r.get("excluded") if isinstance(r.get("excluded"), list) else []
    mixed = r.get("mixed_included") if isinstance(r.get("mixed_included"), list) else []
    return CloseAllView(
        model_version=_int(r.get("model_version")),
        included=[_position_line(p) for p in included],
        excluded=[f"{_position_line(_map(e).get('position'))}：{_map(e).get('reason', '')}" for e in excluded],
        mixed=[_position_line(p) for p in mixed],
    )


def per_position_rows(items: Any) -> list[dict]:
    if not isinstance(items, list):
        return []
    rows = []
    for item in items:
        i = _map(item)
        rows.append({"position_id": i.get("position_id"), "symbol": str(i.get("symbol", "")),
                     "side": L.label_of(L.SIDE_LABELS, i.get("side")),
                     "result": L.label_of(PER_POSITION_LABELS, i.get("result")), "detail": str(i.get("detail", "") or "")})
    return rows


@dataclass(frozen=True)
class SwitchView:
    model_version: int | None
    target: str
    counts_text: str
    open_orders: int
    notes: list[str]


def switch_view(result: Any, *, target: str) -> SwitchView:
    r = _map(result)
    counts = _map(r.get("counts_by_ownership"))
    text = "，".join(f"{L.label_of(COUNT_LABELS, k)} {v}" for k, v in counts.items()) or "无持仓"
    orders = r.get("open_orders")
    notes = r.get("notes")
    return SwitchView(model_version=_int(r.get("model_version")), target=target, counts_text=text,
                      open_orders=len(orders) if isinstance(orders, list) else 0,
                      notes=[str(n) for n in notes] if isinstance(notes, list) else [])


def intent_result_text(status: str, result: Any) -> str:
    """开发页（测试造仓、信号注入、影子意图）按内核的实际返回写一句话：意图状态、拒绝原因；不一律写「已受理」。"""
    data = _map(result)
    state = _str_of(data.get("state"))
    reason = _str_of(data.get("reason"))
    parts: list[str] = []
    if data.get("submitted") is False:
        parts.append("未提交意图")
    elif status == "failed":
        parts.append("被拒绝或失败")
    elif status == "unknown":
        parts.append("结果未知（超时未到终态）")
    elif status == "succeeded":
        parts.append("已完成")
    if state:
        parts.append(f"意图状态 {state}")
    if reason:
        parts.append(f"原因：{reason}")
    if not parts:
        parts.append(f"内核返回 {status or '无状态'}")
    return "，".join(parts)


def _str_of(value: Any) -> str:
    return value if isinstance(value, str) else ""
