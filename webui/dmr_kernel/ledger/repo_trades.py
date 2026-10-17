"""逐笔交易（trade 表）的查询仓储函数（M5）。签名 ``fn(session, ...)``，经 ``Ledger.read`` 调用。

- ``query``：筛选（合约、归属、模型、方向）+ 时间窗（按**平仓时间** ``close_ms``，含两端）+ 排序（``order``：``desc`` 新的在前，缺省；``asc``）
  + 分页（``limit``）；返回 ``(记录列表, 满足筛选的总数)``，总数不受 ``limit`` 影响。
- ``all_in_window``：时间窗内的全部交易（统计用）。
- ``for_symbol``：某合约在时间窗内的交易（K 线页的进出场标记）。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import Ownership, PositionSide
from dmr_kernel.ledger.models import Trade
from dmr_kernel.ledger.records import TradeRecord

__all__ = ["MAX_LIMIT", "all_in_window", "for_symbol", "query"]

MAX_LIMIT = 5000
ORDERS = ("asc", "desc")


def _filters(*, symbol: str | None, ownership: Ownership | None, model: str | None, side: PositionSide | None,
             since_ms: int | None, until_ms: int | None) -> list:
    conditions = []
    if symbol is not None:
        conditions.append(Trade.symbol == symbol)
    if ownership is not None:
        conditions.append(Trade.ownership == ownership)
    if model is not None:
        conditions.append(Trade.model == model)
    if side is not None:
        conditions.append(Trade.side == side)
    if since_ms is not None:
        conditions.append(Trade.close_ms >= since_ms)
    if until_ms is not None:
        conditions.append(Trade.close_ms <= until_ms)
    return conditions


def query(session: Session, *, symbol: str | None = None, ownership: Ownership | None = None, model: str | None = None,
          side: PositionSide | None = None, since_ms: int | None = None, until_ms: int | None = None,
          limit: int = 100, order: str = "desc") -> tuple[list[TradeRecord], int]:
    if order not in ORDERS:
        raise ValueError(f"order 必须是 asc 或 desc：{order!r}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_LIMIT} 之间的整数：{limit!r}")
    conditions = _filters(symbol=symbol, ownership=ownership, model=model, side=side, since_ms=since_ms,
                          until_ms=until_ms)
    total = session.scalar(select(func.count()).select_from(Trade).where(*conditions)) or 0
    if order == "desc":
        ordering = (Trade.close_ms.desc(), Trade.id.desc())
    else:
        ordering = (Trade.close_ms.asc(), Trade.id.asc())
    rows = session.scalars(select(Trade).where(*conditions).order_by(*ordering).limit(limit))
    return [TradeRecord.from_row(row) for row in rows], total


def all_in_window(session: Session, *, since_ms: int | None = None, until_ms: int | None = None) -> list[TradeRecord]:
    conditions = _filters(symbol=None, ownership=None, model=None, side=None, since_ms=since_ms, until_ms=until_ms)
    rows = session.scalars(select(Trade).where(*conditions).order_by(Trade.close_ms, Trade.id))
    return [TradeRecord.from_row(row) for row in rows]


def for_symbol(session: Session, symbol: str, *, since_ms: int | None = None) -> list[TradeRecord]:
    conditions = _filters(symbol=symbol, ownership=None, model=None, side=None, since_ms=since_ms, until_ms=None)
    rows = session.scalars(select(Trade).where(*conditions).order_by(Trade.close_ms, Trade.id))
    return [TradeRecord.from_row(row) for row in rows]
