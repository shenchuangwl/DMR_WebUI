"""订单编号的生成与来源识别（计划报告 4.6「订单编号规则」）。

系统编号：``DK<实例>-<模型>-<意图号>-<分腿>``
  - 实例：1–4 位字母数字（设置里的实例号 + 账本建库时生成的 2 位随机串，避免换库后撞号）
  - 模型：1、2、3 是三个模型；0 是操作员与系统动作（平掉此仓、一键全平、保护单）
  - 意图号：账本里交易意图的整数编号，36 进制小写
  - 分腿：一个小写字母加 0–2 位数字，例如 o（开仓）、c（平仓）、s（止损）、c1（第 1 次重发）
不含合约名（主网有中文合约名）；不超过 36 个字符；只用字母、数字和连字符。

应急入口编号：``DKE-<毫秒时间戳36进制>-<序号>``。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from dmr_kernel.core.types import OrderOrigin

SAFE_CHARS = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
SYSTEM_PREFIX = "DK"
EMERGENCY_PREFIX = "DKE-"
CCXT_DEFAULT_PREFIX = "x-cvBPrNm9"
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9]{1,4}$")
_LEG_RE = re.compile(r"^[a-z][0-9]{0,2}$")
_SYSTEM_RE = re.compile(r"^DK([A-Za-z0-9]{1,4})-([0-3])-([0-9a-z]{1,10})-([a-z][0-9]{0,2})$")
_EMERGENCY_RE = re.compile(r"^DKE-[0-9a-z]{2,12}-[0-9]{1,6}$")
# 旧程序：f"{策略标识}_{买卖}_{持仓方向}_{毫秒时间戳}"[:36]；本拷贝的策略标识与配置模板都是 (DMR\d*|FIRST|BIT)_(LONG|SHORT)
_LEGACY_KNOWN_RE = re.compile(r"^(?:DMR\d*|FIRST|BIT)_(?:LONG|SHORT)_(?:buy|sell)(?:_[A-Za-z]*)?(?:_\d*)?$")
# 另外 6 份拷贝的策略标识未核对：按完整（未截断）格式兜底识别
_LEGACY_GENERIC_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*_(?:buy|sell)_(?:LONG|SHORT|BOTH|None)_\d{10,13}$")


def base36(n: int) -> str:
    if n < 0:
        raise ValueError("负数不能编码")
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_ALPHABET[r])
    return "".join(reversed(out))


def make_client_id(instance: str, model_code: str, intent_id: int, leg: str) -> str:
    if not _INSTANCE_RE.fullmatch(instance or ""):
        raise ValueError(f"实例号必须是 1–4 位字母数字：{instance!r}")
    if model_code not in ("0", "1", "2", "3"):
        raise ValueError(f"模型代码只能是 0–3：{model_code!r}")
    if not isinstance(intent_id, int) or intent_id < 0:
        raise ValueError(f"意图号必须是非负整数：{intent_id!r}")
    if not _LEG_RE.fullmatch(leg or ""):
        raise ValueError(f"分腿必须是一个小写字母加 0–2 位数字：{leg!r}")
    encoded = base36(intent_id)
    if len(encoded) > 10:
        raise ValueError("意图号过大")
    cid = f"{SYSTEM_PREFIX}{instance}-{model_code}-{encoded}-{leg}"
    assert len(cid) <= 36
    return cid


def make_emergency_id(ts_ms: int, seq: int) -> str:
    if seq < 0 or seq > 999999:
        raise ValueError("序号超出范围")
    return f"{EMERGENCY_PREFIX}{base36(ts_ms)}-{seq}"


def resend_leg(leg: str, attempt: int) -> str:
    """第 attempt 次重发用的分腿（实测第 13 项未出结果前，重发一律换新编号）。"""
    if not 1 <= attempt <= 99:
        raise ValueError("重发次数超出范围")
    return f"{leg[0]}{attempt}"


@dataclass(frozen=True)
class ParsedId:
    origin: OrderOrigin
    instance: str | None = None
    model_code: str | None = None
    intent_id: int | None = None
    leg: str | None = None


def parse_client_id(cid: str | None) -> ParsedId:
    if not cid:
        return ParsedId(OrderOrigin.UNKNOWN)
    if _EMERGENCY_RE.fullmatch(cid):
        return ParsedId(OrderOrigin.EMERGENCY)
    m = _SYSTEM_RE.fullmatch(cid)
    if m:
        return ParsedId(OrderOrigin.SYSTEM, m.group(1), m.group(2), int(m.group(3), 36), m.group(4))
    if cid.startswith("autoclose-"):
        return ParsedId(OrderOrigin.LIQUIDATION)
    if cid.startswith("adl_autoclose"):
        return ParsedId(OrderOrigin.ADL)
    if cid.startswith(CCXT_DEFAULT_PREFIX):
        return ParsedId(OrderOrigin.CCXT_DEFAULT)
    if _LEGACY_KNOWN_RE.fullmatch(cid) or _LEGACY_GENERIC_RE.fullmatch(cid):
        return ParsedId(OrderOrigin.LEGACY)
    return ParsedId(OrderOrigin.EXTERNAL)


def is_system_id(cid: str | None, *, instance: str | None = None) -> bool:
    parsed = parse_client_id(cid)
    return parsed.origin is OrderOrigin.SYSTEM and (instance is None or parsed.instance == instance)


def is_emergency_id(cid: str | None) -> bool:
    return parse_client_id(cid).origin is OrderOrigin.EMERGENCY
