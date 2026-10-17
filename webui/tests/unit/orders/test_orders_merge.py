"""订单状态的单调合并（repo_orders.merge_view：订单服务与入账共用的唯一一处）。

规则：已到终态的不再回到非终态；filled_qty 只增不减；有成交且成交量等于数量 → FILLED；
EXPIRED/CANCELED 且有部分成交 → CANCELLED（保留成交量）；raw_status 记最新的交易所原始状态（乱序到达的旧状态不倒退）。
"""
from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from dmr_kernel.core.types import CancelOutcome, CancelResult, OrderState, WriteAccepted, WriteUnknown
from dmr_kernel.ledger import repo_orders

O = OrderState
D = Decimal


@pytest.fixture
def apply(orders):
    """把一个交易所视图合并进订单（走订单服务，和数据流到达的路径一样）。"""

    async def run(order, view, *, source="stream"):
        return await orders.apply_exchange_view(order.client_order_id, view, source=source)

    return run


def final(rec):
    """比较「结果是否一致」用的关键字段。"""
    return (rec.state, rec.filled_qty, rec.avg_price, rec.raw_status)


# ---------------------------------------------------------------- 交易所状态 → 订单状态

@pytest.mark.parametrize("raw,filled,expected", [
    ("NEW", "0", O.ACKNOWLEDGED),
    ("PARTIALLY_FILLED", "0.4", O.PARTIALLY_FILLED),
    ("FILLED", "1", O.FILLED),
    ("CANCELED", "0", O.CANCELLED),
    ("EXPIRED", "0", O.CANCELLED),
    ("EXPIRED_IN_MATCH", "0", O.CANCELLED),
    ("REJECTED", "0", O.REJECTED),
])
async def test_regular_order_status_mapping(new_order, apply, order_info, raw, filled, expected):
    order = await new_order(qty="1")
    status = "EXPIRED" if raw == "EXPIRED_IN_MATCH" else raw
    done = await apply(order, order_info(order, status=status, raw=raw, filled=filled))
    assert done.state is expected and done.raw_status == raw


@pytest.mark.parametrize("raw,expected", [
    ("NEW", O.ACKNOWLEDGED),
    ("TRIGGERING", O.ACKNOWLEDGED),
    ("TRIGGERED", O.ACKNOWLEDGED),  # 已触发但触发后的订单还没成交
    ("FINISHED", O.FILLED),
    ("CANCELED", O.CANCELLED),
    ("EXPIRED", O.CANCELLED),
    ("REJECTED", O.REJECTED),
])
async def test_algo_order_status_mapping(new_stop, apply, algo_info, raw, expected):
    stop = await new_stop()
    done = await apply(stop, algo_info(stop, raw=raw))
    assert done.state is expected and done.raw_status == raw


async def test_unrecognised_raw_status_falls_back_to_the_normalised_status(new_order, apply, order_info):
    order = await new_order()
    done = await apply(order, order_info(order, status="NEW", raw="PENDING_CANCEL"))
    assert done.state is O.ACKNOWLEDGED and done.raw_status == "PENDING_CANCEL"  # 原始状态照记


async def test_completely_unrecognised_status_is_recorded_but_changes_no_state(new_order, apply, order_info):
    order = await new_order()
    done = await apply(order, order_info(order, status="WEIRD", raw="WEIRD"))
    assert done.state is O.PENDING_SEND and done.raw_status == "WEIRD"


async def test_from_every_open_state_a_view_moves_forward(new_order, orders, apply, order_info):
    pending = await new_order(leg="o")
    unknown = await new_order(leg="c")
    await orders.apply_write_result(unknown.client_order_id, WriteUnknown("超时"))
    for order in (pending, unknown):
        assert (await apply(order, order_info(order, status="NEW"))).state is O.ACKNOWLEDGED
    pending2 = await new_order(leg="p")
    assert (await apply(pending2, order_info(pending2, status="PARTIALLY_FILLED", filled="0.5"))).state is \
        O.PARTIALLY_FILLED


# ---------------------------------------------------------------- 终态不回头

