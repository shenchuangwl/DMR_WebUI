"""平仓意图（F12、F5 第 5–7 步、F2 第 3、4 步的市价平仓）：每个分支。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.events import IntentStateChanged
from dmr_kernel.core.types import (
    AlgoType,
    IntentSource,
    IntentState,
    Known,
    OrderPurpose,
    OrderState,
    RejectKind,
    Unknown,
    WorkingType,
)
from dmr_kernel.exchange import ids

from coordinator_kit import (
    BUY, D, LONG, SELL, SHORT, SYM, ack_ok, algo_for, algo_ok, close_spec, fill_ok, position, rejected, spec, tick,
    unknown_write,
)

S = IntentState


# ================================================================ 正常路径


@pytest.mark.parametrize("side, closing_side", [(LONG, SELL), (SHORT, BUY)])
async def test_close_sends_the_opposite_side_with_the_position_side_and_the_live_quantity(rig, side, closing_side):
    await rig.start()
    rig.positions = [position(SYM, side, "0.012")]
    rig.adapter.push_write_result("create_market_order", fill_ok())
    rec = await rig.go(close_spec(side=side))
    assert rec.state is S.COMPLETED
    call = rig.sent("create_market_order")[0]
    # 只有这五个参数：不带只减仓标记，对冲模式直接传持仓方向
    assert set(call) == {"symbol", "side", "position_side", "qty", "client_order_id"}
    assert (call["symbol"], call["side"], call["position_side"], call["qty"]) == (SYM, closing_side, side, D("0.012"))
    parsed = ids.parse_client_id(call["client_order_id"])
    assert parsed.model_code == "0" and parsed.leg == "c" and parsed.intent_id == rec.id
    order = await rig.only_order(rec.id)
    assert order.purpose is OrderPurpose.CLOSE and order.qty == D("0.012") and order.state is OrderState.FILLED


async def test_close_takes_the_quantity_from_the_exchange_not_from_the_spec(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.0071")]
    await rig.go(close_spec(qty=D("5")))  # 规格里的数量不起作用
    assert rig.sent("create_market_order")[0]["qty"] == D("0.0071")


async def test_the_close_walks_the_state_machine_in_order(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", fill_ok())
    rec = await rig.go(close_spec())
    states = [e.state for e in rig.events if isinstance(e, IntentStateChanged) and e.intent_id == rec.id]
    assert states == ["CREATED", "VALIDATED", "DISPATCH_READY", "ACTIVE", "COMPLETED"]  # 平仓不占名额


async def test_a_close_that_is_accepted_but_not_yet_filled_stays_active_until_the_fill(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", ack_ok())
    rec = await rig.go(close_spec())
    assert rec.state is S.ACTIVE and rig.coordinator.key_busy(SYM, LONG)
    order = await rig.only_order(rec.id)
    from coordinator_kit import market_view_for

    await rig.orders.apply_exchange_view(order.client_order_id, market_view_for(order), source="stream")
    await rig.settle()
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED
    assert not rig.coordinator.key_busy(SYM, LONG)


# ================================================================ 即时持仓


async def test_a_close_with_no_position_is_completed_as_closed_elsewhere_and_sends_nothing(rig):
    await rig.start()
    rec = await rig.go(close_spec())
    assert rec.state is S.COMPLETED and "已由其它途径平掉" in rec.state_reason
    assert rig.names() == [] and await rig.order_rows(rec.id) == []


async def test_a_zero_quantity_row_or_the_other_side_is_not_a_position_to_close(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0"), position(SYM, SHORT, "0.01"), position("ETHUSDT", LONG, "1", "3000")]
    rec = await rig.go(close_spec(side=LONG))
    assert rec.state is S.COMPLETED and rig.names() == []


async def test_an_operator_close_fails_when_the_position_cannot_be_read(rig):
    await rig.start()
    rig.adapter.set_read("fetch_positions", Unknown("连接超时"))
    rec = await rig.go(close_spec())
    assert rec.state is S.REJECTED and "持仓读数未知" in rec.state_reason and "连接超时" in rec.state_reason
    assert rig.names() == []


async def test_a_signal_close_that_cannot_read_the_position_waits_and_retries_later(rig):
    await rig.start()
    rig.adapter.set_read("fetch_positions", Unknown("限频"))
    rec = await rig.go(close_spec(source=IntentSource.SIGNAL, params={"retry_until_done": True}))
    assert rec.state is S.RECONCILING and "稍后重试" in rec.state_reason
    assert rig.names() == [] and await rig.order_rows(rec.id) == []
    assert rig.coordinator.key_busy(SYM, LONG)  # 意图保持有效
    await tick(rig)
    await tick(rig)  # 仍然读不到：几轮都不发单，也不下结论
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING and rig.names() == []
    rig.wire()  # 读得到了
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", fill_ok())
    await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED
    assert len(rig.sent("create_market_order")) == 1 and rig.sent("create_market_order")[0]["qty"] == D("0.01")
    assert not rig.coordinator.key_busy(SYM, LONG)


async def test_a_deferred_close_is_completed_when_the_position_turns_out_to_be_gone(rig):
    await rig.start()
    rig.adapter.set_read("fetch_positions", Unknown("限频"))
    rec = await rig.go(close_spec(params={"retry_until_done": True}))
    rig.wire()  # 读到了：仓位已经没了（比如交易所的止损先触发）
    await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "已由其它途径平掉" in done.state_reason and rig.names() == []


async def test_a_deferred_close_is_never_dispatched_twice_at_once(rig):
    await rig.start()
    rig.adapter.set_read("fetch_positions", Unknown("限频"))
    rec = await rig.go(close_spec(params={"retry_until_done": True}))
    rig.wire()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", ack_ok())
    await tick(rig)
    await tick(rig)
    assert len(rig.sent("create_market_order")) == 1  # 第二轮看到的是 ACTIVE，不再派发
    assert len(await rig.order_rows(rec.id)) == 1


# ================================================================ 碎仓（F12：改用全平型止盈条件单）


async def test_a_dust_long_is_closed_with_a_take_profit_condition_one_tick_above_the_price(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.001")]  # 名义金额 50 < 最小名义金额 100
    rig.adapter.push_write_result("create_stop_order", algo_ok())
    rec = await rig.go(close_spec())
    assert rig.sent("create_market_order") == []
    call = rig.sent("create_stop_order")[0]
    assert call["algo_type"] is AlgoType.TAKE_PROFIT_MARKET and call["close_position"] is True and call["qty"] is None
    assert (call["side"], call["position_side"]) == (SELL, LONG)
    assert call["trigger_price"] == D("50000.1") and call["working_type"] is WorkingType.CONTRACT_PRICE
    parsed = ids.parse_client_id(call["client_algo_id"])
    assert parsed.leg == "d" and parsed.intent_id == rec.id
    order = await rig.only_order(rec.id)
    assert order.purpose is OrderPurpose.DUST_CLOSE and order.kind == "algo" and order.state is OrderState.ACKNOWLEDGED
    assert rec.state is S.ACTIVE and rig.coordinator.key_busy(SYM, LONG)  # 等条件单触发
    # 条件单触发并完成（数据流）
    await rig.orders.apply_exchange_view(order.client_order_id, algo_for(order, raw="FINISHED"), source="stream")
    await rig.settle()
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "碎仓条件单" in done.state_reason


async def test_a_dust_short_is_closed_one_tick_below_the_price(rig):
    await rig.start()
    rig.positions = [position(SYM, SHORT, "0.001")]
    rec = await rig.go(close_spec(side=SHORT))
    call = rig.sent("create_stop_order")[0]
    assert (call["side"], call["position_side"]) == (BUY, SHORT) and call["trigger_price"] == D("49999.9")
    assert rec.state is S.ACTIVE


async def test_the_dust_trigger_stays_on_the_correct_side_when_the_price_is_between_ticks(rig):
    await rig.start()
    rig.prices["ETHUSDT"] = D("3000.005")  # 价格步长 0.01，最小名义金额 20：0.005 × 3000 = 15
    rig.positions = [position("ETHUSDT", LONG, "0.005", "3000"), position("ETHUSDT", SHORT, "0.005", "3000")]
    await rig.go_all(close_spec(symbol="ETHUSDT", side=LONG), close_spec(symbol="ETHUSDT", side=SHORT))
    triggers = {c["position_side"]: c["trigger_price"] for c in rig.sent("create_stop_order")}
    assert triggers[LONG] == D("3000.01") and triggers[SHORT] == D("3000.00")  # 多仓在价上方，空仓在价下方


async def test_a_position_exactly_at_the_minimum_notional_is_closed_with_a_market_order(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.002")]  # 0.002 × 50000 = 100，不低于最小名义金额
    await rig.go(close_spec())
    assert len(rig.sent("create_market_order")) == 1 and rig.sent("create_stop_order") == []


async def test_the_exemption_switch_closes_a_dust_position_with_a_market_order(make_rig):
    rig = make_rig(ex_close_min_notional_exempt=True)
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.001")]
    await rig.go(close_spec())
    assert len(rig.sent("create_market_order")) == 1 and rig.sent("create_stop_order") == []


async def test_without_a_last_price_the_dust_check_is_skipped_and_the_market_order_goes_out(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.001")]
    rig.adapter.set_read("fetch_tickers", Unknown("限频"))
    rec = await rig.go(close_spec())
    assert len(rig.sent("create_market_order")) == 1 and rec.state is S.ACTIVE


async def test_a_min_notional_rejection_switches_to_the_dust_path(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]  # 看起来不是碎仓，但交易所拒绝了
    rig.adapter.push_write_result("create_market_order",
                                  rejected(RejectKind.MIN_NOTIONAL, -4164, "Order's notional must be no smaller than 100"))
    rig.adapter.push_write_result("create_stop_order", algo_ok())
    rec = await rig.go(close_spec())
    assert rig.names() == ["create_market_order", "create_stop_order"]
    rows = await rig.order_rows(rec.id)
    assert [(r.leg, r.state) for r in rows] == [("c", OrderState.REJECTED), ("d", OrderState.ACKNOWLEDGED)]
    assert rows[0].error_code == -4164 and rows[1].resend_of == rows[0].client_order_id
    assert rec.state is S.ACTIVE


@pytest.mark.parametrize("params, expected", [({}, S.REJECTED), ({"retry_until_done": True}, S.RECONCILING)])
async def test_the_dust_path_needs_the_price_operators_fail_and_protective_closes_wait(rig, params, expected):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.MIN_NOTIONAL, -4164, "太小"))

    async def hook(name, kw):
        rig.adapter.set_read("fetch_tickers", Unknown("限频"))

    rig.adapter.hook = hook
    rec = await rig.go(close_spec(params=params))
    assert rec.state is expected and "碎仓" in rec.state_reason
    assert rig.sent("create_stop_order") == []


async def test_a_rejected_dust_condition_rejects_the_intent(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.001")]
    rig.adapter.push_write_result("create_stop_order", rejected(RejectKind.IMMEDIATE_TRIGGER, -2021, "会立即触发"))
    rec = await rig.go(close_spec())
    assert rec.state is S.REJECTED and "碎仓条件单" in rec.state_reason and "2021" in rec.state_reason


async def test_an_unknown_dust_condition_result_is_reconciled_by_looking_for_the_algo_order(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.001")]
    rig.adapter.push_write_result("create_stop_order", unknown_write())
    rec = await rig.go(close_spec())
    assert rec.state is S.RECONCILING
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.SENT_UNKNOWN
    rig.adapter.set_read("fetch_algo_order", Known(algo_for(order)))
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE  # 条件单存在，等触发
    assert len(rig.sent("create_stop_order")) == 1


async def test_a_resting_dust_condition_is_polled_when_the_stream_is_silent(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.001")]
    rec = await rig.go(close_spec())
    order = await rig.only_order(rec.id)
    rig.adapter.set_read("fetch_algo_order", Known(algo_for(order, raw="NEW")))
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE  # 还没触发
    rig.adapter.set_read("fetch_algo_order", Known(algo_for(order, raw="FINISHED")))
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED


async def test_a_dust_condition_that_is_cancelled_or_expired_rejects_the_intent(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.001")]
    rec = await rig.go(close_spec())
    order = await rig.only_order(rec.id)
    await rig.orders.apply_exchange_view(order.client_order_id, algo_for(order, raw="EXPIRED"), source="stream")
    await rig.settle()
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "碎仓条件单" in done.state_reason


# ================================================================ −2022（无仓可平或数量超出）


def reduce_rejected():
    return rejected(RejectKind.REDUCE_REJECTED, -2022, "ReduceOnly Order is rejected")


async def test_reduce_rejected_with_a_zero_reread_means_the_position_was_closed_elsewhere(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]

    async def hook(name, kw):
        rig.positions = []  # 交易所一侧的止损刚好先触发了

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_market_order", reduce_rejected())
    rec = await rig.go(close_spec())
    assert rec.state is S.COMPLETED and "已由其它途径平掉" in rec.state_reason
    assert len(rig.sent("create_market_order")) == 1
    assert (await rig.only_order(rec.id)).error_code == -2022


async def test_reduce_rejected_because_the_quantity_exceeds_the_position_resends_once_with_the_new_reading(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]

    async def hook(name, kw):
        rig.positions = [position(SYM, LONG, "0.006")]  # 请求在路上时仓位变小了

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_market_order", reduce_rejected())
    rig.adapter.push_write_result("create_market_order", fill_ok())
    rec = await rig.go(close_spec())
    assert rec.state is S.COMPLETED
    first, second = rig.sent("create_market_order")
    assert first["qty"] == D("0.01") and second["qty"] == D("0.006")
    assert ids.parse_client_id(second["client_order_id"]).leg == "c1"
    rows = await rig.order_rows(rec.id)
    assert [r.state for r in rows] == [OrderState.REJECTED, OrderState.FILLED]
    assert rows[1].resend_of == rows[0].client_order_id


async def test_reduce_rejected_with_an_unchanged_position_is_a_failure(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", reduce_rejected())
    rec = await rig.go(close_spec())
    assert rec.state is S.REJECTED and "不是数量超出" in rec.state_reason and "2022" in rec.state_reason
    assert len(rig.sent("create_market_order")) == 1


async def test_the_quantity_resend_after_reduce_rejected_happens_only_once(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    quantities = iter(["0.006", "0.003"])

    async def hook(name, kw):
        rig.positions = [position(SYM, LONG, next(quantities))]

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_market_order", reduce_rejected())
    rig.adapter.push_write_result("create_market_order", reduce_rejected())
    rec = await rig.go(close_spec())
    assert rec.state is S.REJECTED and len(rig.sent("create_market_order")) == 2


async def test_reduce_rejected_with_an_unreadable_position_fails_for_operators(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]

    async def hook(name, kw):
        rig.adapter.set_read("fetch_positions", Unknown("断连"))

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_market_order", reduce_rejected())
    rec = await rig.go(close_spec())
    assert rec.state is S.REJECTED and "重读持仓失败" in rec.state_reason and "断连" in rec.state_reason


async def test_reduce_rejected_with_an_unreadable_position_waits_for_a_protective_close(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]

    async def hook(name, kw):
        rig.adapter.set_read("fetch_positions", Unknown("断连"))

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_market_order", reduce_rejected())
    rec = await rig.go(close_spec(params={"retry_until_done": True}))
    assert rec.state is S.RECONCILING and "稍后重试" in rec.state_reason
    rig.adapter.hook = None
    rig.wire()
    rig.positions = []  # 之后读到：仓位已经没了
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED


async def test_another_rejection_fails_the_intent_with_the_error_code(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.OTHER, -1111, "精度错误"))
    rec = await rig.go(close_spec())
    assert rec.state is S.REJECTED
    assert "交易所拒绝平仓单" in rec.state_reason and "精度错误" in rec.state_reason and "-1111" in rec.state_reason
    assert not rig.coordinator.key_busy(SYM, LONG)


# ================================================================ 结果未知：回查与重发（F5 第 7 步）


async def unknown_close(rig, **spec_kw):
    """发出一张平仓单，结果未知；之后交易所一侧「查不到」这张单。"""
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", unknown_write("请求超时"))
    rec = await rig.go(close_spec(**spec_kw))
    assert rec.state is S.RECONCILING
    assert (await rig.only_order(rec.id)).state is OrderState.SENT_UNKNOWN
    rig.adapter.set_read("fetch_order", Known(None))
    return rec


async def test_an_unknown_close_is_not_resent_before_three_absences(rig):
    rec = await unknown_close(rig)
    await tick(rig)
    await tick(rig)
    assert len(rig.sent("create_market_order")) == 1
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING and rig.coordinator.key_busy(SYM, LONG)


async def test_after_three_absences_and_an_unchanged_position_the_close_is_resent_under_a_new_id(rig):
    rec = await unknown_close(rig)
    rig.adapter.push_write_result("create_market_order", fill_ok())
    for _ in range(3):
        await tick(rig)
    first, second = rig.sent("create_market_order")
    assert first["client_order_id"] != second["client_order_id"]
    assert ids.parse_client_id(first["client_order_id"]).leg == "c"
    assert ids.parse_client_id(second["client_order_id"]).leg == "c1"
    assert second["qty"] == D("0.01") and second["side"] == SELL and second["position_side"] == LONG
    rows = await rig.order_rows(rec.id)
    assert [r.state for r in rows] == [OrderState.REJECTED, OrderState.FILLED]
    assert "换新编号" in rows[0].error_msg and rows[1].resend_of == rows[0].client_order_id
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED


async def test_the_resend_waits_for_the_signature_window(rig):
    rec = await unknown_close(rig)
    for _ in range(5):  # 每轮 1 秒：第 3 轮就有 3 次不存在，但请求发出还不到 5 秒
        await tick(rig, 1_000)
    assert len(rig.sent("create_market_order")) == 1  # 恰好 5 秒不算超过
    await tick(rig, 1_000)
    assert len(rig.sent("create_market_order")) == 2


async def test_a_changed_position_blocks_the_resend_until_it_is_unchanged_again(rig):
    rec = await unknown_close(rig)
    rig.adapter.push_write_result("create_market_order", fill_ok())
    await tick(rig)
    await tick(rig)
    rig.positions = [position(SYM, LONG, "0.007")]  # 第三次回查时持仓已经变了
    await tick(rig)
    await tick(rig)
    assert len(rig.sent("create_market_order")) == 1  # 持仓变了，不重发
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING
    rig.positions = [position(SYM, LONG, "0.01")]
    await tick(rig)
    assert len(rig.sent("create_market_order")) == 2


async def test_a_zero_position_after_three_absences_completes_instead_of_resending(rig):
    rec = await unknown_close(rig)
    await tick(rig)
    await tick(rig)
    rig.positions = []
    await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "已由其它途径平掉" in done.state_reason
    assert len(rig.sent("create_market_order")) == 1
    assert (await rig.only_order(rec.id)).state is OrderState.REJECTED


async def test_unreadable_reads_never_lead_to_a_resend(rig):
    rec = await unknown_close(rig)
    rig.adapter.set_read("fetch_order", Unknown("查询超时"))
    for _ in range(4):
        await tick(rig)
    assert len(rig.sent("create_market_order")) == 1
    rig.adapter.set_read("fetch_order", Known(None))
    rig.adapter.set_read("fetch_positions", Unknown("限频"))  # 持仓读不到：也不下结论
    for _ in range(4):
        await tick(rig)
    assert len(rig.sent("create_market_order")) == 1
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_a_close_that_must_not_be_resent_is_rejected_after_the_same_conditions(rig):
    rec = await unknown_close(rig, params={"resend_on_unknown": False})
    for _ in range(3):
        await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "回查" in done.state_reason
    assert len(rig.sent("create_market_order")) == 1


async def test_an_unknown_close_that_turns_out_filled_is_completed_without_a_resend(rig):
    rec = await unknown_close(rig)
    from coordinator_kit import market_view_for

    order = await rig.only_order(rec.id)
    rig.adapter.set_read("fetch_order", Known(market_view_for(order)))
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED and len(rig.sent("create_market_order")) == 1


async def test_with_the_same_id_configuration_the_original_order_row_is_sent_again(make_rig):
    rig = make_rig(ex_resend_client_id="same")
    rec = await unknown_close(rig)
    rig.adapter.push_write_result("create_market_order", fill_ok())
    for _ in range(3):
        await tick(rig)
    first, second = rig.sent("create_market_order")
    assert first["client_order_id"] == second["client_order_id"]
    assert len(await rig.order_rows(rec.id)) == 1
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED


async def test_the_number_of_resends_is_capped(rig):
    rec = await unknown_close(rig)
    for _ in range(3):
        rig.adapter.push_write_result("create_market_order", unknown_write())
    for _ in range(12):
        await tick(rig)
    assert len(rig.sent("create_market_order")) == 3  # 原单加两次重发
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "回查" in done.state_reason
    assert len(await rig.order_rows(rec.id)) == 3


async def test_a_resent_order_that_is_rejected_by_the_exchange_fails_the_intent(rig):
    rec = await unknown_close(rig)
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.OTHER, -1111, "精度错误"))
    for _ in range(3):
        await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "交易所拒绝平仓单" in done.state_reason


# ================================================================ 键的串行：同键的平仓与开仓


async def test_a_close_and_an_open_on_the_same_key_never_run_at_the_same_time(rig):
    import asyncio

    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    inside = 0
    peak = 0

    async def hook(name, kw):
        nonlocal inside, peak
        inside += 1
        peak = max(peak, inside)
        await asyncio.sleep(0.02)
        inside -= 1

    rig.adapter.hook = hook
    await rig.go_all(close_spec(), spec(side=LONG), close_spec())
    assert peak == 1
