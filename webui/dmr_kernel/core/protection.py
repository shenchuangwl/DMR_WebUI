"""保护单管理（第 5 层；计划报告 F2 进入托管即保护与移动止损、F3 第 6 步外部保护、F13 交给系统管理止损、4.11 故障通过标准）。

每个登记仓（接管中、托管中、平仓中）都要有止损：系统自己挂一张全平型止损市价条件单（系统保护），或者用户已经挂了满足
F3 第 6 步四条的止损（外部保护，系统不动它）。本模块只**决定**（算止损价、什么时候挂、失败后怎么办）；
交易所的写操作一律提交成交易意图（STOP_PLACE / STOP_REPLACE / STOP_CANCEL / CLOSE）交给协调者执行，结果以意图为准。

**触发**：``start()`` 后订阅 ``PositionRegistered``/``PositionChanged``（登记即保护）、``PositionClosed``（归零后清理）、
短周期 ``CandlesReady``（移动止损）、``AlgoOrderObserved``（数据流看到系统止损被撤）、``OrdersSnapshotTaken``（条件单对账快照：
系统止损还在不在、外部保护是否仍然有效）和 ``IntentStateChanged``（只处理自己提交的意图）。处理函数都在后台任务里跑，
同一个持仓的动作用一把锁串行。**重试没有内部定时器**：调度器每隔几秒调用 ``tick()``（建议不超过 2 秒），重试、持续重试、
空窗计时、兜底扫描意图结果都在 ``tick()`` 里按注入的时钟评估，所以能用 ManualClock 精确测试。

**F2 的失败处理**（``ensure_protected`` / ``rebuild`` 共用一个「保护周期」）：

- 周期开始时保护状态记「重建中」（挂单的这几秒不算无保护，不置抑制）；成功后记「系统保护」。
- 其它原因挂不上：重试间隔 = 窗口 / (次数 + 1)，第 k 次重试在周期开始后 k × 间隔（默认 30 秒、3 次：7.5、15、22.5 秒），
  **最后一次重试失败即下结论**（报告 4.11：接管仓要在 ``protect_retry_window_s`` 秒内转为无保护；调度器每 ~2 秒 tick 一次、
  再加交易所往返，所以最后一次重试不能排在窗口末尾）：
  策略仓（非混合仓）市价平掉（保护性平仓，``retry_until_done``）并冷却 ``cooldown_bars`` 根 K 线，平仓时限在窗口 +
  ``close_one_timeout_s`` 之内；接管仓与混合仓不自动平，保护状态「无保护」、置抑制、发最高级告警，之后每个间隔（同上）持续重试。
  市价平仓也失败：保持「无保护」、发最高级告警，之后同样的间隔持续重试（先再挂止损，挂不上再平仓）。
  平仓意图超过 ``close_one_timeout_s`` 仍没有结论（读不到持仓、结果未知，协调者仍在重试）同样算「市价平仓也失败」：
  发最高级告警（不另发第二笔平仓）。算止损价时的意外错误也算一次失败的尝试（计入重试，窗口用完照样下结论）；
  提交挂单前再看一眼登记簿，算止损价期间已平或被取消托管的不再挂。
- −2021（会立即触发）：策略仓立刻市价平掉并冷却，不重试；接管仓以当刻价格为基准重算（备注「止损已按现价重定」），
  窗口内最多重算 ``protect_retries`` 次，期间记「重建中」，仍挂不上转接管仓的「无保护」分支。
  交易所行为若是「立即触发」，止损单受理后马上成交，登记簿按成交归零，本模块只负责清理残留。
- 初始止损价：``algo.stoploss.initial_stop``（最近 6 根已收盘短周期 K 线的极值 ± 该根 ATR，夹在距基准价 A%–B% 之间）；
  策略仓基准价取成交均价，接管仓取所配置触发价类型对应的当刻价格；按价格步长朝远离现价的方向取整。
  K 线不足以算 ATR 时按 A% 设置，``stop_note`` 注明。崩溃点 ``open_filled_before_stop`` 在第一次挂初始止损之前。

**移动止损**（``trailing``）：每满 6 根已收盘短周期 K 线算一次（``window6``/``stop_price``，周期计数存在 ``position.stop_cycle``，
无论是否采纳都前进）；只有更有利且没被现价越过的才采纳；多个周期同时到齐时取其中最有利的一个，只换一次单。
平仓中的仓顺延到恢复后补做（不前进周期计数）；同一合约订单频率到上限时顺延。更新方式按 ``ex_stop_update_mode`` 交给协调者
STOP_REPLACE，结果**以意图的 ``params["result"]["outcome"]`` 为准**（意图被入账侧重开再完成也不会改变结论；每个意图只处理一次）：
``replaced``/``restored_old`` 登记新的止损编号；``stop_crossed`` 市价平仓（策略仓与接管仓相同，不冷却）；
``gap_exceeded`` 若新止损已在位则采纳并告警，否则按「仍失败」分支处置；``kept_old`` 不动；``unprotected`` 按 F2 第 4 步重试，
但从撤旧单（提交更新）起超过 ``stop_gap_max_s`` 仍没挂上就直接按「仍失败」处置，不再重试一轮。
失败的结论迟到、而登记簿里已是之后另挂上的止损（例如空窗超时后的补挂）时，结论只关于被撤掉的旧单，不再处置。
协调者在新止损结果未知、之后回查确认存在时报 ``replaced`` 而不做空窗判定：gap 模式下本模块自己按 ``stop_gap_max_s`` 计时，
从提交更新起超过上限仍不能确认新单存在，按「仍失败」分支处置；先建后撤（make_before_break）在新单确认之前从不撤旧单，
没有空窗，只等意图的结论。

**结果未知不补挂（F8）**：自己提交的挂止损/换止损意图还没有结论时，旧单的「已撤」推送、条件单快照、外部保护复核、``rebuild()``
都不另挂止损，只等那个意图的结论（gap 模式换止损由上面的空窗计时兜底）。

**手动撤掉系统止损**：数据流事件（推送打开时）或条件单快照发现登记的系统止损不在交易所了，先直接问交易所确认
（读不到不动），确认不在就按登记的止损价立即补挂（90 秒内重新存在）。订单状态 CANCEL_PENDING 但交易所仍挂着的止损视为有效。

**外部保护**（接管仓）：``external_protection_ok`` 判定 F3 第 6 步四条；满足则登记「外部保护」，系统不动用户的止损；
每次条件单快照都复核，失效即告警并转入 F2。用户的止盈单一律不动。``hand_stop_to_system`` 实现 F13「交给系统管理止损」。

**归零后清理**（``cleanup_after_close``）：撤当前止损、待清理的旧单、已移交的单（崩溃点 ``close_filled_before_cleanup`` 在清理之前）；
撤成功才从登记簿里清掉对应编号，撤不成的留着下次再来。仓位还没归零（登记簿里仍是活动持仓）时绝不撤。

**无保护抑制**（``check_unprotected``）：有登记仓处于「无保护」就置抑制原因，否则解除。

**告警**（kind：级别）：``protect_unprotected``：最高级（接管仓/混合仓挂不上止损，无保护，持续重试）；``protective_close_failed``：最高级
（保护性市价平仓失败、没能提交，或超过 ``close_one_timeout_s`` 仍没平掉）；``protect_failed_closing``：警告（策略仓挂不上止损，已发起保护性平仓）；``protect_slow``：警告
（超过 ``protect_target_s`` 仍没挂上）；``stop_missing_rebuilt``：警告（系统止损不在交易所，已补挂）；``external_protection_lost``：警告
（用户的止损失效，转入 F2）；``stop_gap_exceeded``、``stop_crossed_closing``：警告；``protect_restored``：提示（保护恢复）。

**已知限制**：交易所行为是「立即触发」、且触发后订单号不可关联（实测第 12 项默认取值）时，止损单受理后的成交回报可能比本模块记下
「系统保护」更早到达登记簿，登记簿的「推定为系统止损触发」（要求保护状态为系统保护）因此不成立，出场原因可能记成外部；
仓位照常按成交归零、没有保护性平仓、没有残留条件单。触发后订单号可关联时一律记止损。

规格之外的公开成员：``tick``、``idle``、``crashed``、``hand_stop_to_system``，构造函数的可选参数 ``limits``（订单频率）、``wait_s``
（等意图出结果的真实秒数，缺省取配置 ``protect_target_s``）。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Iterable

from dmr_kernel.algo.rounding import round_stop_price
from dmr_kernel.algo.stoploss import initial_stop, is_better, stop_price as stop_calc, window6
from dmr_kernel.core.clock import Clock, interval_ms
from dmr_kernel.core.crashpoints import CrashRegistry, CrashSignal
from dmr_kernel.core.events import (
    AlgoOrderObserved,
    CandlesReady,
    EventBus,
    IntentStateChanged,
    OrdersSnapshotTaken,
    PositionChanged,
    PositionClosed,
    PositionRegistered,
    Subscription,
)
from dmr_kernel.core.intents import IntentSpec, key_protect
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    AlertLevel,
    AlgoOrderInfo,
    IntentAction,
    IntentSource,
    IntentState,
    OrderOrigin,
    OrderPurpose,
    OrderState,
    Ownership,
    PositionSide,
    PositionStatus,
    ProtectionState,
    Suppression,
    Unknown,
    WorkingType,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.adapter import ExchangeAdapter
from dmr_kernel.exchange.rules import InstrumentRules
from dmr_kernel.ledger import repo_orders
from dmr_kernel.ledger.records import IntentRecord, OrderRecord, PositionRecord
from dmr_kernel.ledger.writer import Ledger

if TYPE_CHECKING:  # 只用于类型标注
    from dmr_kernel.alerts.notifier import Notifier
    from dmr_kernel.core.coordinator import Coordinator
    from dmr_kernel.core.limits import Limits
    from dmr_kernel.marketdata.candles import CandleService

log = logging.getLogger(__name__)

__all__ = [
    "MAX_CATCH_UP", "NOTE_NO_ATR", "NOTE_REDEFINED", "Protection",
]

MAX_CATCH_UP = 24  # 一次移动止损最多补算几个 6 根周期（停机很久之后逐步追上）
NOTE_NO_ATR = "K 线不足以计算 ATR，初始止损按 A% 设置"
NOTE_REDEFINED = "止损已按现价重定"
NOTE_EXTERNAL = "外部保护：用户已挂止损，系统不动它"
RESULT_KEY = "result"  # 协调者把 STOP_REPLACE 的结果写在 intent.params["result"]
_TERMINAL_STATES = frozenset(s.value for s in IntentState.terminal())
_TERMINAL_INTENTS = frozenset(IntentState.terminal())
_DONE_KEPT = 4096  # 记住最近处理过的多少个意图号（防止重复处理）
_REGISTERED = PositionStatus.registered()
_STOP_GONE_KEEP = frozenset({"TRIGGERED", "TRIGGERING", "FINISHED"})  # 这几个状态是止损在触发，不是被撤
# stop_note 里属于「上一阶段」的过渡说明：保护恢复后清掉；A% 备注、「止损已按现价重定」这类描述止损价来历的备注保留
_TRANSIENT_NOTES = ("无保护", "系统止损不在交易所", "用户的止损失效", "外部保护")
# 持仓数量变了的 PositionChanged 原因（成交入账、外部调整）：外部保护的数量型止损可能因此不再满足四条，立即复核
_QTY_CHANGE_CAUSES = frozenset({"fill", "外部调整"})
# 窗口内的尝试结果还没出来时下结论的余量（调度器每 ~2 秒 tick 一次，再加交易所往返）：结论最迟在窗口结束前这么多毫秒
# （且不早于窗口减半个重试间隔，保证排在最后一次重试之后）
_VERDICT_MARGIN_MS = 3000
_RATE_LIMITED_CODE = -1003  # 适配层把限频拒绝与限频退避中的「未知」都标成这个错误码


class _PlanFailed(Exception):
    """算不出止损价或读不到所需数据：这一次尝试算失败。"""


@dataclass
class _Cycle:
    """一个持仓的「保护周期」：从开始挂止损（或补挂）到挂上为止的内存状态。"""

    kind: str
    started_ms: int
    retries_used: int = 0  # 已经安排过几次重试
    next_due_ms: int | None = None  # 下一次尝试的时间
    redefined: int = 0  # 接管仓因 −2021 按现价重算的次数
    recompute: bool = False  # 下一次要按现价重算（忽略登记的止损价）
    persistent: bool = False  # 已下「仍失败」的结论，之后按固定间隔持续重试
    pending: int | None = None  # 在途的 STOP_PLACE 意图
    close_pending: int | None = None  # 在途的保护性平仓意图
    close_done_ms: int | None = None  # 平仓意图完成的时间（之后等成交入账）
    crash_checked: bool = False
    slow_alerted: bool = False  # 已经因超过 protect_target_s 仍没挂上而告警过
    close_alerted: bool = False  # 保护性平仓超过 close_one_timeout_s 仍没有结论，已发最高级告警
    last_error: str = ""
    # gap 模式移动止损失败（新旧两张都没挂上）后的无保护上限（撤旧单起 stop_gap_max_s）：到点仍没挂上就直接按「仍失败」处置，
    # 不再重试一轮（F2 第 7 步）
    deadline_ms: int | None = None


@dataclass
class _Tracked:
    """自己提交、还在等结果的意图。"""

    kind: str  # place | replace | close
    position_id: int
    started_ms: int
    trigger: Decimal | None = None
    note: str | None = None
    hand: bool = False  # F13：交给系统管理止损
    extra_old: list[dict[str, Any]] = field(default_factory=list)
    gap_judged: bool = False
    mode: str = ""  # STOP_REPLACE 的更新方式（gap | make_before_break）：只有 gap 模式撤了旧单才有空窗


def _is_system(cid: str | None) -> bool:
    return ids.parse_client_id(cid).origin is OrderOrigin.SYSTEM


def _fmt(value: Decimal) -> str:
    return format(value, "f")


class Protection:
    def __init__(self, *, coordinator: "Coordinator", registry: Registry, modelstate: ModelStateService,
                 candles: "CandleService", rules: InstrumentRules, adapter: ExchangeAdapter, ledger: Ledger,
                 bus: EventBus, clock: Clock, config_get: Callable[[str], Any], notifier: "Notifier | None",
                 crash: CrashRegistry, limits: "Limits | None" = None, wait_s: float | None = None) -> None:
        self._coordinator = coordinator
        self._registry = registry
        self._modelstate = modelstate
        self._candles = candles
        self._rules = rules
        self._adapter = adapter
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._config_get = config_get
        self._notifier = notifier
        self._crash = crash
        self._limits = limits
        self._wait_s = wait_s

        self._subs: list[Subscription] = []
        self._tasks: set[asyncio.Task] = set()
        self._running = False
        self._crashed: CrashSignal | None = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._cycles: dict[int, _Cycle] = {}
        self._tracked: dict[int, _Tracked] = {}
        self._done: OrderedDict[int, None] = OrderedDict()
        self._deferred: set[int] = set()  # 移动止损因「平仓中」而顺延的持仓
        self._placed_ms: dict[int, int] = {}  # 最近一次挂/换止损的时间：早于它的条件单快照不作数
        self._nonce = secrets.randbits(30)  # 幂等键里的实例随机数：重启后的新实例不会和旧实例的键撞上
        self._seq = 0

    # ================================================================ 生命周期

    async def start(self) -> None:
        """订阅事件。重复调用无害。"""
        if self._subs:
            return
        sub = self._bus.subscribe
        self._running = True
        self._subs = [
            sub(PositionRegistered, self._on_registered, name="protection.registered"),
            sub(PositionChanged, self._on_changed, name="protection.changed"),
            sub(PositionClosed, self._on_closed, name="protection.closed"),
            sub(CandlesReady, self._on_candles, name="protection.candles"),
            sub(AlgoOrderObserved, self._on_algo, name="protection.algo"),
            sub(OrdersSnapshotTaken, self._on_snapshot, name="protection.snapshot"),
            sub(IntentStateChanged, self._on_intent, name="protection.intents"),
        ]

    async def stop(self) -> None:
        """取消订阅，取消在途的后台任务。"""
        self._running = False
        for item in self._subs:
            item.cancel()
        self._subs = []
        tasks = [t for t in self._tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def idle(self, timeout_s: float = 10.0) -> None:
        """等所有后台任务结束（测试与关停用）；到点没结束就返回。"""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            pending = [t for t in self._tasks if not t.done()]
            remaining = deadline - asyncio.get_running_loop().time()
            if not pending or remaining <= 0:
                return
            await asyncio.wait(pending, timeout=remaining)

    @property
    def crashed(self) -> CrashSignal | None:
        """崩溃点触发过就是那个 ``CrashSignal``（之后本实例视为已死，不再做任何事）。"""
        return self._crashed

    # ================================================================ 配置与小工具

    def _cfg(self, key: str) -> Any:
        return self._config_get(key)

    def _window_ms(self) -> int:
        return int(self._cfg("protect_retry_window_s")) * 1000

    def _retries(self) -> int:
        return int(self._cfg("protect_retries"))

    def _interval_ms(self) -> int:
        """重试的间隔 = 窗口 / (次数 + 1)：窗口内的重试按它排（第 k 次在 k × 间隔），结论之后的持续重试也用它。
        没有重试（次数为 0）时就是整个窗口。"""
        return max(self._window_ms() // (self._retries() + 1), 1)

    def _working_type(self) -> WorkingType:
        return WorkingType(self._cfg("stop_trigger_price_type"))

    def _wait_seconds(self) -> float:
        return float(self._wait_s) if self._wait_s is not None else float(self._cfg("protect_target_s"))

    def _now(self) -> int:
        return self._clock.now_ms()

    def _lock(self, position_id: int) -> asyncio.Lock:
        return self._locks.setdefault(position_id, asyncio.Lock())

    def _check_alive(self) -> None:
        if self._crashed is not None:
            raise self._crashed

    def _die(self, signal: CrashSignal) -> None:
        """崩溃点触发：本实例视为被杀，其它后台任务取消，不再受理任何事件。"""
        if self._crashed is None:
            self._crashed = signal
        self._running = False
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current and not task.done():
                task.cancel()

    async def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        try:
            return await coro
        except CrashSignal as signal:
            self._die(signal)
            raise

    def _spawn(self, fn: Callable[..., Coroutine[Any, Any, Any]], *args: Any, **kwargs: Any) -> None:
        """在后台任务里跑 ``fn(*args, **kwargs)``（协程在任务里才创建：任务被取消时不会留下没有执行的协程）。"""
        if not self._running or self._crashed is not None:
            return
        task = asyncio.get_running_loop().create_task(self._guarded(fn, *args, **kwargs))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, fn: Callable[..., Coroutine[Any, Any, Any]], *args: Any, **kwargs: Any) -> None:
        try:
            await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except CrashSignal as signal:
            self._die(signal)
        except Exception:
            log.exception("保护单管理的后台任务出错")

    def _alert(self, level: AlertLevel, kind: str, message: str, key: str | None = None) -> None:
        log.log(logging.ERROR if level is AlertLevel.CRITICAL else logging.WARNING, "%s：%s", kind, message)
        if self._notifier is None:
            return
        try:
            self._notifier.raise_alert(level, kind, message, dedupe_key=key)
        except Exception:
            log.exception("发告警失败：%s", kind)

    def _key(self, rec: PositionRecord, purpose: str) -> str:
        """保护动作的幂等键：持仓编号 + 代际 + 用途 + 序号（序号含本实例随机数，重启后的新实例不会撞上旧键）。"""
        self._seq += 1
        return key_protect(rec.id, rec.generation, purpose, (self._nonce << 20) | (self._seq & 0xFFFFF))

    @staticmethod
    def _stale_note(rec: PositionRecord | None) -> str | None:
        """保护恢复时 stop_note 要怎么改：是过渡说明就清空（返回 ""），否则不改（返回 None）。"""
        return "" if rec is not None and rec.stop_note.startswith(_TRANSIENT_NOTES) else None

    @staticmethod
    def _soft_note(rec: PositionRecord, text: str) -> str | None:
        """过渡说明只写在没有备注或备注本身也是过渡说明的时候；描述止损价来历的备注（A%、按现价重定）不被覆盖。"""
        return None if rec.stop_note and not rec.stop_note.startswith(_TRANSIENT_NOTES) else text

    @staticmethod
    def _system_owned(rec: PositionRecord) -> bool:
        """F2 失败分支里的「策略仓」：系统自己开的、没被外部加过仓的。接管仓与混合仓不自动平。"""
        return rec.ownership in (Ownership.STRATEGY, Ownership.AI) and not rec.is_mixed

    def _in_flight(self, position_id: int) -> bool:
        """这个持仓有没有自己提交、还没出结论的挂止损或换止损意图。有就只等它的结论（F8：结果未知时不补挂，只重查）；
        gap 模式下换止损迟迟没有结论由 ``stop_gap_max_s`` 计时处置（``_poll_tracked``）。"""
        return any(t.position_id == position_id and t.kind in ("place", "replace") for t in self._tracked.values())

    # ================================================================ 公开：保护周期

    async def ensure_protected(self, position_id: int, *, reason: str) -> str:
        """F2 第 1–5 步：让这个登记仓有保护。返回：

        ``protected`` 系统止损在位 | ``external`` 用户的止损满足四条 | ``rebuilding`` 正在挂或等重试 |
        ``closing`` 策略仓的保护性市价平仓已发出 | ``unprotected`` 无保护（接管仓持续重试，或平仓也失败） |
        ``closed`` 仓位已不在登记簿 | ``skipped`` 不是登记仓（待确认、已忽略）。
        ``reason`` 为 ``registered`` 的策略仓直接挂单（目标 ``protect_target_s`` 秒），其余情形先问交易所有没有现成的系统止损。
        """
        self._check_alive()
        async with self._lock(position_id):
            return await self._run(self._ensure_locked(position_id, reason))

    async def rebuild(self, position_id: int, *, reason: str) -> str:
        """按登记的止损价补挂（F9 第 5 步、F2 第 9 步）：先直接问交易所，系统止损还在就认领，读不到就什么也不做（返回 ``unknown``），
        确认不在才补挂；登记里没有止损价时按 F2 第 1 步当刻计算。返回值同 ``ensure_protected``，另有 ``unknown``。"""
        self._check_alive()
        async with self._lock(position_id):
            rec = self._registry.by_id(position_id)
            if rec is None:
                return "closed"
            if rec.status not in _REGISTERED:
                return "skipped"
            return await self._run(self._rebuild_locked(rec, reason))

    async def check_unprotected(self) -> None:
        """有登记仓处于「无保护」就置抑制原因（不再新开仓），否则解除（F2 第 5 步）。"""
        bad = [r for r in self._registry.registered() if r.protection_state is ProtectionState.NONE]
        try:
            present = Suppression.UNPROTECTED in self._modelstate.current().suppressions
            if bad and not present:
                names = "、".join(f"{r.symbol} {r.side.value}" for r in bad[:5])
                await self._modelstate.add_suppression(
                    Suppression.UNPROTECTED, reason=f"{len(bad)} 个登记仓处于无保护：{names}")
            elif not bad and present:
                await self._modelstate.remove_suppression(Suppression.UNPROTECTED, reason="没有无保护的登记仓")
        except Exception:
            log.exception("更新「存在无保护持仓」抑制原因失败")

    async def tick(self) -> None:
        """定时驱动（调度器每隔几秒调一次）：扫描自己提交的意图的结果、到期的重试、移动止损的空窗计时、
        没有保护周期却没保护的登记仓；最后更新无保护抑制。"""
        if self._crashed is not None:
            return
        for iid in list(self._tracked):
            await self._guard_step(self._poll_tracked(iid))
        for pid in list(self._cycles):
            await self._guard_step(self._drive_cycle(pid))
        for rec in list(self._registry.registered()):
            if rec.protection_state in (ProtectionState.NONE, ProtectionState.REBUILDING) and rec.id not in self._cycles:
                await self._guard_step(self.ensure_protected(rec.id, reason="tick"))
        self._prune_locks()
        await self.check_unprotected()

    def _prune_locks(self) -> None:
        """已平仓位的锁不再需要（没人持有、没有周期、没有在途意图时丢掉）。"""
        busy = {c.position_id for c in self._tracked.values()}
        for pid, lock in list(self._locks.items()):
            if pid not in busy and pid not in self._cycles and not lock.locked() and self._registry.by_id(pid) is None:
                del self._locks[pid]

    async def _guard_step(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            await self._run(coro)
        except CrashSignal:
            raise
        except Exception:
            log.exception("保护单管理的定时工作出错")

    # ================================================================ 保护周期的核心

    async def _ensure_locked(self, position_id: int, reason: str) -> str:
        rec = self._registry.by_id(position_id)
        if rec is None:
            return "closed"
        if rec.status not in _REGISTERED:
            return "skipped"
        if rec.protection_state is ProtectionState.SYSTEM and (rec.stop_client_id or rec.stop_algo_id):
            if rec.status is PositionStatus.TAKING_OVER:
                await self._registry.mark_managed(rec.id)
            return "protected"
        if rec.protection_state is ProtectionState.EXTERNAL:
            return await self._recheck_external(rec)
        cycle = self._cycles.get(rec.id)
        if cycle is not None:  # 已经有周期在跑：重试的节奏由周期自己掌握，不另起一次尝试
            if cycle.next_due_ms is not None and self._now() >= cycle.next_due_ms:
                return await self._attempt_with_preread(rec, cycle)
            return self._cycle_status(cycle)
        cycle = self._cycles[rec.id] = _Cycle(kind=reason, started_ms=self._now())
        if rec.stop_price is None and not (rec.stop_client_id or rec.stop_algo_id):
            cycle.crash_checked = True
            self._crash.hit("open_filled_before_stop")  # 崩溃点 2：已开仓、初始止损还没挂
        await self._set_state(rec, ProtectionState.REBUILDING)
        if rec.ownership is Ownership.TAKEOVER or reason != "registered":
            done = await self._pre_read(rec, cycle)
            if done is not None:
                return done
        return await self._attempt(rec.id, cycle)

    @staticmethod
    def _cycle_status(cycle: _Cycle) -> str:
        if cycle.close_pending is not None or cycle.close_done_ms is not None:
            return "closing"
        if cycle.persistent and cycle.pending is None:
            return "unprotected"
        return "rebuilding"

    async def _rebuild_locked(self, rec: PositionRecord, reason: str) -> str:
        if self._in_flight(rec.id):
            return self._status_of(rec)  # 在途的挂/换止损结果未知：不补挂第二张，等它的结论（F8）
        algos = await self._open_algos(rec.symbol)
        if algos is None:
            return "unknown"  # F8：读不到不动
        cycle = self._cycles[rec.id] = _Cycle(kind=f"rebuild:{reason}", started_ms=self._now())
        done = await self._pre_read(rec, cycle, algos)  # 交易所上已有系统止损就认领；接管仓用户的止损满足四条就记外部保护
        if done is not None:
            return done
        if rec.protection_state is ProtectionState.SYSTEM:
            self._alert(AlertLevel.WARNING, "stop_missing_rebuilt",
                        f"{rec.symbol} {rec.side.value} 的系统止损不在交易所了（可能被手动撤掉），立即按登记的止损价补挂（{reason}）",
                        key=f"stop_missing:{rec.id}")
        elif rec.protection_state is ProtectionState.EXTERNAL:
            self._alert(AlertLevel.WARNING, "external_protection_lost",
                        f"{rec.symbol} {rec.side.value} 用户的止损已失效（被撤销，或不再满足外部保护的四条标准），系统转入自己挂止损",
                        key=f"external_lost:{rec.id}")
        await self._set_state(rec, ProtectionState.REBUILDING, note=self._soft_note(rec, "系统止损不在交易所，补挂中"))
        return await self._attempt(rec.id, cycle)

    async def _drive_cycle(self, position_id: int) -> None:
        async with self._lock(position_id):
            cycle = self._cycles.get(position_id)
            rec = self._registry.by_id(position_id)
            if cycle is None:
                return
            if rec is None or rec.status not in _REGISTERED:
                self._cycles.pop(position_id, None)  # 已平，或已不是登记仓（取消托管成了外部·已忽略）：不再替它挂止损
                return
            now = self._now()
            if (not cycle.persistent and not cycle.slow_alerted
                    and now - cycle.started_ms > int(self._cfg("protect_target_s")) * 1000):
                cycle.slow_alerted = True  # 目标是 protect_target_s 秒内挂上；超了先提醒（最迟 30 秒由重试窗口保证）
                self._alert(AlertLevel.WARNING, "protect_slow",
                            f"{rec.symbol} {rec.side.value} 的止损超过 {self._cfg('protect_target_s')} 秒的目标仍没有挂上，仍在重试"
                            f"（{cycle.last_error or '等待交易所结果'}）", key=f"protect_slow:{rec.id}")
            if cycle.close_pending is not None:
                self._close_overdue(rec, cycle, now)
                return
            if cycle.pending is not None:
                if self._verdict_overdue(cycle, now):
                    await self._still_failing(
                        rec, cycle, f"重试窗口将尽，止损建单的结果仍未知（{cycle.last_error or '等待交易所结果'}）")
                return
            if cycle.next_due_ms is None and cycle.close_done_ms is None:
                cycle.next_due_ms = now  # 没有安排下一次尝试、也没有在途动作（上一步出了意外错误）：兜底再试
            if cycle.close_done_ms is not None:
                if now - cycle.close_done_ms <= int(self._cfg("close_one_timeout_s")) * 1000:
                    return  # 平仓单已完成，等成交入账
                cycle.close_done_ms = None  # 平仓完成了很久仓位还在：当作这次平仓没成
                cycle.next_due_ms = now
            if cycle.next_due_ms is not None and now >= cycle.next_due_ms:
                await self._attempt_with_preread(rec, cycle)

    def _verdict_overdue(self, cycle: _Cycle, now: int) -> bool:
        """窗口内的尝试结果迟迟不出来（建单应答未知，协调者要按签名时间窗回查、连续几次查不到才判不存在，一次就要十几秒），
        而重试窗口快用完了：不再等它，按「仍失败」下结论（报告 4.11：接管仓在 ``protect_retry_window_s`` 秒内转无保护，
        策略仓在窗口加 ``close_one_timeout_s`` 秒内平掉）。在途的那张不撤、也不另挂第二张：它的结论到了照常处理
        （其实挂上了就记系统保护、解除抑制；策略仓那时已在平仓，平掉后清理残留止损）。"""
        if cycle.persistent or cycle.close_pending is not None or cycle.close_done_ms is not None:
            return False
        margin = min(_VERDICT_MARGIN_MS, self._interval_ms() // 2)
        return now >= cycle.started_ms + self._window_ms() - margin

    def _close_overdue(self, rec: PositionRecord, cycle: _Cycle, now: int) -> None:
        """保护性平仓意图超过 ``close_one_timeout_s`` 仍没有结论（读不到持仓、结果未知，协调者仍在重试）：仓还在、没有止损，
        就是 F2 第 4 步的「市价平仓也失败」，发最高级告警（每次平仓只发一次）。平仓仍交给那个意图，不另发第二笔。"""
        ctx = self._tracked.get(cycle.close_pending) if cycle.close_pending is not None else None
        limit_ms = int(self._cfg("close_one_timeout_s")) * 1000
        if ctx is None or cycle.close_alerted or now - ctx.started_ms <= limit_ms:
            return
        cycle.close_alerted = True
        self._alert(AlertLevel.CRITICAL, "protective_close_failed",
                    f"{rec.symbol} {rec.side.value} 的保护性市价平仓超过 {self._cfg('close_one_timeout_s')} 秒仍没有平掉"
                    f"（结果未知，系统仍在重试），该仓现在没有保护，请立即人工核对", key=f"protective_close_failed:{rec.id}")

    async def _attempt_with_preread(self, rec: PositionRecord, cycle: _Cycle) -> str:
        done = await self._pre_read(rec, cycle)
        if done is not None:
            return done
        return await self._attempt(rec.id, cycle)

    async def _pre_read(self, rec: PositionRecord, cycle: _Cycle, algos: list[AlgoOrderInfo] | None = None) -> str | None:
        """挂单前先问交易所（``algos`` 给了就用现成的读数）：已有系统止损就认领（不挂第二张）；接管仓看用户的止损算不算外部保护。
        返回 None 表示继续挂单。"""
        if algos is None:
            algos = await self._open_algos(rec.symbol)
        if algos is None:
            return await self._attempt_failed(rec, cycle, "读不到条件单，暂不挂单")
        mine = self._system_stops(rec, algos)
        if mine:
            await self._adopt(rec, mine)
            self._cycles.pop(rec.id, None)
            await self.check_unprotected()
            return "protected"
        if rec.ownership is Ownership.TAKEOVER:
            prices = await self._prices(rec.symbol)
            if prices is None:
                return await self._attempt_failed(rec, cycle, "读不到现价，暂不挂单")
            users = [a for a in algos if not _is_system(a.client_algo_id)]
            if self.external_protection_ok(rec, users, *prices):
                await self._set_external(rec)
                self._cycles.pop(rec.id, None)
                await self.check_unprotected()
                return "external"
        return None

    async def _attempt(self, position_id: int, cycle: _Cycle) -> str:
        """算止损价并提交 STOP_PLACE，等意图出结果（最多 wait_s 真实秒）。"""
        rec = self._registry.by_id(position_id)
        if rec is None:
            self._cycles.pop(position_id, None)
            return "closed"
        if rec.status not in _REGISTERED:
            self._cycles.pop(position_id, None)
            return "skipped"
        cycle.next_due_ms = None
        try:
            trigger, working_type, note = await self._plan_stop(rec, cycle)
        except _PlanFailed as exc:
            return await self._attempt_failed(rec, cycle, str(exc))
        except Exception as exc:  # 意外错误（K 线数据异常等）同样算一次失败的尝试：计入重试，窗口用完照样下结论
            log.exception("计算止损价出错：持仓 %s", rec.id)
            return await self._attempt_failed(rec, cycle, f"计算止损价出错：{type(exc).__name__}：{exc}")
        current = self._registry.by_id(position_id)  # 算止损价要等（读现价）：提交前再看一眼，期间可能已平或被取消托管
        if current is None or current.status not in _REGISTERED:
            self._cycles.pop(position_id, None)
            return "closed" if current is None else "skipped"
        rec = current
        if not cycle.persistent and rec.protection_state is not ProtectionState.REBUILDING:
            await self._set_state(rec, ProtectionState.REBUILDING)  # 结论之后的持续重试保持「无保护」，不来回翻
        spec = IntentSpec(
            action=IntentAction.STOP_PLACE, model="op", source=IntentSource.PROTECTION, symbol=rec.symbol,
            side=rec.side, idem_key=self._key(rec, "stop"), reason=f"protect:{cycle.kind}", ttl_s=None,
            params={"trigger_price": _fmt(trigger), "working_type": working_type.value},
            position_id=rec.id, position_generation=rec.generation)
        try:
            submitted = await self._coordinator.submit(spec)
        except Exception as exc:  # 协调者不受理（已停止）或账本写失败：这一次没发出去，按失败处理，之后重试
            log.exception("提交止损意图失败：持仓 %s", rec.id)
            return await self._attempt_failed(rec, cycle, f"提交止损意图失败：{type(exc).__name__}：{exc}")
        iid = submitted.intent_id
        self._tracked[iid] = _Tracked("place", rec.id, self._now(), trigger=trigger, note=note)
        cycle.pending = iid
        return await self._await_intent(iid) or "rebuilding"

    async def _await_intent(self, iid: int) -> str | None:
        await self._coordinator.wait_terminal(iid, self._wait_seconds())
        return await self._finish_locked(iid)

    async def _plan_stop(self, rec: PositionRecord, cycle: _Cycle) -> tuple[Decimal, WorkingType, str | None]:
        """这一次要挂的止损价、触发价类型、要写进 stop_note 的备注（None = 不改备注）。"""
        working_type = self._working_type()
        inst = self._rules.get(rec.symbol)
        if inst is None:
            raise _PlanFailed(f"没有 {rec.symbol} 的合约规则（价格步长），算不出止损价")
        if rec.stop_price is not None and not cycle.recompute:
            raw, note = rec.stop_price, None  # 按登记的止损价补挂
        else:
            raw, note = await self._fresh_stop(rec, working_type)
        if cycle.redefined:
            note = NOTE_REDEFINED if not note else f"{NOTE_REDEFINED}；{note}"
        try:
            trigger = round_stop_price(raw, inst.tick_size, rec.side)
        except ValueError as exc:
            raise _PlanFailed(f"止损价 {raw} 取整失败：{exc}") from None
        return trigger, working_type, note

    async def _fresh_stop(self, rec: PositionRecord, working_type: WorkingType) -> tuple[Decimal, str]:
        """F2 第 1 步：极值 ± ATR 再夹在 A%–B% 之间；K 线不足按 A% 设置。"""
        if rec.ownership is Ownership.TAKEOVER:
            prices = await self._prices(rec.symbol)
            if prices is None:
                raise _PlanFailed(f"读不到 {rec.symbol} 的现价，接管仓算不出基准价")
            base = prices[0] if working_type is WorkingType.CONTRACT_PRICE else prices[1]
        else:
            base = rec.avg_price
        a_pct, b_pct = self._cfg("init_stop_min_pct"), self._cfg("init_stop_max_pct")
        bars = self._candles.frame(rec.symbol, str(self._cfg("timeframe_short")))
        try:
            calc = initial_stop(bars, rec.side, base, a_pct, b_pct)
            if calc is not None:
                return calc.stop, ""
            pct = Decimal(str(a_pct)) / 100
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise _PlanFailed(f"初始止损的 A、B 或基准价无效，算不出止损价：{exc}") from None
        raw = base * (1 - pct) if rec.side is PositionSide.LONG else base * (1 + pct)
        return raw, NOTE_NO_ATR

    # ---------------------------------------------------------------- 失败处置

    async def _attempt_failed(self, rec: PositionRecord, cycle: _Cycle, reason: str, *, immediate: bool = False) -> str:
        cycle.last_error = reason
        now = self._now()
        if immediate:
            if self._system_owned(rec):
                return await self._close_protectively(
                    rec, cycle, why=f"止损建单被拒（会立即触发，-2021）：{reason}", cooldown=True)
            cycle.redefined += 1
            if cycle.redefined <= self._retries() and now - cycle.started_ms <= self._window_ms():
                cycle.recompute = True  # 以当刻价格为基准重算，期间保持「重建中」
                return await self._attempt(rec.id, cycle)
            return await self._still_failing(rec, cycle, f"止损按现价重算 {cycle.redefined - 1} 次后仍会立即触发")
        if (not cycle.persistent and cycle.retries_used < self._retries()
                and (cycle.deadline_ms is None or now < cycle.deadline_ms)):
            cycle.retries_used += 1
            due = cycle.started_ms + cycle.retries_used * self._window_ms() // (self._retries() + 1)  # 最后一次在窗口之内
            cycle.next_due_ms = due if cycle.deadline_ms is None else min(due, cycle.deadline_ms)
            if rec.protection_state is not ProtectionState.REBUILDING:
                await self._set_state(rec, ProtectionState.REBUILDING)
            return "rebuilding"
        return await self._still_failing(rec, cycle, reason)

    async def _still_failing(self, rec: PositionRecord, cycle: _Cycle, why: str) -> str:
        """F2 第 4 步「仍失败」：策略仓市价平掉并冷却；接管仓不自动平，无保护、置抑制、最高级告警，持续重试。"""
        cycle.persistent = True
        cycle.next_due_ms = self._now() + self._interval_ms()
        if self._system_owned(rec):
            return await self._close_protectively(rec, cycle, why=f"止损挂不上：{why}", cooldown=True)
        await self._set_state(rec, ProtectionState.NONE, note=f"无保护：{why}")
        self._alert(AlertLevel.CRITICAL, "protect_unprotected",
                    f"{rec.symbol} {rec.side.value} 的止损挂不上（{why}），该仓现在没有保护，系统会持续重试；"
                    f"请选择手填止损价、平仓或取消托管", key=f"protect_unprotected:{rec.id}")
        await self.check_unprotected()
        return "unprotected"

    async def _close_protectively(self, rec: PositionRecord, cycle: _Cycle, *, why: str, cooldown: bool) -> str:
        """保护性平仓（F12）：市价平掉整个仓，一直重试到平掉；策略仓还要冷却该合约。"""
        if cycle.close_pending is not None:
            return "closing"
        if cooldown:
            await self._cool_down(rec, why)
        await self._set_state(rec, ProtectionState.NONE, note=f"无保护：{why}，已发起保护性市价平仓")
        self._alert(AlertLevel.WARNING, "protect_failed_closing",
                    f"{rec.symbol} {rec.side.value} 的止损挂不上（{why}），已对该仓发起保护性市价平仓",
                    key=f"protect_failed_closing:{rec.id}")
        spec = IntentSpec(
            action=IntentAction.CLOSE, model="op", source=IntentSource.PROTECTION, symbol=rec.symbol, side=rec.side,
            idem_key=self._key(rec, "close"), reason=f"protective: {why}", ttl_s=None,
            params={"retry_until_done": True}, position_id=rec.id, position_generation=rec.generation)
        try:
            submitted = await self._coordinator.submit(spec)
        except Exception as exc:
            log.exception("提交保护性平仓意图失败：持仓 %s", rec.id)
            cycle.next_due_ms = self._now() + self._interval_ms()
            self._alert(AlertLevel.CRITICAL, "protective_close_failed",
                        f"{rec.symbol} {rec.side.value} 的保护性市价平仓没能提交（{type(exc).__name__}：{exc}），该仓仍没有保护，系统会持续重试",
                        key=f"protective_close_failed:{rec.id}")
            return "unprotected"
        self._tracked[submitted.intent_id] = _Tracked("close", rec.id, self._now())
        cycle.close_pending = submitted.intent_id
        cycle.close_alerted = False
        await self.check_unprotected()
        return await self._await_intent(submitted.intent_id) or "closing"

    async def _cool_down(self, rec: PositionRecord, why: str) -> None:
        bars = int(self._cfg("cooldown_bars"))
        if bars <= 0:
            return
        until = self._now() + bars * interval_ms(str(self._cfg("timeframe_short")))
        await self._registry.set_cooldown(rec.symbol, until, f"保护性平仓：{why}")

    # ================================================================ 意图结果的处理（幂等：每个意图只处理一次）

    async def _finish_tracked(self, iid: int) -> None:
        ctx = self._tracked.get(iid)
        if ctx is None:
            return
        async with self._lock(ctx.position_id):
            await self._finish_locked(iid)

    async def _finish_locked(self, iid: int) -> str | None:
        """意图到了终态就处理（只处理一次），返回处理后的状态词；还没到终态返回 None。"""
        ctx = self._tracked.get(iid)
        if ctx is None or iid in self._done:
            return None
        try:
            irec = await self._coordinator.wait_terminal(iid, 0.0)
        except KeyError:
            self._tracked.pop(iid, None)
            return None
        if irec.state not in _TERMINAL_INTENTS:
            return None
        self._done[iid] = None
        while len(self._done) > _DONE_KEPT:
            self._done.popitem(last=False)
        del self._tracked[iid]
        if ctx.kind == "place":
            return await self._on_place_done(iid, ctx, irec)
        if ctx.kind == "replace":
            return await self._on_replace_done(ctx, irec)
        return await self._on_close_done(ctx, irec)

    async def _on_place_done(self, iid: int, ctx: _Tracked, irec: IntentRecord) -> str:
        pid = ctx.position_id
        cycle = self._cycles.get(pid)
        if cycle is not None and cycle.pending == iid:
            cycle.pending = None
        if irec.state is IntentState.COMPLETED:
            order = await self._stop_order(iid)
            return await self._stop_in_place(pid, ctx.trigger, order, ctx.note)
        rec = self._registry.by_id(pid)
        if rec is None:
            self._cycles.pop(pid, None)
            return "closed"
        if rec.status not in _REGISTERED:
            self._cycles.pop(pid, None)
            return "skipped"
        if cycle is None:
            cycle = self._cycles[pid] = _Cycle(kind="late", started_ms=self._now())
        immediate = irec.state_reason == "immediate_trigger"
        return await self._attempt_failed(rec, cycle, irec.state_reason or irec.state.value, immediate=immediate)

    async def _stop_in_place(self, pid: int, trigger: Decimal | None, order: OrderRecord | None,
                             note: str | None) -> str:
        cid = order.client_order_id if order is not None else None
        algo_id = order.algo_id if order is not None else None
        price = order.trigger_price if order is not None and order.trigger_price is not None else trigger
        rec = self._registry.by_id(pid)
        fields: dict[str, Any] = dict(state=ProtectionState.SYSTEM, stop_price=price, stop_client_id=cid,
                                      stop_algo_id=algo_id)
        await self._claim_previous(rec if rec is not None else await self._registry.fetch(pid), cid, algo_id)
        if rec is None:
            # 挂上时仓位已经归零：把编号记在已平记录上，让归零后的清理把它撤掉
            await self._registry.set_protection(pid, **fields)
            await self._cleanup_locked(pid)
            return "closed"
        if note is None:
            note = self._stale_note(rec)
        if note is not None:
            fields["note"] = note
        await self._registry.set_protection(pid, **fields)
        if rec.status is PositionStatus.TAKING_OVER:
            await self._registry.mark_managed(pid)
        cycle = self._cycles.pop(pid, None)
        self._placed_ms[pid] = self._now()
        if cycle is not None and cycle.persistent:
            self._alert(AlertLevel.INFO, "protect_restored", f"{rec.symbol} {rec.side.value} 的止损已重新挂上，保护恢复")
        await self.check_unprotected()
        return "protected"

    async def _on_close_done(self, ctx: _Tracked, irec: IntentRecord) -> str:
        pid = ctx.position_id
        cycle = self._cycles.get(pid)
        if cycle is not None and cycle.close_pending is not None:
            cycle.close_pending = None
        rec = self._registry.by_id(pid)
        if irec.state is IntentState.COMPLETED:
            if cycle is not None and rec is not None:
                cycle.close_done_ms = self._now()  # 平仓单完成，等成交入账、仓位归零
            return "closing" if rec is not None else "closed"
        if rec is None:
            return "closed"
        if rec.status not in _REGISTERED:
            self._cycles.pop(pid, None)
            return "skipped"
        why = irec.state_reason or irec.state.value
        if cycle is None:
            cycle = self._cycles[pid] = _Cycle(kind="close", started_ms=self._now(), persistent=True)
        cycle.next_due_ms = self._now() + self._interval_ms()
        await self._set_state(rec, ProtectionState.NONE, note=f"无保护：保护性平仓失败（{why}）")
        self._alert(AlertLevel.CRITICAL, "protective_close_failed",
                    f"{rec.symbol} {rec.side.value} 的保护性市价平仓失败（{why}），该仓仍没有保护，系统会持续重试",
                    key=f"protective_close_failed:{rec.id}")
        await self.check_unprotected()
        return "unprotected"

    async def _on_replace_done(self, ctx: _Tracked, irec: IntentRecord) -> str:
        pid = ctx.position_id
        raw = irec.params.get(RESULT_KEY) if isinstance(irec.params, dict) else None
        result: dict[str, Any] = raw if isinstance(raw, dict) else {}
        outcome = result.get("outcome")
        rec = self._registry.by_id(pid)
        now = self._now()
        in_place = result.get("stop_in_place")
        stop_on_exchange = outcome in ("replaced", "restored_old") or (outcome == "gap_exceeded" and in_place is True)
        if stop_on_exchange:
            old = irec.params.get("old") if isinstance(irec.params, dict) else None
            await self._record_replaced(pid, result, old if isinstance(old, dict) else {})
            self._placed_ms[pid] = now
            if outcome == "gap_exceeded":
                self._alert(AlertLevel.WARNING, "stop_gap_exceeded",
                            f"移动止损的无保护间隙超过上限（{result.get('gap_ms')} 毫秒），新止损已在位", key=f"stop_gap:{pid}")
            if ctx.hand and rec is not None:
                for extra in ctx.extra_old:
                    await self._cancel_stop(rec, extra.get("client_algo_id"), extra.get("algo_id"))
            if rec is None:
                await self._cleanup_locked(pid)  # 新止损挂在了已归零的仓上：由归零后的清理撤掉
                return "closed"
            self._cycles.pop(pid, None)
            await self.check_unprotected()
            return "protected"
        if rec is None:
            return "closed"
        if rec.status not in _REGISTERED:
            self._cycles.pop(pid, None)
            return "skipped"
        if outcome in (None, "kept_old"):
            return "protected" if rec.protection_state is ProtectionState.SYSTEM else self._status_of(rec)
        old = irec.params.get("old") if isinstance(irec.params, dict) else None
        if self._newer_stop(rec, old if isinstance(old, dict) else {}):
            # 这次更新失败了，但登记簿里已是之后另挂上的一张（例如超过空窗上限后的补挂）：失败结论只关于被撤掉的旧单，
            # 不再把在位的止损记回「无保护」、不再平仓
            return "protected"
        if outcome == "stop_crossed":
            await self._registry.set_protection(rec.id, state=ProtectionState.NONE, clear_stop=True,
                                                note="无保护：移动止损时旧止损价也已被现价越过，视为止损已触发")
            cycle = self._cycles[pid] = _Cycle(kind="stop_crossed", started_ms=now, persistent=True)
            self._alert(AlertLevel.WARNING, "stop_crossed_closing",
                        f"{rec.symbol} {rec.side.value} 移动止损时新旧止损价都已被现价越过（-2021），视为止损已触发，市价平仓",
                        key=f"stop_crossed:{pid}")
            fresh = self._registry.by_id(pid) or rec
            return await self._close_protectively(fresh, cycle, why="移动止损时止损价已被越过，视为止损已触发", cooldown=False)
        # unprotected / gap_exceeded 且没有止损在位：按 F2 第 4 步处置
        why = irec.state_reason or f"移动止损失败（{outcome}）"
        cycle = self._cycles.get(pid)
        if cycle is None:
            cycle = self._cycles[pid] = _Cycle(kind="replace_failed", started_ms=now)
            if ctx.mode != "make_before_break":  # gap 模式：从撤旧单起超过 stop_gap_max_s 仍无保护就不再重试一轮
                cycle.deadline_ms = ctx.started_ms + int(self._cfg("stop_gap_max_s")) * 1000
        if outcome == "gap_exceeded":
            self._alert(AlertLevel.WARNING, "stop_gap_exceeded",
                        f"移动止损的无保护间隙超过上限（{result.get('gap_ms')} 毫秒）且没有止损在位", key=f"stop_gap:{pid}")
            return await self._still_failing(rec, cycle, why)
        return await self._attempt_failed(rec, cycle, why)

    @staticmethod
    def _newer_stop(rec: PositionRecord, old: dict[str, Any]) -> bool:
        """登记簿认为在位（系统保护）的止损不是这次更新要换掉的那张旧单：说明之后已经另挂上了一张。"""
        if rec.protection_state is not ProtectionState.SYSTEM or not (rec.stop_client_id or rec.stop_algo_id):
            return False
        cid, algo_id = old.get("client_algo_id"), old.get("algo_id")
        return not ((cid and cid == rec.stop_client_id) or (algo_id and algo_id == rec.stop_algo_id))

    def _status_of(self, rec: PositionRecord) -> str:
        cycle = self._cycles.get(rec.id)
        if cycle is not None:
            return self._cycle_status(cycle)
        return {ProtectionState.EXTERNAL: "external", ProtectionState.NONE: "unprotected"}.get(
            rec.protection_state, "rebuilding")

    async def _record_replaced(self, pid: int, result: dict[str, Any], old: dict[str, Any]) -> None:
        try:
            price = Decimal(str(result["trigger_price"]))
        except (KeyError, ArithmeticError, ValueError):
            price = None
        prev = self._registry.by_id(pid) or await self._registry.fetch(pid)
        await self._claim_previous(prev, result.get("client_algo_id"), result.get("algo_id"),
                                   gone=((old.get("client_algo_id"), old.get("algo_id")),))
        # 换成了新止损价：备注（A% 说明、外部保护……）不再描述它，清掉；旧价重挂（restored_old）止损价没变，只清过渡说明
        note = self._stale_note(self._registry.by_id(pid)) if result.get("outcome") == "restored_old" else ""
        await self._registry.set_protection(
            pid, state=ProtectionState.SYSTEM, stop_price=price, stop_client_id=result.get("client_algo_id"),
            stop_algo_id=result.get("algo_id"), note=note)
        for old in result.get("cleanup") or []:
            cid, algo_id = old.get("client_algo_id"), old.get("algo_id")
            if cid or algo_id:
                await self._registry.add_stale_stop(pid, client_algo_id=cid, algo_id=algo_id)

    async def _poll_tracked(self, iid: int) -> None:
        ctx = self._tracked.get(iid)
        if ctx is None:
            return
        async with self._lock(ctx.position_id):
            status = await self._finish_locked(iid)
            if status is not None or ctx.kind != "replace" or ctx.gap_judged or iid not in self._tracked:
                return
            if ctx.mode == "make_before_break":
                return  # 先建后撤：新止损确认存在之前旧止损一直在，没有空窗，只等意图的结论
            limit_ms = int(self._cfg("stop_gap_max_s")) * 1000
            if self._now() - ctx.started_ms <= limit_ms:
                return
            ctx.gap_judged = True  # 超过上限仍不能确认新止损存在：按「仍失败」处置，不再重试一轮
            rec = self._registry.by_id(ctx.position_id)
            if rec is None or rec.status not in _REGISTERED:
                return
            cycle = self._cycles.get(rec.id) or _Cycle(kind="gap_timeout", started_ms=self._now())
            self._cycles[rec.id] = cycle
            self._alert(AlertLevel.WARNING, "stop_gap_exceeded",
                        f"{rec.symbol} {rec.side.value} 移动止损从撤旧单起已超过 {limit_ms} 毫秒仍不能确认新止损存在",
                        key=f"stop_gap:{rec.id}")
            await self._still_failing(rec, cycle, f"移动止损超过 {limit_ms} 毫秒仍不能确认新止损存在")

    async def _stop_order(self, intent_id: int) -> OrderRecord | None:
        rows = await self._ledger.read(repo_orders.for_intent, intent_id)
        stops = [o for o in rows if o.purpose is OrderPurpose.STOP]
        live = [o for o in stops if o.state not in (OrderState.REJECTED, OrderState.CANCELLED)]
        pick = live or stops
        return pick[-1] if pick else None

    # ================================================================ 交易所读数

    async def _open_algos(self, symbol: str) -> list[AlgoOrderInfo] | None:
        result = await self._adapter.fetch_open_algo_orders(symbol)
        if isinstance(result, Unknown):
            return None
        return [a for a in result.value if a.symbol == symbol]

    async def _prices(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """（最新价, 标记价）；读不到返回 None。

        行情读被限频拒绝或在限频退避中（``code == -1003``；行情是非保护类读，429/418 后要退避到下一分钟甚至 120 秒）时，
        改读持仓（保护类读，不受退避）里的标记价，两项都取它：挂止损不能等限频过去（报告 4.11：止损挂得上时 30 秒内存在）。"""
        result = await self._adapter.fetch_tickers([symbol])
        if isinstance(result, Unknown):
            return await self._mark_from_positions(symbol) if result.code == _RATE_LIMITED_CODE else None
        ticker = result.value.get(symbol)
        if ticker is None or ticker.last <= 0 or ticker.mark <= 0:
            return None
        return ticker.last, ticker.mark

    async def _mark_from_positions(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        result = await self._adapter.fetch_positions()
        if isinstance(result, Unknown):
            return None
        marks = [p.mark_price for p in result.value if p.symbol == symbol and p.mark_price is not None and p.mark_price > 0]
        if not marks:
            return None
        log.info("%s 行情读被限频，改用持仓里的标记价 %s 作为现价", symbol, marks[0])
        return marks[0], marks[0]

    @staticmethod
    def _system_stops(rec: PositionRecord, algos: Iterable[AlgoOrderInfo]) -> list[AlgoOrderInfo]:
        """交易所上这个键的系统止损（带系统前缀、止损类、全平型、未结）。登记的那张排在最前。"""
        found = [a for a in algos if a.symbol == rec.symbol and a.position_side is rec.side and a.is_open
                 and a.algo_type.is_stop_kind and a.close_position and _is_system(a.client_algo_id)]
        found.sort(key=lambda a: 0 if (a.client_algo_id == rec.stop_client_id or a.algo_id == rec.stop_algo_id) else 1)
        return found

    async def _claim_previous(self, rec: PositionRecord | None, new_cid: str | None, new_algo_id: str | None, *,
                              gone: Iterable[tuple[str | None, str | None]] = ()) -> None:
        """改登记的当前止损之前：登记簿认为在位（系统保护）的那张既不是新单、也不是这次已经换掉的旧单，说明它是别的路径
        （例如超过空窗上限后的补挂）挂上的另一张系统止损——记为待清理的旧单，由对账或归零后的清理撤掉，不留下谁也不认的止损。"""
        if rec is None or rec.protection_state is not ProtectionState.SYSTEM:
            return
        cur_cid, cur_algo = rec.stop_client_id, rec.stop_algo_id
        if not (cur_cid or cur_algo):
            return

        def same(cid: str | None, algo_id: str | None) -> bool:
            return bool((cid and cid == cur_cid) or (algo_id and algo_id == cur_algo))

        if same(new_cid, new_algo_id) or any(same(cid, algo_id) for cid, algo_id in gone):
            return
        await self._registry.add_stale_stop(rec.id, client_algo_id=cur_cid, algo_id=cur_algo)

    @staticmethod
    def _is_my_stop(algo: AlgoOrderInfo, rec: PositionRecord) -> bool:
        if algo.symbol != rec.symbol or algo.position_side is not rec.side:
            return False
        return bool((rec.stop_client_id and algo.client_algo_id == rec.stop_client_id)
                    or (rec.stop_algo_id and algo.algo_id == rec.stop_algo_id))

    # ================================================================ 登记簿写入的小封装

    async def _set_state(self, rec: PositionRecord, state: ProtectionState, *, note: str | None = None) -> None:
        if rec.protection_state is state and (note is None or rec.stop_note == note):
            return
        await self._registry.set_protection(rec.id, state=state, note=note)

    async def _adopt(self, rec: PositionRecord, found: list[AlgoOrderInfo]) -> None:
        """认领交易所上已有的系统止损（登记的那张优先）；多出来的记为待清理的旧单。"""
        keep, extras = found[0], found[1:]
        await self._registry.set_protection(
            rec.id, state=ProtectionState.SYSTEM, stop_price=keep.trigger_price, stop_client_id=keep.client_algo_id,
            stop_algo_id=keep.algo_id, note=self._stale_note(rec))
        for extra in extras:
            await self._registry.add_stale_stop(rec.id, client_algo_id=extra.client_algo_id, algo_id=extra.algo_id)
        if rec.status is PositionStatus.TAKING_OVER:
            await self._registry.mark_managed(rec.id)
        self._placed_ms[rec.id] = self._now()

    async def _set_external(self, rec: PositionRecord) -> None:
        await self._registry.set_protection(rec.id, state=ProtectionState.EXTERNAL, clear_stop=True, note=NOTE_EXTERNAL)
        if rec.status is PositionStatus.TAKING_OVER:
            await self._registry.mark_managed(rec.id)

    # ================================================================ 外部保护（F3 第 6 步、F13）

    def external_protection_ok(self, position: PositionRecord, algo_orders: Iterable[AlgoOrderInfo], last: Decimal,
                               mark: Decimal) -> bool:
        """这些条件单里是否有一张满足四条：止损类；持仓方向一致；全平型或数量满足 ``ex_qty_stop_protect_rule``；
        触发方向正确（多仓触发价低于现价、空仓高于现价，现价取这张单自身的触发价类型对应的价格）。只看未结的单。
        传进来的应该是用户自己的条件单（系统自己的止损不是外部保护）。"""
        return any(self._covers(position, order, last, mark) for order in algo_orders)

    def _covers(self, position: PositionRecord, order: AlgoOrderInfo, last: Decimal, mark: Decimal) -> bool:
        if order.symbol != position.symbol or order.position_side is not position.side or not order.is_open:
            return False
        if not order.algo_type.is_stop_kind:
            return False
        if not order.close_position:
            if order.qty is None:
                return False
            rule = self._cfg("ex_qty_stop_protect_rule")
            if not (order.qty >= position.qty if rule == "ge" else order.qty == position.qty):
                return False
        price = last if order.working_type is WorkingType.CONTRACT_PRICE else mark
        if position.side is PositionSide.LONG:
            return order.trigger_price < price
        return order.trigger_price > price

    async def _recheck_external(self, rec: PositionRecord, algos: Iterable[AlgoOrderInfo] | None = None) -> str:
        """外部保护每次条件单快照都复核；读不到数据就不动；失效即告警并转入 F2。"""
        if self._in_flight(rec.id):
            return "external"  # 交给系统管理止损还没有结论（用户的止损可能已按 gap 模式撤掉）：等它的结论，不另挂
        if algos is None:
            algos = await self._open_algos(rec.symbol)
            if algos is None:
                return "external"
        prices = await self._prices(rec.symbol)
        if prices is None:
            return "external"
        users = [a for a in algos if a.symbol == rec.symbol and not _is_system(a.client_algo_id)]
        if self.external_protection_ok(rec, users, *prices):
            return "external"
        self._alert(AlertLevel.WARNING, "external_protection_lost",
                    f"{rec.symbol} {rec.side.value} 用户的止损已失效（被撤销，或不再满足外部保护的四条标准），系统转入自己挂止损",
                    key=f"external_lost:{rec.id}")
        cycle = self._cycles[rec.id] = _Cycle(kind="external_lost", started_ms=self._now())
        await self._set_state(rec, ProtectionState.REBUILDING, note="用户的止损失效，系统另挂止损")
        done = await self._pre_read(rec, cycle)
        return done if done is not None else await self._attempt(rec.id, cycle)

    async def hand_stop_to_system(self, position_id: int) -> str:
        """F13「交给系统管理止损」：只对保护状态是「外部保护」的接管仓。系统挂上自己的止损、撤掉用户的止损，
        先后顺序同 F2 第 7 步（``ex_stop_update_mode``：make_before_break 先挂后撤，gap 先撤后挂），保护状态转「系统保护」并开始移动。
        返回 ``protected`` | ``closing`` | ``unprotected`` | ``rebuilding`` | ``skipped``（不满足条件）| ``unknown``（读不到交易所）。"""
        self._check_alive()
        async with self._lock(position_id):
            return await self._run(self._hand_locked(position_id))

    async def _hand_locked(self, position_id: int) -> str:
        rec = self._registry.by_id(position_id)
        if (rec is None or rec.ownership is not Ownership.TAKEOVER or rec.status is not PositionStatus.MANAGED
                or rec.protection_state is not ProtectionState.EXTERNAL):
            return "skipped"
        algos = await self._open_algos(rec.symbol)
        prices = await self._prices(rec.symbol)
        if algos is None or prices is None:
            return "unknown"
        users = [a for a in algos if not _is_system(a.client_algo_id) and self._covers(rec, a, *prices)]
        if not users:
            return await self._recheck_external(rec, algos)
        cycle = _Cycle(kind="hand_over", started_ms=self._now())
        try:
            trigger, working_type, _ = await self._plan_stop(rec, cycle)
        except _PlanFailed:
            return "unknown"
        users.sort(key=lambda a: not a.close_position)  # 优先把用户的全平止损当旧单（不允许并存两张全平止损的交易所上，先撤数量型的会误撤）
        first, rest = users[0], users[1:]
        old = {"client_algo_id": first.client_algo_id, "algo_id": first.algo_id, "trigger_price": _fmt(first.trigger_price)}
        extra = [{"client_algo_id": a.client_algo_id, "algo_id": a.algo_id} for a in rest]
        return await self._submit_replace(rec, trigger, working_type, old=old, hand=True, extra_old=extra) or "rebuilding"

    # ================================================================ 取消托管（F13）

    async def cancel_takeover(self, position_id: int, *, mode: str,
                              finalize: Callable[[], Coroutine[Any, Any, Any]], wait_s: float | None = None) -> str:
        """F13「取消托管」与保护单管理串行：先等本管理在途的挂止损/换止损出结论（``wait_s`` 真实秒，缺省同保护目标时限；
        等不到返回 ``busy``，什么也没做），然后在持仓锁里完成，期间不会再有挂单或换单落到这个仓上：

        - ``mode="handover"``：保留当前系统止损，登记为「已移交」（仓位非零时系统不再移动它、对账不当孤儿撤；归零后由清理撤销）；
        - ``mode="cancel_stop"``：先经协调者撤掉系统止损（含待清理的旧单）并确认，撤不掉返回 ``stop_cancel_failed``，状态不改。

        止损处理完后调用 ``finalize()``（调用方把登记簿改成外部（已忽略）），再清掉本管理里这个仓的周期状态。用户自己的止损（外部保护）不动。
        返回 ``done`` | ``busy`` | ``stop_cancel_failed`` | ``closed``（仓已不在登记簿）| ``skipped``（不是接管仓或混合仓，或平仓中）。"""
        if mode not in ("handover", "cancel_stop"):
            raise ValueError(f"取消托管的方式只能是 handover 或 cancel_stop：{mode!r}")
        self._check_alive()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self._wait_seconds() if wait_s is None else wait_s)
        while True:
            async with self._lock(position_id):
                if not self._in_flight(position_id):
                    return await self._run(self._cancel_takeover_locked(position_id, mode, finalize))
            if loop.time() >= deadline:
                return "busy"
            await asyncio.sleep(0.02)

    async def _cancel_takeover_locked(self, position_id: int, mode: str,
                                      finalize: Callable[[], Coroutine[Any, Any, Any]]) -> str:
        rec = self._registry.by_id(position_id)
        if rec is None:
            return "closed"
        if rec.status not in (PositionStatus.MANAGED, PositionStatus.TAKING_OVER) or not (
                rec.ownership is Ownership.TAKEOVER or rec.is_mixed):
            return "skipped"
        has_stop = bool(rec.stop_client_id or rec.stop_algo_id) and rec.protection_state is not ProtectionState.EXTERNAL
        if mode == "handover":
            if has_stop:
                await self._registry.set_handed_over(
                    rec.id, {"client_algo_id": rec.stop_client_id, "algo_id": rec.stop_algo_id})
        else:
            # 先撤待清理的旧单、最后撤当前止损：中途撤不掉时，仍是托管中的仓不会先没了当前止损
            for entry in list(rec.stale_stops):
                cid, algo_id = entry.get("client_algo_id"), entry.get("algo_id")
                if not await self._cancel_stop(rec, cid, algo_id):
                    return "stop_cancel_failed"
                await self._registry.remove_stale_stop(rec.id, client_algo_id=cid, algo_id=algo_id)
            if has_stop and not await self._cancel_stop(rec, rec.stop_client_id, rec.stop_algo_id):
                return "stop_cancel_failed"
        await finalize()
        if mode == "cancel_stop" and has_stop:
            await self._registry.set_protection(rec.id, state=ProtectionState.NONE, clear_stop=True)
        self._cycles.pop(position_id, None)
        self._deferred.discard(position_id)
        self._placed_ms.pop(position_id, None)
        return "done"

    # ================================================================ 移动止损（F2 第 6、7 步）

    async def trailing(self, bar_open_ms: int) -> None:
        """一根短周期 K 线收盘并已取到（``bar_open_ms`` 是它的开盘时间）：对每个托管仓算是否满了新的 6 根。"""
        self._check_alive()
        recs = [r.id for r in self._registry.registered()]
        if not recs:
            return
        results = await asyncio.gather(*(self._trail_task(pid, bar_open_ms) for pid in recs), return_exceptions=True)
        for item in results:
            if isinstance(item, CrashSignal):
                self._die(item)
                raise item
            if isinstance(item, BaseException) and not isinstance(item, asyncio.CancelledError):
                log.error("移动止损出错", exc_info=item)

    async def _trail_task(self, position_id: int, bar_open_ms: int | None) -> None:
        async with self._lock(position_id):
            await self._trail_locked(position_id, bar_open_ms)

    async def _trail_locked(self, position_id: int, bar_open_ms: int | None) -> None:
        rec = self._registry.by_id(position_id)
        if rec is None:
            return
        if rec.status is PositionStatus.CLOSING:
            self._deferred.add(position_id)  # 平仓中的仓顺延到恢复后补做
            return
        if rec.status is not PositionStatus.MANAGED or rec.protection_state is not ProtectionState.SYSTEM:
            return
        if rec.stop_price is None or not (rec.stop_client_id or rec.stop_algo_id) or rec.anchor_ms is None:
            return
        if any(c.kind == "replace" and c.position_id == position_id for c in self._tracked.values()):
            return  # 上一次更新还在途
        inst = self._rules.get(rec.symbol)
        if inst is None:
            return
        frame = self._candles.frame(rec.symbol, str(self._cfg("timeframe_short")))
        if bar_open_ms is not None:
            frame = frame[frame["open_time_ms"] <= bar_open_ms].reset_index(drop=True)
        side, anchor, first_cycle = rec.side, rec.anchor_ms, rec.stop_cycle
        working_type = self._working_type()
        best: Decimal = rec.stop_price
        adopted: Decimal | None = None
        scanned = 0
        price: Decimal | None = None
        while scanned < MAX_CATCH_UP and window6(frame, anchor, first_cycle + scanned) is not None:
            calc = stop_calc(frame, anchor, side, first_cycle + scanned)
            scanned += 1
            if calc is None:
                continue
            try:
                candidate = round_stop_price(calc.stop, inst.tick_size, side)
            except ValueError:
                continue
            if not is_better(side, best, candidate):
                continue
            if price is None:
                prices = await self._prices(rec.symbol)
                if prices is None:
                    return  # 读不到现价：既不能采纳也不能判定被越过，这一轮不决定、不前进
                price = prices[0] if working_type is WorkingType.CONTRACT_PRICE else prices[1]
            if (candidate >= price) if side is PositionSide.LONG else (candidate <= price):
                continue  # 已被现价越过：不采纳，旧止损保留
            best = adopted = candidate
        if scanned == 0:
            return
        if adopted is not None and self._limits is not None and not self._limits.order_rate_ok(rec.symbol, self._now()):
            return  # 同一合约订单频率到上限：顺延，周期计数不前进
        await self._registry.set_protection(rec.id, state=rec.protection_state, stop_cycle=first_cycle + scanned)
        if adopted is not None:
            await self._submit_replace(rec, adopted, working_type)

    async def _submit_replace(self, rec: PositionRecord, new_trigger: Decimal, working_type: WorkingType, *,
                              old: dict[str, Any] | None = None, hand: bool = False,
                              extra_old: list[dict[str, Any]] | None = None) -> str | None:
        if old is None:
            old = {"client_algo_id": rec.stop_client_id, "algo_id": rec.stop_algo_id,
                   "trigger_price": None if rec.stop_price is None else _fmt(rec.stop_price)}
        mode = str(self._cfg("ex_stop_update_mode"))
        spec = IntentSpec(
            action=IntentAction.STOP_REPLACE, model="op", source=IntentSource.PROTECTION, symbol=rec.symbol,
            side=rec.side, idem_key=self._key(rec, "replace"), reason="protect:hand_over" if hand else "protect:trailing",
            ttl_s=None, position_id=rec.id, position_generation=rec.generation,
            params={"old": old, "trigger_price": _fmt(new_trigger), "working_type": working_type.value, "mode": mode})
        submitted = await self._coordinator.submit(spec)
        self._tracked[submitted.intent_id] = _Tracked(
            "replace", rec.id, self._now(), trigger=new_trigger, hand=hand, extra_old=list(extra_old or []), mode=mode)
        return await self._await_intent(submitted.intent_id)

    async def _catch_up(self, position_id: int) -> None:
        """恢复后补做：先确认保护，再补算顺延的移动止损。"""
        async with self._lock(position_id):
            rec = self._registry.by_id(position_id)
            if rec is None:
                return
            if rec.protection_state in (ProtectionState.NONE, ProtectionState.REBUILDING) and rec.id not in self._cycles:
                await self._ensure_locked(position_id, "resume")
            await self._trail_locked(position_id, None)

    # ================================================================ 归零后清理

    async def cleanup_after_close(self, position_id: int) -> None:
        """仓位归零后撤掉这个仓的全部系统保护单：当前止损、待清理的旧单、已移交的单。崩溃点 ``close_filled_before_cleanup``
        在清理之前。撤成功（已撤或交易所说不存在）才从登记簿清掉对应编号；撤不成的留着，下次再来。幂等。"""
        self._check_alive()
        async with self._lock(position_id):
            await self._run(self._cleanup_locked(position_id))

    async def _cleanup_locked(self, position_id: int) -> None:
        if self._registry.by_id(position_id) is not None:
            log.warning("持仓 %s 仍是活动持仓，不清理它的止损", position_id)  # 绝不撤还有仓位的止损
            return
        self._crash.hit("close_filled_before_cleanup")  # 崩溃点 5：平仓单已成交，保护单未清理
        rec = await self._registry.fetch(position_id)
        if rec is None:
            return
        self._cycles.pop(position_id, None)
        self._deferred.discard(position_id)
        self._placed_ms.pop(position_id, None)
        if rec.stop_client_id or rec.stop_algo_id:
            if await self._cancel_stop(rec, rec.stop_client_id, rec.stop_algo_id):
                await self._registry.set_protection(rec.id, state=ProtectionState.NONE, clear_stop=True)
        for entry in list(rec.stale_stops):
            cid, algo_id = entry.get("client_algo_id"), entry.get("algo_id")
            if await self._cancel_stop(rec, cid, algo_id):
                await self._registry.remove_stale_stop(rec.id, client_algo_id=cid, algo_id=algo_id)
        handed = rec.handed_over_stop
        if handed:
            if await self._cancel_stop(rec, handed.get("client_algo_id"), handed.get("algo_id")):
                await self._registry.set_handed_over(rec.id, None)

    async def _cancel_stop(self, rec: PositionRecord, cid: str | None, algo_id: str | None) -> bool:
        """提交 STOP_CANCEL 并等结果：已撤或不存在算成功。"""
        if not cid and not algo_id:
            return True
        spec = IntentSpec(
            action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.PROTECTION, symbol=rec.symbol,
            side=rec.side, idem_key=self._key(rec, "cancel"), reason="protect:cleanup", ttl_s=None,
            params={"client_algo_id": cid, "algo_id": algo_id}, position_id=rec.id, position_generation=rec.generation)
        submitted = await self._coordinator.submit(spec)
        done = await self._coordinator.wait_terminal(submitted.intent_id, self._wait_seconds())
        return done.state is IntentState.COMPLETED

    # ================================================================ 手动撤掉、快照复核

    async def _stop_missing(self, position_id: int, cid: str | None, algo_id: str | None, source: str) -> None:
        async with self._lock(position_id):
            rec = self._registry.by_id(position_id)
            if rec is None or rec.status not in _REGISTERED or rec.protection_state is not ProtectionState.SYSTEM:
                return
            if (cid, algo_id) != (rec.stop_client_id, rec.stop_algo_id):
                return  # 这张已经不是登记的当前止损（换过单了）：与它无关
            if self._in_flight(rec.id):
                return  # 换止损在途（gap 模式撤旧单本来就会让旧单消失）、结果未知：等它的结论，不补挂（F8）
            await self._run(self._rebuild_locked(rec, f"stop_missing:{source}"))

    async def _check_snapshot(self, event: OrdersSnapshotTaken) -> None:
        algos = list(event.algo_orders)
        for rec in list(self._registry.registered()):
            if rec.protection_state is ProtectionState.SYSTEM and (rec.stop_client_id or rec.stop_algo_id):
                if self._placed_ms.get(rec.id, 0) > event.at_ms:
                    continue  # 快照取得比我们最近一次挂/换单还早：不作数
                if not any(self._is_my_stop(a, rec) and a.is_open for a in algos):
                    await self._stop_missing(rec.id, rec.stop_client_id, rec.stop_algo_id, "snapshot")
            elif rec.protection_state is ProtectionState.EXTERNAL:
                await self._recheck_external_locked(rec.id, algos)

    async def _recheck_external_locked(self, position_id: int, algos: list[AlgoOrderInfo] | None) -> None:
        async with self._lock(position_id):
            rec = self._registry.by_id(position_id)
            if rec is None or rec.status not in _REGISTERED or rec.protection_state is not ProtectionState.EXTERNAL:
                return
            await self._run(self._recheck_external(rec, algos))

    async def _recheck_external_by_id(self, position_id: int) -> None:
        await self._recheck_external_locked(position_id, None)  # 现读交易所的条件单

    # ================================================================ 事件订阅

    def _on_registered(self, event: PositionRegistered) -> None:
        if event.status in {s.value for s in _REGISTERED}:
            self._spawn(self.ensure_protected, event.position_id, reason="registered")

    def _on_changed(self, event: PositionChanged) -> None:
        rec = self._registry.by_id(event.position_id)
        if rec is None:
            return
        if event.cause == "takeover" and rec.status in _REGISTERED:
            self._spawn(self.ensure_protected, rec.id, reason="takeover")
        elif event.cause in _QTY_CHANGE_CAUSES and rec.protection_state is ProtectionState.EXTERNAL:
            self._spawn(self._recheck_external_by_id, rec.id)  # F4 第 7 步：数量变了，按 F3 第 6 步复核用户的止损
        if rec.status is PositionStatus.CLOSING:
            self._deferred.add(rec.id)
        elif rec.status is PositionStatus.MANAGED and rec.id in self._deferred:
            self._deferred.discard(rec.id)
            self._spawn(self._catch_up, rec.id)

    def _on_closed(self, event: PositionClosed) -> None:
        self._spawn(self.cleanup_after_close, event.position_id)
        self._spawn(self._after_closed, event.position_id)

    async def _after_closed(self, position_id: int) -> None:
        await self.check_unprotected()

    def _on_candles(self, event: CandlesReady) -> None:
        if event.timeframe == str(self._cfg("timeframe_short")):
            self._spawn(self.trailing, event.bar_open_ms)

    def _on_algo(self, event: AlgoOrderObserved) -> None:
        algo = event.algo
        if algo.is_open or algo.raw_status in _STOP_GONE_KEEP:
            return  # 还在挂着，或是触发了（成交会让持仓归零）：都不是被撤
        for rec in self._registry.registered():
            if rec.protection_state is ProtectionState.SYSTEM and self._is_my_stop(algo, rec):
                self._spawn(self._stop_missing, rec.id, rec.stop_client_id, rec.stop_algo_id, "stream")
                return

    def _on_snapshot(self, event: OrdersSnapshotTaken) -> None:
        self._spawn(self._check_snapshot, event)

    def _on_intent(self, event: IntentStateChanged) -> None:
        if event.intent_id in self._tracked and event.state in _TERMINAL_STATES:
            self._spawn(self._finish_tracked, event.intent_id)
