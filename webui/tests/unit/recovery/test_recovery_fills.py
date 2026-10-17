"""重启恢复第 4 步（成交回放）：暂缓的成交里「快照已经反映、登记簿还没有」的回放进登记簿。

系统自己的成交一律回放（中断点 1：开仓成交了没登记；停机期间止损触发 → 按成交补记为已平、出场原因「止损」）；
已有记录的键上的外部成交也回放（登记簿靠成交推进，不靠快照）；没有记录的键上的外部成交不回放（那个仓由快照登记为待确认）。
快照之后的成交留给释放时按顺序应用。"""
from __future__ import annotations

from dmr_kernel.core.registry import PRESUMED_STOP_NOTE
from dmr_kernel.core.types import AlgoType, ExitReason, OrderOrigin, OrderSide, PositionStatus, Suppression
from dmr_kernel.ledger import repo_ingest
from recovery_kit import D, LONG, SHORT, SYM, SYM2, algo, order_view, pos, trade

STOP = ("DKT-0-1-s", "9001", "95")


async def test_a_system_open_whose_receipt_was_lost_is_registered_as_a_strategy_position(make_env):
    """中断点 1：开仓单发出、回执没到。恢复回查到已成交、补拉到成交 → 按 F1 第 7 步登记为策略仓，不是待确认。"""
    env = await make_env().boot()
    order = await env.open_order_row(qty="1", price="100")
    env.clock.advance(20_000)  # 停机期间
    env.positions = [pos(qty="1", entry="100")]
    result = await env.run()
    rec = env.registry.get_active(SYM, LONG)
    assert rec is not None and rec.status is PositionStatus.MANAGED and rec.qty == D("1")
    assert rec.opened_by_intent_id == env.last_intent.id
    assert result.steps["fills"]["registered_from_fills"] == [
        {"position_id": rec.id, "symbol": SYM, "side": "LONG", "status": "managed"}]
    assert result.steps["reconcile"]["pending_registered"] == []
    assert (await env.orders.get(order.client_order_id)).state.value == "FILLED"
    assert result.steps["release"]["skipped"] == 1 and result.steps["release"]["applied"] == 0  # 已回放，释放时不再应用


async def test_a_stop_that_fired_while_the_kernel_was_down_closes_the_position_as_a_stop(make_env):
    """停机期间止损触发（触发后订单号可关联）：成交补拉回来，登记簿按成交平掉，出场原因「止损」。"""
    env = await make_env().boot()
    info = await env.system_stop(trigger="95", algo_id="9001", on_exchange=False)
    rec = await env.add_position(qty="1", avg="100", stop=(info.client_algo_id, "9001", "95"))
    env.clock.advance(60_000)
    env.algo_views["9001"] = algo(cid=info.client_algo_id, algo_id="9001", raw_status="FINISHED",
                                  actual_order_id="TRIG1")
    env.trades = [trade("tg1", side=OrderSide.SELL, position_side=LONG, qty="1", price="94", order_id="TRIG1",
                        time_ms=env.clock.now_ms() - 30_000)]
    env.positions = []
    result = await env.run()
    assert env.registry.get_active(SYM, LONG) is None
    closed = await env.registry.fetch(rec.id)
    assert closed.status is PositionStatus.CLOSED and closed.exit_reason is ExitReason.STOP
    assert result.steps["fills"]["closed_by_fills"] == [
        {"position_id": rec.id, "symbol": SYM, "side": "LONG", "exit_reason": "stop"}]
    assert result.steps["summary"]["closed_by_fills"] == 1


async def test_a_stop_that_fired_while_down_is_booked_as_a_stop_even_when_the_fill_cannot_be_linked(make_env):
    """触发后订单号关联不上（交易所随机编号，来源记为 ccxt 默认）：满足「推定为系统止损触发」仍记为止损。"""
    env = await make_env().boot()
    rec = await env.add_position(qty="1", avg="100", stop=STOP)
    env.clock.advance(60_000)
    env.order_views["X9"] = order_view(cid="x-cvBPrNm9abcdef", oid="X9", opening=False, price="94")
    env.trades = [trade("tg2", side=OrderSide.SELL, position_side=LONG, qty="1", price="94", order_id="X9",
                        time_ms=env.clock.now_ms() - 30_000)]
    env.positions = []
    await env.run()
    closed = await env.registry.fetch(rec.id)
    assert closed.status is PositionStatus.CLOSED and closed.exit_reason is ExitReason.STOP
    assert PRESUMED_STOP_NOTE in closed.stop_note


