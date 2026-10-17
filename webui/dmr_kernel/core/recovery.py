"""重启恢复（第 6 层；计划报告 F9 重启恢复 10 步、6.2 的 8 个中断点）。

进程（被杀或正常停止）重新起来之后，登记簿、账本和交易所三方的状态可能不一致：订单发出了没记结果、开仓成交了没登记、止损没挂上、
停机期间止损触发了、用户手动开了仓、批量操作做了一半……本模块按 F9 的顺序把它们对齐，对齐之前一直保持「恢复中」抑制。
目标：进程启动后 30 秒内完成第 2 至 5 步（补保护的重建彼此并发）。

**步骤**（``steps`` 里的键）：

1. ``guard``：置「恢复中」；``guard.startup()``，问题清单非空 → 不进入可交易状态（保持「恢复中」、最高级告警），
   **绝不自动清「互斥异常」**；之后其余步骤照常做（守卫有问题不等于不管持仓）。
2. ``snapshot``：``registry.hold_fills()``（成交照常入账，登记簿暂缓应用）→ 启动账户数据流（配置里关掉就不启动）→ 拉一次全账户
   快照：服务器时间（先读，记为 ``snapshot_ms``）、持仓、普通挂单、条件单。读不到就重试；仍读不到整份快照算未知，之后所有需要快照
   才能下结论的步骤一律不做（未知绝不当成空），也不放开交易。
3. ``orders``：快照里带系统前缀的订单/条件单按编号认领、回写单号；账本里其余未到终态的订单逐条按编号回查（查不清的保持未知）；
   补拉有持仓、挂单、近期意图的合约的成交（没补拉完整的合约上不下结论，并算一个问题：登记簿可能缺了停机期间的成交）；
   ``coordinator.resolve_unknown_orders()`` 接手孤立意图（只回查、绝不重发）。
4. ``fills`` 与 ``reconcile``：全量对账。先把 K 线预备好（登记新仓会立刻触发保护单管理算初始止损），再把暂缓的成交里「快照已经反映、
   但登记簿还没有」的回放进登记簿——系统自己的成交一律回放（中断点 1：开仓成交了没登记），已有记录的键上的外部成交也回放（登记簿
   靠成交推进，不是靠快照）；停机期间止损触发的仓就这样按成交补记为已平（出场原因「止损」）。然后交易所有、登记簿没有的持仓
   一律进入待确认、不自动接管（其上有带系统前缀的止损的标「来源待核对」，保留这张止损）；有系统意图在途、系统订单没查清、
   系统成交还没入账的键先不判。最后清理已平记录遗留的保护单，并做一轮持仓对账（快照之后还有暂缓的成交时不做，登记簿此刻还没跟上）。
5. ``protection``：登记仓（托管中、接管中、平仓中）、快照里数量非零、且同合约同方向没有登记的那张带系统前缀止损类条件单的，
   一律交给 ``protection.rebuild()``（它认领现成的、不会补出第二张、读不到交易所返回 ``unknown``）；各仓的重建并发进行。
6. ``closing``：「平仓中」的仓（部分平仓在途时协调者置的，到期恢复的记录只在协调者内存里，重启就丢了）：订单回查到终态后，
   用户发起的平仓回到托管中并提示结果；信号平仓与保护性平仓的意图重新生效；还没查清的保持平仓中——协调者之后把这些意图走到
   终态时，由恢复在总线上观察到并放回托管中（协调者自己已经不记得这个暂停，不放回的话仓位会永远停在平仓中）。这个订阅是
   ``run`` 返回之后唯一还在的东西，随总线一起结束。
7. ``candles``：回补停机期间的 K 线（短周期与长周期）。含「反向信号平仓」的仓的反向过零补判在 M4 接入（留有钩子，本波不实现信号）。
8. ``orphans``：经对账清理孤儿条件单（F8 的五条）。
9. ``operations``：中断的批量操作与切换不续做：清操作锁；``operation`` 表里非终态的操作记为中断（状态 aborted，结果带
   ``interrupted``），给出逐仓结果；这些操作名下还没发出订单的意图置为取消。
10. ``release``：``registry.release_fills(snapshot_ms)``——早于（含等于）快照时间的成交只入账不改登记簿（快照已经反映了它们），
   之后的按成交时间顺序应用；没有出任何问题才解除「恢复中」，状态仍是已停止（恢复不替人开闸）。

任何一步抛出意外异常都只记在那一步的摘要里（``error``）、算一个问题，不挡后面的步骤；一步之内逐项处理的（平仓中的仓、中断的操作、
待确认仓上保留的止损）一项出错也只记那一项、算一个问题，不挡同一步里的其它项。暂缓的成交一定会释放；被取消（没做完）也会释放，
但不会解除「恢复中」。``run`` 可以重复调用。
``tradable`` 为真 = 整个过程没有任何问题（比规格多：守卫之外，快照读不到、步骤出错、补保护读不到交易所也算问题）。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.core.events import EventBus, FillIngested, IntentStateChanged, Subscription
from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.types import (
    AlertLevel,
    AlgoOrderInfo,
    IntentAction,
    IntentState,
    OperationStatus,
    OrderInfo,
    OrderOrigin,
    OrderState,
    PendingReason,
    PerPositionResult,
    PositionInfo,
    PositionSide,
    PositionStatus,
    ProtectionState,
    Suppression,
    Unknown,
)
from dmr_kernel.exchange import ids
from dmr_kernel.ledger import repo_reconcile, repo_recovery
from dmr_kernel.ledger.records import IntentRecord, OperationRecord, PositionRecord

if TYPE_CHECKING:  # 只用于类型标注：这些协作者都在更低的层，运行时由装配层传进来
    from dmr_kernel.alerts.notifier import Notifier
    from dmr_kernel.core.coordinator import Coordinator
    from dmr_kernel.core.guard import Guard
    from dmr_kernel.core.ingest import Ingest
    from dmr_kernel.core.intents import IntentService
    from dmr_kernel.core.modelstate import ModelStateService
    from dmr_kernel.core.orders import OrderService
    from dmr_kernel.core.protection import Protection
    from dmr_kernel.core.reconcile import Reconciler
    from dmr_kernel.core.registry import Registry
    from dmr_kernel.exchange.adapter import AccountStream, ExchangeAdapter
    from dmr_kernel.ledger.writer import Ledger
    from dmr_kernel.marketdata.candles import CandleService
    from dmr_kernel.settings import Settings

__all__ = ["Recovery", "RecoveryResult"]

log = logging.getLogger(__name__)

Key = tuple[str, PositionSide]
ZERO = Decimal("0")
STEP_LABELS = {
    "guard": "守卫自检", "snapshot": "全账户快照", "orders": "未结订单与意图回查", "fills": "成交回放",
    "reconcile": "全量对账", "protection": "补保护", "closing": "平仓中的仓", "candles": "K 线回补",
    "orphans": "孤儿条件单清理", "operations": "中断的操作", "release": "释放暂缓的成交",
}
_UNRESOLVED_ORDERS = frozenset({OrderState.PENDING_SEND, OrderState.SENT_UNKNOWN, OrderState.CANCEL_PENDING})
_PRE_SEND = frozenset({IntentState.CREATED, IntentState.VALIDATED, IntentState.RESERVED, IntentState.DISPATCH_READY})
_TERMINAL_STATES = frozenset(state.value for state in IntentState.terminal())  # IntentStateChanged.state 是字符串
_CLOSE_ACTIONS = (IntentAction.CLOSE, IntentAction.PARTIAL_CLOSE)
_CLOSE_LABEL = {IntentAction.CLOSE: "平仓", IntentAction.PARTIAL_CLOSE: "部分平仓"}
INTERRUPTED_REASON = "进程重启，操作中断，不自动续做"


@dataclass(frozen=True)
class RecoveryResult:
    tradable: bool  # 整个恢复过程没有任何问题（守卫自检通过，快照读到了，没有步骤出错）
    problems: tuple[str, ...]
    duration_ms: int
    steps: Mapping[str, Any]  # 每步摘要：补挂几张止损、补记几笔已平、进入待确认的仓、清理的孤儿、中断的操作……


@dataclass(frozen=True)
class _Snapshot:
    """一份全账户快照。``ms`` 是先读到的服务器时间。"""

    ms: int
    positions: Mapping[Key, PositionInfo]
    open_orders: tuple[OrderInfo, ...]
    algos: tuple[AlgoOrderInfo, ...]


@dataclass
class _Run:
    started_ms: int
    steps: dict[str, dict[str, Any]]
    problems: list[str] = field(default_factory=list)
    snapshot: _Snapshot | None = None
    undecided: set[str] = field(default_factory=set)  # 成交没补拉成的合约：这些合约上不下「未登记」之类的结论
    replayed: set[int] = field(default_factory=set)  # 已经回放进登记簿的成交行
    pending_keys: set[Key] = field(default_factory=set)  # 本次恢复按快照登记的待确认仓所在的键：快照已含早于它的全部成交


class Recovery:
    # 可调参数（类属性：测试里改成小值，不真的等）
    SNAPSHOT_ATTEMPTS = 3  # 快照读不到最多试几次
    SNAPSHOT_RETRY_S = 1.0
    REBUILD_ATTEMPTS = 3  # 补保护读不到交易所（unknown）最多试几次
    REBUILD_RETRY_S = 1.0
    DRAIN_TIMEOUT_S = 5.0  # 等事件总线把事件交给订阅者的上限（真实秒）
    CANDLE_TIMEOUT_S = 10.0  # 补保护之前预备 K 线的上限（真实秒）：到点没好就按已有的算，不耽误补保护
    ORPHAN_WAIT_S = 5.0  # 孤儿清理提交的撤单意图最多等多久（真实秒）
    RECENT_INTENT_MS = 24 * 3_600_000  # 成交补拉：近期意图涉及的合约往前看多久
    FILLS_LOOKBACK_MS = 7 * 24 * 3_600_000  # 判断未登记持仓的来源：往前看多久的成交
    FILLS_PENDING_LOOKBACK_MS = 3_600_000  # 系统成交是否还没入账：往前看多久的订单

    def __init__(self, *, settings: "Settings", adapter: "ExchangeAdapter", stream: "AccountStream | None",
                 ledger: "Ledger", bus: EventBus, clock: Clock, config_get: Callable[[str], Any],
                 modelstate: "ModelStateService", registry: "Registry", intents: "IntentService",
                 orders: "OrderService", ingest: "Ingest", coordinator: "Coordinator", reconciler: "Reconciler",
                 protection: "Protection", guard: "Guard", candles: "CandleService", operations: Any,
                 notifier: "Notifier | None") -> None:
        self._settings = settings
        self._adapter = adapter
        self._stream = stream
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._config_get = config_get
        self._modelstate = modelstate
        self._registry = registry
        self._intents = intents
        self._orders = orders
        self._ingest = ingest
        self._coordinator = coordinator
        self._reconciler = reconciler
        self._protection = protection
        self._guard = guard
        self._candles = candles
        self._operations = operations  # ops.operations.Operations（同层不同组，不导入，只用 finish）
        self._notifier = notifier
        # 第 6 步里保持在平仓中的仓 -> 重启时还没到终态的平仓类意图；总线上的订阅是 run 返回之后唯一还在的东西
        self._closing_watch: dict[int, set[int]] = {}
        self._closing_sub: Subscription | None = None

    # ================================================================ 入口

    async def run(self) -> RecoveryResult:
        run = _Run(started_ms=self._clock.now_ms(), steps={name: {} for name in STEP_LABELS})
        await self._modelstate.add_suppression(Suppression.RECOVERING, reason="重启恢复中")
        crashed = completed = False
        try:
            await self._guarded(run, "guard", self._step_guard)
            await self._guarded(run, "snapshot", self._step_snapshot)
            await self._guarded(run, "orders", self._step_orders)
            await self._guarded(run, "fills", self._step_fills)
            await self._guarded(run, "reconcile", self._step_reconcile)
            await self._guarded(run, "protection", self._step_protection)
            await self._guarded(run, "closing", self._step_closing)
            await self._guarded(run, "candles", self._step_candles)
            await self._guarded(run, "orphans", self._step_orphans)
            await self._guarded(run, "operations", self._step_operations)
            completed = True
        except CrashSignal:
            crashed = True  # 模拟进程被杀：什么收尾都不做
            raise
        finally:
            if not crashed:
                if not completed:  # 被取消（或别的中断）：暂缓的成交要放开，但没做完就不能解除「恢复中」
                    run.problems.append("重启恢复被中断，没有做完")
                await self._finish(run)
        return self._result(run)

    async def _guarded(self, run: _Run, name: str, step: Callable[[_Run, dict[str, Any]], Awaitable[None]]) -> None:
        """跑一步。意外异常只记在这一步的摘要里、算一个问题，不挡后面的步骤（取消与崩溃点照常往外传）。"""
        summary = run.steps[name]
        try:
            await step(run, summary)
        except (CrashSignal, asyncio.CancelledError):
            raise
        except Exception as exc:
            log.exception("重启恢复的「%s」步骤出错", STEP_LABELS[name])
            summary["error"] = f"{type(exc).__name__}: {exc}"
            run.problems.append(f"「{STEP_LABELS[name]}」步骤出错：{type(exc).__name__}: {exc}")
            self._alert(AlertLevel.CRITICAL, "recovery_step_failed",
                        f"重启恢复的「{STEP_LABELS[name]}」步骤出错（{type(exc).__name__}: {exc}），已跳过继续恢复，"
                        "不放开交易，请检查日志", dedupe_key=f"recovery_step_failed:{name}")

    # ================================================================ 第 1 步：置「恢复中」、守卫自检

    async def _step_guard(self, run: _Run, summary: dict[str, Any]) -> None:
        problems = [str(p) for p in await self._guard.startup()]
        summary["problems"] = problems
        start = getattr(self._guard, "start", None)  # 守卫在数据流之前开始订阅，才收得到数据流的中断（可重复调用）
        if callable(start):
            await start()
        if problems:
            run.problems.extend(problems)
            self._alert(AlertLevel.CRITICAL, "recovery_blocked",
                        "启动自检没有通过，不进入可交易状态（保持「恢复中」）：" + "；".join(problems),
                        dedupe_key="recovery_blocked")

    # ================================================================ 第 2 步：暂缓成交、数据流、快照

    async def _step_snapshot(self, run: _Run, summary: dict[str, Any]) -> None:
        self._registry.hold_fills()
        started = False
        if self._stream is not None and bool(self._cfg("ex_stream_enabled", True)):
            try:
                await self._stream.start()  # 起不来不抛：状态变成 interrupted，之后由判活重连
                started = True
            except Exception as exc:
                log.exception("启动账户数据流失败")
                summary["stream_error"] = f"{type(exc).__name__}: {exc}"
        summary["stream_started"] = started
        snapshot, attempts, why = await self._read_snapshot()
        summary["attempts"] = attempts
        summary["ok"] = snapshot is not None
        if snapshot is None:
            summary.update(snapshot_ms=None, positions=None, open_orders=None, algo_orders=None, reason=why)
            run.problems.append(f"无法读取交易所快照（{why}）：未知不当成空，持仓对账、补保护、待确认登记都不做")
            self._alert(AlertLevel.CRITICAL, "recovery_snapshot_unknown",
                        f"重启恢复读不到交易所快照（{why}），无法核对持仓与保护单；不放开交易，请检查交易所连接后重新恢复",
                        dedupe_key="recovery_snapshot_unknown")
            return
        run.snapshot = snapshot
        summary.update(snapshot_ms=snapshot.ms, positions=len(snapshot.positions),
                       open_orders=len(snapshot.open_orders), algo_orders=len(snapshot.algos))

    async def _read_snapshot(self) -> tuple[_Snapshot | None, int, str]:
        why = ""
        for attempt in range(1, self.SNAPSHOT_ATTEMPTS + 1):
            snapshot, why = await self._try_snapshot()
            if snapshot is not None:
                return snapshot, attempt, ""
            log.warning("重启恢复读快照失败（第 %s/%s 次）：%s", attempt, self.SNAPSHOT_ATTEMPTS, why)
            if attempt < self.SNAPSHOT_ATTEMPTS:
                await asyncio.sleep(self.SNAPSHOT_RETRY_S)
        return None, self.SNAPSHOT_ATTEMPTS, why

    async def _try_snapshot(self) -> tuple[_Snapshot | None, str]:
        """服务器时间先读（快照的「截止时间」，与持仓对账一致），再同时读持仓、普通挂单、条件单。缺任何一项都是未知。"""
        server = await self._safe(self._adapter.server_time, "服务器时间")
        if isinstance(server, Unknown):
            return None, server.reason
        positions, open_orders, algos = await asyncio.gather(
            self._safe(self._adapter.fetch_positions, "持仓"), self._safe(self._adapter.fetch_open_orders, "挂单"),
            self._safe(self._adapter.fetch_open_algo_orders, "条件单"))
        failed = [r.reason for r in (positions, open_orders, algos) if isinstance(r, Unknown)]
        if failed:
            return None, "；".join(failed)
        held = {(p.symbol, p.side): p for p in positions.value if p.qty > ZERO}
        return _Snapshot(ms=int(server.value), positions=held, open_orders=tuple(open_orders.value),
                         algos=tuple(algos.value)), ""

    @staticmethod
    async def _safe(call: Callable[[], Awaitable[Any]], what: str) -> Any:
        """适配层的读方法失败返回 Unknown；万一抛出意外的异常也一样当作未知（取消与崩溃点照常往外传）。"""
        try:
            return await call()
        except (CrashSignal, asyncio.CancelledError):
            raise
        except Exception as exc:
            log.error("重启恢复读取%s出错：%r", what, exc, exc_info=True)
            return Unknown(f"读取{what}出错：{exc!r}")

    # ================================================================ 第 3 步：未结订单与意图

    async def _step_orders(self, run: _Run, summary: dict[str, Any]) -> None:
        summary.update(claimed=0, requeried=0, unresolved=[], backfill_symbols=[], backfilled_trades=0,
                       backfill_failed=[])
        snap = run.snapshot
        algo_views = {a.client_algo_id: a for a in snap.algos if _is_system(a.client_algo_id)} if snap else {}
        order_views = {o.client_order_id: o for o in snap.open_orders if _is_system(o.client_order_id)} if snap else {}
        for order in await self._orders.non_terminal():
            cid = order.client_order_id
            view = (algo_views if order.kind == "algo" else order_views).get(cid)
            try:
                if view is not None:  # 快照里带系统前缀的单按编号认领，回写单号
                    await self._orders.apply_exchange_view(cid, view, source="reconcile")
                    summary["claimed"] += 1
                else:
                    await self._orders.requery(cid)
                    summary["requeried"] += 1
            except Exception:
                log.exception("重启恢复回查订单失败：%s", cid)
            after = await self._orders.get(cid)
            if after is not None and after.state in _UNRESOLVED_ORDERS:
                summary["unresolved"].append(cid)  # 查不清的保持未知，协调者的回查会继续
        await self._backfill(run, summary)
        await self._coordinator.resolve_unknown_orders()  # 接手孤立意图：只回查、绝不重发
        await self._drain(summary)

    async def _backfill(self, run: _Run, summary: dict[str, Any]) -> None:
        symbols = await self._trade_symbols(run)
        summary["backfill_symbols"] = symbols
        if not symbols:
            return
        try:
            result = await self._ingest.backfill_trades(symbols)
        except Exception as exc:
            log.exception("重启恢复补拉成交失败")
            summary["backfill_error"] = f"{type(exc).__name__}: {exc}"
            run.undecided.update(symbols)
            self._backfill_problem(run, f"补拉成交出错（{type(exc).__name__}: {exc}）", symbols)
            return
        summary["backfilled_trades"] = int(result)
        failed = sorted(str(s) for s in getattr(result, "failed", ()))  # 读成交返回未知的合约：不能当成「没有成交」
        summary["backfill_failed"] = failed
        run.undecided.update(failed)
        if failed:
            self._backfill_problem(run, "这些合约的成交没有补拉完整", failed)
        await self._drain(summary)

    def _backfill_problem(self, run: _Run, why: str, symbols: list[str]) -> None:
        """成交没补拉完整 = 登记簿可能缺了停机期间的成交（开仓没登记、止损触发没补记）：未知不当成「没有成交」，算问题、不放开交易。"""
        names = "、".join(symbols)
        run.problems.append(f"{why}：{names}；登记簿可能缺了停机期间的成交")
        self._alert(AlertLevel.CRITICAL, "recovery_backfill_failed",
                    f"重启恢复{why}（{names}），登记簿可能缺了停机期间的成交；这些合约上的持仓先不判定，不放开交易",
                    dedupe_key="recovery_backfill_failed")

    async def _trade_symbols(self, run: _Run) -> list[str]:
        symbols = set(self._monitor_symbols(run))
        symbols.update(rec.symbol for rec in await self._intents.non_terminal())
        symbols.update(order.symbol for order in await self._orders.non_terminal())
        since = self._clock.now_ms() - self.RECENT_INTENT_MS
        symbols.update(await self._ledger.read(repo_recovery.recent_intent_symbols, since_ms=since))
        return sorted(symbols)

    def _monitor_symbols(self, run: _Run) -> list[str]:
        """监控集合（本轮 M1）：登记簿里全部活动持仓所在的合约 + 快照里有持仓、挂单、条件单的合约。"""
        symbols = {rec.symbol for rec in self._registry.all_active()}
        if run.snapshot is not None:
            symbols.update(symbol for symbol, _ in run.snapshot.positions)
            symbols.update(o.symbol for o in run.snapshot.open_orders)
            symbols.update(a.symbol for a in run.snapshot.algos)
        return sorted(symbols)

    # ================================================================ 第 4 步：成交回放（登记簿跟上成交）

    async def _step_fills(self, run: _Run, summary: dict[str, Any]) -> None:
        summary.update(held=len(self._registry.held_fills()), replayed=0, replay_failed=0, closed_by_fills=[],
                       registered_from_fills=[], candles_prepared=[])
        snap = run.snapshot
        if snap is None:
            return  # 没有快照：不回放，释放时全部应用
        await self._prepare_candles(run, summary)
        before = {rec.id: rec for rec in self._registry.all_active()}
        replayed, failed = await self._replay(run, snap)
        summary.update(replayed=replayed, replay_failed=failed)
        await self._drain(summary)  # 让保护单管理等订阅者看到登记簿的变化
        after = {rec.id: rec for rec in self._registry.all_active()}
        for pid, rec in before.items():
            if pid not in after:
                closed = await self._registry.fetch(pid)
                summary["closed_by_fills"].append({
                    "position_id": pid, "symbol": rec.symbol, "side": rec.side.value,
                    "exit_reason": None if closed is None or closed.exit_reason is None else closed.exit_reason.value})
        for pid, rec in after.items():
            if pid not in before:
                summary["registered_from_fills"].append({"position_id": pid, "symbol": rec.symbol,
                                                         "side": rec.side.value, "status": rec.status.value})

    async def _replay(self, run: _Run, snap: _Snapshot) -> tuple[int, int]:
        """把暂缓的成交里早于（含等于）快照时间、该回放的、还没回放过的，按成交时间顺序应用到登记簿。返回（成功数，失败数）。"""
        done = failed = 0
        held = sorted((e for e in self._registry.held_fills()
                       if e.trade.time_ms <= snap.ms and e.fill_id not in run.replayed), key=lambda e: e.trade.time_ms)
        for event in held:
            if not self._should_replay(run, event):
                continue
            try:
                await self._registry.apply_fill(event)
                run.replayed.add(event.fill_id)
                done += 1
            except Exception:
                failed += 1
                log.exception("重启恢复回放成交失败：成交行 %s", event.fill_id)
        return done, failed

    def _should_replay(self, run: _Run, event: FillIngested) -> bool:
        """系统自己的成交一律回放（登记簿丢了的开仓、平仓、止损）；其它来源只回放到已有记录的键上（外部调整、强平、自动减仓）。
        没有记录的键上的外部成交不回放：那个仓由快照登记为待确认，快照已经包含这笔成交；本次恢复按快照登记的待确认仓同理
        （数量取自快照，晚到的、早于快照时间的成交再应用一次就重复计数了）。"""
        key = (event.trade.symbol, event.trade.position_side)
        if key in run.pending_keys:
            return False
        if event.origin is OrderOrigin.SYSTEM:
            return True
        return self._registry.get_active(*key) is not None

    # ================================================================ 第 4 步：全量对账

    async def _step_reconcile(self, run: _Run, summary: dict[str, Any]) -> None:
        summary.update(pending_registered=[], origin_to_verify=[], deferred=[], flat_unexplained=[],
                       closed_cleanup=[], positions_reconcile="skipped")
        snap = run.snapshot
        if snap is not None:
            await self._register_unregistered(run, snap, summary)
            self._note_flat_records(snap, summary)
        await self._cleanup_closed(summary)
        if snap is None:
            summary["positions_reconcile"] = "skipped：没有快照"
            return
        if any(e.trade.time_ms > snap.ms for e in self._registry.held_fills()):
            # 快照之后还有暂缓的成交没应用，登记簿此刻比交易所落后：现在对账会把它们误判成无法解释的差异
            summary["positions_reconcile"] = "skipped：快照之后还有暂缓的成交，释放后由定时对账接着做"
            return
        summary["positions_reconcile"] = await self._reconciler.reconcile_positions()

    async def _register_unregistered(self, run: _Run, snap: _Snapshot, summary: dict[str, Any]) -> None:
        for key, info in sorted(snap.positions.items(), key=lambda kv: (kv[0][0], kv[0][1].value)):
            symbol, side = key
            if self._registry.get_active(symbol, side) is not None:
                continue
            why = await self._undecided_reason(run, key)
            if why is not None:  # 查清之前不判定（F3 第 2 步）
                summary["deferred"].append({"symbol": symbol, "side": side.value, "why": why})
                continue
            reason, flags, note, stop = await self._classify(snap, key, info)
            try:
                rec = await self._registry.register_pending(
                    symbol=symbol, side=side, qty=info.qty, avg_price=info.entry_price, reason=reason, flags=flags,
                    note=note)
            except Exception as exc:  # 一仓一主冲突之类：别的路径刚把它登记了
                log.warning("重启恢复登记待确认仓失败：%s %s：%s", symbol, side.value, exc)
                summary["deferred"].append({"symbol": symbol, "side": side.value, "why": f"登记待确认失败：{exc}"})
                continue
            run.pending_keys.add(key)
            entry = {"position_id": rec.id, "symbol": symbol, "side": side.value, "reason": reason.value}
            if stop is not None:  # 保留这张止损，记在待确认仓上（对账不会当孤儿；以后恢复托管时保护单管理认领）
                try:
                    await self._registry.set_protection(
                        rec.id, state=ProtectionState.NONE, stop_price=stop.trigger_price,
                        stop_client_id=stop.client_algo_id, stop_algo_id=stop.algo_id,
                        note="来源待核对：该仓上有带系统前缀的止损，保留未动")
                    summary["origin_to_verify"].append(rec.id)
                    entry["stop_client_id"] = stop.client_algo_id
                except Exception as exc:  # 待确认仓已经登记了：这张止损没记下来也不挡别的键
                    log.exception("重启恢复把待确认仓 %s 上的止损记下来失败", rec.id)
                    summary.setdefault("stop_note_failed", []).append({
                        "position_id": rec.id, "stop_client_id": stop.client_algo_id,
                        "error": f"{type(exc).__name__}: {exc}"})
                    run.problems.append(f"{symbol} {side.value} 待确认仓上的止损 {stop.client_algo_id} 没能记下来"
                                        f"（{type(exc).__name__}: {exc}）")
            summary["pending_registered"].append(entry)
            self._alert(AlertLevel.WARNING, "recovery_unregistered_position",
                        f"重启恢复发现 {symbol} {side.value} 方向有登记簿里没有的持仓（数量 {info.qty}），"
                        f"已登记为待确认（{note}），不会自动接管，请到持仓页处理", dedupe_key=f"recovery_pending:{rec.id}")

    async def _undecided_reason(self, run: _Run, key: Key) -> str | None:
        symbol, side = key
        if symbol in run.undecided:
            return "这个合约的成交没有补拉成功，不能确定它是不是系统自己开的仓"
        if self._coordinator.key_busy(symbol, side):
            return "有系统意图在途"
        for order in await self._orders.non_terminal(symbol=symbol, side=side):
            if order.kind == "market" or order.state in _UNRESOLVED_ORDERS:
                return f"有未到终态的系统订单（{order.client_order_id}）"
        since = self._clock.now_ms() - self.FILLS_PENDING_LOOKBACK_MS
        try:
            pending = await self._ledger.read(repo_reconcile.system_fills_pending, symbol, side, since_ms=since)
        except Exception:
            log.exception("查询系统成交是否已入账失败：%s %s", symbol, side.value)
            return "查不清系统成交是否已入账"
        if pending:
            return "有系统订单已成交而成交明细还没入账"
        return None

    async def _classify(self, snap: _Snapshot, key: Key, info: PositionInfo
                        ) -> tuple[PendingReason, list[str], str, AlgoOrderInfo | None]:
        """F3 归类：一律待确认。其上有带系统前缀的止损 → 来源待核对；账本里有旧程序格式的成交 → 旧程序遗留；其余 → 启动时已有。"""
        symbol, side = key
        flags = ["recovery"]
        stops = system_stops(snap.algos, symbol, side)
        if stops:
            stop = stops[0]
            return (PendingReason.ORIGIN_TO_VERIFY, flags,
                    f"重启恢复时发现：该仓不在登记簿里，但其上有带系统前缀的止损（{stop.client_algo_id}），来源待核对", stop)
        since = self._clock.now_ms() - self.FILLS_LOOKBACK_MS
        fills = await self._ledger.read(repo_recovery.fills_of_key, symbol, side, since_ms=since)
        if any(f.origin is OrderOrigin.LEGACY for f in fills):
            return PendingReason.LEGACY, flags, "重启恢复时发现：该仓不在登记簿里，账本里有旧程序格式的成交，疑为旧程序遗留", None
        return (PendingReason.PREEXISTING, flags,
                f"重启恢复时发现：该仓不在登记簿里（交易所均价 {info.entry_price}），不自动接管，请逐个确认", None)

    def _note_flat_records(self, snap: _Snapshot, summary: dict[str, Any]) -> None:
        """登记簿里还有、交易所明确为零、回放成交也没能解释的：不在这里下结论（一次零读数不算已平），交给持仓对账的连续 3 次零判定。"""
        for rec in self._registry.all_active():
            if (rec.symbol, rec.side) not in snap.positions:
                summary["flat_unexplained"].append({"position_id": rec.id, "symbol": rec.symbol,
                                                    "side": rec.side.value, "status": rec.status.value})

    async def _cleanup_closed(self, summary: dict[str, Any]) -> None:
        """已平记录里还留着止损编号、待清理旧止损、已移交止损的（平仓后清理之前进程被杀，中断点 5）：补做归零后清理。"""
        for rec in await self._ledger.read(repo_recovery.closed_with_leftovers):
            try:
                await self._protection.cleanup_after_close(rec.id)
                summary["closed_cleanup"].append(rec.id)
            except Exception:
                log.exception("重启恢复清理已平仓位 %s 的保护单失败", rec.id)

    # ================================================================ 第 5 步：补保护

    async def _step_protection(self, run: _Run, summary: dict[str, Any]) -> None:
        summary.update(rebuilt=[], unknown=[], errors=[])
        snap = run.snapshot
        if snap is None:
            summary["skipped"] = "没有快照"
            return
        targets = [rec for rec in self._registry.registered() if self._needs_rebuild(rec, snap)]
        results = await asyncio.gather(*(self._rebuild_one(rec) for rec in targets))
        for rec, result in zip(targets, results):
            entry = {"position_id": rec.id, "symbol": rec.symbol, "side": rec.side.value, "result": result}
            if result == "unknown":
                summary["unknown"].append(entry)
                run.problems.append(f"{rec.symbol} {rec.side.value} 补保护时读不到交易所，保护是否在位未知")
            elif result.startswith("error:"):
                summary["errors"].append(entry)
                run.problems.append(f"{rec.symbol} {rec.side.value} 补保护出错（{result[6:].strip()}）")
            else:
                summary["rebuilt"].append(entry)

    @staticmethod
    def _needs_rebuild(rec: PositionRecord, snap: _Snapshot) -> bool:
        """登记仓、快照里数量非零、且同合约同方向没有登记的那张带系统前缀的止损类条件单。"""
        seen = snap.positions.get((rec.symbol, rec.side))
        if seen is None or seen.qty <= ZERO:
            return False
        stops = system_stops(snap.algos, rec.symbol, rec.side)
        if not stops or rec.protection_state is not ProtectionState.SYSTEM:
            return True  # 没有系统止损，或有但登记簿没把它记成当前保护（认领它也是 rebuild 的事，它不会补出第二张）
        recorded = {x for x in (rec.stop_client_id, rec.stop_algo_id) if x}
        return not any(a.client_algo_id in recorded or a.algo_id in recorded for a in stops)

    async def _rebuild_one(self, rec: PositionRecord) -> str:
        result = "unknown"
        for attempt in range(1, self.REBUILD_ATTEMPTS + 1):
            try:
                result = await self._protection.rebuild(rec.id, reason="recovery")
            except Exception as exc:  # 一个仓的保护单管理出错不挡别的仓
                log.exception("重启恢复补保护出错：持仓 %s", rec.id)
                return f"error: {type(exc).__name__}: {exc}"
            if result != "unknown":
                return result
            if attempt < self.REBUILD_ATTEMPTS:
                await asyncio.sleep(self.REBUILD_RETRY_S)
        return result

    # ================================================================ 第 6 步：平仓中的仓

    async def _step_closing(self, run: _Run, summary: dict[str, Any]) -> None:
        summary.update(resumed=[], kept=[], resubmitted=[])
        self._closing_watch.clear()  # 每一轮重新算：重复运行时上一轮观察的仓可能已经变了
        closing = [rec for rec in self._registry.all_active() if rec.status is PositionStatus.CLOSING]
        if not closing:
            return
        active = await self._intents.non_terminal()
        for rec in closing:
            try:
                await self._settle_closing(run, rec, active, summary)
            except Exception as exc:  # 一个仓出错不挡别的仓
                log.exception("重启恢复处理平仓中的仓 %s 出错", rec.id)
                summary.setdefault("errors", []).append(
                    {"position_id": rec.id, "error": f"{type(exc).__name__}: {exc}"})
                run.problems.append(f"{rec.symbol} {rec.side.value} 平仓中的仓处理出错（{type(exc).__name__}: {exc}）")

    async def _settle_closing(self, run: _Run, rec: PositionRecord, active: list[IntentRecord],
                              summary: dict[str, Any]) -> None:
        related = [i for i in active if (i.symbol, i.side) == (rec.symbol, rec.side) and i.action in _CLOSE_ACTIONS]
        for intent in related:
            # 信号平仓与保护性平仓的意图重新生效；批量操作名下的不续做（第 9 步把它们记为中断）
            if intent.params.get("retry_until_done") and intent.state in _PRE_SEND and intent.op_id is None:
                await self._resubmit(run, intent, summary)
        if related:
            summary["kept"].append(rec.id)
            self._alert(AlertLevel.WARNING, "recovery_closing_unresolved",
                        f"{rec.symbol} {rec.side.value} 的平仓/部分平仓还没有结论（意图 "
                        f"{'、'.join(str(i.id) for i in related)}），保持「平仓中」，协调者会继续回查，查清后自动放回托管中",
                        dedupe_key=f"recovery_closing:{rec.id}")
            await self._watch_closing(rec.id, {i.id for i in related})
            return
        last = await self._ledger.read(repo_recovery.last_close_intent, rec.symbol, rec.side)
        await self._registry.set_closing(rec.id, False, cause="recovery")  # 用户发起的平仓：回到托管中并提示结果
        summary["resumed"].append(rec.id)
        self._alert(AlertLevel.INFO, "recovery_closing_resumed", self._closing_message(rec, last),
                    dedupe_key=f"recovery_closing_resumed:{rec.id}")

    # ---- 还没查清的平仓：协调者之后把意图走到终态时，由这里把仓放回托管中

    async def _watch_closing(self, position_id: int, intent_ids: set[int]) -> None:
        self._closing_watch[position_id] = set(intent_ids)
        if self._closing_sub is None:
            self._closing_sub = self._bus.subscribe(IntentStateChanged, self._on_intent_event, name="recovery.closing")
        for intent_id in sorted(intent_ids):  # 读清单与开始观察之间已经到终态的不会再有事件：现在把现状核对一遍
            record = await self._intents.get(intent_id)
            if record is not None and record.state in IntentState.terminal():
                await self._intent_ended(intent_id)

    async def _on_intent_event(self, event: IntentStateChanged) -> None:
        if self._closing_watch and event.state in _TERMINAL_STATES:
            await self._intent_ended(event.intent_id)

    async def _intent_ended(self, intent_id: int) -> None:
        for position_id, waiting in list(self._closing_watch.items()):
            if intent_id not in waiting:
                continue
            waiting.discard(intent_id)
            if waiting:
                continue  # 这个仓上还有别的平仓类意图在途：等它们
            del self._closing_watch[position_id]
            try:
                await self._resume_settled(position_id, intent_id)
            except Exception:
                log.exception("重启恢复把平仓中的仓 %s 放回托管中失败", position_id)
                self._alert(AlertLevel.WARNING, "recovery_closing_resume_failed",
                            f"持仓 {position_id} 的平仓已有结论（意图 {intent_id}），但没能把它从「平仓中」放回托管中，请检查日志",
                            dedupe_key=f"recovery_closing_resume_failed:{position_id}")

    async def _resume_settled(self, position_id: int, intent_id: int) -> None:
        rec = self._registry.by_id(position_id)
        if rec is None or rec.status is not PositionStatus.CLOSING:
            return  # 已经平掉，或别的路径（协调者新一轮暂停结束时）已经放回
        others = [i for i in await self._intents.non_terminal()
                  if (i.symbol, i.side) == (rec.symbol, rec.side) and i.action in _CLOSE_ACTIONS and i.id != intent_id]
        if others:  # 恢复之后又有新的平仓类意图在途：接着等它们（新的部分平仓协调者自己也记着它的暂停，放回两边都无害）
            self._closing_watch[position_id] = {i.id for i in others}
            return
        last = await self._intents.get(intent_id)
        await self._registry.set_closing(position_id, False, cause="recovery")
        self._alert(AlertLevel.INFO, "recovery_closing_resumed", self._closing_message(rec, last),
                    dedupe_key=f"recovery_closing_resumed:{position_id}:{intent_id}")

    async def _resubmit(self, run: _Run, intent: IntentRecord, summary: dict[str, Any]) -> None:
        spec = IntentSpec(
            action=intent.action, model=intent.model, source=intent.source, symbol=intent.symbol, side=intent.side,
            idem_key=intent.idem_key, qty=intent.qty, ratio=intent.ratio, params=dict(intent.params),
            reason=intent.reason, ttl_s=None, operator=intent.operator, op_id=intent.op_id,
            position_id=intent.position_id, position_generation=intent.position_generation,
            parent_intent_id=intent.parent_intent_id, ai_eval_ref=intent.ai_eval_ref)
        try:
            submitted = await self._coordinator.submit(spec)  # 幂等键相同：不会建新意图，只让还没发出的重新派发
            summary["resubmitted"].append(submitted.intent_id)
        except Exception as exc:  # 平仓不会继续，仓位停在平仓中：算问题（照样保持平仓中、继续观察）
            log.exception("重启恢复重新提交平仓意图 %s 失败", intent.id)
            summary.setdefault("resubmit_failed", []).append(
                {"intent_id": intent.id, "error": f"{type(exc).__name__}: {exc}"})
            run.problems.append(f"{intent.symbol} {intent.side.value} 的平仓意图 {intent.id} 没能重新生效"
                                f"（{type(exc).__name__}: {exc}）")

    @staticmethod
    def _closing_message(rec: PositionRecord, last: IntentRecord | None) -> str:
        head = f"{rec.symbol} {rec.side.value} 重启前正在平仓中，恢复后已回到托管中"
        if last is None:
            return head + "（没有查到相关的平仓意图）"
        label = _CLOSE_LABEL.get(last.action, "平仓")
        return head + f"。最近一次{label}（意图 {last.id}）的结果：{last.state.value}" + (
            f"（{last.state_reason}）" if last.state_reason else "")

    # ================================================================ 第 7 步：K 线

    async def _step_candles(self, run: _Run, summary: dict[str, Any]) -> None:
        summary.update(symbols=[], timeframes=[], reverse_cross="M4 接入")
        symbols = self._monitor_symbols(run)
        summary["symbols"] = symbols
        if not symbols:
            return
        timeframes = list(dict.fromkeys(str(self._cfg(key, None)) for key in ("timeframe_short", "timeframe_long")
                                        if self._cfg(key, None)))
        summary["timeframes"] = timeframes
        await self._candles.sync_server_time()
        for timeframe in timeframes:
            await self._candles.warm_up(symbols, timeframe)
            await self._candles.refresh(symbols, timeframe)
        await self._replay_reverse_crosses(symbols)

    async def _replay_reverse_crosses(self, symbols: list[str]) -> None:
        """TODO（M4，T3.3）：对每个含「反向信号平仓」的仓，用回补的 K 线检查停机期间有没有发生过反向过零；有就只平不反。
        本波没有信号计算（M4 才接入），这里只留接口。"""
        return None

    async def _prepare_candles(self, run: _Run, summary: dict[str, Any]) -> None:
        """补保护之前先把短周期 K 线备好：登记新仓会立刻触发保护单管理算初始止损（极值 ± ATR），没有 K 线只能退回 A%。
        有时限：到点没好就按已有的算，不耽误补保护。"""
        symbols = self._monitor_symbols(run)
        timeframe = self._cfg("timeframe_short", None)
        if not symbols or not timeframe:
            return

        async def prepare() -> None:
            await self._candles.sync_server_time()
            await self._candles.warm_up(symbols, str(timeframe))
            await self._candles.refresh(symbols, str(timeframe))

        try:
            await asyncio.wait_for(prepare(), self.CANDLE_TIMEOUT_S)
            summary["candles_prepared"] = symbols
        except asyncio.TimeoutError:
            log.warning("重启恢复预备 K 线超时（%s 秒），初始止损按已有的 K 线算", self.CANDLE_TIMEOUT_S)
        except Exception:
            log.exception("重启恢复预备 K 线出错，初始止损按已有的 K 线算")

    # ================================================================ 第 8 步：孤儿条件单

    async def _step_orphans(self, run: _Run, summary: dict[str, Any]) -> None:
        summary.update(orders_reconcile="", cancel_intents=0)
        summary["orders_reconcile"] = await self._reconciler.reconcile_orders()  # F8 的五条由对账判；撤单经协调者
        idle = getattr(self._coordinator, "idle", None)
        if callable(idle):
            await idle(self.ORPHAN_WAIT_S)  # 等撤单意图做完
        await self._drain(summary)
        cancels = await self._ledger.read(repo_recovery.cancel_intents_since, since_ms=run.started_ms)
        summary["cancel_intents"] = len(cancels)
        summary["cancelled"] = sum(1 for i in cancels if i.state is IntentState.COMPLETED)

    # ================================================================ 第 9 步：中断的批量操作与切换

    async def _step_operations(self, run: _Run, summary: dict[str, Any]) -> None:
        summary.update(interrupted=[], locks_released=[], intents_cancelled=[])
        state = self._modelstate.current()
        for lock in sorted(state.op_locks, key=lambda x: x.value):
            await self._modelstate.release_op_lock(lock)  # 强制释放：持有者进程已经不在了
            summary["locks_released"].append(lock.value)
        for op in await self._ledger.read(repo_recovery.active_operations):
            try:
                await self._interrupt(run, op, summary)
            except Exception as exc:  # 一个操作记不下来不挡别的操作（它留着非终态，下一次恢复再记）
                log.exception("重启恢复把操作 %s 记为中断失败", op.op_id)
                summary.setdefault("errors", []).append({"op_id": op.op_id, "error": f"{type(exc).__name__}: {exc}"})
                run.problems.append(f"操作 {op.op_id} 记为中断失败（{type(exc).__name__}: {exc}）")

    async def _interrupt(self, run: _Run, op: OperationRecord, summary: dict[str, Any]) -> None:
        intents = await self._ledger.read(repo_recovery.intents_of_operation, op.op_id)
        for intent in intents:  # 还没发出订单的意图不续做：取消，免得它一直占着这个键
            if await self._cancel_unsent(intent):
                summary["intents_cancelled"].append(intent.id)
        per_position = [await self._position_result(run, intent) for intent in intents]
        result = {"interrupted": True, "reason": INTERRUPTED_REASON, "op_type": op.op_type.value,
                  "status_before": op.status.value, "interrupted_ms": self._clock.now_ms()}
        await self._operations.finish(op.op_id, OperationStatus.ABORTED, result=result, per_position=per_position)
        summary["interrupted"].append({"op_id": op.op_id, "op_type": op.op_type.value, "positions": len(per_position)})
        self._alert(AlertLevel.WARNING, "operation_interrupted",
                    f"操作 {op.op_id}（{op.op_type.value}）在进程重启时还没做完，已记为中断，不会自动续做；"
                    f"涉及 {len(per_position)} 个持仓，逐仓结果见操作记录", dedupe_key=f"operation_interrupted:{op.op_id}")

    async def _cancel_unsent(self, intent: IntentRecord) -> bool:
        if intent.state not in _PRE_SEND or await self._orders.for_intent(intent.id):
            return False
        target = IntentState.REJECTED if intent.state is IntentState.DISPATCH_READY else IntentState.CANCELLED
        try:
            await self._intents.transition(intent.id, target, reason=INTERRUPTED_REASON, expect={intent.state})
            return True
        except Exception:
            log.exception("重启恢复取消中断操作名下的意图 %s 失败", intent.id)
            return False

    async def _position_result(self, run: _Run, intent: IntentRecord) -> dict[str, Any]:
        """一个意图对应的持仓现在怎样：登记簿已平 / 交易所仍有仓 / 还没查清。"""
        key = (intent.symbol, intent.side)
        rec = None if intent.position_id is None else await self._registry.fetch(intent.position_id)
        if rec is None:
            rec = self._registry.get_active(*key)
        snap = run.snapshot
        if intent.state not in IntentState.terminal() and intent.state not in _PRE_SEND:
            outcome, detail = PerPositionResult.UNKNOWN, f"意图 {intent.id} 还在回查（{intent.state.value}），结果未知"
        elif rec is not None and rec.status is PositionStatus.CLOSED:
            outcome, detail = PerPositionResult.CLOSED, "已平" + (
                f"（{rec.exit_reason.value}）" if rec.exit_reason is not None else "")
        elif snap is None:
            outcome, detail = PerPositionResult.UNKNOWN, "没有交易所快照，持仓现状未知"
        elif key in snap.positions:
            outcome, detail = PerPositionResult.RESIDUAL, f"中断时仍有持仓，数量 {snap.positions[key].qty}"
        else:
            outcome, detail = PerPositionResult.CLOSED_EXTERNALLY, "交易所已无此仓（中断时的平仓是否由本操作完成未查清）"
        return {"position_id": intent.position_id if intent.position_id is not None else getattr(rec, "id", None),
                "symbol": intent.symbol, "side": intent.side.value, "intent_id": intent.id,
                "result": outcome.value, "detail": detail}

    # ================================================================ 第 10 步：释放、解除「恢复中」、结果

    async def _finish(self, run: _Run) -> None:
        snapshot_ms = run.snapshot.ms if run.snapshot is not None else 0  # 没有快照就没有「已被反映」的成交：全部应用
        summary = run.steps["release"]
        summary.update(snapshot_ms=snapshot_ms, applied=0, skipped=0, failed=0, late_replayed=0,
                       recovering_lifted=False)
        await self._replay_late(run, summary)
        try:
            released = await self._registry.release_fills(snapshot_ms)
            summary.update(applied=released.applied, skipped=released.skipped, failed=released.failed)
            if released.failed:
                run.problems.append(f"释放暂缓的成交时有 {released.failed} 笔应用失败，登记簿可能落后于账本")
        except Exception as exc:
            log.exception("释放暂缓的成交失败")
            summary["error"] = f"{type(exc).__name__}: {exc}"
            run.problems.append(f"释放暂缓的成交出错：{exc}")
        if not run.problems:
            try:
                await self._modelstate.remove_suppression(Suppression.RECOVERING, reason="重启恢复完成")
                summary["recovering_lifted"] = True
            except Exception as exc:
                log.exception("解除「恢复中」失败")
                summary["lift_error"] = f"{type(exc).__name__}: {exc}"
                run.problems.append(f"解除「恢复中」出错：{exc}")
        elif not run.steps["guard"].get("problems"):
            self._alert(AlertLevel.CRITICAL, "recovery_incomplete",
                        "重启恢复没有完整做完，保持「恢复中」、不放开交易：" + "；".join(run.problems),
                        dedupe_key="recovery_incomplete")
        stats = self._summary(run)
        run.steps["summary"] = stats
        if not run.problems:
            self._alert(AlertLevel.INFO, "recovery_done",
                        f"重启恢复完成：补保护 {stats['stops_rebuilt']} 个仓，补记已平 {stats['closed_by_fills']} 笔，"
                        f"待确认 {stats['pending_registered']} 个，清理孤儿条件单 {stats['orphans_cleaned']} 张，"
                        f"中断的操作 {stats['operations_interrupted']} 个")

    async def _replay_late(self, run: _Run, summary: dict[str, Any]) -> None:
        """释放之前再回放一遍：数据流把「成交时间早于快照、到得比快照晚」的成交送来了（两条路径谁先谁后没有保证），
        不回放的话释放时按「快照已经反映」跳过，登记仓的数量就永远少算这一笔。第 4 步里回放失败的也在这里再试一次，
        仍然失败才算问题。"""
        snap = run.snapshot
        if snap is None:
            return
        try:
            await self._drain(summary)
            done, failed = await self._replay(run, snap)
            summary["late_replayed"] = done
            if failed:
                run.problems.append(f"回放成交（含释放前补做的这一遍）仍有 {failed} 笔失败，登记簿可能落后于账本")
            if done:
                await self._drain(summary)
        except Exception as exc:
            log.exception("重启恢复回放迟到的成交失败")
            summary["late_replay_error"] = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _summary(run: _Run) -> dict[str, Any]:
        steps = run.steps
        return {
            # 补上（或认领到）系统止损的仓；其它结论（用户自己的止损、还在重试、保护性平仓已发出）只写在步骤摘要里
            "stops_rebuilt": sum(1 for e in steps["protection"].get("rebuilt", ()) if e["result"] == "protected"),
            "closed_by_fills": len(steps["fills"].get("closed_by_fills", ())),
            "pending_registered": len(steps["reconcile"].get("pending_registered", ())),
            "orphans_cleaned": steps["orphans"].get("cancelled", 0),
            "operations_interrupted": len(steps["operations"].get("interrupted", ())),
        }

    def _result(self, run: _Run) -> RecoveryResult:
        return RecoveryResult(tradable=not run.problems, problems=tuple(run.problems),
                              duration_ms=self._clock.now_ms() - run.started_ms, steps=dict(run.steps))

    # ================================================================ 小工具

    async def _drain(self, summary: dict[str, Any]) -> None:
        try:
            await self._bus.drain(self.DRAIN_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("事件总线 %s 秒内没有处理完", self.DRAIN_TIMEOUT_S)
            summary["drain_timeout"] = True

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            return self._config_get(key)
        except LookupError:
            return default

    def _alert(self, level: AlertLevel, kind: str, message: str, *, dedupe_key: str | None = None) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.raise_alert(level, kind, message, dedupe_key=dedupe_key)
        except Exception:
            log.exception("提出告警失败：%s", kind)


def _is_system(cid: str | None) -> bool:
    return ids.is_system_id(cid)


def system_stops(algos: tuple[AlgoOrderInfo, ...] | list[AlgoOrderInfo], symbol: str,
                 side: PositionSide) -> list[AlgoOrderInfo]:
    """这个键上带系统前缀的、止损类的、还挂着的条件单。"""
    return [a for a in algos if a.symbol == symbol and a.position_side is side and a.is_open
            and a.algo_type.is_stop_kind and _is_system(a.client_algo_id)]
