"""补充约定（实现时定下、其它用例没有覆盖的行为）：

- 分发器接受带 ``now_ms()`` 的时钟（内核的 ``ManualClock``），进程内传输可以直接共用内核的时钟；
- 订单、条件单、成交、收益流水四类编号互不相同；
- 资金费结算推送 reason=FUNDING_FEE 的 account_update；
- 行为开关 ``stream_available`` 关掉时，已有连接也立即断开；
- 成交记录的窗口：带 start_ms 时从起点取前 limit 条，否则取最近的 limit 条（都升序返回）；
- 平仓单的余量到期时按当时的持仓截断，剩下的作废；
- 打开 ``push_algo_events`` 时条件单事件的完整顺序，以及 ``triggered_order_link=available`` 时 TRIGGERED 帧给出触发后订单号。
"""
from __future__ import annotations

import asyncio

import pytest

from dmr_standin.dispatcher import build_standin
from dmr_standin.engine import PartialFill
from dmr_standin.feed import StaticFeed
from dmr_standin.stream import StreamClosed
from standin_kit import (D, KERNEL_TOKEN, MANUAL_TOKEN, MH, T0, call, instruments, make_engine, make_standin,
                         open_long, position, stop)

ORDER = {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "type": "MARKET", "qty": "0.01",
         "client_order_id": "DKA1-1-1-o"}


class NowMsClock:
    """形状同内核的 ManualClock：只有 now_ms()，不可调用。"""

    def __init__(self, ms: int):
        self.ms = ms

    def now_ms(self) -> int:
        return self.ms


async def _subscribe(disp):
    _, body, _ = await call(disp, "POST", "/api/private/listen_key")
    return disp.subscribe(body["listen_key"])


async def test_build_standin_accepts_a_clock_with_now_ms():
    clock = NowMsClock(T0)
    feed = StaticFeed(instruments(), clock=clock.now_ms)
    feed.set_price("BTCUSDT", "50000")
    disp = build_standin(feed=feed, clock=clock, kernel_token=KERNEL_TOKEN, manual_token=MANUAL_TOKEN,
                         persist=False, seed=1)
    _, body, _ = await call(disp, "GET", "/api/public/time")
    assert body == {"server_time_ms": T0}
    clock.ms += 5000
    _, body, _ = await call(disp, "GET", "/api/public/time")
    assert body == {"server_time_ms": T0 + 5000}
    status, order, _ = await call(disp, "POST", "/api/private/order", body=ORDER)
    assert status == 200 and order["update_time_ms"] == T0 + 5000


def test_id_kinds_never_collide():
    engine, _, _, _ = make_engine()
    order = open_long(engine, qty="0.02")
    algo = stop(engine, trigger="49000")
    trade = engine.trades(symbol="BTCUSDT")[0]
    tran = engine.income()[0]
    assert len({order["order_id"], algo["algo_id"], trade["trade_id"], tran["tran_id"]}) == 4


async def test_funding_settlement_pushes_account_update():
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body=ORDER)
    sub = await _subscribe(disp)
    status, rows, _ = await call(disp, "POST", "/dev/funding/settle", body={}, headers=MH)
    assert status == 200 and len(rows) == 1
    accounts = [f for f in sub.drain() if f["type"] == "account_update"]
    assert [f["reason"] for f in accounts] == ["FUNDING_FEE"]
    assert accounts[0]["balances"][0]["wallet_balance"] == disp.engine.account()["wallet_balance"]


async def test_switching_stream_unavailable_closes_live_connections():
    disp, _, _ = make_standin()
    sub = await _subscribe(disp)
    await call(disp, "PUT", "/dev/faults", body={"behaviors": {"stream_available": False}}, headers=MH)
    with pytest.raises(StreamClosed):
        await asyncio.wait_for(sub.get(), 1)


async def test_tickers_with_unknown_symbol_is_1121():
    disp, _, _ = make_standin()
    status, err, _ = await call(disp, "GET", "/api/public/tickers", params={"symbols": "BTCUSDT,NOPEUSDT"})
    assert status == 400 and err["code"] == -1121


def test_trade_window_first_n_from_start_else_last_n():
    engine, _, _, clock = make_engine()
    for i in range(4):
        open_long(engine, qty="0.01", cid=f"DKA1-1-{i}-o")
        clock.advance(1000)
    assert [t["time_ms"] for t in engine.trades(symbol="BTCUSDT", start_ms=T0, limit=2)] == [T0, T0 + 1000]
    assert [t["time_ms"] for t in engine.trades(symbol="BTCUSDT", limit=2)] == [T0 + 2000, T0 + 3000]


def test_closing_remainder_is_capped_by_the_position_then_expires():
    engine, _, _, clock = make_engine()
    open_long(engine, qty="0.04")
    order = engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.04"),
                                      client_order_id="DKA1-0-2-p",
                                      partial=PartialFill(ratio=D("0.5"), remainder="later", delay_ms=1000))
    assert order["status"] == "PARTIALLY_FILLED" and order["filled_qty"] == "0.02"
    engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                              client_order_id="DKA1-0-3-p")  # 余量到期前持仓又变小
    clock.advance(1000)
    engine.check_triggers()
    got = engine.get_order(symbol="BTCUSDT", order_id=order["order_id"])
    assert got["status"] == "EXPIRED" and got["filled_qty"] == "0.03"
    assert position(engine, "BTCUSDT", "LONG") is None and position(engine, "BTCUSDT", "SHORT") is None


async def test_algo_event_sequence_and_linked_order_id():
    disp, feed, _ = make_standin()
    await call(disp, "PUT", "/dev/faults", body={"behaviors": {"push_algo_events": True,
                                                                "triggered_order_link": "available"}}, headers=MH)
    await call(disp, "POST", "/api/private/order", body=ORDER)
    sub = await _subscribe(disp)
    status, algo, _ = await call(disp, "POST", "/api/private/algo_order", body={
        "symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG", "type": "STOP_MARKET",
        "trigger_price": "49000", "working_type": "CONTRACT_PRICE", "close_position": True, "qty": None,
        "client_algo_id": "DKA1-0-1-s"})
    assert status == 200
    feed.set_price("BTCUSDT", "48900")
    disp.tick()
    frames = sub.drain()
    algo_frames = [f["algo"] for f in frames if f["type"] == "algo_update"]
    assert [a["status"] for a in algo_frames] == ["NEW", "TRIGGERING", "TRIGGERED", "FINISHED"]
    triggered = algo_frames[2]
    fills = [f for f in frames if f["type"] == "order_update" and f["execution"] == "TRADE"]
    assert triggered["actual_order_id"] == fills[0]["order"]["order_id"]
    assert fills[0]["order"]["client_order_id"] == "DKA1-0-1-s" and fills[0]["order"]["close_position"] is True
