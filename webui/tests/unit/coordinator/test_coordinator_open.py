"""开仓意图（F1 第 5–9 步）：准入段、分键段的每个放弃分支，数量取整，发单结果的三种走向。"""
from __future__ import annotations

import asyncio

import pytest

from dmr_kernel.core.coordinator import EXPIRED_REASON
from dmr_kernel.core.types import (
    AlertLevel,
    CancelOutcome,
    CancelResult,
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    LeverageInfo,
    OpLock,
    OrderOrigin,
    OrderPurpose,
    OrderState,
    RejectKind,
    RiskDecision,
    Suppression,
    Unknown,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.errors import AuthConfigError

from coordinator_kit import (
    BUY, D, LONG, SELL, SHORT, SYM, ack_ok, fill_ok, order_view, position, rejected, spec,
    stop_algo, unknown_write,
)


def no_exchange_activity(rig) -> None:
    assert rig.names() == []  # 没有任何写调用


# ================================================================ 正常路径


async def test_open_sends_one_market_order_with_the_notional_divided_by_the_last_price(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", fill_ok())
    rec = await rig.go(spec())
    assert rec.state is IntentState.COMPLETED
    call = rig.sent("create_market_order")[0]
    assert (call["symbol"], call["side"], call["position_side"]) == (SYM, BUY, LONG)
    assert call["qty"] == D("0.002")  # 100 USDT / 50000
    parsed = ids.parse_client_id(call["client_order_id"])
    assert parsed.origin is OrderOrigin.SYSTEM and parsed.model_code == "1" and parsed.leg == "o"
    assert parsed.intent_id == rec.id and parsed.instance == rig.instance
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.FILLED and order.purpose is OrderPurpose.OPEN and order.qty == D("0.002")
    assert rig.sent("set_leverage") == [] and rig.sent("set_margin_mode") == []  # 杠杆与保证金模式已经符合配置
    assert [(c["model"], c["action"]) for c in rig.riskgate.calls] == [("m1", IntentAction.OPEN)]
    assert rig.list_calls == [(SYM, LONG)]


async def test_open_short_sells_on_the_short_side(rig):
    await rig.start()
    rec = await rig.go(spec(side=SHORT))
    call = rig.sent("create_market_order")[0]
    assert (call["side"], call["position_side"]) == (SELL, SHORT)
    assert rec.state is IntentState.ACTIVE


async def test_the_quantity_is_rounded_down_to_the_market_step(rig):
    await rig.start()
    rec = await rig.go(spec(symbol="ETHUSDT"))
    assert rec.state is IntentState.ACTIVE
    assert rig.sent("create_market_order")[0]["qty"] == D("0.033")  # 100 / 3000 = 0.0333...


async def test_the_intent_may_carry_its_own_notional(rig):
    await rig.start()
    await rig.go(spec(params={"notional": "250"}))
    assert rig.sent("create_market_order")[0]["qty"] == D("0.005")  # 250 / 50000


async def test_an_intent_that_was_reserved_releases_its_slot_when_it_ends(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.INSUFFICIENT_MARGIN, -2019, "保证金不足"))
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 0


async def test_the_slot_stays_reserved_while_the_intent_is_still_running(rig):
    await rig.start()
    rec = await rig.go(spec())  # 受理但未成交：ACTIVE
    assert rec.state is IntentState.ACTIVE
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 1


# ================================================================ 准入段：未过期


async def test_an_expired_intent_is_never_sent(rig):
    await rig.start()
    reads_before = len(rig.adapter.read_calls)  # 启动时刷新过合约规则
    s = spec(ttl_s=5)
    created, _ = await rig.intents.create(s)  # 只落盘，不派发
    rig.clock.advance(5_000)  # 恰好到期（now >= expires_ms）
    result = await rig.coordinator.submit(s)
    await rig.settle()
    rec = await rig.intents.get(result.intent_id)
    assert result.created is False
    assert rec.state is IntentState.EXPIRED and rec.state_reason == EXPIRED_REASON
    no_exchange_activity(rig)
    assert len(rig.adapter.read_calls) == reads_before  # 连交易所的读都不做


async def test_an_intent_that_is_one_millisecond_short_of_expiry_is_still_sent(rig):
    await rig.start()
    s = spec(ttl_s=5)
    await rig.intents.create(s)
    rig.clock.advance(4_999)
    rig.refresh_limits()
    await rig.coordinator.submit(s)
    await rig.settle()
    assert len(rig.sent("create_market_order")) == 1


async def test_an_intent_that_expires_while_queued_behind_another_is_expired_not_sent(rig):
    await rig.start()
    rig.positions = [position(SYM, LONG, "0.01")]
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hook(name, kw):
        entered.set()
        await gate.wait()

    rig.adapter.hook = hook
    from coordinator_kit import close_spec

    await rig.coordinator.submit(close_spec())  # 占着 BTCUSDT 多头这个键
    await asyncio.wait_for(entered.wait(), 2)
    queued = await rig.coordinator.submit(spec(ttl_s=10))  # 排在后面
    await asyncio.sleep(0.05)
    rig.clock.advance(11_000)  # 排队期间过期
    gate.set()
    await rig.settle()
    rec = await rig.intents.get(queued.intent_id)
    assert rec.state is IntentState.EXPIRED
    assert len(rig.sent("create_market_order")) == 1  # 只有平仓单发出过
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 0


# ================================================================ 准入段：模型状态与名单


async def test_open_is_refused_when_the_gate_is_stopped(rig):
    await rig.start(running=False)
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "开仓闸已停止" in rec.state_reason
    no_exchange_activity(rig)
    assert rig.adapter.calls("fetch_positions") == []


async def test_open_is_refused_when_there_is_a_suppression_reason(rig):
    await rig.start()
    await rig.modelstate.add_suppression(Suppression.UNPROTECTED, reason="测试")
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "抑制原因" in rec.state_reason
    no_exchange_activity(rig)


async def test_open_is_refused_when_an_operation_lock_is_held(rig):
    await rig.start()
    await rig.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner="test")
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "操作锁" in rec.state_reason
    no_exchange_activity(rig)


