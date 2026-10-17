"""部分平仓（F4 第 5–9 步）：数量、平仓中暂停、超时不自动重发、崩溃点。"""
from __future__ import annotations

import dataclasses

import pytest

from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.core.types import (
    IntentState,
    Known,
    OrderPurpose,
    OrderState,
    PositionStatus,
    RejectKind,
    Unknown,
)
from dmr_kernel.exchange import ids

from coordinator_kit import (
    D, LONG, SELL, SHORT, SYM, ack_ok, algo_ok, fill_ok, market_view_for, partial_spec, position, register, rejected,
    stop_place_spec, tick, unknown_write,
)

S = IntentState


async def opened(rig, qty: str = "0.01", **kw):
    await rig.start(**kw)
    rig.positions = [position(SYM, LONG, qty)]


# ================================================================ 数量


async def test_partial_close_sends_the_requested_quantity_against_the_position(rig):
    await opened(rig)
    rig.adapter.push_write_result("create_market_order", fill_ok())
    rec = await rig.go(partial_spec("0.004"))
    assert rec.state is S.COMPLETED
    call = rig.sent("create_market_order")[0]
    assert set(call) == {"symbol", "side", "position_side", "qty", "client_order_id"}  # 不带只减仓标记
    assert (call["side"], call["position_side"], call["qty"]) == (SELL, LONG, D("0.004"))
    parsed = ids.parse_client_id(call["client_order_id"])
    assert parsed.leg == "p" and parsed.model_code == "0" and parsed.intent_id == rec.id
    order = await rig.only_order(rec.id)
    assert order.purpose is OrderPurpose.PARTIAL_CLOSE and order.qty == D("0.004")


async def test_partial_close_of_a_short_buys(rig):
    await rig.start()
    rig.positions = [position(SYM, SHORT, "0.01")]
    await rig.go(partial_spec("0.004", side=SHORT))
    call = rig.sent("create_market_order")[0]
    assert call["side"].value == "BUY" and call["position_side"] is SHORT


async def test_the_quantity_never_exceeds_the_live_position(rig):
    await opened(rig, "0.01")
    await rig.go(partial_spec("0.02"))
    assert rig.sent("create_market_order")[0]["qty"] == D("0.01")


async def test_the_quantity_is_rounded_down_to_the_step(rig):
    await opened(rig)
    await rig.go(partial_spec("0.0045"))
    assert rig.sent("create_market_order")[0]["qty"] == D("0.004")


async def test_a_quantity_that_rounds_to_zero_is_rejected(rig):
    await opened(rig)
    rec = await rig.go(partial_spec("0.0004"))
    assert rec.state is S.REJECTED and "取整后为 0" in rec.state_reason and "平掉此仓" in rec.state_reason
    assert rig.names() == []


async def test_a_partial_close_without_a_quantity_is_rejected(rig):
    await opened(rig)
    rec = await rig.go(dataclasses.replace(partial_spec(), qty=None))
    assert rec.state is S.REJECTED and "数量" in rec.state_reason and rig.names() == []


async def test_a_partial_close_of_a_gone_position_is_completed_as_closed_elsewhere(rig):
    await rig.start()
    rec = await rig.go(partial_spec())
    assert rec.state is S.COMPLETED and "已由其它途径平掉" in rec.state_reason and rig.names() == []


async def test_an_unreadable_position_fails_a_partial_close_even_with_the_retry_flag(rig):
    await rig.start()
    rig.adapter.set_read("fetch_positions", Unknown("限频"))
    rec = await rig.go(partial_spec(params={"retry_until_done": True}))
    assert rec.state is S.REJECTED and "持仓读数未知" in rec.state_reason and rig.names() == []


async def test_a_partial_close_records_the_position_it_saw(rig):
    await opened(rig, "0.01")
    rec = await rig.go(partial_spec("0.004"))
    assert rec.params["held_before"] == "0.01"


# ================================================================ 碎仓不走条件单


