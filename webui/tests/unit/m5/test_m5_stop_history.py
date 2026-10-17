"""止损价历史（stop_history 表，迁移 0002）：登记簿改止损价时同事务追加一行；只追加。"""
from __future__ import annotations

from decimal import Decimal

from dmr_kernel.core.types import ProtectionState
from dmr_kernel.ledger import repo_stops

D = Decimal


async def test_registry_records_each_stop_price_change(reg_rig, clock):
    rig = reg_rig
    await rig.up()
    pos = await rig.open_strategy()
    await rig.registry.set_protection(pos.id, state=ProtectionState.SYSTEM, stop_price=D(90), stop_client_id="c1")
    first_ms = clock.now_ms()
    clock.advance(60_000)
    await rig.registry.set_protection(pos.id, state=ProtectionState.SYSTEM, stop_price=D(90))  # 价格没变：不记
    await rig.registry.set_protection(pos.id, state=ProtectionState.SYSTEM, note="只改备注")   # 没给价格：不记
    clock.advance(60_000)
    await rig.registry.set_protection(pos.id, state=ProtectionState.SYSTEM, stop_price=D(95))
    clock.advance(60_000)
    await rig.registry.set_protection(pos.id, state=ProtectionState.NONE, clear_stop=True)
    history = await rig.ledger.read(repo_stops.history, pos.id)
    assert [(h.price, h.at_ms) for h in history] == [
        (D(90), first_ms), (D(95), first_ms + 120_000), (None, first_ms + 180_000)]
    assert all(h.position_id == pos.id for h in history)


async def test_history_is_per_position_and_empty_when_never_stopped(reg_rig):
    rig = reg_rig
    await rig.up()
    a = await rig.open_strategy(symbol="BTCUSDT")
    b = await rig.open_strategy(symbol="ETHUSDT", intent_id=2)
    await rig.registry.set_protection(a.id, state=ProtectionState.SYSTEM, stop_price=D(1))
    assert len(await rig.ledger.read(repo_stops.history, a.id)) == 1
    assert await rig.ledger.read(repo_stops.history, b.id) == []


async def test_segments_close_at_next_change_or_position_close(reg_rig, clock):
    rig = reg_rig
    await rig.up()
    pos = await rig.open_strategy()
    t0 = clock.now_ms()
    await rig.registry.set_protection(pos.id, state=ProtectionState.SYSTEM, stop_price=D(90))
    clock.advance(1000)
    await rig.registry.set_protection(pos.id, state=ProtectionState.SYSTEM, stop_price=D(92))
    segs = await rig.ledger.read(repo_stops.segments, "BTCUSDT", since_ms=0)
    assert segs == [
        {"position_id": pos.id, "price": D(90), "from_ms": t0, "to_ms": t0 + 1000},
        {"position_id": pos.id, "price": D(92), "from_ms": t0 + 1000, "to_ms": None},  # 仍在持有：没有终点
    ]
