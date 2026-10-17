"""实时缓存（api.livecache.LiveCache）：订阅总线事件，缓存对账、数据流、账户、持仓、挂单的最新值，供 /state 等只读接口使用。

没有收到过的就是没有（None），由视图层给 ``known=false``；缓存本身不编造任何值。
"""
from __future__ import annotations

from decimal import Decimal

from api_kit import T0, algo_info, order_info, position_info
from dmr_kernel.api.livecache import LiveCache
from dmr_kernel.core.events import (
    AccountSnapshotTaken,
    ConfigChanged,
    OrdersSnapshotTaken,
    PositionSnapshotTaken,
    ReconcileCompleted,
    StreamAccountReceived,
    StreamAlgoReceived,
    StreamOrderReceived,
    StreamStatusChanged,
)
from dmr_kernel.core.types import (
    PositionSide,
    StreamAccount,
    StreamAlgo,
    StreamOrder,
)

D = Decimal


def account(equity: str = "1000", at: int = T0) -> AccountSnapshotTaken:
    return AccountSnapshotTaken(equity=D(equity), available=D("900"), wallet=D("950"), at_ms=at)


def positions(*infos, at: int = T0, server: int | None = None) -> PositionSnapshotTaken:
    return PositionSnapshotTaken(positions=tuple(infos), server_time_ms=server or at, at_ms=at)


def orders(open_orders=(), algo_orders=(), at: int = T0) -> OrdersSnapshotTaken:
    return OrdersSnapshotTaken(open_orders=tuple(open_orders), algo_orders=tuple(algo_orders), at_ms=at)


async def started(bus, clock) -> LiveCache:
    cache = LiveCache(bus, clock)
    cache.start()
    await bus.start()
    return cache


async def feed(bus, *events) -> None:
    for event in events:
        bus.publish(event)
    await bus.drain()


# ---------------------------------------------------------------- 什么都没收到

async def test_nothing_is_known_before_the_first_event(bus, clock):
    cache = await started(bus, clock)
    assert cache.account is None and cache.positions is None and cache.orders is None
    assert cache.stream is None and cache.last_frame_ms is None
    assert cache.reconcile("positions") is None and cache.reconcile("orders") is None
    assert cache.position_mode is None and cache.restrictions is None
    assert cache.position_quote("BTCUSDT", PositionSide.LONG) is None
    await bus.stop()


# ---------------------------------------------------------------- 各类事件落到各自的位置

async def test_the_account_snapshot_is_cached(bus, clock):
    cache = await started(bus, clock)
    await feed(bus, account("1234.5", at=T0 + 5))
    assert cache.account.equity == D("1234.5") and cache.account.at_ms == T0 + 5
    await bus.stop()


async def test_position_snapshot_gives_quotes_by_symbol_and_side(bus, clock):
    cache = await started(bus, clock)
    long_btc = position_info("BTCUSDT", PositionSide.LONG, mark=D("61000"), pnl=D("500"))
    short_eth = position_info("ETHUSDT", PositionSide.SHORT, qty=D("2"), mark=D("3100"), pnl=D("-200"))
    await feed(bus, positions(long_btc, short_eth))
    assert cache.position_quote("BTCUSDT", PositionSide.LONG) == long_btc
    assert cache.position_quote("ETHUSDT", PositionSide.SHORT) == short_eth
    assert cache.position_quote("BTCUSDT", PositionSide.SHORT) is None  # 另一侧没有
    assert cache.position_quote("XRPUSDT", PositionSide.LONG) is None
    await bus.stop()


async def test_a_zero_quantity_entry_is_not_a_position_quote(bus, clock):
    cache = await started(bus, clock)
    await feed(bus, positions(position_info("BTCUSDT", PositionSide.LONG, qty=D("0"))))
    assert cache.positions is not None  # 快照收到了
    assert cache.position_quote("BTCUSDT", PositionSide.LONG) is None  # 但数量 0 表示这一侧没有仓
    await bus.stop()


async def test_the_next_position_snapshot_replaces_the_whole_previous_one(bus, clock):
    cache = await started(bus, clock)
    await feed(bus, positions(position_info("BTCUSDT"), at=T0), positions(position_info("ETHUSDT"), at=T0 + 10))
    assert cache.position_quote("BTCUSDT", PositionSide.LONG) is None
    assert cache.position_quote("ETHUSDT", PositionSide.LONG) is not None
    await bus.stop()


async def test_the_orders_snapshot_keeps_both_lists_and_its_time(bus, clock):
    cache = await started(bus, clock)
    o, a = order_info(), algo_info()
    await feed(bus, orders([o], [a], at=T0 + 7))
    assert cache.orders.open_orders == (o,) and cache.orders.algo_orders == (a,) and cache.orders.at_ms == T0 + 7
    await bus.stop()


