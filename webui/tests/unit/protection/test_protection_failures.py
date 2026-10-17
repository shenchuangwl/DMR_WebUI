"""保护单管理：挂止损失败的每个分支（F2 第 3、4 步，4.11 故障通过标准）。

默认配置：重试窗口 30 秒、重试 3 次 -> 重试间隔 = 窗口 / (次数 + 1) = 7.5 秒：第 1 次尝试在 0 秒，第 k 次重试在 k×7.5 秒
（7.5、15、22.5 秒）；第 4 次尝试（第 3 次重试）仍失败就下结论，结论之后的持续重试用同一个间隔。
重试由 ``protection.tick()`` 驱动（调度器每隔几秒调用），时间用 ManualClock 推进。重试节奏本身的专项用例见
``test_protection_followups.py``。
"""
from __future__ import annotations

from dmr_kernel.core.types import AlertLevel, ExitRuleset, IntentState, ProtectionState
from protection_kit import LONG, SYM, TF_MS, T0, D, flat_history

WINDOW_MS = 30_000
STEP_MS = 7_500  # 重试间隔 = 窗口 / (次数 + 1)：每次 tick 前进一个间隔，正好到下一次重试
ALL_REJECTED = 4  # 第 1 次尝试 + 3 次重试


def _fail_stops(rig, n=ALL_REJECTED, *, crossed=False):
    for _ in range(n):
        rig.exchange.push_write_result("create_stop_order", rig.exchange.crossed() if crossed else rig.exchange.reject())


async def _run_to_verdict(rig, rec):
    """第 1 次尝试失败，之后每个重试间隔（7.5 秒）tick 一次，直到第 4 次尝试（第 3 次重试，22.5 秒）失败得出结论。"""
    status = await rig.protection.ensure_protected(rec.id, reason="registered")
    for _ in range(3):
        await rig.tick(STEP_MS)
    return status


# ---------------------------------------------------------------- 其它原因挂不上：窗口内重试，仍失败再处置


async def test_other_failures_are_retried_across_the_window_not_all_at_once(rig):
    await rig.start()
    _fail_stops(rig)
    rec = await rig.open_strategy()
    assert (await rig.protection.ensure_protected(rec.id, reason="registered")) == "rebuilding"
    assert len(rig.writes("create_stop_order")) == 1
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.REBUILDING and not rig.suppressed()
    await rig.tick(STEP_MS - 1)
    assert len(rig.writes("create_stop_order")) == 1  # 还没到第一次重试（7.5 秒）
    await rig.tick(1)
    assert len(rig.writes("create_stop_order")) == 2
    await rig.tick(STEP_MS)
    assert len(rig.writes("create_stop_order")) == 3
    await rig.tick(STEP_MS - 1)
    assert len(rig.writes("create_stop_order")) == 3
    await rig.tick(1)
    assert len(rig.writes("create_stop_order")) == 4  # 第 3 次重试在 22.5 秒；这一次也失败了才下结论


async def test_a_strategy_position_is_market_closed_with_cooldown_after_the_retries_fail(rig):
    await rig.start()
    _fail_stops(rig)
    rec = await rig.open_strategy(qty="2")
    await _run_to_verdict(rig, rec)
    (close,) = rig.writes("create_market_order")
    assert close["side"].value == "SELL" and close["position_side"] is LONG and close["qty"] == D(2)
    (intent,) = await rig.intents_of("close")
    assert intent.params.get("retry_until_done") is True  # 保护性平仓：F12，一直重试到平掉
    assert intent.reason.lower().startswith("protective")  # 登记簿据此记出场原因「保护性平仓」
    assert intent.state is IntentState.COMPLETED
    assert rig.registry.cooldown_until(SYM) == T0 + 3 * STEP_MS + 4 * TF_MS  # 22.5 秒得出结论；cooldown_bars=4 根 15 分钟
    assert "protect_failed_closing" in rig.notifier.kinds()


async def test_a_takeover_position_is_never_auto_closed_it_goes_unprotected_with_a_critical_alert(rig):
    await rig.start()
    _fail_stops(rig)
    rec = await rig.open_takeover()
    await _run_to_verdict(rig, rec)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.NONE and "无保护" in row.stop_note
    assert rig.writes("create_market_order") == []
    assert rig.suppressed()  # F2 第 5 步：有无保护的托管仓就置抑制原因
    (alert,) = rig.notifier.of("protect_unprotected")
    assert alert["level"] is AlertLevel.CRITICAL and alert["dedupe_key"]
    assert rig.registry.cooldown_until(SYM) is None


