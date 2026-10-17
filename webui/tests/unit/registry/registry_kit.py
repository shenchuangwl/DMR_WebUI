"""持仓登记测试的公共帮手：造意图、造成交（写进 fill 表并返回 FillIngested）、造资金费，以及装配好的 Rig。

不连任何网络；时间一律用 ManualClock；账本是临时目录里的真实 SQLite（部分唯一索引等约束照常生效）。
"""
from __future__ import annotations

from decimal import Decimal
from itertools import count
from typing import Any

from sqlalchemy import func, select

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import Event, EventBus, FillIngested
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    FillSource,
    IntentAction,
    IntentSource,
    IntentState,
    OperationStatus,
    OperationType,
    OrderOrigin,
    OrderPurpose,
    Ownership,
    PendingReason,
    PositionSide,
    PositionStatus,
    TradeInfo,
)
from dmr_kernel.exchange import ids
from dmr_kernel.ledger import repo_audit, roundtrip
from dmr_kernel.ledger.models import Fill, Income, Intent, Operation, Position, Trade
from dmr_kernel.ledger.records import AuditLogRecord, PositionRecord, TradeRecord
from tests.support.ledger import START_MS

D = Decimal
T0 = START_MS
SYM = "BTCUSDT"
LONG = PositionSide.LONG
SHORT = PositionSide.SHORT
EXTERNAL_ORIGIN = OrderOrigin.EXTERNAL

_SEQ = count(1)
_LEG = {"open": "o", "close": "c", "partial_close": "p", "stop": "s", "dust_close": "d"}
_SOURCE = {
    OrderOrigin.SYSTEM: FillSource.SYSTEM,
    OrderOrigin.EMERGENCY: FillSource.EMERGENCY,
    OrderOrigin.LIQUIDATION: FillSource.LIQUIDATION,
    OrderOrigin.ADL: FillSource.ADL,
    OrderOrigin.LEGACY: FillSource.LEGACY,
}


# ---------------------------------------------------------------- 在写线程里执行的小函数（经 ledger.write 调用）


def _insert_intent(session, **fields) -> int:
    row = Intent(**fields)
    session.add(row)
    session.flush()
    return row.id


def _insert_fill(session, **fields) -> int:
    row = Fill(**fields)
    session.add(row)
    session.flush()
    return row.id


def _insert_income(session, **fields) -> None:
    session.add(Income(**fields))
    session.flush()


def _insert_operation(session, **fields) -> None:
    session.add(Operation(**fields))
    session.flush()


def _fill_position_id(session, fill_id: int) -> int | None:
    return session.get(Fill, fill_id).position_id


def _all_audit(session) -> list[AuditLogRecord]:
    return repo_audit.recent(session, limit=1000)


def _count_trades(session) -> int:
    return session.scalar(select(func.count()).select_from(Trade))


def _all_positions(session) -> list[PositionRecord]:
    return [PositionRecord.from_row(row) for row in session.scalars(select(Position).order_by(Position.id))]


def _raw_add_position(session, **fields) -> int:
    """绕过仓储函数直接插一行持仓：用来验证数据库约束本身。"""
    row = Position(**fields)
    session.add(row)
    session.flush()
    return row.id


# ---------------------------------------------------------------- 造数据


async def add_intent(ledger, *, model: str = "m1", action: IntentAction = IntentAction.OPEN,
                     source: IntentSource = IntentSource.SIGNAL, symbol: str = SYM, side: PositionSide = LONG,
                     reason: str = "", state: IntentState = IntentState.COMPLETED, params: dict[str, Any] | None = None,
                     op_id: str | None = None, now_ms: int = T0) -> int:
    return await ledger.write(
        _insert_intent, idem_key=f"test-{next(_SEQ)}", action=action, model=model, source=source, symbol=symbol,
        side=side, params=params or {}, reason=reason, state=state, state_reason="", created_ms=now_ms,
        updated_ms=now_ms, op_id=op_id)


