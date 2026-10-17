"""条件单类意图：STOP_PLACE（F2 第 2 步）、STOP_REPLACE 两种模式（F2 第 7 步）、STOP_CANCEL。"""
from __future__ import annotations

import dataclasses

import pytest

from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.core.types import (
    AlgoType,
    CancelOutcome,
    CancelResult,
    IntentState,
    Known,
    OrderPurpose,
    OrderState,
    ProtectionState,
    RejectKind,
    Unknown,
    WorkingType,
)
from dmr_kernel.exchange import ids

from coordinator_kit import (
    BUY, D, LONG, SELL, SHORT, SYM, algo_for, algo_ok, position, register, rejected, stop_cancel_spec,
    stop_place_spec, stop_replace_spec, tick, unknown_write,
)

S = IntentState


# ================================================================ STOP_PLACE


async def test_stop_place_sends_a_whole_position_stop_market_order_and_completes_when_accepted(rig):
    await rig.start()
    rig.adapter.push_write_result("create_stop_order", algo_ok(algo_id="5151"))
    rec = await rig.go(stop_place_spec("45000"))
    assert rec.state is S.COMPLETED
    call = rig.sent("create_stop_order")[0]
    assert (call["symbol"], call["side"], call["position_side"]) == (SYM, SELL, LONG)
    assert call["trigger_price"] == D("45000") and call["working_type"] is WorkingType.CONTRACT_PRICE
    assert call["close_position"] is True and call["qty"] is None and call["algo_type"] is AlgoType.STOP_MARKET
    parsed = ids.parse_client_id(call["client_algo_id"])
    assert parsed.leg == "s" and parsed.model_code == "0" and parsed.intent_id == rec.id and parsed.instance == rig.instance
    order = await rig.only_order(rec.id)
    assert order.kind == "algo" and order.purpose is OrderPurpose.STOP and order.state is OrderState.ACKNOWLEDGED
    assert order.algo_id == "5151" and order.trigger_price == D("45000") and order.qty is None
    assert order.working_type is WorkingType.CONTRACT_PRICE and order.close_position is True


async def test_stop_place_for_a_short_buys_and_honours_the_mark_price_trigger_type(rig):
    await rig.start()
    rec = await rig.go(stop_place_spec("55000", side=SHORT,
                                       params={"trigger_price": "55000", "working_type": "MARK_PRICE"}))
    assert rec.state is S.COMPLETED
    call = rig.sent("create_stop_order")[0]
    assert (call["side"], call["position_side"], call["working_type"]) == (BUY, SHORT, WorkingType.MARK_PRICE)