def _mark_mixed(session, position_id):
    from dmr_kernel.ledger.models import Position

    row = session.get(Position, position_id)
    row.is_mixed = True
    row.exit_ruleset = ExitRuleset.STRATEGY_NO_REVERSE


async def test_a_mixed_position_is_treated_like_a_takeover_position_and_not_auto_closed(rig):
    """策略仓被外部加仓成了混合仓：里面有用户的仓位，失败后不自动市价平掉。"""
    await rig.start()
    _fail_stops(rig)
    rec = await rig.open_strategy()
    await rig.ledger.write(_mark_mixed, rec.id)
    await rig.registry.load()
    assert rig.registry.by_id(rec.id).is_mixed
    await _run_to_verdict(rig, rec)
    assert rig.writes("create_market_order") == []
    assert (await rig.row(rec.id)).protection_state is ProtectionState.NONE
    assert rig.notifier.of("protect_unprotected")


async def test_the_retry_that_succeeds_ends_the_cycle_and_clears_the_suppression(rig):
    """第 3 次重试（22.5 秒，最迟的一次）挂上了：止损存在，保护状态回到系统保护，没有平仓。"""
    await rig.start()
    _fail_stops(rig, 3)
    rec = await rig.open_strategy()
    await _run_to_verdict(rig, rec)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97")
    assert len(rig.exchange.stops()) == 1 and rig.writes("create_market_order") == []
    assert rig.clock.now_ms() - T0 <= WINDOW_MS  # 最迟窗口（30 秒）内止损存在
    assert not rig.suppressed()


async def test_a_takeover_position_keeps_retrying_after_the_verdict_and_recovers(rig):
    await rig.start()
    _fail_stops(rig)
    rec = await rig.open_takeover()
    await _run_to_verdict(rig, rec)
    assert len(rig.writes("create_stop_order")) == 4 and rig.suppressed()
    _fail_stops(rig, 2)
    await rig.tick(STEP_MS - 1)
    assert len(rig.writes("create_stop_order")) == 4  # 结论之后每个重试间隔（7.5 秒）再试一次
    await rig.tick(1)
    assert len(rig.writes("create_stop_order")) == 5
    await rig.tick(STEP_MS)
    assert len(rig.writes("create_stop_order")) == 6 and rig.suppressed()
    await rig.tick(STEP_MS)  # 故障排队用完了：这次挂上了
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and not rig.suppressed()
    assert row.stop_note == "" and "protect_restored" in rig.notifier.kinds()  # 「无保护」的说明随保护恢复清掉
    await rig.tick(STEP_MS * 3)
    assert len(rig.writes("create_stop_order")) == 7  # 挂上之后不再重试


async def test_with_zero_retries_a_strategy_position_is_closed_after_the_first_failure(make_rig):
    rig = make_rig(protect_retries=0)
    await rig.start()
    _fail_stops(rig, 1)
    rec = await rig.open_strategy()
    assert (await rig.protection.ensure_protected(rec.id, reason="registered")) == "closing"
    await rig.settle()
    assert len(rig.writes("create_stop_order")) == 1 and len(rig.writes("create_market_order")) == 1


async def test_with_zero_retries_a_takeover_position_is_unprotected_after_the_first_failure(make_rig):
    rig = make_rig(protect_retries=0)
    await rig.start()
    _fail_stops(rig, 1)
    rec = await rig.open_takeover()
    assert (await rig.protection.ensure_protected(rec.id, reason="takeover")) == "unprotected"
    assert rig.suppressed()
    assert (await rig.row(rec.id)).protection_state is ProtectionState.NONE


# ---------------------------------------------------------------- 市价平仓也失败