async def test_a_partial_close_never_uses_the_whole_position_condition_for_dust(rig):
    await opened(rig)
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.MIN_NOTIONAL, -4164, "太小"))
    rec = await rig.go(partial_spec("0.001"))  # 名义金额 50 < 100
    assert rec.state is S.REJECTED and "4164" in rec.state_reason
    assert rig.sent("create_stop_order") == []  # 全平型条件单会平掉整个仓，部分平仓绝不用
    assert len(rig.sent("create_market_order")) == 1  # 也不因为金额小就不发：交易所说了算


# ================================================================ −2022


async def test_reduce_rejected_with_a_zero_reread_is_closed_elsewhere(rig):
    await opened(rig)

    async def hook(name, kw):
        rig.positions = []  # 部分平仓与交易所侧止损同时触发

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.REDUCE_REJECTED, -2022, "无仓可平"))
    rec = await rig.go(partial_spec("0.004"))
    assert rec.state is S.COMPLETED and "已由其它途径平掉" in rec.state_reason


async def test_reduce_rejected_with_a_smaller_nonzero_position_is_a_failure_and_never_resent(rig):
    """F4 第 9 步：被交易所以无仓可平拒绝，先读持仓——为零记「已由其它途径平掉」，非零才记失败。
    部分平仓不自动重发（不能按变小了的持仓再发一张，把用户要保留的那部分也平掉）；由用户重新预览。"""
    await opened(rig)

    async def hook(name, kw):
        rig.positions = [position(SYM, LONG, "0.003")]

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.REDUCE_REJECTED, -2022, "超出"))
    rig.adapter.push_write_result("create_market_order", fill_ok())
    rec = await rig.go(partial_spec("0.004"))
    assert rec.state is S.REJECTED and "0.003" in rec.state_reason and "−2022" in rec.state_reason
    assert [c["qty"] for c in rig.sent("create_market_order")] == [D("0.004")]
    assert [o.leg for o in await rig.order_rows(rec.id)] == ["p"]


async def test_reduce_rejected_while_the_position_is_large_enough_is_a_failure(rig):
    await opened(rig)
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.REDUCE_REJECTED, -2022, "拒绝"))
    rec = await rig.go(partial_spec("0.004"))
    assert rec.state is S.REJECTED and "非零" in rec.state_reason and len(rig.sent("create_market_order")) == 1


# ================================================================ 平仓中：自动动作暂停


async def test_the_managed_position_is_marked_closing_until_the_order_ends(rig):
    await opened(rig)
    managed = await register(rig)
    assert managed.status is PositionStatus.MANAGED
    rig.adapter.push_write_result("create_market_order", ack_ok())
    rec = await rig.go(partial_spec("0.004"))
    assert rec.state is S.ACTIVE
    assert rig.registry.get_active(SYM, LONG).status is PositionStatus.CLOSING  # 自动动作暂停
    order = await rig.only_order(rec.id)
    await rig.orders.apply_exchange_view(order.client_order_id, market_view_for(order), source="stream")
    await rig.settle()
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED
    assert rig.registry.get_active(SYM, LONG).status is PositionStatus.MANAGED  # 订单到终态后恢复


async def test_a_rejected_partial_close_resumes_the_position_at_once(rig):
    await opened(rig)
    await register(rig)
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.OTHER, -1111, "精度错误"))
    rec = await rig.go(partial_spec("0.004"))
    assert rec.state is S.REJECTED
    assert rig.registry.get_active(SYM, LONG).status is PositionStatus.MANAGED


async def test_the_pause_ends_after_partial_close_pause_s_even_if_the_order_is_still_open(make_rig):
    rig = make_rig(partial_close_pause_s=10)
    await opened(rig)
    await register(rig)
    rig.adapter.push_write_result("create_market_order", ack_ok())
    rec = await rig.go(partial_spec("0.004"))
    await tick(rig, 9_999)
    assert rig.registry.get_active(SYM, LONG).status is PositionStatus.CLOSING  # 还差 1 毫秒
    await tick(rig, 1)
    assert rig.registry.get_active(SYM, LONG).status is PositionStatus.MANAGED  # 到点恢复
    assert (await rig.intents.get(rec.id)).state is S.ACTIVE  # 意图本身还在等成交
    assert rig.coordinator.key_busy(SYM, LONG)


