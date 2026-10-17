"""订单三类超时的原语（报告 F1 第 9 步、F4 第 8 步、F5 第 7 步）。

订单服务只提供原语（超时结果 → SENT_UNKNOWN、按编号回查、判定「不存在」、标记拒绝），从不自己重发：
- 开仓单超时：不重发，这次信号作废；订单保持 SENT_UNKNOWN，持续回查到终态；
  只有「超过签名时间窗加宽限 + 连续 3 次查到不存在 + 该侧持仓没有变化」三条同时满足才能置为 REJECTED。
- 平仓单超时：先回查；连续 3 次无果且持仓数量未变，才允许重发（重发用新编号，原单留作记录）。
- 部分平仓超时：不自动重发，结果为「未知」，系统继续回查，查清后更新结果。
"""
from __future__ import annotations

from decimal import Decimal

from dmr_kernel.core.types import (
    IntentAction,
    Known,
    OrderPurpose,
    OrderState,
    Unknown,
    WriteUnknown,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.adapter import SIGN_WINDOW_MS

O = OrderState
GRACE_S = 15


async def _timed_out(orders, order):
    """协调者的动作：落盘 → 记发出时间 → 发请求 → 超时，没有回执。"""
    await orders.record_sent(order.client_order_id)
    return await orders.apply_write_result(order.client_order_id, WriteUnknown("请求超时", http_status=503))


async def test_open_order_timeout_enters_sent_unknown_without_any_resend(new_order, orders, adapter):
    order = await new_order()
    timed_out = await _timed_out(orders, order)
    assert timed_out.state is O.SENT_UNKNOWN
    assert timed_out.sent_ms is not None
    assert adapter.writes == []  # 订单服务不发任何写请求：「不重发，这次信号作废」由协调者执行


async def test_open_order_is_requeried_until_the_exchange_answers_and_a_found_fill_wins(new_order, orders, adapter,
                                                                                         order_info, clock):
    order = await new_order(qty="1")
    await _timed_out(orders, order)
    answers = iter([Unknown("网络"), None, None])  # 未知、不存在、不存在
    adapter.set_read("fetch_order", lambda **kw: next(answers))
    for _ in range(3):
        clock.advance(2_000)
        await orders.requery_unknown()
    mid = await orders.get(order.client_order_id)
    assert mid.state is O.SENT_UNKNOWN and mid.absent_count == 2  # 未知那一次不计数

    adapter.set_read("fetch_order", lambda **kw: order_info(order, status="FILLED", filled="1", avg="100"))
    clock.advance(2_000)
    [found] = await orders.requery_unknown()
    assert found.state is O.FILLED and found.absent_count == 0  # 查到成交：登记持仓的事交给下游，连续不存在清零
    assert await orders.requery_unknown() == []  # 到了终态就不再回查
    assert adapter.writes == []


async def test_open_order_judged_absent_only_when_all_three_conditions_hold(new_order, orders, adapter, clock):
    order = await new_order()
    await _timed_out(orders, order)
    adapter.set_read("fetch_order", lambda **kw: None)  # 交易所每次都明确答复「不存在」

    async def poll():
        clock.advance(1_000)
        await orders.requery_unknown()
        return await orders.get(order.client_order_id)

    def judge(rec, **kw):
        return orders.judge_absent(rec, now_ms=clock.now_ms(), grace_s=GRACE_S, position_unchanged=kw.get("unchanged", True))

    rec = await poll()
    assert (rec.absent_count, judge(rec)) == (1, False)  # 次数不够、时间也不够
    rec = await poll()
    rec = await poll()
    assert rec.absent_count == 3
    assert judge(rec) is False  # 次数够了，但还在签名时间窗 + 宽限之内：交易所仍可能受理
    clock.advance(SIGN_WINDOW_MS + GRACE_S * 1000)  # 发出后已超过窗口加宽限
    assert judge(rec, unchanged=False) is False  # 该侧持仓变了：可能就是这张单成交造成的
    assert judge(rec) is True

    rejected = await orders.mark_rejected(order.client_order_id, reason="连续 3 次查到不存在")
    assert rejected.state is O.REJECTED
    assert await orders.requery_unknown() == []
    assert adapter.writes == []


async def test_a_late_view_before_the_verdict_keeps_the_order_alive(new_order, orders, adapter, order_info, clock):
    order = await new_order(qty="1")
    await _timed_out(orders, order)
    adapter.set_read("fetch_order", lambda **kw: None)
    for _ in range(2):
        clock.advance(1_000)
        await orders.requery_unknown()
    adapter.set_read("fetch_order", lambda **kw: order_info(order, status="NEW"))
    clock.advance(30_000)
    await orders.requery_unknown()
    seen = await orders.get(order.client_order_id)
    assert seen.state is O.ACKNOWLEDGED and seen.absent_count == 0
    assert orders.judge_absent(seen, now_ms=clock.now_ms(), grace_s=GRACE_S, position_unchanged=True) is False


async def test_close_order_timeout_allows_a_resend_only_after_three_misses_with_an_unchanged_position(
        new_order, make_intent, orders, adapter, clock):
    intent = await make_intent(action=IntentAction.CLOSE)
    original = await new_order(intent=intent, leg="c", purpose=OrderPurpose.CLOSE)
    await _timed_out(orders, original)
    adapter.set_read("fetch_order", lambda **kw: None)
    for _ in range(3):
        clock.advance(1_000)
        await orders.requery_unknown()
    clock.advance(SIGN_WINDOW_MS + GRACE_S * 1000)
    rec = await orders.get(original.client_order_id)
    assert rec.absent_count == 3
    assert orders.judge_absent(rec, now_ms=clock.now_ms(), grace_s=GRACE_S, position_unchanged=False) is False
    assert orders.judge_absent(rec, now_ms=clock.now_ms(), grace_s=GRACE_S, position_unchanged=True) is True

    # 允许重发：换新编号（实测第 13 项未出结果前一律换），原单留作记录
    rejected = await orders.mark_rejected(original.client_order_id, reason="连续 3 次回查无果，持仓数量未变")
    resend = await new_order(intent=intent, leg=ids.resend_leg("c", 1), purpose=OrderPurpose.CLOSE,
                             resend_of=original.client_order_id)
    assert rejected.state is O.REJECTED and resend.state is O.PENDING_SEND
    assert resend.client_order_id != original.client_order_id and resend.resend_of == original.client_order_id
    assert adapter.writes == []


async def test_partial_close_timeout_is_only_requeried_never_resent_or_given_up(new_order, make_intent, orders,
                                                                                 adapter, order_info, clock):
    intent = await make_intent(action=IntentAction.PARTIAL_CLOSE)
    order = await new_order(intent=intent, leg="p", purpose=OrderPurpose.PARTIAL_CLOSE, qty="0.4")
    await _timed_out(orders, order)

    adapter.set_read("fetch_order", lambda **kw: None)
    clock.advance(10 * 60_000)  # 过了很久、回查了很多次也没有查到
    for _ in range(6):
        await orders.requery_unknown()
    rec = await orders.get(order.client_order_id)
    assert rec.state is O.SENT_UNKNOWN  # 结果保持「未知」，由用户决定；服务不会自己改成 REJECTED
    assert rec.absent_count == 6
    assert adapter.writes == []

    # 之后查清了：更新结果
    adapter.set_read("fetch_order", lambda **kw: order_info(order, status="FILLED", filled="0.4", avg="100"))
    [resolved] = await orders.requery_unknown()
    assert resolved.state is O.FILLED and resolved.filled_qty == Decimal("0.4")
    assert adapter.writes == []


async def test_requery_keeps_asking_while_the_read_keeps_failing(new_order, orders, adapter, clock):
    order = await new_order(purpose=OrderPurpose.PARTIAL_CLOSE, leg="p")
    await _timed_out(orders, order)
    adapter.set_read("fetch_order", Unknown("交易所不可达"))
    for round_no in range(1, 5):
        clock.advance(1_000)
        [rec] = await orders.requery_unknown()
        assert rec.state is O.SENT_UNKNOWN and rec.absent_count == 0 and rec.last_query_ms == clock.now_ms()
        assert len(adapter.calls("fetch_order")) == round_no
    assert Known(None) != Unknown("x")  # 「明确不存在」和「未知」是两种东西