@pytest.mark.parametrize("model, expected", [("m2", "不是生效模型"), ("m3", "只预留"), ("zzz", "不认识的模型")])
async def test_open_is_refused_for_a_model_that_is_not_the_active_one(rig, model, expected):
    await rig.start()
    rec = await rig.go(spec(model=model))
    assert rec.state is IntentState.REJECTED and expected in rec.state_reason
    no_exchange_activity(rig)


async def test_model_one_must_pass_the_list_check(rig):
    await rig.start()
    rig.list_result = (False, "名单已失效")
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "名单已失效" in rec.state_reason
    assert rig.list_calls == [(SYM, LONG)]
    no_exchange_activity(rig)


async def test_a_failing_list_check_rejects_instead_of_crashing(rig):
    await rig.start()
    rig.list_result = RuntimeError("名单服务出错")
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "名单复核出错" in rec.state_reason
    no_exchange_activity(rig)


async def test_without_a_list_check_the_model_one_open_goes_ahead(ledger, clock):
    from coordinator_kit import Rig

    rig = Rig(ledger, clock)
    rig.coordinator = rig.build_coordinator(list_check=None)
    await rig.start()
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE


async def test_the_list_check_is_only_for_model_one(rig):
    await rig.start()
    rec = await rig.go(spec(model="m2"))  # 模型 2 不是生效模型，被可开仓判定拦下，名单复核根本没被问到
    assert rec.state is IntentState.REJECTED
    assert rig.list_calls == []


# ================================================================ 准入段：订单频率与合约数上限


async def test_open_is_refused_when_the_symbol_already_hit_the_order_rate_limit(make_rig):
    rig = make_rig(max_orders_per_symbol_10m=2)
    await rig.start()
    rig.limits.note_order(SYM, rig.clock.now_ms() - 1000)
    rig.limits.note_order(SYM, rig.clock.now_ms() - 500)
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "10 分钟" in rec.state_reason and "上限" in rec.state_reason
    no_exchange_activity(rig)
    other = await rig.go(spec(symbol="ETHUSDT"))  # 别的合约不受影响
    assert other.state is IntentState.ACTIVE