async def test_an_external_partial_close_during_downtime_is_applied_once(make_env):
    """已有记录的键上的外部成交：登记簿靠成交推进（外部调整），回放一次，释放时不再应用第二次。"""
    env = await make_env().boot()
    rec = await env.add_position(qty="1", avg="100", stop=STOP)
    env.clock.advance(60_000)
    env.order_views["E1"] = order_view(cid="web_abc", oid="E1", qty="0.4", opening=False, price="101")
    env.trades = [trade("te1", side=OrderSide.SELL, position_side=LONG, qty="0.4", price="101", order_id="E1",
                        time_ms=env.clock.now_ms() - 10_000)]
    env.positions = [pos(qty="0.6", entry="100")]
    result = await env.run()
    now = env.registry.get_active(SYM, LONG)
    assert now.id == rec.id and now.qty == D("0.6")
    assert result.steps["fills"]["replayed"] == 1
    assert result.steps["release"]["skipped"] == 1 and result.steps["release"]["applied"] == 0


async def test_a_fill_that_cannot_be_replayed_is_a_problem_but_the_others_still_are(make_env, monkeypatch):
    env = await make_env().boot()
    await env.add_position(qty="1", avg="100", stop=STOP)
    env.clock.advance(60_000)
    now = env.clock.now_ms()
    for name, qty in (("E1", "0.4"), ("E2", "0.1")):
        env.order_views[name] = order_view(cid=f"web_{name}", oid=name, qty=qty, opening=False, price="101")
    env.trades = [
        trade("te1", side=OrderSide.SELL, position_side=LONG, qty="0.4", price="101", order_id="E1", time_ms=now - 20_000),
        trade("te2", side=OrderSide.SELL, position_side=LONG, qty="0.1", price="101", order_id="E2", time_ms=now - 10_000)]
    env.positions = [pos(qty="0.5", entry="100")]
    real = env.registry.apply_fill

    async def flaky(event):
        if event.trade.trade_id == "te1":
            raise RuntimeError("登记簿写失败")
        return await real(event)

    monkeypatch.setattr(env.registry, "apply_fill", flaky)
    result = await env.run()
    assert result.steps["fills"]["replayed"] == 1 and result.steps["fills"]["replay_failed"] == 1
    assert env.registry.get_active(SYM, LONG).qty == D("0.9")  # 第二笔应用了，第一笔没有
    assert result.tradable is False and any("回放成交" in p and "1 笔" in p for p in result.problems)
    assert Suppression.RECOVERING in env.suppressions() and env.registry.holding is False


async def test_an_external_open_on_a_key_without_a_record_is_not_replayed_but_registered_pending_from_the_snapshot(make_env):
    """没有记录的键上的外部成交不回放：待确认仓的数量取自快照（已含这笔成交），再应用就重复计数了。"""
    env = await make_env().boot()
    env.clock.advance(60_000)
    env.order_views["E2"] = order_view(symbol=SYM2, cid="web_xyz", oid="E2", qty="0.5", price="3000")
    env.trades = [trade("te2", symbol=SYM2, side=OrderSide.BUY, position_side=LONG, qty="0.5", price="3000",
                        order_id="E2", time_ms=env.clock.now_ms() - 10_000)]
    env.positions = [pos(SYM2, qty="0.5", entry="3000")]
    result = await env.run()
    rec = env.registry.get_active(SYM2, LONG)
    assert rec.status is PositionStatus.PENDING and rec.qty == D("0.5")  # 不是 1.0
    assert result.steps["fills"]["replayed"] == 0
    assert result.steps["release"]["skipped"] == 1


async def test_fills_after_the_snapshot_are_applied_at_the_release_not_replayed(make_env):
    """快照之后的成交（成交时间晚于服务器时间）不属于「快照已经反映」，释放时按顺序应用。"""
    env = await make_env().boot()
    await env.open_order_row(qty="1", price="100")  # 成交时间 = 现在
    env.skew_ms = -100  # 快照的服务器时间比这笔成交早
    env.positions = []
    result = await env.run()
    rec = env.registry.get_active(SYM, LONG)
    assert rec is not None and rec.qty == D("1")  # 释放时应用了
    assert result.steps["fills"]["replayed"] == 0
    assert result.steps["release"]["applied"] == 1 and result.steps["release"]["skipped"] == 0
    assert result.steps["reconcile"]["positions_reconcile"].startswith("skipped")  # 登记簿还没跟上，不做持仓对账
    assert env.calls_named("reconcile_positions") == []


