"""持仓对账的比对（F3 触发条件、F8）：未登记持仓、数量不一致的三种结局、连续 3 次零持仓才判已平。"""
from __future__ import annotations

import asyncio

from dmr_kernel.core.events import ModelStateChanged, PositionClosed, UnregisteredPositionFound
from dmr_kernel.core.types import AlertLevel, ExitReason, Gate, PositionStatus, Suppression
from dmr_kernel.ledger import repo_audit, repo_reconcile
from reconcile_kit import BUY, D, LONG, SELL, SYM, SYM2, T0, pos, trade, web_view


async def open_diffs(env):
    return await env.ledger.read(repo_reconcile.list_diffs, open_only=True)


# ================================================================ 交易所有、登记簿没有


async def test_a_position_the_registry_does_not_know_is_reported_as_unregistered(make_env, clock):
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "0.5", entry="101")]
    await env.clear_events()

    assert await env.reconciler.reconcile_positions() == "ok"  # 外部仓不是「对账不一致」
    await env.settle()

    [found] = env.events_of(UnregisteredPositionFound)
    assert (found.symbol, found.side, found.qty, found.entry_price, found.trigger, found.at_ms) == (
        SYM, LONG, D("0.5"), D("101"), "reconcile", clock.now_ms())
    assert env.suppressions() == set()
    [diff] = await open_diffs(env)
    assert (diff.symbol, diff.side, diff.kind, diff.exchange_qty) == (SYM, LONG, "unregistered", D("0.5"))
    # 登记簿一个字没动：识别和归类是模型 2 的事
    assert env.registry.all_active() == []


async def test_a_key_with_a_system_intent_in_flight_is_not_judged_external(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "0.5")]
    env.coordinator.busy.add((SYM, LONG))
    await env.clear_events()

    assert await env.reconciler.reconcile_positions() == "ok"
    await env.settle()
    assert env.events_of(UnregisteredPositionFound) == []
    assert await open_diffs(env) == []

    env.coordinator.busy.clear()  # 在途的意图结束了：这一轮再判
    await env.reconciler.reconcile_positions()
    await env.settle()
    assert len(env.events_of(UnregisteredPositionFound)) == 1


async def test_a_filled_system_order_whose_fills_are_not_booked_yet_is_not_external(make_env):
    # 系统开仓单的 REST 应答已经是 FILLED、意图也完成了，但成交明细（数据流或补拉）还没入账：登记簿暂时没有这个仓
    env = await make_env().boot()
    order, fill = await env.system_open_fill(qty="0.5")
    env.positions = [pos(SYM, LONG, "0.5")]
    await env.clear_events()

    await env.reconciler.reconcile_positions()
    await env.settle()
    assert env.events_of(UnregisteredPositionFound) == []

    await env.ingest.ingest_trade(fill, client_order_id=order.client_order_id, path="rest")  # 成交入账，登记簿登记
    await env.settle()
    assert env.registry.get_active(SYM, LONG) is not None
    await env.reconciler.reconcile_positions()
    await env.settle()
    assert env.events_of(UnregisteredPositionFound) == []  # 已登记，更不会是外部仓


async def test_an_unregistered_position_is_announced_once_until_it_changes(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "0.5")]
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()
    await env.settle()
    assert len(env.events_of(UnregisteredPositionFound)) == 1
    assert len(await open_diffs(env)) == 1

    env.positions = [pos(SYM, LONG, "0.7")]  # 数量变了：有新信息，再报一次
    await env.reconciler.reconcile_positions()
    await env.settle()
    assert [e.qty for e in env.events_of(UnregisteredPositionFound)] == [D("0.5"), D("0.7")]
    [diff] = await open_diffs(env)
    assert diff.exchange_qty == D("0.7")  # 同一行更新，不堆积

    env.positions = []  # 消失之后又出现：重新报
    await env.reconciler.reconcile_positions()
    assert await open_diffs(env) == []
    env.positions = [pos(SYM, LONG, "0.7")]
    await env.reconciler.reconcile_positions()
    await env.settle()
    assert len(env.events_of(UnregisteredPositionFound)) == 3