async def test_the_order_rate_window_slides(make_rig):
    rig = make_rig(max_orders_per_symbol_10m=1)
    await rig.start()
    rig.limits.note_order(SYM, rig.clock.now_ms())
    assert (await rig.go(spec())).state is IntentState.REJECTED
    rig.clock.advance(600_000)  # 窗口左开右闭 (now-10 分钟, now]：恰好 10 分钟时旧订单已出窗口
    rig.refresh_limits()
    assert (await rig.go(spec())).state is IntentState.ACTIVE


async def test_every_order_sent_is_counted_for_the_rate_limit(make_rig):
    rig = make_rig(max_orders_per_symbol_10m=1)
    await rig.start()
    first = await rig.go(spec())
    assert first.state is IntentState.ACTIVE
    second = await rig.go(spec(side=SHORT))  # 同一合约，10 分钟内第二笔
    assert second.state is IntentState.REJECTED and "10 分钟" in second.state_reason


async def test_open_is_refused_when_the_symbol_cap_is_reached(make_rig):
    rig = make_rig(max_symbols=1)
    await rig.start()
    rig.positions = [position("ETHUSDT", LONG, "0.5", "3000")]
    rig.refresh_limits()
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "已达持仓上限" in rec.state_reason
    no_exchange_activity(rig)
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 0  # 被拒绝的不占名额


async def test_open_is_refused_when_the_snapshots_are_stale(rig):
    await rig.start()
    rig.clock.advance(25_000)  # 持仓快照 20 秒没更新就过期
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "快照过期" in rec.state_reason
    no_exchange_activity(rig)


async def test_the_degraded_run_mode_uses_the_lower_cap(make_rig):
    from dmr_kernel.core.types import RunMode

    rig = make_rig(max_symbols=30, max_symbols_degraded=1)
    await rig.start()
    await rig.modelstate.set_run_mode(RunMode.POLLING_DEGRADED, reason="测试")
    rig.positions = [position("ETHUSDT", LONG, "0.5", "3000")]
    rig.refresh_limits()
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "已达持仓上限" in rec.state_reason


async def test_several_opens_on_the_same_bar_cannot_jump_the_cap_together(make_rig):
    """同一根 K 线上的多个开仓意图：名额在串行的准入段里一个个占，不会一起越过上限。"""
    rig = make_rig(max_symbols=2)
    await rig.start()
    gate = asyncio.Event()
    entered = []

    async def hook(name, kw):
        entered.append(kw["symbol"])
        await gate.wait()  # 先发出的两张单卡在交易所里，第三个意图的准入发生在它们占着名额的时候

    rig.adapter.hook = hook
    bar = 1_760_000_000_000
    first = await rig.coordinator.submit(spec(symbol="BTCUSDT", idem_key=f"open:m1:BTCUSDT:LONG:{bar}"))
    second = await rig.coordinator.submit(spec(symbol="ETHUSDT", idem_key=f"open:m1:ETHUSDT:LONG:{bar}"))
    third = await rig.coordinator.submit(spec(symbol="DOGEUSDT", idem_key=f"open:m1:DOGEUSDT:LONG:{bar}"))
    fourth = await rig.coordinator.submit(spec(symbol="SOLUSDT", idem_key=f"open:m1:SOLUSDT:LONG:{bar}"))
    await asyncio.sleep(0.2)
    gate.set()
    await rig.settle()
    recs = [await rig.intents.get(r.intent_id) for r in (first, second, third, fourth)]
    assert [r.state for r in recs] == [IntentState.ACTIVE, IntentState.ACTIVE, IntentState.REJECTED,
                                       IntentState.REJECTED]  # 排在前面的先占名额
    assert all("已达持仓上限" in r.state_reason for r in recs[2:])
    assert sorted(entered) == ["BTCUSDT", "ETHUSDT"]


async def test_a_rejected_open_frees_its_slot_for_the_next_one(make_rig):
    rig = make_rig(max_symbols=1)
    await rig.start()
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.OTHER, -1111, "精度错误"))
    first, second = await rig.go_all(spec(symbol="BTCUSDT"), spec(symbol="ETHUSDT"))
    assert first.state is IntentState.REJECTED
    # 两个意图依次进准入段：第二个看到第一个还占着名额，被拒；第一个结束后名额释放，第三个可以占
    third = await rig.go(spec(symbol="ETHUSDT"))
    assert third.state is IntentState.ACTIVE


# ================================================================ 分键段：即时持仓


