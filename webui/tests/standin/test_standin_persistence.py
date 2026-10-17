"""持久化（docs/设计约定.md 7.1）：两把令牌（0600）、状态快照（原子写、变化后 1 秒内落盘）、操作留痕、--no-persist。"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time

from dmr_standin.engine import Engine, PartialFill
from dmr_standin.persistence import ensure_tokens
from standin_kit import D, MH, call, make_engine, make_standin, open_long, open_short, stop


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_tokens_are_created_once_with_0600(tmp_path):
    state_dir = tmp_path / "standin"
    kernel, manual = ensure_tokens(state_dir)
    assert kernel != manual and len(kernel) >= 32 and len(manual) >= 32
    assert _mode(state_dir / "kernel.token") == 0o600
    assert _mode(state_dir / "manual.token") == 0o600
    assert _mode(state_dir) == 0o700
    assert ensure_tokens(state_dir) == (kernel, manual)  # 再次启动沿用
    assert (state_dir / "kernel.token").read_text().strip() == kernel


def test_empty_token_file_is_an_error_not_silently_replaced(tmp_path):
    state_dir = tmp_path / "standin"
    ensure_tokens(state_dir)
    (state_dir / "kernel.token").write_text("")
    try:
        ensure_tokens(state_dir)
    except ValueError:
        pass
    else:
        raise AssertionError("空令牌文件应当报错")


def _busy_engine():
    engine, feed, switches, clock = make_engine(dual_close_position_stop="allow")
    open_long(engine, qty="0.02")
    open_short(engine, symbol="ETHUSDT", qty="0.1", cid="DKA1-1-2-o")
    feed.set_price("BTCUSDT", "51000")
    engine.place_market_order(symbol="BTCUSDT", side="SELL", position_side="LONG", qty=D("0.01"),
                              client_order_id="DKA1-0-3-p")
    stop(engine, trigger="49000", cid="DKA1-0-4-s")
    stop(engine, trigger="48000", close_position=False, qty="0.01", cid="DKA1-0-5-s")
    engine.place_market_order(symbol="DOGEUSDT", side="BUY", position_side="LONG", qty=D("100"),
                              client_order_id="DKA1-1-6-o",
                              partial=PartialFill(ratio=D("0.5"), remainder="later", delay_ms=60_000))
    engine.set_leverage("ETHUSDT", 7)
    engine.set_price_override("DOGEUSDT", price=D("0.25"))
    engine.settle_funding()
    return engine, feed, switches, clock


def _view(engine: Engine) -> dict:
    return {
        "account": engine.account(),
        "positions": engine.positions(),
        "open_orders": engine.open_orders(),
        "open_algos": engine.open_algo_orders(),
        "trades": engine.trades(symbol="BTCUSDT") + engine.trades(symbol="ETHUSDT") + engine.trades(symbol="DOGEUSDT"),
        "income": engine.income(limit=1000),
        "leverage": engine.leverage_info("ETHUSDT"),
        "quote": engine.quote("DOGEUSDT"),
    }


def test_engine_snapshot_round_trip_restores_identical_state():
    engine, feed, switches, clock = _busy_engine()
    snap = json.loads(json.dumps(engine.to_snapshot()))  # 必须能存成 JSON
    restored = Engine.from_snapshot(snap, feed=feed, switches=switches, clock=clock)
    assert _view(restored) == _view(engine)
    # 编号计数器也恢复：新单不会撞号
    old_ids = {o["order_id"] for o in engine.open_orders()} | {t["order_id"] for t in _view(engine)["trades"]}
    new = open_long(restored, qty="0.01", cid="DKA1-1-7-o")
    assert new["order_id"] not in old_ids
    # 部分成交的余量在恢复后照常到期成交
    clock.advance(60_000)
    restored.check_triggers()
    doge = [p for p in restored.positions() if p["symbol"] == "DOGEUSDT"][0]
    assert doge["qty"] == "100"


async def test_dispatcher_writes_state_within_a_second_and_restart_restores(tmp_path):
    state_dir = tmp_path / "standin"
    disp, feed, clock = make_standin(state_dir=state_dir, persist=True)
    await disp.start()
    try:
        await call(disp, "POST", "/api/private/order", body={"symbol": "BTCUSDT", "side": "BUY",
                                                              "position_side": "LONG", "type": "MARKET",
                                                              "qty": "0.01", "client_order_id": "DKA1-1-1-o"})
        await call(disp, "PUT", "/dev/faults", body={"behaviors": {"push_algo_events": True},
                                                      "faults": {"listen_key_keepalive_fail": {"enabled": True}}},
                   headers=MH)
        changed_at = time.monotonic()
        state = state_dir / "state.json"
        while time.monotonic() - changed_at < 1.5:
            if state.exists() and "DKA1-1-1-o" in state.read_text():
                break
            await asyncio.sleep(0.05)
        assert time.monotonic() - changed_at <= 1.2  # 变化后 1 秒内落盘（留 0.2 秒余量）
    finally:
        await disp.stop()
    assert _mode(state) == 0o600
    assert not [p for p in state_dir.iterdir() if p.name.endswith(".tmp")]  # 原子写不留临时文件
    data = json.loads(state.read_text())
    assert data["behaviors"]["push_algo_events"] is True
    ops = (state_dir / "ops.log").read_text().splitlines()
    assert json.loads(ops[-1])["op"] == "faults_update"
    # 重启：同一目录新建分发器，从快照载入
    again, _, _ = make_standin(state_dir=state_dir, persist=True)
    _, positions, _ = await call(again, "GET", "/api/private/positions")
    assert [(p["symbol"], p["qty"]) for p in positions] == [("BTCUSDT", "0.01")]
    _, faults, _ = await call(again, "GET", "/dev/faults", headers=MH)
    assert faults["behaviors"]["push_algo_events"] is True  # 行为开关随快照保留
    assert faults["faults"]["listen_key_keepalive_fail"]["enabled"] is False  # 故障开关重启即复位
    _, log, _ = await call(again, "GET", "/dev/ops_log", headers=MH)
    assert log and log[-1]["op"] == "faults_update"  # 留痕也能读回


async def test_no_persist_writes_no_state_or_ops_files(tmp_path):
    state_dir = tmp_path / "standin"
    disp, _, _ = make_standin(state_dir=state_dir, persist=False)
    await disp.start()
    try:
        await call(disp, "POST", "/dev/manual/order", body={"symbol": "BTCUSDT", "side": "BUY",
                                                             "position_side": "LONG", "qty": "0.01"}, headers=MH)
        disp.flush()
        await asyncio.sleep(0.6)
    finally:
        await disp.stop()
    names = {p.name for p in state_dir.iterdir()} if state_dir.exists() else set()
    assert "state.json" not in names and "ops.log" not in names


def test_corrupt_state_file_is_not_silently_discarded(tmp_path):
    state_dir = tmp_path / "standin"
    state_dir.mkdir(mode=0o700)
    (state_dir / "state.json").write_text("{broken")
    try:
        make_standin(state_dir=state_dir, persist=True)
    except ValueError:
        pass
    else:
        raise AssertionError("损坏的快照应当拒绝启动，而不是当成空账户")
    assert (state_dir / "state.json").read_text() == "{broken"  # 原样保留
