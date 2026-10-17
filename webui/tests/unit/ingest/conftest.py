"""入账测试的公共夹具：临时账本、手动时钟、事件总线与收集器、假适配层、造订单/成交/数据流事件的工厂。

入账只用适配层的读方法（回查订单、补拉成交与收益流水）；假适配层没有写权限令牌，任何写调用都会直接报错。
"""
from __future__ import annotations

import itertools
from decimal import Decimal
from types import SimpleNamespace

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.configdefs import initial_values
from dmr_kernel.core.events import Event, EventBus, StreamAlgoReceived, StreamOrderReceived
from dmr_kernel.core.types import (
    AlgoOrderInfo,
    AlgoType,
    Environment,
    IncomeInfo,
    IntentAction,
    IntentSource,
    OrderInfo,
    OrderPurpose,
    OrderSide,
    PositionSide,
    StreamAlgo,
    StreamOrder,
    TradeInfo,
    WorkingType,
    WriteAccepted,
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

    def kinds(self) -> list[str]:
        return [type(e).__name__ for e in self.events]


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
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def instance(ledger) -> str:
    return "T" + ledger.nonce  # 设置里的实例号 + 账本建库时的 2 位随机串


@pytest.fixture
def intents(ledger, bus, clock, notifier):
    from dmr_kernel.core.intents import IntentService

    return IntentService(ledger, bus, clock, notifier=notifier)


@pytest.fixture
def orders(ledger, bus, clock, adapter, instance):
    from dmr_kernel.core.orders import OrderService

    return OrderService(ledger, bus, clock, adapter, instance=instance)


@pytest.fixture
def ingest(ledger, bus, clock, adapter, instance):
    from dmr_kernel.core.ingest import Ingest

    config = initial_values(Environment.STANDIN)
    return Ingest(ledger, bus, clock, adapter, instance=instance, config_get=config.__getitem__)


@pytest.fixture
def make_ingest(ledger, bus, clock, adapter, instance):
    """再造一个入账实例（模拟重启：同一个账本、新的内存状态）。"""

    def factory():
        from dmr_kernel.core.ingest import Ingest

        config = initial_values(Environment.STANDIN)
        return Ingest(ledger, bus, clock, adapter, instance=instance, config_get=config.__getitem__)

    return factory


# ---------------------------------------------------------------- 账本里的读取

@pytest.fixture
def fills(ledger):
    """async：读成交行（按入账顺序）。"""
    from dmr_kernel.ledger import repo_ingest

    async def read(**filters):
        return await ledger.read(repo_ingest.list_fills, **filters)

    return read


@pytest.fixture
def incomes(ledger):
    """async：读收益流水行（按入账顺序）。"""
    from dmr_kernel.ledger import repo_ingest

    async def read(**filters):
        return await ledger.read(repo_ingest.list_incomes, **filters)

    return read


# ---------------------------------------------------------------- 造意图与订单

@pytest.fixture
def make_intent(intents):
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
def order_info():
    """造交易所订单视图的工厂。raw 不给就取 status；client_order_id 不给就取订单的。"""

    def factory(order, *, status: str = "NEW", raw: str | None = None, filled: str = "0", avg: str | None = None,
                exchange_order_id: str = "9001", client_order_id: str | None = ..., update_ms: int = START_MS,
                qty: Decimal | None = None) -> OrderInfo:
        cid = order.client_order_id if client_order_id is ... else client_order_id
        return OrderInfo(symbol=order.symbol, client_order_id=cid, exchange_order_id=exchange_order_id,
                         side=order.side, position_side=order.position_side, order_type=order.order_type,
                         qty=qty if qty is not None else (order.qty if order.qty is not None else Decimal("1")),
                         filled_qty=Decimal(filled), avg_price=None if avg is None else Decimal(avg), status=status,
                         raw_status=raw or status, close_position=order.close_position, update_time_ms=update_ms)

    return factory


@pytest.fixture
def stranger():
    """一张不是我们下的订单（只有 order_info 需要的那几个字段）。"""

    def factory(client_order_id: str | None = "web_x", *, symbol: str = "BTCUSDT", qty: str = "1",
                side: OrderSide = OrderSide.BUY, position_side: PositionSide = PositionSide.LONG):
        return SimpleNamespace(client_order_id=client_order_id, symbol=symbol, side=side, position_side=position_side,
                               order_type="MARKET", qty=Decimal(qty), close_position=False, trigger_price=None,
                               working_type=None)

    return factory


@pytest.fixture
def algo_info():
    """造条件单视图的工厂。"""

    def factory(order, *, raw: str = "NEW", algo_id: str = "5001", actual_order_id: str | None = None,
                update_ms: int = START_MS, client_algo_id: str | None = ...) -> AlgoOrderInfo:
        cid = order.client_order_id if client_algo_id is ... else client_algo_id
        return AlgoOrderInfo(symbol=order.symbol, client_algo_id=cid, algo_id=algo_id, side=order.side,
                             position_side=order.position_side, algo_type=AlgoType.STOP_MARKET,
                             trigger_price=order.trigger_price or Decimal("90"),
                             working_type=order.working_type or WorkingType.CONTRACT_PRICE,
                             close_position=order.close_position, qty=order.qty, raw_status=raw,
                             actual_order_id=actual_order_id, create_time_ms=START_MS, update_time_ms=update_ms)

    return factory


@pytest.fixture
def new_order(orders, make_intent):
    """async 工厂：（必要时先建意图）再建一张 PENDING_SEND 的订单。"""

    async def factory(*, intent=None, leg: str = "o", purpose: OrderPurpose = OrderPurpose.OPEN,
                      position_side: PositionSide = PositionSide.LONG, qty: str = "1", model: str = "m1",
                      symbol: str = "BTCUSDT", action: IntentAction = IntentAction.OPEN):
        if intent is None:
            intent = await make_intent(model=model, symbol=symbol, side=position_side, action=action)
        side = position_side.open_side() if purpose is OrderPurpose.OPEN else position_side.close_side()
        return await orders.create_pending(intent=intent, leg=leg, purpose=purpose, kind="market",
                                           order_type="MARKET", side=side, position_side=position_side,
                                           qty=Decimal(qty))

    return factory


@pytest.fixture
def acked_order(new_order, orders, order_info):
    """async 工厂：建一张已被交易所受理（ACKNOWLEDGED，交易所订单号已记录）的订单。"""

    async def factory(*, exchange_order_id: str = "9001", **kwargs):
        order = await new_order(**kwargs)
        await orders.record_sent(order.client_order_id)
        return await orders.apply_write_result(
            order.client_order_id, WriteAccepted(order=order_info(order, exchange_order_id=exchange_order_id)))

    return factory


@pytest.fixture
def acked_stop(new_order, orders, algo_info, make_intent):
    """async 工厂：建一张已受理的系统全平型止损条件单（条件单号已记录）。"""

    async def factory(*, algo_id: str = "5001", position_side: PositionSide = PositionSide.LONG,
                      symbol: str = "BTCUSDT", intent=None, leg: str = "s"):
        if intent is None:
            intent = await make_intent(symbol=symbol, side=position_side)
        stop = await orders.create_pending(
            intent=intent, leg=leg, purpose=OrderPurpose.STOP, kind="algo", order_type="STOP_MARKET",
            side=position_side.close_side(), position_side=position_side, qty=None, trigger_price=Decimal("90"),
            working_type=WorkingType.CONTRACT_PRICE, close_position=True)
        return await orders.apply_write_result(stop.client_order_id,
                                               WriteAccepted(algo=algo_info(stop, algo_id=algo_id)))

    return factory


# ---------------------------------------------------------------- 造成交、收益流水与数据流事件

@pytest.fixture
def trade():
    """造成交的工厂。给了订单就沿用它的合约、买卖与持仓方向，交易所订单号默认 9001；成交号自动编号。"""
    counter = itertools.count(1)

    def factory(order=None, *, trade_id: str | None = None, exchange_order_id: str = "9001", symbol: str = "BTCUSDT",
                side: OrderSide = OrderSide.BUY, position_side: PositionSide = PositionSide.LONG, qty: str = "0.5",
                price: str = "100", commission: str = "0.02", commission_asset: str = "USDT",
                realized_pnl: str = "0", time_ms: int = START_MS, maker: bool = False) -> TradeInfo:
        if order is not None:
            symbol, side, position_side = order.symbol, order.side, order.position_side
        return TradeInfo(trade_id=trade_id or f"t{next(counter)}", exchange_order_id=exchange_order_id,
                         symbol=symbol, side=side, position_side=position_side, qty=Decimal(qty),
                         price=Decimal(price), commission=Decimal(commission), commission_asset=commission_asset,
                         realized_pnl=Decimal(realized_pnl), time_ms=time_ms, maker=maker)

    return factory


@pytest.fixture
def income():
    counter = itertools.count(1)

    def factory(*, income_id: str | None = None, income_type: str = "FUNDING_FEE", symbol: str | None = "BTCUSDT",
                amount: str = "-0.12", asset: str = "USDT", time_ms: int = START_MS, info: str = "") -> IncomeInfo:
        return IncomeInfo(income_id=income_id or f"i{next(counter)}", income_type=income_type, symbol=symbol,
                          amount=Decimal(amount), asset=asset, time_ms=time_ms, info=info)

    return factory


@pytest.fixture
def stream_order(order_info):
    """造「订单更新」数据流事件。有 fill 的是成交回报；没有的是 NEW/CANCELED/EXPIRED/REJECTED。"""

    def factory(order, *, execution: str, status: str, raw: str | None = None, filled: str = "0",
                avg: str | None = None, fill: TradeInfo | None = None, exchange_order_id: str = "9001",
                client_order_id: str | None = ..., event_ms: int = START_MS) -> StreamOrderReceived:
        view = order_info(order, status=status, raw=raw, filled=filled, avg=avg, exchange_order_id=exchange_order_id,
                          client_order_id=client_order_id, update_ms=event_ms)
        return StreamOrderReceived(StreamOrder(order=view, execution=execution, fill=fill, event_time_ms=event_ms))

    return factory


@pytest.fixture
def stream_algo(algo_info):
    """造「条件单更新」数据流事件。"""

    def factory(order, *, raw: str = "NEW", algo_id: str = "5001", actual_order_id: str | None = None,
                client_algo_id: str | None = ..., event_ms: int = START_MS) -> StreamAlgoReceived:
        view = algo_info(order, raw=raw, algo_id=algo_id, actual_order_id=actual_order_id,
                         client_algo_id=client_algo_id, update_ms=event_ms)
        return StreamAlgoReceived(StreamAlgo(algo=view, event_time_ms=event_ms))

    return factory
