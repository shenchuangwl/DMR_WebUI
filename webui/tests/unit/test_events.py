"""core.events：内核内部事件总线。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from dmr_kernel.core import events as ev


@dataclass(frozen=True)
class Ping(ev.Event):
    n: int


@dataclass(frozen=True)
class Pong(ev.Event):
    n: int


async def test_subscriber_receives_events_in_publish_order():
    bus = ev.EventBus()
    got = []
    bus.subscribe(Ping, lambda e: got.append(e.n), name="t")
    await bus.start()
    for i in range(5):
        bus.publish(Ping(i))
    await bus.drain()
    assert got == [0, 1, 2, 3, 4]
    await bus.stop()


async def test_type_filtering_and_multiple_subscribers():
    bus = ev.EventBus()
    pings, pongs, all_ = [], [], []
    bus.subscribe(Ping, lambda e: pings.append(e.n), name="pings")
    bus.subscribe(Pong, lambda e: pongs.append(e.n), name="pongs")
    bus.subscribe(None, lambda e: all_.append(type(e).__name__), name="all")
    await bus.start()
    bus.publish(Ping(1))
    bus.publish(Pong(2))
    await bus.drain()
    assert pings == [1] and pongs == [2] and all_ == ["Ping", "Pong"]
    await bus.stop()


async def test_async_handlers_are_awaited_and_drain_waits_for_them():
    bus = ev.EventBus()
    done = []

    async def slow(e):
        await asyncio.sleep(0.05)
        done.append(e.n)

    bus.subscribe(Ping, slow, name="slow")
    await bus.start()
    bus.publish(Ping(7))
    await bus.drain()
    assert done == [7]
    await bus.stop()


async def test_handler_exception_is_logged_and_worker_keeps_running(caplog):
    bus = ev.EventBus()
    got = []

    def flaky(e):
        if e.n == 1:
            raise RuntimeError("boom")
        got.append(e.n)

    bus.subscribe(Ping, flaky, name="flaky")
    await bus.start()
    with caplog.at_level(logging.ERROR):
        bus.publish(Ping(1))
        bus.publish(Ping(2))
        await bus.drain()
    assert got == [2]
    assert any("boom" in r.getMessage() or "boom" in str(r.exc_info) for r in caplog.records)
    await bus.stop()


async def test_events_published_before_start_are_delivered_after_start():
    bus = ev.EventBus()
    got = []
    bus.subscribe(Ping, lambda e: got.append(e.n), name="t")
    bus.publish(Ping(1))
    await bus.start()
    await bus.drain()
    assert got == [1]
    await bus.stop()


async def test_unsubscribe_stops_delivery():
    bus = ev.EventBus()
    got = []
    sub = bus.subscribe(Ping, lambda e: got.append(e.n), name="t")
    await bus.start()
    bus.publish(Ping(1))
    await bus.drain()
    sub.cancel()
    bus.publish(Ping(2))
    await bus.drain()
    assert got == [1]
    await bus.stop()


def test_documented_event_types_exist():
    for name in ("StreamStatusChanged", "StreamOrderReceived", "StreamAlgoReceived",
                 "StreamAccountReceived", "ListenKeyExpired", "FillIngested", "IncomeIngested",
                 "OrderStateChanged", "AlgoOrderObserved", "PositionSnapshotTaken",
                 "UnregisteredPositionFound", "PositionRegistered", "PositionChanged",
                 "PositionClosed", "ProtectionChanged", "CandlesReady", "ModelStateChanged",
                 "OperationProgress", "AlertRaised", "ListSnapshotUpdated",
                 "ReconcileCompleted", "ConfigChanged", "IntentStateChanged",
                 "AccountSnapshotTaken", "RiskStateChanged", "OrdersSnapshotTaken"):
        cls = getattr(ev, name)
        assert issubclass(cls, ev.Event), name
