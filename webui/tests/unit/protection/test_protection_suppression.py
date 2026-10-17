"""保护单管理：无保护抑制的置与解（F2 第 5 步；4.6「无保护持仓」只针对登记仓：接管中、托管中、平仓中）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import ExitReason, PendingReason, ProtectionState, Suppression
from protection_kit import LONG, SYM, D


async def test_an_unprotected_registered_position_sets_the_suppression_and_protecting_it_clears_it(rig):
    await rig.start()
    rec = await rig.open_strategy()
    assert not rig.suppressed()
    await rig.protection.check_unprotected()
    assert rig.suppressed()
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("97"), stop_client_id="x",
                                      stop_algo_id="1")
    await rig.protection.check_unprotected()
    assert not rig.suppressed()


@pytest.mark.parametrize("state, suppresses", [
    (ProtectionState.NONE, True),
    (ProtectionState.REBUILDING, False),  # 重建中不算无保护（挂单的这几秒、重试窗口里）
    (ProtectionState.SYSTEM, False),
    (ProtectionState.EXTERNAL, False),
])
async def test_only_the_none_protection_state_counts(rig, state, suppresses):
    await rig.start()
    rec = await rig.open_strategy()
    await rig.registry.set_protection(rec.id, state=state)
    await rig.protection.check_unprotected()
    assert rig.suppressed() is suppresses


async def test_taking_over_and_closing_positions_count_too(rig):
    await rig.start()
    takeover = await rig.open_takeover()
    await rig.protection.check_unprotected()
    assert rig.suppressed()  # 接管中、无保护
    await rig.registry.set_protection(takeover.id, state=ProtectionState.SYSTEM, stop_price=D("97"),
                                      stop_client_id="x", stop_algo_id="1")
    await rig.protection.check_unprotected()
    assert not rig.suppressed()
    strategy = await rig.open_strategy(symbol="ETHUSDT")
    await rig.registry.set_closing(strategy.id, True, cause="partial_close")  # 平仓中、无保护
    await rig.protection.check_unprotected()
    assert rig.suppressed()


async def test_pending_ignored_and_closed_positions_never_count(rig):
    await rig.start()
    pending = await rig.registry.register_pending(symbol="BTCUSDT", side=LONG, qty=D(1), avg_price=D(100),
                                                  reason=PendingReason.OUT_OF_SCOPE)
    ignored = await rig.registry.register_pending(symbol="ETHUSDT", side=LONG, qty=D(1), avg_price=D(100),
                                                  reason=PendingReason.OUT_OF_SCOPE)
    await rig.registry.ignore(ignored.id, expected_version=None)
    done = await rig.open_strategy()
    await rig.registry.mark_closed(done.id, exit_reason=ExitReason.MANUAL, closed_ms=rig.clock.now_ms())
    assert pending.protection_state is ProtectionState.NONE and ignored.protection_state is ProtectionState.NONE
    await rig.protection.check_unprotected()
    assert not rig.suppressed()


async def test_one_unprotected_position_among_protected_ones_is_enough(rig):
    await rig.start()
    a = await rig.open_strategy(symbol=SYM)
    b = await rig.open_strategy(symbol="ETHUSDT")
    for rec in (a, b):
        await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("97"),
                                          stop_client_id=f"c{rec.id}", stop_algo_id=str(rec.id))
    await rig.protection.check_unprotected()
    assert not rig.suppressed()
    await rig.registry.set_protection(b.id, state=ProtectionState.NONE)
    await rig.protection.check_unprotected()
    assert rig.suppressed()
    await rig.registry.set_protection(b.id, state=ProtectionState.SYSTEM)
    await rig.protection.check_unprotected()
    assert not rig.suppressed()


async def test_repeating_the_check_does_not_churn_the_model_state_version(rig):
    await rig.start()
    await rig.open_strategy()
    await rig.protection.check_unprotected()
    version = rig.modelstate.current().version
    for _ in range(3):
        await rig.protection.check_unprotected()
    assert rig.modelstate.current().version == version  # 已经置上的不重复写
    await rig.protection.check_unprotected()
    assert Suppression.UNPROTECTED in rig.modelstate.current().suppressions


async def test_unprotected_suppression_does_not_hide_other_suppressions(rig):
    await rig.start()
    await rig.modelstate.add_suppression(Suppression.RECONCILE_UNKNOWN, reason="测试")
    rec = await rig.open_strategy()
    await rig.protection.check_unprotected()
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("97"), stop_client_id="x",
                                      stop_algo_id="1")
    await rig.protection.check_unprotected()
    assert rig.modelstate.current().suppressions == frozenset({Suppression.RECONCILE_UNKNOWN})


# ---------------------------------------------------------------- tick 的兜底：没有保护周期的无保护仓也会被补上


async def test_tick_protects_a_registered_position_that_has_no_cycle_and_clears_the_suppression(rig):
    """重启后（或保护单管理起来之前就登记的仓）：tick 发现无保护的登记仓，补挂止损。"""
    await rig.start()
    rec = await rig.open_strategy()
    await rig.protection.check_unprotected()
    assert rig.suppressed()
    await rig.tick()
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and len(rig.exchange.stops()) == 1
    assert not rig.suppressed()


async def test_tick_with_nothing_to_do_changes_nothing(rig):
    await rig.start()
    rec = await rig.open_strategy()
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_price=D("97"), stop_client_id="x",
                                      stop_algo_id="1")
    version = (await rig.row(rec.id)).version
    await rig.tick(60_000)
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).version == version
