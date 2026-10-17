"""保护单管理：独立复核补的用例（F8「未知只重查、不补挂」与 F2 第 7 步的空窗只属于 gap 模式）。

- 先建后撤（make_before_break）在新止损确认存在之前从不撤旧止损，没有空窗：新止损的结果一直未知时，
  不能因为超过 ``stop_gap_max_s`` 就按「仍失败」把策略仓市价平掉、把接管仓转成无保护。
- 在途的止损类意图结果未知时（STOP_REPLACE 回查中、STOP_PLACE 回查中、交给系统管理止损回查中），
  旧单的「已撤」推送、条件单快照、外部保护复核、``rebuild()`` 都不能再补挂一张止损：结论交给那个意图本身
  （以及 gap 模式下的 ``stop_gap_max_s`` 计时）。
- 不留下没人认领的系统止损；取消托管（外部·已忽略）或算止损价期间已平的仓不再挂止损。
- 算止损价时的意外错误照样计入重试、窗口用完照样下结论；保护性平仓超过单仓平仓时限仍没有结论发最高级告警；
  迟到的更新失败结论不推翻之后已经挂上的止损。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.events import AlgoOrderObserved, OrdersSnapshotTaken
from dmr_kernel.core.types import IntentState, ProtectionState, Unknown
from protection_kit import BAR0, LONG, SYM, TF_MS, D, history_then

RISING = [100.5, 101.0, 101.5, 102.0, 102.5, 103.0]


async def _long_with_stop(rig, *, takeover=False, stop="97"):
    rec = await (rig.open_takeover() if takeover else rig.open_strategy())
    if takeover:
        rec = await rig.registry.mark_managed(rec.id)
    rec, algo = await rig.with_system_stop(rec, stop)
    rig.candles.set(SYM, history_then(RISING))
    rig.exchange.set_price(SYM, "105")
    await rig.settle()  # 之前的登记事件先送完（保护单管理还没订阅）
    return rec, algo


async def _trail(rig):
    await rig.protection.trailing(BAR0 + 6 * TF_MS)
    await rig.settle()


def _unresolvable_new_stop(rig):
    """新止损没建成、应答也没有，而且回查一直读不到：意图停在回查中。"""
    rig.exchange.push_write_result("create_stop_order", rig.exchange.unknown())
    rig.exchange.set_read("fetch_algo_order", Unknown("交易所读不到"))


# ---------------------------------------------------------------- 先建后撤：没有空窗


@pytest.mark.parametrize("takeover", [False, True])
async def test_make_before_break_has_no_gap_so_an_unresolved_update_is_never_still_failing(make_rig, takeover):
    rig = make_rig(wait_s=0.1, ex_stop_update_mode="make_before_break")
    await rig.start()
    rec, old = await _long_with_stop(rig, takeover=takeover)
    _unresolvable_new_stop(rig)
    await _trail(rig)
    (intent,) = await rig.intents_of("stop_replace")
    assert intent.state is IntentState.RECONCILING
    for _ in range(5):
        await rig.tick(10_000)  # 50 秒，超过 stop_gap_max_s（30 秒）
    assert rig.exchange.find(SYM, algo_id=old.algo_id).is_open  # 旧止损一直在保护这个仓
    assert rig.writes("cancel_algo_order") == []
    assert rig.writes("create_market_order") == []  # 不能把有保护的策略仓平掉
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_algo_id == old.algo_id
    assert not rig.suppressed()
    assert not rig.notifier.of("stop_gap_exceeded") and not rig.notifier.of("protect_unprotected")


async def test_make_before_break_update_resolved_later_is_still_applied(make_rig):
    """回查恢复后新止损确认存在：照常登记新止损（先建后撤的旧单列为待清理）。"""
    rig = make_rig(wait_s=0.1, ex_stop_update_mode="make_before_break")
    await rig.start()
    rec, old = await _long_with_stop(rig)
    rig.exchange.push_write_result("create_stop_order", rig.exchange.lost_response())  # 其实建成了
    rig.exchange.set_read("fetch_algo_order", Unknown("交易所读不到"))
    await _trail(rig)
    for _ in range(4):
        await rig.tick(10_000)
    assert rig.writes("create_market_order") == [] and not rig.notifier.of("stop_gap_exceeded")
    rig.exchange.set_read("fetch_algo_order", rig.exchange._algo_order)  # 交易所又读得到了
    await rig.tick(5_000)
    await rig.tick(1_000)
    new = [a for a in rig.exchange.stops() if a.algo_id != old.algo_id]
    assert len(new) == 1
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_algo_id == new[0].algo_id
    assert row.stop_price == D("97.5") and rig.writes("create_market_order") == []


# ---------------------------------------------------------------- 在途意图结果未知：不补挂（F8）


async def test_the_cancel_event_of_the_old_stop_during_an_unresolved_replace_does_not_place_another_stop(make_rig):
    rig = make_rig(wait_s=0.1)  # gap 模式（默认）
    await rig.start()
    rec, old = await _long_with_stop(rig)
    await rig.protection.start()
    _unresolvable_new_stop(rig)
    await _trail(rig)
    assert len(rig.writes("create_stop_order")) == 1
    gone = rig.exchange.find(SYM, algo_id=old.algo_id)
    assert not gone.is_open  # 协调者已撤旧单，新单结果未知
    rig.bus.publish(AlgoOrderObserved(algo=gone, source="stream", at_ms=rig.clock.now_ms()))
    await rig.settle()
    await rig.tick(5_000)
    assert len(rig.writes("create_stop_order")) == 1  # 没有再补挂一张
    assert not rig.notifier.of("stop_missing_rebuilt")
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM


async def test_a_snapshot_during_an_unresolved_replace_does_not_place_another_stop(make_rig):
    rig = make_rig(wait_s=0.1)
    await rig.start()
    rec, _ = await _long_with_stop(rig)
    await rig.protection.start()
    _unresolvable_new_stop(rig)
    await _trail(rig)
    rig.clock.advance(1_000)
    rig.bus.publish(OrdersSnapshotTaken(open_orders=(), algo_orders=tuple(rig.exchange.open_algos()),
                                        at_ms=rig.clock.now_ms()))
    await rig.settle()
    await rig.tick(5_000)
    assert len(rig.writes("create_stop_order")) == 1
    assert not rig.notifier.of("stop_missing_rebuilt")


async def test_the_gap_timer_still_decides_after_the_limit_when_the_replace_stays_unresolved(make_rig):
    """不补挂不等于不管：gap 模式下超过 stop_gap_max_s 仍不能确认新止损，照样按「仍失败」处置（策略仓平掉）。"""
    rig = make_rig(wait_s=0.1)
    await rig.start()
    rec, old = await _long_with_stop(rig)
    await rig.protection.start()
    _unresolvable_new_stop(rig)
    await _trail(rig)
    rig.bus.publish(AlgoOrderObserved(algo=rig.exchange.find(SYM, algo_id=old.algo_id), source="stream",
                                      at_ms=rig.clock.now_ms()))
    await rig.settle()
    for _ in range(3):
        await rig.tick(10_000)
    assert len(rig.writes("create_stop_order")) == 1 and rig.writes("create_market_order") == []
    await rig.tick(2_000)  # 32 秒
    assert len(rig.writes("create_market_order")) == 1
    assert rig.notifier.of("stop_gap_exceeded")


async def test_rebuild_while_a_stop_place_is_unresolved_does_not_place_a_second_stop(make_rig):
    rig = make_rig(wait_s=0.1)
    await rig.start()
    rec = await rig.open_strategy()
    rig.candles.set(SYM, history_then([]))
    _unresolvable_new_stop(rig)
    assert await rig.protection.ensure_protected(rec.id, reason="registered") == "rebuilding"
    await rig.settle()
    (intent,) = await rig.intents_of("stop_place")
    assert intent.state is IntentState.RECONCILING
    status = await rig.protection.rebuild(rec.id, reason="recovery")
    await rig.settle()
    assert status == "rebuilding"
    assert len(rig.writes("create_stop_order")) == 1


async def test_an_unresolved_hand_over_is_not_mistaken_for_lost_external_protection(make_rig):
    """gap 模式交给系统管理止损：用户的止损已撤、系统新止损结果未知。快照里没有用户的止损，也不能当成外部保护失效去另挂。"""
    rig = make_rig(wait_s=0.1)
    await rig.start()
    rec = await rig.open_takeover()
    user = rig.exchange.add_stop(SYM, LONG, "95", cid=None)
    rig.candles.set(SYM, history_then([]))
    await rig.settle()
    assert await rig.protection.ensure_protected(rec.id, reason="takeover") == "external"
    await rig.protection.start()
    _unresolvable_new_stop(rig)
    await rig.protection.hand_stop_to_system(rec.id)
    await rig.settle()
    assert not rig.exchange.find(SYM, algo_id=user.algo_id).is_open  # 用户的止损已撤
    rig.clock.advance(1_000)
    rig.bus.publish(OrdersSnapshotTaken(open_orders=(), algo_orders=tuple(rig.exchange.open_algos()),
                                        at_ms=rig.clock.now_ms()))
    await rig.settle()
    await rig.tick(5_000)
    assert len(rig.writes("create_stop_order")) == 1
    assert not rig.notifier.of("external_protection_lost")
    assert not rig.notifier.of("protect_unprotected") and not rig.notifier.of("protective_close_failed")
    assert (await rig.row(rec.id)).protection_state is ProtectionState.EXTERNAL


# ---------------------------------------------------------------- gap 模式新旧都没挂上：重试不能越过 stop_gap_max_s


@pytest.mark.parametrize("takeover", [False, True])
async def test_after_an_unprotected_update_the_retries_stop_at_the_gap_limit(make_rig, takeover):
    """新单与按旧价重挂都被拒（其它原因）：照 F2 第 4 步重试，但从撤旧单起超过 stop_gap_max_s 仍没挂上，
    直接按「仍失败」处置，不再按重试窗口再拖下去（F2 第 7 步「超过上限仍无保护……不再重试一轮」）。"""
    rig = make_rig(stop_gap_max_s=10, protect_retry_window_s=60, protect_retries=3)  # 重试本该在 15/30/45 秒
    await rig.start()
    rec, _ = await _long_with_stop(rig, takeover=takeover)
    for _ in range(3):  # 新单、旧价重挂、到上限时的那次重试
        rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    await _trail(rig)
    (intent,) = await rig.intents_of("stop_replace")
    assert intent.params["result"]["outcome"] == "unprotected"
    assert (await rig.row(rec.id)).protection_state is ProtectionState.REBUILDING
    await rig.tick(9_000)
    assert len(rig.writes("create_stop_order")) == 2  # 上限之前不下结论
    await rig.tick(1_000)  # 撤旧单起 10 秒：最后一次尝试，仍被拒 -> 「仍失败」
    assert len(rig.writes("create_stop_order")) == 3
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.NONE and rig.suppressed()
    if takeover:
        assert rig.writes("create_market_order") == []
        assert rig.notifier.of("protect_unprotected")
    else:
        assert len(rig.writes("create_market_order")) == 1


async def test_an_unprotected_update_inside_the_gap_limit_is_retried_and_recovers(make_rig):
    rig = make_rig(stop_gap_max_s=10, protect_retry_window_s=60, protect_retries=3)
    await rig.start()
    rec, _ = await _long_with_stop(rig)
    for _ in range(2):
        rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    await _trail(rig)
    await rig.tick(10_000)  # 上限时的那次尝试挂上了
    (stop,) = rig.exchange.stops()
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_algo_id == stop.algo_id
    assert stop.trigger_price == D("97") and rig.writes("create_market_order") == [] and not rig.suppressed()


# ---------------------------------------------------------------- 不留下没人认领的系统止损


async def test_a_replacement_that_lands_after_a_still_failing_retry_leaves_no_unclaimed_stop(make_rig):
    """gap 模式：新止损的请求结果未知、超过 stop_gap_max_s 后按「仍失败」补挂了一张；之后那张新止损其实落地、回查确认 replaced。
    交易所上同向有两张系统全平止损：登记簿必须一张是当前止损、另一张记为待清理（交给对账撤），不能有一张谁也不认。"""
    from dmr_kernel.ledger import repo_orders

    rig = make_rig(wait_s=0.1)
    await rig.start()
    rec, old = await _long_with_stop(rig, takeover=True)
    _unresolvable_new_stop(rig)
    await _trail(rig)
    (replace,) = await rig.intents_of("stop_replace")
    for _ in range(3):
        await rig.tick(10_000)
    await rig.tick(2_000)  # 32 秒：按「仍失败」转无保护
    assert (await rig.row(rec.id)).protection_state is ProtectionState.NONE
    await rig.tick(10_000)  # 持续重试：补挂一张
    placed = [a for a in rig.exchange.stops() if a.algo_id != old.algo_id]
    assert len(placed) == 1 and (await rig.row(rec.id)).stop_algo_id == placed[0].algo_id
    orders = await rig.ledger.read(repo_orders.for_intent, replace.id)
    late_cid = orders[0].client_order_id
    rig.exchange.add_stop(SYM, LONG, "97.5", cid=late_cid)  # 换止损的新单其实落地了
    rig.exchange.set_read("fetch_algo_order", rig.exchange._algo_order)
    await rig.tick(5_000)
    await rig.tick(1_000)
    (replace,) = await rig.intents_of("stop_replace")
    assert replace.params["result"]["outcome"] == "replaced"
    row = await rig.row(rec.id)
    claimed = {row.stop_algo_id} | {e.get("algo_id") for e in row.stale_stops}
    on_exchange = {a.algo_id for a in rig.exchange.stops()}
    assert len(on_exchange) == 2 and on_exchange <= claimed
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_algo_id in on_exchange


# ---------------------------------------------------------------- 取消托管（外部·已忽略）之后不再替它挂止损


async def test_a_position_ignored_after_going_unprotected_gets_no_more_stop_attempts(make_rig):
    """接管仓挂不上止损转「无保护」，告警请用户选择「手填止损价、平仓或取消托管」；用户取消托管后它是「外部（已忽略）」，
    持续重试必须停下：不再替它挂止损，也不再发无保护告警。"""
    rig = make_rig()
    await rig.start()
    for _ in range(8):
        rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    rec = await rig.open_takeover()
    await rig.protection.ensure_protected(rec.id, reason="registered")
    for _ in range(3):
        await rig.tick(10_000)
    assert (await rig.row(rec.id)).protection_state is ProtectionState.NONE
    writes, unprotected = len(rig.writes("create_stop_order")), len(rig.notifier.of("protect_unprotected"))
    await rig.registry.ignore(rec.id, expected_version=None)
    for _ in range(4):
        await rig.tick(10_000)
    assert len(rig.writes("create_stop_order")) == writes
    assert len(rig.notifier.of("protect_unprotected")) == unprotected
    assert not rig.suppressed()  # 已忽略的仓不算登记仓，抑制随之解除


async def test_an_ignored_position_with_a_users_stop_is_not_rechecked_into_a_system_stop(make_rig):
    """外部保护的接管仓被取消托管后，外部数量变化的事件不能把它当成「外部保护失效」去挂系统止损。"""
    from dmr_kernel.core.events import PositionChanged

    rig = make_rig()
    await rig.start()
    rec = await rig.open_takeover()
    user = rig.exchange.add_stop(SYM, LONG, "95", cid=None, close_position=False, qty="1")
    rig.candles.set(SYM, history_then([]))
    assert await rig.protection.ensure_protected(rec.id, reason="takeover") == "external"
    await rig.registry.ignore(rec.id, expected_version=None)
    await rig.protection.start()
    await rig.settle()
    rig.exchange.mutate(user, raw_status="CANCELED")  # 用户撤了自己的止损，又调整了数量
    rig.bus.publish(PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="外部调整", version=99,
                                    at_ms=rig.clock.now_ms()))
    await rig.settle()
    await rig.tick(10_000)
    assert rig.writes("create_stop_order") == []
    assert not rig.notifier.of("external_protection_lost")


# ---------------------------------------------------------------- 算止损价时出了意外错误：照样计入重试，窗口用完照样下结论


def _broken_candles(rig):
    def frame(*_args, **_kwargs):
        raise RuntimeError("K 线缓存坏了")

    rig.candles.frame = frame


async def test_an_unexpected_error_while_computing_the_stop_still_ends_in_the_unprotected_verdict(make_rig):
    """算止损价时抛出的不是「算不出」而是意外错误：也算一次失败的尝试。不能每个 tick 都重新来过、一直停在「重建中」：
    那样既不置抑制也不发最高级告警，谁也不知道这个接管仓没有保护（F2 第 4、5 步，4.11）。"""
    rig = make_rig()
    await rig.start()
    _broken_candles(rig)
    rec = await rig.open_takeover()
    assert await rig.protection.ensure_protected(rec.id, reason="takeover") == "rebuilding"
    for _ in range(3):
        await rig.tick(10_000)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.NONE and "K 线缓存坏了" in row.stop_note
    assert rig.suppressed()
    (alert,) = rig.notifier.of("protect_unprotected")
    assert alert["level"].value == "critical"
    assert rig.writes("create_stop_order") == [] and rig.writes("create_market_order") == []  # 接管仓不自动平


async def test_an_unexpected_error_while_computing_the_stop_market_closes_a_strategy_position_after_the_window(make_rig):
    rig = make_rig()
    await rig.start()
    _broken_candles(rig)
    rec = await rig.open_strategy()
    assert await rig.protection.ensure_protected(rec.id, reason="registered") == "rebuilding"
    for _ in range(2):
        await rig.tick(10_000)
    assert rig.writes("create_market_order") == []  # 窗口内只重试
    await rig.tick(10_000)
    (close,) = rig.writes("create_market_order")
    assert close["position_side"] is LONG and close["qty"] == D(1)
    assert rig.registry.cooldown_until(SYM) is not None


# ---------------------------------------------------------------- 保护性平仓一直没有结论：超过单仓平仓时限就是「市价平仓也失败」


async def test_a_protective_close_that_stays_unresolved_raises_the_critical_alert_after_the_close_time_limit(make_rig):
    """策略仓挂不上止损、保护性市价平仓又一直没有结论（读不到持仓，协调者稍后重试，意图停在回查中）：仓还在、没有止损。
    超过 ``close_one_timeout_s`` 仍没平掉就是 F2 第 4 步的「市价平仓也失败」（4.11：要在 protect_retry_window_s 加
    close_one_timeout_s 秒内被市价平掉）：发最高级告警，只发一次；平仓仍由那个意图重试，不另发第二笔。"""
    from dmr_kernel.core.types import AlertLevel

    rig = make_rig()
    await rig.start()
    for _ in range(4):
        rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    rec = await rig.open_strategy()
    await rig.protection.ensure_protected(rec.id, reason="registered")
    await rig.tick(10_000)
    await rig.tick(10_000)
    rig.exchange.set_read("fetch_positions", Unknown("交易所读不到"))
    await rig.tick(10_000)  # 30 秒：第 4 次尝试也失败 -> 保护性平仓，读不到持仓 -> 回查中
    (close,) = await rig.intents_of("close")
    assert close.state is IntentState.RECONCILING
    await rig.tick(10_000)
    assert rig.notifier.of("protective_close_failed") == []  # 平仓发出后 10 秒：还在单仓平仓时限（15 秒）之内
    await rig.tick(6_000)
    (alert,) = rig.notifier.of("protective_close_failed")
    assert alert["level"] is AlertLevel.CRITICAL
    for _ in range(3):
        await rig.tick(10_000)
    assert len(rig.notifier.of("protective_close_failed")) == 1
    assert len(await rig.intents_of("close")) == 1 and rig.writes("create_market_order") == []
    assert (await rig.row(rec.id)).protection_state is ProtectionState.NONE and rig.suppressed()


# ---------------------------------------------------------------- 算止损价期间仓位被取消托管或已平：不再提交挂单


@pytest.mark.parametrize("what", ["ignored", "closed"])
async def test_a_position_that_stops_being_registered_while_the_stop_is_computed_gets_no_stop(make_rig, what):
    """接管仓算止损价要先读现价（有等待）；这期间用户取消托管（外部·已忽略）或仓位已平：提交挂单前要再看一眼登记簿，
    不能把一张全平型止损挂到用户已经收回的仓、或已经归零的仓上。"""
    from dmr_kernel.core.types import ExitReason

    rig = make_rig()
    await rig.start()
    rec = await rig.open_takeover()
    original = rig.protection._plan_stop

    async def plan_then_change(position, cycle):
        planned = await original(position, cycle)
        if what == "ignored":
            await rig.registry.ignore(position.id, expected_version=None)
        else:
            rig.exchange.held.pop((SYM, LONG), None)
            await rig.registry.mark_closed(position.id, exit_reason=ExitReason.EXTERNAL, closed_ms=rig.clock.now_ms())
        return planned

    rig.protection._plan_stop = plan_then_change
    status = await rig.protection.ensure_protected(rec.id, reason="takeover")
    await rig.settle()
    assert status == ("skipped" if what == "ignored" else "closed")
    assert rig.writes("create_stop_order") == [] and await rig.intents_of("stop_place") == []
    await rig.tick(10_000)
    assert rig.writes("create_stop_order") == []


# ---------------------------------------------------------------- 迟到的失败结论不推翻之后已经挂上的止损


async def test_a_late_failed_update_does_not_turn_a_newer_stop_back_into_unprotected(make_rig):
    """gap 模式：新止损结果未知、超过 stop_gap_max_s 按「仍失败」转无保护并补挂了一张；之后那次更新回查确认新单不存在，
    协调者报 unprotected。此时登记簿里已是补挂的那张（系统保护）：迟到的失败结论不能再把它记回「无保护」、
    再发一次最高级告警、再置抑制，也不另挂一张。"""
    rig = make_rig(wait_s=0.1)
    await rig.start()
    rec, old = await _long_with_stop(rig, takeover=True)
    _unresolvable_new_stop(rig)
    await _trail(rig)
    for _ in range(3):
        await rig.tick(10_000)
    await rig.tick(2_000)  # 32 秒：按「仍失败」转无保护
    await rig.tick(10_000)  # 持续重试：补挂一张
    (placed,) = rig.exchange.stops()
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_algo_id == placed.algo_id
    alerts, writes = len(rig.notifier.of("protect_unprotected")), len(rig.writes("create_stop_order"))
    rig.exchange.set_read("fetch_algo_order", rig.exchange._algo_order)  # 回查读得到了：新单确实不存在
    for _ in range(4):
        await rig.tick(2_000)
    (replace,) = await rig.intents_of("stop_replace")
    assert replace.params["result"]["outcome"] == "unprotected"
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_algo_id == placed.algo_id
    assert len(rig.notifier.of("protect_unprotected")) == alerts and not rig.suppressed()
    await rig.tick(10_000)
    assert len(rig.writes("create_stop_order")) == writes and rig.exchange.stops() == [placed]
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM
