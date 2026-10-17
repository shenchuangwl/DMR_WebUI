"""交易意图测试的公共夹具：临时账本、手动时钟、事件总线与收集器、假通知器、造意图的帮手。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import Event, EventBus, FillIngested
from dmr_kernel.core.types import (
    IntentAction,
    IntentSource,
    IntentState,
    OrderOrigin,
    OrderSide,
    PositionSide,
    TradeInfo,
)
from tests.support.ledger import START_MS, temp_ledger


class Collector:
    """订阅全部事件并按发布顺序记下来。"""

    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        bus.subscribe(None, self.events.append, name="collector")

    def of(self, kind: type) -> list:
        return [e for e in self.events if isinstance(e, kind)]


class FakeNotifier:
    """只记录 raise_alert 调用（真实的 Notifier 在 alerts/notifier.py，本测试不依赖它）。"""

    def __init__(self) -> None:
        self.alerts: list[dict] = []

    def raise_alert(self, level, kind, message, *, dedupe_key=None) -> None:
        self.alerts.append({"level": level, "kind": kind, "message": message, "dedupe_key": dedupe_key})


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START_MS)


@pytest.fixture
def ledger(tmp_path, clock):
    with temp_ledger(tmp_path, clock) as opened:
        yield opened


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def collector(bus) -> Collector:
    return Collector(bus)


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def service(ledger, bus, clock, notifier):
    from dmr_kernel.core.intents import IntentService

    return IntentService(ledger, bus, clock, notifier=notifier)


@pytest.fixture
def spec():
    """造 IntentSpec 的工厂：spec("k1", symbol="ETHUSDT", ...)。"""
    from dmr_kernel.core.intents import IntentSpec

    def factory(idem_key: str = "k1", **overrides) -> IntentSpec:
        fields = dict(action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol="BTCUSDT",
                      side=PositionSide.LONG, idem_key=idem_key)
        fields.update(overrides)
        return IntentSpec(**fields)

    return factory


# 从 CREATED 走到各状态的合法路径（用来造测试数据）
PATHS: dict[IntentState, list[IntentState]] = {
    IntentState.CREATED: [],
    IntentState.VALIDATED: [IntentState.VALIDATED],
    IntentState.RESERVED: [IntentState.VALIDATED, IntentState.RESERVED],
    IntentState.DISPATCH_READY: [IntentState.VALIDATED, IntentState.DISPATCH_READY],
    IntentState.ACTIVE: [IntentState.VALIDATED, IntentState.DISPATCH_READY, IntentState.ACTIVE],
    IntentState.RECONCILING: [IntentState.VALIDATED, IntentState.DISPATCH_READY, IntentState.ACTIVE,
                              IntentState.RECONCILING],
    IntentState.COMPLETED: [IntentState.VALIDATED, IntentState.DISPATCH_READY, IntentState.ACTIVE,
                            IntentState.COMPLETED],
    IntentState.REJECTED: [IntentState.REJECTED],
    IntentState.EXPIRED: [IntentState.EXPIRED],
    IntentState.CANCELLED: [IntentState.CANCELLED],
}


@pytest.fixture
def in_state(service, spec):
    """async 工厂：新建一个意图并沿合法路径推进到指定状态，返回最新记录。"""

    async def factory(state: IntentState, idem_key: str, **overrides):
        rec, created = await service.create(spec(idem_key, **overrides))
        assert created
        for step in PATHS[state]:
            rec = await service.transition(rec.id, step, reason=f"测试推进到 {step.value}")
        assert rec.state is state
        return rec

    return factory


@pytest.fixture
def fill_event():
    """造 FillIngested 事件的工厂。"""

    def factory(*, intent_id: int | None, origin: OrderOrigin = OrderOrigin.SYSTEM, trade_id: str = "t1",
                client_order_id: str | None = "DKa1b2-1-1-o", symbol: str = "BTCUSDT",
                position_side: PositionSide = PositionSide.LONG, at_ms: int = START_MS,
                order_purpose: str | None = "open", on_dead_order: bool = False) -> FillIngested:
        trade = TradeInfo(trade_id=trade_id, exchange_order_id="9001", symbol=symbol, side=OrderSide.BUY,
                          position_side=position_side, qty=Decimal("0.1"), price=Decimal("100"),
                          commission=Decimal("0.01"), commission_asset="USDT", realized_pnl=Decimal("0"),
                          time_ms=at_ms)
        return FillIngested(fill_id=1, trade=trade, client_order_id=client_order_id, origin=origin,
                            intent_id=intent_id, order_purpose=order_purpose, path="stream", at_ms=at_ms,
                            on_dead_order=on_dead_order)

    return factory
