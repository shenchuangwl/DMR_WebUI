"""报文里的 JSON 结构（第 7 层 api）：账本记录、交易所数据 -> 字典。纯函数，不碰账本、网络与时钟。

约定（docs/plans/M1-控制接口只读部分.md）：金额数量一律字符串（不用科学计数法），时间是 UTC 毫秒整数，可能缺的字段给 null，
枚举取值、不省略字段。``jsonable`` 是出口前的最后一道：认识的类型转成 JSON 原生类型，不认识的直接报错，不去猜。
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from decimal import Decimal
from enum import Enum
from typing import Any

from dmr_kernel.core.events import OrdersSnapshotTaken
from dmr_kernel.core.types import (
    AlgoOrderInfo,
    OrderInfo,
    OrderState,
    PositionInfo,
    PositionSide,
    PositionStatus,
    RunMode,
)
from dmr_kernel.ledger.records import AlertRecord, AuditLogRecord, OrderRecord, PositionRecord

__all__ = [
    "RUN_MODE_LABELS", "alert_view", "audit_view", "enum_value", "exchange_algo_view", "exchange_order_view",
    "jsonable", "order_view", "plain", "position_view", "stop_on_exchange", "unconfirmed_keys",
]

RUN_MODE_LABELS: dict[RunMode, str] = {RunMode.NORMAL: "正常", RunMode.POLLING_DEGRADED: "仅轮询降级"}
# 命运未定的订单状态：发出了不知道结果、撤单了不知道结果、还没发出
_UNCONFIRMED = frozenset({OrderState.PENDING_SEND, OrderState.SENT_UNKNOWN, OrderState.CANCEL_PENDING})


# ---------------------------------------------------------------- 数字与 JSON 化


def plain(value: Decimal | None) -> str | None:
    """Decimal -> 字符串，不用科学计数法；None 原样。非有限数（NaN、Infinity）按 Decimal 自己的写法。"""
    if value is None:
        return None
    return format(value, "f") if value.is_finite() else str(value)


def enum_value(value: Enum | None) -> Any:
    return None if value is None else value.value


def jsonable(obj: Any) -> Any:
    """递归转成 JSON 原生类型：Decimal -> 字符串，枚举 -> 取值，集合 -> 排序后的列表，字典的键 -> 字符串，
    非有限的浮点 -> null（JSON 里没有 NaN）。不认识的类型抛 ``TypeError``（宁可报错也不乱写）。"""
    if isinstance(obj, Enum):  # StrEnum 也是 str，必须先于下面的原生类型判断
        return jsonable(obj.value)
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, Decimal):
        return plain(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(key.value if isinstance(key, Enum) else key): jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return [jsonable(item) for item in sorted(obj, key=lambda item: (type(item).__name__, str(item)))]
    if isinstance(obj, (list, tuple)):
        return [jsonable(item) for item in obj]
    raise TypeError(f"不能转成 JSON：{type(obj).__name__}")


# ---------------------------------------------------------------- 持仓


def position_view(rec: PositionRecord, *, quote: PositionInfo | None, unconfirmed: bool,
                  stop_on_exchange: bool | None) -> dict[str, Any]:
    """一条持仓。``quote`` 是最近一次持仓快照里这一侧的读数（标记价、浮动盈亏），没有就是 null；
    ``unconfirmed``：这个合约方向上有命运未定的系统订单；``stop_on_exchange``：交易所上有没有止损单（未知为 null）。"""
    return {
        "id": rec.id,
        "symbol": rec.symbol,
        "side": rec.side.value,
        "generation": rec.generation,
        "status": rec.status.value,
        "ownership": rec.ownership.value,
        "is_mixed": rec.is_mixed,
        "qty": plain(rec.qty),
        "avg_price": plain(rec.avg_price),
        "mark_price": None if quote is None else plain(quote.mark_price),
        "unrealized_pnl": None if quote is None else plain(quote.unrealized_pnl),
        "liquidation_price": plain(rec.liquidation_price),
        "protection_state": rec.protection_state.value,
        "stop_price": plain(rec.stop_price),
        "stop_note": rec.stop_note,
        "exit_ruleset": enum_value(rec.exit_ruleset),
        "signal_exit": rec.signal_exit,
        "open_time_ms": rec.open_time_ms,
        "open_time_estimated": rec.open_time_estimated,
        "exchange_qty": plain(rec.exchange_qty),
        "consistent": rec.consistent,
        "has_unconfirmed_orders": unconfirmed,
        "closing": rec.status is PositionStatus.CLOSING,
        "pending_reason": enum_value(rec.pending_reason),
        "pending_flags": jsonable(list(rec.pending_flags)),
        "origin_note": rec.origin_note,
        "leverage": rec.leverage,
        "margin_mode": rec.margin_mode,
        "has_stop_on_exchange": stop_on_exchange,
        "version": rec.version,
    }


def unconfirmed_keys(orders: Iterable[OrderRecord]) -> set[tuple[str, PositionSide]]:
    """有命运未定的系统订单的（合约，持仓方向）。"""
    return {(order.symbol, order.position_side) for order in orders if order.state in _UNCONFIRMED}


def stop_on_exchange(snapshot: OrdersSnapshotTaken | None, symbol: str, side: PositionSide) -> bool | None:
    """最近一次挂单快照里，这个合约方向上有没有还开着的止损类条件单。没有快照就是未知（None）。"""
    if snapshot is None:
        return None
    return any(algo.symbol == symbol and algo.position_side == side and algo.is_open and algo.algo_type.is_stop_kind
               for algo in snapshot.algo_orders)


# ---------------------------------------------------------------- 订单


def order_view(rec: OrderRecord) -> dict[str, Any]:
    """账本里的系统订单。"""
    return {
        "client_order_id": rec.client_order_id,
        "intent_id": rec.intent_id,
        "purpose": rec.purpose.value,
        "symbol": rec.symbol,
        "side": rec.side.value,
        "position_side": rec.position_side.value,
        "kind": rec.kind,
        "order_type": rec.order_type,
        "qty": plain(rec.qty),
        "filled_qty": plain(rec.filled_qty),
        "avg_price": plain(rec.avg_price),
        "trigger_price": plain(rec.trigger_price),
        "state": rec.state.value,
        "raw_status": rec.raw_status,
        "sent_ms": rec.sent_ms,
        "updated_ms": rec.updated_ms,
    }


def exchange_order_view(info: OrderInfo) -> dict[str, Any]:
    """挂单快照里的普通挂单（交易所报告的原样）。"""
    return {
        "client_order_id": info.client_order_id,
        "exchange_order_id": info.exchange_order_id,
        "symbol": info.symbol,
        "side": info.side.value,
        "position_side": info.position_side.value,
        "order_type": info.order_type,
        "qty": plain(info.qty),
        "filled_qty": plain(info.filled_qty),
        "avg_price": plain(info.avg_price),
        "status": info.status,
        "raw_status": info.raw_status,
        "close_position": info.close_position,
        "reduce_only": info.reduce_only,
        "updated_ms": info.update_time_ms,
    }


def exchange_algo_view(info: AlgoOrderInfo) -> dict[str, Any]:
    """挂单快照里的条件单（交易所报告的原样）。"""
    return {
        "client_algo_id": info.client_algo_id,
        "algo_id": info.algo_id,
        "symbol": info.symbol,
        "side": info.side.value,
        "position_side": info.position_side.value,
        "algo_type": info.algo_type.value,
        "trigger_price": plain(info.trigger_price),
        "working_type": info.working_type.value,
        "close_position": info.close_position,
        "qty": plain(info.qty),
        "raw_status": info.raw_status,
        "actual_order_id": info.actual_order_id,
        "created_ms": info.create_time_ms,
        "updated_ms": info.update_time_ms,
    }


# ---------------------------------------------------------------- 操作日志与告警


def audit_view(rec: AuditLogRecord) -> dict[str, Any]:
    return {"id": rec.id, "at_ms": rec.at_ms, "operator": rec.operator, "action": rec.action, "target": rec.target,
            "result": rec.result, "request_id": rec.request_id, "before": jsonable(rec.before),
            "after": jsonable(rec.after)}


def alert_view(rec: AlertRecord) -> dict[str, Any]:
    return {"id": rec.id, "at_ms": rec.at_ms, "level": rec.level.value, "kind": rec.kind, "message": rec.message,
            "delivered": rec.delivered}
