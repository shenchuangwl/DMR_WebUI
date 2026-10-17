"""保护单管理：外部保护（F3 第 6 步四条）、接管仓的保护决定、条件单快照复核、交给系统管理止损（F13）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.events import OrdersSnapshotTaken
from dmr_kernel.core.types import (
    AlertLevel,
    AlgoType,
    PositionStatus,
    ProtectionState,
    Unknown,
    WorkingType,
)
from protection_kit import LONG, SHORT, SYM, D, history_then

RISING = [100.5, 101.0, 101.5, 102.0, 102.5, 103.0]


def snapshot(rig) -> OrdersSnapshotTaken:
    """对账读回的条件单快照（只含未结的）。"""
    return OrdersSnapshotTaken(open_orders=(), algo_orders=tuple(rig.exchange.open_algos()), at_ms=rig.clock.now_ms())


async def publish_snapshot(rig) -> None:
    rig.bus.publish(snapshot(rig))
    await rig.settle()


# ---------------------------------------------------------------- 纯判定：四条逐条反例


async def make_long(rig):
    """启动 Rig 并登记一个多头接管仓（接管中，数量 1）。"""
    await rig.start()
    return await rig.open_takeover()


def check(rig, rec, *orders, last="100", mark="100") -> bool:
    return rig.protection.external_protection_ok(rec, list(orders), D(last), D(mark))


async def test_a_full_close_stop_below_the_price_is_external_protection(rig):
    long_rec = await make_long(rig)
    stop = rig.exchange.add_stop(SYM, LONG, "90", cid=None)
    assert check(rig, long_rec, stop)


@pytest.mark.parametrize("algo_type, expected", [
    (AlgoType.STOP_MARKET, True), (AlgoType.STOP, True), (AlgoType.TRAILING_STOP_MARKET, True),
    (AlgoType.TAKE_PROFIT_MARKET, False), (AlgoType.TAKE_PROFIT, False),
])
async def test_condition_one_the_order_must_be_a_stop_kind(rig, algo_type, expected):
    long_rec = await make_long(rig)
    order = rig.exchange.add_stop(SYM, LONG, "90", cid=None, algo_type=algo_type)
    assert check(rig, long_rec, order) is expected


async def test_a_take_profit_never_counts_even_on_the_protective_side_of_the_price(rig):
    long_rec = await make_long(rig)
    below = rig.exchange.add_stop(SYM, LONG, "90", cid=None, algo_type=AlgoType.TAKE_PROFIT_MARKET)
    above = rig.exchange.add_stop(SYM, LONG, "110", cid=None, algo_type=AlgoType.TAKE_PROFIT_MARKET)
    assert not check(rig, long_rec, below, above)


async def test_condition_two_the_position_side_must_match(rig):
    long_rec = await make_long(rig)
    other_side = rig.exchange.add_stop(SYM, SHORT, "90", cid=None, position_side=SHORT)
    other_symbol = rig.exchange.add_stop("ETHUSDT", LONG, "90", cid=None)
    assert not check(rig, long_rec, other_side)
    assert not check(rig, long_rec, other_symbol)


@pytest.mark.parametrize("close_position, qty, rule, expected", [
    (True, None, "equal_only", True),    # 全平型：不看数量
    (False, "1", "equal_only", True),    # 数量等于持仓
    (False, "0.5", "equal_only", False),  # 数量小于持仓：持仓一旦变小…不算
    (False, "2", "equal_only", False),   # 保守规则：大于持仓也不算（实测第 3 项）
    (False, "2", "ge", True),            # 宽松规则：不小于持仓即算
    (False, "1", "ge", True),
    (False, "0.9", "ge", False),
    (False, None, "ge", False),          # 既不是全平型也没数量：不算
])
async def test_condition_three_close_position_or_a_quantity_that_meets_the_rule(
        rig, close_position, qty, rule, expected):
    long_rec = await make_long(rig)
    rig.config["ex_qty_stop_protect_rule"] = rule
    order = rig.exchange.add_stop(SYM, LONG, "90", cid=None, close_position=close_position, qty=qty)
    assert check(rig, long_rec, order) is expected


@pytest.mark.parametrize("trigger, expected", [("99.99", True), ("100", False), ("101", False), ("50", True)])
async def test_condition_four_a_long_stop_must_trigger_below_the_price(rig, trigger, expected):
    long_rec = await make_long(rig)
    assert check(rig, long_rec, rig.exchange.add_stop(SYM, LONG, trigger, cid=None)) is expected


@pytest.mark.parametrize("trigger, expected", [("100.01", True), ("100", False), ("99", False), ("150", True)])
async def test_condition_four_a_short_stop_must_trigger_above_the_price(rig, trigger, expected):
    await rig.start()
    short = await rig.open_takeover(side=SHORT)
    assert check(rig, short, rig.exchange.add_stop(SYM, SHORT, trigger, cid=None)) is expected


async def test_the_price_is_the_one_of_the_orders_own_trigger_type(rig):
    long_rec = await make_long(rig)
    by_mark = rig.exchange.add_stop(SYM, LONG, "95", cid=None, working_type=WorkingType.MARK_PRICE)
    by_last = rig.exchange.add_stop(SYM, LONG, "95", cid=None, working_type=WorkingType.CONTRACT_PRICE)
    assert check(rig, long_rec, by_mark, last="90", mark="100")  # 按标记价 100：95 在下面
    assert not check(rig, long_rec, by_last, last="90", mark="100")  # 按最新价 90：95 在上面
    assert not check(rig, long_rec, by_mark, last="100", mark="90")


@pytest.mark.parametrize("raw, expected", [("NEW", True), ("TRIGGERING", True), ("CANCELED", False),
                                           ("FINISHED", False), ("EXPIRED", False), ("REJECTED", False)])
async def test_only_open_orders_count_judged_by_the_raw_status(rig, raw, expected):
    long_rec = await make_long(rig)
    assert check(rig, long_rec, rig.exchange.add_stop(SYM, LONG, "90", cid=None, raw=raw)) is expected


async def test_any_qualifying_order_is_enough(rig):
    long_rec = await make_long(rig)
    bad = rig.exchange.add_stop(SYM, LONG, "110", cid=None)
    good = rig.exchange.add_stop(SYM, LONG, "90", cid=None)
    assert check(rig, long_rec, bad, good) and not check(rig, long_rec, bad) and not check(rig, long_rec)


# ---------------------------------------------------------------- 接管仓的保护决定


async def test_a_takeover_position_with_a_qualifying_user_stop_gets_external_protection(rig):
    await rig.start()
    user = rig.exchange.add_stop(SYM, LONG, "90", cid=None)
    rec = await rig.open_takeover()
    assert (await rig.protection.ensure_protected(rec.id, reason="takeover")) == "external"
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.EXTERNAL and row.status is PositionStatus.MANAGED
    assert row.stop_price is None and row.stop_client_id is None and "外部保护" in row.stop_note
    assert rig.exchange.writes == [] and rig.exchange.stops() == [user]  # 系统不动用户的止损
    assert not rig.suppressed()


@pytest.mark.parametrize("make_order", [
    lambda ex: ex.add_stop(SYM, LONG, "110", cid=None),                                # 触发方向不对
    lambda ex: ex.add_stop(SYM, LONG, "90", cid=None, close_position=False, qty="0.5"),  # 数量不够
    lambda ex: ex.add_stop(SYM, LONG, "90", cid=None, algo_type=AlgoType.TAKE_PROFIT_MARKET),  # 止盈
    lambda ex: ex.add_stop(SYM, SHORT, "90", cid=None, position_side=SHORT),             # 方向不一致
])
async def test_a_user_order_that_fails_a_condition_is_left_alone_and_the_system_places_its_own_stop(rig, make_order):
    await rig.start()
    user = make_order(rig.exchange)
    rec = await rig.open_takeover()
    assert (await rig.protection.ensure_protected(rec.id, reason="takeover")) == "protected"
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and len(rig.writes("create_stop_order")) == 1
    assert rig.writes("cancel_algo_order") == []  # 用户的单（含止盈）一律不动
    assert user in rig.exchange.open_algos()


async def test_a_system_stop_already_on_the_exchange_is_adopted_not_duplicated(rig):
    """重启后补保护（F9 第 5 步）：交易所上已经有系统止损，认领它，不挂第二张。"""
    await rig.start()
    existing = rig.exchange.add_stop(SYM, LONG, "96")  # 系统编号
    rec = await rig.open_takeover()
    assert (await rig.protection.ensure_protected(rec.id, reason="recovery")) == "protected"
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("96")
    assert (row.stop_client_id, row.stop_algo_id) == (existing.client_algo_id, existing.algo_id)
    assert row.status is PositionStatus.MANAGED and rig.writes("create_stop_order") == []


async def test_unreadable_orders_are_not_treated_as_none_so_nothing_is_placed(rig):
    await rig.start()
    rig.exchange.set_read("fetch_open_algo_orders", Unknown("读不到"))
    rec = await rig.open_takeover()
    assert (await rig.protection.ensure_protected(rec.id, reason="takeover")) == "rebuilding"
    assert rig.exchange.writes == []
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.REBUILDING and row.status is PositionStatus.TAKING_OVER
    rig.exchange.wire()
    await rig.tick(10_000)  # 下一次重试读到了：没有用户的止损，自己挂
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.status is PositionStatus.MANAGED
    assert len(rig.writes("create_stop_order")) == 1


# ---------------------------------------------------------------- 条件单快照复核（每次都复核，失效即转入 F2 并告警）


async def _external_position(rig, trigger="90", **order_kw):
    user = rig.exchange.add_stop(SYM, LONG, trigger, cid=None, **order_kw)
    rec = await rig.open_takeover()
    assert (await rig.protection.ensure_protected(rec.id, reason="takeover")) == "external"
    await rig.protection.start()
    return rec, user


async def test_a_valid_external_protection_survives_every_snapshot(rig):
    await rig.start()
    rec, _ = await _external_position(rig)
    for _ in range(3):
        await publish_snapshot(rig)
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).protection_state is ProtectionState.EXTERNAL
    assert rig.notifier.alerts == []


async def test_when_the_users_stop_disappears_the_system_takes_over_and_warns(rig):
    await rig.start()
    rec, user = await _external_position(rig)
    rig.exchange.mutate(user, raw_status="CANCELED")  # 用户在网页上撤掉了自己的止损
    await publish_snapshot(rig)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and len(rig.writes("create_stop_order")) == 1
    (alert,) = rig.notifier.of("external_protection_lost")
    assert alert["level"] is AlertLevel.WARNING


async def test_when_the_users_stop_stops_qualifying_the_system_places_its_own_and_keeps_the_users(rig):
    await rig.start()
    rec, user = await _external_position(rig, "90")
    rig.exchange.set_price(SYM, "85")  # 价格跌到用户的止损价之下：多仓的止损触发价不再低于现价
    await publish_snapshot(rig)
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and rig.notifier.of("external_protection_lost")
    assert rig.writes("cancel_algo_order") == [] and user in rig.exchange.open_algos()


async def test_the_quantity_rule_is_rechecked_on_every_snapshot(rig):
    await rig.start()
    rig.config["ex_qty_stop_protect_rule"] = "ge"
    rec, _ = await _external_position(rig, "90", close_position=False, qty="2")
    await publish_snapshot(rig)
    assert (await rig.row(rec.id)).protection_state is ProtectionState.EXTERNAL
    rig.config["ex_qty_stop_protect_rule"] = "equal_only"  # 数量 2 ≠ 持仓 1：不再算
    await publish_snapshot(rig)
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM


async def test_an_unreadable_price_does_not_invalidate_external_protection(rig):
    await rig.start()
    rec, user = await _external_position(rig)
    rig.exchange.set_read("fetch_tickers", Unknown("读不到"))
    rig.exchange.mutate(user, raw_status="CANCELED")  # 即使快照里没有用户的单：读不到现价就不下结论
    await publish_snapshot(rig)
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).protection_state is ProtectionState.EXTERNAL


async def test_the_users_take_profit_is_never_touched_in_any_recheck(rig):
    await rig.start()
    rec, user = await _external_position(rig)
    tp = rig.exchange.add_stop(SYM, LONG, "120", cid=None, algo_type=AlgoType.TAKE_PROFIT_MARKET)
    rig.exchange.mutate(user, raw_status="CANCELED")
    await publish_snapshot(rig)
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM
    assert rig.writes("cancel_algo_order") == [] and tp in rig.exchange.open_algos()


# ---------------------------------------------------------------- F13 交给系统管理止损


async def test_handing_the_stop_to_the_system_in_gap_mode_cancels_the_users_then_places_ours(rig):
    await rig.start()
    rec, user = await _external_position(rig)
    assert (await rig.protection.hand_stop_to_system(rec.id)) == "protected"
    await rig.settle()
    assert rig.names() == ["cancel_algo_order", "create_stop_order"]  # 挂与撤的先后同 F2 第 7 步：gap 先撤后挂
    cancel = rig.writes("cancel_algo_order")[0]
    assert cancel["algo_id"] == user.algo_id and cancel["client_algo_id"] is None  # 用户的单没有系统编号：按交易所单号撤
    (ours,) = rig.exchange.stops()
    assert ours.trigger_price == D("97") and ours.client_algo_id and ours.algo_id != user.algo_id
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("97")
    assert (row.stop_client_id, row.stop_algo_id) == (ours.client_algo_id, ours.algo_id)
    assert "外部保护" not in row.stop_note  # 不再是外部保护：说明清掉


async def test_handing_the_stop_over_in_make_before_break_mode_places_ours_first(make_rig):
    rig = make_rig(ex_stop_update_mode="make_before_break")
    await rig.start()
    rec, user = await _external_position(rig)
    assert (await rig.protection.hand_stop_to_system(rec.id)) == "protected"
    assert rig.names() == ["create_stop_order", "cancel_algo_order"]  # 先挂后撤
    assert rig.exchange.find(SYM, algo_id=user.algo_id).raw_status == "CANCELED" and len(rig.exchange.stops()) == 1


async def test_after_the_hand_over_the_system_trails_the_stop(rig):
    await rig.start()
    rec, _ = await _external_position(rig)
    await rig.protection.hand_stop_to_system(rec.id)
    rig.candles.set(SYM, history_then(RISING))
    rig.exchange.set_price(SYM, "105")
    await rig.protection.trailing(rig.candles.frame(SYM, "15m")["open_time_ms"].iloc[-1].item())
    await rig.settle()
    row = await rig.row(rec.id)
    assert row.stop_price == D("97.5") and row.stop_cycle == 1


async def test_a_rejected_new_stop_restores_the_users_price_as_a_system_stop(rig):
    await rig.start()
    rec, user = await _external_position(rig, "90")
    rig.exchange.push_write_result("create_stop_order", rig.exchange.reject())
    assert (await rig.protection.hand_stop_to_system(rec.id)) == "protected"
    (restored,) = rig.exchange.stops()
    assert restored.trigger_price == D("90")  # 新单被拒：按用户原来的止损价重挂，保护不断
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.SYSTEM and row.stop_price == D("90")


async def test_the_users_other_protective_stops_are_cancelled_after_ours_is_in_place(rig):
    await rig.start()
    rec, first = await _external_position(rig, "90")
    second = rig.exchange.add_stop(SYM, LONG, "92", cid=None)
    tp = rig.exchange.add_stop(SYM, LONG, "120", cid=None, algo_type=AlgoType.TAKE_PROFIT_MARKET)
    assert (await rig.protection.hand_stop_to_system(rec.id)) == "protected"
    assert rig.names() == ["cancel_algo_order", "create_stop_order", "cancel_algo_order"]
    (ours,) = rig.exchange.stops()
    assert ours.algo_id not in (first.algo_id, second.algo_id) and tp in rig.exchange.open_algos()  # 止盈不动


async def test_handing_over_is_only_for_external_protected_takeover_positions(rig):
    await rig.start()
    strategy = await rig.open_strategy()
    assert (await rig.protection.hand_stop_to_system(strategy.id)) == "skipped"
    takeover = await rig.open_takeover(symbol="ETHUSDT")
    assert (await rig.protection.hand_stop_to_system(takeover.id)) == "skipped"  # 还没有外部保护
    assert (await rig.protection.hand_stop_to_system(9999)) == "skipped"
    assert rig.exchange.writes == []


async def test_handing_over_without_readable_orders_changes_nothing(rig):
    await rig.start()
    rec, user = await _external_position(rig)
    rig.exchange.set_read("fetch_open_algo_orders", Unknown("读不到"))
    assert (await rig.protection.hand_stop_to_system(rec.id)) == "unknown"
    assert rig.exchange.writes == [] and (await rig.row(rec.id)).protection_state is ProtectionState.EXTERNAL


async def test_rebuild_of_a_takeover_position_respects_a_qualifying_users_stop(rig):
    """重启后补保护（F9 第 5 步）：接管仓上用户的止损满足四条，就记外部保护、不另挂（同向第二张全平型止损还会被 −4130 拒绝）。"""
    await rig.start()
    user = rig.exchange.add_stop(SYM, LONG, "90", cid=None)
    rec = await rig.open_takeover()
    assert (await rig.protection.rebuild(rec.id, reason="recovery")) == "external"
    row = await rig.row(rec.id)
    assert row.protection_state is ProtectionState.EXTERNAL and row.status is PositionStatus.MANAGED
    assert rig.exchange.writes == [] and rig.exchange.stops() == [user]


async def test_rebuild_of_an_external_position_whose_users_stop_is_gone_places_the_systems_own(rig):
    await rig.start()
    rec, user = await _external_position(rig)
    rig.exchange.mutate(user, raw_status="CANCELED")
    assert (await rig.protection.rebuild(rec.id, reason="recovery")) == "protected"
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM
    assert len(rig.notifier.of("external_protection_lost")) == 1  # 失效即告警


async def test_a_quantity_change_rechecks_the_external_protection_at_once(rig):
    """F4 第 7 步：部分平仓、外部加减仓之后数量变了，用户的数量型止损可能不再满足：不等下一次对账快照，立即复核。"""
    from dmr_kernel.core.events import PositionChanged

    await rig.start()
    rig.config["ex_qty_stop_protect_rule"] = "ge"
    rec, _ = await _external_position(rig, "90", close_position=False, qty="2")
    assert (await rig.row(rec.id)).protection_state is ProtectionState.EXTERNAL
    rig.config["ex_qty_stop_protect_rule"] = "equal_only"  # 数量变了之后，用户的 2 不再算
    for cause in ("partial_close", "protection"):  # 这些原因不是数量变化：不复核
        rig.bus.publish(PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause=cause, version=9,
                                        at_ms=rig.clock.now_ms()))
    await rig.settle()
    assert (await rig.row(rec.id)).protection_state is ProtectionState.EXTERNAL
    rig.bus.publish(PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="fill", version=10,
                                    at_ms=rig.clock.now_ms()))
    await rig.settle()
    assert (await rig.row(rec.id)).protection_state is ProtectionState.SYSTEM
    assert rig.notifier.of("external_protection_lost")
