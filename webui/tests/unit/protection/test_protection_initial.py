"""保护单管理：初始止损的算法与挂单（F2 第 1、2 步）。

K 线用 ``protection_kit.frame_of`` 造：多仓止损 = 最近 6 根的最低价 − 该根 ATR（平的 K 线 half=1 时 ATR=2，最低价 99，原始止损 97），
再夹在距基准价 A%–B% 之间（替身环境 A=1、B=5），按价格步长（SOLUSDT 为 0.01）朝远离现价的方向取整。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.core.types import (
    ExitReason,
    IntentState,
    OrderOrigin,
    PendingReason,
    PositionStatus,
    ProtectionState,
    WorkingType,
)
from dmr_kernel.exchange import ids
from protection_kit import BAR0, LONG, SHORT, SYM, TF_MS, D, flat_history, frame_of


async def _protect(rig, rec, reason="registered"):
    status = await rig.protection.ensure_protected(rec.id, reason=reason)
    await rig.settle()
    return status, await rig.row(rec.id)


# ---------------------------------------------------------------- 价格：极值 ± ATR，夹在 A%–B% 之间


async def test_long_initial_stop_is_lowest_low_minus_atr_and_is_registered(rig):
    await rig.start()
    rig.candles.set(SYM, flat_history())
    rec = await rig.open_strategy(price="100")
    status, row = await _protect(rig, rec)
    assert status == "protected"
    calls = rig.writes("create_stop_order")
    assert len(calls) == 1
    call = calls[0]
    assert call["symbol"] == SYM and call["position_side"] is LONG and call["side"].value == "SELL"
    assert call["trigger_price"] == D("97") and call["close_position"] is True and call["qty"] is None
    assert call["working_type"] is WorkingType.CONTRACT_PRICE
    assert ids.parse_client_id(call["client_algo_id"]).origin is OrderOrigin.SYSTEM
    # 登记簿：系统保护，记下止损价与两个编号
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97")
    assert row.stop_client_id == call["client_algo_id"]
    assert row.stop_algo_id == rig.exchange.stops()[0].algo_id
    assert not rig.suppressed()
    (intent,) = await rig.intents_of("stop_place")
    assert intent.state is IntentState.COMPLETED and intent.position_id == rec.id


async def test_short_initial_stop_is_highest_high_plus_atr(rig):
    await rig.start()
    rig.candles.set(SYM, flat_history())
    rec = await rig.open_strategy(side=SHORT, price="100")
    status, row = await _protect(rig, rec)
    assert status == "protected"
    call = rig.writes("create_stop_order")[0]
    assert call["position_side"] is SHORT and call["side"].value == "BUY"
    assert call["trigger_price"] == D("103")  # 最高价 101 + ATR 2
    assert row.stop_price == D("103")


@pytest.mark.parametrize("center, half, expected", [
    (100.0, 0.1, "99"),   # 原始止损 99.7 离基准价只有 0.3%，近于 A=1%：先限下限，推到 99
    (81.0, 1.0, "95"),    # 原始止损 78 离基准价 22%，远于 B=5%：再限上限，拉回 95
    (100.0, 1.0, "97"),   # 3% 在 A–B 之间：不动
])
async def test_the_stop_is_clamped_between_a_and_b_percent_of_the_base_price(rig, center, half, expected):
    await rig.start()
    rig.candles.set(SYM, flat_history(center=center, half=half))
    rec = await rig.open_strategy(price="100")
    _, row = await _protect(rig, rec)
    assert row.stop_price == D(expected)


async def test_a_short_stop_is_clamped_the_same_way(rig):
    await rig.start()
    rig.candles.set(SYM, flat_history(center=120.0))  # 最高 121 + 2 = 123，离基准价 100 远于 B：夹到 105
    rec = await rig.open_strategy(side=SHORT, price="100")
    _, row = await _protect(rig, rec)
    assert row.stop_price == D("105")


async def test_long_stop_rounds_down_and_short_stop_rounds_up_to_the_tick(rig):
    """方向取整：多仓向下、空仓向上，都朝远离现价的方向（价格步长 0.01）。"""
    await rig.start()
    rig.candles.set(SYM, flat_history(center=100.423, half=1.1))  # 多：97.123 -> 97.12；空：103.723 -> 103.73
    long_rec = await rig.open_strategy(side=LONG)
    short_rec = await rig.open_strategy(side=SHORT)
    _, long_row = await _protect(rig, long_rec)
    _, short_row = await _protect(rig, short_rec)
    assert long_row.stop_price == D("97.12") and short_row.stop_price == D("103.73")


@pytest.mark.parametrize("bars", [0, 5, 13])
async def test_without_enough_bars_for_atr_the_stop_is_a_percent_away_with_a_note(rig, bars):
    """K 线不足以算 ATR（少于 6 根，或少于 ATR 周期 14 根）：按 A% 设置，并在 stop_note 注明。"""
    await rig.start()
    rig.candles.set(SYM, frame_of([100.0] * bars, start_ms=BAR0 - bars * TF_MS))
    rec = await rig.open_strategy(price="100")
    status, row = await _protect(rig, rec)
    assert status == "protected" and row.stop_price == D("99")  # 100 的 1%
    assert "A%" in row.stop_note and "ATR" in row.stop_note
    short = await rig.open_strategy(side=SHORT, price="100")
    _, short_row = await _protect(rig, short)
    assert short_row.stop_price == D("101")


async def test_a_full_history_leaves_no_note(rig):
    await rig.start()
    rig.candles.set(SYM, flat_history())
    rec = await rig.open_strategy()
    _, row = await _protect(rig, rec)
    assert row.stop_note == ""


# ---------------------------------------------------------------- 基准价：策略仓取成交均价，接管仓取所配触发价类型的当刻价格


async def test_a_strategy_position_uses_its_average_price_not_the_current_price(rig):
    await rig.start()
    rig.candles.set(SYM, flat_history(center=81.0))  # 原始止损 78：被夹到基准价的 95%
    rig.exchange.set_price(SYM, "90")
    rec = await rig.open_strategy(price="100")
    _, row = await _protect(rig, rec)
    assert row.stop_price == D("95")  # 基准价是成交均价 100，不是现价 90


async def test_a_takeover_position_uses_the_current_price_of_the_configured_trigger_type(rig, make_rig):
    await rig.start()
    rig.candles.set(SYM, flat_history(center=81.0))
    rig.exchange.set_price(SYM, "102", "104")  # 最新价 102，标记价 104
    rec = await rig.open_takeover(price="100")
    status, row = await _protect(rig, rec)
    assert status == "protected" and row.stop_price == D("96.9")  # 102 × 95%
    assert rig.writes("create_stop_order")[0]["working_type"] is WorkingType.CONTRACT_PRICE


async def test_a_takeover_position_with_mark_price_triggers_uses_the_mark_price(ledger, clock):
    from protection_kit import Rig

    rig = Rig(ledger, clock, stop_trigger_price_type="MARK_PRICE")
    await rig.start()
    rig.candles.set(SYM, flat_history(center=81.0))
    rig.exchange.set_price(SYM, "102", "104")
    rec = await rig.open_takeover(price="100")
    _, row = await _protect(rig, rec)
    assert row.stop_price == D("98.8")  # 标记价 104 × 95%
    assert rig.writes("create_stop_order")[0]["working_type"] is WorkingType.MARK_PRICE


async def test_a_strategy_position_with_mark_price_triggers_places_a_mark_price_stop(ledger, clock):
    from protection_kit import Rig

    rig = Rig(ledger, clock, stop_trigger_price_type="MARK_PRICE")
    await rig.start()
    rig.candles.set(SYM, flat_history())
    rec = await rig.open_strategy()
    await _protect(rig, rec)
    assert rig.writes("create_stop_order")[0]["working_type"] is WorkingType.MARK_PRICE


async def test_a_takeover_position_becomes_managed_once_it_is_protected(rig):
    await rig.start()
    rig.candles.set(SYM, flat_history())
    rec = await rig.open_takeover()
    assert rec.status is PositionStatus.TAKING_OVER
    status, row = await _protect(rig, rec, reason="takeover")
    assert status == "protected" and row.status is PositionStatus.MANAGED  # F9 第 5 步：接管中的仓补挂成功后转托管中


async def test_a_takeover_position_stays_taking_over_while_it_has_no_stop(rig):
    await rig.start()
    rig.candles.set(SYM, flat_history())
    rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    rec = await rig.open_takeover()
    status, row = await _protect(rig, rec, reason="takeover")
    assert status == "rebuilding" and row.status is PositionStatus.TAKING_OVER


# ---------------------------------------------------------------- 幂等与跳过


async def test_protecting_an_already_protected_position_places_nothing_more(rig):
    await rig.start()
    rec = await rig.open_strategy()
    assert (await rig.protection.ensure_protected(rec.id, reason="registered")) == "protected"
    assert (await rig.protection.ensure_protected(rec.id, reason="again")) == "protected"
    assert len(rig.writes("create_stop_order")) == 1 and len(rig.exchange.stops()) == 1


async def test_a_position_that_is_not_registered_is_skipped(rig):
    await rig.start()
    pending = await rig.registry.register_pending(symbol=SYM, side=LONG, qty=D(1), avg_price=D(100),
                                                  reason=PendingReason.OUT_OF_SCOPE)
    assert (await rig.protection.ensure_protected(pending.id, reason="x")) == "skipped"
    assert rig.writes("create_stop_order") == []


async def test_a_missing_or_closed_position_reports_closed(rig):
    await rig.start()
    assert (await rig.protection.ensure_protected(9999, reason="x")) == "closed"
    rec = await rig.open_strategy()
    await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=rig.clock.now_ms())
    assert (await rig.protection.ensure_protected(rec.id, reason="x")) == "closed"
    assert rig.writes("create_stop_order") == []


async def test_a_position_in_closing_state_still_gets_protected(rig):
    """部分平仓在途期间只允许保护类动作（F4 第 6 步）：平仓中的仓照样要有止损。"""
    await rig.start()
    rec = await rig.open_strategy()
    await rig.registry.set_closing(rec.id, True, cause="partial_close")
    status, row = await _protect(rig, rec)
    assert status == "protected" and row.status is PositionStatus.CLOSING


async def test_a_stop_is_placed_even_when_the_gate_is_closed_and_suppressions_exist(rig):
    """保护类动作不受开仓闸、抑制原因影响（它们只拦新增风险）。"""
    from dmr_kernel.core.types import Suppression

    await rig.start()
    await rig.modelstate.add_suppression(Suppression.RECONCILE_UNKNOWN, reason="测试")
    rec = await rig.open_strategy()
    status, _ = await _protect(rig, rec)
    assert status == "protected"


# ---------------------------------------------------------------- 崩溃点与配置


async def test_crash_point_open_filled_before_stop_fires_before_any_stop_is_sent(rig):
    await rig.start()
    rec = await rig.open_strategy()
    rig.crash.arm("open_filled_before_stop")
    with pytest.raises(CrashSignal):
        await rig.protection.ensure_protected(rec.id, reason="registered")
    assert rig.writes("create_stop_order") == [] and rig.exchange.stops() == []
    assert rig.protection.crashed is not None
    row = await rig.row(rec.id)
    assert row.stop_price is None and row.protection_state is not ProtectionState.SYSTEM


async def test_after_a_restart_the_unprotected_position_gets_its_stop(rig):
    """已开仓而止损没挂上就被杀：重启后的新实例补上（F9 第 5 步），而且只有一张。"""
    await rig.start()
    rec = await rig.open_strategy()
    rig.crash.arm("open_filled_before_stop")
    with pytest.raises(CrashSignal):
        await rig.protection.ensure_protected(rec.id, reason="registered")
    reborn = rig.build_protection()
    assert (await reborn.ensure_protected(rec.id, reason="recovery")) == "protected"
    assert len(rig.exchange.stops()) == 1


async def test_invalid_stop_band_configuration_sends_nothing_and_counts_as_a_failed_attempt(make_rig):
    rig = make_rig(init_stop_min_pct=None)
    await rig.start()
    rec = await rig.open_strategy()
    status, row = await _protect(rig, rec)
    assert status == "rebuilding" and rig.writes("create_stop_order") == []
    assert row.protection_state is ProtectionState.REBUILDING


async def test_the_position_is_marked_rebuilding_while_the_stop_is_being_placed(rig):
    """没有止损的这几秒不算「无保护」（不置抑制）：挂单期间保护状态是「重建中」。"""
    await rig.start()
    seen: list = []

    async def hook(name, kw):
        seen.append((name, rig.registry.get_active(SYM, LONG).protection_state, rig.suppressed()))

    rig.exchange.hook = hook
    rec = await rig.open_strategy()
    await rig.protection.ensure_protected(rec.id, reason="registered")
    assert seen == [("create_stop_order", ProtectionState.REBUILDING, False)]


async def test_concurrent_requests_for_one_position_place_only_one_stop(rig):
    import asyncio

    await rig.start()
    rec = await rig.open_strategy()
    results = await asyncio.gather(*(rig.protection.ensure_protected(rec.id, reason=f"r{i}") for i in range(4)))
    assert results.count("protected") == 4 and len(rig.writes("create_stop_order")) == 1
    assert len(rig.exchange.stops()) == 1


async def test_two_positions_are_protected_independently(rig):
    import asyncio

    await rig.start()
    rig.candles.set("ETHUSDT", flat_history())
    rig.exchange.set_price("ETHUSDT", "100")
    a = await rig.open_strategy(symbol=SYM)
    b = await rig.open_strategy(symbol="ETHUSDT")
    rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())  # 第一张被拒（谁先谁后不定）
    await asyncio.gather(rig.protection.ensure_protected(a.id, reason="registered"),
                         rig.protection.ensure_protected(b.id, reason="registered"))
    states = sorted([(await rig.row(a.id)).protection_state.value, (await rig.row(b.id)).protection_state.value])
    assert states == ["rebuilding", "system"]  # 一个在重试，另一个不受影响
