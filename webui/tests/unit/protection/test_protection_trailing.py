"""保护单管理：移动止损（F2 第 6、7 步）。

K 线用 ``history_then``：40 根平的历史 + 持仓所在的那根（不计入窗口）+ 之后的若干根。之后的 K 线中心价每根 +0.5（多）或 −0.5（空），
每根的真实波幅和 ATR 都是 2：窗口 0（100.5…103）最低价 99.5、止损 97.5；窗口 1（103.5…106）止损 100.5。
空仓镜像：窗口 0（99.5…97）最高价 100.5、止损 102.5。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.events import IntentStateChanged
from dmr_kernel.core.types import (
    AlertLevel,
    CancelOutcome,
    CancelResult,
    IntentState,
    ProtectionState,
)
from protection_kit import BAR0, LONG, SHORT, SYM, TF_MS, D, history_then

RISING = [100.5, 101.0, 101.5, 102.0, 102.5, 103.0]
RISING2 = [103.5, 104.0, 104.5, 105.0, 105.5, 106.0]
FALLING = [99.5, 99.0, 98.5, 98.0, 97.5, 97.0]


def last_bar(n: int) -> int:
    """持仓之后第 n 根 K 线的开盘时间。"""
    return BAR0 + n * TF_MS


async def long_with_stop(rig, *, stop="97", after=RISING, price="105", takeover=False):
    rec = await (rig.open_takeover() if takeover else rig.open_strategy())
    if takeover:
        rec = await rig.registry.mark_managed(rec.id)
    rec, algo = await rig.with_system_stop(rec, stop)
    rig.candles.set(SYM, history_then(after))
    rig.exchange.set_price(SYM, price)
    return rec, algo


async def trail(rig, n_bars):
    await rig.protection.trailing(last_bar(n_bars))
    await rig.settle()


# ---------------------------------------------------------------- 只朝有利方向、周期计数


async def test_a_full_window_moves_the_stop_up_in_gap_mode(rig):
    await rig.start()
    rec, old = await long_with_stop(rig)
    await trail(rig, 6)
    assert rig.names() == ["cancel_algo_order", "create_stop_order"]  # gap：先撤旧、后挂新
    (new,) = rig.exchange.stops()
    assert new.trigger_price == D("97.5") and rig.exchange.find(SYM, algo_id=old.algo_id).raw_status == "CANCELED"
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97.5") and row.stop_cycle == 1
    assert (row.stop_client_id, row.stop_algo_id) == (new.client_algo_id, new.algo_id)
    (intent,) = await rig.intents_of("stop_replace")
    assert intent.state is IntentState.COMPLETED and intent.params["mode"] == "gap"
    assert intent.params["result"]["outcome"] == "replaced"
    assert intent.params["old"]["algo_id"] == old.algo_id


async def test_fewer_than_six_new_bars_do_nothing_and_keep_the_cycle(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig, after=RISING[:5])
    await trail(rig, 5)
    assert rig.exchange.writes == []
    assert (await rig.row(rec.id)).stop_cycle == 0


async def test_bars_after_the_given_bar_are_not_used(rig):
    """trailing(bar_open_ms) 只看开盘时间不晚于它的 K 线：第 6 根还没到就不算满。"""
    await rig.start()
    rec, _ = await long_with_stop(rig)
    await trail(rig, 5)
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).stop_cycle == 0


async def test_a_candidate_that_is_not_better_is_not_adopted_but_the_cycle_advances(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig, stop="98")  # 候选 97.5 比现有的 98 低：对多仓不利
    await trail(rig, 6)
    assert rig.exchange.writes == []
    row = await rig.row(rec.id)
    assert row.stop_price == D("98") and row.stop_cycle == 1  # 周期计数无论是否采纳都前进


async def test_an_equal_candidate_is_not_adopted(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig, stop="97.5")
    await trail(rig, 6)
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).stop_cycle == 1


@pytest.mark.parametrize("price, adopted", [("97.4", False), ("97.5", False), ("97.51", True)])
async def test_a_candidate_crossed_by_the_current_price_is_not_adopted(rig, price, adopted):
    """候选止损价 97.5：现价低于或等于它就已被越过（挂了会立即触发），旧止损保留；高于才采纳。周期计数照样前进。"""
    await rig.start()
    rec, _ = await long_with_stop(rig, price=price)
    await trail(rig, 6)
    row = await rig.row(rec.id)
    assert row.stop_cycle == 1
    assert (row.stop_price == D("97.5")) is adopted
    assert (len(rig.writes("create_stop_order")) == 1) is adopted


async def test_the_crossing_check_uses_the_price_of_the_configured_trigger_type(ledger, clock):
    from protection_kit import Rig

    rig = Rig(ledger, clock, stop_trigger_price_type="MARK_PRICE")
    await rig.start()
    rec, _ = await long_with_stop(rig, price="105")
    rig.exchange.set_price(SYM, "105", "97.4")  # 最新价 105，标记价 97.4：新止损按标记价触发，已被越过
    await trail(rig, 6)
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).stop_cycle == 1


async def test_a_short_position_mirrors_the_rules(rig):
    await rig.start()
    rec = await rig.open_strategy(side=SHORT)
    rec, _ = await rig.with_system_stop(rec, "103")
    rig.candles.set(SYM, history_then(FALLING))
    rig.exchange.set_price(SYM, "95")
    await trail(rig, 6)
    (create,) = rig.writes("create_stop_order")
    assert create["trigger_price"] == D("102.5") and create["side"].value == "BUY" and create["position_side"] is SHORT
    row = await rig.row(rec.id)
    assert row.stop_price == D("102.5") and row.stop_cycle == 1


async def test_a_short_candidate_crossed_or_worse_is_not_adopted(rig):
    await rig.start()
    rec = await rig.open_strategy(side=SHORT)
    rec, _ = await rig.with_system_stop(rec, "103")
    rig.candles.set(SYM, history_then(FALLING))
    rig.exchange.set_price(SYM, "102.5")  # 现价已到候选止损价：空仓的止损要在现价之上，被越过
    await trail(rig, 6)
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).stop_cycle == 1
    # 更不利（比现有的 102 还高）：不采纳
    other = await rig.open_strategy(symbol="ETHUSDT", side=SHORT)
    other, _ = await rig.with_system_stop(other, "102")
    rig.candles.set("ETHUSDT", history_then(FALLING))
    rig.exchange.set_price("ETHUSDT", "95")
    await trail(rig, 6)
    assert rig.exchange.writes == [] and (await rig.row(other.id)).stop_cycle == 1


async def test_several_pending_windows_are_evaluated_together_and_the_best_is_set_once(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig, after=RISING + RISING2, price="110")
    await trail(rig, 12)
    (create,) = rig.writes("create_stop_order")
    assert create["trigger_price"] == D("100.5")
    row = await rig.row(rec.id)
    assert row.stop_price == D("100.5") and row.stop_cycle == 2


async def test_each_cycle_is_counted_once(rig):
    """同一批 K 线重复触发移动止损，不会重复算同一个周期，也不会重复换单。"""
    await rig.start()
    rec, _ = await long_with_stop(rig)
    await trail(rig, 6)
    await trail(rig, 6)
    assert len(rig.writes("create_stop_order")) == 1 and (await rig.row(rec.id)).stop_cycle == 1


async def test_cycles_continue_from_the_stored_count(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig, after=RISING + RISING2, price="110")
    await rig.registry.set_protection(rec.id, state=ProtectionState.SYSTEM, stop_cycle=1)  # 窗口 0 已经算过
    await trail(rig, 12)
    (create,) = rig.writes("create_stop_order")
    assert create["trigger_price"] == D("100.5") and (await rig.row(rec.id)).stop_cycle == 2


async def test_only_system_protected_managed_positions_are_trailed(rig):
    await rig.start()
    # 外部保护的接管仓：系统不动用户的止损，也不前进周期
    ext = await rig.open_takeover()
    ext = await rig.registry.mark_managed(ext.id)
    await rig.registry.set_protection(ext.id, state=ProtectionState.EXTERNAL, clear_stop=True)
    rig.candles.set(SYM, history_then(RISING))
    rig.exchange.set_price(SYM, "105")
    await trail(rig, 6)
    assert rig.exchange.writes == [] and (await rig.row(ext.id)).stop_cycle == 0
    # 接管中（还没转托管中）、没有止损的：也不动
    rig2 = await rig.open_strategy(symbol="ETHUSDT")
    rig.candles.set("ETHUSDT", history_then(RISING))
    rig.exchange.set_price("ETHUSDT", "105")
    await trail(rig, 6)
    assert rig.exchange.writes == [] and (await rig.row(rig2.id)).stop_cycle == 0


# ---------------------------------------------------------------- 平仓中顺延、订单频率顺延


async def test_a_closing_position_is_deferred_and_caught_up_after_it_resumes(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig)
    await rig.registry.set_closing(rec.id, True, cause="partial_close")
    await trail(rig, 6)
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).stop_cycle == 0  # 顺延：周期计数不前进
    await rig.registry.set_closing(rec.id, False, cause="partial_close_resume")
    await trail(rig, 6)
    assert len(rig.writes("create_stop_order")) == 1 and (await rig.row(rec.id)).stop_cycle == 1


async def test_the_deferred_update_is_made_up_by_the_resume_event_without_a_new_bar(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig)
    await rig.protection.start()
    await rig.registry.set_closing(rec.id, True, cause="partial_close")
    await rig.settle()
    await trail(rig, 6)
    assert rig.exchange.writes == []
    await rig.registry.set_closing(rec.id, False, cause="partial_close_resume")
    await rig.settle()  # 不再调 trailing：恢复事件触发补做
    (create,) = rig.writes("create_stop_order")
    assert create["trigger_price"] == D("97.5") and (await rig.row(rec.id)).stop_cycle == 1


async def test_an_order_rate_limit_defers_the_update_without_advancing_the_cycle(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig)
    for _ in range(25):  # max_orders_per_symbol_10m 默认 25
        rig.limits.note_order(SYM, rig.clock.now_ms())
    assert not rig.limits.order_rate_ok(SYM, rig.clock.now_ms())
    await trail(rig, 6)
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).stop_cycle == 0
    rig.advance(600_001)  # 10 分钟的窗口过去了
    await trail(rig, 6)
    assert len(rig.writes("create_stop_order")) == 1 and (await rig.row(rec.id)).stop_cycle == 1


async def test_the_rate_limit_does_not_defer_a_window_that_adopts_nothing(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig, stop="98")
    for _ in range(25):
        rig.limits.note_order(SYM, rig.clock.now_ms())
    await trail(rig, 6)
    assert (await rig.row(rec.id)).stop_cycle == 1  # 不换单就不占订单频率，周期照常前进


async def test_an_unknown_current_price_makes_no_decision_and_keeps_the_cycle(rig):
    from dmr_kernel.core.types import Unknown

    await rig.start()
    rec, _ = await long_with_stop(rig)
    rig.exchange.set_read("fetch_tickers", Unknown("读不到"))
    await trail(rig, 6)
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).stop_cycle == 0


# ---------------------------------------------------------------- 两种更新方式


async def test_make_before_break_places_the_new_stop_before_cancelling_the_old(make_rig):
    rig = make_rig(ex_stop_update_mode="make_before_break")
    await rig.start()
    rec, old = await long_with_stop(rig)
    await trail(rig, 6)
    assert rig.names() == ["create_stop_order", "cancel_algo_order"]  # 先挂后撤
    (new,) = rig.exchange.stops()
    assert new.trigger_price == D("97.5") and new.algo_id != old.algo_id
    row = await rig.row(rec.id)
    assert row.stop_price == D("97.5") and row.stale_stops == []
    (intent,) = await rig.intents_of("stop_replace")
    assert intent.params["mode"] == "make_before_break" and intent.params["result"]["cleanup"] == []


async def test_make_before_break_keeps_both_and_lists_the_old_one_when_the_cancel_is_unknown(make_rig):
    rig = make_rig(ex_stop_update_mode="make_before_break")
    await rig.start()
    rec, old = await long_with_stop(rig)
    rig.exchange.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    await trail(rig, 6)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97.5")
    assert row.stale_stops == [{"client_algo_id": old.client_algo_id, "algo_id": old.algo_id}]  # 待清理的旧单
    assert len(rig.exchange.stops()) == 2


# ---------------------------------------------------------------- 协调者的各种结果：以 params["result"] 为准


async def test_restored_old_records_the_restored_stop_at_the_old_price(rig):
    await rig.start()
    rec, old = await long_with_stop(rig)
    rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())  # 新单被拒，旧价重挂成功
    await trail(rig, 6)
    (restored,) = rig.exchange.stops()
    assert restored.trigger_price == D("97") and restored.algo_id != old.algo_id
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97")
    assert (row.stop_client_id, row.stop_algo_id) == (restored.client_algo_id, restored.algo_id)
    assert row.stop_cycle == 1 and rig.writes("create_market_order") == []
    (intent,) = await rig.intents_of("stop_replace")
    assert intent.state is IntentState.REJECTED and intent.params["result"]["outcome"] == "restored_old"


@pytest.mark.parametrize("takeover", [False, True])
async def test_stop_crossed_closes_the_position_for_strategy_and_takeover_alike(rig, takeover):
    await rig.start()
    rec, _ = await long_with_stop(rig, takeover=takeover)
    for _ in range(2):  # 新单与旧价重挂都被 −2021 拒绝
        rig.exchange.push_write_result("create_stop_order", rig.exchange.crossed())
    await trail(rig, 6)
    (close,) = rig.writes("create_market_order")
    assert close["side"].value == "SELL" and close["position_side"] is LONG and close["qty"] == D(1)
    (intent,) = await rig.intents_of("close")
    assert intent.params.get("retry_until_done") is True and intent.reason.lower().startswith("protective")
    assert rig.registry.cooldown_until(SYM) is None  # 移动止损被越过的平仓不冷却（规格只写了市价平仓）
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.NONE and rig.suppressed()
    assert "stop_crossed_closing" in rig.notifier.kinds()


async def test_unprotected_result_follows_the_retry_branch_of_f2_step_4(rig):
    """新旧两张都挂不上（其它原因）：该方向现在没有止损，按 F2 第 4 步重试，按登记的止损价补挂。"""
    await rig.start()
    rec, _ = await long_with_stop(rig)
    for _ in range(2):
        rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    await trail(rig, 6)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.REBUILDING and rig.exchange.stops() == [] and not rig.suppressed()
    await rig.tick(10_000)  # 第 1 次重试
    (stop,) = rig.exchange.stops()
    assert stop.trigger_price == D("97")  # 登记的止损价（新价没能采纳）
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97")


async def _slow(rig, ms=31_000):
    """第一次挂单（新止损）请求耗时 ms 毫秒：撤旧到新单存在的空窗超过 stop_gap_max_s。"""
    calls = []

    async def hook(name, kw):
        calls.append(name)
        if name == "create_stop_order" and calls.count("create_stop_order") == 1:
            rig.advance(ms)

    rig.exchange.hook = hook


async def test_gap_exceeded_with_the_new_stop_in_place_adopts_it_and_warns(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig)
    await _slow(rig)
    await trail(rig, 6)
    (new,) = rig.exchange.stops()
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97.5")
    assert row.stop_algo_id == new.algo_id and rig.writes("create_market_order") == []
    (alert,) = rig.notifier.of("stop_gap_exceeded")
    assert alert["level"] is AlertLevel.WARNING
    (intent,) = await rig.intents_of("stop_replace")
    assert intent.params["result"]["outcome"] == "gap_exceeded" and intent.params["result"]["stop_in_place"] is True


async def test_gap_exceeded_without_any_stop_closes_a_strategy_position_with_cooldown(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig)
    await _slow(rig)
    rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())  # 新单被拒，且已超过上限：不再重试
    await trail(rig, 6)
    assert rig.exchange.stops() == [] and len(rig.writes("create_stop_order")) == 1
    (close,) = rig.writes("create_market_order")
    assert close["position_side"] is LONG
    assert rig.registry.cooldown_until(SYM) == rig.clock.now_ms() + 4 * TF_MS  # 按「仍失败」分支：冷却
    (intent,) = await rig.intents_of("stop_replace")
    assert intent.params["result"]["outcome"] == "gap_exceeded" and intent.params["result"]["stop_in_place"] is False


async def test_gap_exceeded_without_any_stop_leaves_a_takeover_position_unprotected(rig):
    await rig.start()
    rec, _ = await long_with_stop(rig, takeover=True)
    await _slow(rig)
    rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    await trail(rig, 6)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.NONE and rig.suppressed()
    assert rig.writes("create_market_order") == []
    assert rig.notifier.of("protect_unprotected")[0]["level"] is AlertLevel.CRITICAL


async def test_kept_old_changes_nothing_but_the_cycle(rig):
    await rig.start()
    rec, old = await long_with_stop(rig)
    rig.exchange.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    await trail(rig, 6)  # 撤旧结果未知，回查旧单仍在：放弃本次更新
    assert rig.writes("create_stop_order") == []
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97")
    assert row.stop_algo_id == old.algo_id and row.stop_cycle == 1
    (intent,) = await rig.intents_of("stop_replace")
    assert intent.params["result"]["outcome"] == "kept_old"


# ---------------------------------------------------------------- 结果幂等、空窗计时


async def test_a_result_is_applied_once_even_if_the_intent_is_reopened_and_completed_again(rig):
    """止损类意图可能经历 REJECTED/COMPLETED → ACTIVE（入账侧重开）→ COMPLETED：结论只认第一次的 params["result"]，不重复处理。"""
    await rig.start()
    rec, _ = await long_with_stop(rig)
    await rig.protection.start()
    rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    await trail(rig, 6)
    (intent,) = await rig.intents_of("stop_replace")
    assert intent.state is IntentState.REJECTED
    before, writes, alerts = await rig.row(rec.id), len(rig.exchange.writes), len(rig.notifier.alerts)
    await rig.intents.reopen_for_late_fill(intent.id, reason="晚到成交")
    await rig.intents.transition(intent.id, IntentState.COMPLETED, reason="再次完成")
    for state in ("COMPLETED", "ACTIVE", "COMPLETED"):  # 重复的终态事件
        rig.bus.publish(IntentStateChanged(intent_id=intent.id, state=state, action="stop_replace", symbol=SYM,
                                           side=LONG, at_ms=rig.clock.now_ms()))
    await rig.settle()
    after = await rig.row(rec.id)
    assert (after.stop_price, after.stop_client_id, after.stop_algo_id, after.version) == (
        before.stop_price, before.stop_client_id, before.stop_algo_id, before.version)
    assert len(rig.exchange.writes) == writes and len(rig.notifier.alerts) == alerts
    (again,) = await rig.intents_of("stop_replace")
    assert again.state is IntentState.COMPLETED and again.params["result"]["outcome"] == "restored_old"


async def test_a_new_stop_whose_result_is_unknown_is_confirmed_by_the_requery(make_rig):
    rig = make_rig(wait_s=0.1)
    await rig.start()
    rec, old = await long_with_stop(rig)
    rig.exchange.push_write_result("create_stop_order", rig.exchange.lost_response())
    await trail(rig, 6)
    assert (await rig.row(rec.id)).stop_algo_id == old.algo_id  # 结果还没出来：登记仍是旧的
    await rig.tick(6_000)  # 协调者回查到新单存在 -> replaced
    (new,) = rig.exchange.stops()
    row = await rig.row(rec.id)
    assert row.stop_price == D("97.5") and (row.stop_client_id, row.stop_algo_id) == (new.client_algo_id, new.algo_id)
    assert row.protection_state is ProtectionState.SYSTEM and rig.writes("create_market_order") == []


@pytest.mark.parametrize("takeover", [False, True])
async def test_gap_timer_applies_the_still_failing_branch_when_the_new_stop_cannot_be_confirmed(make_rig, takeover):
    rig = make_rig(wait_s=0.1)
    """新止损的结果一直未知（回查也没有确认）：从提交更新起超过 stop_gap_max_s，按「仍失败」分支处置。"""
    await rig.start()
    rec, _ = await long_with_stop(rig, takeover=takeover)
    rig.exchange.push_write_result("create_stop_order", rig.exchange.unknown())  # 没建成，应答也没有
    await trail(rig, 6)
    await rig.tick(29_000)
    assert rig.writes("create_market_order") == [] and (await rig.row(rec.id)).protection_state is not ProtectionState.NONE
    await rig.tick(2_000)  # 31 秒
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.NONE and rig.suppressed()
    if takeover:
        assert rig.writes("create_market_order") == []
        assert rig.notifier.of("protect_unprotected")[0]["level"] is AlertLevel.CRITICAL
    else:
        assert len(rig.writes("create_market_order")) == 1
        assert rig.registry.cooldown_until(SYM) is not None
    assert rig.notifier.of("stop_gap_exceeded")


async def test_a_confirmed_new_stop_inside_the_gap_limit_needs_no_special_handling(make_rig):
    rig = make_rig(wait_s=0.1)
    await rig.start()
    rec, _ = await long_with_stop(rig)
    rig.exchange.push_write_result("create_stop_order", rig.exchange.lost_response())
    await trail(rig, 6)
    await rig.tick(20_000)
    await rig.tick(20_000)  # 即使之后又过了很久，已经确认的更新不会被当成空窗超限
    assert rig.writes("create_market_order") == [] and not rig.notifier.of("stop_gap_exceeded")
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM


async def test_a_long_backlog_of_windows_is_caught_up_in_bounded_steps(rig):
    """停机很久之后一次积了很多个 6 根周期：每次最多补算 MAX_CATCH_UP 个，剩下的下一次接着算。"""
    from dmr_kernel.core.protection import MAX_CATCH_UP

    await rig.start()
    after = [100.0 + 0.5 * k for k in range(1, 181)]  # 30 个窗口
    rec, _ = await long_with_stop(rig, after=after, price="300")
    await trail(rig, 180)
    row = await rig.row(rec.id)
    assert MAX_CATCH_UP == 24 and row.stop_cycle == 24
    assert row.stop_price == D("166.5")  # 窗口 23：第一根中心价 100+0.5×139=169.5，−1−2
    await trail(rig, 180)
    row = await rig.row(rec.id)
    assert row.stop_cycle == 30 and row.stop_price == D("184.5")  # 窗口 29：中心价 187.5