@pytest.mark.parametrize("terminal_status,terminal_state,filled", [
    ("FILLED", O.FILLED, "1"), ("CANCELED", O.CANCELLED, "0"), ("CANCELED", O.CANCELLED, "0.4"),
])
@pytest.mark.parametrize("late_status,late_filled", [("NEW", "0"), ("PARTIALLY_FILLED", "0.2")])
async def test_a_terminal_order_never_returns_to_a_non_terminal_state(new_order, apply, order_info, terminal_status,
                                                                      terminal_state, filled, late_status,
                                                                      late_filled):
    order = await new_order(qty="1")
    await apply(order, order_info(order, status=terminal_status, filled=filled))
    again = await apply(order, order_info(order, status=late_status, filled=late_filled))
    assert again.state is terminal_state
    assert again.filled_qty == max(D(filled), D(late_filled))  # 状态不回头，但迟到的成交量照记


async def test_rejected_order_stays_rejected_for_views_without_fills(new_order, apply, order_info):
    order = await new_order()
    await apply(order, order_info(order, status="REJECTED"))
    assert (await apply(order, order_info(order, status="NEW"))).state is O.REJECTED


async def test_a_stale_cancel_cannot_undo_a_fill(new_order, apply, order_info):
    order = await new_order(qty="1")
    await apply(order, order_info(order, status="FILLED", filled="1", avg="100"))
    again = await apply(order, order_info(order, status="EXPIRED", raw="EXPIRED", filled="0"))
    assert (again.state, again.filled_qty, again.raw_status) == (O.FILLED, D("1"), "FILLED")


@pytest.mark.parametrize("first", ["CANCELED", "REJECTED"])
async def test_full_fill_evidence_upgrades_a_cancelled_or_rejected_order_to_filled(new_order, apply, order_info, first):
    order = await new_order(qty="1")
    await apply(order, order_info(order, status=first))
    done = await apply(order, order_info(order, status="FILLED", filled="1", avg="100"))
    assert done.state is O.FILLED and done.filled_qty == D("1") and done.raw_status == "FILLED"


async def test_fills_found_on_a_rejected_order_turn_it_into_a_cancelled_partial(new_order, apply, order_info):
    """被误判为拒绝的订单后来有了成交：成交证明订单存在，不能再留在 REJECTED。"""
    order = await new_order(qty="1")
    await apply(order, order_info(order, status="REJECTED"))
    done = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.5", avg="100"))
    assert done.state is O.CANCELLED and done.filled_qty == D("0.5")


# ---------------------------------------------------------------- filled_qty 只增不减

async def test_filled_qty_never_decreases(new_order, apply, order_info):
    order = await new_order(qty="1")
    await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.7", avg="101"))
    stale = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.3", avg="100"))
    assert (stale.filled_qty, stale.avg_price) == (D("0.7"), D("101"))  # 均价跟着最大的成交量走
    grown = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.9", avg="102"))
    assert (grown.filled_qty, grown.avg_price) == (D("0.9"), D("102"))


async def test_missing_or_zero_average_price_never_overwrites_a_known_one(new_order, apply, order_info):
    order = await new_order(qty="1")
    await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.3", avg="100"))
    kept = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.5", avg=None))
    assert (kept.filled_qty, kept.avg_price) == (D("0.5"), D("100"))
    zero = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.6", avg="0"))
    assert (zero.filled_qty, zero.avg_price) == (D("0.6"), D("100"))


async def test_average_price_follows_the_largest_quantity_and_the_exchange_wins_ties(new_order, apply, order_info,
                                                                                    ledger, clock):
    order = await new_order(qty="1")
    merge = lambda **kw: ledger.write(repo_orders.merge_view, order.client_order_id, kw.pop("view", None),  # noqa: E731
                                      now_ms=clock.now_ms(), **kw)
    _, local = await merge(fills_qty=D("0.5"), fills_avg=D("99"))
    assert local.avg_price == D("99")
    _, tie = await merge(view=order_info(order, status="PARTIALLY_FILLED", filled="0.5", avg="99.5"))
    assert tie.avg_price == D("99.5")  # 数量相同：交易所给的均价为准
    _, more_local = await merge(fills_qty=D("0.8"), fills_avg=D("98"))
    assert more_local.avg_price == D("98")  # 本地汇总的成交量更大：跟着它走
    _, stale = await merge(view=order_info(order, status="PARTIALLY_FILLED", filled="0.6", avg="97"))
    assert (stale.filled_qty, stale.avg_price) == (D("0.8"), D("98"))  # 交易所视图的成交量更小：不能把均价拉回去


async def test_reject_refuses_an_order_with_fills_even_without_a_state_filter(new_order, apply, order_info, ledger,
                                                                              clock):
    order = await new_order(qty="1")
    await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.5"))
    before, after = await ledger.write(repo_orders.reject, order.client_order_id, now_ms=clock.now_ms(),
                                       error_code=None, error_msg="不该生效", from_states=None, audit=False)
    assert after.state is O.PARTIALLY_FILLED and after == before


