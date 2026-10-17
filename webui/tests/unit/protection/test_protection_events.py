"""保护单管理：事件订阅与后台任务的生命周期（start/stop、各事件的触发与忽略、出错与崩溃点的处理）。"""
from __future__ import annotations

import asyncio

from dmr_kernel.core.events import CandlesReady, IntentStateChanged
from dmr_kernel.core.types import ProtectionState
from protection_kit import BAR0, LONG, SYM, TF_MS, D, history_then

RISING = [100.5, 101.0, 101.5, 102.0, 102.5, 103.0]


async def test_a_registered_position_is_protected_through_the_event(rig):
    await rig.start(protection=True)
    rec = await rig.open_strategy()
    await rig.settle()
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and len(rig.exchange.stops()) == 1


async def test_a_takeover_registration_event_for_a_pending_position_protects_it(rig):
    """待确认仓转接管（PositionChanged，cause=takeover）也要触发保护；待确认仓本身（登记时的事件）不触发。"""
    from dmr_kernel.core.types import PendingReason

    await rig.start(protection=True)
    rig.exchange.set_held(SYM, LONG, "1", "100")
    pending = await rig.registry.register_pending(symbol=SYM, side=LONG, qty=D(1), avg_price=D(100),
                                                  reason=PendingReason.MODEL_NOT_ACTIVE)
    await rig.settle()
    assert rig.exchange.writes == [] and (await rig.row(pending.id)).protection_state is ProtectionState.NONE
    await rig.registry.begin_takeover(symbol=SYM, side=LONG, qty=D(1), avg_price=D(100), open_time_ms=BAR0 + 3_000,
                                      open_time_estimated=False, anchor_ms=BAR0 + 3_000, signal_exit=False,
                                      position_id=pending.id)
    await rig.settle()
    row = await rig.row(pending.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.status.value == "managed"


async def test_other_position_changes_do_not_trigger_anything(rig):
    await rig.start(protection=True)
    rec = await rig.open_strategy()
    await rig.settle()
    writes = len(rig.exchange.writes)
    await rig.registry.set_closing(rec.id, True, cause="partial_close")
    await rig.registry.set_closing(rec.id, False, cause="whatever")
    await rig.settle()
    assert len(rig.exchange.writes) == writes  # 已有保护，其它变化不重复挂


async def test_a_short_timeframe_candles_event_triggers_trailing(rig):
    await rig.start()
    rec = await rig.open_strategy()
    rec, _ = await rig.with_system_stop(rec, "97")
    rig.candles.set(SYM, history_then(RISING))
    rig.exchange.set_price(SYM, "105")
    await rig.protection.start()
    rig.bus.publish(CandlesReady(timeframe="15m", bar_open_ms=BAR0 + 6 * TF_MS, symbols=(SYM,), at_ms=rig.clock.now_ms()))
    await rig.settle()
    assert (await rig.row(rec.id)).stop_price == D("97.5")


async def test_a_long_timeframe_candles_event_is_ignored(rig):
    await rig.start()
    rec = await rig.open_strategy()
    rec, _ = await rig.with_system_stop(rec, "97")
    rig.candles.set(SYM, history_then(RISING))
    rig.exchange.set_price(SYM, "105")
    await rig.protection.start()
    rig.bus.publish(CandlesReady(timeframe="1h", bar_open_ms=BAR0 + 6 * TF_MS, symbols=(SYM,), at_ms=rig.clock.now_ms()))
    await rig.settle()
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).stop_cycle == 0


async def test_start_is_idempotent_and_stop_unsubscribes(rig):
    await rig.start()
    await rig.protection.start()
    await rig.protection.start()
    first = await rig.open_strategy()
    await rig.settle()
    assert len(rig.writes("create_stop_order")) == 1  # 重复 start 不会重复订阅、重复挂单
    await rig.protection.stop()
    second = await rig.open_strategy(symbol="ETHUSDT")
    await rig.settle()
    assert len(rig.writes("create_stop_order")) == 1 and (await rig.row(second.id)).stop_price is None
    assert (await rig.row(first.id)).protection_state is ProtectionState.SYSTEM


async def test_a_failing_handler_does_not_stop_later_events_from_being_handled(rig, monkeypatch):
    await rig.start(protection=True)
    calls = []
    original = rig.protection._ensure_locked

    async def flaky(position_id, reason):
        calls.append(position_id)
        if len(calls) == 1:
            raise RuntimeError("第一次就出错")
        return await original(position_id, reason)

    monkeypatch.setattr(rig.protection, "_ensure_locked", flaky)
    first = await rig.open_strategy()
    second = await rig.open_strategy(symbol="ETHUSDT")
    await rig.settle()
    assert calls == [first.id, second.id]
    assert (await rig.row(first.id)).protection_state is not ProtectionState.SYSTEM
    assert (await rig.row(second.id)).protection_state is ProtectionState.SYSTEM
    assert rig.protection.crashed is None


async def test_a_crash_point_inside_a_handler_kills_the_instance(rig):
    await rig.start(protection=True)
    rig.crash.arm("open_filled_before_stop")
    first = await rig.open_strategy()
    await rig.settle()
    assert rig.protection.crashed is not None and rig.exchange.writes == []
    second = await rig.open_strategy(symbol="ETHUSDT")  # 之后的事件一律不处理
    await rig.settle()
    await rig.protection.tick()
    assert rig.exchange.writes == [] and (await rig.row(second.id)).stop_price is None
    assert (await rig.row(first.id)).protection_state is not ProtectionState.SYSTEM


async def test_events_for_intents_it_did_not_submit_are_ignored(rig):
    await rig.start(protection=True)
    rig.bus.publish(IntentStateChanged(intent_id=4242, state="COMPLETED", action="stop_replace", symbol=SYM, side=LONG,
                                       at_ms=rig.clock.now_ms()))
    await rig.settle()
    assert rig.exchange.writes == [] and rig.notifier.alerts == []


async def test_idle_returns_at_once_when_nothing_is_running(rig):
    await rig.start(protection=True)
    await asyncio.wait_for(rig.protection.idle(5.0), 1.0)


async def test_the_default_wait_for_an_intent_comes_from_protect_target_s(rig):
    """没有指定 wait_s 时，等意图出结果的真实秒数取配置 protect_target_s。"""
    protection = rig.build_protection(wait_s=None)
    assert protection._wait_seconds() == float(rig.config["protect_target_s"])
    assert rig.build_protection(wait_s=0.25)._wait_seconds() == 0.25


async def test_the_unprotected_check_runs_after_each_state_change_not_only_on_tick(rig):
    """失败结论一下，抑制立即置上，不用等下一次 tick。"""
    await rig.start()
    for _ in range(4):
        rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    rec = await rig.open_takeover()
    await rig.protection.ensure_protected(rec.id, reason="takeover")
    for _ in range(3):
        rig.advance(10_000)
        await rig.protection.tick()
    assert rig.suppressed()