async def test_open_gives_up_when_the_live_position_read_is_unknown(rig):
    await rig.start()
    rig.adapter.set_read("fetch_positions", Unknown("连接超时"))
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "持仓读数未知" in rec.state_reason and "连接超时" in rec.state_reason
    no_exchange_activity(rig)


@pytest.mark.parametrize("held_side", [LONG, SHORT])
async def test_open_gives_up_when_either_side_already_holds_a_position(rig, held_side):
    await rig.start()
    rig.positions = [position(SYM, held_side, "0.01")]
    rec = await rig.go(spec(side=LONG))
    assert rec.state is IntentState.REJECTED and "已有持仓" in rec.state_reason
    no_exchange_activity(rig)


async def test_a_zero_quantity_entry_or_another_symbol_is_not_a_position(rig):
    await rig.start()
    rig.positions = [position(SYM, SHORT, "0"), position("ETHUSDT", LONG, "1", "3000")]
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE


# ================================================================ 分键段：未触发的止损类条件单


async def test_open_gives_up_when_the_user_has_an_untriggered_stop_on_that_side(rig):
    await rig.start()
    rig.algos = [stop_algo(SYM, LONG, cid="web_abc", algo_id="9001")]
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "用户自己的" in rec.state_reason and "9001" in rec.state_reason
    no_exchange_activity(rig)  # 用户的单一个字都不碰


async def test_a_user_stop_without_any_client_id_is_also_the_users(rig):
    await rig.start()
    rig.algos = [stop_algo(SYM, LONG, cid=None, algo_id="9002")]
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "用户自己的" in rec.state_reason
    no_exchange_activity(rig)


async def test_stops_on_the_other_side_or_quantity_take_profits_do_not_block_an_open(rig):
    """对侧的止损、别的合约的止损、同侧带数量的用户止盈单都不拦开仓。
    同侧用户的**全平型**止盈单会作用到之后新开的仓上，按 T0.16b 改为放弃开仓（见 test_coordinator_followups.py）。"""
    import dataclasses

    from dmr_kernel.core.types import AlgoType

    await rig.start()
    rig.algos = [stop_algo(SYM, SHORT, cid="web_x", algo_id="1"),
                 dataclasses.replace(stop_algo(SYM, LONG, cid="web_y", algo_id="2", algo_type=AlgoType.TAKE_PROFIT_MARKET),
                                     close_position=False, qty=D("0.002")),
                 stop_algo("ETHUSDT", LONG, cid="web_z", algo_id="3")]
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE
    assert rig.sent("cancel_algo_order") == []


async def test_a_system_stop_is_cancelled_and_confirmed_gone_before_the_open(rig):
    await rig.start()
    cid = ids.make_client_id(rig.instance, "0", 7, "s")
    rig.algos = [stop_algo(SYM, LONG, cid=cid, algo_id="5001")]

    async def hook(name, kw):
        if name == "cancel_algo_order":
            rig.algos = []

    rig.adapter.hook = hook
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE
    assert rig.names() == ["cancel_algo_order", "create_market_order"]  # 先撤并确认不存在，再开仓
    assert rig.sent("cancel_algo_order")[0]["client_algo_id"] == cid


async def test_a_cancelled_system_stop_is_recorded_as_cancelled_in_the_ledger(rig):
    await rig.start()
    from dmr_kernel.core.intents import IntentSpec
    from dmr_kernel.core.types import WorkingType

    owner, _ = await rig.intents.create(IntentSpec(action=IntentAction.STOP_PLACE, model="op",
                                                   source=IntentSource.PROTECTION, symbol=SYM, side=LONG,
                                                   idem_key="protect:1:g1:initial:1"))
    stop = await rig.orders.create_pending(
        intent=owner, leg="s", purpose=OrderPurpose.STOP, kind="algo", order_type="STOP_MARKET", side=SELL,
        position_side=LONG, qty=None, trigger_price=D("45000"), working_type=WorkingType.CONTRACT_PRICE,
        close_position=True)
    rig.algos = [stop_algo(SYM, LONG, cid=stop.client_order_id, algo_id="5002")]

    async def hook(name, kw):
        if name == "cancel_algo_order":
            rig.algos = []

    rig.adapter.hook = hook
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.CANCELLED))
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE
    assert (await rig.orders.get(stop.client_order_id)).state is OrderState.CANCELLED


