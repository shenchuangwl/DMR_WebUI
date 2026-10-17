"""九种故障的注入（docs/设计约定.md 7.5；报告 4.11）。经分发器调用；真实等待只用很短的时限。

「进程被杀后重启」由内核侧测试实现；「停机期间止损触发」见 test_standin_http 的价格覆盖用例；
「手动撤掉系统的止损」见 test_standin_http 的 /dev/manual/cancel 用例。
"""
from __future__ import annotations

import asyncio

import pytest

from dmr_standin.dispatcher import Disconnect
from dmr_standin.stream import StreamClosed, StreamRefused
from standin_kit import MH, call, make_standin

ORDER = {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "type": "MARKET", "qty": "0.02",
         "client_order_id": "DKA1-1-1-o"}
STOP = {"symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG", "type": "STOP_MARKET",
        "trigger_price": "49000", "working_type": "CONTRACT_PRICE", "close_position": True, "qty": None,
        "client_algo_id": "DKA1-0-1-s"}


async def _faults(disp, **faults):
    status, body, _ = await call(disp, "PUT", "/dev/faults", body={"faults": faults}, headers=MH)
    assert status == 200, body
    return body


async def _positions(disp):
    status, body, _ = await call(disp, "GET", "/api/private/positions")
    assert status == 200
    return body


# ------------------------------------------------------------------ 1 下单超时


async def test_order_timeout_accept_then_hang_fills_but_holds_the_response():
    disp, _, _ = make_standin()
    await _faults(disp, order_timeout={"enabled": True, "mode": "accept_then_hang", "hang_s": 5, "count": 1})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(call(disp, "POST", "/api/private/order", body=ORDER), 0.1)
    positions = await _positions(disp)
    assert [(p["position_side"], p["qty"]) for p in positions] == [("LONG", "0.02")]  # 成交了
    status, got, _ = await call(disp, "GET", "/api/private/order",
                                params={"symbol": "BTCUSDT", "client_order_id": "DKA1-1-1-o"})
    assert status == 200 and got["status"] == "FILLED"  # 按编号能回查到


async def test_order_timeout_accept_then_hang_eventually_answers():
    disp, _, _ = make_standin()
    await _faults(disp, order_timeout={"enabled": True, "hang_s": 0.05})
    status, body, _ = await asyncio.wait_for(call(disp, "POST", "/api/private/order", body=ORDER), 2)
    assert status == 200 and body["status"] == "FILLED"


async def test_order_timeout_drop_does_not_execute_and_disconnects():
    disp, _, _ = make_standin()
    await _faults(disp, order_timeout={"enabled": True, "mode": "drop", "hang_s": 0.05})
    with pytest.raises(Disconnect):
        await asyncio.wait_for(call(disp, "POST", "/api/private/order", body=ORDER), 2)
    assert await _positions(disp) == []
    status, err, _ = await call(disp, "GET", "/api/private/order",
                                params={"symbol": "BTCUSDT", "client_order_id": "DKA1-1-1-o"})
    assert status == 400 and err["code"] == -2013  # 没有受理


async def test_order_timeout_scope_market_does_not_touch_algo_orders():
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body=ORDER)
    await _faults(disp, order_timeout={"enabled": True, "mode": "drop", "scope": "market", "hang_s": 0.05})
    status, _, _ = await asyncio.wait_for(call(disp, "POST", "/api/private/algo_order", body=STOP), 2)
    assert status == 200


async def test_order_timeout_scope_algo_and_all():
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body=ORDER)
    await _faults(disp, order_timeout={"enabled": True, "mode": "drop", "scope": "algo", "hang_s": 0.05})
    with pytest.raises(Disconnect):
        await asyncio.wait_for(call(disp, "POST", "/api/private/algo_order", body=STOP), 2)
    status, _, _ = await asyncio.wait_for(call(disp, "POST", "/api/private/order",
                                               body={**ORDER, "client_order_id": "DKA1-1-2-o"}), 2)
    assert status == 200
    await _faults(disp, order_timeout={"enabled": True, "mode": "drop", "scope": "all", "hang_s": 0.05})
    with pytest.raises(Disconnect):
        await asyncio.wait_for(call(disp, "POST", "/api/private/order",
                                    body={**ORDER, "client_order_id": "DKA1-1-3-o"}), 2)


async def test_cancel_result_unknown_when_cancel_times_out():
    """撤单的第三种结果「未知」：scope=all 时撤单也会超时（accept_then_hang：撤掉了但回执扣住）。"""
    disp, feed, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body=ORDER)
    await call(disp, "POST", "/api/private/algo_order", body=STOP)
    await _faults(disp, order_timeout={"enabled": True, "scope": "all", "hang_s": 5})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(call(disp, "DELETE", "/api/private/algo_order",
                                    params={"symbol": "BTCUSDT", "client_algo_id": "DKA1-0-1-s"}), 0.1)
    status, listed, _ = await call(disp, "GET", "/api/private/open_algo_orders")
    assert listed == []


# ------------------------------------------------------------------ 2 止损建单失败


