"""成交（fill 表）与收益流水（income 表）入账的仓储函数。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录或基本类型。

- **去重**：成交按 ``trade_id`` 唯一，收益流水按 ``income_id`` 唯一；数据流与补拉两条路径到达的同一笔成交只入账一次。
  币安的成交号按合约编号，理论上两个合约会撞号，而库里的成交号是全表唯一的：撞号时后到的那个合约的成交号存成
  ``合约:成交号``，两笔都不丢，重复到达时也都认得出来。
- **认领**：``record_trade`` 在同一个事务里按订单表认领系统成交（自定义编号 → 交易所订单号 → 条件单触发后订单号），
  写 fill 行并调用 ``repo_orders.merge_view`` 合并对应订单（订单状态的单调合并只有那一处）。
- **水位**：补拉的水位存在 meta 表（``ingest.trade_wm:<合约>``、``ingest.income_wm``），只前进不后退；
  成交水位没有记录时退而取账本里这个合约最晚的成交时间。
- 本模块不导入 ``exchange``（同层不同组）：订单编号的归类（``ids.parse_client_id``）在上层做好再传进来。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import (
    AlgoOrderInfo,
    FillSource,
    IncomeInfo,
    OrderInfo,
    OrderOrigin,
    TradeInfo,
)
from dmr_kernel.ledger import repo_meta, repo_orders
from dmr_kernel.ledger.models import Fill, Income, Order
from dmr_kernel.ledger.records import FillRecord, IncomeRecord, OrderRecord

TRADE_WM_PREFIX = "ingest.trade_wm:"
INCOME_WM_KEY = "ingest.income_wm"
COMMISSION_QUOTE = "USDT"
_CHUNK = 200  # 一次 IN 查询的参数个数上限（SQLite 的变量个数有上限）


# ---------------------------------------------------------------- 内部：成交号与认领

def _fill_slot(session: Session, symbol: str, trade_id: str) -> tuple[Fill | None, str]:
    """已入账的那一行（没有则 None），以及新行应该用的成交号（正常就是 ``trade_id``，撞号时带合约前缀）。"""
    plain = session.scalars(select(Fill).where(Fill.trade_id == trade_id)).first()
    if plain is None:
        return None, trade_id
    if plain.symbol == symbol:
        return plain, trade_id
    key = f"{symbol}:{trade_id}"
    return session.scalars(select(Fill).where(Fill.trade_id == key)).first(), key


def _find_order(session: Session, *, symbol: str, client_order_id: str | None = None,
                exchange_order_id: str | None = None, algo_id: str | None = None) -> Order | None:
    """认领系统订单：自定义编号 → 交易所订单号 → 条件单触发后订单号 → 条件单号。
    订单号与条件单号只在同一个合约里比对（不同合约的订单号可能碰巧相同）。"""
    if client_order_id:
        row = session.get(Order, client_order_id)
        if row is not None:
            return row
    candidates = []
    if exchange_order_id:
        candidates += [Order.exchange_order_id == exchange_order_id, Order.actual_order_id == exchange_order_id]
    if algo_id:
        candidates.append(Order.algo_id == algo_id)
    for condition in candidates:
        stmt = select(Order).where(condition, Order.symbol == symbol).order_by(Order.created_ms, repo_orders.ROWID)
        row = session.scalars(stmt).first()
        if row is not None:
            return row
    return None


def _commission_usdt(trade: TradeInfo) -> Decimal | None:
    """手续费折合 USDT：本来就是 USDT 的原样；零手续费折合也是零；其它币种需要行情，入账时不编造。"""
    if trade.commission_asset == COMMISSION_QUOTE:
        return trade.commission
    if trade.commission == 0:
        return Decimal("0")
    return None


def _fill_totals(session: Session, client_order_id: str) -> tuple[Decimal, Decimal | None]:
    """这张订单已入账成交的合计数量与加权均价。"""
    rows = session.execute(select(Fill.qty, Fill.price).where(Fill.client_order_id == client_order_id)).all()
    quantity = sum((qty for qty, _ in rows), Decimal("0"))
    if quantity <= 0:
        return Decimal("0"), None
    return quantity, sum((qty * price for qty, price in rows), Decimal("0")) / quantity


# ---------------------------------------------------------------- 成交

def lookup(session: Session, *, symbol: str, trade_id: str, client_order_id: str | None,
           exchange_order_id: str) -> tuple[bool, OrderRecord | None, str | None]:
    """入账前的只读查询：``(是否已入账, 认领到的系统订单, 同一张交易所订单已入账成交里记的自定义编号)``。

    第三项只在没认领到、调用方也没有编号时才查：同一张外部订单的第二笔成交可以直接沿用第一笔查到的编号，
    不用再问交易所。
    """
    existing, _ = _fill_slot(session, symbol, trade_id)
    if existing is not None:
        return True, None, None
    row = _find_order(session, symbol=symbol, client_order_id=client_order_id, exchange_order_id=exchange_order_id)
    if row is not None:
        return False, OrderRecord.from_row(row), None
    sibling = None
    if client_order_id is None:
        stmt = (select(Fill.client_order_id)
                .where(Fill.exchange_order_id == exchange_order_id, Fill.symbol == symbol,
                       Fill.client_order_id.is_not(None))
                .order_by(Fill.id).limit(1))
        sibling = session.scalars(stmt).first()
    return False, None, sibling


def known_trade_ids(session: Session, symbol: str, trade_ids: Sequence[str]) -> set[str]:
    """这些成交号里已经入过账的（补拉时先筛掉重复，省得逐笔认领和回查）。"""
    unique = list(dict.fromkeys(trade_ids))
    known: set[str] = set()
    for offset in range(0, len(unique), _CHUNK):
        chunk = unique[offset:offset + _CHUNK]
        wanted = chunk + [f"{symbol}:{trade_id}" for trade_id in chunk]
        stored = dict(session.execute(select(Fill.trade_id, Fill.symbol).where(Fill.trade_id.in_(wanted))).all())
        for trade_id in chunk:
            if stored.get(trade_id) == symbol or f"{symbol}:{trade_id}" in stored:
                known.add(trade_id)
    return known


def record_trade(session: Session, *, trade: TradeInfo, client_order_id: str | None, origin: OrderOrigin,
                 source: FillSource, path: str, now_ms: int,
                 view: OrderInfo | None = None) -> tuple[FillRecord, OrderRecord | None, OrderRecord | None] | None:
    """一个事务里：去重 → 认领系统订单 → 写 fill 行 → 合并订单。成交号已入账返回 None（什么也不写）。

    返回 ``(成交行, 合并前的订单, 合并后的订单)``，没认领到订单时后两项是 None。认领到订单时来源一律是系统
    （调用方在写之前读到的归类可能已经过时，以这里的认领为准）；``client_order_id`` 记订单表里的编号。
    ``view`` 是同一张订单的交易所视图（数据流成交回报带的，或回查得到的），一并合并，让 ``raw_status`` 等跟上。
    """
    existing, key = _fill_slot(session, trade.symbol, trade.trade_id)
    if existing is not None:
        return None
    claimed = _find_order(session, symbol=trade.symbol, client_order_id=client_order_id,
                          exchange_order_id=trade.exchange_order_id)
    intent_id = None
    if claimed is not None:
        origin, source = OrderOrigin.SYSTEM, FillSource.SYSTEM
        client_order_id, intent_id = claimed.client_order_id, claimed.intent_id
    row = Fill(trade_id=key, exchange_order_id=trade.exchange_order_id, client_order_id=client_order_id,
               symbol=trade.symbol, side=trade.side, position_side=trade.position_side, qty=trade.qty,
               price=trade.price, commission=trade.commission, commission_asset=trade.commission_asset,
               commission_usdt=_commission_usdt(trade), realized_pnl=trade.realized_pnl, time_ms=trade.time_ms,
               source=source, origin=origin, position_id=None, intent_id=intent_id, path=path, created_ms=now_ms)
    session.add(row)
    session.flush()
    before = after = None
    if claimed is not None:
        quantity, average = _fill_totals(session, claimed.client_order_id)
        merged = repo_orders.merge_view(session, claimed.client_order_id, view, now_ms=now_ms, fills_qty=quantity,
                                        fills_avg=average)
        if merged is not None:
            before, after = merged
    return FillRecord.from_row(row), before, after


def list_fills(session: Session, *, symbol: str | None = None, client_order_id: str | None = None,
               intent_id: int | None = None) -> list[FillRecord]:
    """成交（按入账顺序）。"""
    stmt = select(Fill)
    if symbol is not None:
        stmt = stmt.where(Fill.symbol == symbol)
    if client_order_id is not None:
        stmt = stmt.where(Fill.client_order_id == client_order_id)
    if intent_id is not None:
        stmt = stmt.where(Fill.intent_id == intent_id)
    return [FillRecord.from_row(row) for row in session.scalars(stmt.order_by(Fill.id))]


# ---------------------------------------------------------------- 订单事件（没有成交的那种）

def merge_order_event(session: Session, view: OrderInfo, *,
                      now_ms: int) -> tuple[OrderRecord, OrderRecord] | None:
    """数据流的订单事件（NEW/CANCELED/EXPIRED/REJECTED……）并进系统订单。不是我们的订单返回 None。"""
    row = _find_order(session, symbol=view.symbol, client_order_id=view.client_order_id,
                      exchange_order_id=view.exchange_order_id)
    if row is None:
        return None
    return repo_orders.merge_view(session, row.client_order_id, view, now_ms=now_ms)


def merge_algo_event(session: Session, view: AlgoOrderInfo, *,
                     now_ms: int) -> tuple[OrderRecord, OrderRecord] | None:
    """数据流的条件单事件并进系统条件单（状态、条件单号、触发后订单号）。不是我们的条件单返回 None。"""
    row = _find_order(session, symbol=view.symbol, client_order_id=view.client_algo_id, algo_id=view.algo_id)
    if row is None:
        return None
    return repo_orders.merge_view(session, row.client_order_id, view, now_ms=now_ms)


# ---------------------------------------------------------------- 收益流水

def record_incomes(session: Session, incomes: Sequence[IncomeInfo], *, now_ms: int) -> list[IncomeRecord]:
    """按 ``income_id`` 去重写 income 表，返回新写入的（按传入顺序）。同一批里重复的也只写一次。"""
    del now_ms  # income 表没有入账时间列；保留参数让签名和 record_trade 一致
    unique = list(dict.fromkeys(item.income_id for item in incomes))
    known: set[str] = set()
    for offset in range(0, len(unique), _CHUNK):
        chunk = unique[offset:offset + _CHUNK]
        known.update(session.scalars(select(Income.income_id).where(Income.income_id.in_(chunk))))
    fresh: list[IncomeRecord] = []
    for item in incomes:
        if item.income_id in known:
            continue
        known.add(item.income_id)
        row = Income(income_id=item.income_id, income_type=item.income_type, symbol=item.symbol, amount=item.amount,
                     asset=item.asset, time_ms=item.time_ms, position_id=None, info=item.info)
        session.add(row)
        session.flush()
        fresh.append(IncomeRecord.from_row(row))
    return fresh


def list_incomes(session: Session, *, symbol: str | None = None,
                 income_type: str | None = None) -> list[IncomeRecord]:
    """收益流水（按入账顺序）。"""
    stmt = select(Income)
    if symbol is not None:
        stmt = stmt.where(Income.symbol == symbol)
    if income_type is not None:
        stmt = stmt.where(Income.income_type == income_type)
    return [IncomeRecord.from_row(row) for row in session.scalars(stmt.order_by(Income.id))]


# ---------------------------------------------------------------- 补拉水位

def get_watermark(session: Session, key: str) -> int | None:
    value = repo_meta.get(session, key)
    return None if value is None else int(value)


def set_watermark(session: Session, key: str, value: int) -> None:
    """水位只前进不后退。"""
    current = get_watermark(session, key)
    if current is None or value > current:
        repo_meta.put(session, key, str(int(value)))


def trade_watermark(session: Session, symbol: str) -> int | None:
    """补拉成交的水位：这个合约上次完整补拉见到的最晚成交时间；没补拉过返回 None。

    只认补拉自己记的水位，不取「账本里最晚的成交时间」：数据流可能已经入账了更晚的成交，那样会把补拉没来得及
    处理的更早的成交挡在窗口外。
    """
    return get_watermark(session, TRADE_WM_PREFIX + symbol)


def set_trade_watermark(session: Session, symbol: str, value: int) -> None:
    set_watermark(session, TRADE_WM_PREFIX + symbol, value)