async def test_open_gives_up_when_the_cancelled_system_stop_is_still_there(rig):
    await rig.start()
    cid = ids.make_client_id(rig.instance, "0", 7, "s")
    rig.algos = [stop_algo(SYM, LONG, cid=cid, algo_id="5001")]  # 撤单「成功」了，但条件单读数里还在
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "仍然存在" in rec.state_reason
    assert rig.names() == ["cancel_algo_order"]  # 撤过，但没有开仓


async def test_an_unknown_cancel_result_is_settled_by_looking_again(rig):
    await rig.start()
    cid = ids.make_client_id(rig.instance, "0", 7, "s")
    rig.algos = [stop_algo(SYM, LONG, cid=cid, algo_id="5001")]
    rig.adapter.push_write_result("cancel_algo_order", CancelResult(CancelOutcome.UNKNOWN, message="超时"))

    async def hook(name, kw):
        if name == "cancel_algo_order":
            rig.algos = []  # 请求其实到了交易所

    rig.adapter.hook = hook
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE  # 撤单结果未知，但复读确认已不存在，可以继续


async def test_open_gives_up_when_the_conditional_orders_cannot_be_read(rig):
    await rig.start()
    rig.adapter.set_read("fetch_open_algo_orders", Unknown("限频"))
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "条件单读数未知" in rec.state_reason
    no_exchange_activity(rig)


async def test_open_gives_up_when_the_conditional_orders_cannot_be_read_after_the_cancel(rig):
    await rig.start()
    cid = ids.make_client_id(rig.instance, "0", 7, "s")
    rig.algos = [stop_algo(SYM, LONG, cid=cid, algo_id="5001")]

    async def hook(name, kw):
        if name == "cancel_algo_order":
            rig.adapter.set_read("fetch_open_algo_orders", Unknown("断连"))

    rig.adapter.hook = hook
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "条件单读数未知" in rec.state_reason
    assert rig.sent("create_market_order") == []


# ================================================================ 分键段：杠杆与保证金模式


async def test_matching_leverage_and_margin_mode_are_not_touched(rig):
    await rig.start()
    rig.leverage[SYM] = LeverageInfo(SYM, 2, "isolated")
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE
    assert rig.sent("set_leverage") == [] and rig.sent("set_margin_mode") == []


async def test_a_different_leverage_is_set_when_both_sides_are_idle(rig):
    await rig.start()
    rig.leverage[SYM] = LeverageInfo(SYM, 20, "isolated")
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE
    assert rig.names() == ["set_leverage", "create_market_order"]
    assert rig.sent("set_leverage")[0] == {"symbol": SYM, "leverage": 2}


async def test_a_different_margin_mode_is_set_before_the_leverage(rig):
    await rig.start()
    rig.leverage[SYM] = LeverageInfo(SYM, 20, "cross")
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE
    assert rig.names() == ["set_margin_mode", "set_leverage", "create_market_order"]
    assert rig.sent("set_margin_mode")[0] == {"symbol": SYM, "margin_mode": "isolated"}


async def test_a_mismatch_with_resting_orders_on_the_symbol_gives_up(rig):
    await rig.start()
    rig.leverage[SYM] = LeverageInfo(SYM, 20, "isolated")
    rig.algos = [stop_algo(SYM, SHORT, cid="web_x", algo_id="1")]  # 对侧的一张条件单：算有挂单
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "挂单" in rec.state_reason
    no_exchange_activity(rig)


async def test_a_mismatch_with_open_orders_gives_up(rig):
    await rig.start()
    rig.leverage[SYM] = LeverageInfo(SYM, 20, "isolated")
    rig.open_orders = [order_view({"symbol": SYM, "client_order_id": "web_1", "side": BUY, "position_side": SHORT,
                                   "qty": D("1")}, status="NEW")]
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "挂单" in rec.state_reason
    no_exchange_activity(rig)


async def test_an_unreadable_leverage_gives_up(rig):
    await rig.start()
    rig.adapter.set_read("fetch_leverage", Unknown("超时"))
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "杠杆读数未知" in rec.state_reason
    no_exchange_activity(rig)


