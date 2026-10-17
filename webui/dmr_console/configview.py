"""设置页的纯逻辑（不碰 NiceGUI、不碰网络）：把 ``GET /config`` 的条目整理成分组的行，并解析输入框里的文字。

内核的配置描述不带取值类型，这里按值与可选项推断：有可选项 → choice；布尔 → bool；列表 → symbols；整数 → int；其余（金额是字符串）→ decimal。
解析只做「形状」检查（整数、数字、合约代码拆分）；范围、冲突与部署级开关一律由内核校验并给出中文原因。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from dmr_console.fmt import DASH

READONLY_GROUPS = frozenset({"模型3"})
READONLY_NOTE = "改档需要部署级开关"
SYMBOL_SPLIT = re.compile(r"[,，\s]+")


@dataclass(frozen=True)
class ConfigRow:
    key: str
    label: str
    kind: str  # symbols / bool / choice / int / decimal
    value: Any
    value_text: str
    input_text: str
    default_text: str
    range_text: str
    unit: str
    apply_text: str
    affects_text: str
    choices: tuple[str, ...]
    editable: bool
    readonly_note: str
    note: str
    required_for_live: bool


@dataclass(frozen=True)
class ConfigGroup:
    name: str
    rows: tuple[ConfigRow, ...]


def _kind(value: Any, choices: tuple[str, ...]) -> str:
    if choices:
        return "choice"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, list):
        return "symbols"
    if isinstance(value, int):
        return "int"
    return "decimal"


def format_value(value: Any) -> str:
    if value is None:
        return "未设置"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "（空）"
    return str(value)


def _range_text(low: Any, high: Any) -> str:
    if low is not None and high is not None:
        return f"{low} – {high}"
    if low is not None:
        return f"≥ {low}"
    if high is not None:
        return f"≤ {high}"
    return DASH


def _input_text(kind: str, value: Any) -> str:
    if value is None:
        return ""
    if kind == "symbols":
        return ", ".join(str(v) for v in value) if isinstance(value, list) else ""
    return str(value)


def deploy_switch_on(state: Any) -> bool:
    """内核在 ``/state`` 的 ``model3.deploy_switch`` 里说部署级开关是开的才算开；没有这个字段按关（只读）。"""
    model3 = state.get("model3") if isinstance(state, Mapping) else None
    return isinstance(model3, Mapping) and model3.get("deploy_switch") is True


def _row(raw: Mapping[str, Any], deploy_switch: bool = False) -> ConfigRow | None:
    key = raw.get("key")
    if not isinstance(key, str) or not key:
        return None
    value = raw.get("value")
    raw_choices = raw.get("choices")
    choices = tuple(str(c) for c in raw_choices) if isinstance(raw_choices, list) else ()
    kind = _kind(value if value is not None else raw.get("default"), choices)
    group = raw.get("group") if isinstance(raw.get("group"), str) else "其他"
    readonly = (group in READONLY_GROUPS or raw.get("requires_deploy_switch") is True) and not deploy_switch
    note = raw.get("note")
    apply_text = raw.get("apply")
    return ConfigRow(
        key=key, label=raw["label"] if isinstance(raw.get("label"), str) and raw["label"] else key, kind=kind, value=value,
        value_text=format_value(value), input_text=_input_text(kind, value),
        default_text=format_value(raw.get("default")), range_text=_range_text(raw.get("min"), raw.get("max")),
        unit=raw["unit"] if isinstance(raw.get("unit"), str) else "",
        apply_text=apply_text if isinstance(apply_text, str) and apply_text else DASH,
        affects_text="影响已有持仓" if raw.get("affects_existing") is True else "不影响已有持仓",
        choices=choices, editable=not readonly, readonly_note=READONLY_NOTE if readonly else "",
        note=note if isinstance(note, str) else "", required_for_live=raw.get("required_for_live") is True)


def config_groups(items: Any, *, deploy_switch: bool = False) -> list[ConfigGroup]:
    """按内核给的顺序分组（组按第一次出现的顺序）。坏条目忽略。部署级开关关着（默认）时模型 3 的项一律只读。"""
    order: list[str] = []
    buckets: dict[str, list[ConfigRow]] = {}
    if not isinstance(items, (list, tuple)):
        return []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        row = _row(raw, deploy_switch)
        if row is None:
            continue
        group = raw.get("group") if isinstance(raw.get("group"), str) else "其他"
        if group not in buckets:
            order.append(group)
            buckets[group] = []
        buckets[group].append(row)
    return [ConfigGroup(name, tuple(buckets[name])) for name in order]


def parse_input(row: ConfigRow, raw: Any) -> tuple[Any, str]:
    """输入框的内容 → 要提交的值。返回 ``(值, 错误)``；错误非空时不提交。"""
    if row.kind == "bool":
        return (raw, "") if isinstance(raw, bool) else (None, "需要选择是或否")
    text = raw.strip() if isinstance(raw, str) else ""
    if row.kind == "symbols":
        return [s.upper() for s in SYMBOL_SPLIT.split(text) if s], ""
    if not text:
        return None, "不能为空"
    if row.kind == "choice":
        return (text, "") if text in row.choices else (None, "只能取 " + "、".join(row.choices))
    if row.kind == "int":
        return (int(text), "") if re.fullmatch(r"[+-]?\d+", text) else (None, "需要整数")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None, "需要数字"
    return (text, "") if number.is_finite() else (None, "需要数字")