async def test_a_view_without_fills_leaves_existing_fills_alone(new_order, apply, order_info):
    order = await new_order(qty="1")
    await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.5", avg="100"))
    after = await apply(order, order_info(order, status="NEW", filled="0"))
    assert (after.state, after.filled_qty, after.avg_price) == (O.PARTIALLY_FILLED, D("0.5"), D("100"))


# ---------------------------------------------------------------- 成交量等于数量 → FILLED

@pytest.mark.parametrize("status", ["NEW", "PARTIALLY_FILLED", "FILLED"])
async def test_filled_quantity_equal_to_the_order_quantity_means_filled(new_order, apply, order_info, status):
    order = await new_order(qty="1")
    done = await apply(order, order_info(order, status=status, filled="1", avg="100"))
    assert done.state is O.FILLED


async def test_overfill_is_still_filled(new_order, apply, order_info):
    order = await new_order(qty="1")
    assert (await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="1.2"))).state is O.FILLED


async def test_any_fill_on_an_unconfirmed_order_makes_it_partially_filled(new_order, apply, order_info):
    order = await new_order(qty="1")
    done = await apply(order, order_info(order, status="NEW", filled="0.1"))
    assert done.state is O.PARTIALLY_FILLED


async def test_fills_alone_without_an_exchange_view_drive_the_state(new_order, ledger, clock):
    """入账走的路径：只有成交汇总，没有交易所的订单视图。"""
    order = await new_order(qty="1")

    async def merge(qty, avg):
        before, after = await ledger.write(repo_orders.merge_view, order.client_order_id, None, now_ms=clock.now_ms(),
                                           fills_qty=D(qty), fills_avg=D(avg))
        return before, after

    before, half = await merge("0.5", "100")
    assert before.state is O.PENDING_SEND
    assert (half.state, half.filled_qty, half.avg_price, half.raw_status) == (O.PARTIALLY_FILLED, D("0.5"), D("100"), None)
    _, whole = await merge("1", "101")
    assert (whole.state, whole.filled_qty, whole.avg_price) == (O.FILLED, D("1"), D("101"))
    _, again = await merge("0.5", "100")  # 迟到的小汇总：不倒退
    assert (again.state, again.filled_qty, again.avg_price) == (O.FILLED, D("1"), D("101"))


async def test_stop_order_without_quantity_is_filled_only_when_the_exchange_says_so(new_stop, apply, algo_info, ledger,
                                                                                    clock):
    stop = await new_stop()
    assert stop.qty is None
    _, partial = await ledger.write(repo_orders.merge_view, stop.client_order_id, None, now_ms=clock.now_ms(),
                                    fills_qty=D("5"), fills_avg=D("90"))
    assert (partial.state, partial.filled_qty) == (O.PARTIALLY_FILLED, D("5"))  # 没有数量可比，不能自己推断 FILLED
    done = await apply(stop, algo_info(stop, raw="FINISHED", actual_order_id="9100"))
    assert (done.state, done.filled_qty, done.actual_order_id) == (O.FILLED, D("5"), "9100")


async def test_triggered_stop_with_fills_then_finished_in_any_order(new_stop, apply, algo_info, ledger, clock):
    a = await new_stop()
    b = await new_stop(position_side=a.position_side, trigger_price="91")
    for stop, order_of_events in ((a, ("fills", "finished")), (b, ("finished", "fills"))):
        for event in order_of_events:
            if event == "fills":
                await ledger.write(repo_orders.merge_view, stop.client_order_id, None, now_ms=clock.now_ms(),
                                   fills_qty=D("5"), fills_avg=D("90"))
            else:
                await apply(stop, algo_info(stop, raw="FINISHED"))
    ra = await apply(a, algo_info(a, raw="TRIGGERED"))  # 迟到的旧状态也不倒退
    rb = await apply(b, algo_info(b, raw="TRIGGERED"))
    assert (ra.state, ra.filled_qty, ra.raw_status) == (rb.state, rb.filled_qty, rb.raw_status) == (O.FILLED, D("5"), "FINISHED")


# ---------------------------------------------------------------- 部分成交后余量作废

