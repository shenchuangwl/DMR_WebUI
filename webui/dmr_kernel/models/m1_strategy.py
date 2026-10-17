"""模型 1（完全自动化量化交易，5 层）：启动自检里本模型特有的项、四个按钮的含义，以及按名单自动交易的业务（``StrategyService``）。

``StrategyService``（报告 F1、F10 的消费端、F11，计划 M4 第 3 节）订阅 ``CandlesReady``（短周期收盘后）与 ``PositionClosed``：

1. **开仓（F1）**：名单内每个合约，用最近两根已收盘短周期的 DMR12 与「该根收盘时最新已收盘长周期」的 DMR6 算 ``open_signal``；
   预检不过的跳过并留痕到 ``audit_log``（``m1.skip_open``）；通过则按名单分数从高到低依次提交 OPEN 意图
   （幂等键 ``key_open("m1", 合约, 方向, 信号 K 线开盘时间)``，同一根 K 线内重启不会产生第二个意图）。
   持仓上限、杠杆与保证金模式、风控由协调者的准入段负责，被拒绝的意图本身就是留痕。
2. **信号平仓与反手（F11）**：对出场规则集含「反向信号平仓」的持仓（策略仓、混合仓、``signal_exit`` 的接管仓），每根已收盘短周期
   从「上次已判定的那根」之后逐根判定（进度记在账本 ``meta``，``ledger.repo_m1``）：CLOSE → 平仓意图（原因 ``signal``）；
   REVERSE → 平仓意图（原因 ``reverse``），平仓完成且该键没有未了结的意图后，在信号产生后 ``intent_ttl_s`` 内提交反手开仓
   （``reverse_of_generation``，预检同开仓）；长周期取不到（PENDING）记「待补判」，取到之后补做、只平不反；
   逐根补判命中旧的那根也只平不反。永不反手：开仓闸已关、模型 1 不是生效模型、混合仓、接管仓。平仓不受闸、抑制、风控影响，
   停止后与切走后照常计算。
3. **信号注入**（替身环境，``/dev/inject_signal``）：构造一次「当根已收盘 K 线」的信号，走与自然信号相同的预检与提交
   （幂等键另加注入序号，来源 ``injected``）。
4. ``watchlist()`` 给 ``GET /watchlist``；``monitored_symbols()`` 给 K 线作业；``list_check`` 给协调者的名单复核钩子。

业务判断全在内核；控制台只展示。
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pandas as pd

from dmr_kernel.algo.dmr import dmr
from dmr_kernel.algo.signal import ExitAction, exit_signal, open_signal
from dmr_kernel.core.clock import Clock, floor_to_interval, interval_ms
from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.events import CandlesReady, EventBus, IntentStateChanged, PositionChanged, PositionClosed
from dmr_kernel.core.intents import IntentSpec, key_open, key_signal_close
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    AlertLevel,
    Candle,
    ExitReason,
    ExitRuleset,
    Gate,
    IntentAction,
    IntentSource,
    Model,
    Ownership,
    PositionSide,
    PositionStatus,
)
from dmr_kernel.exchange.rules import InstrumentRules
from dmr_kernel.ledger import repo_audit, repo_m1
from dmr_kernel.ledger.records import PositionRecord
from dmr_kernel.ledger.writer import Ledger
from dmr_kernel.marketdata.candles import CandleService
from dmr_kernel.models.base import TradingModel, checked_config

log = logging.getLogger(__name__)

__all__ = ["ExitHit", "ExitScan", "StrategyModel", "StrategyService", "dmr_values", "long_open_for", "open_side",
           "scan_exit"]

SHORT_DMR = 12
LONG_DMR = 6
_SIDE_CN = {PositionSide.LONG: "多头", PositionSide.SHORT: "空头"}
_CROSS = {"up": (-1.0, 1.0), "down": (1.0, -1.0)}
_TREND = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0, "missing": None}
INJECT_CROSSES = tuple(_CROSS)
INJECT_TRENDS = tuple(_TREND)


class StrategyModel(TradingModel):
    model = Model.M1
    meanings = {
        "start": "打开开仓闸，按信号自动开仓",
        "stop": "关上开仓闸，撤掉未成交的开仓单",
        "partial_close": "任何一个托管持仓",
        "close_all": "默认平策略仓",
    }

    def start_problems(self, ctx: Mapping[str, Any]) -> list[str]:
        """F7：名单有效（有效但为空也算通过）；杠杆与保证金模式已配置。问题按名单、杠杆、保证金模式的顺序排列。"""
        problems: list[str] = []
        if ctx.get("list_valid") is not True:
            problems.append("名单无效或还没有取到：模型 1 需要有效的名单（有效但为空也可以）")
        for key in ("leverage", "margin_mode"):
            _, problem = checked_config(ctx, key)
            if problem is not None:
                problems.append(problem)
        return problems


# ====================================================================== 纯计算：DMR 取值表、开仓信号、出场逐根判定


def dmr_values(candles: Sequence[Candle], n: int) -> dict[int, float | None]:
    """已收盘 K 线（升序、连续）的 DMR(n)：开盘时间 → 值；无效值（前 n−1 根、缺价）是 None。"""
    if not candles:
        return {}
    high = pd.Series([float(c.high) for c in candles], dtype="float64")
    low = pd.Series([float(c.low) for c in candles], dtype="float64")
    series = dmr(high, low, n)
    out: dict[int, float | None] = {}
    for candle, value in zip(candles, series.tolist()):
        out[candle.open_time_ms] = float(value) if value is not None and math.isfinite(value) else None
    return out


def long_open_for(short_open_ms: int, short_ms: int, long_ms: int) -> int:
    """短周期这根（开盘时间）收盘时，最新已收盘的长周期那根的开盘时间。"""
    close_ms = short_open_ms + short_ms
    return close_ms - close_ms % long_ms - long_ms


def open_side(*, latest_ms: int, short_ms: int, long_ms: int, s12: Mapping[int, float | None],
              l6: Mapping[int, float | None]) -> PositionSide | None:
    """F1 第 2 步：最近两根已收盘短周期的 DMR12 过零，配合长周期 DMR6。长周期缺失不出信号。"""
    return open_signal(s12.get(latest_ms - short_ms), s12.get(latest_ms), l6.get(long_open_for(latest_ms, short_ms, long_ms)))


@dataclass(frozen=True)
class ExitHit:
    """出场判定命中：哪根 K 线（开盘时间）、什么动作（CLOSE 或 REVERSE）、是不是迟到的（补判到旧的那根，只平不反）。"""

    bar_open_ms: int
    action: ExitAction
    late: bool


@dataclass(frozen=True)
class ExitScan:
    hit: ExitHit | None
    judged_ms: int  # 新的「上次已判定的那根」
    pending: tuple[int, ...]  # 仍待补判的 K 线开盘时间


def scan_exit(*, side: PositionSide, judged_ms: int, latest_ms: int, short_ms: int, long_ms: int,
              s12: Mapping[int, float | None], l6: Mapping[int, float | None],
              pending: Sequence[int] = ()) -> ExitScan:
    """从 ``judged_ms`` 之后到 ``latest_ms`` 的每一根短周期逐根做出场判定（F11 第 2 步）。

    第一个命中就停（持仓要平了）：命中不是最新一根时降级为只平（REVERSE → CLOSE，``late``）。长周期取不到（PENDING）的那根记入
    待补判；之后长周期取到了，待补判的那根用最新的长周期 DMR6 重判，命中也只平不反。
    """
    new_pending: list[int] = []
    hit: ExitHit | None = None
    judged = judged_ms
    step = short_ms
    bar = judged_ms + step
    while bar <= latest_ms:
        action = exit_signal(s12.get(bar - step), s12.get(bar), l6.get(long_open_for(bar, short_ms, long_ms)), side)
        judged = bar
        if action is ExitAction.PENDING:
            new_pending.append(bar)
        elif action is not None:
            late = bar != latest_ms
            hit = ExitHit(bar, ExitAction.CLOSE if late else action, late)
            break
        bar += step
    if hit is not None:
        return ExitScan(hit, judged, tuple(sorted({*pending, *new_pending})))
    remaining = list(new_pending)
    latest_long = l6.get(long_open_for(latest_ms, short_ms, long_ms))
    for pend in sorted(set(pending)):
        if latest_long is None:
            remaining.append(pend)
            continue
        action = exit_signal(s12.get(pend - step), s12.get(pend), latest_long, side)
        if action in (ExitAction.CLOSE, ExitAction.REVERSE) and hit is None:
            hit = ExitHit(pend, ExitAction.CLOSE, True)  # 补判：只平不反
        elif action is ExitAction.PENDING:
            remaining.append(pend)
        # 其余（不再构成信号）丢掉
    if hit is not None:
        remaining = [x for x in remaining if x != hit.bar_open_ms]
    return ExitScan(hit, judged, tuple(sorted(set(remaining))))


# ====================================================================== 服务


@dataclass
class _Reversal:
    symbol: str
    old_side: PositionSide
    generation: int
    bar_open_ms: int  # 信号 K 线
    signal_ms: int  # 信号产生的时刻（期限从这里起算）
    closed: bool = False  # 平仓单造成的 PositionClosed 已收到
    position_id: int | None = None  # 被平的旧仓（反手前复查它的系统止损已撤、仍是纯策略仓）


@dataclass
class _Tables:
    """一轮里各合约的 DMR 取值表（每个合约只算一次）。"""

    s12: dict[int, float | None] = field(default_factory=dict)
    l6: dict[int, float | None] = field(default_factory=dict)


class StrategyService:
    """按名单自动开仓、信号平仓与反手、信号注入。``listsync_get`` 返回 ``ListSync``（或同接口的替身；还没接入返回 None）。"""

    def __init__(self, *, registry: Registry, modelstate: ModelStateService, coordinator: Coordinator,
                 candles: CandleService, ledger: Ledger, bus: EventBus, clock: Clock,
                 config_get: Callable[[str], Any], notifier: Any, rules: InstrumentRules, limits: Limits | None = None,
                 listsync_get: Callable[[], Any]) -> None:
        self._registry = registry
        self._modelstate = modelstate
        self._coordinator = coordinator
        self._candles = candles
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._cfg = config_get
        self._notifier = notifier
        self._rules = rules
        self._limits = limits
        self._listsync_get = listsync_get
        self._subs: list[Any] = []
        self._running = False
        self._lock = asyncio.Lock()  # 自然信号与注入、出场判定串行
        self._reversal_lock = asyncio.Lock()
        self._reversals: dict[tuple[str, PositionSide], _Reversal] = {}

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._subs = [
            self._bus.subscribe(CandlesReady, self._on_candles, name="m1.candles"),
            self._bus.subscribe(PositionClosed, self._on_closed, name="m1.closed"),
            self._bus.subscribe(IntentStateChanged, self._on_intent, name="m1.intents"),
            self._bus.subscribe(PositionChanged, self._on_changed, name="m1.changed"),  # 旧仓的止损撤完（清掉编号）
        ]

    async def stop(self) -> None:
        self._running = False
        for sub in self._subs:
            sub.cancel()
        self._subs = []

    # ------------------------------------------------------------ 名单

    def _listsync(self) -> Any:
        return self._listsync_get()

    def _view(self) -> Any:
        listsync = self._listsync()
        return None if listsync is None else listsync.current()

    def _view_problem(self, view: Any) -> str | None:
        """名单视图不能用的原因；能用返回 None。过了 ``valid_until_ms`` 的视图即使写着 valid 也不当有效（防御：不依赖名单同步及时重算）。"""
        if view is None:
            return "名单服务还没有接入"
        if not view.valid:
            return f"名单无效：{view.reason}"
        until = getattr(view, "valid_until_ms", None)
        if until is None or self._clock.now_ms() >= until:
            return "名单无效：已过 valid_until_utc"
        return None

    def _expired(self, member: Any) -> bool:
        hours = int(self._cfg("list_member_ttl_h"))
        first = getattr(member, "first_selected_ms", None)
        return hours > 0 and first is not None and self._clock.now_ms() > first + hours * 3_600_000

    def list_symbols(self) -> set[str]:
        """当前有效名单的成员（K 线作业要取的合约）；名单无效或还没接入是空集合。"""
        view = self._view()
        if self._view_problem(view) is not None:
            return set()
        return {m.symbol for m in view.members}

    def list_check(self, symbol: str, side: PositionSide) -> tuple[bool, str]:
        """协调者的名单复核钩子（F1 第 4 步）：名单仍有效且仍含该合约、成员未过有效时长。"""
        view = self._view()
        if view is None:  # 名单同步没有装配（只有测试装配会这样）：钩子不起作用，与 M4 之前一致；自然信号的预检仍要求名单
            return True, ""
        problem = self._view_problem(view)
        if problem is not None:
            return False, problem
        member = self._listsync().member(symbol)
        if member is None:
            return False, f"{symbol} 已不在名单内"
        if self._expired(member):
            return False, f"{symbol} 的名单成员已过有效时长"
        return True, ""

    # ------------------------------------------------------------ 预检（F1 第 3 步）

    def _symbol_block(self, symbol: str, side: PositionSide, member: Any, *, include_cap: bool = False) -> str | None:
        """和合约有关的预检，只读。返回不开的原因（中文），能开返回 None。"""
        if member is None:
            return f"{symbol} 不在名单内"
        if self._expired(member):
            return f"{symbol} 的名单成员已过有效时长"
        if bool(self._cfg("require_direction_match")) and member.direction is not side:
            return f"名单方向（{_SIDE_CN[member.direction]}）与信号方向（{_SIDE_CN[side]}）不一致"
        until = self._registry.cooldown_until(symbol)
        if until is not None:
            return f"{symbol} 在冷却期内（还剩 {max(until - self._clock.now_ms(), 0) // 1000} 秒）"
        instrument = self._rules.get(symbol)
        if instrument is None or not instrument.tradable:
            return f"{symbol} 现在不在交易状态（交易所限制或下架）"
        blocked = self._registry.occupancy(symbol).strategy_open_block(side)
        if blocked is not None:
            return blocked
        if include_cap and self._limits is not None:
            occupancy = self._limits.occupancy(degraded=False, now_ms=self._clock.now_ms())
            if (symbol, side.value) not in occupancy.keys and occupancy.used + occupancy.reserved >= occupancy.cap:
                return f"持仓合约数已达上限（{occupancy.used + occupancy.reserved}/{occupancy.cap}）"
        return None

    def open_block(self, symbol: str, side: PositionSide) -> str | None:
        """F1 第 3 步的全部预检（模型 1 生效、闸开、无抑制、无操作锁、名单有效、成员有效、方向、冷却、限制、占用）。"""
        allowed, why = self._modelstate.current().can_open(Model.M1)
        if not allowed:
            return why
        problem = self._view_problem(self._view())
        if problem is not None:
            return problem
        return self._symbol_block(symbol, side, self._listsync().member(symbol))

    def _open_params(self, member: Any) -> dict[str, Any]:
        view = self._view()
        params: dict[str, Any] = {"notional": format(Decimal(str(self._cfg("trade_notional_usdt"))), "f"),
                                  "list_direction": member.direction.value,
                                  "list_score": format(Decimal(str(member.score)), "f")}
        if view is not None and getattr(view, "snapshot_id", None) is not None:
            params["list_snapshot_id"] = view.snapshot_id
        return params

    def _open_spec(self, symbol: str, side: PositionSide, member: Any, idem_key: str, *, source: IntentSource,
                   reason: str) -> IntentSpec:
        return IntentSpec(action=IntentAction.OPEN, model=Model.M1.value, source=source, symbol=symbol, side=side,
                          idem_key=idem_key, ttl_s=int(self._cfg("intent_ttl_s")), params=self._open_params(member),
                          reason=reason)

    async def _trail(self, action: str, symbol: str, side: PositionSide | None, detail: dict[str, Any],
                     result: str = "skipped") -> None:
        """留痕：写 audit_log。写不进去只记日志，不影响交易。"""
        try:
            await self._ledger.write(repo_audit.append, at_ms=self._clock.now_ms(), operator="m1", action=action,
                                     target=symbol if side is None else f"{symbol}:{side.value}", after=detail,
                                     result=result)
        except Exception:
            log.exception("模型 1 留痕失败：%s %s", action, symbol)

    # ------------------------------------------------------------ 收盘后

    def _frames(self, symbol: str, short_tf: str, long_tf: str) -> _Tables:
        return _Tables(s12=dmr_values(self._candles.closed(symbol, short_tf), SHORT_DMR),
                       l6=dmr_values(self._candles.closed(symbol, long_tf), LONG_DMR))

    def _exit_enabled(self, rec: PositionRecord) -> bool:
        if rec.status is not PositionStatus.MANAGED:
            return False
        if rec.ownership is Ownership.STRATEGY:
            return True
        return rec.ownership is Ownership.TAKEOVER and bool(rec.signal_exit)

    def _exit_positions(self) -> list[PositionRecord]:
        return [rec for rec in self._registry.registered() if self._exit_enabled(rec)]

    async def _on_candles(self, event: CandlesReady) -> None:
        if not self._running or event.timeframe != self._cfg("timeframe_short"):
            return
        try:
            async with self._lock:
                await self._run(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("模型 1 处理 %s 收盘出错", event.timeframe)

    async def _run(self, event: CandlesReady) -> None:
        short_tf, long_tf = str(self._cfg("timeframe_short")), str(self._cfg("timeframe_long"))
        ready = set(event.symbols)
        positions = self._exit_positions()
        members = self._members_by_score() if self._model_one_may_open() else []
        wanted = sorted(({rec.symbol for rec in positions} | {m.symbol for m in members}) & ready)
        if wanted:
            try:
                await self._candles.refresh(wanted, long_tf)  # 长周期整点刚过时，保证拿到最新一根
            except Exception:
                log.exception("长周期 K 线刷新失败")
        self._alert_missing({rec.symbol for rec in positions} | {m.symbol for m in members}, short_tf, long_tf)
        tables: dict[str, _Tables] = {}

        def tables_of(symbol: str) -> _Tables:
            if symbol not in tables:
                tables[symbol] = self._frames(symbol, short_tf, long_tf)
            return tables[symbol]

        exited: set[str] = set()  # 这一轮出了平仓信号的合约：不再按自然信号开仓（对侧仓的后续只能走反手）
        for rec in positions:  # 出场先于开仓：反手的平仓步要先出去
            if rec.symbol in ready:
                try:
                    if await self._exit_one(rec, event.bar_open_ms, tables_of(rec.symbol), short_tf, long_tf):
                        exited.add(rec.symbol)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("模型 1 出场判定出错：%s %s", rec.symbol, rec.side.value)
        short_ms, long_ms = interval_ms(short_tf), interval_ms(long_tf)
        for member in members:
            if member.symbol not in ready:
                continue
            table = tables_of(member.symbol)
            side = open_side(latest_ms=event.bar_open_ms, short_ms=short_ms, long_ms=long_ms, s12=table.s12,
                             l6=table.l6)
            if side is None:
                continue
            skip = None
            if member.symbol in exited or await self._exited_on(member.symbol, event.bar_open_ms):
                skip = "同一根 K 线上刚对该合约出了平仓信号：反手只由信号平仓处理，这里不开"
            elif table.s12 and max(table.s12) > event.bar_open_ms:
                skip = "收盘事件迟到：已有更新的已收盘 K 线，不按旧的那根开仓"
            if skip is not None:
                await self._trail("m1.skip_open", member.symbol, side, {"reason": skip, "bar_open_ms": event.bar_open_ms})
                continue
            try:
                await self._open_from_signal(member, side, event.bar_open_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("模型 1 提交开仓意图出错：%s", member.symbol)

    async def _exited_on(self, symbol: str, bar_open_ms: int) -> bool:
        """该合约在这一根（或更晚的一根）上出过平仓信号（记在账本，重启后仍在）。"""
        last = await self._ledger.read(repo_m1.last_exit_bar, symbol)
        return last is not None and last >= bar_open_ms

    def _model_one_may_open(self) -> bool:
        return self._view_problem(self._view()) is None

    def _members_by_score(self) -> list[Any]:
        view = self._view()
        return sorted(view.members, key=lambda m: m.score, reverse=True) if view is not None else []

    def _alert_missing(self, symbols: set[str], short_tf: str, long_tf: str) -> None:
        limit = int(self._cfg("kline_missing_alert_periods"))
        for symbol in sorted(symbols):
            for timeframe in (short_tf, long_tf):
                missing = self._candles.missing_periods(symbol, timeframe)
                if missing >= limit and self._notifier is not None:
                    self._notifier.raise_alert(
                        AlertLevel.WARNING, "kline_missing",
                        f"{symbol} 的 {timeframe} K 线已连续 {missing} 个周期取不到（告警阈值 {limit} 个周期）",
                        dedupe_key=f"kline_missing:{symbol}:{timeframe}")

    # ------------------------------------------------------------ 开仓

    async def _open_from_signal(self, member: Any, side: PositionSide, bar_open_ms: int) -> None:
        symbol = member.symbol
        reason = self.open_block(symbol, side)
        if reason is not None:
            await self._trail("m1.skip_open", symbol, side, {"reason": reason, "bar_open_ms": bar_open_ms})
            return
        spec = self._open_spec(symbol, side, member, key_open(Model.M1.value, symbol, side, bar_open_ms),
                               source=IntentSource.SIGNAL, reason="signal open")
        await self._coordinator.submit(spec)

    # ------------------------------------------------------------ 出场与反手

    def _reverse_allowed(self, rec: PositionRecord) -> bool:
        state = self._modelstate.current()
        if state.active_model is not Model.M1 or state.gate is not Gate.RUNNING:
            return False
        return (rec.ownership is Ownership.STRATEGY and not rec.is_mixed
                and rec.exit_ruleset in (None, ExitRuleset.STRATEGY))

    async def _exit_one(self, rec: PositionRecord, latest_ms: int, table: _Tables, short_tf: str, long_tf: str) -> bool:
        """逐根判定一个持仓；出了平仓意图返回 True。"""
        symbol, side = rec.symbol, rec.side
        if (symbol, side) in self._reversals:
            return True  # 反手的平仓步已经出去了
        short_ms, long_ms = interval_ms(short_tf), interval_ms(long_tf)
        state = await self._ledger.read(repo_m1.get_exit_state, symbol, side, rec.generation)
        judged = state.judged_ms
        if judged is None:  # 第一次：从开仓时刻所在的那根短周期 K 线开始判
            base = rec.open_time_ms if rec.open_time_ms is not None else self._clock.now_ms()
            if rec.ownership is not Ownership.STRATEGY:  # 接管仓：只看系统登记它之后的过零，接手之前的历史过零不算
                base = max(base, rec.created_ms)
            judged = floor_to_interval(base, short_tf) - short_ms
        scan = scan_exit(side=side, judged_ms=judged, latest_ms=latest_ms, short_ms=short_ms, long_ms=long_ms,
                         s12=table.s12, l6=table.l6, pending=state.pending)
        hit = scan.hit
        if hit is not None:
            stale = bool(table.s12) and max(table.s12) > latest_ms  # 收盘事件迟到：已有更新的已收盘 K 线，只平不反
            reverse = hit.action is ExitAction.REVERSE and not stale and self._reverse_allowed(rec)
            spec = IntentSpec(
                action=IntentAction.CLOSE, model=Model.M1.value if rec.ownership is Ownership.STRATEGY else Model.M2.value,
                source=IntentSource.SIGNAL, symbol=symbol, side=side,
                idem_key=key_signal_close(symbol, side, rec.generation, hit.bar_open_ms),
                params={"retry_until_done": True}, reason="reverse" if reverse else "signal",
                position_id=rec.id, position_generation=rec.generation)
            if reverse:
                self._reversals[(symbol, side)] = _Reversal(symbol, side, rec.generation, hit.bar_open_ms,
                                                            self._clock.now_ms(), position_id=rec.id)
            await self._ledger.write(repo_m1.mark_exit_bar, symbol, latest_ms)  # 先记：这一根不再按自然信号开反向仓
            try:
                await self._coordinator.submit(spec)
            except BaseException:
                self._reversals.pop((symbol, side), None)
                raise
        if hit is not None or scan.judged_ms != state.judged_ms or scan.pending != state.pending:
            await self._ledger.write(repo_m1.save_exit_state, symbol, side, rec.generation,
                                     judged_ms=scan.judged_ms, pending=scan.pending)
        return hit is not None

    async def _on_closed(self, event: PositionClosed) -> None:
        if not self._running:
            return
        try:
            rec = await self._registry.fetch(event.position_id)
            if rec is not None:
                await self._ledger.write(repo_m1.drop_exit_state, rec.symbol, rec.side, rec.generation)
            rv = self._reversals.get((event.symbol, event.side))
            if rv is None or (rec is not None and rec.generation != rv.generation):
                return
            if event.exit_reason != ExitReason.REVERSE.value:
                self._reversals.pop((event.symbol, event.side), None)
                await self._trail("m1.reverse_cancel", event.symbol, event.side,
                                  {"reason": f"持仓由 {event.exit_reason} 平掉，不反手"})
                return
            rv.closed = True
            await self._try_reversals()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("模型 1 处理平仓事件出错")

    async def _on_intent(self, event: IntentStateChanged) -> None:
        if self._running and any(rv.closed for rv in self._reversals.values()):
            try:
                await self._try_reversals()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("模型 1 反手出错")

    async def _on_changed(self, event: PositionChanged) -> None:
        if self._running and any(rv.closed and rv.position_id == event.position_id for rv in self._reversals.values()):
            try:
                await self._try_reversals()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("模型 1 反手出错")

    async def tick(self) -> None:
        """每个保护周期调用：重试等着开仓的反手，丢掉过期的。"""
        if self._running and self._reversals:
            await self._try_reversals()

    async def _try_reversals(self) -> None:
        async with self._reversal_lock:
            for key, rv in list(self._reversals.items()):
                await self._try_reversal(key, rv)

    async def _try_reversal(self, key: tuple[str, PositionSide], rv: _Reversal) -> None:
        deadline = rv.signal_ms + int(self._cfg("intent_ttl_s")) * 1000
        if self._clock.now_ms() > deadline:
            self._reversals.pop(key, None)
            await self._trail("m1.reverse_cancel", rv.symbol, rv.old_side.opposite(),
                              {"reason": "超过信号后的有效期，放弃反手", "bar_open_ms": rv.bar_open_ms})
            return
        if not rv.closed or self._coordinator.key_busy(rv.symbol, rv.old_side):
            return  # 旧仓还没平完，或平仓后的善后（撤旧止损）还在途
        old = await self._registry.fetch(rv.position_id) if rv.position_id is not None else None
        if old is not None:
            if old.is_mixed or old.ownership is not Ownership.STRATEGY or old.exit_ruleset not in (None, ExitRuleset.STRATEGY):
                self._reversals.pop(key, None)
                await self._trail("m1.reverse_cancel", rv.symbol, rv.old_side.opposite(),
                                  {"reason": "旧仓平掉前已变成混合仓或不再是策略仓，永不反手", "bar_open_ms": rv.bar_open_ms})
                return
            if old.stop_client_id or old.stop_algo_id or old.stale_stops or old.handed_over_stop:
                return  # 旧仓的系统止损还没撤掉（F11 第 6 步）：等，过了期限就只平不反
        self._reversals.pop(key, None)
        new_side = rv.old_side.opposite()
        reason = self.open_block(rv.symbol, new_side)
        if reason is not None:
            await self._trail("m1.reverse_cancel", rv.symbol, new_side, {"reason": reason, "bar_open_ms": rv.bar_open_ms})
            return
        member = self._listsync().member(rv.symbol)
        spec = self._open_spec(
            rv.symbol, new_side, member,
            key_open(Model.M1.value, rv.symbol, new_side, rv.bar_open_ms, reverse_of_generation=rv.generation),
            source=IntentSource.SIGNAL, reason="signal reverse open")
        await self._coordinator.submit(spec)

    # ------------------------------------------------------------ 信号注入

    async def inject(self, *, symbol: str, short_cross: str, long_trend: str, wait_s: float = 10.0) -> dict[str, Any]:
        """构造一次「当根已收盘 K 线」的信号并走自然信号的预检与提交。返回
        ``{"signal","bar_open_ms","submitted"[,"intent_id","state","reason"]}``；不改任何 K 线数据。"""
        if short_cross not in _CROSS or long_trend not in _TREND:
            raise ValueError("short_cross 必须是 up / down，long_trend 必须是 bullish / bearish / neutral / missing")
        prev12, curr12 = _CROSS[short_cross]
        side = open_signal(prev12, curr12, _TREND[long_trend])
        short_tf = str(self._cfg("timeframe_short"))
        bar_open_ms = floor_to_interval(self._clock.now_ms(), short_tf) - interval_ms(short_tf)
        result: dict[str, Any] = {"signal": None if side is None else side.value, "bar_open_ms": bar_open_ms,
                                  "submitted": False}
        if side is None:
            result["reason"] = "这组短周期过零与长周期趋势不构成信号"
            await self._trail("m1.inject", symbol, None, dict(result), result="no_signal")
            return result
        async with self._lock:
            reason = self.open_block(symbol, side)
            if reason is not None:
                result["reason"] = reason
                await self._trail("m1.inject", symbol, side, dict(result), result="skipped")
                return result
            member = self._listsync().member(symbol)
            seq = await self._ledger.write(repo_m1.next_inject_seq)
            spec = self._open_spec(symbol, side, member,
                                   key_open(Model.M1.value, symbol, side, bar_open_ms, injected_seq=seq),
                                   source=IntentSource.INJECTED, reason="signal injected open")
            submitted = await self._coordinator.submit(spec)
        result.update(submitted=True, intent_id=submitted.intent_id, injected_seq=seq)
        rec = await self._coordinator.wait_terminal(submitted.intent_id, wait_s)
        result["state"] = rec.state.value
        if rec.state_reason:
            result["reason"] = rec.state_reason
        await self._trail("m1.inject", symbol, side, dict(result), result="submitted")
        return result

    # ------------------------------------------------------------ 只读：名单页

    def watchlist(self) -> dict[str, Any]:
        """``GET /watchlist`` 的内容（报文约定 M4 第 4 节）。``avoid_reason`` 用预检里和合约有关的部分算，只读。"""
        view = self._view()
        if view is None:
            return {"valid": False, "reason": "名单服务还没有接入", "fetched_ms": None, "valid_until_ms": None,
                    "business_date": None, "members": [], "rejected": []}
        members = []
        for member in view.members:
            held = any(self._registry.get_active(member.symbol, side) is not None
                       for side in (PositionSide.LONG, PositionSide.SHORT))  # 含待确认与外部仓
            members.append({"symbol": member.symbol, "direction": member.direction.value,
                            "score": format(Decimal(str(member.score)), "f"),
                            "first_selected_ms": member.first_selected_ms, "held": held,
                            "avoid_reason": self._symbol_block(member.symbol, member.direction, member,
                                                               include_cap=True)})
        problem = self._view_problem(view)
        reason = view.reason or ("" if problem is None else problem)
        return {"valid": problem is None, "reason": reason, "fetched_ms": view.fetched_ms,
                "valid_until_ms": view.valid_until_ms, "business_date": view.business_date, "members": members,
                "rejected": [{"code": code, "reason": why} for code, why in view.rejected]}