async def test_replay_goes_in_trade_time_order(make_env):
    """先开后平的两笔系统成交，补拉接口把后面的排在前面：按成交时间回放，仓位归零（顺序反了会留下一个残仓）。"""
    env = await make_env().boot()
    await env.open_order_row(qty="1", price="100")
    env.clock.advance(1_000)
    await env.close_order_row(qty="1", price="110")
    env.trades.reverse()
    env.clock.advance(5_000)
    env.positions = []
    await env.run()
    assert env.registry.get_active(SYM, LONG) is None


# ================================================================ 迟到的成交：成交时间早于快照、到得比快照晚


async def deliver_late_external_fill(env, *, symbol: str, side=LONG, qty: str, opening: bool, oid: str) -> None:
    """数据流把一笔成交时间早于快照的外部成交送来了，但已经过了回放那一步（挂单对账时才到）。"""
    env.order_views[oid] = order_view(symbol=symbol, cid="web_late", oid=oid, qty=qty, opening=opening)
    late = trade(f"late-{oid}", symbol=symbol, side=side.open_side() if opening else side.close_side(),
                 position_side=side, qty=qty, price="100", order_id=oid, time_ms=env.clock.now_ms() - 10_000)

    async def arrives():
        env.calls.append("reconcile_orders")
        await env.ingest.ingest_trade(late, client_order_id="web_late", path="stream")
        return "ok"

    env.reconciler.reconcile_orders = arrives


async def test_a_late_fill_older_than_the_snapshot_is_still_applied_to_a_registered_position(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1", avg="100", stop=STOP)
    env.positions = [pos(qty="0.6", entry="100")]  # 交易所已经少了 0.4
    await deliver_late_external_fill(env, symbol=SYM, qty="0.4", opening=False, oid="L1")
    result = await env.run()
    assert env.registry.get_active(SYM, LONG).qty == D("0.6") and env.registry.get_active(SYM, LONG).id == rec.id
    assert result.steps["release"]["late_replayed"] == 1
    assert result.steps["release"]["skipped"] == 1 and result.steps["release"]["applied"] == 0  # 只应用一次


async def test_a_late_fill_is_not_applied_to_a_pending_position_registered_from_the_snapshot(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM2, qty="0.5", entry="3000")]
    await deliver_late_external_fill(env, symbol=SYM2, qty="0.5", opening=True, oid="L2")
    result = await env.run()
    rec = env.registry.get_active(SYM2, LONG)
    assert rec.status is PositionStatus.PENDING and rec.qty == D("0.5")  # 快照已含这笔：不再加一次
    assert result.steps["release"]["late_replayed"] == 0 and result.steps["release"]["skipped"] == 1


async def test_without_a_snapshot_there_is_no_late_replay(make_env):
    env = await make_env().boot()
    env.fail("fetch_positions")
    result = await env.run()
    assert result.steps["release"]["late_replayed"] == 0


# ================================================================ 全量对账：未登记的持仓一律进入待确认，不自动接管


async def test_an_unregistered_position_becomes_pending_and_is_never_taken_over(make_env):
    env = await make_env().boot()
    env.positions = [pos(SYM2, qty="0.5", entry="3000")]
    result = await env.run()
    rec = env.registry.get_active(SYM2, LONG)
    assert rec.status is PositionStatus.PENDING and rec.ownership.value == "none"
    assert rec.pending_reason.value == "preexisting" and "recovery" in rec.pending_flags
    assert (rec.qty, rec.avg_price) == (D("0.5"), D("3000"))
    assert "不自动接管" in rec.origin_note
    assert result.steps["reconcile"]["pending_registered"] == [
        {"position_id": rec.id, "symbol": SYM2, "side": "LONG", "reason": "preexisting"}]
    assert env.protection.rebuilt == []  # 待确认仓不是登记仓：不替它挂止损
    assert env.alerts("recovery_unregistered_position")


async def test_a_system_prefixed_stop_on_an_unregistered_position_is_kept_and_flagged_origin_to_verify(make_env):
    env = await make_env().boot()
    stop = algo(SYM2, LONG, cid=env.own_id(intent=9), algo_id="7001", trigger="2900")
    env.algos = [stop]
    env.positions = [pos(SYM2, qty="0.5", entry="3000")]
    result = await env.run()
    rec = env.registry.get_active(SYM2, LONG)
    assert rec.status is PositionStatus.PENDING and rec.pending_reason.value == "origin_to_verify"
    assert (rec.stop_client_id, rec.stop_algo_id, rec.stop_price) == (stop.client_algo_id, "7001", D("2900"))
    assert result.steps["reconcile"]["origin_to_verify"] == [rec.id]
    assert env.adapter.writes == [] and env.coordinator.submitted == []  # 保留这张止损：不撤、不另挂