async def test_stop_place_does_not_touch_the_protection_state_in_the_registry(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    await register(rig)
    rec = await rig.go(stop_place_spec("45000"))
    assert rec.state is S.COMPLETED
    registered = rig.registry.get_active(SYM, LONG)
    assert registered.protection_state is ProtectionState.NONE and registered.stop_price is None  # 由保护单管理处理


async def test_an_immediate_trigger_rejection_is_reported_with_the_machine_readable_reason(rig):
    await rig.start()
    rig.adapter.push_write_result("create_stop_order",
                                  rejected(RejectKind.IMMEDIATE_TRIGGER, -2021, "Order would immediately trigger."))
    rec = await rig.go(stop_place_spec("51000"))
    assert rec.state is S.REJECTED and rec.state_reason == "immediate_trigger"
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.REJECTED and order.error_code == -2021
    assert not rig.coordinator.key_busy(SYM, LONG)


@pytest.mark.parametrize("kind, code", [(RejectKind.DUAL_CLOSE_POSITION, -4130), (RejectKind.STOP_LIMIT_REACHED, -4045),
                                        (RejectKind.RATE_LIMITED, -1003), (RejectKind.OTHER, -1111)])
async def test_other_rejections_carry_the_error_code_in_the_reason(rig, kind, code):
    await rig.start()
    rig.adapter.push_write_result("create_stop_order", rejected(kind, code, "被拒"))
    rec = await rig.go(stop_place_spec())
    assert rec.state is S.REJECTED and rec.state_reason != "immediate_trigger"
    assert "交易所拒绝止损单" in rec.state_reason and str(code) in rec.state_reason


@pytest.mark.parametrize("params", [
    {}, {"trigger_price": "45000"}, {"working_type": "CONTRACT_PRICE"},
    {"trigger_price": "abc", "working_type": "CONTRACT_PRICE"}, {"trigger_price": "0", "working_type": "CONTRACT_PRICE"},
    {"trigger_price": "-5", "working_type": "CONTRACT_PRICE"}, {"trigger_price": "45000", "working_type": "LAST"},
])
async def test_invalid_stop_parameters_are_rejected_without_any_request(rig, params):
    await rig.start()
    rec = await rig.go(stop_place_spec(params=params))
    assert rec.state is S.REJECTED and "止损" in rec.state_reason
    assert rig.names() == [] and await rig.order_rows(rec.id) == []


async def test_stop_place_is_idempotent(rig):
    await rig.start()
    s = stop_place_spec("45000", idem_key="protect:1:g1:initial:1")
    await rig.coordinator.submit(s)
    await rig.coordinator.submit(s)
    await rig.settle()
    await rig.coordinator.submit(s)
    await rig.settle()
    assert len(rig.sent("create_stop_order")) == 1


async def test_an_unknown_stop_result_is_reconciled_by_looking_up_the_algo_order(rig):
    await rig.start()
    rig.adapter.push_write_result("create_stop_order", unknown_write("请求超时"))
    rec = await rig.go(stop_place_spec())
    assert rec.state is S.RECONCILING and "按编号回查" in rec.state_reason
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.SENT_UNKNOWN
    rig.adapter.set_read("fetch_algo_order", Known(algo_for(order)))  # 存在
    await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "回查确认" in done.state_reason
    assert (await rig.only_order(rec.id)).state is OrderState.ACKNOWLEDGED
    assert len(rig.sent("create_stop_order")) == 1  # 没有重发


async def test_an_unknown_stop_is_rejected_after_three_absences_and_the_signature_window(rig):
    await rig.start()
    rig.adapter.push_write_result("create_stop_order", unknown_write())
    rec = await rig.go(stop_place_spec())
    rig.adapter.set_read("fetch_algo_order", Known(None))
    await tick(rig, 1_000)
    await tick(rig, 1_000)
    await tick(rig, 1_000)  # 三次不存在，但请求发出才 3 秒
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING
    await tick(rig, 3_000)  # 6 秒，超过 5 秒窗口
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "回查" in done.state_reason
    assert len(rig.sent("create_stop_order")) == 1 and not rig.coordinator.key_busy(SYM, LONG)


async def test_an_unreadable_lookup_never_decides_for_an_unknown_stop(rig):
    await rig.start()
    rig.adapter.push_write_result("create_stop_order", unknown_write())
    rec = await rig.go(stop_place_spec())
    rig.adapter.set_read("fetch_algo_order", Unknown("限频"))
    for _ in range(5):
        await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_an_unknown_stop_that_was_cancelled_or_expired_meanwhile_is_rejected(rig):
    await rig.start()
    rig.adapter.push_write_result("create_stop_order", unknown_write())
    rec = await rig.go(stop_place_spec())
    order = await rig.only_order(rec.id)
    rig.adapter.set_read("fetch_algo_order", Known(algo_for(order, raw="EXPIRED")))
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.REJECTED


# ================================================================ STOP_REPLACE


async def placed_stop(rig, trigger: str = "45000", algo_id: str = "77"):
    rig.adapter.push_write_result("create_stop_order", algo_ok(algo_id=algo_id))
    rec = await rig.go(stop_place_spec(trigger))
    assert rec.state is S.COMPLETED
    return await rig.only_order(rec.id)


async def replace(rig, old_order, *, mode: str | None = "gap", new: str = "46000", old: str = "45000", **kw):
    return await rig.go(stop_replace_spec(new, old, old_cid=old_order.client_order_id, old_algo_id="77", mode=mode,
                                          **kw))


def result_of(rec) -> dict:
    return rec.params["result"]


async def test_gap_mode_cancels_the_old_stop_then_places_the_new_one(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", algo_ok(algo_id="78"))
    rec = await replace(rig, old)
    assert rec.state is S.COMPLETED
    assert rig.names()[1:] == ["cancel_algo_order", "create_stop_order"]  # 先撤后挂
    cancel = rig.sent("cancel_algo_order")[0]
    assert cancel["client_algo_id"] == old.client_order_id and cancel["algo_id"] == "77"
    new_call = rig.sent("create_stop_order")[1]
    assert new_call["trigger_price"] == D("46000") and new_call["position_side"] is LONG
    new_order = await rig.only_order(rec.id)
    assert ids.parse_client_id(new_order.client_order_id).leg == "s" and new_order.state is OrderState.ACKNOWLEDGED
    assert (await rig.orders.get(old.client_order_id)).state is OrderState.CANCELLED
    assert result_of(rec) == {
        "outcome": "replaced", "mode": "gap", "stop_in_place": True, "cleanup": [], "gap_ms": 0,
        "client_algo_id": new_order.client_order_id, "algo_id": "78", "trigger_price": "46000"}


async def test_gap_mode_persists_the_intent_and_the_new_order_row_before_cancelling_the_old_stop(rig):
    await rig.start()
    old = await placed_stop(rig)
    seen = []

    async def hook(name, kw):
        if name == "cancel_algo_order":
            pending = [r for r in await rig.orders.non_terminal(symbol=SYM) if r.state is OrderState.PENDING_SEND]
            intent = await rig.intents.get(pending[0].intent_id)
            seen.append((len(pending), pending[0].leg, pending[0].trigger_price, pending[0].sent_ms, intent.state))

    rig.adapter.hook = hook
    await replace(rig, old)
    assert seen == [(1, "s", D("46000"), None, S.DISPATCH_READY)]  # 撤旧单的那一刻，新单行与本意图都已落盘


async def test_the_stop_cancelled_before_new_crash_leaves_the_pending_new_order_on_disk(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.crash.arm("stop_cancelled_before_new")
    result = await rig.coordinator.submit(stop_replace_spec("46000", old_cid=old.client_order_id, old_algo_id="77"))
    with pytest.raises(CrashSignal) as raised:
        await rig.coordinator.wait_terminal(result.intent_id, 2.0)
    assert raised.value.name == "stop_cancelled_before_new"
    assert rig.names()[1:] == ["cancel_algo_order"]  # 旧单撤了，新单没发
    new_order = await rig.only_order(result.intent_id)
    assert new_order.state is OrderState.PENDING_SEND and new_order.sent_ms is None and new_order.trigger_price == D("46000")
    assert (await rig.orders.get(old.client_order_id)).state is OrderState.CANCELLED
    assert (await rig.intents.get(result.intent_id)).state is S.DISPATCH_READY


@pytest.mark.parametrize("outcome", [CancelOutcome.CANCELLED, CancelOutcome.NOT_FOUND])
async def test_a_cancel_that_found_the_old_stop_gone_or_cancelled_lets_the_new_one_go_ahead(rig, outcome):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(outcome))
    rec = await replace(rig, old)
    assert rec.state is S.COMPLETED and result_of(rec)["outcome"] == "replaced"


async def test_an_unknown_cancel_is_settled_by_looking_and_a_gone_old_stop_lets_the_new_one_go_ahead(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rig.adapter.set_read("fetch_algo_order", Known(None))
    rec = await replace(rig, old)
    assert rec.state is S.COMPLETED and result_of(rec)["outcome"] == "replaced"
    assert (await rig.orders.get(old.client_order_id)).state is OrderState.CANCEL_PENDING  # 旧单行等周期回查


async def test_an_unknown_cancel_with_the_old_stop_still_there_abandons_this_update(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rig.adapter.set_read("fetch_algo_order", Known(algo_for(old, algo_id="77")))
    rec = await replace(rig, old)
    assert rec.state is S.REJECTED and "放弃本次更新" in rec.state_reason and "旧止损仍在" in rec.state_reason
    assert result_of(rec)["outcome"] == "kept_old" and result_of(rec)["stop_in_place"] is True
    assert len(rig.sent("create_stop_order")) == 1  # 只有最初的那张；新单没有发
    new_order = await rig.only_order(rec.id)
    assert new_order.state is OrderState.REJECTED and new_order.sent_ms is None


async def test_an_unknown_cancel_that_cannot_be_looked_up_also_abandons_and_says_it_does_not_know(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rig.adapter.set_read("fetch_algo_order", Unknown("限频"))
    rec = await replace(rig, old)
    assert rec.state is S.REJECTED and result_of(rec)["outcome"] == "kept_old"
    assert result_of(rec)["stop_in_place"] is None
    assert len(rig.sent("create_stop_order")) == 1


async def test_a_rejected_new_stop_is_replaced_by_the_old_price(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", rejected(RejectKind.OTHER, -1111, "精度错误"))
    rig.adapter.push_write_result("create_stop_order", algo_ok(algo_id="79"))
    rec = await replace(rig, old)
    assert rig.names()[1:] == ["cancel_algo_order", "create_stop_order", "create_stop_order"]
    restore_call = rig.sent("create_stop_order")[2]
    assert restore_call["trigger_price"] == D("45000")  # 按旧止损价重挂
    rows = await rig.order_rows(rec.id)
    assert [(r.leg, r.state) for r in rows] == [("s", OrderState.REJECTED), ("s1", OrderState.ACKNOWLEDGED)]
    assert rows[1].resend_of == rows[0].client_order_id
    assert rec.state is S.REJECTED and "已按旧止损价重新挂上" in rec.state_reason and "-1111" in rec.state_reason
    assert result_of(rec)["outcome"] == "restored_old" and result_of(rec)["stop_in_place"] is True
    assert result_of(rec)["trigger_price"] == "45000" and result_of(rec)["client_algo_id"] == rows[1].client_order_id


async def test_when_the_old_price_is_crossed_too_the_result_is_stop_crossed(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", rejected(RejectKind.IMMEDIATE_TRIGGER, -2021, "会立即触发"))
    rig.adapter.push_write_result("create_stop_order", rejected(RejectKind.IMMEDIATE_TRIGGER, -2021, "会立即触发"))
    rec = await replace(rig, old)
    assert rec.state is S.REJECTED and result_of(rec)["outcome"] == "stop_crossed"
    assert result_of(rec)["stop_in_place"] is False
    assert len(rig.sent("create_stop_order")) == 3


async def test_when_the_old_price_cannot_be_replaced_either_nothing_is_protected(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", rejected(RejectKind.OTHER, -1111, "拒绝"))
    rig.adapter.push_write_result("create_stop_order", rejected(RejectKind.STOP_LIMIT_REACHED, -4045, "条件单太多"))
    rec = await replace(rig, old)
    assert rec.state is S.REJECTED and result_of(rec)["outcome"] == "unprotected" and result_of(rec)["stop_in_place"] is False
    assert "-4045" in rec.state_reason


async def test_a_rejected_new_stop_without_a_known_old_price_leaves_the_position_unprotected(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", rejected(RejectKind.OTHER, -1111, "拒绝"))
    s = stop_replace_spec("46000", old_cid=old.client_order_id, old_algo_id="77")
    s = dataclasses.replace(s, params={**s.params, "old": {"client_algo_id": old.client_order_id, "algo_id": "77"}})
    rec = await rig.go(s)
    assert rec.state is S.REJECTED and result_of(rec)["outcome"] == "unprotected"
    assert "没有旧止损价" in rec.state_reason and len(rig.sent("create_stop_order")) == 2


async def test_the_gap_limit_cuts_off_the_retry_when_the_new_stop_was_rejected_late(rig):
    await rig.start()
    old = await placed_stop(rig)

    async def hook(name, kw):
        if name == "create_stop_order":
            rig.clock.advance(31_000)  # 新单的请求拖了 31 秒（上限 30 秒）

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_stop_order", rejected(RejectKind.OTHER, -1111, "拒绝"))
    rec = await replace(rig, old)
    assert rec.state is S.REJECTED and result_of(rec)["outcome"] == "gap_exceeded"
    assert result_of(rec)["stop_in_place"] is False and result_of(rec)["gap_ms"] == 31_000
    assert len(rig.sent("create_stop_order")) == 2  # 不再重试一轮


async def test_a_new_stop_that_exists_only_after_the_gap_limit_is_reported_as_gap_exceeded(rig):
    await rig.start()
    old = await placed_stop(rig)

    async def hook(name, kw):
        if name == "create_stop_order":
            rig.clock.advance(30_001)

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_stop_order", algo_ok(algo_id="78"))
    rec = await replace(rig, old)
    assert rec.state is S.REJECTED and result_of(rec)["outcome"] == "gap_exceeded"
    assert result_of(rec)["stop_in_place"] is True and result_of(rec)["algo_id"] == "78"  # 止损在位，但间隙超了


async def test_the_time_spent_inside_the_cancel_request_counts_toward_the_gap(rig):
    """空窗从撤单请求发出算起：旧单最早此刻消失，撤单应答慢了也是无保护的时间。"""
    await rig.start()
    old = await placed_stop(rig)

    async def hook(name, kw):
        if name == "cancel_algo_order":
            rig.clock.advance(30_001)

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_stop_order", algo_ok(algo_id="79"))
    rec = await replace(rig, old)
    assert rec.state is S.REJECTED and result_of(rec)["outcome"] == "gap_exceeded"
    assert result_of(rec)["stop_in_place"] is True and result_of(rec)["gap_ms"] >= 30_001


async def test_exactly_the_gap_limit_is_still_within_it(rig):
    await rig.start()
    old = await placed_stop(rig)

    async def hook(name, kw):
        if name == "create_stop_order":
            rig.clock.advance(30_000)

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_stop_order", algo_ok(algo_id="78"))
    rec = await replace(rig, old)
    assert rec.state is S.COMPLETED and result_of(rec)["outcome"] == "replaced" and result_of(rec)["gap_ms"] == 30_000


async def test_the_gap_limit_comes_from_the_configuration(make_rig):
    rig = make_rig(stop_gap_max_s=3)
    await rig.start()
    old = await placed_stop(rig)

    async def hook(name, kw):
        if name == "create_stop_order":
            rig.clock.advance(3_001)

    rig.adapter.hook = hook
    rec = await replace(rig, old)
    assert result_of(rec)["outcome"] == "gap_exceeded"


async def test_a_restored_old_stop_that_arrives_after_the_gap_limit_is_gap_exceeded(rig):
    await rig.start()
    old = await placed_stop(rig)
    calls = []

    async def hook(name, kw):
        if name == "create_stop_order":
            calls.append(kw["trigger_price"])
            if len(calls) == 2:
                rig.clock.advance(31_000)  # 按旧价重挂的请求拖了很久

    rig.adapter.hook = hook
    rig.adapter.push_write_result("create_stop_order", rejected(RejectKind.OTHER, -1111, "拒绝"))
    rig.adapter.push_write_result("create_stop_order", algo_ok(algo_id="79"))
    rec = await replace(rig, old)
    assert result_of(rec)["outcome"] == "gap_exceeded" and result_of(rec)["stop_in_place"] is True
    assert result_of(rec)["trigger_price"] == "45000"


async def test_an_unknown_new_stop_is_reconciled_and_found(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", unknown_write("超时"))
    rec = await replace(rig, old)
    assert rec.state is S.RECONCILING and "按编号回查" in rec.state_reason
    new_order = await rig.only_order(rec.id)
    rig.adapter.set_read("fetch_algo_order", Known(algo_for(new_order, algo_id="78")))
    await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED
    assert result_of(done)["outcome"] == "replaced" and result_of(done)["algo_id"] == "78" and result_of(done)["cleanup"] == []


async def test_an_unknown_new_stop_that_never_existed_leaves_the_position_unprotected(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", unknown_write("超时"))
    rec = await replace(rig, old)
    rig.adapter.set_read("fetch_algo_order", Known(None))
    for _ in range(3):
        await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and result_of(done)["outcome"] == "unprotected" and result_of(done)["stop_in_place"] is False


async def test_an_unknown_restore_is_reconciled_to_restored_old(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", rejected(RejectKind.OTHER, -1111, "拒绝"))
    rig.adapter.push_write_result("create_stop_order", unknown_write("超时"))
    rec = await replace(rig, old)
    assert rec.state is S.RECONCILING
    restore = (await rig.order_rows(rec.id))[-1]
    assert restore.leg == "s1"
    rig.adapter.set_read("fetch_algo_order", Known(algo_for(restore, algo_id="79")))
    await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and result_of(done)["outcome"] == "restored_old" and result_of(done)["algo_id"] == "79"


async def test_the_mode_defaults_to_the_configured_one_and_unknown_modes_are_rejected(make_rig):
    rig = make_rig(ex_stop_update_mode="make_before_break")
    await rig.start()
    old = await placed_stop(rig)
    rec = await replace(rig, old, mode=None)
    assert rig.names()[1:] == ["create_stop_order", "cancel_algo_order"] and result_of(rec)["mode"] == "make_before_break"
    other = await replace(rig, old, mode="parallel")
    assert other.state is S.REJECTED and "不认识的移动止损更新方式" in other.state_reason


async def test_a_replace_without_old_stop_ids_is_rejected(rig):
    await rig.start()
    s = stop_replace_spec("46000")
    s = dataclasses.replace(s, params={**s.params, "old": {"trigger_price": "45000"}})
    rec = await rig.go(s)
    assert rec.state is S.REJECTED and "旧止损单的编号" in rec.state_reason and rig.names() == []


# ---------------------------------------------------------------- make_before_break


async def test_make_before_break_places_the_new_stop_then_cancels_the_old_one(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", algo_ok(algo_id="78"))
    rec = await replace(rig, old, mode="make_before_break")
    assert rec.state is S.COMPLETED
    assert rig.names()[1:] == ["create_stop_order", "cancel_algo_order"]  # 先建后撤
    new_order = await rig.only_order(rec.id)
    assert result_of(rec) == {"outcome": "replaced", "mode": "make_before_break", "stop_in_place": True, "cleanup": [],
                              "client_algo_id": new_order.client_order_id, "algo_id": "78", "trigger_price": "46000"}
    assert (await rig.orders.get(old.client_order_id)).state is OrderState.CANCELLED


async def test_make_before_break_keeps_both_stops_when_the_cancel_result_is_unknown(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rec = await replace(rig, old, mode="make_before_break")
    assert rec.state is S.COMPLETED and "待清理" in rec.state_reason
    assert result_of(rec)["cleanup"] == [{"client_algo_id": old.client_order_id, "algo_id": "77"}]  # 旧单待清理
    assert result_of(rec)["stop_in_place"] is True


async def test_make_before_break_treats_an_already_missing_old_stop_as_cleaned(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.NOT_FOUND))
    rec = await replace(rig, old, mode="make_before_break")
    assert rec.state is S.COMPLETED and result_of(rec)["cleanup"] == []


async def test_make_before_break_keeps_the_old_stop_when_the_new_one_is_rejected(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order",
                                  rejected(RejectKind.DUAL_CLOSE_POSITION, -4130, "同向已有全平型止损"))
    rec = await replace(rig, old, mode="make_before_break")
    assert rec.state is S.REJECTED and "-4130" in rec.state_reason
    assert result_of(rec)["outcome"] == "kept_old" and result_of(rec)["stop_in_place"] is True
    assert rig.sent("cancel_algo_order") == []  # 新单没挂上，旧单一动不动


async def test_make_before_break_with_an_unknown_new_stop_lists_the_old_one_for_cleanup_once_found(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", unknown_write("超时"))
    rec = await replace(rig, old, mode="make_before_break")
    assert rec.state is S.RECONCILING and rig.sent("cancel_algo_order") == []
    new_order = await rig.only_order(rec.id)
    rig.adapter.set_read("fetch_algo_order", Known(algo_for(new_order, algo_id="78")))
    await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED
    assert result_of(done)["cleanup"] == [{"client_algo_id": old.client_order_id, "algo_id": "77"}]


async def test_make_before_break_with_an_unknown_new_stop_that_never_existed_keeps_the_old_one(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("create_stop_order", unknown_write("超时"))
    rec = await replace(rig, old, mode="make_before_break")
    rig.adapter.set_read("fetch_algo_order", Known(None))
    for _ in range(3):
        await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and result_of(done)["outcome"] == "kept_old" and result_of(done)["stop_in_place"] is True


# ================================================================ STOP_CANCEL


async def test_stop_cancel_cancels_by_client_id_and_exchange_id_and_completes(rig):
    await rig.start()
    old = await placed_stop(rig)
    rec = await rig.go(stop_cancel_spec(old.client_order_id, "77"))
    assert rec.state is S.COMPLETED and "已撤销" in rec.state_reason
    call = rig.sent("cancel_algo_order")[0]
    assert call == {"symbol": SYM, "client_algo_id": old.client_order_id, "algo_id": "77"}
    assert (await rig.orders.get(old.client_order_id)).state is OrderState.CANCELLED


async def test_stop_cancel_of_a_missing_stop_is_also_completed(rig):
    await rig.start()
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.NOT_FOUND))
    rec = await rig.go(stop_cancel_spec())
    assert rec.state is S.COMPLETED and "已不存在" in rec.state_reason


async def test_stop_cancel_by_exchange_id_only_and_for_a_users_stop(rig):
    await rig.start()
    await rig.go(stop_cancel_spec(None, "9001"))
    await rig.go(stop_cancel_spec("web_abc", "9002"))  # 用户的单：编号不是系统编号，只能按交易所单号撤
    first, second = rig.sent("cancel_algo_order")
    assert first == {"symbol": SYM, "client_algo_id": None, "algo_id": "9001"}
    assert second == {"symbol": SYM, "client_algo_id": None, "algo_id": "9002"}


async def test_stop_cancel_without_any_id_is_rejected(rig):
    await rig.start()
    rec = await rig.go(stop_cancel_spec(None, None))
    assert rec.state is S.REJECTED and "缺少编号" in rec.state_reason and rig.names() == []


async def test_an_unknown_stop_cancel_is_settled_by_looking_for_the_order(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rec = await rig.go(stop_cancel_spec(old.client_order_id, "77"))
    assert rec.state is S.RECONCILING and rig.coordinator.key_busy(SYM, LONG)
    rig.adapter.set_read("fetch_algo_order", Known(None))
    await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "已不存在" in done.state_reason


async def test_a_stop_that_is_still_there_is_only_given_up_on_after_the_signature_window(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rec = await rig.go(stop_cancel_spec(old.client_order_id, "77"))
    rig.adapter.set_read("fetch_algo_order", Known(algo_for(old, algo_id="77")))
    await tick(rig, 5_000)  # 恰好 5 秒：撤单请求还可能生效
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING
    await tick(rig, 1_000)
    done = await rig.intents.get(rec.id)
    assert done.state is S.REJECTED and "仍然存在" in done.state_reason


async def test_an_unreadable_lookup_leaves_an_unknown_stop_cancel_alone(rig):
    await rig.start()
    old = await placed_stop(rig)
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))
    rec = await rig.go(stop_cancel_spec(old.client_order_id, "77"))
    rig.adapter.set_read("fetch_algo_order", Unknown("限频"))
    for _ in range(4):
        await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING
