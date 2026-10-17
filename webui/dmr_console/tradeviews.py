"""逐笔交易、统计、K 线的视图（纯函数，不碰 NiceGUI、不碰网络）。

内核的数值是字符串、时间是 UTC 毫秒；这里只用 Decimal 做格式化，时间换成本地时区显示。
任何坏数据（缺字段、类型不对、混进来的非对象）都不抛异常：该显示什么就显示破折号，坏条目跳过。
只有画图用的数值才转成 float（ECharts 只认数字），转不出来的当成没有。
"""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any

from dmr_console import labels as L
from dmr_console.fmt import DASH, fmt_minute, fmt_money, fmt_number, fmt_pct, fmt_signed_money, fmt_span, fmt_time, sign_of

DATE_HINT = "（例如 2025-10-09）"
HEADLINE_KEYS = ("profit_loss_ratio", "profit_rate", "win_rate")  # 顺序固定：盈亏比、盈利率、胜率
HEADLINE_LABELS = {"profit_loss_ratio": "盈亏比", "profit_rate": "盈利率", "win_rate": "胜率"}
DETAIL_LABELS = (("count", "笔数"), ("net_pnl", "净盈亏合计"), ("gross_pnl", "毛盈亏"), ("fees", "手续费"), ("funding", "资金费"),
                 ("avg_duration", "平均持仓时长"), ("max_win", "最大单笔盈利"), ("max_loss", "最大单笔亏损"))
GROUP_BY_KEYS = ("model", "ownership", "side", "symbol")
GROUP_TITLES = {"model": "按模型", "ownership": "按归属", "side": "按方向", "symbol": "按合约"}
_GROUP_LABELS = {"model": L.MODEL_LABELS, "ownership": L.OWNERSHIP_LABELS, "side": L.SIDE_LABELS, "symbol": {}}
_ALIASES = {
    "profit_loss_ratio": ("profit_loss_ratio", "pl_ratio", "payoff_ratio"),
    "profit_rate": ("profit_rate", "return_rate"),
    "win_rate": ("win_rate",),
    "count": ("count", "trades", "n"),
    "net_pnl": ("net_pnl", "net"),
    "gross_pnl": ("gross_pnl", "gross"),
    "fees": ("fees", "fee"),
    "funding": ("funding",),
    "avg_duration_ms": ("avg_duration_ms", "avg_hold_ms", "avg_duration"),
    "max_win": ("max_win", "max_profit"),
    "max_loss": ("max_loss",),
}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _float(value: Any) -> float | None:
    number = _decimal(value)
    if number is None:
        return None
    result = float(number)
    return result if math.isfinite(result) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ------------------------------------------------------------------ 逐笔交易

def trade_rows(data: Any, *, tz: tzinfo | None) -> list[dict[str, Any]]:
    items = _dict(data).get("items")
    rows: list[dict[str, Any]] = []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            continue
        opened = fmt_time(raw.get("open_ms"), tz)
        if raw.get("open_time_estimated") is True and opened != DASH:
            opened = "约 " + opened  # 开仓时间是估计的（接管前就有的仓）
        exit_reason = raw.get("exit_reason")
        model = raw.get("model")
        return_pct = raw.get("return_pct")
        duration = raw.get("duration_ms")
        rows.append({
            "id": raw.get("id"),
            "opened": opened,
            "closed": fmt_time(raw.get("close_ms"), tz),
            "symbol": str(raw.get("symbol", "")),
            "side": L.label_of(L.SIDE_LABELS, raw.get("side")),
            "ownership": L.label_of(L.OWNERSHIP_LABELS, raw.get("ownership")),
            "mixed": "混合仓" if raw.get("is_mixed") is True else "",
            "model": L.label_of(L.TRADE_MODEL_LABELS, model) if isinstance(model, str) and model else DASH,
            "qty": fmt_number(raw.get("max_qty")),
            "open_avg": fmt_number(raw.get("open_avg")),
            "close_avg": fmt_number(raw.get("close_avg")),
            "gross": fmt_signed_money(raw.get("gross_pnl")),
            "fees": fmt_money(raw.get("fees")),
            "funding": fmt_money(raw.get("funding")),
            "net": fmt_signed_money(raw.get("net_pnl")),
            "entry_reason": str(raw.get("entry_reason") or ""),
            "exit_reason": L.label_of(L.EXIT_REASON_LABELS, exit_reason) if exit_reason is not None else "",
            "duration": fmt_span(duration) if _decimal(duration) is not None else DASH,
            "return_pct": fmt_pct(return_pct) if return_pct is not None else DASH,
            "pnl_sign": sign_of(raw.get("net_pnl")),
            "gross_num": _float(raw.get("gross_pnl")) or 0.0,  # 只给表格排序用（显示文字是字符串，按字母序排会错）
            "net_num": _float(raw.get("net_pnl")) or 0.0,
        })
    return rows


def parse_day(text: str, tz: tzinfo | None) -> int | None:
    """``年-月-日`` → 该日本地 00:00 的 UTC 毫秒；写法不对返回 None。"""
    try:
        day = datetime.strptime(text.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    moment = day.replace(tzinfo=tz) if tz is not None else day.astimezone()
    return int(moment.timestamp() * 1000)


def trade_query(*, symbol: Any, ownership: Any, model: Any, side: Any, since: Any, until: Any,
                tz: tzinfo | None) -> tuple[dict[str, Any], str]:
    """筛选表单 → ``GET /trades`` 的参数。返回 ``(参数, 错误)``；错误非空就不要发请求。"""
    params: dict[str, Any] = {}
    text = symbol.strip().upper() if isinstance(symbol, str) else ""
    if text:
        params["symbol"] = text
    for key, value in (("ownership", ownership), ("model", model), ("side", side)):
        if isinstance(value, str) and value.strip():
            params[key] = value.strip()
    since_text = since.strip() if isinstance(since, str) else ""
    until_text = until.strip() if isinstance(until, str) else ""
    since_ms = parse_day(since_text, tz) if since_text else None
    until_ms = parse_day(until_text, tz) if until_text else None
    if since_text and since_ms is None:
        return {}, f"开始日期需要写成 年-月-日{DATE_HINT}"
    if until_text and until_ms is None:
        return {}, f"结束日期需要写成 年-月-日{DATE_HINT}"
    if since_ms is not None and until_ms is not None and since_ms > until_ms:
        return {}, "开始日期不能晚于结束日期"
    if since_ms is not None:
        params["since_ms"] = since_ms
    if until_ms is not None:
        params["until_ms"] = until_ms + 86_400_000  # 结束日期含当天：到次日 00:00（不含）
    return params, ""


# ------------------------------------------------------------------ 统计

def _pick(block: dict, name: str) -> Any:
    for alias in _ALIASES[name]:
        if alias in block:
            return block[alias]
    return None


def _ratio_text(value: Any) -> str:
    return DASH if _decimal(value) is None else fmt_money(value, 2)


def _rate_text(value: Any) -> str:
    """比率（0–1 的小数字符串）→ 百分数。"""
    number = _decimal(value)
    return DASH if number is None else fmt_pct(number * 100, 2)


def _count_text(value: Any) -> str:
    return str(value) if _int(value) is not None else DASH


def stats_headline(block: Any) -> list[dict[str, str]]:
    """三个大数字，顺序固定：盈亏比、盈利率、胜率。盈亏比为 null 时写明原因。"""
    data = _dict(block)
    count = _pick(data, "count")
    out: list[dict[str, str]] = []
    for key in HEADLINE_KEYS:
        raw = _pick(data, key)
        value = _ratio_text(raw) if key == "profit_loss_ratio" else _rate_text(raw)
        note = ""
        if not data:
            note = "尚无数据"
        elif count == 0:
            note = "还没有交易"
        elif key == "profit_loss_ratio" and raw is None:
            note = "没有亏损交易"
        out.append({"key": key, "label": HEADLINE_LABELS[key], "value": value, "note": note})
    return out


def stats_details(block: Any) -> list[dict[str, str]]:
    data = _dict(block)
    duration = _pick(data, "avg_duration_ms")
    values = {
        "count": _count_text(_pick(data, "count")),
        "net_pnl": fmt_signed_money(_pick(data, "net_pnl")),
        "gross_pnl": fmt_signed_money(_pick(data, "gross_pnl")),
        "fees": fmt_money(_pick(data, "fees")),
        "funding": fmt_money(_pick(data, "funding")),
        "avg_duration": fmt_span(duration) if _decimal(duration) is not None else DASH,
        "max_win": fmt_signed_money(_pick(data, "max_win")),
        "max_loss": fmt_signed_money(_pick(data, "max_loss")),
    }
    return [{"key": key, "label": label, "value": values[key]} for key, label in DETAIL_LABELS]


def stats_group_rows(groups: Any, group_by: str) -> list[dict[str, Any]]:
    table = _GROUP_LABELS.get(group_by, {})
    rows: list[dict[str, Any]] = []
    for item in groups if isinstance(groups, list) else []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        stats = _dict(item.get("stats"))
        net = _pick(stats, "net_pnl")
        rows.append({
            "key": (L.label_of(table, key) or DASH) if key is not None else DASH,
            "count": _count_text(_pick(stats, "count")),
            "profit_loss_ratio": _ratio_text(_pick(stats, "profit_loss_ratio")),
            "profit_rate": _rate_text(_pick(stats, "profit_rate")),
            "win_rate": _rate_text(_pick(stats, "win_rate")),
            "net_pnl": fmt_signed_money(net),
            "gross_pnl": fmt_signed_money(_pick(stats, "gross_pnl")),
            "fees": fmt_money(_pick(stats, "fees")),
            "funding": fmt_money(_pick(stats, "funding")),
            "pnl_sign": sign_of(net),
        })
    return rows


# ------------------------------------------------------------------ K 线

def _candles(data: Any) -> list[tuple[int, float, float, float, float]]:
    out: list[tuple[int, float, float, float, float]] = []
    raw = _dict(data).get("candles")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, (list, tuple)) or len(item) < 5:
            continue
        ts = _int(item[0])
        values = [_float(v) for v in item[1:5]]
        if ts is None or any(v is None for v in values):
            continue
        out.append((ts, *values))  # type: ignore[arg-type]
    out.sort(key=lambda c: c[0])
    return out


def _index_at(times: list[int], ts: Any, *, clamp: bool) -> int | None:
    stamp = _int(ts)
    if stamp is None or not times:
        return None
    idx = bisect_right(times, stamp) - 1
    if idx < 0:
        return 0 if clamp else None
    return idx


def kline_option(data: Any, *, tz: tzinfo | None) -> dict[str, Any]:
    """蜡烛图 + DMR 副图 + 进出场标记 + 止损线的 ECharts 选项。"""
    source = _dict(data)
    candles = _candles(data)
    times = [c[0] for c in candles]
    labels = [fmt_minute(t, tz) for t in times]
    dmr_by_time: dict[int, float] = {}
    for item in source.get("dmr") if isinstance(source.get("dmr"), list) else []:
        if isinstance(item, (list, tuple)) and len(item) >= 2 and _int(item[0]) is not None:
            value = _float(item[1])
            if value is not None:
                dmr_by_time[item[0]] = value
    entries: list[list[Any]] = []
    exits: list[list[Any]] = []
    for trade in source.get("trades") if isinstance(source.get("trades"), list) else []:
        if not isinstance(trade, dict):
            continue
        for ts_key, price_key, bucket in (("open_ms", "open_price", entries), ("close_ms", "close_price", exits)):
            idx, price = _index_at(times, trade.get(ts_key), clamp=False), _float(trade.get(price_key))
            if idx is not None and price is not None:
                bucket.append([labels[idx], price])
    stops: list[list[list[Any]]] = []
    for stop in source.get("stops") if isinstance(source.get("stops"), list) else []:
        if not isinstance(stop, dict):
            continue
        start, end, price = (_index_at(times, stop.get("from_ms"), clamp=True), _index_at(times, stop.get("to_ms"), clamp=True),
                             _float(stop.get("price")))
        if start is not None and end is not None and price is not None:
            stops.append([[labels[start], price], [labels[end], price]])
    series: list[dict[str, Any]] = [
        {"name": "K线", "type": "candlestick", "xAxisIndex": 0, "yAxisIndex": 0,
         "data": [[c[1], c[4], c[3], c[2]] for c in candles]},  # ECharts 次序：开、收、低、高
        {"name": "DMR", "type": "line", "xAxisIndex": 1, "yAxisIndex": 1, "showSymbol": False, "connectNulls": False,
         "data": [dmr_by_time.get(t) for t in times]},
        {"name": "开仓", "type": "scatter", "xAxisIndex": 0, "yAxisIndex": 0, "symbol": "triangle", "symbolSize": 14,
         "itemStyle": {"color": "#2e7d32"}, "data": entries},
        {"name": "平仓", "type": "scatter", "xAxisIndex": 0, "yAxisIndex": 0, "symbol": "diamond", "symbolSize": 14,
         "itemStyle": {"color": "#ef6c00"}, "data": exits},
    ]
    for number, points in enumerate(stops, start=1):
        series.append({"name": f"止损 {number}", "type": "line", "xAxisIndex": 0, "yAxisIndex": 0, "showSymbol": False,
                       "lineStyle": {"type": "dashed", "width": 1.5, "color": "#c62828"}, "itemStyle": {"color": "#c62828"},
                       "data": points})
    return {
        "animation": False,
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}},
        "legend": {"data": [s["name"] for s in series if s["type"] != "line" or s["name"] == "DMR"]},
        "grid": [{"left": 60, "right": 24, "top": 48, "height": "52%"}, {"left": 60, "right": 24, "top": "72%", "height": "16%"}],
        "xAxis": [{"type": "category", "data": labels, "gridIndex": 0, "boundaryGap": True, "axisLabel": {"show": False}},
                  {"type": "category", "data": labels, "gridIndex": 1, "boundaryGap": True}],
        "yAxis": [{"scale": True, "gridIndex": 0}, {"scale": True, "gridIndex": 1, "name": "DMR"}],
        "dataZoom": [{"type": "inside", "xAxisIndex": [0, 1]}],
        "series": series,
    }


def kline_summary(data: Any) -> str:
    source = _dict(data)
    candles = _candles(data)
    if not candles:
        return "还没有 K 线数据"
    trades = [t for t in source.get("trades", []) if isinstance(t, dict)] if isinstance(source.get("trades"), list) else []
    stops = [s for s in source.get("stops", []) if isinstance(s, dict)] if isinstance(source.get("stops"), list) else []
    opens = sum(1 for t in trades if _int(t.get("open_ms")) is not None and _float(t.get("open_price")) is not None)
    closes = sum(1 for t in trades if _int(t.get("close_ms")) is not None and _float(t.get("close_price")) is not None)
    return f"K 线 {len(candles)} 根 · 开仓标记 {opens} 个 · 平仓标记 {closes} 个 · 止损线 {len(stops)} 段"