@pytest.mark.parametrize("raw", ["EXPIRED", "EXPIRED_IN_MATCH", "CANCELED"])
async def test_partial_fill_then_the_rest_is_voided_gives_cancelled_with_the_fill_kept(new_order, apply, order_info,
                                                                                       raw):
    order = await new_order(qty="1")
    await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.4", avg="100"))
    status = "CANCELED" if raw == "CANCELED" else "EXPIRED"
    done = await apply(order, order_info(order, status=status, raw=raw, filled="0.4", avg="100"))
    assert (done.state, done.filled_qty, done.avg_price, done.raw_status) == (O.CANCELLED, D("0.4"), D("100"), raw)


# ---------------------------------------------------------------- raw_status

async def test_raw_status_follows_the_exchange_progress(new_order, apply, order_info):
    order = await new_order(qty="1")
    seen = []
    for status, filled in (("NEW", "0"), ("PARTIALLY_FILLED", "0.4"), ("FILLED", "1")):
        seen.append((await apply(order, order_info(order, status=status, filled=filled))).raw_status)
    assert seen == ["NEW", "PARTIALLY_FILLED", "FILLED"]


async def test_a_late_older_raw_status_does_not_move_raw_status_backwards(new_order, apply, order_info):
    order = await new_order(qty="1")
    await apply(order, order_info(order, status="EXPIRED", raw="EXPIRED_IN_MATCH", filled="0.4"))
    late = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.4"))
    assert late.raw_status == "EXPIRED_IN_MATCH" and late.state is O.CANCELLED
    late2 = await apply(order, order_info(order, status="NEW"))
    assert late2.raw_status == "EXPIRED_IN_MATCH"


async def test_the_first_terminal_raw_status_is_kept_when_another_terminal_one_arrives(new_order, apply, order_info):
    order = await new_order(qty="1")
    await apply(order, order_info(order, status="EXPIRED", raw="EXPIRED_IN_MATCH"))
    again = await apply(order, order_info(order, status="CANCELED"))
    assert again.raw_status == "EXPIRED_IN_MATCH" and again.state is O.CANCELLED


# ---------------------------------------------------------------- 乱序到达，结果一致

async def test_expired_then_trade_equals_trade_then_expired(new_order, apply, order_info):
    """计划要求的两个顺序：先 EXPIRED 后 TRADE，先 TRADE 后 EXPIRED。"""
    trade_first = await new_order(qty="1")
    await apply(trade_first, order_info(trade_first, status="PARTIALLY_FILLED", filled="0.4", avg="100"))
    expired_second = await apply(trade_first, order_info(trade_first, status="EXPIRED", raw="EXPIRED_IN_MATCH",
                                                         filled="0.4", avg="100"))

    expired_first = await new_order(qty="1")
    await apply(expired_first, order_info(expired_first, status="EXPIRED", raw="EXPIRED_IN_MATCH", filled="0.4",
                                          avg="100"))
    trade_second = await apply(expired_first, order_info(expired_first, status="PARTIALLY_FILLED", filled="0.4",
                                                         avg="100"))

    assert final(expired_second) == final(trade_second) == (O.CANCELLED, D("0.4"), D("100"), "EXPIRED_IN_MATCH")


async def test_expired_event_that_does_not_carry_the_fill_still_converges(new_order, apply, order_info):
    """EXPIRED 事件里的累计成交量是 0（比 TRADE 事件更早生成）时，两种到达顺序结果仍一致。"""
    a = await new_order(qty="1")
    await apply(a, order_info(a, status="PARTIALLY_FILLED", filled="0.4", avg="100"))
    ra = await apply(a, order_info(a, status="EXPIRED", filled="0"))
    b = await new_order(qty="1")
    await apply(b, order_info(b, status="EXPIRED", filled="0"))
    rb = await apply(b, order_info(b, status="PARTIALLY_FILLED", filled="0.4", avg="100"))
    assert final(ra) == final(rb) == (O.CANCELLED, D("0.4"), D("100"), "EXPIRED")


def _views(order_info, order, kind):
    if kind == "partial_then_void":
        return [order_info(order, status="NEW"),
                order_info(order, status="PARTIALLY_FILLED", filled="0.4", avg="100"),
                order_info(order, status="EXPIRED", raw="EXPIRED_IN_MATCH", filled="0.4", avg="100")]
    if kind == "partial_then_cancel":
        return [order_info(order, status="NEW"),
                order_info(order, status="PARTIALLY_FILLED", filled="0.4", avg="100"),
                order_info(order, status="CANCELED", filled="0.4", avg="100")]
    return [order_info(order, status="NEW"),
            order_info(order, status="PARTIALLY_FILLED", filled="0.3", avg="99"),
            order_info(order, status="PARTIALLY_FILLED", filled="0.7", avg="100"),
            order_info(order, status="FILLED", filled="1", avg="101")]


