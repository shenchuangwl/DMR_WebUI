"""``inventory``（T1.9 上线清点）：经控制接口列出全部持仓与挂单（含外部、待确认），逐个给出可选动作的提示。

只读：``GET /positions``、``GET /positions/external``、``GET /orders``。本轮只列出与提示，不自动执行任何动作
（「接管」「忽略」在 M3 才有；「平掉」用控制台的「平掉此仓」或 ``close-all``）。数据缺了、形状怪了都不抛异常，缺的显示「未知」。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dmr_kernel.cli.client import KernelError, KernelUnreachable
from dmr_kernel.cli.commands import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    CommandContext,
    _refusal_lines,
    _say,
    _unreachable_lines,
)

__all__ = ["run_inventory"]

SYSTEM_PREFIX = "DK"  # 与 exchange.ids 一致：命令行不导入内核内部，这里各写一份（单测对拍）
STATUS_LABEL = {"managed": "托管中", "taking_over": "接管中", "closing": "平仓中", "pending": "待确认", "ignored": "外部（已忽略）",
                "closed": "已平"}
SIDE_LABEL = {"LONG": "多", "SHORT": "空"}
CLOSE_HINT = "平掉（控制台「平掉此仓」，或 close-all 平掉全部托管仓）"
ADOPT_HINT = "接管（M3 起可用）"
IGNORE_HINT = "忽略（M3 起可用）"
NOT_RUN = "本命令只列出与提示，不会自动执行任何动作。"


def _m(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(data: Any, key: str = "items") -> list[Mapping[str, Any]]:
    rows = _m(data).get(key)
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _v(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    return "未知" if value is None or value == "" else str(value)


def _is_system(cid: Any) -> bool:
    return isinstance(cid, str) and cid.startswith(SYSTEM_PREFIX)  # 含应急前缀 DKE-


def _position_line(row: Mapping[str, Any]) -> list[str]:
    status = str(row.get("status"))
    label = STATUS_LABEL.get(status, status if row.get("status") else "未知")
    side = SIDE_LABEL.get(str(row.get("side")), _v(row, "side"))
    head = (f"  [{_v(row, 'id')}] {_v(row, 'symbol')} {side}  {label}  数量 {_v(row, 'qty')}  均价 {_v(row, 'avg_price')}"
            f"  标记价 {_v(row, 'mark_price')}  浮盈 {_v(row, 'unrealized_pnl')}  止损 {_v(row, 'stop_price')}")
    lines = [head]
    note = row.get("origin_note")
    reason = row.get("pending_reason")
    if note or reason:
        lines.append(f"      来源/原因：{note or ''}{'  ' if note and reason else ''}{reason or ''}")
    if status in ("pending", "ignored"):
        lines.append(f"      可选动作：{CLOSE_HINT}；{ADOPT_HINT}；{IGNORE_HINT}")
    elif status in ("managed", "taking_over"):
        lines.append(f"      可选动作：{CLOSE_HINT}")
    return lines


def _order_line(kind: str, row: Mapping[str, Any]) -> str:
    cid = row.get("client_order_id") if "client_order_id" in row else row.get("client_algo_id")
    origin = "系统" if _is_system(cid) else "外部"
    ident = cid or row.get("exchange_order_id") or row.get("algo_id")
    side = SIDE_LABEL.get(str(row.get("position_side")), _v(row, "position_side"))
    kind_text = _v(row, "order_type") if "order_type" in row else _v(row, "algo_type")
    extra = f"  触发价 {_v(row, 'trigger_price')}" if kind == "条件单" else f"  数量 {_v(row, 'qty')}"
    return (f"  {kind} [{origin}] {_v(row, 'symbol')} {_v(row, 'side')}/{side} {kind_text}{extra}"
            f"  编号 {ident if ident else '未知'}" + ("" if _is_system(cid) else "  （外部挂单：只能在 Binance 或经接管后处理）"))


def run_inventory(ctx: CommandContext) -> int:
    try:
        registered = ctx.client.get("/positions")
        external = ctx.client.get("/positions/external")
        orders = ctx.client.get("/orders")
    except KernelUnreachable as exc:
        _say(ctx.err, _unreachable_lines(ctx.settings, exc))
        return EXIT_UNAVAILABLE
    except KernelError as exc:
        _say(ctx.err, _refusal_lines(ctx.settings, exc))
        return EXIT_FAILED

    out = ctx.out
    positions = _items(registered) + _items(external)
    print(f"持仓清点：共 {len(positions)} 个", file=out)
    if not positions:
        print("  没有持仓。", file=out)
    for row in positions:
        _say(out, _position_line(row))

    system, open_orders, algo = _items(orders, "system"), _items(orders, "exchange_open"), _items(orders, "exchange_algo")
    total = len(system) + len(open_orders) + len(algo)
    print(f"挂单清点：共 {total} 笔（账本里的系统订单 {len(system)}，交易所普通挂单 {len(open_orders)}，条件单 {len(algo)}）", file=out)
    if _m(orders).get("snapshot_ms") is None:
        print("  注意：内核还没有取到交易所的挂单快照，下面只有账本里的系统订单。", file=out)
    if total == 0:
        print("  没有挂单。", file=out)
    for row in system:
        print(f"  系统订单 [系统] {_v(row, 'symbol')} {_v(row, 'side')}/{_v(row, 'position_side')} {_v(row, 'order_type')}"
              f"  数量 {_v(row, 'qty')}  状态 {_v(row, 'state')}  编号 {_v(row, 'client_order_id')}", file=out)
    for row in open_orders:
        print(_order_line("挂单", row), file=out)
    for row in algo:
        print(_order_line("条件单", row), file=out)
    print(NOT_RUN, file=out)
    return EXIT_OK