async def test_an_older_snapshot_never_replaces_a_newer_one_but_an_equal_time_does(bus, clock):
    cache = await started(bus, clock)
    await feed(bus, account("100", at=T0 + 20), positions(position_info(mark=D("1")), at=T0 + 20),
               orders([order_info(oid="new")], at=T0 + 20))
    await feed(bus, account("999", at=T0 + 10), positions(position_info(mark=D("2")), at=T0 + 10),
               orders([order_info(oid="old")], at=T0 + 10))  # 两轮对账重叠：先发起的后返回
    assert cache.account.equity == D("100")
    assert cache.position_quote("BTCUSDT", PositionSide.LONG).mark_price == D("1")
    assert cache.orders.open_orders[0].exchange_order_id == "new"
    await feed(bus, account("101", at=T0 + 20))
    assert cache.account.equity == D("101")  # 同一时刻的后到者生效
    await bus.stop()


async def test_reconcile_results_are_kept_per_kind(bus, clock):
    cache = await started(bus, clock)
    await feed(bus, ReconcileCompleted(kind="positions", status="ok", at_ms=T0 + 1),
               ReconcileCompleted(kind="orders", status="mismatch", at_ms=T0 + 2))
    assert (cache.reconcile("positions").status, cache.reconcile("positions").at_ms) == ("ok", T0 + 1)
    assert (cache.reconcile("orders").status, cache.reconcile("orders").at_ms) == ("mismatch", T0 + 2)
    await feed(bus, ReconcileCompleted(kind="positions", status="unknown", at_ms=T0 + 3))
    assert cache.reconcile("positions").status == "unknown"  # 以最近一次为准，不是最近一次成功的
    assert cache.reconcile("orders").status == "mismatch"
    await bus.stop()


# ---------------------------------------------------------------- 数据流

async def test_stream_status_is_cached_and_connected_counts_as_a_recent_frame(bus, clock):
    cache = await started(bus, clock)
    await feed(bus, StreamStatusChanged(state="connected", reason="", at_ms=T0 + 100))
    assert cache.stream.state == "connected" and cache.last_frame_ms == T0 + 100
    await feed(bus, StreamStatusChanged(state="interrupted", reason="静默", at_ms=T0 + 900))
    assert cache.stream.state == "interrupted" and cache.stream.reason == "静默"
    assert cache.last_frame_ms == T0 + 100  # 中断不是收到了帧
    await bus.stop()


async def test_every_kind_of_stream_frame_stamps_the_last_frame_time_with_the_clock(bus, clock):
    cache = await started(bus, clock)
    order_frame = StreamOrderReceived(payload=StreamOrder(order=order_info(), execution="NEW", fill=None,
                                                         event_time_ms=T0))
    algo_frame = StreamAlgoReceived(payload=StreamAlgo(algo=algo_info(), event_time_ms=T0))
    account_frame = StreamAccountReceived(payload=StreamAccount(positions=(), reason="ORDER", event_time_ms=T0))
    for offset, frame in ((1_000, order_frame), (2_000, algo_frame), (3_000, account_frame)):
        clock.set(T0 + offset)
        await feed(bus, frame)
        assert cache.last_frame_ms == T0 + offset
    await bus.stop()


# ---------------------------------------------------------------- 没有事件来源的两项由装配登记

async def test_position_mode_and_restrictions_are_recorded_by_the_assembly(bus, clock):
    cache = await started(bus, clock)
    clock.set(T0 + 50)
    cache.note_position_mode(True)
    cache.note_restrictions(False)
    assert (cache.position_mode.dual_side, cache.position_mode.at_ms) == (True, T0 + 50)
    assert (cache.restrictions.restricted, cache.restrictions.at_ms) == (False, T0 + 50)
    assert cache.restrictions.detail == ()
    cache.note_restrictions(True, detail=(("量化规则", "限制开仓"),), at_ms=T0 + 60)
    assert cache.restrictions.restricted is True and cache.restrictions.detail == (("量化规则", "限制开仓"),)
    assert cache.restrictions.at_ms == T0 + 60
    await bus.stop()


# ---------------------------------------------------------------- 订阅的生命周期

async def test_events_before_start_and_after_stop_are_not_seen(bus, clock):
    cache = LiveCache(bus, clock)
    await bus.start()
    await feed(bus, account("1"))
    assert cache.account is None  # 还没 start
    cache.start()
    cache.start()  # 重复 start 无害（不会订阅两次）
    await feed(bus, account("2"))
    assert cache.account.equity == D("2")
    cache.stop()
    cache.stop()
    await feed(bus, account("3"))
    assert cache.account.equity == D("2")
    await bus.stop()


async def test_unrelated_events_are_ignored(bus, clock):
    cache = await started(bus, clock)
    await feed(bus, ConfigChanged(key="leverage", at_ms=T0))
    assert cache.account is None and cache.stream is None
    await bus.stop()


async def test_note_frame_records_the_streams_own_last_frame_time_and_never_goes_back():
    """心跳帧不发业务事件；装配层每 5 秒把数据流自己的最近一帧时间登记进来，供状态条显示。"""
    from dmr_kernel.core.clock import ManualClock
    from dmr_kernel.core.events import EventBus

    cache = LiveCache(EventBus(), ManualClock(T0))
    assert cache.last_frame_ms is None
    cache.note_frame(T0 + 10_000)
    cache.note_frame(None)
    cache.note_frame(T0 + 5_000)  # 更早的不覆盖
    assert cache.last_frame_ms == T0 + 10_000