async def test_a_registered_key_is_never_reported_and_closes_the_open_unregistered_diff(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "0.5")]
    await env.reconciler.reconcile_positions()
    assert len(await open_diffs(env)) == 1

    await env.add_position(qty="0.5")  # 模型 2 把它登记了
    await env.clear_events()
    await env.reconciler.reconcile_positions()
    await env.settle()
    assert env.events_of(UnregisteredPositionFound) == []
    assert await open_diffs(env) == []


# ================================================================ 数量不一致的三种结局


async def test_equal_quantities_need_no_trade_backfill(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1")]

    assert await env.reconciler.reconcile_positions() == "ok"

    assert env.adapter.calls("fetch_trades") == []
    assert len(env.adapter.calls("fetch_positions")) == 1  # 没有可疑的键，不多读
    assert (await env.registry.fetch(rec.id)).consistent is True


async def test_outcome_one_a_key_with_a_system_intent_in_flight_is_not_judged(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]
    env.coordinator.busy.add((SYM, LONG))

    assert await env.reconciler.reconcile_positions() == "ok"

    assert env.adapter.calls("fetch_trades") == []  # 在途就先不判，也不补拉
    assert env.suppressions() == set() and await open_diffs(env) == []
    stored = await env.registry.fetch(rec.id)
    assert stored.exchange_qty == D("1.5") and stored.consistent is False  # 看到了，只是还不下结论


async def test_outcome_two_a_difference_explained_by_an_external_fill_is_resolved_by_the_registry(make_env):
    # 用户手动加仓 0.5：成交没有经数据流到达，对账先补拉成交，登记簿按「外部调整」更新数量，再比就一致了
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    added = trade("t1", side=BUY, position_side=LONG, qty="0.5")
    env.trades = [added]
    env.order_views[added.exchange_order_id] = web_view(added)
    env.positions = [pos(SYM, LONG, "1.5")]

    assert await env.reconciler.reconcile_positions() == "ok"
    await env.settle()

    assert [c["symbol"] for c in env.adapter.calls("fetch_trades")] == [SYM]  # 只补拉这一个合约
    stored = await env.registry.fetch(rec.id)
    assert stored.qty == D("1.5") and stored.is_mixed is True and stored.consistent is True
    assert env.suppressions() == set() and await open_diffs(env) == []


async def test_outcome_three_an_unexplained_difference_sets_mismatch(make_env, clock):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]
    await env.clear_events()

    assert await env.reconciler.reconcile_positions() == "mismatch"
    await env.settle()

    assert Suppression.RECONCILE_MISMATCH in env.suppressions()
    assert env.modelstate.current().mismatch_since_ms == clock.now_ms()
    [diff] = await open_diffs(env)
    assert (diff.symbol, diff.side, diff.kind, diff.local_qty, diff.exchange_qty) == (
        SYM, LONG, "qty_mismatch", D("1"), D("1.5"))
    [alert] = env.alerts("reconcile_mismatch")
    assert alert.level is AlertLevel.CRITICAL
    stored = await env.registry.fetch(rec.id)
    assert stored.qty == D("1") and stored.consistent is False  # 登记簿不替人改数量
    assert [e.status for e in env.completed("positions")] == ["mismatch"]


async def test_an_unexplained_difference_on_an_unregistered_status_is_recorded_but_is_no_mismatch(make_env):
    # 「对账不一致」只针对接管中、托管中、平仓中的登记仓；待确认仓和外部仓不计入
    env = await make_env().boot()
    await env.add_position(symbol=SYM, status="pending", qty="1")
    await env.add_position(symbol=SYM2, status="ignored", qty="1")
    env.positions = [pos(SYM, LONG, "1.2"), pos(SYM2, LONG, "1.2")]

    assert await env.reconciler.reconcile_positions() == "ok"

    assert Suppression.RECONCILE_MISMATCH not in env.suppressions()
    assert {d.symbol for d in await open_diffs(env)} == {SYM, SYM2}


async def test_taking_over_and_closing_positions_count_as_registered_for_mismatch(make_env):
    env = await make_env().boot()
    await env.add_position(symbol=SYM, status="closing", qty="1")
    env.positions = [pos(SYM, LONG, "2")]
    assert await env.reconciler.reconcile_positions() == "mismatch"
    assert Suppression.RECONCILE_MISMATCH in env.suppressions()

    await env.add_position(symbol=SYM2, status="taking_over", qty="1")  # 接管中也是登记仓
    env.positions = [pos(SYM, LONG, "1"), pos(SYM2, LONG, "3")]
    assert await env.reconciler.reconcile_positions() == "mismatch"
    assert {d.symbol for d in await open_diffs(env)} == {SYM2}


