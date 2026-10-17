"""统计（纯函数，可复算）：在给定的逐笔交易集合上算盈亏比、盈利率、胜率等（M5，报告 8.4 第 11 项）。

书面定义（净盈亏 = 毛盈亏 − 手续费 + 资金费，已在 trade 表里）：

- **盈亏比** ``profit_loss_ratio`` = 盈利交易（净盈亏 > 0）的平均净盈亏 ÷ |亏损交易（净盈亏 < 0）的平均净盈亏|。
  没有亏损交易为 ``None``（含全是盈利或持平、也含没有交易）；有亏损但没有盈利交易为 0。
- **盈利率** ``profit_rate`` = 净盈亏合计 ÷ 开仓名义金额合计（分母只累加有开仓名义金额的交易；分母为 0 或没有交易为 ``None``）。
- **胜率** ``win_rate`` = 净盈亏 > 0 的笔数 ÷ 总笔数（没有交易为 ``None``）；持平的笔数算在总笔数里，不算赢也不算亏。
- 另给：笔数、赢、亏、净盈亏合计、毛盈亏、手续费、资金费合计、开仓名义金额合计、平均持仓时长（毫秒，四舍五入的整数，
  只算有开仓时间的交易）、最大单笔盈利（净盈亏最大的盈利交易，没有为 ``None``）与最大单笔亏损（净盈亏最小的亏损交易，负数，没有为 ``None``）。

结果字典的键顺序固定：盈亏比、盈利率、胜率在最前（控制台按这个顺序展示）。数值是 Decimal（接口出口再转字符串）。

``grouped``：按模型、归属、方向、合约分组；混合仓（``is_mixed``）不进分组，单列在 ``mixed``；``overall`` 是全部交易
（因此 overall = 各分组 + 混合仓）。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from dmr_kernel.ledger.records import TradeRecord

__all__ = ["GROUP_BY", "compute", "grouped"]

ZERO = Decimal(0)
UNKNOWN_KEY = "unknown"


def _model_key(trade: TradeRecord) -> str:
    return trade.model or UNKNOWN_KEY


GROUP_BY: dict[str, Callable[[TradeRecord], str]] = {
    "model": _model_key,
    "ownership": lambda t: t.ownership.value,
    "side": lambda t: t.side.value,
    "symbol": lambda t: t.symbol,
}


def compute(trades: Iterable[TradeRecord]) -> dict[str, Any]:
    rows = list(trades)
    wins = [t.net_pnl for t in rows if t.net_pnl > 0]
    losses = [t.net_pnl for t in rows if t.net_pnl < 0]
    net = sum((t.net_pnl for t in rows), ZERO)
    notional = sum((t.open_notional for t in rows if t.open_notional is not None), ZERO)
    durations = [t.close_ms - t.open_ms for t in rows if t.open_ms is not None]

    ratio: Decimal | None
    if not losses:
        ratio = None
    elif not wins:
        ratio = ZERO
    else:
        ratio = (sum(wins, ZERO) / len(wins)) / abs(sum(losses, ZERO) / len(losses))
    return {
        "profit_loss_ratio": ratio,
        "profit_rate": net / notional if notional else None,
        "win_rate": Decimal(len(wins)) / len(rows) if rows else None,
        "count": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "net_pnl": net,
        "gross_pnl": sum((t.gross_pnl for t in rows), ZERO),
        "fees": sum((t.fees for t in rows), ZERO),
        "funding": sum((t.funding for t in rows), ZERO),
        "open_notional": notional,
        "avg_duration_ms": (int((Decimal(sum(durations)) / len(durations)).to_integral_value(ROUND_HALF_UP))
                            if durations else None),
        "max_win": max(wins) if wins else None,
        "max_loss": min(losses) if losses else None,
    }


def grouped(trades: Iterable[TradeRecord], group_by: str) -> dict[str, Any]:
    """``{"overall": 统计, "groups": [{"key","stats"}...]（按 key 升序）, "mixed": 统计}``。"""
    if group_by not in GROUP_BY:
        raise ValueError(f"不认识的分组：{group_by!r}（可选 {'、'.join(GROUP_BY)}）")
    rows = list(trades)
    key_of = GROUP_BY[group_by]
    buckets: dict[str, list[TradeRecord]] = {}
    for trade in rows:
        if not trade.is_mixed:
            buckets.setdefault(key_of(trade), []).append(trade)
    return {
        "overall": compute(rows),
        "groups": [{"key": key, "stats": compute(buckets[key])} for key in sorted(buckets)],
        "mixed": compute(t for t in rows if t.is_mixed),
    }
