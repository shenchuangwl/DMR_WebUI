"""复核补充：止损触发后的重开、回查期重发与订单事件的竞态、沿用原编号的重发次数、没发出去的订单行、碎仓条件单的回查。"""
from __future__ import annotations

from dmr_kernel.core.events import IntentStateChanged
from dmr_kernel.core.types import IntentState, Known, OrderPurpose, OrderState, RejectKind
from dmr_kernel.ledger import repo_orders

from coordinator_kit import (
    D, LONG, SYM, ack_ok, algo_for, algo_ok, close_spec, fill_ok, market_view_for, position, rejected, spec,
    stop_place_spec, stop_replace_spec, tick, unknown_write,
)

S = IntentState
TERMINAL = frozenset(S.terminal())


async def unknown_close(rig, qty: str = "0.01", **spec_kw):
    """操作员平仓，平仓单结果未知；之后交易所一侧「查不到」这张单。"""
    await rig.start()
    rig.positions = [position(SYM, LONG, qty)]
    rig.adapter.push_write_result("create_market_order", unknown_write("请求超时"))
    rec = await rig.go(close_spec(**spec_kw))
    assert rec.state is S.RECONCILING
    rig.adapter.set_read("fetch_order", Known(None))
    return rec


def drain_before_read(rig, name: str, armed) -> None:
    """读方法返回之前先让总线把排队的事件处理完：把「订单事件的处理」精确地插进派发者读交易所的那个空当。"""
    original = getattr(rig.adapter, name)

    async def wrapped(*args, **kwargs):
        if armed():
            await rig.bus.drain(2.0)
        return await original(*args, **kwargs)

    setattr(rig.adapter, name, wrapped)


def record_state_at_each_send(rig, rec_id: int, names: tuple[str, ...]) -> list[IntentState]:
    """每次向交易所发单的那一刻，记下意图在账本里的状态。"""
    states: list[IntentState] = []

    async def hook(name, kw):
        if name in names:
            states.append((await rig.intents.get(rec_id)).state)

    rig.adapter.hook = hook
    return states


# ================================================================ 止损触发：入账侧把已完成的止损意图重开


async def stop_placed(rig, intent_spec):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_stop_order", algo_ok())
    rec = await rig.go(intent_spec)
    assert rec.state is S.COMPLETED
    stops = [o for o in await rig.order_rows(rec.id) if o.purpose is OrderPurpose.STOP]
    return rec, stops[-1]


async def test_a_stop_intent_reopened_by_the_fill_of_its_triggered_stop_is_completed_again(rig):
    """系统止损触发后成交带着止损单的意图号入账，入账侧把已完成的 STOP_PLACE 重开成 ACTIVE。
    止损单已经触发并成交，意图不能卡在 ACTIVE：否则这个「合约 + 方向」永远算有在途的系统动作。"""
    rec, stop = await stop_placed(rig, stop_place_spec())
    await rig.orders.apply_exchange_view(stop.client_order_id, algo_for(stop, raw="FINISHED"), source="stream")
    await rig.intents.reopen_for_late_fill(rec.id, reason="晚到成交：止损触发后的成交")
    await rig.settle()
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "触发" in done.state_reason
    assert not rig.coordinator.key_busy(SYM, LONG)
    states = [e.state for e in rig.events if isinstance(e, IntentStateChanged) and e.intent_id == rec.id]
    assert states[-3:] == ["COMPLETED", "ACTIVE", "COMPLETED"]


async def test_a_replaced_stop_that_triggers_later_does_not_leave_its_intent_active(rig):
    rec, stop = await stop_placed(rig, stop_replace_spec(mode="gap"))
    result = rec.params["result"]
    await rig.orders.apply_exchange_view(stop.client_order_id, algo_for(stop, raw="FINISHED"), source="stream")
    await rig.intents.reopen_for_late_fill(rec.id, reason="晚到成交：止损触发后的成交")
    await rig.settle()
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and not rig.coordinator.key_busy(SYM, LONG)
    assert done.params["result"] == result  # 结果不被改写


async def test_a_reopened_stop_intent_whose_stop_has_not_filled_stays_open(rig):
    """反例：止损单没有成交（还挂着）却被重开：这是要人核对的异常，协调者不替人收尾。"""
    rec, _ = await stop_placed(rig, stop_place_spec())
    await rig.intents.reopen_for_late_fill(rec.id, reason="晚到成交：来历不明")
    await rig.settle()
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE and rig.coordinator.key_busy(SYM, LONG)


# ================================================================ 回查期的重发：订单事件不能抢先结束意图