async def add_operation(ledger, op_id: str, op_type: OperationType, *, now_ms: int = T0) -> None:
    await ledger.write(_insert_operation, op_id=op_id, op_type=op_type, status=OperationStatus.SUCCEEDED,
                       params={}, started_ms=now_ms)


async def make_fill(ledger, *, qty: Decimal | int | str, price: Decimal | int | str, symbol: str = SYM,
                    position_side: PositionSide = LONG, opening: bool = True,
                    origin: OrderOrigin = OrderOrigin.SYSTEM, purpose: str | None | bool = True,
                    intent_id: int | None = None, client_order_id: str | None | bool = True, time_ms: int | None = None,
                    commission: Decimal | int | str = 0, commission_usdt: Decimal | int | str | None | bool = True,
                    commission_asset: str = "USDT", realized_pnl: Decimal | int | str = 0) -> FillIngested:
    """写一行 fill（同一事务外，和入账一样先落盘），返回对应的 FillIngested 事件。

    purpose=True：按 opening 取 open/close；None：没有订单用途；字符串：原样使用。
    client_order_id=True：系统来源时按用途生成系统编号，其它来源生成一个看不出来源的外部编号；None：没有编号。
    commission_usdt=True：与 commission 相同（USDT 手续费入账时的写法）。
    """
    n = next(_SEQ)
    qty, price, commission, realized_pnl = D(str(qty)), D(str(price)), D(str(commission)), D(str(realized_pnl))
    if purpose is True:
        purpose = OrderPurpose.OPEN.value if opening else OrderPurpose.CLOSE.value
    if client_order_id is True:
        if origin is OrderOrigin.SYSTEM:
            leg = _LEG.get(str(purpose or "open"), "o")
            client_order_id = ids.make_client_id("a1", "1", intent_id or 0, leg)
        else:
            client_order_id = f"ext{n}abcdefghijklmnop"
    if commission_usdt is True:
        commission_usdt = commission if commission_asset == "USDT" else None
    elif commission_usdt is not None and commission_usdt is not False:
        commission_usdt = D(str(commission_usdt))
    else:
        commission_usdt = None
    time_ms = T0 if time_ms is None else time_ms
    side = position_side.open_side() if opening else position_side.close_side()
    trade_id = f"t{n}"
    exchange_order_id = f"x{n}"
    fill_id = await ledger.write(
        _insert_fill, trade_id=trade_id, exchange_order_id=exchange_order_id, client_order_id=client_order_id,
        symbol=symbol, side=side, position_side=position_side, qty=qty, price=price, commission=commission,
        commission_asset=commission_asset, commission_usdt=commission_usdt, realized_pnl=realized_pnl,
        time_ms=time_ms, source=_SOURCE.get(origin, FillSource.EXTERNAL), origin=origin, intent_id=intent_id,
        path="stream", created_ms=time_ms)
    trade = TradeInfo(trade_id=trade_id, exchange_order_id=exchange_order_id, symbol=symbol, side=side,
                      position_side=position_side, qty=qty, price=price, commission=commission,
                      commission_asset=commission_asset, realized_pnl=realized_pnl, time_ms=time_ms)
    return FillIngested(fill_id=fill_id, trade=trade, client_order_id=client_order_id, origin=origin,
                        intent_id=intent_id, order_purpose=purpose, path="stream", at_ms=time_ms)


async def add_income(ledger, *, amount: Decimal | int | str, time_ms: int, symbol: str = SYM,
                     income_type: str = "FUNDING_FEE") -> None:
    n = next(_SEQ)
    await ledger.write(_insert_income, income_id=f"inc{n}", income_type=income_type, symbol=symbol,
                       amount=D(str(amount)), asset="USDT", time_ms=time_ms, info="")


async def fill_position_id(ledger, fill_id: int) -> int | None:
    return await ledger.read(_fill_position_id, fill_id)


async def audit_rows(ledger) -> list[AuditLogRecord]:
    return await ledger.read(_all_audit)


async def trade_of(ledger, position_id: int) -> TradeRecord | None:
    return await ledger.read(roundtrip.get_trade, position_id)


async def trade_count(ledger) -> int:
    return await ledger.read(_count_trades)


async def all_position_rows(ledger) -> list[PositionRecord]:
    return await ledger.read(_all_positions)


async def raw_add_position(ledger, **fields) -> int:
    base = dict(symbol=SYM, side=LONG, generation=1, status="managed", ownership=Ownership.STRATEGY, qty=D(1),
                avg_price=D(100), max_qty=D(1), created_ms=T0, updated_ms=T0)
    base.update(fields)
    base["status"] = PositionStatus(base["status"])
    return await ledger.write(_raw_add_position, **base)


# ---------------------------------------------------------------- 装配


class Rig:
    """登记簿 + 事件总线 + 事件收集器。协程测试里先 ``await rig.up()``。"""

    def __init__(self, ledger, clock: ManualClock, config: dict[str, Any] | None = None):
        self.ledger = ledger
        self.clock = clock
        self.config: dict[str, Any] = {"leverage": 3, "margin_mode": "isolated", **(config or {})}
        self.bus = EventBus()
        self.registry = Registry(ledger, self.bus, clock, config_get=self.config.__getitem__)
        self.seen: list[Event] = []
        self._spy = self.bus.subscribe(None, self.seen.append, name="spy")

    async def up(self) -> "Rig":
        await self.registry.load()
        return self

    async def events(self, *types: type) -> list[Event]:
        await self.bus.start()
        await self.bus.drain()
        return [e for e in self.seen if not types or isinstance(e, types)]

    def clear_events(self) -> None:
        """忘掉此前的所有事件，包括已经发布、还没送到收集器的（总线在 events() 里才开始投递）。"""
        while not self._spy.queue.empty():
            self._spy.queue.get_nowait()
            self._spy.queue.task_done()
        self.seen.clear()

    def another(self) -> "Rig":
        """同一账本上的另一个登记簿（模拟重启后重新装配，或不共享内存缓存的并发写入方）。"""
        return Rig(self.ledger, self.clock, self.config)

    # ---- 快捷造仓

    async def open_strategy(self, *, symbol: str = SYM, side: PositionSide = LONG, qty: Decimal | int | str = 1,
                            price: Decimal | int | str = 100, intent_id: int = 1, ownership: Ownership = Ownership.STRATEGY,
                            model: str = "m1", **kw) -> PositionRecord:
        return await self.registry.register_open(
            symbol=symbol, side=side, qty=D(str(qty)), avg_price=D(str(price)), open_time_ms=kw.pop("open_time_ms", T0),
            ownership=ownership, model=model, intent_id=intent_id, **kw)

    async def pending(self, *, symbol: str = SYM, side: PositionSide = LONG, qty: Decimal | int | str = 1,
                      price: Decimal | int | str = 100, reason: PendingReason = PendingReason.OUT_OF_SCOPE,
                      **kw) -> PositionRecord:
        return await self.registry.register_pending(symbol=symbol, side=side, qty=D(str(qty)),
                                                    avg_price=D(str(price)), reason=reason, **kw)

    async def takeover(self, *, symbol: str = SYM, side: PositionSide = LONG, qty: Decimal | int | str = 1,
                       price: Decimal | int | str = 100, **kw) -> PositionRecord:
        params = dict(open_time_ms=T0, open_time_estimated=False, anchor_ms=T0, signal_exit=False)
        params.update(kw)
        return await self.registry.begin_takeover(symbol=symbol, side=side, qty=D(str(qty)),
                                                  avg_price=D(str(price)), **params)

    async def managed_takeover(self, **kw) -> PositionRecord:
        rec = await self.takeover(**kw)
        return await self.registry.mark_managed(rec.id)

    async def apply(self, **kw) -> PositionRecord | None:
        """造一笔成交并交给登记簿。"""
        return await self.registry.apply_fill(await make_fill(self.ledger, **kw))
