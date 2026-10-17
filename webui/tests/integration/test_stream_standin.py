"""StandinAccountStream 对进程内替身交易所（T0.10）：事件发布、静默判活、断线重连、listenKey 续期与失效、数据流不可用。

全部走 ``tests/support/inproc.py``：直接调替身的分发器，不开套接字。替身和被测数据流共用同一个 ManualClock：
推进它，再调替身的 ``tick()``（心跳、listenKey 失效）与数据流的 ``check_liveness()``（调度器每 5 秒调一次的那个）。
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from dmr_kernel.core.events import (
    ListenKeyExpired,
    StreamAccountReceived,
    StreamAlgoReceived,
    StreamOrderReceived,
    StreamStatusChanged,
)
from dmr_kernel.core.types import AlgoType, OrderSide, PositionSide, WorkingType, WriteAccepted
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.stream import StandinAccountStream
from tests.support.inproc import AUTH_HEADER, T0_MS, make_stack

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
BUY, SELL = OrderSide.BUY, OrderSide.SELL


def make_rig(**kw):
    s = make_stack(**kw)
    s.stream = s.adapter.account_stream()
    s.events = []
    s.bus.subscribe(None, s.events.append, name="seen")
    return s


async def settle(s) -> None:
    for _ in range(6):
        await asyncio.sleep(0)
    await s.bus.drain(2.0)


async def begin(s) -> None:
    await s.bus.start()
    await s.stream.start()
    await settle(s)


async def step(s, seconds: float = 1.0, *, tick: bool = True) -> None:
    """推进时钟，让替身跑一次（心跳、listenKey 失效），读循环处理完帧，再做一次判活。"""
    s.clock.advance(round(seconds * 1000))
    if tick:
        s.disp.tick()
    await settle(s)
    await s.stream.check_liveness()
    await settle(s)


async def run_for(s, seconds: int, *, every: float = 1.0, until=None) -> int:
    """按固定步长推进，直到 ``until()`` 为真或满 ``seconds`` 秒；返回实际推进的秒数。"""
    elapsed = 0.0
    while elapsed < seconds and not (until and until()):
        await step(s, every)
        elapsed += every
    return int(elapsed)


def states(s) -> list[str]:
    return [e.state for e in s.events if isinstance(e, StreamStatusChanged)]


def status(s, state: str) -> list[StreamStatusChanged]:
    return [e for e in s.events if isinstance(e, StreamStatusChanged) and e.state == state]


def business(s) -> list:
    """订单、条件单、账户这三类业务事件（不含连接状态与 listenKey 失效）。"""
    return [e for e in s.events if isinstance(e, (StreamOrderReceived, StreamAlgoReceived, StreamAccountReceived))]


def cid(n: int, leg: str = "o", model: str = "1") -> str:
    return ids.make_client_id("A1", model, n, leg)


async def buy_long(s, n: int = 1, qty: str = "0.01"):
    result = await s.adapter.create_market_order(s.cap, symbol="BTCUSDT", side=BUY, position_side=LONG, qty=D(qty),
                                                 client_order_id=cid(n))
    assert isinstance(result, WriteAccepted), result
    return result


async def manual(s, path: str, body: dict):
    status_, payload, _ = await s.disp.handle("POST", path, {}, json.dumps(body).encode(),
                                              {AUTH_HEADER: s.manual_token})
    assert status_ == 200, payload
    return payload


def set_switches(s, patch: dict) -> None:
    s.disp.switches.update(patch)


# ================================================================ 事件发布到总线


async def test_connecting_registers_one_subscription_on_the_standin():
    s = make_rig()
    await begin(s)
    assert s.stream.state == "connected" and s.disp.hub.count() == 1
    assert states(s) == ["connected"] and status(s, "connected")[0].at_ms == T0_MS
    assert [r[:2] for r in s.transport.requests] == [("POST", "/api/private/listen_key")]
    assert s.transport.ws_paths == ["/ws/stream"]
    assert s.stream.last_keepalive_ok_ms == T0_MS


async def test_a_market_fill_publishes_order_account_and_order_events():
    s = make_rig()
    await begin(s)
    await buy_long(s, 1)
    await settle(s)
    events = business(s)
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["StreamOrderReceived", "StreamAccountReceived", "StreamOrderReceived"]  # 默认 account_first
    new, account, trade = events
    assert new.payload.execution == "NEW" and new.payload.fill is None
    assert new.payload.order.client_order_id == cid(1) and new.payload.order.symbol == "BTCUSDT"
    assert account.payload.reason == "ORDER"
    assert [(p.symbol, p.side, p.qty) for p in account.payload.positions if p.qty > 0] == [("BTCUSDT", LONG, D("0.01"))]
    assert account.payload.balances and account.payload.balances[0][0] == "USDT"
    assert trade.payload.execution == "TRADE" and trade.payload.order.status == "FILLED"
    fill = trade.payload.fill
    assert fill.exchange_order_id == trade.payload.order.exchange_order_id and fill.qty == D("0.01")
    assert fill.price == D("50000") and fill.commission > 0
    assert trade.payload.event_time_ms == T0_MS


async def test_fill_event_order_switch_changes_the_order_of_the_events():
    s = make_rig()
    set_switches(s, {"behaviors": {"fill_event_order": "order_first"}})
    await begin(s)
    await buy_long(s, 1)
    await settle(s)
    assert [type(e).__name__ for e in business(s)] == ["StreamOrderReceived", "StreamOrderReceived",
                                                       "StreamAccountReceived"]
    assert [e.payload.execution for e in business(s) if isinstance(e, StreamOrderReceived)] == ["NEW", "TRADE"]


async def test_closing_to_flat_publishes_a_zero_quantity_position():
    s = make_rig()
    await begin(s)
    await buy_long(s, 1)
    await s.adapter.create_market_order(s.cap, symbol="BTCUSDT", side=SELL, position_side=LONG, qty=D("0.01"),
                                        client_order_id=cid(2, "c"))
    await settle(s)
    accounts = [e.payload for e in business(s) if isinstance(e, StreamAccountReceived)]
    assert len(accounts) == 2
    last = [p for p in accounts[-1].positions if p.symbol == "BTCUSDT" and p.side is LONG]
    assert last and last[0].qty == 0 and last[0].liquidation_price is None


async def test_a_manual_order_on_the_exchange_side_arrives_without_a_system_client_id():
    s = make_rig()
    await begin(s)
    await manual(s, "/dev/manual/order", {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "qty": "0.01"})
    await settle(s)
    orders = [e.payload for e in business(s) if isinstance(e, StreamOrderReceived)]
    assert {o.order.symbol for o in orders} == {"BTCUSDT"} and len(orders) == 2
    assert all(not ids.is_system_id(o.order.client_order_id or "") for o in orders)
    assert (orders[0].order.client_order_id or "").startswith("web_")


async def test_algo_events_only_arrive_when_the_exchange_pushes_them():
    s = make_rig()
    await begin(s)
    await buy_long(s, 1)
    placed = await s.adapter.create_stop_order(
        s.cap, symbol="BTCUSDT", side=SELL, position_side=LONG, trigger_price=D("40000"),
        working_type=WorkingType.CONTRACT_PRICE, client_algo_id=cid(1, "s", "0"), close_position=True, qty=None,
        algo_type=AlgoType.STOP_MARKET)
    assert isinstance(placed, WriteAccepted)
    await settle(s)
    assert not [e for e in business(s) if isinstance(e, StreamAlgoReceived)]  # 默认不推条件单事件（实测第 11 项）
    set_switches(s, {"behaviors": {"push_algo_events": True}})
    await s.adapter.cancel_algo_order(s.cap, symbol="BTCUSDT", client_algo_id=cid(1, "s", "0"))
    await settle(s)
    algos = [e.payload for e in business(s) if isinstance(e, StreamAlgoReceived)]
    assert len(algos) == 1 and algos[0].algo.client_algo_id == cid(1, "s", "0")
    assert algos[0].algo.raw_status == "CANCELED"


async def test_heartbeats_keep_the_stream_alive_without_any_business_event():
    s = make_rig()
    await begin(s)
    for _ in range(30):  # 5 分钟，每步 10 秒
        await step(s, 10)
    assert states(s) == ["connected"] and business(s) == []
    assert s.stream.last_frame_ms == s.clock.now_ms()


# ================================================================ 静默（stream_drop: silent）


async def test_silent_stream_is_interrupted_after_stream_silence_s_and_frames_are_not_replayed():
    s = make_rig()
    await begin(s)
    set_switches(s, {"stream_drop": {"enabled": True, "mode": "silent", "duration_s": 100_000}})
    await buy_long(s, 1)  # 静默期间的帧被丢弃，不补发
    await settle(s)
    assert business(s) == []
    elapsed = await run_for(s, 89)
    assert elapsed == 89 and s.stream.state == "connected"
    await step(s, 1)
    assert s.stream.state == "interrupted" and states(s) == ["connected", "interrupted"]
    assert "90" in status(s, "interrupted")[0].reason
    assert s.disp.hub.count() == 0  # 我们这边已经断开了
    assert business(s) == []


async def test_after_silence_ends_the_stream_reconnects_and_becomes_stable():
    s = make_rig()
    await begin(s)
    set_switches(s, {"stream_drop": {"enabled": True, "mode": "silent", "duration_s": 100_000}})
    await run_for(s, 90)
    assert s.stream.state == "interrupted"
    s.disp.switches.reset()
    await step(s, 1)  # 退避 1 秒后重连
    assert s.stream.state == "connected"
    assert states(s) == ["connected", "interrupted", "connected"]
    await run_for(s, 59, every=1.0)
    assert states(s)[-1] == "connected"
    await step(s, 1)
    assert states(s) == ["connected", "interrupted", "connected", "recovered_stable"]
    await buy_long(s, 2)  # 恢复之后数据流照常推送
    await settle(s)
    assert len(business(s)) == 3


# ================================================================ 断线（stream_drop: close）


async def test_close_fault_interrupts_at_once_retries_with_backoff_then_recovers_and_stabilises():
    s = make_rig()
    await begin(s)
    set_switches(s, {"stream_drop": {"enabled": True, "mode": "close", "duration_s": 120}})
    await settle(s)  # 替身立刻关掉全部连接，不用等判活
    assert s.stream.state == "interrupted" and states(s) == ["connected", "interrupted"]
    connects_before = len(s.transport.ws_paths)
    elapsed = await run_for(s, 119)
    assert elapsed == 119 and s.stream.state == "interrupted"
    # 窗口 120 秒内的重试：1、3、8、18、48、78、108 秒各一次（退避 1、2、5、10、30、30、30），全部被拒绝
    assert len(s.transport.ws_paths) - connects_before == 7
    assert states(s) == ["connected", "interrupted"]  # 重试失败不重复发 interrupted
    await run_for(s, 100, until=lambda: s.stream.state == "connected")
    assert s.stream.state == "connected" and states(s) == ["connected", "interrupted", "connected"]
    assert s.clock.now_ms() - T0_MS == 138_000  # 窗口之后的第一次重试（108 + 30 秒）
    await run_for(s, 59)
    assert "recovered_stable" not in states(s)
    await step(s, 1)
    assert states(s) == ["connected", "interrupted", "connected", "recovered_stable"]
    await buy_long(s, 1)
    await settle(s)
    assert len(business(s)) == 3


# ================================================================ listenKey 续期与失效


async def test_keepalive_renews_the_listen_key_so_the_stream_stays_up_for_hours():
    s = make_rig()
    await begin(s)
    await run_for(s, 2 * 3600, every=60)
    assert s.stream.state == "connected" and states(s) == ["connected"]
    puts = s.transport.sent("PUT", "/api/private/listen_key")
    assert len(puts) == 6  # 每 20 分钟一次
    assert s.stream.last_keepalive_ok_ms is not None and s.clock.now_ms() - s.stream.last_keepalive_ok_ms < 20 * 60_000
    assert s.disp.hub.count() == 1


async def test_failing_keepalive_interrupts_after_listenkey_stale_min_and_the_stream_comes_back():
    s = make_rig()
    await begin(s)
    set_switches(s, {"listen_key_keepalive_fail": {"enabled": True}})
    await run_for(s, 49 * 60, every=60)
    assert s.stream.state == "connected"  # 49 分钟：续期一直失败，但还没超过 50 分钟
    assert len(s.transport.sent("PUT", "/api/private/listen_key")) >= 3
    assert s.stream.last_keepalive_ok_ms == T0_MS
    await step(s, 60)
    assert s.stream.state == "interrupted"
    assert "listenKey" in status(s, "interrupted")[0].reason
    await step(s, 1)  # 重连（建 listenKey 的 POST 不受续期故障影响）
    assert s.stream.state == "connected" and states(s) == ["connected", "interrupted", "connected"]
    assert s.stream.last_keepalive_ok_ms == s.clock.now_ms()


async def test_listen_key_expired_frame_rebuilds_the_stream_with_a_new_key():
    s = make_rig()
    await begin(s)
    s.clock.advance(61 * 60_000)  # 没有续期，替身的 listenKey 失效
    s.disp.tick()  # 替身推 listen_key_expired 并断开
    await settle(s)
    assert [e.at_ms for e in s.events if isinstance(e, ListenKeyExpired)] == [s.clock.now_ms()]
    assert states(s) == ["connected", "interrupted", "connected"]
    assert "listenKey" in status(s, "interrupted")[0].reason
    assert s.stream.state == "connected" and s.disp.hub.count() == 1
    assert len(s.transport.sent("POST", "/api/private/listen_key")) == 2 and len(s.transport.ws_paths) == 2
    assert s.stream.last_keepalive_ok_ms == s.clock.now_ms()
    await buy_long(s, 1)  # 重建之后的新连接照常收事件
    await settle(s)
    assert len(business(s)) == 3


# ================================================================ 数据流不可用 / 认证


async def test_stream_unavailable_at_start_is_interrupted_and_recovers_when_it_comes_back():
    s = make_rig()
    set_switches(s, {"behaviors": {"stream_available": False}})
    await begin(s)
    assert s.stream.state == "interrupted" and states(s) == ["interrupted"]
    assert "拒绝" in status(s, "interrupted")[0].reason
    await run_for(s, 30)
    assert s.stream.state == "interrupted" and states(s) == ["interrupted"]
    assert s.disp.hub.count() == 0
    set_switches(s, {"behaviors": {"stream_available": True}})
    await run_for(s, 60, until=lambda: s.stream.state == "connected")
    assert s.stream.state == "connected" and s.disp.hub.count() == 1
    await run_for(s, 60)
    assert states(s) == ["interrupted", "connected", "recovered_stable"]


async def test_stream_becoming_unavailable_while_connected_interrupts_at_once():
    s = make_rig()
    await begin(s)
    set_switches(s, {"behaviors": {"stream_available": False}})
    await settle(s)
    assert s.stream.state == "interrupted" and states(s) == ["connected", "interrupted"]
    await run_for(s, 20)
    assert s.stream.state == "interrupted"
    set_switches(s, {"behaviors": {"stream_available": True}})
    await run_for(s, 40, until=lambda: s.stream.state == "connected")
    assert s.stream.state == "connected"


async def test_a_wrong_token_is_a_configuration_problem_not_an_exception():
    s = make_rig(token="not-the-kernel-token")
    await begin(s)  # 不抛
    assert s.stream.state == "interrupted" and states(s) == ["interrupted"]
    assert "401" in status(s, "interrupted")[0].reason
    assert s.transport.ws_paths == []


# ================================================================ 停止


async def test_stop_disconnects_from_the_standin_and_publishes_stopped():
    s = make_rig()
    await begin(s)
    await s.stream.stop()
    await settle(s)
    assert s.stream.state == "stopped" and s.disp.hub.count() == 0
    assert states(s) == ["connected", "stopped"]
    await buy_long(s, 1)
    await settle(s)
    assert business(s) == []


async def test_adapter_close_stops_the_stream_it_created():
    s = make_rig()
    await begin(s)
    await s.adapter.close()
    await settle(s)
    assert s.stream.state == "stopped" and s.disp.hub.count() == 0
    assert isinstance(s.stream, StandinAccountStream)
