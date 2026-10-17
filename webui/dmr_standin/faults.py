"""替身交易所的故障开关与行为开关（docs/设计约定.md 7.5）。

两张表的键名、取值、默认值照抄 7.5：
- 故障开关（报告 4.11 的九种里由替身模拟的七种）：取值列表的第一个是默认；``enabled`` 默认关。
- 行为开关（报告 6.1 的实测项）：取值元组的第一个是默认，也就是表里粗体的保守取值。

修改（``update``）的格式与 ``to_json`` 相同，可以只给要改的部分::

    {"faults": {"order_timeout": {"enabled": true, "mode": "drop"}}, "behaviors": {"push_algo_events": true}}

也可以把键直接写在顶层（两张表的键不重名）：``{"order_timeout": {...}, "push_algo_events": true}``。
整个修改先全部校验、再一起生效；任何一项不合法就整体拒绝（``SwitchError``，带中文原因），已有状态不变。

- ``count``：空（None）表示直到关闭；正整数表示再生效这么多次，用完自动关闭（恢复正常）。
  只有真正命中的请求才计数（例如 ``order_timeout`` 的 scope 不匹配的请求不消耗次数）。
- ``stream_drop`` 没有 count：从打开的时刻起算 ``duration_s`` 秒，时长过后自动关闭。
- 数值字段：``ratio`` 输出为字符串（十进制小数）；秒数输出为数（整数或小数）；HTTP 状态与错误码是整数。
- 故障开关不随快照保存（替身重启即复位）；行为开关随快照保存（它模拟的是交易所本身的行为）。
"""
from __future__ import annotations

import copy
import math
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

Validator = Callable[[Any, str], Any]


class SwitchError(ValueError):
    """开关的修改不合法（原因是中文，直接返回给调用方）。"""


# ---------------------------------------------------------------- 字段校验


def _bool(value: Any, name: str) -> bool:
    if type(value) is bool:
        return value
    raise SwitchError(f"{name} 必须是 true 或 false")


def _choice(*values: Any) -> Validator:
    def check(value: Any, name: str) -> Any:
        for v in values:
            if type(value) is type(v) and value == v:
                return v
        shown = " | ".join(str(v) for v in values)
        raise SwitchError(f"{name} 只能是 {shown}")

    return check