async def test_when_the_protective_close_also_fails_the_position_stays_unprotected_and_keeps_retrying(rig):
    await rig.start()
    _fail_stops(rig)
    rig.exchange.push_write_result("create_market_order", rig.exchange.reject())
    rec = await rig.open_strategy()
    await _run_to_verdict(rig, rec)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.NONE and rig.suppressed()
    (alert,) = rig.notifier.of("protective_close_failed")
    assert alert["level"] is AlertLevel.CRITICAL
    (intent,) = await rig.intents_of("close")
    assert intent.state is IntentState.REJECTED
    await rig.tick(STEP_MS)  # 持续重试：先再挂一次止损，这次挂上了
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and not rig.suppressed()


async def test_the_protective_close_is_retried_when_the_stop_still_cannot_be_placed(rig):
    await rig.start()
    _fail_stops(rig, 5)
    rig.exchange.push_write_result("create_market_order", rig.exchange.reject())
    rec = await rig.open_strategy()
    await _run_to_verdict(rig, rec)
    assert len(rig.writes("create_market_order")) == 1
    await rig.tick(STEP_MS)  # 止损第 5 次失败 -> 再平一次，这次平掉了
    assert len(rig.writes("create_stop_order")) == 5 and len(rig.writes("create_market_order")) == 2
    intents = await rig.intents_of("close")
    assert [i.state for i in intents] == [IntentState.REJECTED, IntentState.COMPLETED]


async def test_a_close_that_is_still_in_flight_is_not_sent_twice(make_rig):
    rig = make_rig(wait_s=0.1)
    await rig.start()
    _fail_stops(rig)
    rig.exchange.push_write_result("create_market_order", rig.exchange.unknown())  # 结果未知：回查中
    rec = await rig.open_strategy()
    await _run_to_verdict(rig, rec)
    for _ in range(3):  # wait_s 很短：机器忙时意图结果可能晚一轮 tick 才被处理，结论也就晚一轮（不靠真实耗时定输赢）
        if rig.writes("create_market_order"):
            break
        await rig.tick(STEP_MS)
    assert len(rig.writes("create_market_order")) == 1
    for _ in range(4):
        await rig.tick(STEP_MS)
    assert len(rig.writes("create_market_order")) == 1  # 在途的平仓单没有结论之前不重复发


# ---------------------------------------------------------------- −2021：止损位已被现价越过


async def test_immediate_trigger_closes_a_strategy_position_at_once_with_cooldown_and_no_retry(rig):
    await rig.start()
    _fail_stops(rig, 1, crossed=True)
    rec = await rig.open_strategy()
    assert (await rig.protection.ensure_protected(rec.id, reason="registered")) == "closing"
    await rig.settle()
    assert len(rig.writes("create_stop_order")) == 1  # 不再重试
    (close,) = rig.writes("create_market_order")
    assert close["position_side"] is LONG
    (intent,) = await rig.intents_of("close")
    assert intent.params.get("retry_until_done") is True and intent.reason.lower().startswith("protective")
    assert rig.registry.cooldown_until(SYM) == T0 + 4 * TF_MS
    assert (await rig.row(rec.id)).protection_state is ProtectionState.NONE


async def test_the_stop_failed_stop_intent_carries_the_immediate_trigger_reason(rig):
    await rig.start()
    _fail_stops(rig, 1, crossed=True)
    rec = await rig.open_strategy()
    await rig.protection.ensure_protected(rec.id, reason="registered")
    (intent,) = await rig.intents_of("stop_place")
    assert intent.state is IntentState.REJECTED and intent.state_reason == "immediate_trigger"


async def test_immediate_trigger_on_a_takeover_position_recomputes_from_the_current_price(rig):
    await rig.start()
    rig.candles.set(SYM, flat_history(center=81.0))  # 原始止损 78：由基准价（现价）的 5% 决定
    rig.exchange.set_price(SYM, "100")
    states: list = []

    async def hook(name, kw):
        states.append(rig.registry.get_active(SYM, LONG).protection_state)
        if len(states) == 1:
            rig.exchange.set_price(SYM, "90")  # 第一次被拒之后价格掉了：重算要以新的现价为基准

    rig.exchange.hook = hook
    _fail_stops(rig, 1, crossed=True)
    rec = await rig.open_takeover()
    assert (await rig.protection.ensure_protected(rec.id, reason="takeover")) == "protected"
    first, second = [c["trigger_price"] for c in rig.writes("create_stop_order")]
    assert first == D("95") and second == D("85.5")  # 100×95%，90×95%
    assert states == [ProtectionState.REBUILDING, ProtectionState.REBUILDING]  # 重算期间记「重建中」
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and "止损已按现价重定" in row.stop_note
    assert rig.writes("create_market_order") == []


