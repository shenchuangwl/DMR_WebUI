"""合约数上限：占用怎么数（对冲两侧各一个、外部持仓也算、挂单也算）、快照什么时候过期、上限取哪个。

占用 = 最近一次持仓快照与挂单快照里「有持仓或任何挂单」的（合约, 方向）数 + 已预占且未到终态的开仓意图数。
"""
from __future__ import annotations

import dataclasses

import pytest

from dmr_kernel.core.limits import HARD_MAX_SYMBOLS, Occupancy
from dmr_kernel.core.types import PositionSide

LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
T0 = 1_760_000_000_000


def occ(limits, *, degraded=False, now_ms=T0) -> Occupancy:
    return limits.occupancy(degraded=degraded, now_ms=now_ms)


def test_a_new_limits_has_seen_no_snapshot_so_it_is_stale(limits):
    assert occ(limits) == Occupancy(used=0, reserved=0, cap=30, stale=True, keys=frozenset())


def test_empty_fresh_snapshots_are_not_stale_and_use_nothing(limits, feed):
    feed()
    assert occ(limits) == Occupancy(used=0, reserved=0, cap=30, stale=False, keys=frozenset())


# ---------------------------------------------------------------- 持仓


def test_hedge_mode_counts_both_sides_of_a_symbol_as_two(limits, feed, make_position):
    feed(positions=[make_position("BTCUSDT", LONG), make_position("BTCUSDT", SHORT), make_position("ETHUSDT", LONG)])
    result = occ(limits)
    assert result.used == 3
    assert result.keys == frozenset({("BTCUSDT", "LONG"), ("BTCUSDT", "SHORT"), ("ETHUSDT", "LONG")})


def test_keys_are_plain_strings_with_the_original_exchange_symbol(limits, feed, make_position):
    feed(positions=[make_position("币安人生USDT", SHORT)])
    (key,) = occ(limits).keys
    assert key == ("币安人生USDT", "SHORT") and type(key[0]) is str and type(key[1]) is str


def test_zero_quantity_entries_are_not_positions(limits, feed, make_position):
    feed(positions=[make_position("BTCUSDT", LONG, qty="0"), make_position("BTCUSDT", SHORT, qty="0.00"),
                    make_position("ETHUSDT", LONG, qty="0.001")])
    assert occ(limits).keys == frozenset({("ETHUSDT", "LONG")})


def test_every_position_in_the_snapshot_counts_whoever_opened_it(limits, feed, make_position):
    """外部持仓（不是系统开的、登记簿里是外部或待确认）也占交易所的合约数：Limits 只看快照，不问归属。"""
    feed(positions=[make_position("XRPUSDT", LONG), make_position("DOGEUSDT", SHORT)])
    assert occ(limits).used == 2


# ---------------------------------------------------------------- 挂单与条件单


def test_an_open_order_counts_even_without_a_position(limits, feed, make_order):
    feed(orders=[make_order("SOLUSDT", LONG)])
    result = occ(limits)
    assert result.used == 1 and result.keys == frozenset({("SOLUSDT", "LONG")})


def test_a_conditional_order_counts_while_it_is_open(limits, feed, make_algo):
    feed(algos=[make_algo("SOLUSDT", SHORT, "NEW"), make_algo("ADAUSDT", LONG, "TRIGGERING")])
    assert occ(limits).keys == frozenset({("SOLUSDT", "SHORT"), ("ADAUSDT", "LONG")})


@pytest.mark.parametrize("raw_status", ["TRIGGERED", "FINISHED", "CANCELED", "EXPIRED", "REJECTED"])
def test_a_conditional_order_that_is_no_longer_open_does_not_count(limits, feed, make_algo, raw_status):
    feed(algos=[make_algo("SOLUSDT", SHORT, raw_status)])
    assert occ(limits).used == 0


@pytest.mark.parametrize(("status", "counts"), [("NEW", True), ("PARTIALLY_FILLED", True), ("FILLED", False),
                                                  ("CANCELED", False), ("EXPIRED", False), ("REJECTED", False)])
def test_only_working_orders_count(limits, feed, make_order, status, counts):
    feed(orders=[make_order("SOLUSDT", LONG, status)])
    assert occ(limits).used == (1 if counts else 0)


def test_a_key_with_a_position_and_orders_is_counted_once(limits, feed, make_position, make_order, make_algo):
    feed(positions=[make_position("BTCUSDT", LONG)], orders=[make_order("BTCUSDT", LONG), make_order("BTCUSDT", LONG)],
         algos=[make_algo("BTCUSDT", LONG)])
    assert occ(limits).used == 1


def test_orders_on_the_other_side_take_their_own_slot(limits, feed, make_position, make_order):
    feed(positions=[make_position("BTCUSDT", LONG)], orders=[make_order("BTCUSDT", SHORT)])
    assert occ(limits).keys == frozenset({("BTCUSDT", "LONG"), ("BTCUSDT", "SHORT")})


# ---------------------------------------------------------------- 快照的更新

def test_a_new_snapshot_replaces_the_previous_one_of_the_same_kind(limits, feed, make_position, make_order):
    feed(positions=[make_position("BTCUSDT", LONG), make_position("ETHUSDT", LONG)], orders=[make_order("SOLUSDT", LONG)])
    limits.update_positions([make_position("ETHUSDT", LONG)], T0 + 1000)  # BTC 已平
    assert occ(limits, now_ms=T0 + 1000).keys == frozenset({("ETHUSDT", "LONG"), ("SOLUSDT", "LONG")})
    limits.update_orders([], [], T0 + 1000)  # 挂单都撤了
    assert occ(limits, now_ms=T0 + 1000).keys == frozenset({("ETHUSDT", "LONG")})


def test_snapshots_may_be_given_as_one_shot_iterators(limits, make_position, make_order, make_algo):
    limits.update_positions((make_position(f"C{i}USDT") for i in range(3)), T0)
    limits.update_orders((make_order(f"O{i}USDT") for i in range(2)), iter([make_algo("A0USDT")]), T0)
    assert occ(limits).used == 6


def test_an_older_snapshot_never_replaces_a_newer_one(limits, make_position, make_order):
    """两轮对账重叠、先发起的后返回时，不能让旧快照盖掉新快照。"""
    limits.update_positions([make_position("BTCUSDT", LONG)], T0 + 5000)
    limits.update_positions([make_position("ETHUSDT", LONG), make_position("SOLUSDT", LONG)], T0 + 1000)
    limits.update_orders([make_order("ADAUSDT", LONG)], [], T0 + 5000)
    limits.update_orders([], [], T0 + 1000)
    assert occ(limits, now_ms=T0 + 5000).keys == frozenset({("BTCUSDT", "LONG"), ("ADAUSDT", "LONG")})


def test_a_snapshot_with_the_same_time_is_accepted(limits, make_position):
    limits.update_positions([make_position("BTCUSDT", LONG)], T0)
    limits.update_positions([make_position("ETHUSDT", LONG)], T0)
    limits.update_orders([], [], T0)
    assert occ(limits).keys == frozenset({("ETHUSDT", "LONG")})


# ---------------------------------------------------------------- 快照过期


def test_without_an_orders_snapshot_the_occupancy_is_stale(limits, make_position):
    limits.update_positions([make_position()], T0)
    assert occ(limits).stale is True


def test_without_a_positions_snapshot_the_occupancy_is_stale(limits, make_order):
    limits.update_orders([make_order()], [], T0)
    assert occ(limits).stale is True


@pytest.mark.parametrize(("age_ms", "stale"), [(0, False), (20_000, False), (20_001, True), (600_000, True)])
def test_a_positions_snapshot_older_than_twice_the_position_reconcile_interval_is_stale(limits, age_ms, stale):
    limits.update_positions([], T0)
    limits.update_orders([], [], T0 + age_ms)  # 挂单快照总是新的
    assert occ(limits, now_ms=T0 + age_ms).stale is stale


@pytest.mark.parametrize(("age_ms", "stale"), [(0, False), (120_000, False), (120_001, True), (600_000, True)])
def test_an_orders_snapshot_older_than_twice_the_order_reconcile_interval_is_stale(limits, age_ms, stale):
    limits.update_orders([], [], T0)
    limits.update_positions([], T0 + age_ms)  # 持仓快照总是新的
    assert occ(limits, now_ms=T0 + age_ms).stale is stale


def test_the_stale_thresholds_follow_the_configured_intervals(limits, config, feed):
    feed()
    config["reconcile_positions_s"] = 30
    config["reconcile_orders_s"] = 100
    limits.update_positions([], T0 + 60_000)
    assert occ(limits, now_ms=T0 + 60_000).stale is False  # 订单快照 60 秒 < 200 秒
    limits.update_positions([], T0 + 200_000)
    assert occ(limits, now_ms=T0 + 200_000).stale is False
    limits.update_positions([], T0 + 200_001)
    assert occ(limits, now_ms=T0 + 200_001).stale is True  # 订单快照 200.001 秒 > 200 秒


def test_a_fresh_snapshot_ends_staleness(limits, feed):
    feed()
    assert occ(limits, now_ms=T0 + 500_000).stale is True
    feed(at_ms=T0 + 500_000)
    assert occ(limits, now_ms=T0 + 500_000).stale is False


# ---------------------------------------------------------------- 上限


def test_the_cap_is_max_symbols_normally_and_the_degraded_cap_when_degraded(limits, feed):
    feed()
    assert occ(limits, degraded=False).cap == 30
    assert occ(limits, degraded=True).cap == 10


def test_the_hard_cap_is_35_whatever_the_config_says(limits, config, feed):
    feed()
    assert HARD_MAX_SYMBOLS == 35
    config["max_symbols"] = 50
    config["max_symbols_degraded"] = 49
    assert occ(limits, degraded=False).cap == 35
    assert occ(limits, degraded=True).cap == 35


def test_the_degraded_cap_never_exceeds_the_normal_cap(limits, config, feed):
    feed()
    config["max_symbols"] = 8
    config["max_symbols_degraded"] = 12  # 配置校验会拒绝这种组合；这里只确认「降级只会收紧」
    assert occ(limits, degraded=True).cap == 8


def test_config_changes_apply_to_the_next_call(limits, config, feed):
    feed()
    config["max_symbols"] = 12
    assert occ(limits).cap == 12


def test_occupancy_is_an_immutable_value(limits, feed):
    feed()
    result = occ(limits)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.used = 5  # type: ignore[misc]
    assert isinstance(result.keys, frozenset)