async def test_unreadable_orders_while_fixing_the_leverage_gives_up(rig):
    await rig.start()
    rig.leverage[SYM] = LeverageInfo(SYM, 20, "isolated")
    rig.adapter.set_read("fetch_open_orders", Unknown("限频"))
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "挂单读数未知" in rec.state_reason
    no_exchange_activity(rig)


async def test_a_rejected_leverage_change_gives_up(rig):
    await rig.start()
    rig.leverage[SYM] = LeverageInfo(SYM, 20, "isolated")
    rig.adapter.push_write_result("set_leverage", rejected(RejectKind.HAS_POSITION_OR_ORDERS, -4161, "杠杆无法下调"))
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "设置杠杆被拒绝" in rec.state_reason and "-4161" in rec.state_reason
    assert rig.sent("create_market_order") == []


async def test_an_unknown_margin_mode_change_gives_up_for_this_signal(rig):
    await rig.start()
    rig.leverage[SYM] = LeverageInfo(SYM, 2, "cross")
    rig.adapter.push_write_result("set_margin_mode", unknown_write("超时"))
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "结果未知" in rec.state_reason
    assert rig.sent("create_market_order") == []


@pytest.mark.parametrize("key", ["leverage", "margin_mode"])
async def test_missing_leverage_configuration_gives_up(make_rig, key):
    rig = make_rig(**{key: None})
    await rig.start()
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "未设置" in rec.state_reason
    no_exchange_activity(rig)


async def test_leverage_is_only_checked_for_model_one(rig):
    await rig.start()
    rig.leverage[SYM] = LeverageInfo(SYM, 20, "cross")
    rec = await rig.go(spec(model="op", source=IntentSource.TEST_ENTRY))
    assert rec.state is IntentState.ACTIVE
    assert rig.adapter.calls("fetch_leverage") == []
    assert rig.sent("set_leverage") == [] and rig.sent("set_margin_mode") == []


# ================================================================ 分键段：风控闸


async def test_open_is_refused_when_the_risk_gate_says_no(rig):
    await rig.start()
    rig.riskgate.decision = RiskDecision(False, "日亏损已达上限", "account")
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "风控" in rec.state_reason and "日亏损已达上限" in rec.state_reason
    assert rig.sent("create_market_order") == []


async def test_a_failing_risk_evaluation_refuses_the_open(rig):
    await rig.start()
    rig.riskgate.error = RuntimeError("账本忙")
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "风控评估出错" in rec.state_reason
    assert rig.sent("create_market_order") == []


# ================================================================ 数量


async def test_open_gives_up_when_the_rounded_notional_is_below_the_minimum(make_rig):
    rig = make_rig(trade_notional_usdt=D("60"))
    await rig.start()
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "最小名义金额" in rec.state_reason  # 0.001 × 50000 = 50 < 100
    assert rig.sent("create_market_order") == []


async def test_open_gives_up_when_the_quantity_rounds_down_to_zero(make_rig):
    rig = make_rig(trade_notional_usdt=D("40"))
    await rig.start()
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "数量" in rec.state_reason
    assert rig.sent("create_market_order") == []


async def test_open_gives_up_when_the_last_price_is_unknown(rig):
    await rig.start()
    rig.adapter.set_read("fetch_tickers", Unknown("限频"))
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "最新价读数未知" in rec.state_reason


async def test_open_gives_up_when_the_symbol_has_no_ticker(rig):
    await rig.start()
    rig.prices.pop(SYM)
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "最新价读数未知" in rec.state_reason


async def test_open_gives_up_when_the_instrument_rules_are_unknown(rig):
    await rig.start()
    rig.prices["ADAUSDT"] = D("0.5")
    rec = await rig.go(spec(symbol="ADAUSDT"))
    assert rec.state is IntentState.REJECTED and "合约规则未知" in rec.state_reason


@pytest.mark.parametrize("notional", [None, "abc", "0", "-5"])
async def test_open_gives_up_without_a_usable_notional(make_rig, notional):
    rig = make_rig(trade_notional_usdt=None)
    await rig.start()
    params = {} if notional is None else {"notional": notional}
    rec = await rig.go(spec(params=params))
    assert rec.state is IntentState.REJECTED and "名义金额" in rec.state_reason


# ================================================================ 发单结果