@pytest.mark.parametrize("kind,expected", [
    ("partial_then_void", (O.CANCELLED, D("0.4"), D("100"), "EXPIRED_IN_MATCH")),
    ("partial_then_cancel", (O.CANCELLED, D("0.4"), D("100"), "CANCELED")),
    ("fill_in_steps", (O.FILLED, D("1"), D("101"), "FILLED")),
])
async def test_every_arrival_order_gives_the_same_final_record(new_order, apply, order_info, kind, expected):
    count = 0
    probe = await new_order(qty="1")
    n = len(_views(order_info, probe, kind))
    for permutation in itertools.permutations(range(n)):
        order = await new_order(qty="1")
        views = _views(order_info, order, kind)
        result = None
        for index in permutation:
            result = await apply(order, views[index])
        assert final(result) == expected, f"到达顺序 {permutation} 的结果不同：{final(result)}"
        count += 1
    assert count >= 6


# ---------------------------------------------------------------- 撤单中

async def test_cancel_pending_is_not_reverted_by_views_that_predate_the_cancel(new_order, orders, apply, order_info):
    order = await new_order(qty="1")
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order)))
    await orders.apply_cancel_result(order.client_order_id, CancelResult(CancelOutcome.UNKNOWN))
    assert (await apply(order, order_info(order, status="NEW"))).state is O.CANCEL_PENDING
    partial = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.3"))
    assert partial.state is O.CANCEL_PENDING and partial.filled_qty == D("0.3")  # 成交照记，状态仍在等撤单结果
    assert (await apply(order, order_info(order, status="CANCELED", filled="0.3"))).state is O.CANCELLED


async def test_cancel_pending_order_that_filled_in_the_meantime_is_filled(new_order, orders, apply, order_info):
    order = await new_order(qty="1")
    await orders.apply_write_result(order.client_order_id, WriteAccepted(order=order_info(order)))
    await orders.apply_cancel_result(order.client_order_id, CancelResult(CancelOutcome.UNKNOWN))
    assert (await apply(order, order_info(order, status="FILLED", filled="1"))).state is O.FILLED


# ---------------------------------------------------------------- 其它字段

async def test_acked_time_is_the_first_time_the_exchange_confirmed_the_order(new_order, apply, order_info, clock):
    order = await new_order(qty="1")
    assert order.acked_ms is None
    clock.advance(100)
    first = await apply(order, order_info(order))
    first_ack = clock.now_ms()
    clock.advance(900)
    second = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.5"))
    assert first.acked_ms == first_ack == second.acked_ms


async def test_a_rejected_view_is_not_an_acknowledgement(new_order, apply, order_info):
    order = await new_order()
    assert (await apply(order, order_info(order, status="REJECTED"))).acked_ms is None


async def test_identifiers_are_filled_in_once_and_never_erased(new_order, new_stop, apply, order_info, algo_info):
    order = await new_order()
    await apply(order, order_info(order, exchange_order_id="777"))
    again = await apply(order, order_info(order, exchange_order_id="888"))
    assert again.exchange_order_id == "777"
    stop = await new_stop()
    await apply(stop, algo_info(stop, algo_id="5001", actual_order_id="9100"))
    later = await apply(stop, algo_info(stop, algo_id="5001", actual_order_id=None))
    assert (later.algo_id, later.actual_order_id) == ("5001", "9100")


async def test_updated_time_only_moves_when_something_changed(new_order, apply, order_info, clock):
    order = await new_order(qty="1")
    first = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.5"))
    clock.advance(5_000)
    same = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.5"))
    assert same.updated_ms == first.updated_ms
    clock.advance(5_000)
    grown = await apply(order, order_info(order, status="PARTIALLY_FILLED", filled="0.6"))
    assert grown.updated_ms == clock.now_ms()


async def test_merge_view_returns_before_and_after_and_none_for_strangers(new_order, order_info, ledger, clock):
    order = await new_order(qty="1")
    before, after = await ledger.write(repo_orders.merge_view, order.client_order_id,
                                       order_info(order, status="FILLED", filled="1"), now_ms=clock.now_ms())
    assert before == order and after.state is O.FILLED
    assert await ledger.write(repo_orders.merge_view, "DKnope-1-1-o", order_info(order), now_ms=clock.now_ms()) is None