async def test_an_order_event_cannot_end_the_intent_while_the_resend_after_reduce_rejected_is_on_its_way(rig):
    """回查期重发的平仓单被 −2022 拒绝、重读持仓变小：派发者要按新读数再发一次。这期间订单事件的处理不能先把意图
    置为 REJECTED——否则下一张单是替一个已经结束的意图发出去的（成交后再被当成晚到成交）。"""
    rec = await unknown_close(rig)
    states = record_state_at_each_send(rig, rec.id, ("create_market_order",))
    original_hook = rig.adapter.hook

    async def hook(name, kw):
        await original_hook(name, kw)
        if name == "create_market_order" and len(states) == 1:
            rig.positions = [position(SYM, LONG, "0.006")]  # 交易所答复超出持仓：持仓其实已经变小

    rig.adapter.hook = hook
    drain_before_read(rig, "fetch_positions", lambda: len(states) >= 1)
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.REDUCE_REJECTED, -2022, "超出持仓"))
    rig.adapter.push_write_result("create_market_order", fill_ok())
    for _ in range(3):
        await tick(rig)
    assert [c["qty"] for c in rig.sent("create_market_order")] == [D("0.01"), D("0.01"), D("0.006")]
    assert len(states) == 2 and not set(states) & TERMINAL, states  # 每张单发出时意图都还没结束
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED


async def test_an_order_event_cannot_end_the_intent_while_the_dust_fallback_after_4164_is_on_its_way(rig):
    rec = await unknown_close(rig)
    states = record_state_at_each_send(rig, rec.id, ("create_market_order", "create_stop_order"))
    drain_before_read(rig, "fetch_tickers", lambda: len(states) >= 1)
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.MIN_NOTIONAL, -4164, "名义金额太小"))
    rig.adapter.push_write_result("create_stop_order", algo_ok())
    for _ in range(3):
        await tick(rig)
    assert len(rig.sent("create_market_order")) == 2 and len(rig.sent("create_stop_order")) == 1
    assert len(states) == 2 and not set(states) & TERMINAL, states
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE  # 碎仓条件单挂着等触发


async def test_an_active_intent_whose_order_filled_without_an_event_is_completed_by_the_periodic_check(rig):
    """兜底：订单已经成交但协调者没收到事件（事件处理出错等），回查时补上「完成」。"""
    await rig.start()
    rig.adapter.push_write_result("create_market_order", ack_ok())
    rec = await rig.go(spec())
    assert rec.state is S.ACTIVE
    order = await rig.only_order(rec.id)
    await rig.ledger.write(repo_orders.merge_view, order.client_order_id, market_view_for(order),
                           now_ms=rig.clock.now_ms())  # 直接写账本：不发事件
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED
    assert not rig.coordinator.key_busy(SYM, LONG)


# ================================================================ 沿用原编号重发（ex_resend_client_id=same）


async def test_same_id_resends_are_capped_and_each_waits_for_fresh_absences_and_the_signature_window(make_rig):
    rig = make_rig(ex_resend_client_id="same")
    rec = await unknown_close(rig)
    for _ in range(3):
        rig.adapter.push_write_result("create_market_order", unknown_write())
    for _ in range(3):
        await tick(rig)
    assert len(rig.sent("create_market_order")) == 2  # 第一次重发
    await tick(rig)
    # 重发之前攒下的「不存在」不能再算：重发之后要重新连续 3 次不存在、并且重发已超过签名时间窗
    assert len(rig.sent("create_market_order")) == 2
    await tick(rig)
    assert len(rig.sent("create_market_order")) == 2
    await tick(rig)
    assert len(rig.sent("create_market_order")) == 3  # 第二次重发
    for _ in range(10):
        await tick(rig)
    calls = rig.sent("create_market_order")
    assert len(calls) == 3 and len({c["client_order_id"] for c in calls}) == 1  # 原单加两次重发，同一个编号
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "回查" in done.state_reason
    assert (await rig.only_order(rec.id)).state is OrderState.REJECTED


async def test_a_same_id_resend_inside_the_signature_window_is_not_judged_absent(make_rig):
    """重发后很快就有三次「不存在」（例如回查很频繁），但重发还没超过签名时间窗：不能下结论。"""
    rig = make_rig(ex_resend_client_id="same")
    rec = await unknown_close(rig)
    rig.adapter.push_write_result("create_market_order", unknown_write())
    for _ in range(3):
        await tick(rig)
    assert len(rig.sent("create_market_order")) == 2
    for _ in range(4):
        await tick(rig, 1_000)  # 重发后 4 秒内又查了 4 次
    assert len(rig.sent("create_market_order")) == 2
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


# ================================================================ 没发出去的订单行不能一直挂着


