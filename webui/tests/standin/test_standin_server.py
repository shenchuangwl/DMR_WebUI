"""HTTP/WebSocket 外壳（dmr_standin.server）：用 starlette 的 TestClient 在进程内调用，不开任何套接字。

只测几条代表性路由和一次 WebSocket 订阅；协议细节在分发器的测试里。
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dmr_standin.server import create_app
from standin_kit import KH, MH, T0, make_standin

ORDER = {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "type": "MARKET", "qty": "0.01",
         "client_order_id": "DKA1-1-1-o"}


@pytest.fixture
def client():
    disp, feed, clock = make_standin()
    with TestClient(create_app(disp, background=False)) as c:
        c.standin = disp
        yield c


def test_public_route_and_weight_header(client):
    r = client.get("/api/public/time")
    assert r.status_code == 200 and r.json() == {"server_time_ms": T0}
    assert r.headers["X-MBX-USED-WEIGHT-1M"] == "1"
    assert r.headers["content-type"].startswith("application/json")


def test_private_route_requires_token(client):
    assert client.post("/api/private/order", json=ORDER).status_code == 401
    r = client.post("/api/private/order", json=ORDER, headers=KH)
    assert r.status_code == 200 and r.json()["status"] == "FILLED"
    r = client.get("/api/private/order", params={"symbol": "BTCUSDT", "client_order_id": "DKA1-1-1-o"}, headers=KH)
    assert r.status_code == 200 and r.json()["client_order_id"] == "DKA1-1-1-o"
    r = client.delete("/api/private/order", params={"symbol": "BTCUSDT", "client_order_id": "DKA1-1-1-o"},
                      headers=KH)
    assert r.status_code == 400 and r.json()["code"] == -2011


def test_rate_limited_response_carries_retry_after(client):
    client.put("/dev/faults", json={"faults": {"rate_limit": {"enabled": True, "scope": "all"}}}, headers=MH)
    r = client.get("/api/private/account", headers=KH)
    assert r.status_code == 429 and r.json()["code"] == -1003 and "retry-after" in r.headers


def test_drop_closes_without_a_complete_response(client):
    client.put("/dev/faults", json={"faults": {"order_timeout": {"enabled": True, "mode": "drop", "hang_s": 0.01}}},
               headers=MH)
    r = client.post("/api/private/order", json=ORDER, headers=KH)
    assert r.content == b""  # 没有任何响应体：连接被断开
    assert client.get("/api/private/positions", headers=KH).json() == []


def test_websocket_subscription_receives_order_frames(client):
    key = client.post("/api/private/listen_key", headers=KH).json()["listen_key"]
    with client.websocket_connect(f"/ws/stream?listen_key={key}") as ws:
        assert client.post("/api/private/order", json=ORDER, headers=KH).status_code == 200
        frames = [ws.receive_json() for _ in range(3)]
        assert [f["type"] for f in frames] == ["order_update", "account_update", "order_update"]
        assert frames[0]["order"]["client_order_id"] == "DKA1-1-1-o"


def test_websocket_with_bad_listen_key_is_refused(client):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws/stream?listen_key=nope"):
            pass
    assert e.value.code == 1008


def test_websocket_closed_by_stream_drop(client):
    key = client.post("/api/private/listen_key", headers=KH).json()["listen_key"]
    with client.websocket_connect(f"/ws/stream?listen_key={key}") as ws:
        client.put("/dev/faults", json={"faults": {"stream_drop": {"enabled": True, "mode": "close"}}}, headers=MH)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