async def test_algo_create_fail_reject_uses_default_code_and_creates_nothing():
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body=ORDER)
    await _faults(disp, algo_create_fail={"enabled": True})
    status, err, _ = await call(disp, "POST", "/api/private/algo_order", body=STOP)
    assert status == 400 and err["code"] == -2010
    _, listed, _ = await call(disp, "GET", "/api/private/open_algo_orders")
    assert listed == []


async def test_algo_create_fail_reject_with_custom_code():
    disp, _, _ = make_standin()
    await _faults(disp, algo_create_fail={"enabled": True, "code": -4045})
    status, err, _ = await call(disp, "POST", "/api/private/algo_order", body=STOP)
    assert status == 400 and err["code"] == -4045


async def test_algo_create_fail_unknown_is_503_and_creates_nothing():
    disp, _, _ = make_standin()
    await call(disp, "POST", "/api/private/order", body=ORDER)
    await _faults(disp, algo_create_fail={"enabled": True, "mode": "unknown", "count": 1})
    status, err, _ = await call(disp, "POST", "/api/private/algo_order", body=STOP)
    assert status == 503 and err["code"] == -1007
    assert "execution status unknown" in err["msg"]
    _, listed, _ = await call(disp, "GET", "/api/private/open_algo_orders")
    assert listed == []
    status, _, _ = await call(disp, "POST", "/api/private/algo_order", body=STOP)  # count 用完自动恢复
    assert status == 200


async def test_algo_create_fail_does_not_affect_market_orders():
    disp, _, _ = make_standin()
    await _faults(disp, algo_create_fail={"enabled": True})
    status, _, _ = await call(disp, "POST", "/api/private/order", body=ORDER)
    assert status == 200


# ------------------------------------------------------------------ 3 持仓查询失败


async def test_position_query_fail_error_mode_is_500():
    disp, _, _ = make_standin()
    await _faults(disp, position_query_fail={"enabled": True, "count": 1})
    status, err, _ = await call(disp, "GET", "/api/private/positions")
    assert status == 500 and isinstance(err["code"], int)
    assert (await _positions(disp)) == []  # 用完一次后恢复
    status, _, _ = await call(disp, "GET", "/api/private/account")
    assert status == 200  # 只影响持仓查询


async def test_position_query_fail_hang_mode_never_answers_in_time():
    disp, _, _ = make_standin()
    await _faults(disp, position_query_fail={"enabled": True, "mode": "hang"})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(call(disp, "GET", "/api/private/positions"), 0.1)


# ------------------------------------------------------------------ 5 部分成交


async def test_partial_fill_remainder_later():
    disp, _, clock = make_standin()
    await _faults(disp, partial_fill={"enabled": True, "ratio": "0.5", "remainder": "later", "delay_s": 5})
    status, order, _ = await call(disp, "POST", "/api/private/order", body=ORDER)
    assert status == 200 and order["status"] == "PARTIALLY_FILLED" and order["filled_qty"] == "0.01"
    assert [p["qty"] for p in await _positions(disp)] == ["0.01"]
    _, listed, _ = await call(disp, "GET", "/api/private/open_orders", params={"symbol": "BTCUSDT"})
    assert [o["order_id"] for o in listed] == [order["order_id"]]
    clock.advance(4_999)
    disp.tick()
    assert [p["qty"] for p in await _positions(disp)] == ["0.01"]
    clock.advance(1)
    disp.tick()
    assert [p["qty"] for p in await _positions(disp)] == ["0.02"]
    _, got, _ = await call(disp, "GET", "/api/private/order", params={"symbol": "BTCUSDT",
                                                                       "order_id": order["order_id"]})
    assert got["status"] == "FILLED" and got["filled_qty"] == "0.02"
    _, trades, _ = await call(disp, "GET", "/api/private/trades", params={"symbol": "BTCUSDT"})
    assert [t["qty"] for t in trades] == ["0.01", "0.01"]


async def test_partial_fill_remainder_expire():
    disp, _, clock = make_standin()
    await _faults(disp, partial_fill={"enabled": True, "ratio": "0.25", "remainder": "expire", "delay_s": 1})
    key = (await call(disp, "POST", "/api/private/listen_key"))[1]["listen_key"]
    sub = disp.subscribe(key)
    status, order, _ = await call(disp, "POST", "/api/private/order",
                                  body={**ORDER, "qty": "0.04"})
    assert order["status"] == "PARTIALLY_FILLED" and order["filled_qty"] == "0.01"
    clock.advance(1000)
    disp.tick()
    _, got, _ = await call(disp, "GET", "/api/private/order", params={"symbol": "BTCUSDT",
                                                                       "order_id": order["order_id"]})
    assert got["status"] == "EXPIRED" and got["raw_status"] == "EXPIRED_IN_MATCH" and got["filled_qty"] == "0.01"
    assert [p["qty"] for p in await _positions(disp)] == ["0.01"]  # 以已成交的数量为准
    execs = [f["execution"] for f in sub.drain() if f["type"] == "order_update"]
    assert execs == ["NEW", "TRADE", "EXPIRED"]


