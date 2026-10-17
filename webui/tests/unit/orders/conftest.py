"""订单测试的公共夹具：临时账本、手动时钟、事件总线与收集器、假适配层、造意图与订单的工厂。"""
from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import Event, EventBus
from dmr_kernel.core.types import (
    AlgoOrderInfo,
    AlgoType,
    IntentAction,
    IntentSource,
    OrderInfo,
    OrderPurpose,
    OrderSide,
    PositionSide,
    WorkingType,
)
from tests.support.fake_adapter import FakeAdapter
from tests.support.ledger import START_MS, temp_ledger


class Collector:
    """订阅全部事件并按发布顺序记下来。"""

    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        bus.subscribe(None, self.events.append, name="collector")

    def of(self, kind: type) -> list:
        return [e for e in self.events if isinstance(e, kind)]


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
def adapter() -> FakeAdapter:
    """订单服务只用适配层的读方法；没有写权限令牌，任何写调用都会直接报错。"""
    return FakeAdapter()


@pytest.fixture
def instance(ledger) -> str:
    return "T" + ledger.nonce  # 设置里的实例号 + 账本建库时的 2 位随机串


@pytest.fixture
def intents(ledger, bus, clock):
    from dmr_kernel.core.intents import IntentService

    return IntentService(ledger, bus, clock)


@pytest.fixture
def orders(ledger, bus, clock, adapter, instance):
    from dmr_kernel.core.orders import OrderService

    return OrderService(ledger, bus, clock, adapter, instance=instance)


@pytest.fixture
def make_intent(intents):
    """async 工厂：新建一个开仓（或指定动作）意图，幂等键自动不重复。"""
    counter = itertools.count(1)

    async def factory(*, model: str = "m1", symbol: str = "BTCUSDT", side: PositionSide = PositionSide.LONG,
                      action: IntentAction = IntentAction.OPEN, source: IntentSource = IntentSource.SIGNAL):
        from dmr_kernel.core.intents import IntentSpec

        rec, created = await intents.create(IntentSpec(action=action, model=model, source=source, symbol=symbol,
                                                       side=side, idem_key=f"test:{next(counter)}"))
        assert created
        return rec

    return factory


@pytest.fixture
def new_order(orders, make_intent):
    """async 工厂：（必要时先建意图）再建一张 PENDING_SEND 的订单。"""

    async def factory(*, intent=None, leg: str = "o", purpose: OrderPurpose = OrderPurpose.OPEN, kind: str = "market",
                      order_type: str | None = None, side: OrderSide | None = None,
                      position_side: PositionSide = PositionSide.LONG, qty: Decimal | str | None = "1",
                      trigger_price: Decimal | None = None, working_type: WorkingType | None = None,
                      close_position: bool = False, resend_of: str | None = None, model: str = "m1",
                      symbol: str = "BTCUSDT"):
        if intent is None:
            intent = await make_intent(model=model, symbol=symbol, side=position_side)
        if side is None:
            side = position_side.open_side() if purpose is OrderPurpose.OPEN else position_side.close_side()
        if order_type is None:
            order_type = "MARKET" if kind == "market" else "STOP_MARKET"
        if isinstance(qty, str):
            qty = Decimal(qty)
        return await orders.create_pending(intent=intent, leg=leg, purpose=purpose, kind=kind, order_type=order_type,
                                           side=side, position_side=position_side, qty=qty,
                                           trigger_price=trigger_price, working_type=working_type,
                                           close_position=close_position, resend_of=resend_of)

    return factory


@pytest.fixture
def new_stop(new_order):
    """async 工厂：一张全平型止损条件单（kind=algo，数量为空）。"""

    async def factory(*, intent=None, leg: str = "s", position_side: PositionSide = PositionSide.LONG,
                      trigger_price: str = "90", model: str = "m1", symbol: str = "BTCUSDT"):
        return await new_order(intent=intent, leg=leg, purpose=OrderPurpose.STOP, kind="algo",
                               order_type="STOP_MARKET", position_side=position_side, qty=None,
                               trigger_price=Decimal(trigger_price), working_type=WorkingType.CONTRACT_PRICE,
                               close_position=True, model=model, symbol=symbol)

    return factory


@pytest.fixture
def order_info():
    """造交易所订单视图的工厂。raw 不给就取 status。"""

    def factory(order, *, status: str = "NEW", raw: str | None = None, filled: str = "0", avg: str | None = None,
                exchange_order_id: str = "9001", update_ms: int = START_MS) -> OrderInfo:
        return OrderInfo(symbol=order.symbol, client_order_id=order.client_order_id, exchange_order_id=exchange_order_id,
                         side=order.side, position_side=order.position_side, order_type=order.order_type,
                         qty=order.qty if order.qty is not None else Decimal("1"), filled_qty=Decimal(filled),
                         avg_price=None if avg is None else Decimal(avg), status=status, raw_status=raw or status,
                         close_position=order.close_position, update_time_ms=update_ms)

    return factory


@pytest.fixture
def algo_info():
    """造条件单视图的工厂。"""

    def factory(order, *, raw: str = "NEW", algo_id: str = "5001", actual_order_id: str | None = None,
                update_ms: int = START_MS) -> AlgoOrderInfo:
        return AlgoOrderInfo(symbol=order.symbol, client_algo_id=order.client_order_id, algo_id=algo_id,
                             side=order.side, position_side=order.position_side, algo_type=AlgoType.STOP_MARKET,
                             trigger_price=order.trigger_price or Decimal("90"),
                             working_type=order.working_type or WorkingType.CONTRACT_PRICE,
                             close_position=order.close_position, qty=order.qty, raw_status=raw,
                             actual_order_id=actual_order_id, create_time_ms=START_MS, update_time_ms=update_ms)

    return factory
