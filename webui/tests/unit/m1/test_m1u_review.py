"""模型 1 复核补充（用桩，不起内核）：资金安全相关的几个边界。

- 反手的开仓步必须等旧仓的系统止损已撤（F11 第 6 步），旧仓在平仓前变成了混合仓也不反手；
- 同一根 K 线上刚出了平仓信号的合约，这一轮不按自然信号开仓（对侧仓即使已经很快平掉，也只能走反手；混合仓永不反手）；
- 迟到或乱序的收盘事件（缓存里已有更新的已收盘 K 线）不按旧的那根开仓；
- 新接管的仓（开启了反向信号平仓）不按系统接手之前的历史过零平仓。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import CandlesReady
from dmr_kernel.core.registry import KeyOccupancy
from dmr_kernel.core.types import (
    Candle,
    ExitRuleset,
    Gate,
    IntentAction,
    Model,
    Ownership,
    PositionSide,
    PositionStatus,
)
from dmr_kernel.ledger import repo_m1
from dmr_kernel.models.m1_strategy import StrategyService, _Reversal

L, S = PositionSide.LONG, PositionSide.SHORT
SH, LG = 900_000, 3_600_000
T0 = 1_791_943_200_000  # 整点
NOW = T0 + 3_000  # T0 - SH 那根刚收盘

UP_CROSS = [100.0] * 39 + [101.0]
DOWN_CROSS = [100.0] * 39 + [99.0]
BULL_1H = [100.0 + i for i in range(30)]
BEAR_1H = [200.0 - i for i in range(30)]


@dataclass(frozen=True)
class Member:
    symbol: str
    direction: PositionSide
    score: Decimal = Decimal("1")
    first_selected_ms: int | None = NOW - 1000


class Sync:
    def __init__(self, members=()):
        self.view = SimpleNamespace(valid=True, reason="", members=tuple(members), snapshot_id=3, fetched_ms=1,
                                    valid_until_ms=NOW + 86_400_000, business_date="d", rejected=())

    def current(self):
        return self.view

    def member(self, symbol):
        return next((m for m in self.view.members if m.symbol == symbol), None)


def bars(end_open_ms: int, period: int, mids: list[float]) -> list[Candle]:
    first = end_open_ms - (len(mids) - 1) * period
    return [Candle(first + i * period, m, m, m, m, 1.0, first + (i + 1) * period - 1) for i, m in enumerate(mids)]


class Candles:
    def __init__(self, short: list[Candle], long: list[Candle]):
        self.series = {"15m": short, "1h": long}

    async def refresh(self, symbols, timeframe):
        return None

    def closed(self, symbol, timeframe):
        return list(self.series[timeframe])

    def missing_periods(self, symbol, timeframe):
        return 0


class Ledger:
    def __init__(self):
        self.writes: list = []

        self.exit_bars: dict[str, int] = {}

    async def read(self, fn, *args, **kwargs):
        if fn is repo_m1.last_exit_bar:
            return self.exit_bars.get(args[0])
        return repo_m1.ExitState()

    async def write(self, fn, *args, **kwargs):
        self.writes.append((fn.__name__, args, kwargs))
        if fn is repo_m1.mark_exit_bar:
            self.exit_bars[args[0]] = max(self.exit_bars.get(args[0], args[1]), args[1])
        return 1


class Registry:
    def __init__(self, records=()):
        self.records = list(records)
        self.fetched: dict[int, object] = {}

    def registered(self):
        return list(self.records)

    def occupancy(self, symbol):
        on = {rec.side: rec for rec in self.records if rec.symbol == symbol}
        return KeyOccupancy(symbol, on.get(L), on.get(S))

    def cooldown_until(self, symbol):
        return None

    def get_active(self, symbol, side):
        return next((r for r in self.records if r.symbol == symbol and r.side is side), None)

    async def fetch(self, position_id):
        return self.fetched.get(position_id)


class Coordinator:
    def __init__(self, registry: Registry | None = None, *, close_fast: bool = False):
        self.specs: list = []
        self.registry = registry
        self.close_fast = close_fast

    async def submit(self, spec):
        self.specs.append(spec)
        if self.close_fast and spec.action is IntentAction.CLOSE:  # 平仓极快：登记簿在开仓循环之前就看不到旧仓了
            self.registry.records = [r for r in self.registry.records if r.id != spec.position_id]
        return SimpleNamespace(intent_id=len(self.specs))

    def key_busy(self, symbol, side):
        return False


def position(**changes):
    base = dict(id=1, symbol="BTCUSDT", side=L, generation=1, status=PositionStatus.MANAGED,
                ownership=Ownership.STRATEGY, is_mixed=False, exit_ruleset=ExitRuleset.STRATEGY, signal_exit=False,
                open_time_ms=T0 - 3 * SH, created_ms=T0 - 3 * SH, stop_client_id=None, stop_algo_id=None,
                stale_stops=[], handed_over_stop=None)
    base.update(changes)
    return SimpleNamespace(**base)


def make(*, registry=None, candles=None, sync=None, coordinator=None, config=None):
    cfg = {"require_direction_match": False, "list_member_ttl_h": 0, "trade_notional_usdt": Decimal("10"),
           "intent_ttl_s": 60, "timeframe_short": "15m", "timeframe_long": "1h", "kline_missing_alert_periods": 2}
    cfg.update(config or {})
    registry = registry or Registry()
    state = SimpleNamespace(can_open=lambda model: (True, ""), active_model=Model.M1, gate=Gate.RUNNING)
    service = StrategyService(
        registry=registry, modelstate=SimpleNamespace(current=lambda: state),
        coordinator=coordinator or Coordinator(registry), candles=candles, ledger=Ledger(), bus=None,
        clock=ManualClock(NOW), config_get=cfg.__getitem__, notifier=None,
        rules=SimpleNamespace(get=lambda symbol: SimpleNamespace(tradable=True)), limits=None,
        listsync_get=lambda: sync)
    service._running = True
    return service


def ready(bar_open_ms: int = T0 - SH) -> CandlesReady:
    return CandlesReady(timeframe="15m", bar_open_ms=bar_open_ms, symbols=("BTCUSDT",), at_ms=NOW)


# ---------------------------------------------------------------- 反手：旧止损已撤、旧仓仍是纯策略仓


async def test_a_reversal_waits_until_the_old_system_stop_is_cancelled():
    registry = Registry()
    coordinator = Coordinator(registry)
    service = make(registry=registry, coordinator=coordinator, sync=Sync([Member("BTCUSDT", S)]))
    rv = _Reversal("BTCUSDT", L, 1, T0 - SH, NOW, closed=True)
    rv.position_id = 1
    service._reversals[("BTCUSDT", L)] = rv
    registry.fetched[1] = position(status=PositionStatus.CLOSED, stop_client_id="dmr-stop-1")  # 撤旧止损还没完成
    await service._try_reversals()
    assert coordinator.specs == [] and ("BTCUSDT", L) in service._reversals
    registry.fetched[1] = position(status=PositionStatus.CLOSED)  # 撤掉了
    await service._try_reversals()
    assert [(s.action, s.side) for s in coordinator.specs] == [(IntentAction.OPEN, S)]


async def test_a_position_that_became_mixed_before_it_closed_is_not_reversed():
    registry = Registry()
    coordinator = Coordinator(registry)
    trail: list = []
    service = make(registry=registry, coordinator=coordinator, sync=Sync([Member("BTCUSDT", S)]))

    async def fake_trail(action, symbol, side, detail, result="skipped"):
        trail.append((action, detail["reason"]))

    service._trail = fake_trail
    rv = _Reversal("BTCUSDT", L, 1, T0 - SH, NOW, closed=True)
    rv.position_id = 1
    service._reversals[("BTCUSDT", L)] = rv
    registry.fetched[1] = position(status=PositionStatus.CLOSED, is_mixed=True,
                                   exit_ruleset=ExitRuleset.STRATEGY_NO_REVERSE)
    await service._try_reversals()
    assert coordinator.specs == [] and service._reversals == {}
    assert trail and trail[0][0] == "m1.reverse_cancel" and "混合" in trail[0][1]


# ---------------------------------------------------------------- 同一根 K 线：平仓信号之后不再按自然信号开反向仓


async def test_no_natural_open_on_the_bar_whose_exit_signal_just_closed_a_mixed_position():
    registry = Registry([position(is_mixed=True, exit_ruleset=ExitRuleset.STRATEGY_NO_REVERSE)])
    coordinator = Coordinator(registry, close_fast=True)
    candles = Candles(bars(T0 - SH, SH, DOWN_CROSS), bars(T0 - LG, LG, BEAR_1H))
    service = make(registry=registry, coordinator=coordinator, candles=candles, sync=Sync([Member("BTCUSDT", S)]))
    await service._run(ready())
    assert [(s.action, s.reason) for s in coordinator.specs] == [(IntentAction.CLOSE, "signal")]


# ---------------------------------------------------------------- 迟到、乱序的收盘事件


async def test_a_late_close_event_for_an_older_bar_does_not_open():
    candles = Candles(bars(T0, SH, UP_CROSS + [101.0]), bars(T0 - LG, LG, BULL_1H))  # 缓存里已有更新的 T0 那根
    coordinator = Coordinator()
    service = make(candles=candles, coordinator=coordinator, sync=Sync([Member("BTCUSDT", L)]))
    await service._run(ready(T0 - SH))  # 过零在 T0 - SH，但它已经不是最近一根已收盘 K 线
    assert coordinator.specs == []


async def test_the_latest_bar_still_opens():
    candles = Candles(bars(T0 - SH, SH, UP_CROSS), bars(T0 - LG, LG, BULL_1H))
    coordinator = Coordinator()
    service = make(candles=candles, coordinator=coordinator, sync=Sync([Member("BTCUSDT", L)]))
    await service._run(ready(T0 - SH))
    assert [(s.action, s.side) for s in coordinator.specs] == [(IntentAction.OPEN, L)]


# ---------------------------------------------------------------- 新接管的仓不按接手之前的历史过零平仓


async def test_a_fresh_takeover_is_not_closed_by_a_cross_from_before_the_system_held_it():
    # 外部仓 5 小时前开的，刚刚被接管（开启了反向信号平仓）；对多头的下穿发生在 T0 - 3*SH（接管之前）
    rec = position(ownership=Ownership.TAKEOVER, exit_ruleset=ExitRuleset.TAKEOVER, signal_exit=True,
                   open_time_ms=T0 - 20 * SH, created_ms=NOW - 1000)
    registry = Registry([rec])
    candles = Candles(bars(T0 - SH, SH, [100.0] * 37 + [99.0, 99.0, 99.0]), bars(T0 - LG, LG, BULL_1H))
    coordinator = Coordinator(registry)
    service = make(registry=registry, candles=candles, coordinator=coordinator, sync=Sync())
    await service._run(ready())
    assert coordinator.specs == []


async def test_a_strategy_position_still_judges_bars_since_it_opened():
    # 策略仓在 T0 - 4*SH 那根里开的；之后 T0 - 3*SH 的下穿当时没判到（短周期缺了），现在补判：只平不反
    rec = position(open_time_ms=T0 - 4 * SH + 5000, created_ms=T0 - 4 * SH + 6000)
    registry = Registry([rec])
    candles = Candles(bars(T0 - SH, SH, [100.0] * 37 + [99.0, 99.0, 99.0]), bars(T0 - LG, LG, BEAR_1H))
    coordinator = Coordinator(registry)
    service = make(registry=registry, candles=candles, coordinator=coordinator, sync=Sync())
    await service._run(ready())
    assert [(s.action, s.reason) for s in coordinator.specs] == [(IntentAction.CLOSE, "signal")]


# ---------------------------------------------------------------- 迟到的收盘事件：出场只平不反


async def test_a_late_close_event_reverse_cross_closes_only_and_never_reverses():
    # 多头策略仓；下穿在 T0 - SH，长周期偏空（本该反手），但缓存里已有更新的 T0 那根：迟到的事件只平不反
    registry = Registry([position()])
    candles = Candles(bars(T0, SH, DOWN_CROSS + [99.0]), bars(T0 - LG, LG, BEAR_1H))
    coordinator = Coordinator(registry)
    service = make(registry=registry, candles=candles, coordinator=coordinator, sync=Sync([Member("BTCUSDT", S)]))
    await service._run(ready(T0 - SH))
    assert [(s.action, s.reason) for s in coordinator.specs] == [(IntentAction.CLOSE, "signal")]
    assert service._reversals == {}


async def test_the_same_reverse_cross_on_the_latest_bar_still_reverses():
    registry = Registry([position()])
    candles = Candles(bars(T0 - SH, SH, DOWN_CROSS), bars(T0 - LG, LG, BEAR_1H))
    coordinator = Coordinator(registry)
    service = make(registry=registry, candles=candles, coordinator=coordinator, sync=Sync([Member("BTCUSDT", S)]))
    await service._run(ready(T0 - SH))
    assert [(s.action, s.reason) for s in coordinator.specs] == [(IntentAction.CLOSE, "reverse")]
    assert ("BTCUSDT", L) in service._reversals


# ---------------------------------------------------------------- 过了 valid_until 的名单视图（即使替身仍说 valid）绝不当有效


def test_a_view_past_its_valid_until_is_never_treated_as_valid():
    sync = Sync([Member("BTCUSDT", L)])
    sync.view.valid_until_ms = NOW - 1  # 名单同步没来得及重算，视图仍写着 valid=True
    service = make(sync=sync)
    assert service.open_block("BTCUSDT", L) is not None
    assert service.list_check("BTCUSDT", L)[0] is False
    assert service.list_symbols() == set()
    assert service.watchlist()["valid"] is False
