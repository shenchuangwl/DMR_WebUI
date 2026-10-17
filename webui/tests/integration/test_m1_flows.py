"""模型 1 全流程（T3.2）：完整装配的内核 + 进程内替身 + 假名单；K 线由 StaticFeed 设定（DMR12 过零、DMR6 同向）。

覆盖计划 M4 第 1 节验收里与模型 1 有关的各条：信号开仓并挂止损、预检的各种跳过与留痕（闸关、名单方向、冷却、外部仓、避让、
上限、名单失效）、同一根 K 线内重启不产生第二个意图、信号平仓（只平）与反手（先平后开、时限）、永不反手（闸关、混合仓、接管仓）、
长周期缺失的补判、短周期缺一根后的补判、信号注入、``GET /watchlist``、名单有效接到启动自检。
名单用满足 ``ListSync`` 接口的假对象（``current``/``member``/``poll``）；测试里不联网。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from dmr_kernel.core.intents import IntentSpec, key_open, key_signal_close
from dmr_kernel.core.types import (
    ExitReason,
    Gate,
    IntentAction,
    IntentSource,
    IntentState,
    Ownership,
    PendingReason,
    PositionSide,
    PositionStatus,
)
from dmr_kernel.ledger import repo_audit, repo_m1
from dmr_kernel.ledger.models import Intent
from dmr_kernel.ledger.records import IntentRecord
from tests.support.inproc import AUTH_HEADER
from tests.support.kernel import FakeRecovery, abandon, api_client, make_kernel, make_standin_world, settle

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
SH, LG = 900_000, 3_600_000
T0 = 1_791_943_200_000  # 整点
NOW = T0 + 3_000  # 收盘后 3 秒：最近一根已收盘的短周期开盘时间是 T0 - SH，长周期是 T0 - LG
BASE = "/api/v1"

UP_CROSS = [100.0] * 39 + [101.0]  # 最后一根 DMR12 由 0 变正
DOWN_CROSS = [100.0] * 39 + [99.0]  # 最后一根 DMR12 由 0 变负
BULL_1H = [100.0 + i for i in range(30)]
BEAR_1H = [200.0 - i for i in range(30)]


# ---------------------------------------------------------------- 假名单（满足 ListSync 的接口）


@dataclass(frozen=True)
class Member:
    symbol: str
    direction: PositionSide
    score: Decimal
    first_selected_ms: int | None = T0 - 3_600_000
    raw_code: str = ""


@dataclass(frozen=True)
class View:
    valid: bool
    reason: str
    snapshot_id: int | None
    fetched_ms: int | None
    valid_until_ms: int | None
    business_date: str | None
    status: str | None
    members: tuple[Member, ...]
    rejected: tuple[tuple[str, str], ...]


class FakeListSync:
    def __init__(self, members: list[Member], *, valid: bool = True, reason: str = "") -> None:
        self.view = View(valid, reason, 7, NOW, T0 + 86_400_000, "2026-10-17", "ok", tuple(members), (("坏币", "查不到"),))
        self.polls = 0

    def current(self) -> View:
        return self.view

    def member(self, symbol: str) -> Member | None:
        return next((m for m in self.view.members if m.symbol == symbol), None)

    async def poll(self) -> View:
        self.polls += 1
        return self.view

    def set(self, **changes: Any) -> None:
        self.view = replace(self.view, **changes)


def member(symbol: str, direction: PositionSide = LONG, score: str = "1") -> Member:
    return Member(symbol, direction, D(score), raw_code=symbol)


# ---------------------------------------------------------------- K 线与世界


def set_bars(standin, symbol: str, timeframe: str, end_open_ms: int, mids: list[float]) -> None:
    """以 end_open_ms 为最后一根已收盘 K 线的开盘时间，设定一串连续的 K 线（高低都取中价）。"""
    period = SH if timeframe == "15m" else LG
    first = end_open_ms - (len(mids) - 1) * period
    rows = [[first + i * period, str(m), str(m), str(m), str(m), "1", first + (i + 1) * period - 1]
            for i, m in enumerate(mids)]
    standin.feed.set_candles(symbol, timeframe, rows)


def set_market(standin, symbol: str, mids_15m: list[float], mids_1h: list[float] | None = BULL_1H, *,
               short_end: int = T0 - SH, long_end: int = T0 - LG) -> None:
    set_bars(standin, symbol, "15m", short_end, mids_15m)
    if mids_1h is not None:
        set_bars(standin, symbol, "1h", long_end, mids_1h)


@asynccontextmanager
async def world(tmp_path, members: list[Member] | None = None, *, config: dict | None = None, standin=None,
                listsync: FakeListSync | None = None, running: bool = True, advance: bool = True):
    standin = standin or make_standin_world(equity="100000")
    if advance:
        standin.clock.advance(NOW - standin.clock.now_ms())
    overrides = {"trade_notional_usdt": D("500"), **(config or {})}
    kernel, standin = make_kernel(tmp_path, standin=standin, recovery_factory=FakeRecovery(), config_overrides=overrides)
    ls = listsync or FakeListSync(members or [])
    kernel.set_listsync(ls)
    await kernel.start(serve_http=False, run_jobs=False)
    try:
        await prepare(kernel, running=running)
        yield kernel, standin, ls
    finally:
        await kernel.stop()


async def prepare(kernel, *, running: bool = True) -> None:
    await kernel.jobs["reconcile_positions"]()
    await kernel.jobs["reconcile_orders"]()
    if running:
        await kernel.modelstate.set_gate(Gate.RUNNING, reason="test")
    await settle(kernel)


async def close_bar(kernel, *, rounds: int = 2) -> None:
    """短周期收盘作业（CandlesReady 由模型 1 处理）→ 等派发完 → 补拉成交让登记簿跟上。"""
    await kernel.jobs["bar_close_short"]()
    await settle(kernel)
    for _ in range(rounds):
        await kernel.jobs["backfill_trades"]()
        await settle(kernel)


async def next_bar(kernel, standin, bars: int = 1) -> None:
    standin.clock.advance(bars * SH)
    await kernel.jobs["reconcile_positions"]()  # 时钟跳了很多：持仓与挂单快照要重新对一次，否则按过期处理
    await kernel.jobs["reconcile_orders"]()
    await close_bar(kernel)


def exit_market(standin, *, bearish: bool, symbol: str = "BTCUSDT") -> None:
    """入场（UP_CROSS，T0-SH 收盘）之后过了 5 根：T0+4*SH 这根 DMR12 由正变负（对多头是反向过零），
    长周期新出的那根（T0，整点后第一根）DMR6 为负（bearish）或为正。长周期只在整点换根，所以反手要隔一个整点。"""
    set_bars(standin, symbol, "15m", T0 + 4 * SH, UP_CROSS + [101.0] * 4 + [98.0])
    set_bars(standin, symbol, "1h", T0, BULL_1H + [90.0 if bearish else 135.0])


async def intents(kernel) -> list[IntentRecord]:
    def read(session):
        return [IntentRecord.from_row(row) for row in session.scalars(select(Intent).order_by(Intent.id))]

    return await kernel.ledger.read(read)


async def intent_keys(kernel, prefix: str = "") -> list[str]:
    return [i.idem_key for i in await intents(kernel) if i.idem_key.startswith(prefix)]


async def audit(kernel, action: str) -> list:
    return await kernel.ledger.read(repo_audit.recent, limit=200, action=action)


def position(kernel, symbol: str, side: PositionSide = LONG):
    return kernel.registry.get_active(symbol, side)


async def opened_long(kernel, standin, symbol: str = "BTCUSDT") -> None:
    set_market(standin, symbol, UP_CROSS)
    await close_bar(kernel)
    assert position(kernel, symbol) is not None


def btc_world(tmp_path, **kwargs):
    return world(tmp_path, [member("BTCUSDT")], **kwargs)


# ====================================================================== 开仓（F1）


async def test_a_natural_signal_opens_a_strategy_position_with_a_stop_and_list_facts(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, _):
        set_market(standin, "BTCUSDT", UP_CROSS)
        await close_bar(kernel)
        rec = position(kernel, "BTCUSDT")
        assert rec is not None and rec.status in (PositionStatus.MANAGED, PositionStatus.TAKING_OVER)
        assert rec.ownership is Ownership.STRATEGY and rec.list_snapshot_id == 7 and rec.list_direction == "LONG"
        assert rec.list_score == D("1") and rec.open_time_ms >= NOW
        assert await intent_keys(kernel, "open:") == [key_open("m1", "BTCUSDT", LONG, T0 - SH)]
        await kernel.jobs["protection_tick"]()
        await settle(kernel)
        assert position(kernel, "BTCUSDT").stop_price is not None  # 开仓后挂了止损


async def test_nothing_opens_without_a_signal_or_for_a_symbol_outside_the_list(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, _):
        set_market(standin, "BTCUSDT", [100.0] * 40)  # 没有过零
        set_market(standin, "ETHUSDT", UP_CROSS)  # 有信号但不在名单
        await close_bar(kernel)
        assert await intents(kernel) == []


async def test_a_short_signal_against_a_bullish_long_trend_does_not_open(tmp_path):
    async with btc_world(tmp_path, config={"require_direction_match": False}) as (kernel, standin, _):
        set_market(standin, "BTCUSDT", DOWN_CROSS, BULL_1H)
        await close_bar(kernel)
        assert await intents(kernel) == []


async def test_a_missing_long_period_gives_no_open_signal(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, _):
        set_market(standin, "BTCUSDT", UP_CROSS, None)
        await close_bar(kernel)
        assert await intents(kernel) == []


async def test_list_direction_must_match_the_signal_and_the_skip_is_left_in_the_audit_log(tmp_path):
    async with world(tmp_path, [member("BTCUSDT", SHORT)]) as (kernel, standin, _):
        set_market(standin, "BTCUSDT", UP_CROSS)
        await close_bar(kernel)
        assert await intents(kernel) == []
        (skip,) = await audit(kernel, "m1.skip_open")
        assert skip.target == "BTCUSDT:LONG" and "名单方向" in skip.after["reason"]


async def test_direction_match_can_be_switched_off(tmp_path):
    async with world(tmp_path, [member("BTCUSDT", SHORT)], config={"require_direction_match": False}) as (kernel, standin, _):
        set_market(standin, "BTCUSDT", UP_CROSS)
        await close_bar(kernel)
        assert position(kernel, "BTCUSDT") is not None


async def test_a_closed_gate_blocks_opening_and_says_so(tmp_path):
    async with btc_world(tmp_path, running=False) as (kernel, standin, _):
        set_market(standin, "BTCUSDT", UP_CROSS)
        await close_bar(kernel)
        assert await intents(kernel) == []
        (skip,) = await audit(kernel, "m1.skip_open")
        assert "开仓闸" in skip.after["reason"]


async def test_a_suppression_blocks_opening(tmp_path):
    from dmr_kernel.core.types import Suppression

    async with btc_world(tmp_path) as (kernel, standin, _):
        await kernel.modelstate.add_suppression(Suppression.RECONCILE_UNKNOWN, reason="test")
        set_market(standin, "BTCUSDT", UP_CROSS)
        await close_bar(kernel)
        assert await intents(kernel) == []
        assert "抑制" in (await audit(kernel, "m1.skip_open"))[0].after["reason"]


async def test_an_invalid_list_blocks_opening(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, ls):
        ls.set(valid=False, reason="接口不可达", members=())
        set_market(standin, "BTCUSDT", UP_CROSS)
        await close_bar(kernel)
        assert await intents(kernel) == []


async def test_the_cooldown_blocks_opening(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, _):
        await kernel.registry.set_cooldown("BTCUSDT", NOW + 3_600_000, "test")
        set_market(standin, "BTCUSDT", UP_CROSS)
        await close_bar(kernel)
        assert await intents(kernel) == []
        assert "冷却" in (await audit(kernel, "m1.skip_open"))[0].after["reason"]


async def test_an_external_position_on_either_side_blocks_both_directions(tmp_path):
    for external_side, signal_trend in ((SHORT, UP_CROSS), (LONG, UP_CROSS)):
        sub = tmp_path / external_side.value
        async with btc_world(sub) as (kernel, standin, _):
            await kernel.registry.register_pending(symbol="BTCUSDT", side=external_side, qty=D("0.01"),
                                                   avg_price=D("50000"), reason=PendingReason.PREEXISTING)
            set_market(standin, "BTCUSDT", signal_trend)
            await close_bar(kernel)
            assert await intents(kernel) == []
            assert "待确认" in (await audit(kernel, "m1.skip_open"))[0].after["reason"]


async def test_the_symbol_cap_rejects_the_lower_scored_signal_and_leaves_a_trace(tmp_path):
    members = [member("ETHUSDT", LONG, "0.5"), member("BTCUSDT", LONG, "0.9")]
    async with world(tmp_path, members, config={"max_symbols": 1}) as (kernel, standin, _):
        set_market(standin, "BTCUSDT", UP_CROSS)
        set_market(standin, "ETHUSDT", UP_CROSS)
        await close_bar(kernel)
        rows = [r for r in await intents(kernel) if r.action is IntentAction.OPEN]
        assert [r.symbol for r in rows] == ["BTCUSDT", "ETHUSDT"]  # 分数高的先提交
        assert rows[0].state is IntentState.COMPLETED
        assert rows[1].state is IntentState.REJECTED and "上限" in rows[1].state_reason
        assert position(kernel, "BTCUSDT") is not None and position(kernel, "ETHUSDT") is None


async def test_the_list_hook_rejects_an_intent_whose_symbol_left_the_list(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, ls):
        ls.set(members=())
        spec = IntentSpec(action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol="BTCUSDT", side=LONG,
                          idem_key="hook:1", ttl_s=60, params={"notional": "500"})
        result = await kernel.coordinator.submit(spec)
        rec = await kernel.coordinator.wait_terminal(result.intent_id, 10)
        assert rec.state is IntentState.REJECTED and "名单复核未通过" in rec.state_reason


async def test_a_restart_inside_the_same_bar_creates_no_second_intent(tmp_path):
    standin = make_standin_world(equity="100000")
    async with btc_world(tmp_path, standin=standin) as (kernel, _, _):
        set_market(standin, "BTCUSDT", UP_CROSS)
        await close_bar(kernel)
        rec = position(kernel, "BTCUSDT")
        assert rec is not None
        # 把仓平掉（不冷却、不挡下一次），再在「同一根 K 线内」重启并重放这一根
        closing = await kernel.coordinator.submit(IntentSpec(
            action=IntentAction.CLOSE, model="m1", source=IntentSource.OPERATOR, symbol="BTCUSDT", side=LONG,
            idem_key="op:close:1", reason="manual", position_id=rec.id, position_generation=rec.generation))
        await kernel.coordinator.wait_terminal(closing.intent_id, 10)
        await kernel.jobs["backfill_trades"]()
        await settle(kernel)
        assert position(kernel, "BTCUSDT") is None
        await abandon(kernel)
    async with btc_world(tmp_path, standin=standin, advance=False) as (kernel2, _, _):
        await close_bar(kernel2)
        assert await intent_keys(kernel2, "open:") == [key_open("m1", "BTCUSDT", LONG, T0 - SH)]
        assert position(kernel2, "BTCUSDT") is None


# ====================================================================== 信号平仓与反手（F11）


async def test_a_reverse_cross_with_a_same_direction_long_trend_closes_only(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, _):
        await opened_long(kernel, standin)
        exit_market(standin, bearish=False)
        await next_bar(kernel, standin, 5)
        assert position(kernel, "BTCUSDT") is None and position(kernel, "BTCUSDT", SHORT) is None
        closed = await kernel.registry.fetch(1)
        assert closed.exit_reason is ExitReason.SIGNAL
        assert await intent_keys(kernel, "sigclose:") == [key_signal_close("BTCUSDT", LONG, closed.generation, T0 + 4 * SH)]
        assert await intent_keys(kernel, "open:") == [key_open("m1", "BTCUSDT", LONG, T0 - SH)]


async def test_a_reverse_cross_against_the_long_trend_reverses_when_everything_allows(tmp_path):
    async with btc_world(tmp_path, config={"require_direction_match": False}) as (kernel, standin, _):
        await opened_long(kernel, standin)
        exit_market(standin, bearish=True)
        await next_bar(kernel, standin, 5)
        old = await kernel.registry.fetch(1)
        assert old.exit_reason is ExitReason.REVERSE
        new = position(kernel, "BTCUSDT", SHORT)
        assert new is not None and new.ownership is Ownership.STRATEGY and new.generation >= 1
        reverse_key = key_open("m1", "BTCUSDT", SHORT, T0 + 4 * SH, reverse_of_generation=old.generation)
        assert reverse_key in await intent_keys(kernel)
        assert position(kernel, "BTCUSDT", LONG) is None


async def test_the_reverse_open_goes_through_the_full_prechecks_including_the_list_direction(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, ls):  # 名单方向仍是 LONG：反手的空头开仓被挡
        await opened_long(kernel, standin)
        exit_market(standin, bearish=True)
        await next_bar(kernel, standin, 5)
        assert (await kernel.registry.fetch(1)).exit_reason is ExitReason.REVERSE
        assert position(kernel, "BTCUSDT") is None and position(kernel, "BTCUSDT", SHORT) is None
        cancels = await audit(kernel, "m1.reverse_cancel")
        assert cancels and "名单方向" in cancels[0].after["reason"]


async def test_a_reversal_follows_the_list_when_it_now_says_short(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, ls):
        await opened_long(kernel, standin)
        ls.set(members=(member("BTCUSDT", SHORT),))
        exit_market(standin, bearish=True)
        await next_bar(kernel, standin, 5)
        assert position(kernel, "BTCUSDT", SHORT) is not None


async def test_a_reversal_that_is_too_late_is_given_up_with_a_trace(tmp_path):
    async with btc_world(tmp_path, config={"require_direction_match": False}) as (kernel, standin, _):
        await opened_long(kernel, standin)
        exit_market(standin, bearish=True)
        original = kernel.m1._try_reversal

        async def late(key, rv):  # 平仓单完成得太晚：信号后已经过了意图有效期
            standin.clock.advance((int(kernel.config.get("intent_ttl_s")) + 1) * 1000)
            await original(key, rv)

        kernel.m1._try_reversal = late
        await next_bar(kernel, standin, 5)
        assert (await kernel.registry.fetch(1)).exit_reason is ExitReason.REVERSE
        assert position(kernel, "BTCUSDT", SHORT) is None
        assert "有效期" in (await audit(kernel, "m1.reverse_cancel"))[0].after["reason"]


async def test_a_closed_gate_means_never_reverse_but_the_close_still_happens(tmp_path):
    async with btc_world(tmp_path, config={"require_direction_match": False}) as (kernel, standin, _):
        await opened_long(kernel, standin)
        await kernel.modelstate.set_gate(Gate.STOPPED, reason="test")
        exit_market(standin, bearish=True)
        await next_bar(kernel, standin, 5)
        assert position(kernel, "BTCUSDT") is None and position(kernel, "BTCUSDT", SHORT) is None
        assert (await kernel.registry.fetch(1)).exit_reason is ExitReason.SIGNAL
        assert len(await intent_keys(kernel, "open:")) == 1


async def test_a_mixed_position_is_closed_but_never_reversed(tmp_path):
    async with btc_world(tmp_path, config={"require_direction_match": False}) as (kernel, standin, _):
        await opened_long(kernel, standin)
        await standin.disp.handle("POST", "/dev/manual/order", {}, json.dumps(
            {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "qty": "0.001"}).encode(),
            {AUTH_HEADER: standin.manual_token})
        await kernel.jobs["reconcile_positions"]()
        await kernel.jobs["backfill_trades"]()
        await settle(kernel)
        assert position(kernel, "BTCUSDT").is_mixed
        exit_market(standin, bearish=True)
        await next_bar(kernel, standin, 5)
        assert position(kernel, "BTCUSDT") is None and position(kernel, "BTCUSDT", SHORT) is None
        assert (await kernel.registry.fetch(1)).exit_reason is ExitReason.SIGNAL
        assert len(await intent_keys(kernel, "open:")) == 1


async def test_a_takeover_position_follows_signal_exit_only_when_enabled_and_never_reverses(tmp_path):
    for enabled in (False, True):
        async with btc_world(tmp_path / str(enabled), config={"require_direction_match": False}) as (kernel, standin, _):
            rec = await kernel.registry.begin_takeover(
                symbol="BTCUSDT", side=LONG, qty=D("0.01"), avg_price=D("50000"), open_time_ms=NOW,
                open_time_estimated=False, anchor_ms=NOW, signal_exit=enabled)
            await kernel.registry.mark_managed(rec.id)
            exit_market(standin, bearish=True)
            await next_bar(kernel, standin, 5)
            assert bool(await intent_keys(kernel, "sigclose:")) is enabled
            assert await intent_keys(kernel, "open:") == []


async def test_exit_is_computed_even_after_the_model_is_switched_away(tmp_path):
    from dmr_kernel.core.types import Model

    async with btc_world(tmp_path) as (kernel, standin, _):
        await opened_long(kernel, standin)
        await kernel.modelstate.set_gate(Gate.STOPPED, reason="test")
        await kernel.modelstate.set_active_model(Model.M2, expected_version=kernel.modelstate.current().version,
                                                 operator="t", op_id="sw1")
        exit_market(standin, bearish=False)
        await next_bar(kernel, standin, 5)
        assert position(kernel, "BTCUSDT") is None


async def test_a_missing_long_period_is_made_up_later_and_only_closes(tmp_path):
    async with btc_world(tmp_path, config={"require_direction_match": False}) as (kernel, standin, _):
        await opened_long(kernel, standin)
        exit_market(standin, bearish=True)
        set_bars(standin, "BTCUSDT", "1h", T0 - LG, BULL_1H)  # 整点后新出的那根长周期还取不到
        await next_bar(kernel, standin, 5)
        rec = position(kernel, "BTCUSDT")
        assert rec is not None and await intent_keys(kernel, "sigclose:") == []
        state = await kernel.ledger.read(repo_m1.get_exit_state, "BTCUSDT", LONG, rec.generation)
        assert state.pending == (T0 + 4 * SH,)  # 待补判
        # 长周期恢复（趋势对多头是空头，正常会反手），下一根短周期收盘时补判：只平不反
        set_bars(standin, "BTCUSDT", "15m", T0 + 5 * SH, UP_CROSS + [101.0] * 4 + [98.0, 97.0])
        set_bars(standin, "BTCUSDT", "1h", T0, BULL_1H + [90.0])
        await next_bar(kernel, standin)
        assert position(kernel, "BTCUSDT") is None and position(kernel, "BTCUSDT", SHORT) is None
        assert (await kernel.registry.fetch(1)).exit_reason is ExitReason.SIGNAL
        assert len(await intent_keys(kernel, "open:")) == 1
        assert await intent_keys(kernel, "sigclose:") == [
            key_signal_close("BTCUSDT", LONG, rec.generation, T0 + 4 * SH)]


async def test_a_short_period_bar_that_arrives_late_is_still_judged_and_only_closes(tmp_path):
    async with btc_world(tmp_path, config={"require_direction_match": False}) as (kernel, standin, _):
        await opened_long(kernel, standin)
        rec = position(kernel, "BTCUSDT")
        # T0+4*SH 这根短周期还没取到（交易所只给到 T0+3*SH）：这一轮不判
        set_bars(standin, "BTCUSDT", "15m", T0 + 3 * SH, UP_CROSS + [101.0] * 4)
        set_bars(standin, "BTCUSDT", "1h", T0, BULL_1H + [90.0])
        await next_bar(kernel, standin, 5)
        assert position(kernel, "BTCUSDT") is not None
        # 下一轮这一根补到了，反向过零就发生在上一根（T0+4*SH）：逐根补判，命中也只平不反
        set_bars(standin, "BTCUSDT", "15m", T0 + 5 * SH, UP_CROSS + [101.0] * 4 + [98.0, 97.0])
        await next_bar(kernel, standin)
        assert position(kernel, "BTCUSDT") is None and position(kernel, "BTCUSDT", SHORT) is None
        assert await intent_keys(kernel, "sigclose:") == [
            key_signal_close("BTCUSDT", LONG, rec.generation, T0 + 4 * SH)]
        assert len(await intent_keys(kernel, "open:")) == 1


async def test_exit_progress_survives_a_restart_and_a_bar_is_not_judged_twice(tmp_path):
    standin = make_standin_world(equity="100000")
    async with btc_world(tmp_path, standin=standin) as (kernel, _, _):
        await opened_long(kernel, standin)
        set_market(standin, "BTCUSDT", UP_CROSS + [101.5], BULL_1H, short_end=T0)
        await next_bar(kernel, standin)
        rec = position(kernel, "BTCUSDT")
        assert (await kernel.ledger.read(repo_m1.get_exit_state, "BTCUSDT", LONG, rec.generation)).judged_ms == T0
        await abandon(kernel)
    async with btc_world(tmp_path, standin=standin, advance=False) as (kernel2, _, _):
        rec = position(kernel2, "BTCUSDT")
        assert (await kernel2.ledger.read(repo_m1.get_exit_state, "BTCUSDT", LONG, rec.generation)).judged_ms == T0
        await close_bar(kernel2)
        assert await intent_keys(kernel2, "sigclose:") == []


# ====================================================================== 信号注入、名单页、装配


async def post(client, token, path, body):
    return await client.post(BASE + path, json=body, headers={"Authorization": f"Bearer {token}"})


async def test_an_injected_signal_opens_through_the_natural_path_and_can_be_repeated(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, _):
        token = kernel.app.state.ctx.local_token
        async with api_client(kernel) as client:
            ok = await post(client, token, "/dev/inject_signal", {"op_id": "inj-1", "symbol": "BTCUSDT",
                                                                    "short_cross": "up", "long_trend": "bullish"})
            assert ok.status_code == 200, ok.text
            body = ok.json()
            assert body["status"] == "succeeded" and body["result"]["signal"] == "LONG"
            assert body["result"]["state"] == "COMPLETED" and body["result"]["injected_seq"] == 1
            await kernel.jobs["backfill_trades"]()
            await settle(kernel)
            assert position(kernel, "BTCUSDT") is not None
            again = await post(client, token, "/dev/inject_signal", {"op_id": "inj-1", "symbol": "BTCUSDT",
                                                                       "short_cross": "up", "long_trend": "bullish"})
            assert again.json() == body  # 同一个操作号：原样重放
        keys = await intent_keys(kernel, "open:")
        assert len(keys) == 1 and keys[0].endswith(":inj1")
        rows = await intents(kernel)
        assert rows[0].source is IntentSource.INJECTED
        record = await kernel.operations.get("inj-1")
        assert record.op_type.value == "inject_signal" and record.status.value == "succeeded"


async def test_injection_goes_through_the_same_prechecks_and_reports_why_not(tmp_path):
    async with btc_world(tmp_path) as (kernel, standin, _):
        token = kernel.app.state.ctx.local_token
        async with api_client(kernel) as client:
            async def inject(op, symbol, cross, trend):
                response = await post(client, token, "/dev/inject_signal", {"op_id": op, "symbol": symbol,
                                                                           "short_cross": cross, "long_trend": trend})
                return response.status_code, response.json()

            code, body = await inject("i-a", "BTCUSDT", "up", "missing")
            assert code == 200 and body["status"] == "failed" and not body["result"]["submitted"]
            code, body = await inject("i-b", "ETHUSDT", "up", "bullish")  # 不在名单
            assert body["status"] == "failed" and "不在名单" in body["result"]["reason"]
            code, body = await inject("i-c", "BTCUSDT", "down", "bearish")  # 名单方向是 LONG
            assert body["status"] == "failed" and "名单方向" in body["result"]["reason"]
            await kernel.registry.set_cooldown("BTCUSDT", NOW + 1_000_000, "t")
            code, body = await inject("i-d", "BTCUSDT", "up", "bullish")
            assert body["status"] == "failed" and "冷却" in body["result"]["reason"]
            code, _ = await inject("i-e", "NOPEUSDT", "up", "bullish")
            assert code == 422
            code, _ = await inject("i-f", "BTCUSDT", "sideways", "bullish")
            assert code == 422
        assert await intents(kernel) == []


async def test_watchlist_shows_members_held_flags_and_why_a_member_is_avoided(tmp_path):
    members = [member("BTCUSDT", LONG, "0.9"), member("ETHUSDT", SHORT, "0.5")]
    async with world(tmp_path, members) as (kernel, standin, ls):
        await kernel.registry.register_pending(symbol="ETHUSDT", side=LONG, qty=D("0.1"), avg_price=D("3000"),
                                               reason=PendingReason.PREEXISTING)
        set_market(standin, "BTCUSDT", UP_CROSS)
        await close_bar(kernel)
        token = kernel.app.state.ctx.local_token
        async with api_client(kernel, token) as client:
            response = await client.get(BASE + "/watchlist")
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"valid", "reason", "fetched_ms", "valid_until_ms", "business_date", "members", "rejected"}
        assert body["valid"] is True and body["business_date"] == "2026-10-17" and body["fetched_ms"] == NOW
        btc, eth = body["members"]
        assert set(btc) == {"symbol", "direction", "score", "first_selected_ms", "held", "avoid_reason"}
        assert (btc["symbol"], btc["direction"], btc["score"], btc["held"]) == ("BTCUSDT", "LONG", "0.9", True)
        assert "同方向已有策略仓" in btc["avoid_reason"]
        assert eth["held"] is True and "待确认" in eth["avoid_reason"]  # 外部仓：被避让
        assert body["rejected"] == [{"code": "坏币", "reason": "查不到"}]


async def test_watchlist_marks_a_clean_member_as_not_avoided_and_an_invalid_list_as_invalid(tmp_path):
    async with btc_world(tmp_path) as (kernel, _, ls):
        token = kernel.app.state.ctx.local_token
        async with api_client(kernel, token) as client:
            body = (await client.get(BASE + "/watchlist")).json()
            assert body["members"][0]["avoid_reason"] is None and body["members"][0]["held"] is False
            ls.set(valid=False, reason="接口不可达")
            body = (await client.get(BASE + "/watchlist")).json()
            assert body["valid"] is False and body["reason"] == "接口不可达"


async def test_the_watchlist_flags_a_full_book_as_avoided(tmp_path):
    members = [member("BTCUSDT", LONG, "0.9"), member("ETHUSDT", LONG, "0.5")]
    async with world(tmp_path, members, config={"max_symbols": 1}) as (kernel, standin, _):
        set_market(standin, "BTCUSDT", UP_CROSS)
        await close_bar(kernel)
        await kernel.jobs["reconcile_positions"]()
        await settle(kernel)
        reasons = {m["symbol"]: m["avoid_reason"] for m in kernel.m1.watchlist()["members"]}
        assert "上限" in reasons["ETHUSDT"]


async def test_start_self_check_follows_the_list_and_the_poll_job_calls_the_list_sync(tmp_path):
    async with btc_world(tmp_path) as (kernel, _, ls):
        deps = kernel.app.state.ops_services.deps
        assert deps.list_valid() is True and kernel.list_valid() is True
        ls.set(valid=False, reason="x")
        assert deps.list_valid() is False
        await kernel.jobs["list_poll"]()
        assert ls.polls == 1


async def test_monitored_symbols_include_the_list_members_and_the_registered_positions(tmp_path):
    members = [member("BTCUSDT"), member("ETHUSDT")]
    async with world(tmp_path, members) as (kernel, _, ls):
        assert await kernel.monitored_symbols() == ["BTCUSDT", "ETHUSDT"]
        ls.set(valid=False, reason="x", members=())
        assert await kernel.monitored_symbols() == []
        await kernel.registry.begin_takeover(symbol="DOGEUSDT", side=LONG, qty=D("100"), avg_price=D("0.2"),
                                             open_time_ms=NOW, open_time_estimated=False, anchor_ms=NOW,
                                             signal_exit=False)
        assert await kernel.monitored_symbols() == ["DOGEUSDT"]


# ====================================================================== 复核补充：重放平仓信号那根 K 线


async def test_replaying_the_exit_bar_after_a_mixed_position_closed_never_opens_the_other_side(tmp_path):
    """混合仓被信号平掉后，同一根 K 线的收盘事件再来一次（重复投递或重启后补跑）：反向的自然信号也不能开，否则等于反手。"""
    async with btc_world(tmp_path, config={"require_direction_match": False}) as (kernel, standin, _):
        await opened_long(kernel, standin)
        await standin.disp.handle("POST", "/dev/manual/order", {}, json.dumps(
            {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "qty": "0.001"}).encode(),
            {AUTH_HEADER: standin.manual_token})
        await kernel.jobs["reconcile_positions"]()
        await kernel.jobs["backfill_trades"]()
        await settle(kernel)
        assert position(kernel, "BTCUSDT").is_mixed
        exit_market(standin, bearish=True)
        await next_bar(kernel, standin, 5)
        assert position(kernel, "BTCUSDT") is None
        await close_bar(kernel)  # 同一根 K 线再处理一次
        assert position(kernel, "BTCUSDT", SHORT) is None
        assert len(await intent_keys(kernel, "open:")) == 1


async def test_replaying_the_exit_bar_after_a_restart_never_opens_the_other_side(tmp_path):
    standin = make_standin_world(equity="100000")
    async with btc_world(tmp_path, standin=standin, running=True) as (kernel, _, _):
        await opened_long(kernel, standin)
        await kernel.modelstate.set_gate(Gate.STOPPED, reason="test")  # 闸关：只平不反
        exit_market(standin, bearish=True)
        await next_bar(kernel, standin, 5)
        assert position(kernel, "BTCUSDT") is None
        await abandon(kernel)
    async with btc_world(tmp_path, standin=standin, advance=False,
                         config={"require_direction_match": False}) as (kernel2, _, _):  # 重启后闸又开了
        await close_bar(kernel2)
        assert position(kernel2, "BTCUSDT", SHORT) is None
        assert len(await intent_keys(kernel2, "open:")) == 1


async def test_the_signal_close_ignores_suppressions_and_the_risk_gate_but_never_reverses_under_them(tmp_path):
    """平仓意图是减仓：抑制原因与风控闸都挡不住它（F11 第 3 步）；反手的开仓步照样被挡、只平不反并留痕。"""
    from dmr_kernel.core.types import RiskDecision, Suppression

    async with btc_world(tmp_path, config={"require_direction_match": False}) as (kernel, standin, _):
        await opened_long(kernel, standin)
        await kernel.modelstate.add_suppression(Suppression.MUTEX_ANOMALY, reason="test")
        evaluated: list = []

        async def deny(*, model, action, now_ms):
            evaluated.append(action)
            return RiskDecision(False, "测试：风控拒绝一切", "account")

        kernel.riskgate.evaluate = deny
        exit_market(standin, bearish=True)
        await next_bar(kernel, standin, 5)
        assert position(kernel, "BTCUSDT") is None and position(kernel, "BTCUSDT", SHORT) is None
        assert (await kernel.registry.fetch(1)).exit_reason in (ExitReason.SIGNAL, ExitReason.REVERSE)
        assert len(await intent_keys(kernel, "open:")) == 1
        assert IntentAction.CLOSE not in evaluated
        cancels = await audit(kernel, "m1.reverse_cancel")
        assert cancels and "抑制" in cancels[0].after["reason"]