async def test_a_persisting_mismatch_keeps_one_diff_row_and_one_alert(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]
    for _ in range(3):
        assert await env.reconciler.reconcile_positions() == "mismatch"

    assert len(await open_diffs(env)) == 1
    assert len(env.alerts("reconcile_mismatch")) == 1
    assert len(env.reconciler.status()["diffs"]) == 1
    # 同样的差异上一轮已经补拉查过：不再每一轮都补拉、复核
    assert len(env.adapter.calls("fetch_trades")) == 1 and len(env.adapter.calls("fetch_positions")) == 4


async def test_the_mismatch_suppression_needs_a_human_even_when_the_difference_disappears(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]
    await env.reconciler.reconcile_positions()

    env.positions = [pos(SYM, LONG, "1")]  # 差异消失了
    assert await env.reconciler.reconcile_positions() == "ok"
    assert await open_diffs(env) == []  # 差异行记为已解决
    assert Suppression.RECONCILE_MISMATCH in env.suppressions()  # 但抑制要人工确认才解除
    assert env.modelstate.current().mismatch_since_ms is not None


async def test_confirm_mismatch_lifts_the_suppression_and_leaves_an_audit_record(make_env, clock):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]
    await env.reconciler.reconcile_positions()

    assert await env.reconciler.confirm_mismatch(operator="alice") is True

    assert Suppression.RECONCILE_MISMATCH not in env.suppressions()
    assert env.modelstate.current().mismatch_since_ms is None
    assert await open_diffs(env) == []
    audit = await env.ledger.read(repo_audit.recent, action="reconcile.confirm_mismatch")
    assert [(a.operator, a.at_ms) for a in audit] == [("alice", clock.now_ms())]
    assert await env.reconciler.confirm_mismatch(operator="alice") is False  # 没有可确认的

    # 差异其实还在：下一轮重新发现，重新置「对账不一致」
    assert await env.reconciler.reconcile_positions() == "mismatch"
    assert Suppression.RECONCILE_MISMATCH in env.suppressions()


async def test_a_mismatch_unresolved_for_too_long_closes_the_gate(make_env, clock):
    env = await make_env(reconcile_mismatch_stop_s=300).boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="测试")
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]
    await env.reconciler.reconcile_positions()

    clock.advance(299_999)
    await env.reconciler.reconcile_positions()
    assert env.modelstate.current().gate is Gate.RUNNING
    assert env.alerts("reconcile_mismatch_stop") == []

    clock.advance(1)  # 恰好 300 秒
    await env.reconciler.reconcile_positions()
    assert env.modelstate.current().gate is Gate.STOPPED
    [alert] = env.alerts("reconcile_mismatch_stop")
    assert alert.level is AlertLevel.CRITICAL
    # 关了闸，抑制仍在，等人工确认
    assert Suppression.RECONCILE_MISMATCH in env.suppressions()


async def test_the_auto_stop_timer_runs_even_when_the_exchange_is_unreachable(make_env, clock):
    env = await make_env(reconcile_mismatch_stop_s=300).boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="测试")
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]
    await env.reconciler.reconcile_positions()

    env.fail("fetch_positions")
    clock.advance(301_000)
    assert await env.reconciler.reconcile_positions() == "unknown"
    assert env.modelstate.current().gate is Gate.STOPPED


# ================================================================ 登记仓在交易所为零


