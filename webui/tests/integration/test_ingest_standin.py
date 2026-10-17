"""入账（T0.11）对进程内替身交易所：下单 → 数据流成交 → 补拉成交 → fill 表里同一笔成交只有一行。

全部走 ``tests/support/inproc.py``：不开套接字，替身、适配层、账户数据流与入账共用一个 ManualClock 和一条总线。
下单的写法与协调者一致：先落盘订单行（``create_pending`` → ``record_sent``），再发请求，最后把结果交给订单服务。
数据流和补拉是同一笔成交的两条到达路径，谁先到都只入账一次；总线不启动时事件只排队，借此让补拉先于数据流事件处理。
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from dmr_kernel.core.events import FillIngested, IncomeIngested
from dmr_kernel.core.ingest import Ingest
from dmr_kernel.core.intents import IntentService, IntentSpec
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.types import (
    AlgoType,
    IntentAction,
    IntentSource,
    OrderOrigin,
    OrderPurpose,
    OrderSide,
    OrderState,
    PositionSide,
    WorkingType,
    WriteAccepted,
)
from dmr_kernel.ledger import repo_ingest
from tests.support.inproc import AUTH_HEADER, make_stack
from tests.support.ledger import open_ledger

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
BUY, SELL = OrderSide.BUY, OrderSide.SELL
O = OrderState
SYM = "BTCUSDT"
MINUTE = 60_000


class Rig:
    def __init__(self, tmp_path, **config: Any) -> None:
        self.s = make_stack(config=config or None, request_timeout_s=0.05)
        self.clock, self.bus, self.adapter, self.cap = self.s.clock, self.s.bus, self.s.adapter, self.s.cap
        self.ledger = open_ledger(tmp_path, self.clock)
        self.instance = "T" + self.ledger.nonce
        self.cfg = self.s.config.__getitem__
        self.intents = IntentService(self.ledger, self.bus, self.clock)
        self.orders = OrderService(self.ledger, self.bus, self.clock, self.adapter, instance=self.instance)
        self.ingest = self.new_ingest()
        self.stream = self.adapter.account_stream()
        self.events: list = []
        self.bus.subscribe(None, self.events.append, name="rig.collect")
        self._n = 0

    def new_ingest(self) -> Ingest:
        return Ingest(self.ledger, self.bus, self.clock, self.adapter, instance=self.instance, config_get=self.cfg)

    async def start(self, *, bus: bool = True, stream: bool = True) -> "Rig":
        """``bus=False``：总线先不启动，事件只排队（订阅已经建好）；稍后 ``await rig.bus.start()``。"""
        await self.ingest.start()
        if bus:
            await self.bus.start()
        if stream:
            await self.stream.start()
        await self.pump()
        return self

    async def pump(self) -> None:
        """让数据流读循环把已到的帧发布到总线上。"""
        for _ in range(8):
            await asyncio.sleep(0)

    async def settle(self) -> None:
        for _ in range(3):
            await self.pump()
            await self.bus.drain(10.0)

    # ------------------------------------------------------------ 下单（与协调者一致的写法）

    async def _intent(self, action: IntentAction, symbol: str, side: PositionSide):
        self._n += 1
        rec, created = await self.intents.create(IntentSpec(
            action=action, model="m1", source=IntentSource.SIGNAL, symbol=symbol, side=side,
            idem_key=f"ingest:{self._n}"))
        assert created
        return rec

    async def place(self, qty: str = "0.002", *, symbol: str = SYM, side: PositionSide = LONG,
                    purpose: OrderPurpose = OrderPurpose.OPEN):
        """落盘订单行 → 发市价单 → 记结果。返回（订单行，写结果）；成交由数据流或补拉入账。"""
        opening = purpose is OrderPurpose.OPEN
        intent = await self._intent(IntentAction.OPEN if opening else IntentAction.CLOSE, symbol, side)
        order = await self.orders.create_pending(
            intent=intent, leg="o" if opening else "c", purpose=purpose, kind="market", order_type="MARKET",
            side=side.open_side() if opening else side.close_side(), position_side=side, qty=D(qty))
        await self.orders.record_sent(order.client_order_id)
        result = await self.adapter.create_market_order(
            self.cap, symbol=symbol, side=order.side, position_side=side, qty=D(qty),
            client_order_id=order.client_order_id)
        assert isinstance(result, WriteAccepted), result
        await self.orders.apply_write_result(order.client_order_id, result)
        return await self.orders.get(order.client_order_id), result

    async def place_stop(self, trigger: str = "45000", *, symbol: str = SYM, side: PositionSide = LONG):
        intent = await self._intent(IntentAction.STOP_PLACE, symbol, side)
        order = await self.orders.create_pending(
            intent=intent, leg="s", purpose=OrderPurpose.STOP, kind="algo", order_type=AlgoType.STOP_MARKET.value,
            side=side.close_side(), position_side=side, qty=None, trigger_price=D(trigger),
            working_type=WorkingType.CONTRACT_PRICE, close_position=True)
        await self.orders.record_sent(order.client_order_id)
        result = await self.adapter.create_stop_order(
            self.cap, symbol=symbol, side=order.side, position_side=side, trigger_price=D(trigger),
            working_type=WorkingType.CONTRACT_PRICE, client_algo_id=order.client_order_id, close_position=True,
            algo_type=AlgoType.STOP_MARKET)
        assert isinstance(result, WriteAccepted), result
        return await self.orders.apply_write_result(order.client_order_id, result)

    async def manual(self, path: str, body: dict) -> Any:
        status, payload, _ = await self.s.disp.handle("POST", path, {}, json.dumps(body).encode(),
                                                      {AUTH_HEADER: self.s.manual_token})
        assert status == 200, payload
        return payload

    # ------------------------------------------------------------ 读

    async def fills(self):
        return await self.ledger.read(repo_ingest.list_fills)

    def fill_events(self) -> list[FillIngested]:
        return [e for e in self.events if isinstance(e, FillIngested)]


@pytest.fixture
def make_rig(tmp_path):
    made: list[Rig] = []

    def factory(**config: Any) -> Rig:
        rig = Rig(tmp_path, **config)
        made.append(rig)
        return rig

    yield factory
    for rig in made:
        rig.ledger.close_sync()


@pytest.fixture
def rig(make_rig) -> Rig:
    return make_rig()


# ================================================================ 下单 → 数据流成交 → 补拉成交


async def test_order_then_stream_fill_then_backfill_leaves_exactly_one_fill_row(rig):
    await rig.start()
    order, _ = await rig.place()
    await rig.settle()
    [row] = await rig.fills()  # 数据流先到：入账一行
    assert (row.path, row.client_order_id, row.origin, row.intent_id) == ("stream", order.client_order_id,
                                                                           OrderOrigin.SYSTEM, order.intent_id)
    assert (row.symbol, row.side, row.position_side, row.qty, row.price) == (SYM, BUY, LONG, D("0.002"), D("50000"))
    assert row.commission == D("0.05") and row.commission_asset == "USDT" and row.realized_pnl == 0
    assert await rig.ingest.backfill_trades([SYM]) == 0  # 补拉读到同一笔成交：不再入账
    await rig.settle()
    assert [r.id for r in await rig.fills()] == [row.id]
    filled = await rig.orders.get(order.client_order_id)
    assert (filled.state, filled.filled_qty, filled.avg_price) == (O.FILLED, D("0.002"), D("50000"))
    assert [e.trade.trade_id for e in rig.fill_events()] == [row.trade_id]  # 只发布了一次成交入账事件


async def test_a_fill_the_stream_never_delivered_is_recovered_by_the_backfill_exactly_once(rig):
    await rig.start(stream=False)  # 数据流没连
    order, _ = await rig.place()
    await rig.settle()
    assert await rig.fills() == []
    assert await rig.ingest.backfill_trades([SYM]) == 1
    await rig.settle()
    [row] = await rig.fills()
    assert (row.path, row.client_order_id, row.origin, row.qty) == ("rest", order.client_order_id,
                                                                     OrderOrigin.SYSTEM, D("0.002"))
    assert await rig.ingest.backfill_trades([SYM]) == 0  # 水位之后再补一次：没有新成交
    rig.clock.advance(61_000)
    assert await rig.ingest.backfill_trades([SYM]) == 0  # 回看重叠窗口里再读到它：认出是重复
    assert len(await rig.fills()) == 1 and len(rig.fill_events()) == 1


async def test_the_backfill_that_gets_there_first_makes_the_later_stream_event_a_duplicate(rig):
    await rig.start(bus=False)  # 总线没启动：数据流事件已经排在队里，但还没人处理
    order, _ = await rig.place()
    await rig.pump()
    assert await rig.ingest.backfill_trades([SYM]) == 1  # 补拉先到
    await rig.bus.start()
    await rig.settle()  # 数据流的成交事件现在才被处理
    [row] = await rig.fills()
    assert (row.path, row.client_order_id) == ("rest", order.client_order_id)
    assert len(rig.fill_events()) == 1
    assert (await rig.orders.get(order.client_order_id)).state is O.FILLED


async def test_a_partial_fill_arrives_in_two_parts_and_each_part_is_ingested_once(make_rig):
    rig = make_rig()
    rig.s.disp.switches.update({"partial_fill": {"enabled": True, "ratio": "0.5", "remainder": "later",
                                                 "delay_s": 5, "count": 1}})
    await rig.start()
    order, _ = await rig.place("0.004")
    await rig.settle()
    assert [r.qty for r in await rig.fills()] == [D("0.002")]
    half = await rig.orders.get(order.client_order_id)
    assert (half.state, half.filled_qty) == (O.PARTIALLY_FILLED, D("0.002"))
    rig.clock.advance(6_000)
    rig.s.disp.tick()  # 余量到期成交
    await rig.settle()
    rows = await rig.fills()
    assert [r.qty for r in rows] == [D("0.002"), D("0.002")] and len({r.trade_id for r in rows}) == 2
    assert await rig.ingest.backfill_trades([SYM]) == 0
    whole = await rig.orders.get(order.client_order_id)
    assert (whole.state, whole.filled_qty) == (O.FILLED, D("0.004"))
    assert len(rig.fill_events()) == 2


async def test_a_part_of_a_fill_the_stream_missed_is_filled_in_by_the_backfill(make_rig):
    rig = make_rig()
    rig.s.disp.switches.update({"partial_fill": {"enabled": True, "ratio": "0.5", "remainder": "later",
                                                 "delay_s": 5, "count": 1}})
    await rig.start()
    order, _ = await rig.place("0.004")
    await rig.settle()
    await rig.stream.stop()  # 余量成交时数据流已经断了
    rig.clock.advance(6_000)
    rig.s.disp.tick()
    await rig.settle()
    assert [r.qty for r in await rig.fills()] == [D("0.002")]
    assert await rig.ingest.backfill_trades([SYM]) == 1  # 只补上缺的那一笔
    await rig.settle()
    rows = await rig.fills()
    assert sorted((r.path, r.qty) for r in rows) == [("rest", D("0.002")), ("stream", D("0.002"))]
    assert (await rig.orders.get(order.client_order_id)).state is O.FILLED


# ================================================================ 不是我们下的单、重启、收益流水


async def test_a_manual_order_on_the_exchange_is_an_external_fill_ingested_once_from_the_stream(rig):
    await rig.start()
    await rig.manual("/dev/manual/order", {"symbol": SYM, "side": "BUY", "position_side": "LONG", "qty": "0.002"})
    await rig.settle()
    [row] = await rig.fills()
    assert row.origin is OrderOrigin.EXTERNAL and row.client_order_id.startswith("web_") and row.intent_id is None
    assert row.source.value == "external" and row.path == "stream"
    assert await rig.ingest.backfill_trades([SYM]) == 0
    assert len(await rig.fills()) == 1


async def test_a_manual_order_found_only_by_the_backfill_is_looked_up_by_its_exchange_order_id(rig):
    await rig.start(stream=False)
    await rig.manual("/dev/manual/order", {"symbol": SYM, "side": "BUY", "position_side": "LONG", "qty": "0.002"})
    assert await rig.ingest.backfill_trades([SYM]) == 1  # 成交记录不带编号：按交易所订单号回查出 web_ 编号
    [row] = await rig.fills()
    assert row.origin is OrderOrigin.EXTERNAL and row.path == "rest" and row.client_order_id.startswith("web_")
    assert await rig.ingest.backfill_trades([SYM]) == 0


async def test_a_restarted_ingest_recognises_what_the_ledger_already_has(rig):
    await rig.start()
    order, _ = await rig.place()
    await rig.settle()
    [row] = await rig.fills()
    again = rig.new_ingest()  # 重启：同一个账本，全新的内存状态
    rig.clock.advance(5 * MINUTE)
    assert await again.backfill_trades([SYM]) == 0
    assert [r.id for r in await rig.fills()] == [row.id]
    assert (await rig.orders.get(order.client_order_id)).state is O.FILLED


async def test_a_closing_fill_carries_the_realised_pnl_and_is_ingested_once(rig):
    await rig.start()
    await rig.place("0.002")
    rig.s.feed.set_price(SYM, "51000")
    closing, _ = await rig.place("0.002", purpose=OrderPurpose.CLOSE)
    await rig.settle()
    assert await rig.ingest.backfill_trades([SYM]) == 0
    rows = await rig.fills()
    assert [(r.side, r.price) for r in rows] == [(BUY, D("50000")), (SELL, D("51000"))]
    assert rows[1].realized_pnl == D("2") and rows[1].client_order_id == closing.client_order_id  # (51000-50000)×0.002


async def test_income_rows_are_backfilled_once(rig):
    await rig.start()
    await rig.place("0.002")
    await rig.settle()
    rig.s.feed.set_funding_rate(SYM, "0.0001")
    await rig.manual("/dev/funding/settle", {"symbol": SYM})
    assert await rig.ingest.backfill_income() >= 2  # 开仓手续费 + 资金费
    await rig.settle()
    incomes = await rig.ledger.read(repo_ingest.list_incomes)
    funding = [i for i in incomes if i.income_type == "FUNDING_FEE"]
    assert [(i.symbol, i.asset) for i in funding] == [(SYM, "USDT")] and funding[0].amount < 0  # 多头付资金费
    assert {i.income_type for i in incomes} == {"COMMISSION", "FUNDING_FEE"}
    assert await rig.ingest.backfill_income() == 0  # 再拉一次：全是重复
    assert len(await rig.ledger.read(repo_ingest.list_incomes)) == len(incomes)
    assert len([e for e in rig.events if isinstance(e, IncomeIngested)]) == len(incomes)


# ================================================================ 条件单触发后的成交


async def test_a_triggered_stop_is_claimed_by_the_system_stop_order_when_the_exchange_links_it(make_rig):
    rig = make_rig()
    rig.s.disp.switches.update({"behaviors": {"triggered_order_link": "available", "push_algo_events": True}})
    await rig.start()
    await rig.place("0.002")
    stop = await rig.place_stop("45000")
    assert stop.state is O.ACKNOWLEDGED
    rig.s.feed.set_price(SYM, "44000")
    rig.s.disp.tick()  # 触发：止损单变成一张市价平仓单并成交
    await rig.settle()
    rows = await rig.fills()
    assert len(rows) == 2
    closing = rows[1]
    assert (closing.side, closing.price, closing.origin) == (SELL, D("44000"), OrderOrigin.SYSTEM)
    assert closing.client_order_id == stop.client_order_id and closing.intent_id == stop.intent_id
    assert await rig.ingest.backfill_trades([SYM]) == 0
    assert len(await rig.fills()) == 2
    done = await rig.orders.get(stop.client_order_id)
    assert done.actual_order_id is not None and done.state is O.FILLED


async def test_a_triggered_stop_the_exchange_does_not_link_is_one_external_fill_not_two(make_rig):
    rig = make_rig()  # 替身默认：触发后的订单编号与条件单无关，也不推条件单事件
    await rig.start()
    await rig.place("0.002")
    stop = await rig.place_stop("45000")
    rig.s.feed.set_price(SYM, "44000")
    rig.s.disp.tick()
    await rig.settle()
    assert await rig.ingest.backfill_trades([SYM]) == 0  # 数据流已经入账，补拉认出是同一笔
    rows = await rig.fills()
    assert len(rows) == 2
    closing = rows[1]
    assert closing.side is SELL and closing.price == D("44000")
    assert closing.origin is not OrderOrigin.SYSTEM and closing.client_order_id != stop.client_order_id
    assert len({r.trade_id for r in rows}) == 2 and len(rig.fill_events()) == 2