async def test_a_partial_close_on_an_unregistered_position_just_goes_ahead(rig):
    await opened(rig)  # 登记簿里没有这个仓
    rec = await rig.go(partial_spec("0.004"))
    assert rec.state is S.ACTIVE and len(rig.sent("create_market_order")) == 1


# ================================================================ 超时：不自动重发


async def test_an_unknown_partial_close_is_never_resent_and_the_result_stays_unknown(rig):
    await opened(rig)
    rig.adapter.push_write_result("create_market_order", unknown_write("请求超时"))
    rec = await rig.go(partial_spec("0.004"))
    assert rec.state is S.RECONCILING and rig.coordinator.key_busy(SYM, LONG)
    rig.adapter.set_read("fetch_order", Unknown("查询超时"))
    for _ in range(4):
        await tick(rig)
    assert len(rig.sent("create_market_order")) == 1
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING  # 结果记「未知」，继续回查


async def test_an_unknown_partial_close_that_turns_out_filled_updates_the_result(rig):
    await opened(rig)
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(partial_spec("0.004"))
    order = await rig.only_order(rec.id)
    rig.adapter.set_read("fetch_order", Known(market_view_for(order)))
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED and len(rig.sent("create_market_order")) == 1


async def test_an_unknown_partial_close_the_exchange_never_saw_is_rejected_not_resent(rig):
    await opened(rig)
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(partial_spec("0.004"))
    rig.adapter.set_read("fetch_order", Known(None))
    for _ in range(3):
        await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "回查" in done.state_reason
    assert len(rig.sent("create_market_order")) == 1  # 绝不自动重发，由用户决定


async def test_a_changed_position_keeps_an_unknown_partial_close_unresolved(rig):
    await opened(rig)
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(partial_spec("0.004"))
    rig.adapter.set_read("fetch_order", Known(None))
    rig.positions = [position(SYM, LONG, "0.006")]  # 仓位少了 0.004：这张单可能其实成交了
    for _ in range(4):
        await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_the_pause_is_released_when_the_unknown_result_is_finally_rejected(rig):
    await opened(rig)
    await register(rig)
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(partial_spec("0.004"))
    assert rig.registry.get_active(SYM, LONG).status is PositionStatus.CLOSING
    rig.adapter.set_read("fetch_order", Known(None))
    for _ in range(3):
        await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.REJECTED
    assert rig.registry.get_active(SYM, LONG).status is PositionStatus.MANAGED


# ================================================================ 崩溃点与同键的保护类动作


async def test_partial_close_sent_crash_leaves_the_order_row_and_the_position_figure_on_disk(rig):
    await opened(rig)
    rig.crash.arm("partial_close_sent")
    result = await rig.coordinator.submit(partial_spec("0.004"))
    with pytest.raises(CrashSignal) as raised:
        await rig.coordinator.wait_terminal(result.intent_id, 2.0)
    assert raised.value.name == "partial_close_sent"
    row = await rig.only_order(result.intent_id)
    assert row.state is OrderState.PENDING_SEND and row.sent_ms is not None and row.qty == D("0.004")
    assert len(rig.sent("create_market_order")) == 1
    stored = await rig.intents.get(result.intent_id)
    assert stored.state is S.DISPATCH_READY and stored.params["held_before"] == "0.01"  # 回查时据此判断持仓变没变


async def test_a_partial_close_in_flight_does_not_block_a_protective_action_on_the_same_key(rig):
    await opened(rig)
    rig.adapter.push_write_result("create_market_order", ack_ok())
    partial = await rig.go(partial_spec("0.004"))
    assert partial.state is S.ACTIVE  # 订单没到终态
    rig.adapter.push_write_result("create_stop_order", algo_ok())
    stop = await rig.go(stop_place_spec("45000"))  # 键的串行只覆盖「决定 + 发单」，不挡保护类动作
    assert stop.state is S.COMPLETED and len(rig.sent("create_stop_order")) == 1


async def test_a_partial_close_does_not_cancel_the_existing_stop(rig):
    await opened(rig)
    rig.algos = []
    await rig.go(partial_spec("0.004"))
    assert rig.sent("cancel_algo_order") == [] and rig.sent("create_stop_order") == []  # 期间不撤现有止损
