"""只读路由（报文约定第 3 节）：``/health``、``/state``、``/positions``、``/positions/external``、``/orders``、
``/oplog``、``/alerts``、``/config``、``/operations/{op_id}``、``/watchlist``（M4 名单页）、``/trades``、``/stats``、``/candles``（M5）。

只读接口绝不改变任何状态：这里不写账本、不发事件、不清理到期的席位；唯一的副作用是鉴权记下会话的最近活动时间。

``GET /operations/{op_id}`` 返回操作记录（``ops.operations.Operations.get``）：``op_id``、``op_type``、``status``、``operator``、
``started_ms``、``ended_ms``（进行中为 null）、``params``、``result``、``per_position``（没有为 null）；没有这个操作号（含不合规、
不可能存在的操作号）404 ``not_found``。服务对象取 ``app.state.services.operations``（装配层放了就用它），没有就按账本现建一个
只读的 ``Operations``（不接总线，``get`` 不写账本、不发事件）。
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Request

from dmr_kernel.api import errors, views
from dmr_kernel.api.common import Principal
from dmr_kernel.api.deps import ctx_of, int_query, ok, require_principal, services_of
from dmr_kernel.api.state import build_state
from dmr_kernel.algo.dmr import dmr as dmr_series
from dmr_kernel.core.types import Ownership, PositionSide, PositionStatus, Unknown
from dmr_kernel.ledger import repo_audit, repo_candles, repo_orders, repo_stops, repo_trades, stats as ledger_stats
from dmr_kernel.ledger.records import OperationRecord, TradeRecord
from dmr_kernel.ops.operations import Operations, check_op_id

__all__ = ["authed", "public"]

public = APIRouter()
authed = APIRouter(dependencies=[Depends(require_principal)])

DEFAULT_PAGE = 100
MAX_PAGE = 1000
_EXTERNAL = (PositionStatus.PENDING, PositionStatus.IGNORED)


@public.get("/health")
async def health(request: Request):
    ctx = ctx_of(request)
    return ok({"status": "ok", "env": ctx.settings.environment.value, "version": ctx.kernel_version,
               "time_ms": ctx.clock.now_ms()})


@authed.get("/state")
async def state(request: Request, principal: Principal = Depends(require_principal)):
    return ok(await build_state(ctx_of(request), services_of(request), principal))


async def _position_items(request: Request, wanted: tuple[PositionStatus, ...] | None) -> list[dict]:
    ctx, live = ctx_of(request), services_of(request).live
    if wanted is None:
        records = ctx.registry.registered()
    else:
        records = [rec for rec in ctx.registry.all_active() if rec.status in wanted]
    records.sort(key=lambda rec: (rec.symbol, rec.side.value))
    unconfirmed = views.unconfirmed_keys(await ctx.ledger.read(repo_orders.non_terminal))
    return [views.position_view(rec, quote=live.position_quote(rec.symbol, rec.side),
                                unconfirmed=(rec.symbol, rec.side) in unconfirmed,
                                stop_on_exchange=views.stop_on_exchange(live.orders, rec.symbol, rec.side))
            for rec in records]


@authed.get("/positions")
async def positions(request: Request):
    """登记仓：接管中、托管中、平仓中。"""
    return ok({"items": await _position_items(request, None)})


@authed.get("/positions/external")
async def positions_external(request: Request):
    """待确认与外部（已忽略）的仓。"""
    return ok({"items": await _position_items(request, _EXTERNAL)})


@authed.get("/orders")
async def orders(request: Request):
    ctx, live = ctx_of(request), services_of(request).live
    system = await ctx.ledger.read(repo_orders.non_terminal)
    snapshot = live.orders
    return ok({"system": [views.order_view(rec) for rec in system],
               "exchange_open": [] if snapshot is None else [views.exchange_order_view(o) for o in snapshot.open_orders],
               "exchange_algo": [] if snapshot is None else [views.exchange_algo_view(a) for a in snapshot.algo_orders],
               "snapshot_ms": None if snapshot is None else snapshot.at_ms})


@authed.get("/oplog")
async def oplog(request: Request, limit: str | None = None, before_id: str | None = None):
    count = int_query("limit", limit, default=DEFAULT_PAGE, minimum=1, maximum=MAX_PAGE)
    before = int_query("before_id", before_id, default=None, minimum=1, maximum=2**63 - 1)
    records = await ctx_of(request).ledger.read(repo_audit.recent, limit=count, before_id=before)
    return ok({"items": [views.audit_view(rec) for rec in records]})


@authed.get("/alerts")
async def alerts(request: Request, limit: str | None = None):
    count = int_query("limit", limit, default=DEFAULT_PAGE, minimum=1, maximum=MAX_PAGE)
    records = await ctx_of(request).notifier.recent(count)
    return ok({"items": [views.alert_view(rec) for rec in records]})


@authed.get("/config")
async def config(request: Request):
    return ok({"items": ctx_of(request).config.describe()})


def _operations_of(request: Request) -> Operations:
    """查操作记录用的服务：装配层把 ``operations`` 放进 ``app.state.services`` 就用它，没有就按账本现建一个只读的。"""
    provided = getattr(services_of(request), "operations", None)
    if provided is not None:
        return provided
    ctx = ctx_of(request)
    return Operations(ctx.ledger, ctx.clock)


def _operation_view(record: OperationRecord) -> dict:
    return {"op_id": record.op_id, "op_type": views.enum_value(record.op_type), "status": views.enum_value(record.status),
            "operator": record.operator, "started_ms": record.started_ms, "ended_ms": record.ended_ms,
            "params": record.params, "result": record.result, "per_position": record.per_position}


@authed.get("/operations/{op_id}")
async def operation(op_id: str, request: Request):
    try:
        check_op_id(op_id)
    except ValueError:  # 不合规的操作号不可能有记录
        raise errors.not_found("没有这个操作") from None
    record = await _operations_of(request).get(op_id)
    if record is None:
        raise errors.not_found("没有这个操作")
    return ok(_operation_view(record))


@authed.get("/watchlist")
async def watchlist(request: Request):
    """名单页（M4）：``{"valid","reason","fetched_ms","valid_until_ms","business_date","members":[{"symbol","direction","score",
    "first_selected_ms","held","avoid_reason"}],"rejected":[{"code","reason"}]}``。``avoid_reason`` 用模型 1 的预检逻辑算出，只读。
    模型 1 没有装配答 501。"""
    service = getattr(request.app.state, "m1", None)
    if service is None:
        raise errors.not_implemented("名单页需要模型 1")
    return ok(service.watchlist())


# ---------------------------------------------------------------- M5：逐笔交易、统计、K 线

MAX_MS = 2**63 - 1
TRADE_ORDERS = ("asc", "desc")
CANDLE_TIMEFRAMES = {"15m": 12, "1h": 6}  # 周期 → DMR 的 n（短周期 15m 用 12，长周期 1h 用 6）
DEFAULT_CANDLES = 200
MAX_CANDLES = 1000


def _choice(name: str, raw: str | None, enum_type: type, *, upper: bool) -> Any:
    if raw is None or raw == "":
        return None
    try:
        return enum_type(raw.strip().upper() if upper else raw.strip().lower())
    except ValueError:
        values = "、".join(item.value for item in enum_type)
        raise errors.invalid(f"{name} 必须是 {values} 之一") from None


def trade_view(rec: TradeRecord) -> dict[str, Any]:
    """逐笔交易：trade 表的全部字段，另加 ``duration_ms``（平仓 − 开仓，开仓时间未知为 null）与
    ``return_pct``（净盈亏 ÷ 开仓名义金额，是比值不是百分数；没有名义金额为 null）。"""
    view = {field.name: getattr(rec, field.name) for field in dataclasses.fields(rec)}
    view["duration_ms"] = None if rec.open_ms is None else rec.close_ms - rec.open_ms
    view["return_pct"] = rec.net_pnl / rec.open_notional if rec.open_notional else None
    return view


@authed.get("/trades")
async def trades(request: Request, symbol: str | None = None, ownership: str | None = None, model: str | None = None,
                 side: str | None = None, since_ms: str | None = None, until_ms: str | None = None,
                 limit: str | None = None, order: str | None = None):
    """``{"items":[trade...],"total"}``。时间窗按平仓时间（含两端）；``order`` 缺省 ``desc``（新的在前）；``limit`` 缺省 100。"""
    count = int_query("limit", limit, default=DEFAULT_PAGE, minimum=1, maximum=MAX_PAGE)
    since = int_query("since_ms", since_ms, default=None, minimum=0, maximum=MAX_MS)
    until = int_query("until_ms", until_ms, default=None, minimum=0, maximum=MAX_MS)
    if order not in (None, "") and order not in TRADE_ORDERS:
        raise errors.invalid("order 必须是 asc 或 desc")
    records, total = await ctx_of(request).ledger.read(
        repo_trades.query, symbol=(symbol.strip().upper() or None) if symbol else None,
        ownership=_choice("ownership", ownership, Ownership, upper=False), model=(model.strip().lower() or None) if model else None,
        side=_choice("side", side, PositionSide, upper=True), since_ms=since, until_ms=until, limit=count,
        order=order or "desc")
    return ok({"items": [trade_view(rec) for rec in records], "total": total})


@authed.get("/stats")
async def stats(request: Request, group_by: str | None = None, since_ms: str | None = None,
                until_ms: str | None = None):
    """``{"overall":{...},"groups":[{"key","stats"}],"mixed":{...}}``；统计口径见 ``ledger.stats``（盈亏比、盈利率、胜率在最前）。
    ``group_by`` 缺省 ``model``；混合仓不进分组，单列在 ``mixed``。"""
    group = group_by or "model"
    if group not in ledger_stats.GROUP_BY:
        raise errors.invalid("group_by 必须是 " + "、".join(ledger_stats.GROUP_BY) + " 之一")
    since = int_query("since_ms", since_ms, default=None, minimum=0, maximum=MAX_MS)
    until = int_query("until_ms", until_ms, default=None, minimum=0, maximum=MAX_MS)
    records = await ctx_of(request).ledger.read(repo_trades.all_in_window, since_ms=since, until_ms=until)
    return ok(ledger_stats.grouped(records, group))


async def _closed_bars(request: Request, symbol: str, timeframe: str, limit: int) -> list[Any]:
    """已收盘的 K 线（升序，最多 ``limit`` 根）：先取账本的 candle 表；不足时用行情缓存与（装配层放了 ``app.state.candles`` 时）
    经 CandleService 拉取补足；拉不到就用手上有的。"""
    loaded = await ctx_of(request).ledger.read(repo_candles.load_recent, timeframe, [symbol], limit)
    bars = {bar.open_time_ms: bar for bar in loaded.get(symbol, [])}
    service = getattr(request.app.state, "candles", None)
    if service is not None and len(bars) < limit:
        for bar in service.closed(symbol, timeframe):
            bars[bar.open_time_ms] = bar
        if len(bars) < limit:
            fetched = await service.fetch_closed(symbol, timeframe, min(limit, 99))
            if not isinstance(fetched, Unknown):
                for bar in fetched.value:
                    bars.setdefault(bar.open_time_ms, bar)
    return [bars[key] for key in sorted(bars)][-limit:]


@authed.get("/candles")
async def candles(request: Request, symbol: str | None = None, tf: str | None = None, limit: str | None = None):
    """``{"candles":[[open_time_ms,o,h,l,c,v]...],"dmr":[[t,value|null]...],"trades":[...],"stops":[...]}``。
    DMR 用 ``algo.dmr``（15m 的 n=12、1h 的 n=6），前 n−1 个为 null；进出场标记来自 trade 表（只含已平的），止损线来自 stop_history。"""
    if not symbol or not symbol.strip():
        raise errors.invalid("symbol 必须给出")
    if tf not in CANDLE_TIMEFRAMES:
        raise errors.invalid("tf 必须是 " + "、".join(CANDLE_TIMEFRAMES) + " 之一")
    count = int_query("limit", limit, default=DEFAULT_CANDLES, minimum=1, maximum=MAX_CANDLES)
    name = symbol.strip().upper()
    bars = await _closed_bars(request, name, tf, count)
    if not bars:
        return ok({"candles": [], "dmr": [], "trades": [], "stops": []})
    values = dmr_series(pd.Series([bar.high for bar in bars], dtype="float64"),
                        pd.Series([bar.low for bar in bars], dtype="float64"), CANDLE_TIMEFRAMES[tf])
    first_ms = bars[0].open_time_ms
    ledger = ctx_of(request).ledger
    marks = await ledger.read(repo_trades.for_symbol, name, since_ms=first_ms)
    segments = await ledger.read(repo_stops.segments, name, since_ms=first_ms)
    return ok({
        "candles": [[bar.open_time_ms, bar.open, bar.high, bar.low, bar.close, bar.volume] for bar in bars],
        "dmr": [[bar.open_time_ms, None if math.isnan(value) else float(value)] for bar, value in zip(bars, values)],
        "trades": [{"position_id": t.position_id, "side": t.side, "open_ms": t.open_ms, "open_price": t.open_avg,
                    "close_ms": t.close_ms, "close_price": t.close_avg, "exit_reason": t.exit_reason} for t in marks],
        "stops": segments,
    })
