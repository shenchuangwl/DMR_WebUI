"""完成判定：订阅订单状态变化，决定意图是完成还是放弃（规格「完成判定」一段）。"""
from __future__ import annotations

from dmr_kernel.core.events import IntentStateChanged
from dmr_kernel.core.types import IntentState, Known, OrderState, RejectKind

from coordinator_kit import (
    D, LONG, SYM, ack_ok, close_spec, fill_ok, market_view_for, order_view, partial_ok, position, rejected, spec, tick,
    unknown_write,
)

S = IntentState


async def working(rig, **spec_kw):
    rig.adapter.push_write_result("create_market_order", ack_ok())
    rec = await rig.go(spec(**spec_kw))
    assert rec.state is S.ACTIVE
    return rec, await rig.only_order(rec.id)


async def stream(rig, order, view):
    await rig.orders.apply_exchange_view(order.client_order_id, view, source="stream")
    await rig.settle()


async def test_a_fully_filled_order_completes_the_intent_and_frees_the_slot(rig):
    await rig.start()
    rec, order = await working(rig)
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 1
    await stream(rig, order, market_view_for(order))
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "已全部成交" in done.state_reason and "0.002/0.002" in done.state_reason
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 0


async def test_a_partial_fill_keeps_the_intent_active_until_the_order_ends(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", partial_ok("0.5"))
    rec = await rig.go(spec())
    assert rec.state is S.ACTIVE
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.PARTIALLY_FILLED
    await stream(rig, order, market_view_for(order, status="PARTIALLY_FILLED", filled=D("0.0015")))
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE
    await stream(rig, order, market_view_for(order))  # 其余成交到达
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED


async def test_an_order_voided_by_the_exchange_after_a_partial_fill_completes_with_a_note(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", partial_ok("0.5"))
    rec = await rig.go(spec())
    order = await rig.only_order(rec.id)
    await stream(rig, order, market_view_for(order, status="EXPIRED", filled=D("0.001")))  # 市价单的余量被作废
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "部分成交" in done.state_reason and "0.001/0.002" in done.state_reason


async def test_an_order_voided_without_any_fill_rejects_the_intent(rig):
    await rig.start()
    rec, order = await working(rig)
    await stream(rig, order, market_view_for(order, status="EXPIRED", filled=D("0"), avg=None))
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "没有成交" in done.state_reason
    assert not rig.coordinator.key_busy(SYM, LONG)


async def test_an_order_rejected_later_by_the_exchange_rejects_the_intent(rig):
    await rig.start()
    rec, order = await working(rig)
    rejected_view = order_view({"symbol": SYM, "client_order_id": order.client_order_id, "side": order.side,
                                "position_side": order.position_side, "qty": order.qty}, status="REJECTED", avg=None)
    await stream(rig, order, rejected_view)
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "订单被拒绝" in done.state_reason


async def test_an_unknown_order_found_open_moves_the_intent_back_to_active(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(spec())
    assert rec.state is S.RECONCILING
    order = await rig.only_order(rec.id)
    rig.adapter.set_read("fetch_order", Known(market_view_for(order, status="NEW", filled=D("0"), avg=None)))
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE  # 确认存在了，只是还没成交
    await stream(rig, order, market_view_for(order))
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED


async def test_the_intent_ends_exactly_once_however_many_events_arrive(rig):
    await rig.start()
    rec, order = await working(rig)
    await stream(rig, order, market_view_for(order))
    await stream(rig, order, market_view_for(order))  # 重复的回报
    await tick(rig)
    ends = [e for e in rig.events if isinstance(e, IntentStateChanged) and e.intent_id == rec.id
            and e.state in ("COMPLETED", "REJECTED")]
    assert [e.state for e in ends] == ["COMPLETED"]


async def test_events_of_other_intents_and_orders_without_an_intent_are_ignored(rig):
    await rig.start()
    rec, order = await working(rig)
    other = await rig.go(spec(symbol="ETHUSDT"))
    other_order = await rig.only_order(other.id)
    await stream(rig, other_order, market_view_for(other_order))
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE  # 别的意图的成交不影响这一个
    assert (await rig.intents.get(other.id)).state is S.COMPLETED


async def test_a_close_completes_when_its_order_fills(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", ack_ok())
    rec = await rig.go(close_spec())
    order = await rig.only_order(rec.id)
    await stream(rig, order, market_view_for(order))
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "已全部成交" in done.state_reason


# ================================================================ 晚到成交重开意图之后

async def test_an_intent_reopened_by_a_late_fill_is_completed_again_when_its_order_is_filled(rig):
    """订单在 REST 应答里就是 FILLED 时意图先完成，成交回报随后才入账：入账侧会把终态意图重开成 ACTIVE。
    订单明明已经全部成交，意图不能就此卡在 ACTIVE（键一直忙）。"""
    await rig.start()
    rig.adapter.push_write_result("create_market_order", fill_ok())
    rec = await rig.go(spec())
    assert rec.state is S.COMPLETED
    await rig.intents.reopen_for_late_fill(rec.id, reason="晚到成交 t1")
    await rig.settle()
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "已全部成交" in done.state_reason
    assert not rig.coordinator.key_busy(SYM, LONG)
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 0
    states = [e.state for e in rig.events if isinstance(e, IntentStateChanged) and e.intent_id == rec.id]
    assert states[-3:] == ["COMPLETED", "ACTIVE", "COMPLETED"]


async def test_a_reopened_intent_is_never_rejected_again_behind_the_operators_back(rig):
    """被拒绝的意图因为晚到成交重开：重开本身就是要人去核对的异常，协调者不替人再把它拒绝掉，也不来回翻。"""
    await rig.start()
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.OTHER, -1111, "拒绝"))
    rec = await rig.go(spec())
    assert rec.state is S.REJECTED
    await rig.intents.reopen_for_late_fill(rec.id, reason="晚到成交 t2")
    await rig.settle()
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE
    states = [e.state for e in rig.events if isinstance(e, IntentStateChanged) and e.intent_id == rec.id]
    assert states[-2:] == ["REJECTED", "ACTIVE"]
    assert rig.coordinator.key_busy(SYM, LONG)  # 还没处理完：键仍算有在途