def _count(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is int and value >= 1:
        return value
    raise SwitchError(f"{name} 必须为空（直到关闭）或正整数")


def _seconds(*, allow_zero: bool) -> Validator:
    def check(value: Any, name: str) -> int | float:
        if type(value) in (int, float) and math.isfinite(value) and (value > 0 or (allow_zero and value == 0)):
            return value
        raise SwitchError(f"{name} 必须是{'非负' if allow_zero else '正'}的秒数")

    return check


def _ratio(value: Any, name: str) -> str:
    if type(value) in (str, int, float):
        try:
            d = Decimal(str(value))
        except InvalidOperation:
            d = None
        if d is not None and d.is_finite() and Decimal(0) < d < Decimal(1):
            return format(d.normalize(), "f")
    raise SwitchError(f"{name} 必须是 0 与 1 之间（不含两端）的小数")


def _code(value: Any, name: str) -> int:
    if type(value) is int:
        return value
    raise SwitchError(f"{name} 必须是整数错误码")


# ---------------------------------------------------------------- 7.5 的两张表

FAULT_SPECS: dict[str, dict[str, Validator]] = {
    "order_timeout": {"enabled": _bool, "mode": _choice("accept_then_hang", "drop"),
                      "scope": _choice("market", "algo", "all"), "count": _count,
                      "hang_s": _seconds(allow_zero=False)},
    "algo_create_fail": {"enabled": _bool, "mode": _choice("reject", "unknown"), "code": _code, "count": _count},
    "position_query_fail": {"enabled": _bool, "mode": _choice("error", "hang"), "count": _count},
    "partial_fill": {"enabled": _bool, "ratio": _ratio, "remainder": _choice("later", "expire"),
                     "delay_s": _seconds(allow_zero=True), "count": _count},
    "stream_drop": {"enabled": _bool, "mode": _choice("close", "silent"), "duration_s": _seconds(allow_zero=False)},
    "rate_limit": {"enabled": _bool, "http": _choice(429, 418), "scope": _choice("reads", "writes", "all"),
                   "count": _count},
    "listen_key_keepalive_fail": {"enabled": _bool},
}

FAULT_DEFAULTS: dict[str, dict[str, Any]] = {
    "order_timeout": {"enabled": False, "mode": "accept_then_hang", "scope": "market", "count": None, "hang_s": 30},
    "algo_create_fail": {"enabled": False, "mode": "reject", "code": -2010, "count": None},
    "position_query_fail": {"enabled": False, "mode": "error", "count": None},
    "partial_fill": {"enabled": False, "ratio": "0.5", "remainder": "later", "delay_s": 5, "count": None},
    "stream_drop": {"enabled": False, "mode": "close", "duration_s": 60},
    "rate_limit": {"enabled": False, "http": 429, "scope": "reads", "count": None},
    "listen_key_keepalive_fail": {"enabled": False},
}

# 行为开关：键 -> 取值（第一个是默认，即 7.5 表里的粗体）；注释是对应的实测项
BEHAVIOR_CHOICES: dict[str, tuple[Any, ...]] = {
    "close_qty_exceeds": ("reject", "truncate"),  # 1：平仓数量超过持仓 −2022 | 截断
    "close_min_notional_exempt": (False, True),  # 2：平仓单是否豁免最小名义金额
    "qty_stop_after_shrink": ("reject", "close_remaining"),  # 3：数量型止损在持仓变小后触发
    "conditional_limit": ("per_symbol_10", "account_200"),  # 4：条件单数量上限
    "dual_close_position_stop": ("reject_4130", "allow"),  # 5、16：同向第二张全平型止损
    "stop_crossed_on_create": ("reject_2021", "trigger_immediately"),  # 10：建单时触发价已被越过
    "push_algo_events": (False, True),  # 11：是否推送条件单事件
    "triggered_order_link": ("unavailable", "available"),  # 12：触发后订单能否关联到条件单
    "duplicate_client_id_after_fill": ("accept", "reject"),  # 13：成交后同编号重发
    "fill_event_order": ("account_first", "order_first"),  # 14：持仓事件与订单成交事件的先后
    "close_position_stop_autocancel_on_flat": (False, True),  # 15：仓位归零后全平型止损是否自动撤销
    "stream_available": (True, False),  # 8：能否建立账户数据流
}
BEHAVIOR_DEFAULTS: dict[str, Any] = {k: v[0] for k, v in BEHAVIOR_CHOICES.items()}

assert set(FAULT_SPECS) == set(FAULT_DEFAULTS)
assert all(set(FAULT_SPECS[k]) == set(FAULT_DEFAULTS[k]) for k in FAULT_SPECS)
assert not set(FAULT_SPECS) & set(BEHAVIOR_CHOICES)


class Switches:
    """两张开关表的当前值。时钟是返回 UTC 毫秒的可调用对象（stream_drop 的时长按它计）。"""

    def __init__(self, clock: Callable[[], int]):
        self._clock = clock
        self._faults: dict[str, dict[str, Any]] = copy.deepcopy(FAULT_DEFAULTS)
        self._behaviors: dict[str, Any] = dict(BEHAVIOR_DEFAULTS)
        self._drop_since_ms: int | None = None
        self.version = 0
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------ 读取

    def behavior(self, key: str) -> Any:
        return self._behaviors[key]

    def behaviors(self) -> dict[str, Any]:
        return dict(self._behaviors)

    def fault(self, key: str) -> dict[str, Any]:
        self._expire()
        return dict(self._faults[key])

    def to_json(self) -> dict[str, Any]:
        self._expire()
        return {"faults": copy.deepcopy(self._faults), "behaviors": dict(self._behaviors)}

    def stream_drop(self) -> tuple[str, int] | None:
        """数据流断线窗口：(mode, 结束时刻毫秒)；没有打开或时长已过时为 None（过了就自动关闭）。"""
        self._expire()
        cfg = self._faults["stream_drop"]
        if not cfg["enabled"] or self._drop_since_ms is None:
            return None
        return cfg["mode"], self._drop_since_ms + int(cfg["duration_s"] * 1000)

    # ------------------------------------------------------------ 命中计数

    def consume(self, key: str, when: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any] | None:
        """故障开关打开且（when 为真）时命中一次：返回命中时的配置副本并消耗一次 count；否则返回 None。"""
        self._expire()
        cfg = self._faults[key]
        if not cfg["enabled"] or (when is not None and not when(dict(cfg))):
            return None
        hit = dict(cfg)
        if cfg.get("count") is not None:
            cfg["count"] -= 1
            if cfg["count"] <= 0:
                cfg["enabled"] = False
                cfg["count"] = None
            self._changed()
        return hit

    # ------------------------------------------------------------ 修改

    def add_listener(self, fn: Callable[[], None]) -> None:
        """开关变化后回调（数据流中心据此立即断开连接）。"""
        self._listeners.append(fn)

    def update(self, patch: Any) -> None:
        if not isinstance(patch, Mapping):
            raise SwitchError("开关修改必须是 JSON 对象")
        fault_patch: dict[str, dict[str, Any]] = {}
        behavior_patch: dict[str, Any] = {}

        def add_fault(key: str, fields: Any) -> None:
            if key not in FAULT_SPECS:
                raise SwitchError(f"未知的故障开关：{key}")
            if not isinstance(fields, Mapping):
                raise SwitchError(f"故障开关 {key} 的值必须是 JSON 对象")
            fault_patch.setdefault(key, {}).update(fields)

        def add_behavior(key: str, value: Any) -> None:
            if key not in BEHAVIOR_CHOICES:
                raise SwitchError(f"未知的行为开关：{key}")
            behavior_patch[key] = value

        for key, value in patch.items():
            if key in ("faults", "behaviors"):
                if not isinstance(value, Mapping):
                    raise SwitchError(f"{key} 必须是 JSON 对象")
                for k, v in value.items():
                    (add_fault if key == "faults" else add_behavior)(k, v)
            elif key in FAULT_SPECS:
                add_fault(key, value)
            elif key in BEHAVIOR_CHOICES:
                add_behavior(key, value)
            else:
                raise SwitchError(f"未知的开关：{key}")

        new_faults = copy.deepcopy(self._faults)
        for key, fields in fault_patch.items():
            for field, value in fields.items():
                spec = FAULT_SPECS[key].get(field)
                if spec is None:
                    raise SwitchError(f"故障开关 {key} 没有字段 {field}")
                new_faults[key][field] = spec(value, f"{key}.{field}")
        new_behaviors = dict(self._behaviors)
        for key, value in behavior_patch.items():
            new_behaviors[key] = _choice(*BEHAVIOR_CHOICES[key])(value, key)

        self._expire()
        was_dropping = self._faults["stream_drop"]["enabled"]
        self._faults, self._behaviors = new_faults, new_behaviors
        drop_patch = fault_patch.get("stream_drop", {})
        if not new_faults["stream_drop"]["enabled"]:
            self._drop_since_ms = None
        elif not was_dropping or drop_patch.get("enabled") is True:
            self._drop_since_ms = self._clock()  # 打开（或重新打开）的时刻起算
        self._changed()

    def reset(self) -> None:
        self._faults = copy.deepcopy(FAULT_DEFAULTS)
        self._behaviors = dict(BEHAVIOR_DEFAULTS)
        self._drop_since_ms = None
        self._changed()

    def load_behaviors(self, data: Any) -> None:
        """从快照恢复行为开关（不合法的值抛 SwitchError，不静默忽略）。"""
        self.update({"behaviors": data})

    # ------------------------------------------------------------ 内部

    def _expire(self) -> None:
        cfg = self._faults["stream_drop"]
        if cfg["enabled"] and self._drop_since_ms is not None:
            if self._clock() >= self._drop_since_ms + int(cfg["duration_s"] * 1000):
                cfg["enabled"] = False
                self._drop_since_ms = None
                self._changed()

    def _changed(self) -> None:
        self.version += 1
        for fn in list(self._listeners):
            fn()
