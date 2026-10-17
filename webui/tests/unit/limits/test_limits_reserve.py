"""合约数上限：预占（try_reserve / release）。

规则：快照过期 → 不放行「快照过期按未知处理」；已占用 + 已预占 + 1 > 上限 → 不放行「已达持仓上限」；
预占在意图到终态时释放；同一批多个预占不会一起越过上限；降级时上限收紧。
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from dmr_kernel.core import types as shared_types
from dmr_kernel.core.limits import RiskDecision
from dmr_kernel.core.types import PositionSide

LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
T0 = 1_760_000_000_000


def reserve(limits, intent_id, symbol="NEWUSDT", side=LONG, *, degraded=False, now_ms=T0):
    return limits.try_reserve(intent_id, symbol, side, degraded=degraded, now_ms=now_ms)


def occupy(feed, make_position, names, side=LONG):
    feed(positions=[make_position(n, side) for n in names])


# ---------------------------------------------------------------- 决定的形状


def test_a_decision_is_an_immutable_value_with_allowed_reason_and_level():
    decision = RiskDecision(True)
    assert (decision.allowed, decision.reason, decision.level) == (True, "", "")
    with pytest.raises(FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]


def test_a_granted_reservation_has_no_reason_and_no_level(limits, feed):
    feed()
    assert reserve(limits, 1) == RiskDecision(True, "", "")


def test_the_decision_class_is_the_shared_one_from_core_types(limits, feed):
    """合约数上限与风控闸返回同一个 RiskDecision（来自 core.types），上层可以统一判断；模块里仍保留这个名字。"""
    assert RiskDecision is shared_types.RiskDecision
    refused = reserve(limits, 1)  # 还没有任何快照：不放行
    feed()
    granted = reserve(limits, 2)
    assert refused.allowed is False and granted.allowed is True
    assert type(refused) is shared_types.RiskDecision and type(granted) is shared_types.RiskDecision


# ---------------------------------------------------------------- 快照过期按未知处理


def test_nothing_can_be_reserved_before_any_snapshot_arrived(limits):
    decision = reserve(limits, 1)
    assert decision.allowed is False and decision.level == "stale"
    assert "快照过期按未知处理" in decision.reason
    assert limits.occupancy(degraded=False, now_ms=T0).reserved == 0  # 被拒绝的不占名额


def test_a_missing_orders_snapshot_alone_refuses(limits, make_position):
    limits.update_positions([make_position()], T0)
    assert reserve(limits, 1).level == "stale"


def test_a_missing_positions_snapshot_alone_refuses(limits, make_order):
    limits.update_orders([make_order()], [], T0)
    assert reserve(limits, 1).level == "stale"


@pytest.mark.parametrize(("positions_age", "orders_age", "refused"), [
    (20_000, 120_000, False),
    (20_001, 0, True),
    (0, 120_001, True),
    (30_000, 130_000, True),
])
def test_stale_snapshots_refuse_even_when_there_is_plenty_of_room(limits, positions_age, orders_age, refused):
    now = T0 + 1_000_000
    limits.update_positions([], now - positions_age)
    limits.update_orders([], [], now - orders_age)
    decision = reserve(limits, 1, now_ms=now)
    assert decision.allowed is not refused
    assert decision.level == ("stale" if refused else "")


def test_a_fresh_snapshot_lifts_the_refusal(limits, feed):
    assert reserve(limits, 1).allowed is False
    feed()
    assert reserve(limits, 1).allowed is True


# ---------------------------------------------------------------- 占用 + 预占 + 1 与上限


def test_the_cap_is_checked_against_used_plus_reserved_plus_one(limits, config, feed, make_position):
    config["max_symbols"] = 3
    occupy(feed, make_position, ["A1USDT"])
    assert reserve(limits, 1).allowed is True  # 1 + 0 + 1 = 2 ≤ 3
    assert reserve(limits, 2).allowed is True  # 1 + 1 + 1 = 3 ≤ 3
    decision = reserve(limits, 3)  # 1 + 2 + 1 = 4 > 3
    assert decision.allowed is False and "已达持仓上限" in decision.reason and decision.level == ""
    result = limits.occupancy(degraded=False, now_ms=T0)
    assert (result.used, result.reserved, result.cap) == (1, 2, 3)


def test_the_refusal_names_the_numbers(limits, config, feed, make_position):
    config["max_symbols"] = 2
    occupy(feed, make_position, ["A1USDT", "A2USDT"])
    reason = reserve(limits, 1).reason
    assert "已达持仓上限" in reason
    assert "已占用 2" in reason and "已预占 0" in reason and "上限 2" in reason


def test_a_refused_reservation_takes_no_slot(limits, config, feed, make_position):
    config["max_symbols"] = 1
    occupy(feed, make_position, ["A1USDT"])
    assert reserve(limits, 1).allowed is False
    assert limits.occupancy(degraded=False, now_ms=T0).reserved == 0
    config["max_symbols"] = 2
    assert reserve(limits, 2).allowed is True  # 上限放宽后，被拒绝的那次没有留下痕迹


def test_a_batch_of_reservations_cannot_cross_the_cap_together(limits, feed, make_position, symbols):
    """同一根 K 线上同时来了 6 个开仓意图，现有 28 个、上限 30：只有 2 个拿到名额。"""
    occupy(feed, make_position, symbols(28))
    results = [reserve(limits, i, f"N{i}USDT") for i in range(1, 7)]
    assert [r.allowed for r in results] == [True, True, False, False, False, False]
    result = limits.occupancy(degraded=False, now_ms=T0)
    assert (result.used, result.reserved) == (28, 2)


def test_positions_orders_and_conditional_orders_all_count_toward_the_cap(limits, config, feed, make_position,
                                                                           make_order, make_algo):
    config["max_symbols"] = 4
    feed(positions=[make_position("P1USDT")], orders=[make_order("O1USDT")], algos=[make_algo("S1USDT")])
    assert reserve(limits, 1).allowed is True  # 3 + 0 + 1 = 4
    assert reserve(limits, 2).allowed is False


def test_both_sides_of_a_hedged_symbol_take_two_slots_of_the_cap(limits, config, feed, make_position):
    config["max_symbols"] = 2
    feed(positions=[make_position("BTCUSDT", LONG), make_position("BTCUSDT", SHORT)])
    assert reserve(limits, 1, "ETHUSDT").allowed is False


def test_an_external_position_takes_a_slot_like_any_other(limits, config, feed, make_position):
    config["max_symbols"] = 1
    feed(positions=[make_position("EXTUSDT", SHORT)])  # 不是系统开的，Limits 也不知道也不关心
    assert reserve(limits, 1).allowed is False


def test_an_open_order_takes_a_slot_even_without_a_position(limits, config, feed, make_order):
    config["max_symbols"] = 1
    feed(orders=[make_order("LMTUSDT")])
    assert reserve(limits, 1).allowed is False


# ---------------------------------------------------------------- 预占与释放


def test_a_reservation_is_held_until_released(limits, config, feed, make_position):
    config["max_symbols"] = 2
    occupy(feed, make_position, ["A1USDT"])
    assert reserve(limits, 1).allowed is True
    assert reserve(limits, 2).allowed is False  # 满了
    limits.release(1)  # 意图 1 到终态
    assert limits.occupancy(degraded=False, now_ms=T0).reserved == 0
    assert reserve(limits, 2).allowed is True


def test_the_reservation_is_not_dropped_by_new_snapshots(limits, config, feed, make_position):
    """成交后持仓快照里出现了这个仓，但意图要到终态才释放：这段时间内按「已占用 + 已预占」都数，偏保守，不会多放。"""
    config["max_symbols"] = 2
    feed()
    assert reserve(limits, 1, "BTCUSDT").allowed is True
    feed(positions=[make_position("BTCUSDT")], at_ms=T0 + 1000)
    result = limits.occupancy(degraded=False, now_ms=T0 + 1000)
    assert (result.used, result.reserved) == (1, 1)
    assert reserve(limits, 2, "ETHUSDT", now_ms=T0 + 1000).allowed is False
    limits.release(1)
    assert reserve(limits, 2, "ETHUSDT", now_ms=T0 + 1000).allowed is True


def test_releasing_an_unknown_intent_is_harmless(limits, feed):
    feed()
    limits.release(999)
    limits.release(999)
    assert reserve(limits, 1).allowed is True
    limits.release(1)
    limits.release(1)  # 重复释放
    assert limits.occupancy(degraded=False, now_ms=T0).reserved == 0


def test_reserving_the_same_intent_again_does_not_take_a_second_slot(limits, config, feed, make_position):
    config["max_symbols"] = 2
    occupy(feed, make_position, ["A1USDT"])
    assert reserve(limits, 7).allowed is True
    assert reserve(limits, 7).allowed is True  # 重试同一个意图：名额还是那一个
    assert limits.occupancy(degraded=False, now_ms=T0).reserved == 1
    assert reserve(limits, 8).allowed is False  # 1 + 1 + 1 = 3 > 2


def test_an_intent_that_already_holds_a_slot_keeps_it_when_the_snapshots_go_stale(limits, feed):
    feed()
    assert reserve(limits, 7).allowed is True
    assert reserve(limits, 7, now_ms=T0 + 1_000_000).allowed is True
    assert reserve(limits, 8, now_ms=T0 + 1_000_000).level == "stale"


def test_reservations_survive_snapshot_updates_and_count_in_the_occupancy_view(limits, feed, make_position):
    feed()
    for i in range(1, 4):
        assert reserve(limits, i, f"N{i}USDT").allowed is True
    feed(positions=[make_position("OTHERUSDT")], at_ms=T0 + 5000)
    result = limits.occupancy(degraded=False, now_ms=T0 + 5000)
    assert (result.used, result.reserved) == (1, 3)
    assert ("N1USDT", "LONG") not in result.keys  # keys 只列快照里的键，预占不在里面


# ---------------------------------------------------------------- 降级时收紧


def test_degraded_mode_tightens_the_cap(limits, feed, make_position, symbols):
    occupy(feed, make_position, symbols(10))
    assert reserve(limits, 1, degraded=False).allowed is True  # 10 + 0 + 1 ≤ 30
    limits.release(1)
    decision = reserve(limits, 2, degraded=True)  # 10 + 0 + 1 > 10
    assert decision.allowed is False and "已达持仓上限" in decision.reason and "上限 10" in decision.reason


def test_a_degraded_cap_refuses_by_the_degraded_number_while_already_over_it(limits, feed, make_position, symbols):
    occupy(feed, make_position, symbols(15))  # 已有 15 个，降级上限 10：只是不再放新的，不去动已有的
    assert reserve(limits, 1, degraded=True).allowed is False
    assert reserve(limits, 2, degraded=False).allowed is True


def test_the_hard_cap_of_35_binds_even_if_the_config_allows_more(limits, config, feed, make_position, symbols):
    config["max_symbols"] = 50
    occupy(feed, make_position, symbols(35))
    assert reserve(limits, 1).allowed is False
    occupy(feed, make_position, symbols(34))
    assert reserve(limits, 1).allowed is True


def test_config_changes_apply_to_the_next_reservation(limits, config, feed, make_position, symbols):
    occupy(feed, make_position, symbols(5))
    config["max_symbols"] = 5
    assert reserve(limits, 1).allowed is False
    config["max_symbols"] = 6
    assert reserve(limits, 1).allowed is True