async def test_immediate_trigger_on_a_takeover_position_gives_up_after_the_allowed_recomputes(rig):
    await rig.start()
    _fail_stops(rig, 10, crossed=True)
    rec = await rig.open_takeover()
    status = await rig.protection.ensure_protected(rec.id, reason="takeover")
    assert status == "unprotected"
    assert len(rig.writes("create_stop_order")) == 4  # 第 1 次 + 最多重算 3 次（protect_retries）
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.NONE and rig.suppressed()
    assert rig.writes("create_market_order") == []  # 接管仓不自动平
    assert rig.notifier.of("protect_unprotected")[0]["level"] is AlertLevel.CRITICAL


async def test_recomputes_are_bounded_by_protect_retries(make_rig):
    rig = make_rig(protect_retries=1)
    await rig.start()
    _fail_stops(rig, 10, crossed=True)
    rec = await rig.open_takeover()
    await rig.protection.ensure_protected(rec.id, reason="takeover")
    assert len(rig.writes("create_stop_order")) == 2


# ---------------------------------------------------------------- 结果未知：回查确认


async def test_an_unknown_stop_result_is_confirmed_by_the_coordinators_requery(make_rig):
    rig = make_rig(wait_s=0.1)
    """请求其实成功了、应答丢了：协调者按编号回查到条件单后意图完成，保护单管理据此登记，不重复挂。"""
    await rig.start()
    rig.exchange.push_write_result("create_stop_order", rig.exchange.lost_response())
    rec = await rig.open_strategy()
    assert (await rig.protection.ensure_protected(rec.id, reason="registered")) == "rebuilding"
    await rig.settle()  # 协调者把意图转入回查（RECONCILING）
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.REBUILDING and not rig.suppressed()
    await rig.tick(6_000)  # 协调者每 5 秒回查一次未知订单
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97")
    assert row.stop_algo_id == rig.exchange.stops()[0].algo_id
    assert len(rig.writes("create_stop_order")) == 1 and len(rig.exchange.stops()) == 1


async def test_an_unknown_stop_result_with_a_started_protection_is_finished_by_the_intent_event(make_rig):
    rig = make_rig(wait_s=0.1)
    await rig.start(protection=True)
    rig.exchange.push_write_result("create_stop_order", rig.exchange.lost_response())
    rec = await rig.open_strategy()
    await rig.settle()
    assert (await rig.row(rec.id)).protection_state is ProtectionState.REBUILDING
    rig.advance(6_000)
    await rig.coordinator.resolve_unknown_orders()
    await rig.settle()  # 不调 tick：意图结束的事件就够了
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM


# ---------------------------------------------------------------- 开着/不开着的事件订阅：登记即保护


async def test_registering_a_position_triggers_protection_through_the_event(rig):
    await rig.start(protection=True)
    rig.candles.set(SYM, flat_history())
    rec = await rig.open_strategy()
    await rig.settle()
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97")


# ---------------------------------------------------------------- 健壮性：提交失败、持续重试不来回翻、卡住的周期会被兜底


async def test_a_failure_to_submit_the_stop_intent_counts_as_a_failed_attempt_and_is_retried(rig, monkeypatch):
    await rig.start()
    rec = await rig.open_strategy()
    real = rig.coordinator.submit
    calls = []

    async def flaky(spec):
        calls.append(spec.action)
        if len(calls) == 1:
            raise RuntimeError("账本写失败")
        return await real(spec)

    monkeypatch.setattr(rig.coordinator, "submit", flaky)
    assert (await rig.protection.ensure_protected(rec.id, reason="registered")) == "rebuilding"  # 不抛、不卡住
    assert rig.exchange.writes == []
    await rig.tick(STEP_MS)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and len(rig.exchange.stops()) == 1