async def test_three_consecutive_explicit_zeros_close_the_position(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = []
    await env.clear_events()

    for _ in range(2):
        assert await env.reconciler.reconcile_positions() == "ok"
        assert env.registry.get_active(SYM, LONG) is not None  # 单次、两次零读数都不判已平
    assert (await env.registry.fetch(rec.id)).exchange_qty == D("0")
    assert env.alerts("position_closed_by_reconcile") == []

    assert await env.reconciler.reconcile_positions() == "ok"
    await env.settle()
    assert env.registry.get_active(SYM, LONG) is None
    closed = await env.registry.fetch(rec.id)
    assert closed.status is PositionStatus.CLOSED and closed.exit_reason is ExitReason.UNKNOWN
    [event] = env.events_of(PositionClosed)
    assert event.position_id == rec.id and event.exit_reason == "unknown"
    [alert] = env.alerts("position_closed_by_reconcile")
    assert alert.level is AlertLevel.WARNING


async def test_a_non_zero_reading_restarts_the_zero_count(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    for readings in ([], [], [pos(SYM, LONG, "1")], [], []):
        env.positions = readings
        await env.reconciler.reconcile_positions()
    assert env.registry.get_active(SYM, LONG).id == rec.id  # 零、零、非零、零、零：没有连续三次

    env.positions = []
    await env.reconciler.reconcile_positions()  # 现在是连续第三次
    assert env.registry.get_active(SYM, LONG) is None


async def test_an_unknown_round_breaks_the_zero_count(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = []
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()
    env.fail("fetch_positions")
    assert await env.reconciler.reconcile_positions() == "unknown"
    env.heal()
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()
    assert env.registry.get_active(SYM, LONG).id == rec.id  # 未知不是零：之前的两次不能算数

    await env.reconciler.reconcile_positions()
    assert env.registry.get_active(SYM, LONG) is None


async def test_a_trade_record_showing_the_position_went_flat_closes_it_without_waiting(make_env):
    # 「成交记录显示过零」：补拉到用户手动平仓的成交，登记簿按成交归零（出场原因：外部平仓），第一轮就判已平
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    exit_fill = trade("t9", side=SELL, position_side=LONG, qty="1", time_ms=T0 + 1)
    env.trades = [exit_fill]
    env.order_views[exit_fill.exchange_order_id] = web_view(exit_fill)
    env.positions = []
    await env.clear_events()

    assert await env.reconciler.reconcile_positions() == "ok"
    await env.settle()

    assert env.registry.get_active(SYM, LONG) is None
    closed = await env.registry.fetch(rec.id)
    assert closed.status is PositionStatus.CLOSED and closed.exit_reason is ExitReason.EXTERNAL
    assert len(env.events_of(PositionClosed)) == 1  # 对账没有再判一次已平
    assert env.alerts("position_closed_by_reconcile") == []


async def test_pending_and_ignored_records_are_closed_after_three_zeros_too(make_env):
    env = await make_env().boot()
    await env.add_position(symbol=SYM, status="pending")
    await env.add_position(symbol=SYM2, status="ignored")
    env.positions = []
    for _ in range(3):
        await env.reconciler.reconcile_positions()
    assert env.registry.all_active() == []
    # 用户自己的仓没了只是知会一声，不是需要处理的警告
    assert {a.level for a in env.alerts("position_closed_by_reconcile")} == {AlertLevel.INFO}


async def test_the_zero_count_belongs_to_the_record_not_the_key(make_env):
    env = await make_env().boot()
    await env.add_position()
    env.positions = []
    for _ in range(3):
        await env.reconciler.reconcile_positions()
    assert env.registry.get_active(SYM, LONG) is None

    second = await env.add_position()  # 同一个键，新一代的记录
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()
    assert env.registry.get_active(SYM, LONG).id == second.id  # 从头数


async def test_a_zero_reading_triggers_a_trade_backfill_each_round(make_env):
    env = await make_env().boot()
    await env.add_position()
    env.positions = []
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()
    assert [c["symbol"] for c in env.adapter.calls("fetch_trades")] == [SYM, SYM]


# ================================================================ 补拉之后的复核读数


async def test_suspects_cost_exactly_one_extra_positions_read_for_all_of_them(make_env):
    env = await make_env().boot()
    await env.add_position(symbol=SYM, qty="1")
    await env.add_position(symbol=SYM2, qty="1")
    env.positions = [pos(SYM, LONG, "2"), pos(SYM2, LONG, "3")]

    await env.reconciler.reconcile_positions()

    assert sorted(c["symbol"] for c in env.adapter.calls("fetch_trades")) == [SYM, SYM2]
    assert len(env.adapter.calls("fetch_positions")) == 2  # 首读一次，补拉之后复核读一次


async def test_if_the_recheck_read_fails_nothing_is_concluded(make_env):
    from dmr_kernel.core.types import Known, Unknown

    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    answers = [Known([pos(SYM, LONG, "1.5")]), Unknown("复核读数失败")]
    env.adapter.set_read("fetch_positions", lambda: answers.pop(0) if len(answers) > 1 else answers[0])

    # 首读成功；复核读数未知：这一轮对这个键不下结论，也就不算一轮完整的持仓对账（core.guard 不能据此结束「数据流切换中」）
    assert await env.reconciler.reconcile_positions() == "unknown"
    await env.settle()

    assert env.suppressions() == set() and await open_diffs(env) == []
    assert env.registry.get_active(SYM, LONG).id == rec.id
    assert [e.status for e in env.completed("positions")] == ["unknown"]
    assert env.reconciler.status()["unknown_streak"] == 0  # 读本身是成功的：不累计「连续未知」，不置「对账未知」


async def test_if_the_recheck_read_fails_the_third_zero_does_not_close_yet(make_env):
    from dmr_kernel.core.types import Known, Unknown

    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = []
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()
    answers = [Known([]), Unknown("复核读数失败")]
    env.adapter.set_read("fetch_positions", lambda: answers.pop(0) if len(answers) > 1 else answers[0])

    await env.reconciler.reconcile_positions()
    assert env.registry.get_active(SYM, LONG).id == rec.id

    env.adapter.set_read("fetch_positions", lambda: Known([]))
    await env.reconciler.reconcile_positions()  # 下一轮读得到了：确认
    assert env.registry.get_active(SYM, LONG) is None


async def test_a_position_that_reappears_in_the_recheck_is_not_closed(make_env):
    from dmr_kernel.core.types import Known

    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = []
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_positions()
    answers = [Known([]), Known([pos(SYM, LONG, "1")])]  # 首读为零，复核读数又看到了
    env.adapter.set_read("fetch_positions", lambda: answers.pop(0) if len(answers) > 1 else answers[0])

    await env.reconciler.reconcile_positions()
    assert env.registry.get_active(SYM, LONG).id == rec.id


async def test_if_the_event_bus_cannot_settle_nothing_is_concluded(make_env):
    # 补拉到的成交要经事件总线交给登记簿；总线迟迟处理不完时，不能拿还没更新的登记簿去判「不一致」
    env = await make_env().boot()
    env.reconciler.DRAIN_TIMEOUT_S = 0.05
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]
    blocked = asyncio.Event()

    async def stuck(_event):
        await blocked.wait()

    env.bus.subscribe(ModelStateChanged, stuck, name="test.stuck")
    env.bus.publish(ModelStateChanged(version=99, at_ms=T0))
    await asyncio.sleep(0.01)

    assert await env.reconciler.reconcile_positions() == "unknown"
    assert env.suppressions() == set() and await open_diffs(env) == []
    blocked.set()



async def test_a_failed_backfill_leaves_only_that_symbol_undecided(make_env, monkeypatch):
    env = await make_env().boot()
    await env.add_position(symbol=SYM, qty="1")
    await env.add_position(symbol=SYM2, qty="1")
    env.positions = [pos(SYM, LONG, "2"), pos(SYM2, LONG, "2")]
    real = env.ingest.backfill_trades
    broken = {SYM}

    async def flaky(symbols):
        if broken & set(symbols):
            raise RuntimeError("账本忙")
        return await real(symbols)

    monkeypatch.setattr(env.ingest, "backfill_trades", flaky)

    assert await env.reconciler.reconcile_positions() == "mismatch"  # SYM2 照常判定
    assert {d.symbol for d in await open_diffs(env)} == {SYM2}  # SYM 没补拉成：不下结论
    assert env.reconciler.status()["positions_status"] == "mismatch"

    broken.clear()
    assert await env.reconciler.reconcile_positions() == "mismatch"
    assert {d.symbol for d in await open_diffs(env)} == {SYM, SYM2}


async def test_only_a_round_without_undecided_keys_is_ok(make_env):
    # 有键因为在途先不判，不影响这一轮算不算完整对账；有键因为技术原因没能复核，才不算
    env = await make_env().boot()
    await env.add_position(symbol=SYM, qty="1")
    env.positions = [pos(SYM, LONG, "2")]
    env.coordinator.busy.add((SYM, LONG))

    assert await env.reconciler.reconcile_positions() == "ok"
