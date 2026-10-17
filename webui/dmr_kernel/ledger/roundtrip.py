"""逐笔交易（trade 表）的生成：一笔 = 仓位从零变非零到再次归零（基础版；docs/设计约定.md 第 8 节）。

``build_trade(session, position_id)`` 在持仓归零的同一个事务里调用：按已平的持仓记录、归到它名下的成交
（``fill.position_id``，登记簿入账时回填）和资金费（``income``）算出整笔交易，写进 ``trade`` 表（每个持仓一行）。
重复调用更新同一行：资金费入账晚于平仓时，再调一次就能补算。

取数规则：
- 开仓时间：持仓登记的开仓时间；待确认仓等没有登记开仓时间的，取第一笔开仓方向成交的时间并标为「估计」。
- 开仓均价：归到这个持仓的开仓方向成交的加权均价——前提是这些成交解释得了全部开仓数量（合计不少于最大数量）；
  解释不了（接管仓里原有的部分没有成交记录）就取登记的均价。开仓名义金额 = 开仓均价 × 最大数量。
- 平仓均价：平仓方向成交的加权均价；没有成交时为空。
- 毛盈亏：成交的实现盈亏合计。手续费（USDT）：成交里折算好的 USDT 值，没折算的 USDT 手续费原值，其它币种折算不了就不计。
- 资金费：该合约在持仓期间（含开平两端）的 FUNDING_FEE，带符号；对冲两侧同时有仓时，同一笔资金费按两侧名义金额
  （均价 × 最大数量）分摊，另一侧已平或还没开的时段全部归本侧。净盈亏 = 毛盈亏 − 手续费 + 资金费。
"""
from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import ExitReason, Ownership, PositionStatus
from dmr_kernel.ledger.models import Fill, Income, Intent, Position, Trade
from dmr_kernel.ledger.records import TradeRecord

__all__ = ["build_trade", "get_trade"]

FUNDING_FEE = "FUNDING_FEE"
FEE_ASSET = "USDT"
ZERO = Decimal(0)
# 读不到开仓意图时，按归属推断模型
_OWNERSHIP_MODEL = {Ownership.STRATEGY: "m1", Ownership.TAKEOVER: "m2", Ownership.AI: "m3"}


def get_trade(session: Session, position_id: int) -> TradeRecord | None:
    row = session.scalars(select(Trade).where(Trade.position_id == position_id)).first()
    return None if row is None else TradeRecord.from_row(row)


def build_trade(session: Session, position_id: int) -> TradeRecord:
    """生成（或重算）已平持仓的逐笔交易。持仓不存在抛 LookupError；还没平抛 ValueError。"""
    pos = session.get(Position, position_id)
    if pos is None:
        raise LookupError(f"持仓 {position_id} 不存在")
    if pos.status is not PositionStatus.CLOSED:
        raise ValueError(f"持仓 {position_id} 还没有平掉（状态 {pos.status.value}），不能生成逐笔交易")

    fills = list(session.scalars(select(Fill).where(Fill.position_id == position_id).order_by(Fill.time_ms, Fill.id)))
    open_side = pos.side.open_side()
    opens = [f for f in fills if f.side == open_side]
    closes = [f for f in fills if f.side != open_side]

    open_ms, estimated = pos.open_time_ms, pos.open_time_estimated
    if open_ms is None and opens:
        open_ms, estimated = opens[0].time_ms, True
    close_ms = pos.closed_ms if pos.closed_ms is not None else pos.updated_ms

    open_qty = sum((f.qty for f in opens), ZERO)
    open_avg = _weighted(opens) if opens and open_qty >= pos.max_qty else pos.avg_price
    close_avg = _weighted(closes) if closes else None
    gross = sum((f.realized_pnl for f in fills), ZERO)
    fees = sum((_fee_usdt(f) for f in fills), ZERO)
    funding = _funding(session, pos, open_ms if open_ms is not None else pos.created_ms, close_ms)

    values = dict(
        symbol=pos.symbol, side=pos.side, ownership=pos.ownership, model=_model_of(session, pos),
        is_mixed=pos.is_mixed, open_ms=open_ms, close_ms=close_ms, open_time_estimated=estimated,
        max_qty=pos.max_qty, open_avg=open_avg, close_avg=close_avg, open_notional=open_avg * pos.max_qty,
        gross_pnl=gross, fees=fees, funding=funding, net_pnl=gross - fees + funding, entry_reason=pos.entry_reason,
        exit_reason=pos.exit_reason or ExitReason.UNKNOWN, list_snapshot_id=pos.list_snapshot_id,
        list_direction=pos.list_direction, list_score=pos.list_score)
    row = session.scalars(select(Trade).where(Trade.position_id == position_id)).first()
    if row is None:
        row = Trade(position_id=position_id, **values)
        session.add(row)
    else:
        for name, value in values.items():
            setattr(row, name, value)
    session.flush()
    return TradeRecord.from_row(row)


def _weighted(fills: Iterable[Fill]) -> Decimal:
    """按数量加权的成交均价。"""
    rows = list(fills)
    return sum((f.qty * f.price for f in rows), ZERO) / sum((f.qty for f in rows), ZERO)


def _fee_usdt(fill: Fill) -> Decimal:
    if fill.commission_usdt is not None:
        return fill.commission_usdt
    return fill.commission if fill.commission_asset == FEE_ASSET else ZERO


def _model_of(session: Session, pos: Position) -> str | None:
    if pos.opened_by_intent_id is not None:
        intent = session.get(Intent, pos.opened_by_intent_id)
        if intent is not None:
            return intent.model
    return _OWNERSHIP_MODEL.get(pos.ownership)


def _holding_start(session: Session, pos: Position) -> int:
    """持仓开始持有的时间：登记的开仓时间，其次第一笔开仓成交，最后是登记时间。"""
    if pos.open_time_ms is not None:
        return pos.open_time_ms
    first = session.scalars(select(Fill.time_ms).where(Fill.position_id == pos.id, Fill.side == pos.side.open_side())
                            .order_by(Fill.time_ms).limit(1)).first()
    return first if first is not None else pos.created_ms


def _funding(session: Session, pos: Position, start_ms: int, end_ms: int) -> Decimal:
    incomes = list(session.scalars(
        select(Income).where(Income.income_type == FUNDING_FEE, Income.symbol == pos.symbol,
                             Income.time_ms >= start_ms, Income.time_ms <= end_ms).order_by(Income.time_ms, Income.id)))
    if not incomes:
        return ZERO
    # 对侧持仓（任一代）的持有区间与名义金额：只取可能与本笔重叠的
    others = [
        (_holding_start(session, other), other.closed_ms, other.avg_price * other.max_qty)
        for other in session.scalars(select(Position).where(
            Position.symbol == pos.symbol, Position.side == pos.side.opposite(), Position.id != pos.id,
            or_(Position.closed_ms.is_(None), Position.closed_ms >= start_ms)))
    ]
    own = pos.avg_price * pos.max_qty
    total = ZERO
    for income in incomes:
        shared = own + sum((weight for begin, end, weight in others
                            if begin <= income.time_ms and (end is None or income.time_ms <= end)), ZERO)
        total += income.amount * own / shared if shared > 0 else income.amount
    return total
