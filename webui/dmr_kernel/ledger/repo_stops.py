"""止损价历史（stop_history 表，迁移 0002）的仓储函数。只追加。

- ``set_protection_recording``：登记簿改保护状态的写函数（``repo_registry.set_protection``）加一步——止损价变了就在**同一个事务**里追加一行
  （价格变为新值；``clear_stop`` 清掉止损则追加一行空价格，标记止损线到此为止）。价格没变、没给价格都不记。
- ``history``：一个持仓的全部记录，按时间升序。
- ``segments``：一个合约的止损线分段 ``{"position_id","price","from_ms","to_ms"}``：每段从一次变化起，到下一次变化（或清除、或持仓平仓）止；
  仍在持有且止损没变的最后一段 ``to_ms`` 为空。只给与 ``since_ms`` 之后仍有交集的分段。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.ledger import repo_registry
from dmr_kernel.ledger.models import Position, StopHistory

__all__ = ["StopPoint", "append", "history", "segments", "set_protection_recording"]


@dataclass(frozen=True, slots=True)
class StopPoint:
    position_id: int
    price: Decimal | None
    at_ms: int


def append(session: Session, position_id: int, price: Decimal | None, at_ms: int) -> StopPoint:
    session.add(StopHistory(position_id=position_id, price=price, at_ms=at_ms))
    session.flush()
    return StopPoint(position_id, price, at_ms)


def set_protection_recording(session: Session, position_id: int, **kwargs: Any) -> Any:
    """同 ``repo_registry.set_protection``（参数与返回值一致），并在止损价变化时追加历史。"""
    change = repo_registry.set_protection(session, position_id, **kwargs)
    before, after = change.before, change.record
    if before is not None and before.stop_price != after.stop_price:
        append(session, position_id, after.stop_price, kwargs["now_ms"])
    elif before is None and after.stop_price is not None:
        append(session, position_id, after.stop_price, kwargs["now_ms"])
    return change


def history(session: Session, position_id: int) -> list[StopPoint]:
    rows = session.scalars(select(StopHistory).where(StopHistory.position_id == position_id)
                           .order_by(StopHistory.at_ms, StopHistory.id))
    return [StopPoint(row.position_id, row.price, row.at_ms) for row in rows]


def segments(session: Session, symbol: str, *, since_ms: int = 0) -> list[dict[str, Any]]:
    positions = {row.id: row.closed_ms for row in session.scalars(select(Position).where(Position.symbol == symbol))}
    if not positions:
        return []
    rows = list(session.scalars(select(StopHistory).where(StopHistory.position_id.in_(sorted(positions)))
                                .order_by(StopHistory.position_id, StopHistory.at_ms, StopHistory.id)))
    out: list[dict[str, Any]] = []
    for position_id, closed_ms in positions.items():
        points = [row for row in rows if row.position_id == position_id]
        for index, point in enumerate(points):
            if point.price is None:
                continue
            end = points[index + 1].at_ms if index + 1 < len(points) else closed_ms
            if end is not None and end < since_ms:
                continue
            out.append({"position_id": position_id, "price": point.price, "from_ms": point.at_ms, "to_ms": end})
    out.sort(key=lambda seg: (seg["from_ms"], seg["position_id"]))
    return out
