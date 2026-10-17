"""页面数据整理（纯函数，不碰 NiceGUI）：持仓按归属分组、操作日志与告警行。

内核给的是 JSON；这里把它变成页面直接显示的字符串行。数值一律走 ``fmt``（Decimal，不经 float）；
坏数据（None、数字、缺字段）不抛异常：非字典的条目被忽略，缺字段显示「—」。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any, Mapping

from dmr_console import labels as L
from dmr_console.fmt import DASH, fmt_age, fmt_number, fmt_remaining, fmt_signed_money, fmt_time, sign_of

POSITION_GROUP_KEYS = ("strategy", "mixed", "takeover", "ai", "other", "pending", "external")

GROUP_TITLES = {
    "strategy": "策略仓",
    "mixed": "混合仓",
    "takeover": "接管仓",
    "ai": "AI 仓",
    "other": "其他归属",
    "pending": "待确认仓",
    "external": "外部仓（已忽略）",
}
GROUP_HINTS = {
    "strategy": "模型 1 开的仓，由系统托管并保护",
    "mixed": "策略仓与接管仓合并而成，单列统计",
    "takeover": "手动开的仓，已按接管规则托管",
    "ai": "模型 3（预留）开的仓",
    "other": "归属字段不在已知取值内，原样列出",
    "pending": "系统还不确定怎么处理：不会自动平仓，也不会替你设止损",
    "external": "已被你忽略的外部仓：系统只读，不会动它",
}
OWNERSHIP_GROUP = {"strategy": "strategy", "takeover": "takeover", "ai": "ai"}
OPLOG_CAP = 1000
CELL_MAX_CHARS = 80


@dataclass(frozen=True)
class PositionGroup:
    key: str
    title: str
    hint: str
    rows: tuple[dict, ...]


# ------------------------------------------------------------------ 取值帮手

def _dash(text: Any) -> str:
    return text if isinstance(text, str) and text else DASH


def _flag_text(value: Any, yes: str, no: str, unknown: str = "未知") -> str:
    return yes if value is True else no if value is False else unknown


def _label(table: Mapping[str, str], code: Any) -> str:
    return L.label_of(table, code) or DASH


def _dicts(items: Any) -> list[dict]:
    return [item for item in items if isinstance(item, dict)] if isinstance(items, (list, tuple)) else []


# ------------------------------------------------------------------ 持仓

def _group_key(item: Mapping[str, Any], *, from_external: bool) -> str | None:
    """持仓归到哪一组；已平的不显示（返回 None）。登记仓里状态为待确认的也归「待确认」。"""
    status = item.get("status")
    if status == "closed":
        return None
    if status == "pending":
        return "pending"
    if status == "ignored" or from_external:
        return "external"
    if item.get("is_mixed") is True:
        return "mixed"
    ownership = item.get("ownership")
    return OWNERSHIP_GROUP.get(ownership, "other") if isinstance(ownership, str) else "other"


def _stop_text(item: Mapping[str, Any]) -> str:
    price = fmt_number(item.get("stop_price"))
    note = item.get("stop_note")
    note = note if isinstance(note, str) else ""
    if not note:
        return price
    return note if price == DASH else f"{price}（{note}）"


def _leverage_text(item: Mapping[str, Any]) -> str:
    leverage, mode = item.get("leverage"), item.get("margin_mode")
    mode_text = L.label_of(L.MARGIN_MODE_LABELS, mode)
    if leverage is None or isinstance(leverage, bool):
        return mode_text or DASH
    return f"{leverage}x {mode_text}".rstrip()


def _consistency_text(item: Mapping[str, Any]) -> str:
    consistent = item.get("consistent")
    if consistent is True:
        return "一致"
    if consistent is False:
        exchange_qty = item.get("exchange_qty")
        return f"不一致（交易所 {fmt_number(exchange_qty)}）" if exchange_qty is not None else "不一致"
    return "未知"


def _row(item: Mapping[str, Any], tz: tzinfo | None) -> dict:
    status = item.get("status")
    registered = status in L.REGISTERED_STATUSES
    protection = item.get("protection_state")
    consistent, unconfirmed = item.get("consistent"), item.get("has_unconfirmed_orders")
    opened = fmt_time(item.get("open_time_ms"), tz)
    if opened != DASH and item.get("open_time_estimated") is True:
        opened += "（估计）"
    flags = item.get("pending_flags")
    note = item.get("origin_note")
    return {
        "id": item.get("id"),
        "symbol": _dash(item.get("symbol")),
        "side": _label(L.SIDE_LABELS, item.get("side")),
        "side_code": item.get("side") if isinstance(item.get("side"), str) else "",
        "status": _label(L.POSITION_STATUS_LABELS, status),
        "status_code": status if isinstance(status, str) else "",
        "ownership": _label(L.OWNERSHIP_LABELS, item.get("ownership")),
        "qty": fmt_number(item.get("qty")),
        "avg_price": fmt_number(item.get("avg_price")),
        "mark_price": fmt_number(item.get("mark_price")),
        "pnl": fmt_signed_money(item.get("unrealized_pnl")),
        "pnl_sign": sign_of(item.get("unrealized_pnl")),
        "liquidation": fmt_number(item.get("liquidation_price")),
        "protection": _label(L.PROTECTION_LABELS, protection),
        "stop": _stop_text(item),
        "exit": _label(L.EXIT_RULESET_LABELS, item.get("exit_ruleset")),
        "consistent": _consistency_text(item),
        "unconfirmed": _flag_text(unconfirmed, "有", "无"),
        "closing": _flag_text(item.get("closing"), "是", "否"),
        "opened": opened,
        "leverage": _leverage_text(item),
        "pending_reason": _label(L.PENDING_REASON_LABELS, item.get("pending_reason")),
        "exchange_stop": _flag_text(item.get("has_stop_on_exchange"), "有", "无"),
        "flags": "、".join(str(f) for f in flags if f) if isinstance(flags, list) else "",
        "note": note if isinstance(note, str) else "",
        "version": item.get("version"),
        # 登记仓没有系统保护是最严重的情形；本地与交易所不一致、有未确认的订单要提醒
        "danger": registered and protection == "none",
        "warn": consistent is not True or unconfirmed is not False or (registered and protection == "rebuilding"),
    }


def _sort_key(row: Mapping[str, Any]) -> tuple:
    row_id = row.get("id")
    return (row["symbol"], row["side_code"], row_id if isinstance(row_id, int) and not isinstance(row_id, bool) else 0, str(row_id))


def group_positions(registered: Any, external: Any, *, tz: tzinfo | None = None) -> list[PositionGroup]:
    """``registered``：GET /positions 的 items（接管中、托管中、平仓中）；``external``：GET /positions/external 的 items
    （待确认与外部已忽略）。永远返回 7 组（可为空），顺序固定，组内按合约、方向、编号排序。"""
    buckets: dict[str, list[dict]] = {key: [] for key in POSITION_GROUP_KEYS}
    for items, from_external in ((registered, False), (external, True)):
        for item in _dicts(items):
            key = _group_key(item, from_external=from_external)
            if key is not None:
                buckets[key].append(_row(item, tz))
    return [PositionGroup(key=key, title=GROUP_TITLES[key], hint=GROUP_HINTS[key],
                          rows=tuple(sorted(buckets[key], key=_sort_key))) for key in POSITION_GROUP_KEYS]


# ------------------------------------------------------------------ 操作日志

def _json_cell(value: Any) -> tuple[str, str]:
    """（单元格里显示的文字, 完整文字）。完整文字用于悬停提示；没有值时完整文字为空。"""
    if value is None:
        return DASH, ""
    if isinstance(value, str):
        full = value
    else:
        try:
            full = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            full = str(value)
    if not full:
        return DASH, ""
    return (full if len(full) <= CELL_MAX_CHARS else full[:CELL_MAX_CHARS] + "…"), full


def oplog_rows(items: Any, *, tz: tzinfo | None = None) -> list[dict]:
    rows = []
    for item in _dicts(items):
        before, before_full = _json_cell(item.get("before"))
        after, after_full = _json_cell(item.get("after"))
        rows.append({
            "id": item.get("id"),
            "time": fmt_time(item.get("at_ms"), tz),
            "operator": _dash(item.get("operator")),
            "action": _dash(item.get("action")),
            "target": _dash(item.get("target")),
            "result": _dash(item.get("result")),
            "request_id": _dash(item.get("request_id")),
            "before": before, "before_full": before_full,
            "after": after, "after_full": after_full,
        })
    return rows


def _item_id(item: Mapping[str, Any]) -> int | None:
    value = item.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def merge_oplog(existing: Any, incoming: Any, *, cap: int = OPLOG_CAP) -> list[dict]:
    """把新取到的一页并进已有的列表：按 id 去重（同一 id 取 ``incoming`` 里的新副本）、id 倒序、最多 ``cap`` 条。
    既用于「加载更多」（并入更旧的一页），也用于刷新最新页（并入更新的记录）。没有整数 id 的条目无法排序，丢弃。"""
    merged: dict[int, dict] = {}
    for batch in (existing, incoming):
        for item in _dicts(batch):
            item_id = _item_id(item)
            if item_id is not None:
                merged[item_id] = item
    return [merged[key] for key in sorted(merged, reverse=True)[:cap]]


# ------------------------------------------------------------------ 选币名单（内核 GET /watchlist）

AVOID_NONE = "无"


def watchlist_rows(data: Any, *, tz: tzinfo | None = None) -> list[dict]:
    """名单成员行：合约、方向、分数、首次入选时间、是否已持仓、被避让的原因（原因照内核给的）。保持内核给的顺序（分数从高到低）。"""
    rows = []
    for item in _dicts(_map_of(data).get("members")):
        avoid = item.get("avoid_reason")
        avoid_text = avoid if isinstance(avoid, str) and avoid else AVOID_NONE
        rows.append({
            "symbol": _dash(item.get("symbol")),
            "side": _label(L.SIDE_LABELS, item.get("direction")),
            "score": fmt_number(item.get("score")),
            "first_selected": fmt_time(item.get("first_selected_ms"), tz),
            "held": "已持仓" if item.get("held") is True else "否",
            "avoid": avoid_text,
            "row_class": "dmr-row-warn" if avoid_text != AVOID_NONE else "",
        })
    return rows


def rejected_rows(data: Any) -> list[dict]:
    """被拒收的成员：原始代码与原因。"""
    return [{"code": _dash(item.get("code")), "reason": _dash(item.get("reason"))}
            for item in _dicts(_map_of(data).get("rejected"))]


def watchlist_summary(data: Any, *, now_ms: int, tz: tzinfo | None = None) -> dict[str, str]:
    """名单页顶部的几行字：有效与否（无效带原因）、成员数、有效期、业务日、最近拉取。"""
    if not isinstance(data, Mapping):
        return {"status": "尚无数据", "tone": "unknown", "count": DASH, "valid_until": DASH, "date": DASH, "fetched": DASH}
    valid = data.get("valid")
    reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
    if valid is True:
        status, tone = "有效", "ok"
    elif valid is False:
        status, tone = (f"无效：{reason}" if reason else "无效"), "warn"
    else:
        status, tone = "未知", "unknown"
    until = data.get("valid_until_ms")
    if isinstance(until, int) and not isinstance(until, bool):
        valid_until = f"{fmt_time(until, tz)}（{fmt_remaining(until - now_ms)}）" if until > now_ms else f"{fmt_time(until, tz)}（已到期）"
    else:
        valid_until = DASH
    fetched = data.get("fetched_ms")
    members = data.get("members")
    date = data.get("business_date")
    return {
        "status": status, "tone": tone,
        "count": f"成员 {len(_dicts(members))} 个" if isinstance(members, list) else DASH,
        "valid_until": valid_until,
        "date": date if isinstance(date, str) and date else DASH,
        "fetched": fmt_age(now_ms - fetched) if isinstance(fetched, int) and not isinstance(fetched, bool) else DASH,
    }


def _map_of(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ------------------------------------------------------------------ 告警

ALERT_TONES = {"critical": "danger", "warning": "warn", "info": "info"}


def alert_rows(items: Any, *, tz: tzinfo | None = None) -> list[dict]:
    rows = []
    for item in _dicts(items):
        level = item.get("level")
        delivered = item.get("delivered")
        rows.append({
            "id": item.get("id"),
            "time": fmt_time(item.get("at_ms"), tz),
            "level": _label(L.ALERT_LEVEL_LABELS, level),
            "tone": ALERT_TONES.get(level, "warn") if isinstance(level, str) else "warn",
            "kind": _dash(item.get("kind")),
            "message": _dash(item.get("message")),
            "delivered": _flag_text(delivered, "已送达", "未送达", DASH),
        })
    return rows