async def test_a_stop_with_another_instances_system_prefix_is_flagged_origin_to_verify_too(make_env):
    env = await make_env().boot()
    env.algos = [algo(SYM2, LONG, cid=env.other_instance_id(), algo_id="7002")]
    env.positions = [pos(SYM2, qty="0.5", entry="3000")]
    await env.run()
    assert env.registry.get_active(SYM2, LONG).pending_reason.value == "origin_to_verify"


async def test_only_system_prefixed_stop_kind_open_orders_on_the_same_side_count_as_origin_to_verify(make_env):
    env = await make_env().boot()
    env.algos = [
        algo(SYM2, LONG, cid="web_stop", algo_id="1"),  # 用户自己的止损
        algo(SYM2, LONG, cid=env.own_id(intent=3), algo_id="2", algo_type=AlgoType.TAKE_PROFIT_MARKET),  # 止盈
        algo(SYM2, LONG, cid=env.own_id(intent=4), algo_id="3", raw_status="FINISHED"),  # 已不在挂
        algo(SYM2, SHORT, cid=env.own_id(intent=5), algo_id="4"),  # 另一侧
    ]
    env.positions = [pos(SYM2, qty="0.5", entry="3000")]
    await env.run()
    rec = env.registry.get_active(SYM2, LONG)
    assert rec.pending_reason.value == "preexisting" and rec.stop_client_id is None


async def test_legacy_format_fills_make_it_a_legacy_leftover(make_env):
    env = await make_env().boot()
    await env.ingest.ingest_trade(trade("tl1", symbol=SYM2, side=OrderSide.BUY, position_side=LONG, qty="0.5",
                                        price="3000", time_ms=env.clock.now_ms() - 3_600_000),
                                  client_order_id="DMR_LONG_buy_1760000000000", path="stream")
    await env.settle()
    assert (await env.ledger.read(_fills_of, SYM2))[0].origin is OrderOrigin.LEGACY
    env.positions = [pos(SYM2, qty="0.5", entry="3000")]
    await env.run()
    assert env.registry.get_active(SYM2, LONG).pending_reason.value == "legacy"


def _fills_of(session, symbol):
    return repo_ingest.list_fills(session, symbol=symbol)


async def test_both_sides_of_a_symbol_are_handled_independently(make_env):
    env = await make_env().boot()
    await env.add_position(side=LONG)  # 多仓登记过
    env.positions = [pos(side=LONG), pos(side=SHORT, qty="2", entry="105")]  # 空仓没登记
    await env.run()
    assert env.registry.get_active(SYM, LONG).status is PositionStatus.MANAGED
    short = env.registry.get_active(SYM, SHORT)
    assert short.status is PositionStatus.PENDING and short.qty == D("2")