async def test_an_accepted_unfilled_order_keeps_the_intent_active_until_the_fill_arrives(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", ack_ok())
    rec = await rig.go(spec())
    assert rec.state is IntentState.ACTIVE
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.ACKNOWLEDGED
    # 成交回报（数据流）到达：订单合并为 FILLED，意图完成
    view = order_view({"symbol": SYM, "client_order_id": order.client_order_id, "side": BUY, "position_side": LONG,
                       "qty": order.qty}, status="FILLED")
    await rig.orders.apply_exchange_view(order.client_order_id, view, source="stream")
    await rig.settle()
    assert (await rig.intents.get(rec.id)).state is IntentState.COMPLETED


async def test_a_rejected_open_order_rejects_the_intent_with_the_error_code(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", rejected(RejectKind.INSUFFICIENT_MARGIN, -2019, "保证金不足"))
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED
    assert "交易所拒绝开仓单" in rec.state_reason and "保证金不足" in rec.state_reason and "-2019" in rec.state_reason
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.REJECTED and order.error_code == -2019
    assert not rig.coordinator.key_busy(SYM, LONG)


async def test_an_unknown_open_result_is_never_resent_and_keeps_the_key_busy(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", unknown_write("请求超时"))
    rec = await rig.go(spec())
    assert rec.state is IntentState.RECONCILING
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.SENT_UNKNOWN
    assert rig.coordinator.key_busy(SYM, LONG)
    # 交易所一侧什么也查不到（读数未知）：回查多少轮也不重发，也不下结论
    rig.adapter.set_read("fetch_order", Unknown("查询超时"))
    for _ in range(3):
        rig.clock.advance(6_000)
        await rig.coordinator.resolve_unknown_orders()
    assert len(rig.sent("create_market_order")) == 1
    assert (await rig.intents.get(rec.id)).state is IntentState.RECONCILING
    assert (await rig.only_order(rec.id)).state is OrderState.SENT_UNKNOWN


async def test_an_unknown_open_that_turns_out_filled_completes_the_intent(rig):
    await rig.start()
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(spec())
    order = await rig.only_order(rec.id)
    view = order_view({"symbol": SYM, "client_order_id": order.client_order_id, "side": BUY, "position_side": LONG,
                       "qty": order.qty}, status="FILLED")
    rig.adapter.set_read("fetch_order", Known(view))
    rig.clock.advance(6_000)
    await rig.coordinator.resolve_unknown_orders()
    await rig.settle()
    assert (await rig.only_order(rec.id)).state is OrderState.FILLED
    assert (await rig.intents.get(rec.id)).state is IntentState.COMPLETED
    assert len(rig.sent("create_market_order")) == 1


async def test_an_unknown_open_is_rejected_only_after_the_window_three_absences_and_an_unchanged_position(rig):
    """F1 第 9 步：发出已超过签名时间窗加宽限秒数、连续 3 次查到不存在、该侧持仓没有变化，三条同时满足才置为 REJECTED。"""
    await rig.start()
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(spec())
    rig.adapter.set_read("fetch_order", Known(None))  # 交易所明确答复：不存在
    for _ in range(3):  # 3 次不存在，但离发出才 18 秒：不到 5 秒窗口 + 15 秒宽限
        rig.clock.advance(6_000)
        await rig.coordinator.resolve_unknown_orders()
    assert (await rig.intents.get(rec.id)).state is IntentState.RECONCILING
    rig.positions = [position(SYM, LONG, "0.002")]  # 窗口过了、次数够了，但这一侧的持仓出现了：不下结论
    rig.clock.advance(6_000)
    await rig.coordinator.resolve_unknown_orders()
    assert (await rig.intents.get(rec.id)).state is IntentState.RECONCILING
    rig.positions = []  # 持仓没有变化：三条满足
    rig.clock.advance(6_000)
    await rig.coordinator.resolve_unknown_orders()
    await rig.settle()
    done = await rig.intents.get(rec.id)
    assert done.state is IntentState.REJECTED and "回查" in done.state_reason
    assert (await rig.only_order(rec.id)).state is OrderState.REJECTED
    assert not rig.coordinator.key_busy(SYM, LONG)
    assert len(rig.sent("create_market_order")) == 1  # 全程没有重发


async def test_the_grace_period_comes_from_the_configuration(make_rig):
    rig = make_rig(order_unknown_grace_s=0)
    await rig.start()
    rig.adapter.push_write_result("create_market_order", unknown_write())
    rec = await rig.go(spec())
    rig.adapter.set_read("fetch_order", Known(None))
    for _ in range(3):
        rig.clock.advance(6_000)  # 6、12、18 秒，宽限 0：窗口 5 秒，第三次不存在时全部满足
        await rig.coordinator.resolve_unknown_orders()
    await rig.settle()
    assert (await rig.intents.get(rec.id)).state is IntentState.REJECTED


async def test_authentication_failure_from_the_exchange_rejects_with_a_critical_alert(rig):
    await rig.start()

    async def hook(name, kw):
        raise AuthConfigError("认证失败（HTTP 401）")

    rig.adapter.hook = hook
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "认证" in rec.state_reason
    order = await rig.only_order(rec.id)
    assert order.state is OrderState.REJECTED
    assert any(a["level"] is AlertLevel.CRITICAL for a in rig.notifier.alerts)


async def test_an_unexpected_error_before_the_send_rejects_the_intent(rig):
    await rig.start()
    rig.adapter.set_read("fetch_positions", lambda: 1 / 0)
    rec = await rig.go(spec())
    assert rec.state is IntentState.REJECTED and "内部错误" in rec.state_reason
    assert any(a["kind"] == "coordinator_internal_error" for a in rig.notifier.alerts)
    assert rig.sent("create_market_order") == []
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 0


async def test_an_unexpected_error_after_the_send_leaves_the_intent_for_reconciliation(rig):
    await rig.start()
    original = rig.orders.apply_write_result

    async def broken(*a, **kw):
        raise RuntimeError("账本写失败")

    rig.orders.apply_write_result = broken
    rec = await rig.go(spec())
    rig.orders.apply_write_result = original
    assert rec.state is IntentState.RECONCILING  # 请求已发出，结果没记下：按未知处理，绝不重发
    assert len(rig.sent("create_market_order")) == 1
    assert any(a["kind"] == "coordinator_internal_error" for a in rig.notifier.alerts)


# ================================================================ 测试专用入口


async def test_test_entry_skips_gate_model_suppression_and_list_checks_outside_live(rig):
    await rig.start(running=False)  # 闸关着
    await rig.modelstate.add_suppression(Suppression.RECONCILE_UNKNOWN, reason="测试")
    await rig.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner="t")
    rig.list_result = (False, "名单无效")
    rec = await rig.go(spec(model="op", source=IntentSource.TEST_ENTRY))
    assert rec.state is IntentState.ACTIVE
    assert len(rig.sent("create_market_order")) == 1
    assert rig.list_calls == []


async def test_test_entry_still_goes_through_the_risk_gate_and_the_cap(make_rig):
    rig = make_rig(max_symbols=1)
    await rig.start(running=False)
    rig.riskgate.decision = RiskDecision(False, "账户级触发", "account")
    denied = await rig.go(spec(model="op", source=IntentSource.TEST_ENTRY))
    assert denied.state is IntentState.REJECTED and "账户级触发" in denied.state_reason
    rig.riskgate.decision = RiskDecision(True)
    rig.positions = [position("ETHUSDT", LONG, "0.5", "3000")]
    rig.refresh_limits()
    capped = await rig.go(spec(model="op", source=IntentSource.TEST_ENTRY))
    assert capped.state is IntentState.REJECTED and "已达持仓上限" in capped.state_reason


async def test_test_entry_is_refused_in_the_live_environment(make_rig):
    rig = make_rig(live=True)
    await rig.start()
    reads_before = len(rig.adapter.read_calls)
    rec = await rig.go(spec(model="op", source=IntentSource.TEST_ENTRY))
    assert rec.state is IntentState.REJECTED and "测试专用入口" in rec.state_reason and "非实盘" in rec.state_reason
    assert rig.names() == [] and len(rig.adapter.read_calls) == reads_before


async def test_a_non_test_entry_open_is_not_exempt_from_anything(rig):
    await rig.start(running=False)
    rec = await rig.go(spec(model="op", source=IntentSource.OPERATOR))
    assert rec.state is IntentState.REJECTED  # model "op" 不是三个模型之一
    no_exchange_activity(rig)