async def test_partial_fill_count_and_scope_only_api_market_orders():
    disp, _, _ = make_standin()
    await _faults(disp, partial_fill={"enabled": True, "count": 1})
    _, manual, _ = await call(disp, "POST", "/dev/manual/order", body={"symbol": "BTCUSDT", "side": "BUY",
                                                                        "position_side": "LONG", "qty": "0.02"},
                              headers=MH)
    assert manual["status"] == "FILLED"  # 模拟手动下单不受影响
    _, first, _ = await call(disp, "POST", "/api/private/order", body=ORDER)
    _, second, _ = await call(disp, "POST", "/api/private/order", body={**ORDER, "client_order_id": "DKA1-1-2-o"})
    assert first["status"] == "PARTIALLY_FILLED" and second["status"] == "FILLED"


# ------------------------------------------------------------------ 7 数据流断线


async def _sub(disp):
    status, body, _ = await call(disp, "POST", "/api/private/listen_key")
    return body["listen_key"], disp.subscribe(body["listen_key"])


async def test_stream_drop_close_closes_and_refuses_reconnect_for_duration():
    disp, _, clock = make_standin()
    key, sub = await _sub(disp)
    await _faults(disp, stream_drop={"enabled": True, "mode": "close", "duration_s": 30})
    with pytest.raises(StreamClosed):
        await asyncio.wait_for(sub.get(), 1)
    with pytest.raises(StreamRefused):
        disp.subscribe(key)
    clock.advance(30_000)
    disp.tick()
    again = disp.subscribe(key)
    await call(disp, "POST", "/api/private/order", body=ORDER)
    assert any(f["type"] == "order_update" for f in again.drain())


async def test_stream_drop_silent_keeps_connection_but_sends_nothing():
    disp, _, clock = make_standin()
    key, sub = await _sub(disp)
    await _faults(disp, stream_drop={"enabled": True, "mode": "silent", "duration_s": 60})
    await call(disp, "POST", "/api/private/order", body=ORDER)
    for _ in range(5):
        clock.advance(10_000)
        disp.tick()
    late = disp.subscribe(key)  # 窗口内新连上的也收不到
    clock.advance(9_999)
    disp.tick()
    assert sub.drain() == [] and late.drain() == []  # 连心跳也没有
    assert not sub.closed and not late.closed
    clock.advance(1)  # 时长到
    disp.tick()
    await call(disp, "POST", "/api/private/order", body={**ORDER, "client_order_id": "DKA1-1-2-o"})
    frames = sub.drain()
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "heartbeat" and "order_update" in kinds  # 恢复发帧
    cids = {f["order"]["client_order_id"] for f in frames if f["type"] == "order_update"}
    assert cids == {"DKA1-1-2-o"}  # 窗口内的事件已丢失，不补发
    assert [f["type"] for f in late.drain()][0] == "heartbeat"


# ------------------------------------------------------------------ 8 限频被拒


async def test_rate_limit_429_with_retry_after_and_count():
    disp, _, _ = make_standin()
    await _faults(disp, rate_limit={"enabled": True, "http": 429, "scope": "all", "count": 2})
    for _ in range(2):
        status, err, headers = await call(disp, "GET", "/api/private/account")
        assert status == 429 and err["code"] == -1003 and int(headers["Retry-After"]) >= 1
        assert "X-MBX-USED-WEIGHT-1M" in headers
    status, _, _ = await call(disp, "GET", "/api/private/account")
    assert status == 200  # count 用完后自动恢复


async def test_rate_limit_418_scope_writes_only():
    disp, _, _ = make_standin()
    await _faults(disp, rate_limit={"enabled": True, "http": 418, "scope": "writes"})
    status, _, _ = await call(disp, "GET", "/api/public/time")
    assert status == 200
    status, _, _ = await call(disp, "GET", "/api/private/positions")
    assert status == 200
    status, err, headers = await call(disp, "POST", "/api/private/order", body=ORDER)
    assert status == 418 and err["code"] == -1003 and "Retry-After" in headers
    assert await _positions(disp) == []  # 被拒的单没有执行


async def test_rate_limit_scope_reads_hits_public_and_private_reads():
    disp, _, _ = make_standin()
    await _faults(disp, rate_limit={"enabled": True})  # 默认 429、reads
    status, _, _ = await call(disp, "GET", "/api/public/klines", params={"symbol": "BTCUSDT", "interval": "15m",
                                                                         "limit": "1"})
    assert status == 429
    status, _, _ = await call(disp, "POST", "/api/private/order", body=ORDER)
    assert status == 200
    status, _, _ = await call(disp, "GET", "/dev/state", headers=MH)
    assert status == 200  # 开发接口不限频


# ------------------------------------------------------------------ 复位


async def test_faults_reset_restores_everything():
    disp, _, _ = make_standin()
    await _faults(disp, rate_limit={"enabled": True, "scope": "all"},
                  position_query_fail={"enabled": True})
    status, body, _ = await call(disp, "POST", "/dev/faults/reset", headers=MH)
    assert status == 200 and not any(f["enabled"] for f in body["faults"].values())
    status, _, _ = await call(disp, "GET", "/api/private/positions")
    assert status == 200
