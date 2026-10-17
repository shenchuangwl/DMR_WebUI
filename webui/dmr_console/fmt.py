"""界面格式化。内核给的数值一律是字符串：这里只用 Decimal，不经过 float；任何输入都不抛异常。

时间：内核一律 UTC 毫秒，只有界面层换成本地时间显示（本机 +07）。
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any

DASH = "—"


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def fmt_money(value: Any, places: int = 2) -> str:
    """金额：四舍五入到 places 位（默认 2），带千分位。无法解析的原样返回。"""
    if _blank(value):
        return DASH
    number = _decimal(value)
    if number is None:
        return str(value)
    with localcontext() as ctx:
        ctx.prec = 80
        quantum = Decimal(1).scaleb(-places)
        rounded = number.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)  # 不显示 -0.00
    return format(rounded, ",f")


def fmt_signed_money(value: Any, places: int = 2) -> str:
    text = fmt_money(value, places)
    number = _decimal(value)
    if number is not None and text != DASH and not text.startswith("-") and number > 0:
        return "+" + text
    return text


def fmt_pct(value: Any, places: int = 2) -> str:
    text = fmt_money(value, places)
    return text if text == DASH or _decimal(value) is None else text + "%"


def fmt_number(value: Any) -> str:
    """价格、数量：保留内核给的位数，不用指数形式。无法解析的原样返回。"""
    if _blank(value):
        return DASH
    number = _decimal(value)
    if number is None:
        return str(value)
    return format(number, "f")


def sign_of(value: Any) -> int:
    number = _decimal(value)
    if number is None or number == 0:
        return 0
    return 1 if number > 0 else -1


def _moment(ms: Any, tz: tzinfo | None) -> datetime | None:
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)
    except (OverflowError, OSError, ValueError):
        return None


def fmt_time(ms: Any, tz: tzinfo | None = None) -> str:
    moment = _moment(ms, tz)
    return DASH if moment is None else moment.strftime("%Y-%m-%d %H:%M:%S")


def fmt_minute(ms: Any, tz: tzinfo | None = None) -> str:
    moment = _moment(ms, tz)
    return DASH if moment is None else moment.strftime("%Y-%m-%d %H:%M")


def fmt_clock(ms: Any, tz: tzinfo | None = None) -> str:
    moment = _moment(ms, tz)
    return DASH if moment is None else moment.strftime("%H:%M:%S")


def fmt_age(delta_ms: int | float | None) -> str:
    """距今多久（delta = 现在 − 发生时刻）。负数（时钟差）按「刚刚」。"""
    if delta_ms is None:
        return DASH
    seconds = int(delta_ms // 1000)
    if seconds < 1:
        return "刚刚"
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def fmt_remaining(delta_ms: int | float | None) -> str:
    """还剩多久（delta = 到期时刻 − 现在）。"""
    if delta_ms is None:
        return DASH
    if int(delta_ms // 1000) <= 0:
        return "已到期"
    return fmt_span(delta_ms)


def fmt_span(delta_ms: int | float | None) -> str:
    """一段时长（不带「前」「后」「已到期」）。"""
    if delta_ms is None:
        return DASH
    seconds = max(int(delta_ms // 1000), 0)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} 分钟" if rest == 0 else f"{minutes} 分 {rest} 秒"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours} 小时" if minutes == 0 else f"{hours} 小时 {minutes} 分"
