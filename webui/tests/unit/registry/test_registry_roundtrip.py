"""逐笔交易：一笔 = 仓位从零变非零到再次归零（roundtrip.build_trade）。数值逐项核对。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import ExitReason, IntentAction, Ownership
from dmr_kernel.ledger import roundtrip
from registry_kit import (
    D,
    EXTERNAL_ORIGIN,
    LONG,
    SHORT,
    SYM,
    T0,
    add_income,
    add_intent,
    trade_count,
    trade_of,
)


async def _close_intent(rig, reason: str = "signal") -> int:
    return await add_intent(rig.ledger, action=IntentAction.CLOSE, reason=reason)


async def test_simple_round_trip_numbers(rig):
    await rig.up()
    open_iid = await add_intent(rig.ledger, reason="信号开多",
                                params={"list_snapshot_id": 5, "list_direction": "LONG", "list_score": "0.7"})
    await rig.apply(qty=2, price=100, intent_id=open_iid, time_ms=T0 + 1_000, commission="0.08")
    closed = await rig.apply(qty=2, price=110, opening=False, intent_id=await _close_intent(rig),
                             time_ms=T0 + 61_000, commission="0.088", realized_pnl=20)
    trade = await trade_of(rig.ledger, closed.id)
    assert trade.position_id == closed.id and trade.id > 0
    assert (trade.symbol, trade.side, trade.ownership, trade.model) == (SYM, LONG, Ownership.STRATEGY, "m1")
    assert trade.is_mixed is False and trade.open_time_estimated is False
    assert (trade.open_ms, trade.close_ms) == (T0 + 1_000, T0 + 61_000)
    assert trade.max_qty == D(2)
    assert (trade.open_avg, trade.close_avg, trade.open_notional) == (D(100), D(110), D(200))
    assert (trade.gross_pnl, trade.fees, trade.funding) == (D(20), D("0.168"), D(0))
    assert trade.net_pnl == D("19.832")  # 毛 − 手续费 + 资金费
    assert trade.entry_reason == "信号开多" and trade.exit_reason is ExitReason.SIGNAL
    assert (trade.list_snapshot_id, trade.list_direction, trade.list_score) == (5, "LONG", D("0.7"))


async def test_short_round_trip_uses_the_short_directions(rig):
    await rig.up()
    iid = await add_intent(rig.ledger, side=SHORT)
    await rig.apply(qty=3, price=200, position_side=SHORT, intent_id=iid, time_ms=T0)
    closed = await rig.apply(qty=3, price=180, position_side=SHORT, opening=False,
                             intent_id=await _close_intent(rig), time_ms=T0 + 10, realized_pnl=60)
    trade = await trade_of(rig.ledger, closed.id)
    assert trade.side is SHORT
    assert (trade.open_avg, trade.close_avg, trade.open_notional, trade.gross_pnl) == (D(200), D(180), D(600), D(60))


async def test_several_fills_are_weighted_by_quantity(rig):
    await rig.up()
    iid = await add_intent(rig.ledger)
    await rig.apply(qty=1, price=100, intent_id=iid, time_ms=T0)
    await rig.apply(qty=3, price=104, intent_id=iid, time_ms=T0 + 1)
    cid = await _close_intent(rig)
    await rig.apply(qty=1, price=105, opening=False, intent_id=cid, time_ms=T0 + 2, realized_pnl=4)
    closed = await rig.apply(qty=3, price=109, opening=False, intent_id=cid, time_ms=T0 + 3, realized_pnl=15)
    trade = await trade_of(rig.ledger, closed.id)
    assert trade.max_qty == D(4)
    assert trade.open_avg == D(103) and trade.open_notional == D(412)       # (100 + 312) / 4
    assert trade.close_avg == D(108) and trade.gross_pnl == D(19)           # (105 + 327) / 4
    assert trade.close_ms == T0 + 3


async def test_fees_prefer_usdt_value_and_skip_unconverted_assets(rig):
    await rig.up()
    iid = await add_intent(rig.ledger)
    await rig.apply(qty=1, price=100, intent_id=iid, commission="0.0004", commission_asset="BNB",
                    commission_usdt="0.5")                                  # 已折成 USDT 的
    await rig.apply(qty=1, price=100, intent_id=iid, commission="0.3", commission_usdt=None)   # USDT 手续费，没折算也直接用
    await rig.apply(qty=1, price=100, intent_id=iid, commission="0.002", commission_asset="BNB",
                    commission_usdt=None)                                   # 折算不了的非 USDT 手续费：不计
    closed = await rig.apply(qty=3, price=100, opening=False, intent_id=await _close_intent(rig), commission="0.2")
    trade = await trade_of(rig.ledger, closed.id)
    assert trade.fees == D("1.0")  # 0.5 + 0.3 + 0 + 0.2
    assert trade.net_pnl == D("-1.0")


async def test_funding_counts_only_the_holding_period_of_this_contract(rig):
    await rig.up()
    iid = await add_intent(rig.ledger)
    await rig.apply(qty=1, price=100, intent_id=iid, time_ms=T0 + 1_000)
    await add_income(rig.ledger, amount="-9", time_ms=T0 + 500)                       # 开仓前
    await add_income(rig.ledger, amount="-1.5", time_ms=T0 + 2_000)                   # 持仓期间
    await add_income(rig.ledger, amount="0.4", time_ms=T0 + 61_000)                   # 恰在平仓时刻：含在内
    await add_income(rig.ledger, amount="-8", time_ms=T0 + 70_000)                    # 平仓后
    await add_income(rig.ledger, amount="-7", time_ms=T0 + 2_000, symbol="ETHUSDT")   # 别的合约
    await add_income(rig.ledger, amount="-6", time_ms=T0 + 2_000, income_type="COMMISSION")  # 不是资金费
    closed = await rig.apply(qty=1, price=100, opening=False, intent_id=await _close_intent(rig),
                             time_ms=T0 + 61_000, commission="0.1", realized_pnl=3)
    trade = await trade_of(rig.ledger, closed.id)
    assert trade.funding == D("-1.1")
    assert trade.net_pnl == D("3") - D("0.1") + D("-1.1")  # 毛 − 手续费 + 资金费（资金费带符号）


async def test_funding_is_split_by_notional_while_both_sides_are_held(rig):
    """对冲两侧同时有仓时，同一笔资金费按两侧名义金额（均价 × 最大数量）分摊；另一侧已平后全部归本侧。"""
    await rig.up()
    short_iid = await add_intent(rig.ledger, side=SHORT)
    long_iid = await add_intent(rig.ledger)
    await rig.apply(qty=3, price=100, position_side=SHORT, intent_id=short_iid, time_ms=T0)           # 名义 300
    await rig.apply(qty=1, price=100, position_side=LONG, intent_id=long_iid, time_ms=T0 + 1_000)     # 名义 100
    await add_income(rig.ledger, amount="-4", time_ms=T0 + 10_000)    # 两侧都有仓：空 3/4，多 1/4
    await add_income(rig.ledger, amount="-2", time_ms=T0 + 60_000)    # 空仓 T0+50_000 已平：全归多
    short_closed = await rig.apply(qty=3, price=95, position_side=SHORT, opening=False, time_ms=T0 + 50_000,
                                   intent_id=await _close_intent(rig))
    long_closed = await rig.apply(qty=1, price=95, position_side=LONG, opening=False, time_ms=T0 + 100_000,
                                  intent_id=await _close_intent(rig))
    assert (await trade_of(rig.ledger, short_closed.id)).funding == D("-3")
    assert (await trade_of(rig.ledger, long_closed.id)).funding == D("-3")   # -1（分摊）+ -2（独占）


async def test_funding_split_counts_a_still_open_other_side(rig):
    await rig.up()
    await rig.open_strategy(side=SHORT, qty=1, price=300, open_time_ms=T0)             # 名义 300，一直没平
    iid = await add_intent(rig.ledger)
    await rig.apply(qty=1, price=100, intent_id=iid, time_ms=T0 + 1_000)               # 名义 100
    await add_income(rig.ledger, amount="-8", time_ms=T0 + 5_000)
    closed = await rig.apply(qty=1, price=100, opening=False, intent_id=await _close_intent(rig), time_ms=T0 + 9_000)
    assert (await trade_of(rig.ledger, closed.id)).funding == D("-2")                   # 100 / 400


async def test_takeover_position_without_fills_uses_registered_average_and_peak(rig):
    await rig.up()
    await rig.managed_takeover(qty=2, price=100, open_time_ms=T0 - 5_000, open_time_estimated=True)
    closed = await rig.apply(qty=2, price=110, opening=False, origin=EXTERNAL_ORIGIN, purpose=None,
                             time_ms=T0 + 100, commission="0.1", realized_pnl=20)
    trade = await trade_of(rig.ledger, closed.id)
    assert (trade.ownership, trade.model) == (Ownership.TAKEOVER, "m2")
    assert trade.open_time_estimated is True and trade.open_ms == T0 - 5_000
    assert (trade.open_avg, trade.open_notional, trade.close_avg) == (D(100), D(200), D(110))
    assert (trade.gross_pnl, trade.fees, trade.net_pnl) == (D(20), D("0.1"), D("19.9"))
    assert trade.exit_reason is ExitReason.EXTERNAL and trade.entry_reason == ""


async def test_takeover_with_a_later_external_add_keeps_the_blended_average(rig):
    """既有持仓没有成交记录：成交解释不了全部开仓数量时，开仓均价与名义金额以登记的均价和最大数量为准。"""
    await rig.up()
    await rig.managed_takeover(qty=2, price=100)
    await rig.apply(qty=2, price=120, origin=EXTERNAL_ORIGIN, purpose=None, time_ms=T0 + 10)   # 外部加仓：均价 110
    closed = await rig.apply(qty=4, price=130, opening=False, origin=EXTERNAL_ORIGIN, purpose=None,
                             time_ms=T0 + 20, realized_pnl=80)
    trade = await trade_of(rig.ledger, closed.id)
    assert trade.max_qty == D(4)
    assert trade.open_avg == D(110) and trade.open_notional == D(440)
    assert trade.close_avg == D(130) and trade.gross_pnl == D(80)


async def test_mixed_position_trade_is_flagged(rig):
    await rig.up()
    await rig.open_strategy(qty=1, price=100)
    await rig.apply(qty=1, price=110, origin=EXTERNAL_ORIGIN, purpose=None)
    closed = await rig.apply(qty=2, price=120, opening=False, intent_id=await _close_intent(rig, "manual"))
    trade = await trade_of(rig.ledger, closed.id)
    assert trade.is_mixed is True and trade.ownership is Ownership.STRATEGY
    assert trade.exit_reason is ExitReason.MANUAL


@pytest.mark.parametrize("ownership, intent_model, expected", [
    (Ownership.STRATEGY, "op", "op"),      # 意图的模型优先
    (Ownership.STRATEGY, None, "m1"),      # 读不到意图：按归属
    (Ownership.AI, None, "m3"),
])
async def test_trade_model_comes_from_the_opening_intent_or_the_ownership(rig, ownership, intent_model, expected):
    await rig.up()
    if intent_model is None:
        rec = await rig.open_strategy(ownership=ownership, intent_id=987_654)   # 没有这个意图
    else:
        iid = await add_intent(rig.ledger, model=intent_model)
        rec = await rig.open_strategy(ownership=ownership, intent_id=iid)
    await rig.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=T0 + 1)
    assert (await trade_of(rig.ledger, rec.id)).model == expected


async def test_open_time_of_an_unregistered_origin_is_estimated_from_the_first_open_fill(rig):
    await rig.up()
    pend = await rig.pending(qty=1, price=100)                       # 待确认仓：不知道什么时候开的
    await rig.apply(qty=1, price=110, origin=EXTERNAL_ORIGIN, purpose=None, time_ms=T0 + 1_000)   # 之后外部加仓
    closed = await rig.apply(qty=2, price=120, opening=False, origin=EXTERNAL_ORIGIN, purpose=None,
                             time_ms=T0 + 5_000)
    trade = await trade_of(rig.ledger, pend.id)
    assert closed.id == pend.id
    assert trade.open_ms == T0 + 1_000 and trade.open_time_estimated is True
    assert trade.ownership is Ownership.NONE and trade.model is None

    other = await rig.pending(symbol="ETHUSDT")                       # 一笔成交也没有：开仓时间留空
    await rig.registry.mark_closed(other.id, exit_reason=ExitReason.EXTERNAL, closed_ms=T0 + 9_000)
    empty = await trade_of(rig.ledger, other.id)
    assert empty.open_ms is None and empty.close_ms == T0 + 9_000
    assert (empty.open_avg, empty.close_avg) == (D(100), None)       # 没有成交：开仓用登记均价，平仓均价为空
    assert (empty.gross_pnl, empty.fees, empty.funding, empty.net_pnl) == (D(0), D(0), D(0), D(0))


async def test_build_trade_can_be_rerun_and_updates_the_same_row(rig):
    await rig.up()
    iid = await add_intent(rig.ledger)
    await rig.apply(qty=1, price=100, intent_id=iid, time_ms=T0)
    closed = await rig.apply(qty=1, price=100, opening=False, intent_id=await _close_intent(rig), time_ms=T0 + 5_000)
    first = await trade_of(rig.ledger, closed.id)
    assert first.funding == D(0)
    await add_income(rig.ledger, amount="-0.7", time_ms=T0 + 2_000)          # 资金费入账晚于平仓
    again = await rig.ledger.write(roundtrip.build_trade, closed.id)
    assert again.id == first.id and again.funding == D("-0.7") and again.net_pnl == D("-0.7")
    assert await trade_count(rig.ledger) == 1
    assert await trade_of(rig.ledger, closed.id) == again


async def test_build_trade_rejects_unknown_and_unclosed_positions(rig):
    await rig.up()
    rec = await rig.open_strategy()
    with pytest.raises(ValueError):
        await rig.ledger.write(roundtrip.build_trade, rec.id)          # 还没平
    with pytest.raises(LookupError):
        await rig.ledger.write(roundtrip.build_trade, 9_999)
    assert await trade_count(rig.ledger) == 0
    assert await rig.ledger.read(roundtrip.get_trade, rec.id) is None


async def test_two_generations_make_two_trades(rig):
    await rig.up()
    iid = await add_intent(rig.ledger)
    cid = await _close_intent(rig)
    first_open = await rig.apply(qty=1, price=100, intent_id=iid, time_ms=T0)
    first = await rig.apply(qty=1, price=101, opening=False, intent_id=cid, time_ms=T0 + 10, realized_pnl=1)
    second_open = await rig.apply(qty=2, price=102, intent_id=iid, time_ms=T0 + 20)
    second = await rig.apply(qty=2, price=104, opening=False, intent_id=cid, time_ms=T0 + 30, realized_pnl=4)
    assert first_open.id == first.id != second.id == second_open.id
    t1, t2 = await trade_of(rig.ledger, first.id), await trade_of(rig.ledger, second.id)
    assert (t1.max_qty, t1.gross_pnl, t1.open_avg) == (D(1), D(1), D(100))
    assert (t2.max_qty, t2.gross_pnl, t2.open_avg) == (D(2), D(4), D(102))   # 第二代不带第一代的成交
    assert await trade_count(rig.ledger) == 2
