"""T0.16b 协调者的三处后续改进。

1. 开仓前的残留条件单检查（F1 第 5 步）：该合约该方向上带系统前缀的全平型条件单（任何类型，含碎仓用的全平型止盈
   TAKE_PROFIT_MARKET）先撤并确认不存在；用户的止损类条件单、用户的全平型止盈单放弃这次开仓并留痕。
2. 未知的平仓单回查后确认不存在、但该侧持仓变了（非零）：按规格不重发，意图保持 RECONCILING，发一条告警
   （WARNING，kind=close_unknown_position_changed，dedupe_key 含意图号），只发一次。
3. ``stop()`` 之后再 ``start()``：抛 RuntimeError，不静默地不受理。
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

from dmr_kernel.core.coordinator import CoordinatorStopped
from dmr_kernel.core.types import (
    AlertLevel,
    AlgoType,
    CancelOutcome,
    CancelResult,
    IntentState,
    Known,
    OrderPurpose,
    OrderState,
    Unknown,
)
from dmr_kernel.exchange import ids

from coordinator_kit import (
    D, LONG, SHORT, SYM, algo_for, close_spec, fill_ok, partial_spec, position, spec, stop_algo, tick, unknown_write,
)

S = IntentState
USER_CID = "web_tp1"  # 用户自己下的单：没有系统前缀


# ================================================================ 公用小工具


def system_cid(rig, leg: str = "d", intent: int = 7) -> str:
    """系统编号（带系统前缀）。分腿 d 是碎仓用的全平型止盈，s 是止损。"""
    return ids.make_client_id(rig.instance, "0", intent, leg)


def resting(*, cid, algo_id: str = "9001", algo_type: AlgoType = AlgoType.TAKE_PROFIT_MARKET, symbol: str = SYM,
            side=LONG, whole: bool = True, raw: str = "NEW"):
    """交易所一侧挂着的一张条件单。``whole`` 为假就是带数量的（不是全平型）。"""
    base = stop_algo(symbol, side, cid=cid, algo_id=algo_id, algo_type=algo_type, raw=raw)
    return base if whole else dataclasses.replace(base, close_position=False, qty=D("0.002"))


def vanish_on_cancel(rig) -> None:
    """撤单请求一到，交易所一侧对应的条件单就没了（按系统编号撤）。"""

    async def hook(name, kw):
        if name == "cancel_algo_order":
            rig.algos = [a for a in rig.algos if a.client_algo_id != kw.get("client_algo_id")]

    rig.adapter.hook = hook


def no_exchange_activity(rig) -> None:
    assert rig.names() == []  # 没有任何写调用：用户的单一个字都不碰，也没有开仓


def changed_alerts(rig) -> list[dict]:
    return [a for a in rig.notifier.alerts if a["kind"] == "close_unknown_position_changed"]


# ================================================================ 一、开仓前的残留条件单：系统编号的全平型先撤


@pytest.mark.parametrize("algo_type", list(AlgoType), ids=lambda t: t.value)
async def test_a_system_whole_position_condition_of_any_type_is_cancelled_and_confirmed_gone_before_the_open(
        rig, algo_type):
    await rig.start()
    cid = system_cid(rig, "d")
    rig.algos = [resting(cid=cid, algo_type=algo_type)]
    vanish_on_cancel(rig)
    rec = await rig.go(spec())
    assert rec.state is S.ACTIVE
    assert rig.names() == ["cancel_algo_order", "create_market_order"]  # 先撤并确认不存在，再开仓
    assert rig.sent("cancel_algo_order")[0]["client_algo_id"] == cid


async def test_a_dust_take_profit_left_resting_after_the_position_vanished_is_cancelled_before_the_next_open(rig):
    """真实的碎仓条件单：碎仓平仓挂了全平型止盈，仓位随后被别的途径平掉，止盈单还留在交易所——它会作用到新开的仓上。"""
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.001")]  # 名义金额 50 < 最小名义金额 100
    close = await rig.go(close_spec())
    assert close.state is S.ACTIVE
    dust = await rig.only_order(close.id)
    assert dust.purpose is OrderPurpose.DUST_CLOSE and dust.order_type == "TAKE_PROFIT_MARKET"
    rig.positions = []  # 仓位被别的途径平掉了
    rig.algos = [algo_for(dust)]  # 全平型止盈单还留在交易所
    vanish_on_cancel(rig)
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.CANCELLED))
    rec = await rig.go(spec())
    assert rec.state is S.ACTIVE
    assert rig.names() == ["create_stop_order", "cancel_algo_order", "create_market_order"]
    assert rig.sent("cancel_algo_order")[0]["client_algo_id"] == dust.client_order_id
    assert (await rig.orders.get(dust.client_order_id)).state is OrderState.CANCELLED  # 撤单结果记进了订单行
    assert (await rig.intents.get(close.id)).state is S.REJECTED  # 碎仓意图随之结束：条件单被撤销，没有成交


async def test_open_gives_up_when_the_cancelled_system_take_profit_is_still_there(rig):
    await rig.start()
    cid = system_cid(rig, "d")
    rig.algos = [resting(cid=cid)]  # 撤单「成功」了，但条件单读数里还在
    rec = await rig.go(spec())
    assert rec.state is S.REJECTED and "仍然存在" in rec.state_reason and cid in rec.state_reason
    assert rig.names() == ["cancel_algo_order"]  # 撤过，但没有开仓


async def test_every_system_residual_on_that_side_is_cancelled_a_stop_and_a_take_profit_together(rig):
    await rig.start()
    stop_cid, dust_cid = system_cid(rig, "s", 5), system_cid(rig, "d", 6)
    rig.algos = [resting(cid=stop_cid, algo_id="1", algo_type=AlgoType.STOP_MARKET),
                 resting(cid=dust_cid, algo_id="2")]
    vanish_on_cancel(rig)
    rec = await rig.go(spec())
    assert rec.state is S.ACTIVE
    assert [c["client_algo_id"] for c in rig.sent("cancel_algo_order")] == [stop_cid, dust_cid]
    assert rig.names() == ["cancel_algo_order", "cancel_algo_order", "create_market_order"]


async def test_open_gives_up_when_the_conditional_orders_cannot_be_read_after_cancelling_a_take_profit(rig):
    await rig.start()
    rig.algos = [resting(cid=system_cid(rig, "d"))]

    async def hook(name, kw):
        if name == "cancel_algo_order":
            rig.adapter.set_read("fetch_open_algo_orders", Unknown("断连"))

    rig.adapter.hook = hook
    rec = await rig.go(spec())
    assert rec.state is S.REJECTED and "条件单读数未知" in rec.state_reason
    assert rig.sent("create_market_order") == []


# ================================================================ 一、开仓前的残留条件单：用户的放弃并留痕


@pytest.mark.parametrize("algo_type", [AlgoType.TAKE_PROFIT_MARKET, AlgoType.TAKE_PROFIT], ids=lambda t: t.value)
@pytest.mark.parametrize("cid", [USER_CID, None], ids=["with_id", "no_id"])
async def test_a_users_whole_position_take_profit_makes_the_open_give_up_and_is_left_alone(rig, algo_type, cid):
    await rig.start()
    rig.algos = [resting(cid=cid, algo_id="9003", algo_type=algo_type)]
    rec = await rig.go(spec())
    assert rec.state is S.REJECTED
    reason = rec.state_reason  # 留痕：谁的单、什么类型、哪张
    assert "用户自己的" in reason and "9003" in reason and algo_type.value in reason
    assert (cid or "无编号") in reason
    no_exchange_activity(rig)


@pytest.mark.parametrize("whole", [True, False], ids=["whole_position", "with_quantity"])
@pytest.mark.parametrize("algo_type", [AlgoType.STOP_MARKET, AlgoType.STOP, AlgoType.TRAILING_STOP_MARKET],
                         ids=lambda t: t.value)
async def test_a_users_stop_kind_condition_still_makes_the_open_give_up_whatever_its_form(rig, algo_type, whole):
    await rig.start()
    rig.algos = [resting(cid=USER_CID, algo_id="9004", algo_type=algo_type, whole=whole)]
    rec = await rig.go(spec())
    assert rec.state is S.REJECTED and "用户自己的" in rec.state_reason and "9004" in rec.state_reason
    no_exchange_activity(rig)


async def test_a_users_whole_position_take_profit_in_the_triggering_state_still_blocks(rig):
    await rig.start()
    rig.algos = [resting(cid=USER_CID, algo_id="9005", raw="TRIGGERING")]
    rec = await rig.go(spec())
    assert rec.state is S.REJECTED and "用户自己的" in rec.state_reason
    no_exchange_activity(rig)


async def test_a_users_whole_position_take_profit_blocks_the_open_of_a_short_too(rig):
    await rig.start()
    rig.algos = [resting(cid=USER_CID, algo_id="9006", side=SHORT)]
    rec = await rig.go(spec(side=SHORT))
    assert rec.state is S.REJECTED and "用户自己的" in rec.state_reason and "9006" in rec.state_reason
    no_exchange_activity(rig)


async def test_a_user_condition_abandons_the_open_before_any_system_residual_is_cancelled(rig):
    await rig.start()
    dust_cid = system_cid(rig, "d")
    rig.algos = [resting(cid=dust_cid, algo_id="5551"), resting(cid=USER_CID, algo_id="5552")]
    vanish_on_cancel(rig)
    rec = await rig.go(spec())
    assert rec.state is S.REJECTED and "用户自己的" in rec.state_reason
    assert "5552" in rec.state_reason and "5551" not in rec.state_reason  # 留痕里只写用户的那张
    no_exchange_activity(rig)  # 这次开仓放弃了，系统自己的残留单也不动（下一次开仓再清）
    assert [a.client_algo_id for a in rig.algos] == [dust_cid, USER_CID]


# ================================================================ 一、开仓前的残留条件单：不拦的


@pytest.mark.parametrize("cid_kind", ["user", "system"])
@pytest.mark.parametrize("algo_type", [AlgoType.TAKE_PROFIT_MARKET, AlgoType.TAKE_PROFIT], ids=lambda t: t.value)
async def test_a_take_profit_that_is_not_a_whole_position_order_blocks_nothing_and_is_left_alone(
        rig, algo_type, cid_kind):
    await rig.start()
    cid = USER_CID if cid_kind == "user" else system_cid(rig, "d")
    rig.algos = [resting(cid=cid, algo_type=algo_type, whole=False)]
    rec = await rig.go(spec())
    assert rec.state is S.ACTIVE
    assert rig.sent("cancel_algo_order") == []


@pytest.mark.parametrize("cid_kind", ["user", "system"])
@pytest.mark.parametrize("algo_type", list(AlgoType), ids=lambda t: t.value)
async def test_whole_position_conditions_on_the_other_side_or_another_symbol_are_ignored(rig, algo_type, cid_kind):
    await rig.start()
    cids = ([USER_CID + "a", USER_CID + "b"] if cid_kind == "user"
            else [system_cid(rig, "d", 8), system_cid(rig, "d", 9)])
    rig.algos = [resting(cid=cids[0], algo_id="1", algo_type=algo_type, side=SHORT),
                 resting(cid=cids[1], algo_id="2", algo_type=algo_type, symbol="ETHUSDT")]
    rec = await rig.go(spec())
    assert rec.state is S.ACTIVE
    assert rig.sent("cancel_algo_order") == []


@pytest.mark.parametrize("raw", ["FINISHED", "CANCELED", "EXPIRED", "REJECTED", "TRIGGERED"])
async def test_conditions_that_are_no_longer_open_never_block(rig, raw):
    await rig.start()
    rig.algos = [resting(cid=USER_CID, algo_id="1", raw=raw),
                 resting(cid=system_cid(rig, "d"), algo_id="2", raw=raw)]
    rec = await rig.go(spec())
    assert rec.state is S.ACTIVE
    assert rig.sent("cancel_algo_order") == []


# ================================================================ 二、未知的平仓单：确认不存在但持仓变了


async def unknown_close(rig, intent_spec=None, *, qty: str = "0.01"):
    """发出一张平仓单，结果未知；之后交易所一侧「查不到」这张单。"""
    await rig.start()
    rig.positions = [position(SYM, LONG, qty)]
    rig.adapter.push_write_result("create_market_order", unknown_write("请求超时"))
    rec = await rig.go(intent_spec or close_spec())
    assert rec.state is S.RECONCILING
    assert (await rig.only_order(rec.id)).state is OrderState.SENT_UNKNOWN
    rig.adapter.set_read("fetch_order", Known(None))
    return rec


async def test_a_confirmed_absent_close_with_a_changed_position_is_not_resent_and_raises_one_warning(rig):
    rec = await unknown_close(rig)
    await tick(rig)
    await tick(rig)
    assert changed_alerts(rig) == []  # 才查到不存在 2 次，还没到下结论的时候
    rig.positions = [position(SYM, LONG, "0.007")]  # 第三次回查时持仓已经变了
    await tick(rig)
    [alert] = changed_alerts(rig)
    assert alert["level"] is AlertLevel.WARNING
    assert alert["dedupe_key"] is not None and str(rec.id) in alert["dedupe_key"]
    message = alert["message"]
    assert str(rec.id) in message and "人工核对" in message
    assert "0.007" in message and "0.01" in message  # 现在的持仓与发单时的持仓
    assert len(rig.sent("create_market_order")) == 1  # 按规格不重发
    still = await rig.intents.get(rec.id)
    assert still.state is S.RECONCILING and rig.coordinator.key_busy(SYM, LONG)  # 意图保持 RECONCILING


async def test_the_warning_is_raised_only_once_however_long_the_position_stays_changed(rig):
    rec = await unknown_close(rig)
    rig.positions = [position(SYM, LONG, "0.007")]
    for _ in range(8):
        await tick(rig)
    assert len(changed_alerts(rig)) == 1  # 通知器的假实现不去重：只发一次是协调者自己保证的
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING
    assert len(rig.sent("create_market_order")) == 1


async def test_each_intent_gets_its_own_warning_with_its_own_id_in_the_dedupe_key(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01"), position(SYM, SHORT, "0.01")]
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rig.adapter.push_write_result("create_market_order", unknown_write())
    long_rec, short_rec = await rig.go_all(close_spec(side=LONG), close_spec(side=SHORT))
    assert long_rec.state is S.RECONCILING and short_rec.state is S.RECONCILING
    rig.adapter.set_read("fetch_order", Known(None))
    rig.positions = [position(SYM, LONG, "0.007"), position(SYM, SHORT, "0.007")]
    for _ in range(5):
        await tick(rig)
    keys = [a["dedupe_key"] for a in changed_alerts(rig)]
    assert len(keys) == 2 and len(set(keys)) == 2  # 一个意图一条，键不同
    assert any(str(long_rec.id) in key for key in keys) and any(str(short_rec.id) in key for key in keys)


async def test_no_warning_before_the_absence_is_confirmed(rig):
    rec = await unknown_close(rig)
    rig.positions = [position(SYM, LONG, "0.007")]  # 一开始持仓就变了
    await tick(rig)
    await tick(rig)
    assert changed_alerts(rig) == []  # 连续不存在才 2 次
    await tick(rig)
    assert len(changed_alerts(rig)) == 1  # 第 3 次
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_no_warning_before_the_signature_window_has_passed(rig):
    await unknown_close(rig)
    rig.positions = [position(SYM, LONG, "0.007")]
    for _ in range(5):  # 每轮 1 秒：第 3 轮就有 3 次不存在，但请求发出还不到 5 秒
        await tick(rig, 1_000)
    assert changed_alerts(rig) == []  # 恰好 5 秒不算超过
    await tick(rig, 1_000)
    assert len(changed_alerts(rig)) == 1


async def test_no_warning_while_the_order_lookups_are_unreadable(rig):
    rec = await unknown_close(rig)
    rig.adapter.set_read("fetch_order", Unknown("查询超时"))
    rig.positions = [position(SYM, LONG, "0.007")]
    for _ in range(5):
        await tick(rig)
    assert changed_alerts(rig) == []  # 没有「确认不存在」，也就谈不上「持仓变了」的结论
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_no_warning_while_the_position_cannot_be_read(rig):
    await unknown_close(rig)
    rig.adapter.set_read("fetch_positions", Unknown("限频"))
    for _ in range(5):
        await tick(rig)
    assert changed_alerts(rig) == []


async def test_no_warning_when_the_position_is_unchanged_and_the_close_is_resent_as_before(rig):
    rec = await unknown_close(rig)
    rig.adapter.push_write_result("create_market_order", fill_ok())
    for _ in range(3):
        await tick(rig)
    assert len(rig.sent("create_market_order")) == 2
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED
    assert rig.notifier.alerts == []


async def test_no_warning_when_the_position_is_gone_the_close_is_completed_instead(rig):
    rec = await unknown_close(rig)
    await tick(rig)
    await tick(rig)
    rig.positions = []
    await tick(rig)
    done = await rig.intents.get(rec.id)
    assert done.state is S.COMPLETED and "已由其它途径平掉" in done.state_reason
    assert changed_alerts(rig) == []


async def test_after_the_warning_a_position_that_is_unchanged_again_lets_the_resend_go_ahead_without_a_second_one(rig):
    rec = await unknown_close(rig)
    rig.positions = [position(SYM, LONG, "0.007")]
    for _ in range(3):
        await tick(rig)
    assert len(changed_alerts(rig)) == 1 and len(rig.sent("create_market_order")) == 1
    rig.positions = [position(SYM, LONG, "0.01")]  # 人工核对后持仓回到发单时的数量
    rig.adapter.push_write_result("create_market_order", fill_ok())
    await tick(rig)
    assert len(rig.sent("create_market_order")) == 2
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED
    assert len(changed_alerts(rig)) == 1


async def test_after_the_warning_a_flat_position_completes_the_intent_and_the_key_is_free_again(rig):
    rec = await unknown_close(rig)
    rig.positions = [position(SYM, LONG, "0.007")]
    for _ in range(3):
        await tick(rig)
    assert rig.coordinator.key_busy(SYM, LONG)
    rig.positions = []  # 操作员核对后把剩下的仓位平掉了
    await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.COMPLETED
    assert not rig.coordinator.key_busy(SYM, LONG)
    assert len(changed_alerts(rig)) == 1


async def test_a_close_that_must_not_be_resent_stays_reconciling_and_warns_instead_of_being_rejected(rig):
    rec = await unknown_close(rig, close_spec(params={"resend_on_unknown": False}))
    rig.positions = [position(SYM, LONG, "0.007")]
    for _ in range(4):
        await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING  # 持仓没变才会被拒；变了就留给人
    assert len(changed_alerts(rig)) == 1 and len(rig.sent("create_market_order")) == 1


async def test_the_warning_also_covers_the_same_id_resend_configuration(make_rig):
    rig = make_rig(ex_resend_client_id="same")
    rec = await unknown_close(rig)
    rig.positions = [position(SYM, LONG, "0.007")]
    for _ in range(4):
        await tick(rig)
    assert len(changed_alerts(rig)) == 1 and len(rig.sent("create_market_order")) == 1
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_an_unknown_partial_close_with_a_changed_position_also_warns_once_and_is_never_resent(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(partial_spec("0.004"))
    assert rec.state is S.RECONCILING
    rig.adapter.set_read("fetch_order", Known(None))
    rig.positions = [position(SYM, LONG, "0.006")]  # 仓位少了 0.004：这张单可能其实成交了
    for _ in range(6):
        await tick(rig)
    [alert] = changed_alerts(rig)
    assert alert["level"] is AlertLevel.WARNING and str(rec.id) in alert["dedupe_key"]
    assert "部分平仓" in alert["message"] and "0.006" in alert["message"]
    assert len(rig.sent("create_market_order")) == 1
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_an_unknown_dust_condition_with_a_changed_position_also_warns_once_and_is_not_placed_again(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.001")]
    rig.adapter.push_write_result("create_stop_order", unknown_write())
    rec = await rig.go(close_spec())
    assert rec.state is S.RECONCILING
    assert (await rig.only_order(rec.id)).state is OrderState.SENT_UNKNOWN
    rig.adapter.set_read("fetch_algo_order", Known(None))
    rig.positions = [position(SYM, LONG, "0.002")]  # 发条件单时是 0.001，现在变了
    for _ in range(6):
        await tick(rig)
    [alert] = changed_alerts(rig)
    assert str(rec.id) in alert["dedupe_key"] and "碎仓" in alert["message"]
    assert len(rig.sent("create_stop_order")) == 1
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING


async def test_a_coordinator_without_a_notifier_still_holds_the_intent_without_failing(make_rig):
    rig = make_rig()
    rig.coordinator = rig.build_coordinator(notifier=None)
    rec = await unknown_close(rig)
    rig.positions = [position(SYM, LONG, "0.007")]
    for _ in range(4):
        await tick(rig)
    assert (await rig.intents.get(rec.id)).state is S.RECONCILING
    assert len(rig.sent("create_market_order")) == 1


# ================================================================ 三、stop() 之后不能再 start()

RESTART_REFUSED = "协调者停止后不能重新启动，请重建实例"


async def test_start_after_stop_raises_a_clear_runtime_error(rig):
    await rig.start()
    await rig.coordinator.stop(0.1)
    with pytest.raises(RuntimeError, match=RESTART_REFUSED):
        await rig.coordinator.start()


async def test_the_refusal_is_not_the_benign_shutdown_signal_that_callers_may_swallow(rig):
    await rig.start()
    await rig.coordinator.stop(0.1)
    with pytest.raises(RuntimeError) as refused:
        await rig.coordinator.start()
    assert not isinstance(refused.value, CoordinatorStopped)  # 关停期间「不再受理」可能被成片捕获；误用不该被它吞掉


async def test_a_refused_restart_leaves_the_coordinator_stopped_and_inert(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(spec())
    assert rec.state is S.RECONCILING
    await rig.coordinator.stop(0.1)
    with pytest.raises(RuntimeError):
        await rig.coordinator.start()
    with pytest.raises(CoordinatorStopped):
        await rig.coordinator.submit(spec(symbol="ETHUSDT"))
    rig.adapter.set_read("fetch_order", Known(None))
    rig.clock.advance(60_000)
    await rig.coordinator.resolve_unknown_orders()
    assert rig.adapter.calls("fetch_order") == []  # 没有悄悄重新订阅、重新回查


async def test_start_after_a_repeated_stop_is_refused_too(rig):
    await rig.start()
    await rig.coordinator.stop(0.1)
    await rig.coordinator.stop(0.1)
    with pytest.raises(RuntimeError, match=RESTART_REFUSED):
        await rig.coordinator.start()


async def test_stop_before_any_start_also_spends_the_instance(rig):
    await rig.coordinator.stop(0.1)
    with pytest.raises(RuntimeError, match=RESTART_REFUSED):
        await rig.coordinator.start()


async def test_start_while_stop_is_still_draining_is_refused(rig):
    await rig.start()
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hook(name, kw):
        entered.set()
        await gate.wait()

    rig.adapter.hook = hook
    result = await rig.coordinator.submit(spec())
    await asyncio.wait_for(entered.wait(), 2)
    stopper = asyncio.create_task(rig.coordinator.stop(5.0))
    await asyncio.sleep(0.05)
    assert not stopper.done()  # 还在等在途的派发步骤
    with pytest.raises(RuntimeError, match=RESTART_REFUSED):
        await rig.coordinator.start()
    gate.set()
    await asyncio.wait_for(stopper, 2)
    assert (await rig.intents.get(result.intent_id)).state is S.ACTIVE  # 在途的这一步照常做完


async def test_a_start_that_was_never_stopped_is_still_harmless_when_repeated(rig):
    await rig.start()
    await rig.coordinator.start()
    rec = await rig.go(spec())
    assert rec.state is S.ACTIVE


async def test_a_rebuilt_coordinator_takes_over_the_unfinished_intents_of_the_stopped_one(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", unknown_write())
    first = await rig.go(spec())
    assert first.state is S.RECONCILING
    await rig.coordinator.stop(0.1)
    with pytest.raises(RuntimeError, match=RESTART_REFUSED):
        await rig.coordinator.start()
    rig.coordinator = rig.build_coordinator()  # 按提示重建实例
    await rig.coordinator.start()
    assert rig.coordinator.key_busy(SYM, LONG)  # 账本里未到终态的意图由新实例接管
    rig.adapter.set_read("fetch_order", Known(None))
    second = await rig.go(spec(symbol="ETHUSDT"))
    assert second.state is S.ACTIVE
    assert len(rig.sent("create_market_order")) == 2  # 第一个意图没有被重发
