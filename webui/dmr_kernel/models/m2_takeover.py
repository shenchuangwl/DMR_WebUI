"""模型 2（接管手动订单，5 层）：启动自检里本模型特有的项、四个按钮的含义，以及识别与接管的业务（``TakeoverService``）。

``TakeoverService``（报告 F3、4.6，计划 M3 第 2 节）订阅登记簿与对账发布的「未登记持仓」、待确认仓的数量变化、模型状态变化：

1. **启动快照**：模型 2 每次启动（闸打开）先记下启动时刻（``modelstate.set_m2_started``），再读一次全账户持仓，
   没有登记的仓登记为待确认（启动时已有，或旧程序遗留）并写账本、告警；已有的 ``MODEL_NOT_ACTIVE`` 等可重判的待确认仓一并重判。
2. **排除与归类**：该键有未到终态的系统意图或订单（``coordinator.key_busy``）先不判，之后每个保护周期再来；按成交记录找到
   「仓位从零变非零的那一笔」：旧程序格式 → ``LEGACY``，找不到或来源不是外部成交（含 ccxt 默认编号）→ ``UNKNOWN_ORIGIN``；
   模型 2 未生效/未运行/有操作锁 → ``MODEL_NOT_ACTIVE``；那一笔不晚于启动时刻 → ``PREEXISTING``；不在清单 → ``OUT_OF_SCOPE``；
   名义金额（当刻标记价）不在上下限之间 → ``NOTIONAL_OUT_OF_RANGE``。全部通过才自动接管。
3. **接管登记**：均价取交易所持仓均价；开仓时间取那一笔成交的时间（手动接管时取不到则用当刻并标注为估计）；``begin_takeover`` 之后
   触发崩溃点 ``takeover_half_registered``，再交给保护单管理（它订阅 ``PositionRegistered``/``PositionChanged(takeover)`` 自动做 F2
   并在保护挂上后转托管中）。
4. **重判与提醒**：待确认仓数量变化后重判「金额不在范围内」与「模型 2 未在运行」两类；待确认超过 ``pending_remind_min`` 分钟重复告警；
   读不到交易所或有意图在途而没判成的键由 ``tick()`` 重试。

业务判断全在内核；控制台只展示。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.crashpoints import CrashRegistry, CrashSignal
from dmr_kernel.core.events import EventBus, ModelStateChanged, PositionChanged, PositionClosed, UnregisteredPositionFound
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.registry import KeyOccupied, Registry, StaleVersion
from dmr_kernel.core.types import (
    AlertLevel,
    Gate,
    Known,
    Model,
    OrderOrigin,
    OrderSide,
    PendingReason,
    PositionInfo,
    PositionSide,
    PositionStatus,
    Unknown,
)
from dmr_kernel.exchange.adapter import ExchangeAdapter
from dmr_kernel.ledger import repo_takeover
from dmr_kernel.ledger.records import FillRecord, PositionRecord
from dmr_kernel.ledger.writer import Ledger
from dmr_kernel.models.base import TradingModel, checked_config

log = logging.getLogger(__name__)

__all__ = ["PENDING_LABELS", "TakeoverModel", "TakeoverRefused", "TakeoverService", "open_run", "run_qty"]


class TakeoverModel(TradingModel):
    model = Model.M2
    meanings = {
        "start": "开始自动接管清单内的手动仓",
        "stop": "不再接管新仓",
        "partial_close": "任何一个托管持仓",
        "close_all": "默认平接管仓",
    }

    def start_problems(self, ctx: Mapping[str, Any]) -> list[str]:
        """F7：接管清单非空；接管的名义金额上下限已配置（且下限小于上限）。问题按清单、下限、上限的顺序排列。"""
        problems: list[str] = []
        symbols, problem = checked_config(ctx, "takeover_symbols")
        if problem is not None:
            problems.append(problem)
        elif not symbols:
            problems.append("接管清单是空的：模型 2 至少需要一个要接管的合约")

        low, low_problem = checked_config(ctx, "takeover_min_notional")
        high, high_problem = checked_config(ctx, "takeover_max_notional")
        problems.extend(p for p in (low_problem, high_problem) if p is not None)
        if low_problem is None and high_problem is None and not low < high:
            problems.append("自动接管的名义金额必须下限 < 上限")
        return problems


# ====================================================================== 识别与接管（M3）

PENDING_LABELS = {
    PendingReason.OUT_OF_SCOPE: "合约不在接管清单",
    PendingReason.NOTIONAL_OUT_OF_RANGE: "名义金额不在自动接管的上下限之间",
    PendingReason.PREEXISTING: "启动模型 2 时账户里已有的仓",
    PendingReason.LEGACY: "旧程序遗留的仓",
    PendingReason.UNKNOWN_ORIGIN: "来源不明（取不到开仓成交，或不是手动下单）",
    PendingReason.ORIGIN_TO_VERIFY: "来源待核对（其上有带系统前缀的止损）",
    PendingReason.MODEL_NOT_ACTIVE: "模型 2 未生效、未在运行或有操作锁",
}
_SIDE_CN = {PositionSide.LONG: "多头", PositionSide.SHORT: "空头"}
_UNREADABLE = object()  # 交易所持仓读不到（不是「没有仓」）


class TakeoverRefused(Exception):
    """接管被拒绝（中文原因）：``unavailable`` 为真表示只是暂时读不到交易所，可以稍后重试。"""

    def __init__(self, message: str, *, unavailable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.unavailable = unavailable


@dataclass(frozen=True)
class _Verdict:
    reason: PendingReason | None  # None = 全部满足，可以自动接管
    flags: tuple[str, ...] = ()
    note: str = ""
    open_ms: int | None = None  # 仓位从零变非零那一笔成交的时间；取不到是 None


def _opening(side: PositionSide) -> OrderSide:
    return OrderSide.BUY if side is PositionSide.LONG else OrderSide.SELL


def run_qty(run: list[FillRecord], side: PositionSide) -> Decimal:
    """``open_run`` 给出的那一段成交累计下来的净持仓。"""
    opening = _opening(side)
    return sum((f.qty if f.side is opening else -f.qty for f in run), Decimal(0))


def open_run(fills: list[FillRecord], side: PositionSide) -> list[FillRecord] | None:
    """成交（按成交时间升序）里最后一段「仓位从零变非零」之后的成交；一笔开仓成交都没有或现在已是零返回 None。"""
    opening = _opening(side)
    running = Decimal(0)
    start: int | None = None
    for index, fill in enumerate(fills):
        before = running
        running += fill.qty if fill.side is opening else -fill.qty
        if running < 0:
            running = Decimal(0)
        if before <= 0 < running:
            start = index
    if start is None or running <= 0:
        return None
    return fills[start:]


class TakeoverService:
    """识别未登记的外部持仓并归类、自动接管；``take_over`` 供「接管」按钮用。"""

    def __init__(self, *, registry: Registry, modelstate: ModelStateService, coordinator: Coordinator,
                 adapter: ExchangeAdapter, ledger: Ledger, bus: EventBus, clock: Clock,
                 config_get: Callable[[str], Any], notifier: Any, crash: CrashRegistry) -> None:
        self._registry = registry
        self._modelstate = modelstate
        self._coordinator = coordinator
        self._adapter = adapter
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._cfg = config_get
        self._notifier = notifier
        self._crash = crash
        self._subs: list[Any] = []
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._running = False
        self._crashed: CrashSignal | None = None
        self._active_run = False  # 上一次看到的状态：模型 2 生效且运行中
        self._can_auto = False
        self._deferred: dict[tuple[str, PositionSide], str] = {}  # 先不判的键 → 触发来源
        self._snapshot_due = False  # 启动快照没读到交易所，等 tick 重试
        self._run_ready = False  # 本次运行的启动时刻已记下（启动处理做过）；没做过时不拿旧的启动时刻放行自动接管
        self._last_alert_ms: dict[int, int] = {}

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        state = self._modelstate.current()
        self._active_run = state.active_model is Model.M2 and state.gate is Gate.RUNNING
        self._can_auto = state.can_auto_takeover()[0]
        self._run_ready = self._active_run  # 进程重启时模型 2 已在运行：库里的启动时刻就是本次运行的
        self._subs = [
            self._bus.subscribe(UnregisteredPositionFound, self._on_unregistered, name="m2.unregistered"),
            self._bus.subscribe(PositionChanged, self._on_changed, name="m2.changed"),
            self._bus.subscribe(PositionClosed, self._on_closed, name="m2.closed"),
            self._bus.subscribe(ModelStateChanged, self._on_model_state, name="m2.modelstate"),
        ]

    async def stop(self) -> None:
        self._running = False
        for sub in self._subs:
            sub.cancel()
        self._subs = []
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    @property
    def crashed(self) -> CrashSignal | None:
        """崩溃点触发后不再处理任何事件（本实例视为被杀）。"""
        return self._crashed

    async def _guard(self, coro: Any) -> None:
        if self._crashed is not None or not self._running:
            coro.close()
            return
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except CrashSignal as signal:
            self._crashed = signal
            self._running = False
        except Exception:
            log.exception("模型 2 处理出错")

    # ------------------------------------------------------------ 事件

    async def _on_unregistered(self, event: UnregisteredPositionFound) -> None:
        await self._guard(self._process(event.symbol, event.side, event.trigger))

    async def _on_changed(self, event: PositionChanged) -> None:
        if event.cause == "qty_change":
            await self._guard(self._rejudge(event.position_id))

    async def _on_closed(self, event: PositionClosed) -> None:
        self._last_alert_ms.pop(event.position_id, None)
        self._deferred.pop((event.symbol, event.side), None)

    async def _on_model_state(self, event: ModelStateChanged) -> None:
        state = self._modelstate.current()
        running = state.active_model is Model.M2 and state.gate is Gate.RUNNING
        can_auto = state.can_auto_takeover()[0]
        was_running, was_can = self._active_run, self._can_auto
        self._active_run, self._can_auto = running, can_auto
        if not running:
            self._run_ready = False
        if running and not was_running:
            await self._guard(self._on_started())
        elif can_auto and not was_can:
            await self._guard(self._rejudge_all())

    # ------------------------------------------------------------ 启动快照

    async def _on_started(self) -> None:
        async with self._lock:
            await self._modelstate.set_m2_started(self._clock.now_ms())
            self._run_ready = True
            self._snapshot_due = True  # 快照做完才放行自动接管
            await self._snapshot_locked()
        await self._drain_deferred()

    def _hold(self) -> bool:
        """现在能自动接管，但本次运行的启动处理或启动快照还没做完：先不判（判了会拿上一次运行的启动时刻，或把快照还没
        标成「启动时已有」的仓当成新仓）。启动处理做完后统一处理。"""
        return self._modelstate.current().can_auto_takeover()[0] and (not self._run_ready or self._snapshot_due)

    async def _drain_deferred(self) -> None:
        for (symbol, side), trigger in list(self._deferred.items()):
            await self._process(symbol, side, trigger)

    async def _snapshot_locked(self) -> None:
        started = self._modelstate.current().m2_started_ms
        result = await self._adapter.fetch_positions()
        if isinstance(result, Unknown):
            self._snapshot_due = True
            log.warning("模型 2 启动快照没读到持仓（%s），稍后重试", result.reason)
            return
        self._snapshot_due = False
        for info in result.value:
            if info.qty <= 0:
                continue
            key = (info.symbol, info.side)
            existing = self._registry.get_active(*key)
            if existing is not None:
                continue
            if self._coordinator.key_busy(*key):
                self._deferred[key] = "start"
                continue
            fills = await self._ledger.read(repo_takeover.key_fills, info.symbol, info.side)
            run = open_run(fills, info.side)
            open_ms = run[0].time_ms if run else None
            if (open_ms is not None and started is not None and open_ms > started
                    and run_qty(run, info.side) == info.qty):
                continue  # 启动之后才开的（成交记录对得上现在的数量）：走正常的识别（它的事件会在快照之后处理）
            # 对不上（账本里缺这个仓更早的成交，例如程序启动前就开着的仓又被加了仓）：按启动时已有处理
            if any(f.origin is OrderOrigin.LEGACY for f in (run or fills)):
                verdict = _Verdict(PendingReason.LEGACY, ("legacy",), "启动模型 2 时发现：成交记录里有旧程序格式的订单，疑为旧程序遗留", open_ms)
            else:
                verdict = _Verdict(PendingReason.PREEXISTING, ("preexisting",),
                                   f"启动模型 2 时账户里已有（交易所均价 {info.entry_price}），不自动接管，请逐个确认", open_ms)
            await self._register_pending(info, verdict)
        await self._rejudge_all_locked()

    # ------------------------------------------------------------ 读与判

    async def _read(self, symbol: str, side: PositionSide) -> Any:
        """交易所上这个键的持仓：``PositionInfo``、``None``（明确没有）或 ``_UNREADABLE``。"""
        result = await self._adapter.fetch_positions()
        if isinstance(result, Unknown):
            return _UNREADABLE
        for info in result.value:
            if info.symbol == symbol and info.side is side and info.qty > 0:
                return info
        return None

    async def _mark(self, info: PositionInfo) -> Decimal | None:
        if info.mark_price is not None:
            return info.mark_price
        result = await self._adapter.fetch_tickers([info.symbol])
        if isinstance(result, Known) and info.symbol in result.value:
            return result.value[info.symbol].mark
        return None

    def _thresholds(self) -> tuple[Decimal | None, Decimal | None]:
        def read(key: str) -> Decimal | None:
            try:
                value = self._cfg(key)
            except LookupError:
                return None
            return None if value is None else Decimal(str(value))

        return read("takeover_min_notional"), read("takeover_max_notional")

    async def _judge(self, info: PositionInfo) -> _Verdict:
        fills = await self._ledger.read(repo_takeover.key_fills, info.symbol, info.side)
        run = open_run(fills, info.side)
        open_ms = run[0].time_ms if run else None
        if any(f.origin is OrderOrigin.LEGACY for f in (run or fills)):
            return _Verdict(PendingReason.LEGACY, ("legacy",),
                            "成交记录里有旧程序格式的订单，疑为旧程序遗留，不自动接管", open_ms)
        if run is None or run[0].origin is not OrderOrigin.EXTERNAL:
            why = "取不到仓位从零变非零的那笔成交" if run is None else "开仓成交的编号不是手动下单的编号"
            return _Verdict(PendingReason.UNKNOWN_ORIGIN, ("unknown_origin",), f"来源不明：{why}，不自动接管", open_ms)
        state = self._modelstate.current()
        ok, why_not = state.can_auto_takeover()
        if not ok:
            return _Verdict(PendingReason.MODEL_NOT_ACTIVE, ("model_not_active",), f"{why_not}，不自动接管", open_ms)
        started = state.m2_started_ms
        if started is None or open_ms is None or open_ms <= started:
            return _Verdict(PendingReason.PREEXISTING, ("preexisting",),
                            "这个仓是启动模型 2 之前开的，不自动接管，请逐个确认", open_ms)
        if info.symbol not in set(self._cfg("takeover_symbols") or []):
            return _Verdict(PendingReason.OUT_OF_SCOPE, ("out_of_scope",), "合约不在接管清单内", open_ms)
        low, high = self._thresholds()
        mark = await self._mark(info)
        if mark is None or low is None or high is None:
            return _Verdict(PendingReason.NOTIONAL_OUT_OF_RANGE, ("price_unknown",),
                            "取不到标记价或上下限未配置，无法判断名义金额", open_ms)
        notional = info.qty * mark
        if not low <= notional <= high:
            return _Verdict(PendingReason.NOTIONAL_OUT_OF_RANGE, ("notional_out_of_range",),
                            f"名义金额 {notional:f} 不在 {low:f}–{high:f} USDT 之间", open_ms)
        return _Verdict(None, (), "", open_ms)

    # ------------------------------------------------------------ 处理一个未登记的键

    async def _process(self, symbol: str, side: PositionSide, trigger: str) -> None:
        key = (symbol, side)
        async with self._lock:
            if self._registry.get_active(*key) is not None:
                self._deferred.pop(key, None)
                return
            if self._coordinator.key_busy(*key):  # 自己的意图在途：先不判（F3 第 2 步）
                self._deferred[key] = trigger
                return
            if self._hold():  # 启动处理或启动快照没做完
                self._deferred[key] = trigger
                return
            info = await self._read(symbol, side)
            if info is _UNREADABLE:
                self._deferred[key] = trigger
                return
            self._deferred.pop(key, None)
            if info is None:
                return
            verdict = await self._judge(info)
            if verdict.reason is None:
                await self._auto_takeover(info, verdict, None)
            else:
                await self._register_pending(info, verdict)

    async def _register_pending(self, info: PositionInfo, verdict: _Verdict) -> PositionRecord | None:
        try:
            rec = await self._registry.register_pending(
                symbol=info.symbol, side=info.side, qty=info.qty, avg_price=info.entry_price, reason=verdict.reason,
                flags=verdict.flags, note=verdict.note)
        except KeyOccupied:
            return None
        self._alert_pending(rec, verdict.reason, verdict.note, first=True)
        return rec

    async def _auto_takeover(self, info: PositionInfo, verdict: _Verdict, existing: PositionRecord | None) -> PositionRecord | None:
        open_ms = verdict.open_ms if verdict.open_ms is not None else self._clock.now_ms()
        try:
            rec = await self._registry.begin_takeover(
                symbol=info.symbol, side=info.side, qty=info.qty, avg_price=info.entry_price, open_time_ms=open_ms,
                open_time_estimated=verdict.open_ms is None, anchor_ms=open_ms,
                signal_exit=bool(self._cfg("takeover_signal_exit")),
                position_id=None if existing is None else existing.id,
                expected_version=None if existing is None else existing.version)
        except (KeyOccupied, StaleVersion):
            return None
        self._crash.hit("takeover_half_registered")  # 崩溃点 7：登记已提交，保护单还没挂
        self._last_alert_ms.pop(rec.id, None)
        log.info("已接管：编号 %s %s %s 数量 %s 均价 %s", rec.id, rec.symbol, rec.side.value, rec.qty, rec.avg_price)
        return rec

    # ------------------------------------------------------------ 重判

    async def _rejudge(self, position_id: int) -> None:
        async with self._lock:
            await self._rejudge_locked(position_id)

    async def _rejudge_all(self) -> None:
        async with self._lock:
            await self._rejudge_all_locked()

    async def _rejudge_all_locked(self) -> None:
        for rec in self._registry.all_active():
            if rec.status is PositionStatus.PENDING and rec.pending_reason is not None and rec.pending_reason.recheckable:
                await self._rejudge_locked(rec.id)

    async def _rejudge_locked(self, position_id: int) -> None:
        rec = self._registry.by_id(position_id)
        if (rec is None or rec.status is not PositionStatus.PENDING or rec.pending_reason is None
                or not rec.pending_reason.recheckable):
            return
        if self._coordinator.key_busy(rec.symbol, rec.side):
            return
        if self._hold():  # 启动快照做完时会统一重判
            return
        info = await self._read(rec.symbol, rec.side)
        if info is _UNREADABLE or info is None:
            return
        verdict = await self._judge(info)
        if verdict.reason is None:
            await self._auto_takeover(info, verdict, rec)
        elif verdict.reason is not rec.pending_reason:
            updated = await self._registry.set_pending(rec.id, reason=verdict.reason, flags=verdict.flags, note=verdict.note)
            self._alert_pending(updated, verdict.reason, verdict.note, first=True)

    # ------------------------------------------------------------ 手动接管（「接管」按钮）

    async def take_over(self, position_id: int, *, expected_version: int | None) -> PositionRecord:
        """待确认仓 → 接管中（F13：只在模型 2 生效时可用；调用方先查）。读不到交易所抛 ``TakeoverRefused(unavailable=True)``。
        开仓时间取成交记录里从零变非零的那一笔，取不到用当刻并标注为估计；锚点同开仓时间。"""
        async with self._lock:
            rec = self._registry.by_id(position_id)
            if rec is None or rec.status is not PositionStatus.PENDING:
                raise TakeoverRefused("只有待确认仓可以接管")
            if expected_version is not None and rec.version != expected_version:
                raise StaleVersion(f"持仓 {position_id} 的状态版本已变化（{expected_version} → {rec.version}），请刷新后重试")
            info = await self._read(rec.symbol, rec.side)
            if info is _UNREADABLE:
                raise TakeoverRefused("现在读不到交易所持仓，请稍后重试", unavailable=True)
            if info is None:
                raise TakeoverRefused("交易所上已经没有这个仓")
            fills = await self._ledger.read(repo_takeover.key_fills, info.symbol, info.side)
            run = open_run(fills, info.side)
            open_ms = run[0].time_ms if run else None
            verdict = _Verdict(None, (), "", open_ms)
            try:
                taken = await self._auto_takeover(info, verdict, rec)
            finally:
                self._deferred.pop((rec.symbol, rec.side), None)
            if taken is None:
                raise TakeoverRefused("持仓状态已变化，请刷新后重试")
            return taken

    # ------------------------------------------------------------ 告警与定时

    def _alert_pending(self, rec: PositionRecord, reason: PendingReason | None, note: str, *, first: bool) -> None:
        if self._notifier is None:
            return
        self._last_alert_ms[rec.id] = self._clock.now_ms()
        label = PENDING_LABELS.get(reason, "待确认") if reason is not None else "待确认"
        text = (f"{rec.symbol} {_SIDE_CN[rec.side]} 有待确认的仓（数量 {rec.qty}，均价 {rec.avg_price}）：{label}。"
                "请到持仓页选择接管或忽略")
        key = f"pending:{rec.id}:{reason.value if reason else ''}:{'new' if first else self._clock.now_ms()}"
        self._notifier.raise_alert(AlertLevel.WARNING, "position_pending", text, dedupe_key=key)

    async def tick(self) -> None:
        """重试没判成的键与启动快照，待确认仓超过 ``pending_remind_min`` 分钟没处理就重复告警。保护周期的定时作业会调用它。"""
        if self._crashed is not None or not self._running:
            return
        if self._active_run and not self._run_ready:  # 启动处理出过错：重做（记下的启动时刻只会更晚，更保守）
            await self._guard(self._on_started())
        elif self._snapshot_due and self._active_run:
            await self._guard(self._retry_snapshot())
        for (symbol, side), trigger in list(self._deferred.items()):
            await self._guard(self._process(symbol, side, trigger))
        remind_ms = int(self._cfg("pending_remind_min")) * 60_000
        now = self._clock.now_ms()
        for rec in self._registry.all_active():
            if rec.status is not PositionStatus.PENDING:
                continue
            last = self._last_alert_ms.setdefault(rec.id, now)
            if now - last >= remind_ms:
                self._alert_pending(rec, rec.pending_reason, rec.origin_note, first=False)

    async def _retry_snapshot(self) -> None:
        async with self._lock:
            await self._snapshot_locked()