async def test_an_already_registered_key_is_not_registered_again(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    env.positions = [pos()]
    result = await env.run()
    assert env.registry.get_active(SYM, LONG).id == rec.id
    assert result.steps["reconcile"]["pending_registered"] == []


# ================================================================ 查清之前不判定（F3 第 2 步）


async def test_a_key_with_a_system_intent_in_flight_is_not_judged(make_env):
    env = await make_env().boot()
    env.coordinator.busy.add((SYM2, LONG))
    env.positions = [pos(SYM2, qty="0.5", entry="3000")]
    result = await env.run()
    assert env.registry.get_active(SYM2, LONG) is None
    (deferred,) = result.steps["reconcile"]["deferred"]
    assert deferred["symbol"] == SYM2 and "在途" in deferred["why"]


async def test_a_key_with_an_unresolved_system_order_is_not_judged(make_env):
    env = await make_env().boot()
    await env.open_order_row(symbol=SYM2, qty="0.5", price="3000")
    env.fail("fetch_order")  # 订单查不清
    env.positions = [pos(SYM2, qty="0.5", entry="3000")]
    result = await env.run()
    assert env.registry.get_active(SYM2, LONG) is None
    assert "未到终态的系统订单" in result.steps["reconcile"]["deferred"][0]["why"]


async def test_a_key_whose_system_order_is_filled_but_whose_fills_are_not_booked_is_not_judged(make_env):
    """订单行已经是 FILLED、成交明细却没入账（成交接口还没跟上）：不能把系统自己开的仓当成外部仓。"""
    env = await make_env().boot()
    order = await env.open_order_row(symbol=SYM2, qty="0.5", price="3000", exchange_view=True)
    env.trades.clear()  # 交易所的成交接口还没给出这笔成交
    env.positions = [pos(SYM2, qty="0.5", entry="3000")]
    result = await env.run()
    assert (await env.orders.get(order.client_order_id)).state.value == "FILLED"
    assert env.registry.get_active(SYM2, LONG) is None
    assert "成交明细还没入账" in result.steps["reconcile"]["deferred"][0]["why"]


# ================================================================ 登记簿有、交易所零：不凭一次零读数判已平


async def test_a_registered_position_that_is_flat_on_the_exchange_without_any_explaining_fill_is_left_alone(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1", avg="100", stop=STOP)
    env.positions = []  # 读成功、明确为零，但没有任何成交解释
    result = await env.run()
    assert env.registry.get_active(SYM, LONG).id == rec.id  # 一次零读数不判已平：交给持仓对账的连续 3 次零
    assert result.steps["reconcile"]["flat_unexplained"] == [
        {"position_id": rec.id, "symbol": SYM, "side": "LONG", "status": "managed"}]
    assert env.protection.rebuilt == []  # 数量为零的不补保护


# ================================================================ 已平记录遗留的保护单


async def test_closed_records_with_leftover_protection_are_cleaned_up(make_env):
    """中断点 5：平仓单已成交、保护单未清理。进程被杀后登记簿里是已平、止损编号还留着。"""
    env = await make_env().boot()
    rec = await env.add_position(stop=STOP)
    await env.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=env.clock.now_ms())
    env.positions = []
    result = await env.run()
    assert env.protection.cleaned == [rec.id]
    assert result.steps["reconcile"]["closed_cleanup"] == [rec.id]


async def test_closed_records_without_leftovers_are_not_touched(make_env):
    env = await make_env().boot()
    rec = await env.add_position()
    await env.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=env.clock.now_ms())
    await env.run()
    assert env.protection.cleaned == []


async def test_closed_records_with_stale_or_handed_over_stops_are_cleaned_too(make_env):
    env = await make_env().boot()
    stale = await env.add_position(symbol="SOLUSDT")
    await env.registry.add_stale_stop(stale.id, client_algo_id="DKT-0-9-s", algo_id=None)
    handed = await env.add_position(symbol="XRPUSDT")
    await env.registry.set_handed_over(handed.id, {"client_algo_id": "u1", "algo_id": "5"})
    for rec in (stale, handed):
        await env.registry.mark_closed(rec.id, exit_reason=ExitReason.EXTERNAL, closed_ms=env.clock.now_ms())
    await env.run()
    assert sorted(env.protection.cleaned) == sorted([stale.id, handed.id])


async def test_a_successful_run_does_the_full_position_reconcile_once(make_env):
    env = await make_env().boot()
    result = await env.run()
    assert env.calls_named("reconcile_positions") == ["reconcile_positions"]
    assert result.steps["reconcile"]["positions_reconcile"] == "ok"
    assert env.index_of("reconcile_positions") < env.index_of("reconcile_orders")  # 先对持仓（孤儿清理要新鲜的零读数）再对挂单


async def test_a_failure_to_record_the_kept_stop_does_not_stop_the_other_keys(make_env, monkeypatch):
    env = await make_env().boot()
    env.positions = [pos(), pos(SYM2, qty="2")]
    env.algos = [algo(SYM, LONG, cid=env.own_id(intent=88), algo_id="7001", trigger="90")]

    async def broken(*args, **kwargs):
        raise RuntimeError("账本写失败")

    monkeypatch.setattr(env.registry, "set_protection", broken)
    result = await env.run()
    assert env.registry.get_active(SYM, LONG).status is PositionStatus.PENDING  # 待确认仓照样登记了
    assert env.registry.get_active(SYM2, LONG).status is PositionStatus.PENDING  # 另一个键也照常处理
    assert len(result.steps["reconcile"]["pending_registered"]) == 2
    (failed,) = result.steps["reconcile"]["stop_note_failed"]
    assert failed["stop_client_id"] == env.own_id(intent=88) and "账本写失败" in failed["error"]
    assert result.tradable is False and any("账本写失败" in p for p in result.problems)