async def test_an_order_row_created_for_an_intent_that_expired_before_sending_is_closed(rig):
    """订单行刚落盘、请求还没发，意图已被定时任务置为过期：请求不能发，订单行也不能一直是 PENDING_SEND
    （否则一直被当成「在途的系统订单」反复回查）。"""
    await rig.start()
    original = rig.riskgate.evaluate

    async def evaluate(**kw):
        rig.clock.advance(61_000)  # 有效期 60 秒
        assert await rig.intents.expire_due(rig.clock.now_ms())
        return await original(**kw)

    rig.riskgate.evaluate = evaluate
    rec = await rig.go(spec())
    assert rec.state is S.EXPIRED
    assert rig.sent("create_market_order") == []
    row = await rig.only_order(rec.id)  # 过期发生在订单行落盘之前一刻：订单行已经建了
    assert row.state is OrderState.REJECTED and row.sent_ms is None and "未发出" in row.error_msg


async def test_an_order_row_left_behind_by_an_internal_error_before_sending_is_closed(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]

    async def broken(cid):
        raise RuntimeError("账本忙")

    rig.orders.record_sent = broken
    rec = await rig.go(close_spec())
    assert rec.state is S.REJECTED and "内部错误" in rec.state_reason
    assert rig.sent("create_market_order") == []
    row = await rig.only_order(rec.id)
    assert row.state is OrderState.REJECTED and row.sent_ms is None


async def test_an_order_that_was_sent_is_never_closed_by_the_cleanup(rig):
    """反例：请求已经发出（发出时间已记）的订单，意图出了内部错误也只转回查，订单行不被改成 REJECTED。"""
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    original = rig.orders.apply_write_result

    async def broken(cid, result):
        raise RuntimeError("账本忙")

    rig.orders.apply_write_result = broken
    rec = await rig.go(close_spec())
    rig.orders.apply_write_result = original
    assert rec.state is S.RECONCILING
    row = await rig.only_order(rec.id)
    assert row.state is OrderState.PENDING_SEND and row.sent_ms is not None


# ================================================================ 碎仓条件单结果未知


async def unknown_dust_close(rig, **spec_kw):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.001")]  # 名义金额 50 < 最小名义金额 100
    rig.adapter.push_write_result("create_stop_order", unknown_write())
    rec = await rig.go(close_spec(**spec_kw))
    assert rec.state is S.RECONCILING
    rig.adapter.set_read("fetch_algo_order", Known(None))
    return rec


async def test_an_unknown_dust_condition_the_exchange_never_saw_is_placed_again_under_a_new_id(rig):
    rec = await unknown_dust_close(rig)
    rig.adapter.push_write_result("create_stop_order", algo_ok())
    for _ in range(3):
        await tick(rig)
    first, second = rig.sent("create_stop_order")
    assert first["client_algo_id"] != second["client_algo_id"]
    rows = await rig.order_rows(rec.id)
    assert [(r.purpose, r.leg, r.state) for r in rows] == [
        (OrderPurpose.DUST_CLOSE, "d", OrderState.REJECTED), (OrderPurpose.DUST_CLOSE, "d1", OrderState.ACKNOWLEDGED)]
    assert rows[1].resend_of == rows[0].client_order_id
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE


async def test_a_changed_position_keeps_an_unknown_dust_condition_unresolved(rig):
    rec = await unknown_dust_close(rig)
    await tick(rig)
    await tick(rig)
    rig.positions = [position(SYM, LONG, "0.0015")]
    for _ in range(3):
        await tick(rig)
    assert len(rig.sent("create_stop_order")) == 1
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_an_unknown_dust_condition_that_must_not_be_resent_is_rejected(rig):
    rec = await unknown_dust_close(rig, params={"resend_on_unknown": False})
    for _ in range(3):
        await tick(rig)
    assert len(rig.sent("create_stop_order")) == 1
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "回查" in done.state_reason


# ================================================================ 适配层在发单途中抛异常


async def test_an_adapter_exception_during_the_send_is_an_unknown_result_and_releases_the_key(rig):
    """适配层抛出意外异常：请求到没到交易所不明，按未知处理（不重发、名额不放），同时键锁必须放开——同键随后的
    保护类意图照常执行。"""
    await rig.start()

    async def hook(name, kw):
        if name == "create_market_order":
            raise ConnectionResetError("连接被重置")

    rig.adapter.hook = hook
    rec = await rig.go(spec())
    assert rec.state is S.RECONCILING
    assert (await rig.only_order(rec.id)).state is OrderState.SENT_UNKNOWN
    assert any(a["kind"] == "coordinator_internal_error" and "不会重发" in a["message"] for a in rig.notifier.alerts)
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 1  # 订单可能存在：名额不放
    assert rig.coordinator.key_busy(SYM, LONG)
    rig.adapter.hook = None
    stop = await rig.go(stop_place_spec())
    assert stop.state is S.COMPLETED  # 键锁已放开
    assert rig.sent("create_market_order") == []  # 抛异常的那次没被假适配层记下；之后也没有重发
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING
