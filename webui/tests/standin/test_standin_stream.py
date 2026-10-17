"""账户数据流（docs/设计约定.md 7.3）：订阅中心的帧、顺序开关、条件单事件开关、心跳、listenKey 过期与续期。

经分发器的进程内接口订阅（内核集成测试将来用的就是这条路），不开任何套接字。
"""
from __future__ import annotations

import asyncio

import pytest

from dmr_standin.stream import StreamClosed, StreamRefused
from standin_kit import MH, T0, call, make_standin


async def _key(disp) -> str:
    status, body, _ = await call(disp, "POST", "/api/private/listen_key")
    assert status == 200
    assert body["expires_ms"] == T0 + 60 * 60_000
    return body["listen_key"]


async def _order(disp, side="BUY", ps="LONG", qty="0.01", cid="DKA1-1-1-o"):
    status, body, _ = await call(disp, "POST", "/api/private/order", body={
        "symbol": "BTCUSDT", "side": side, "position_side": ps, "type": "MARKET", "qty": qty,
        "client_order_id": cid})
    assert status == 200, body
    return body


async def test_market_fill_frames_account_first_by_default():
    disp, _, clock = make_standin()
    sub = disp.subscribe(await _key(disp))
    order = await _order(disp)
    frames = sub.drain()
    assert [f["type"] for f in frames] == ["order_update", "account_update", "order_update"]
    new, acct, trade = frames
    assert new["execution"] == "NEW" and new["fill"] is None
    assert new["order"]["client_order_id"] == "DKA1-1-1-o"  # 数据流的订单事件带自定义编号
    assert trade["execution"] == "TRADE" and trade["order"]["status"] == "FILLED"
    assert trade["order"]["client_order_id"] == "DKA1-1-1-o"
    assert trade["fill"]["order_id"] == order["order_id"] and trade["fill"]["qty"] == "0.01"
    assert acct["reason"] == "ORDER"
    longs = [p for p in acct["positions"] if p["position_side"] == "LONG"]
    assert longs[0]["symbol"] == "BTCUSDT" and longs[0]["qty"] == "0.01"
    assert acct["balances"][0]["asset"] == "USDT" and "wallet_balance" in acct["balances"][0]
    assert all(f["event_time_ms"] == clock() for f in frames)


async def test_order_first_switch_puts_trade_before_account():
    disp, _, _ = make_standin()
    await call(disp, "PUT", "/dev/faults", body={"behaviors": {"fill_event_order": "order_first"}}, headers=MH)
    sub = disp.subscribe(await _key(disp))
    await _order(disp)
    kinds = [(f["type"], f.get("execution")) for f in sub.drain()]
    assert kinds == [("order_update", "NEW"), ("order_update", "TRADE"), ("account_update", None)]


async def test_account_update_lists_both_sides_including_closed_one():
    disp, _, _ = make_standin()
    await _order(disp)
    sub = disp.subscribe(await _key(disp))
    await _order(disp, side="SELL", ps="LONG", cid="DKA1-0-2-c")
    acct = [f for f in sub.drain() if f["type"] == "account_update"][0]
    longs = [p for p in acct["positions"] if p["position_side"] == "LONG"]
    assert longs and longs[0]["qty"] == "0"  # 平掉的一侧以 0 出现


async def test_algo_events_only_pushed_when_switch_on():
    disp, feed, _ = make_standin()
    sub = disp.subscribe(await _key(disp))
    await _order(disp)
    sub.drain()
    body = {"symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG", "type": "STOP_MARKET",
            "trigger_price": "49000", "working_type": "CONTRACT_PRICE", "close_position": True, "qty": None,
            "client_algo_id": "DKA1-0-1-s"}
    status, algo, _ = await call(disp, "POST", "/api/private/algo_order", body=body)
    assert status == 200
    assert sub.drain() == []  # 默认不推（实测第 11 项保守取值）
    await call(disp, "PUT", "/dev/faults", body={"behaviors": {"push_algo_events": True}}, headers=MH)
    status, algo2, _ = await call(disp, "POST", "/api/private/algo_order",
                                  body={**body, "client_algo_id": "DKA1-0-2-t", "type": "TAKE_PROFIT_MARKET",
                                        "trigger_price": "52000"})
    assert status == 200
    frames = sub.drain()
    assert [f["type"] for f in frames] == ["algo_update"]
    assert frames[0]["algo"]["client_algo_id"] == "DKA1-0-2-t" and frames[0]["algo"]["status"] == "NEW"
    feed.set_price("BTCUSDT", "48000")
    disp.tick()
    statuses = [f["algo"]["status"] for f in sub.drain() if f["type"] == "algo_update"]
    assert statuses[-1] == "FINISHED" and "TRIGGERED" in statuses


async def test_heartbeat_every_10_seconds():
    disp, _, clock = make_standin()
    sub = disp.subscribe(await _key(disp))
    clock.advance(9_999)
    disp.tick()
    assert sub.drain() == []
    clock.advance(1)
    disp.tick()
    assert sub.drain() == [{"type": "heartbeat", "server_time_ms": T0 + 10_000}]
    clock.advance(10_000)
    disp.tick()
    assert [f["type"] for f in sub.drain()] == ["heartbeat"]


async def test_listen_key_expires_after_60_minutes_without_keepalive():
    disp, _, clock = make_standin()
    key = await _key(disp)
    sub = disp.subscribe(key)
    clock.advance(60 * 60_000 - 1)
    disp.tick()
    assert all(f["type"] == "heartbeat" for f in sub.drain())
    clock.advance(1)
    disp.tick()
    frames = []
    with pytest.raises(StreamClosed):
        while True:
            frames.append(await asyncio.wait_for(sub.get(), 1))
    assert frames[-1] == {"type": "listen_key_expired", "event_time_ms": T0 + 60 * 60_000}
    assert sub.closed
    status, body, _ = await call(disp, "PUT", "/api/private/listen_key", body={"listen_key": key})
    assert status == 400 and body["code"] == -1125
    with pytest.raises(StreamRefused):
        disp.subscribe(key)


async def test_keepalive_extends_listen_key():
    disp, _, clock = make_standin()
    key = await _key(disp)
    sub = disp.subscribe(key)
    clock.advance(30 * 60_000)
    status, body, _ = await call(disp, "PUT", "/api/private/listen_key", body={"listen_key": key})
    assert status == 200 and body == {"expires_ms": clock() + 60 * 60_000}
    clock.advance(45 * 60_000)
    disp.tick()
    assert not sub.closed


async def test_post_listen_key_returns_the_active_key_and_extends_it():
    disp, _, clock = make_standin()
    key = await _key(disp)
    clock.advance(1000)
    status, body, _ = await call(disp, "POST", "/api/private/listen_key")
    assert body["listen_key"] == key and body["expires_ms"] == clock() + 60 * 60_000


async def test_keepalive_failure_switch_returns_1125():
    disp, _, _ = make_standin()
    key = await _key(disp)
    await call(disp, "PUT", "/dev/faults", body={"faults": {"listen_key_keepalive_fail": {"enabled": True}}},
               headers=MH)
    status, body, _ = await call(disp, "PUT", "/api/private/listen_key", body={"listen_key": key})
    assert status == 400 and body["code"] == -1125
    await call(disp, "POST", "/dev/faults/reset", headers=MH)
    status, _, _ = await call(disp, "PUT", "/api/private/listen_key", body={"listen_key": key})
    assert status == 200


async def test_stream_unavailable_switch_refuses_subscription():
    disp, _, _ = make_standin()
    key = await _key(disp)
    await call(disp, "PUT", "/dev/faults", body={"behaviors": {"stream_available": False}}, headers=MH)
    with pytest.raises(StreamRefused):
        disp.subscribe(key)


async def test_unknown_listen_key_is_refused():
    disp, _, _ = make_standin()
    with pytest.raises(StreamRefused):
        disp.subscribe("not-a-key")


async def test_client_close_stops_delivery():
    disp, _, _ = make_standin()
    sub = disp.subscribe(await _key(disp))
    sub.close()
    await _order(disp)
    assert sub.closed and sub.drain() == []