async def test_persistent_retries_keep_the_unprotected_state_without_flipping_it(rig):
    """结论之后的持续重试期间，保护状态一直是「无保护」：不在「重建中」与「无保护」之间来回翻（那会反复发事件、写账本）。"""
    await rig.start()
    _fail_stops(rig, 8)
    rec = await rig.open_takeover()
    await _run_to_verdict(rig, rec)
    changes = len([e for e in rig.events if type(e).__name__ == "ProtectionChanged"])
    for _ in range(3):
        await rig.tick(STEP_MS)
    assert len(rig.writes("create_stop_order")) == 7  # 又试了 3 次，都失败
    assert len([e for e in rig.events if type(e).__name__ == "ProtectionChanged"]) == changes
    assert (await rig.row(rec.id)).protection_state is ProtectionState.NONE and rig.suppressed()


async def test_a_cycle_that_lost_its_schedule_is_resumed_by_the_next_tick(rig):
    await rig.start()
    _fail_stops(rig, 1)
    rec = await rig.open_strategy()
    assert (await rig.protection.ensure_protected(rec.id, reason="registered")) == "rebuilding"
    rig.protection._cycles[rec.id].next_due_ms = None  # 假设上一步出了意外错误，没来得及安排下一次
    await rig.tick()
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM


async def test_missing_the_five_second_target_raises_one_warning_while_it_keeps_retrying(rig):
    await rig.start()
    _fail_stops(rig, 2)
    rec = await rig.open_strategy()
    await rig.protection.ensure_protected(rec.id, reason="registered")
    await rig.tick(4_000)
    assert rig.notifier.of("protect_slow") == []  # 目标 protect_target_s=5 秒
    await rig.tick(2_000)
    (alert,) = rig.notifier.of("protect_slow")
    assert alert["level"] is AlertLevel.WARNING
    await rig.tick(STEP_MS)
    assert len(rig.notifier.of("protect_slow")) == 1  # 只提醒一次


async def test_a_stop_placed_within_the_target_raises_no_warning(rig):
    await rig.start()
    rec = await rig.open_strategy()
    await rig.protection.ensure_protected(rec.id, reason="registered")
    await rig.tick(STEP_MS * 3)
    assert rig.notifier.of("protect_slow") == []


async def test_asking_again_while_a_cycle_is_waiting_for_its_retry_does_not_add_an_attempt(rig):
    """重试的节奏由保护周期自己掌握：期间再调 ensure_protected（事件、手动、tick 都可能）不另起一次尝试，也不占用重试次数。"""
    await rig.start()
    _fail_stops(rig, 4)
    rec = await rig.open_strategy()
    assert (await rig.protection.ensure_protected(rec.id, reason="registered")) == "rebuilding"
    rig.advance(3_000)
    assert (await rig.protection.ensure_protected(rec.id, reason="again")) == "rebuilding"
    assert (await rig.protection.ensure_protected(rec.id, reason="tick")) == "rebuilding"
    assert len(rig.writes("create_stop_order")) == 1
    await _run_to_verdict(rig, rec)  # 重试次数没被多余的调用占掉：仍是 4 次尝试之后才下结论
    assert len(rig.writes("create_stop_order")) == 4 and len(rig.writes("create_market_order")) == 1


async def test_ensure_on_a_takeover_position_in_its_persistent_phase_reports_unprotected(rig):
    await rig.start()
    _fail_stops(rig, 8)
    rec = await rig.open_takeover()
    await _run_to_verdict(rig, rec)
    assert (await rig.protection.ensure_protected(rec.id, reason="manual")) == "unprotected"
    assert len(rig.writes("create_stop_order")) == 4  # 没有因为这次询问多试一次


async def test_a_completed_close_whose_fill_never_shows_up_is_not_trusted_forever(rig):
    """保护性平仓单完成后等成交入账、仓位归零；close_one_timeout_s 之后仓位还在登记簿里，就当这次平仓没成，重新保护（先挂止损）。"""
    await rig.start()
    _fail_stops(rig, 4)
    rec = await rig.open_strategy()
    await _run_to_verdict(rig, rec)
    assert len(rig.writes("create_market_order")) == 1 and len(rig.writes("create_stop_order")) == 4
    await rig.tick(14_000)
    assert len(rig.writes("create_stop_order")) == 4  # 还在等成交入账（close_one_timeout_s=15 秒）
    await rig.tick(2_000)
    assert len(rig.writes("create_stop_order")) == 5  # 超时：再试着挂止损
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM
