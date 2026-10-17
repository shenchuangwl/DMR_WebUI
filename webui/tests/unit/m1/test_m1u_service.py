"""模型 1 服务的预检与名单复核（用桩，不起内核）：每条预检的原因、先后次序，以及没接入名单时的样子。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.registry import KeyOccupancy
from dmr_kernel.core.types import PositionSide
from dmr_kernel.models.m1_strategy import StrategyService

L, S = PositionSide.LONG, PositionSide.SHORT
NOW = 1_791_943_203_000


@dataclass(frozen=True)
class Member:
    symbol: str
    direction: PositionSide
    score: Decimal = Decimal("1")
    first_selected_ms: int | None = NOW - 1000


class Sync:
    def __init__(self, members=(), valid=True, reason=""):
        self.view = SimpleNamespace(valid=valid, reason=reason, members=tuple(members), snapshot_id=3, fetched_ms=1,
                                    valid_until_ms=NOW + 86_400_000, business_date="d", rejected=())

    def current(self):
        return self.view

    def member(self, symbol):
        return next((m for m in self.view.members if m.symbol == symbol), None)


def make(sync=None, *, can_open=(True, ""), occupancy=None, cooldown=None, tradable=True, config=None):
    cfg = {"require_direction_match": True, "list_member_ttl_h": 0, "trade_notional_usdt": Decimal("10"),
           "intent_ttl_s": 60, "timeframe_short": "15m", "timeframe_long": "1h", "kline_missing_alert_periods": 2}
    cfg.update(config or {})
    registry = SimpleNamespace(occupancy=lambda symbol: occupancy or KeyOccupancy(symbol, None, None),
                               cooldown_until=lambda symbol: cooldown, get_active=lambda *_: None)
    service = StrategyService(
        registry=registry, modelstate=SimpleNamespace(current=lambda: SimpleNamespace(can_open=lambda model: can_open)),
        coordinator=None, candles=None, ledger=None, bus=None, clock=ManualClock(NOW), config_get=cfg.__getitem__,
        notifier=None, rules=SimpleNamespace(get=lambda symbol: SimpleNamespace(tradable=tradable)), limits=None,
        listsync_get=lambda: sync)
    return service


def test_every_precheck_reason_in_order():
    sync = Sync([Member("BTCUSDT", L)])
    assert make(sync).open_block("BTCUSDT", L) is None
    assert "开仓闸" in make(sync, can_open=(False, "开仓闸已停止")).open_block("BTCUSDT", L)
    assert "名单服务" in make(None).open_block("BTCUSDT", L)
    assert "名单无效：接口不可达" in make(Sync(valid=False, reason="接口不可达")).open_block("BTCUSDT", L)
    assert "不在名单内" in make(sync).open_block("ETHUSDT", L)
    assert "名单方向" in make(sync).open_block("BTCUSDT", S)
    assert make(sync, config={"require_direction_match": False}).open_block("BTCUSDT", S) is None
    assert "冷却" in make(sync, cooldown=NOW + 5000).open_block("BTCUSDT", L)
    assert "交易状态" in make(sync, tradable=False).open_block("BTCUSDT", L)


def test_a_member_older_than_the_member_lifetime_is_not_opened_and_fails_the_list_check():
    old = Member("BTCUSDT", L, first_selected_ms=NOW - 3 * 3_600_000)
    service = make(Sync([old]), config={"list_member_ttl_h": 2})
    assert "有效时长" in service.open_block("BTCUSDT", L)
    assert service.list_check("BTCUSDT", L)[0] is False
    assert make(Sync([old]), config={"list_member_ttl_h": 0}).open_block("BTCUSDT", L) is None  # 0 = 当日有效


def test_the_list_hook_wants_a_valid_list_that_still_holds_the_symbol():
    sync = Sync([Member("BTCUSDT", L)])
    assert make(sync).list_check("BTCUSDT", L) == (True, "")
    ok, why = make(sync).list_check("ETHUSDT", L)
    assert not ok and "不在名单" in why
    ok, why = make(Sync(valid=False, reason="过期")).list_check("BTCUSDT", L)
    assert not ok and "过期" in why
    assert make(None).list_check("BTCUSDT", L) == (True, "")  # 没装配名单同步：钩子不起作用


def test_without_a_list_sync_the_watchlist_is_empty_and_invalid_and_nothing_is_monitored():
    service = make(None)
    assert service.watchlist() == {"valid": False, "reason": "名单服务还没有接入", "fetched_ms": None,
                                   "valid_until_ms": None, "business_date": None, "members": [], "rejected": []}
    assert service.list_symbols() == set()


def test_an_invalid_list_monitors_nothing_even_though_it_still_shows_members():
    service = make(Sync([Member("BTCUSDT", L)], valid=False, reason="x"))
    assert service.list_symbols() == set()
    assert [m["symbol"] for m in service.watchlist()["members"]] == ["BTCUSDT"]


def test_open_params_carry_the_notional_and_the_list_facts_as_strings():
    service = make(Sync([Member("BTCUSDT", L, Decimal("0.75"))]))
    params = service._open_params(Member("BTCUSDT", L, Decimal("0.75")))
    assert params == {"notional": "10", "list_direction": "LONG", "list_score": "0.75", "list_snapshot_id": 3}


async def test_a_reversal_past_the_intent_lifetime_is_dropped_without_opening():
    trail: list = []
    service = make(Sync([Member("BTCUSDT", S)]))

    async def fake_trail(action, symbol, side, detail, result="skipped"):
        trail.append((action, symbol, side, detail["reason"]))

    service._trail = fake_trail
    service._coordinator = SimpleNamespace(key_busy=lambda *_: False)
    from dmr_kernel.models.m1_strategy import _Reversal

    rv = _Reversal("BTCUSDT", L, 1, NOW - 120_000, NOW - 61_000, closed=True)
    service._reversals[("BTCUSDT", L)] = rv
    await service._try_reversals()
    assert service._reversals == {} and trail and trail[0][0] == "m1.reverse_cancel" and "有效期" in trail[0][3]


async def test_a_reversal_waits_while_the_old_key_is_still_busy_and_before_the_close_is_seen():
    service = make(Sync([Member("BTCUSDT", S)]))
    submitted: list = []

    async def submit(spec):
        submitted.append(spec)
        return SimpleNamespace(intent_id=1)

    busy = {"value": True}
    service._coordinator = SimpleNamespace(key_busy=lambda *_: busy["value"], submit=submit)
    from dmr_kernel.models.m1_strategy import _Reversal

    rv = _Reversal("BTCUSDT", L, 1, NOW - 5000, NOW - 1000, closed=False)
    service._reversals[("BTCUSDT", L)] = rv
    await service._try_reversals()  # 还没看到平仓
    rv.closed = True
    await service._try_reversals()  # 旧键上的善后还在途
    assert submitted == [] and ("BTCUSDT", L) in service._reversals
    busy["value"] = False
    await service._try_reversals()
    assert len(submitted) == 1 and submitted[0].symbol == "BTCUSDT" and submitted[0].side is S
    assert submitted[0].idem_key.endswith(":rev1") and submitted[0].ttl_s == 60
    assert service._reversals == {}
