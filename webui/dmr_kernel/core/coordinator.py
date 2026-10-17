"""协调者（第 4 层）：交易意图的执行者，也是唯一持有 ``WriteCapability`` 的模块（应急入口除外）。

只**执行**交易意图；判断（要不要挂、挂多少、失败后怎么办）在上层。规则出处：计划报告 F1 第 5–9 步、F2 第 2–4、7 步、
F4 第 5–9 步、F5 第 5–7 步、F12；接口与测试清单见 ``docs/plans/M1-波次3至5-接口与测试.md`` 的「波次 3」。

**先落盘，后下单**：``submit`` 先把意图写进账本（幂等键去重）再进后台派发；派发时订单行先于请求落盘
（``create_pending`` → ``record_sent`` → 发请求 → ``apply_write_result``）。同一个意图只会产生一笔订单：重复提交、
重启后重放都不会再建第二笔——发现意图已有订单行就转入回查（RECONCILING），按编号查清之前不重发。

**并发**：开仓类意图先过全局串行的准入段（一把 ``asyncio.Lock``，先提交的先占名额）；之后所有意图按「合约 + 方向」
分键串行（同键同一时刻只派发一个，异键并发）。键的串行只覆盖「决定 + 发单 + 记结果」，发出之后等成交是异步的
（订阅 ``OrderStateChanged`` 判定完成），所以在途的部分平仓单不会挡住同键的保护类动作。杠杆与保证金模式的
「检查 + 设置」按合约另加一把锁。某个意图正有派发任务在处理时（含回查期的重发），订单事件不替它下结论，只记下
「待判定」，由派发者收尾时补做：派发者可能马上还要发下一张单（−2022 后按新读数重发、−4164 改走碎仓）。

**意图的状态**：每一步放弃都置 REJECTED 并写明中文原因；过期置 EXPIRED。请求发出后结果未知（超时、断连、503）置
RECONCILING：不重发开仓单，按编号回查（``resolve_unknown_orders``，调度器每 5 秒调用）。平仓单在「连续 3 次回查
不存在、请求已过签名时间窗、持仓数量未变」之后才换新编号重发（碎仓条件单换新编号重挂；部分平仓永不自动重发，
被 −2022 拒绝而持仓非零时直接失败，F4 第 9 步）。
平仓单（含部分平仓与碎仓条件单）已确认不存在、却因该侧持仓变了（非零）而不满足上面三条时：不重发，意图保持 RECONCILING
（该键一直算忙），并发一条 WARNING 告警 ``close_unknown_position_changed``（``dedupe_key`` 含意图号，每个意图只发一次），
提示人工核对——这张单可能其实部分成交过，也可能是别的操作改了仓位。整仓平仓此后若持仓回到发单时的数量，照常重发；
归零则记为已由其它途径平掉。
"持仓读数未知" 时，带 ``params.retry_until_done`` 的平仓（信号平仓、保护性平仓）也是置 RECONCILING，没有订单，
由 ``resolve_unknown_orders`` 稍后重新派发；操作员发起的平仓直接失败。

**开仓前的残留条件单**（F1 第 5 步）：开仓前，该合约该方向上不得有会作用到新开的仓上的未触发条件单——止损类（任何形式），
以及全平型的（任何类型，含碎仓平仓用的全平型止盈 ``TAKE_PROFIT_MARKET``：仓位被别的途径平掉后它残留在交易所，会在新仓上触发）。
带系统前缀的先撤并复读确认不存在；用户自己的（止损类、全平型止盈）放弃这次开仓，原因写进意图（不替用户撤）。
带数量的止盈单不是全平型，不拦。

**生命周期**：协调者是一次性对象，``stop()`` 之后不能再 ``start()``（抛 ``RuntimeError``，要重启请重建实例，新实例会接管
账本里未到终态的意图）。

**重启与晚到成交**：重启（或排空超时被取消）后没有人重新提交、却已有订单行的意图，``resolve_unknown_orders`` 会接手
（``_adopt_orphans``）：转入回查，不重发。订单在 REST 应答里就是 FILLED 时意图先完成，成交回报随后才入账，入账侧
（``IntentService``）会把已完成的意图当作「晚到成交」重开成 ACTIVE；订单已全部成交的，协调者再把它完成（只补「完成」，
不替人再拒绝一个被重开的意图）。系统止损触发后的成交同样会把已结束的止损类意图重开：止损单已触发并成交的再完成一次。
回查时对 ACTIVE 的意图也补做一次「只补完成」的判定（兜底漏掉的事件）。意图在请求发出之前就被别处结束（过期、撤销）
或出了内部错误时，落了盘却没发出的订单行置为 REJECTED。条件单替换的空窗（``stop_gap_max_s``）从撤旧单的请求发出时算起。

**意图参数（``params``）**：

- OPEN：``notional``（可选，覆盖配置的单笔名义金额）。
- CLOSE：``retry_until_done``（读不到持仓时稍后重试）、``resend_on_unknown``（未知三次后重发，默认真）。
- PARTIAL_CLOSE：``IntentSpec.qty`` 是请求数量（实际数量 = min(请求, 即时持仓)，按步长向下取整）。
- STOP_PLACE：``trigger_price``、``working_type``。
- STOP_REPLACE：``old``（``client_algo_id``、``algo_id``、``trigger_price``）、``trigger_price``、``working_type``、
  ``mode``（``gap`` | ``make_before_break``，缺省取配置 ``ex_stop_update_mode``）。
- STOP_CANCEL：``client_algo_id`` 与/或 ``algo_id``。ORDER_CANCEL：``client_order_id``。
- 协调者自己写入的：``held_before``（发部分平仓单 / 碎仓条件单时的即时持仓，结果未知时判断「持仓数量未变」）、
  ``same_id_resend``（``ex_resend_client_id=same`` 时沿用原编号重发的次数、时间与当时的「不存在」次数：
  每次重发之后都要重新满足签名时间窗与连续 3 次不存在，重发次数与换新编号一样以 ``MAX_CLOSE_RESENDS`` 为限）。

**STOP_REPLACE 的结果**写进意图 ``params["result"]``（保护单管理据此更新登记），字段：``outcome``、``mode``、
``stop_in_place``（本意图结束时该方向上有没有我们的止损在交易所；``None`` 表示不知道）、在位止损的
``client_algo_id``/``algo_id``/``trigger_price``、``gap_ms``（撤旧到新单存在的毫秒数，仅 gap 模式）、``cleanup``
（待清理的旧单列表）。``outcome``：

- ``replaced``（COMPLETED）新止损已在位；make_before_break 下撤旧失败或未知时 ``cleanup`` 列出旧单。
- ``restored_old``（REJECTED）新单被拒，已按旧止损价重挂。
- ``stop_crossed``（REJECTED）新单被拒、旧价也被越过（−2021）：没有止损，视为已触发，上层市价平仓。
- ``gap_exceeded``（REJECTED）从撤旧到新单存在超过 ``stop_gap_max_s``（新单若已在位 ``stop_in_place`` 为真）。
- ``kept_old``（REJECTED）本次更新放弃，旧单没动（撤旧结果未知且仍在，或 make_before_break 的新单被拒）。
- ``unprotected``（REJECTED）其它失败，没有止损在位，由保护单管理按 F2 第 4 步处置。
STOP_PLACE 被 −2021 拒绝时 ``state_reason`` 恰为 ``immediate_trigger``。

**崩溃点**（``CrashRegistry``）：``open_sent`` 与 ``partial_close_sent`` 在适配层调用返回后、记结果前；
``stop_cancelled_before_new`` 在撤旧单之后、挂新单之前。崩溃点触发后协调者视为进程被杀：其它在途任务被取消，
不再受理新意图，``wait_terminal`` 重抛 ``CrashSignal``。

公开方法不用 ``create_market_order``、``cancel_order`` 这类与适配层写方法同名的名字（结构检查会把它们当写方法调用）。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from dmr_kernel.algo.rounding import floor_to_step
from dmr_kernel.algo.sizing import size_order
from dmr_kernel.core.clock import Clock
from dmr_kernel.core.crashpoints import CrashRegistry, CrashSignal
from dmr_kernel.core.events import EventBus, IntentStateChanged, OrderStateChanged, Subscription
from dmr_kernel.core.intents import EXPIRED_REASON, IntentService, InvalidTransition
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.orders import DuplicateOrder, OrderNotFound, OrderService
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import (
    AlertLevel,
    AlgoOrderInfo,
    AlgoType,
    CancelOutcome,
    CancelResult,
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    Model,
    OrderOrigin,
    OrderPurpose,
    OrderState,
    PositionSide,
    PositionStatus,
    ReadResult,
    RejectKind,
    RunMode,
    Unknown,
    WorkingType,
    WriteAccepted,
    WriteRejected,
    WriteUnknown,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.adapter import SIGN_WINDOW_MS, ExchangeAdapter, WriteCapability
from dmr_kernel.exchange.errors import AuthConfigError
from dmr_kernel.exchange.rules import InstrumentRules
from dmr_kernel.ledger import repo_audit, repo_coordinator
from dmr_kernel.ledger.records import IntentRecord, OrderRecord
from dmr_kernel.ledger.writer import Ledger

if TYPE_CHECKING:  # 只用于类型标注：不在运行时导入同层（第 3 层以下）之外的东西
    from dmr_kernel.alerts.notifier import Notifier
    from dmr_kernel.core.intents import IntentSpec
    from dmr_kernel.core.riskgate import RiskGate

log = logging.getLogger(__name__)

__all__ = [
    "Coordinator", "CoordinatorStopped", "EXPIRED_REASON", "MAX_CLOSE_RESENDS", "OUTCOME_GAP_EXCEEDED",
    "OUTCOME_KEPT_OLD", "OUTCOME_REPLACED", "OUTCOME_RESTORED_OLD", "OUTCOME_STOP_CROSSED", "OUTCOME_UNPROTECTED",
    "RESULT_KEY", "SubmitResult",
]

S = IntentState
O = OrderState

RESULT_KEY = repo_coordinator.RESULT_KEY
OUTCOME_REPLACED = "replaced"
OUTCOME_RESTORED_OLD = "restored_old"
OUTCOME_STOP_CROSSED = "stop_crossed"
OUTCOME_GAP_EXCEEDED = "gap_exceeded"
OUTCOME_KEPT_OLD = "kept_old"
OUTCOME_UNPROTECTED = "unprotected"

IMMEDIATE_TRIGGER_REASON = "immediate_trigger"
RESTART_REFUSED = "协调者停止后不能重新启动，请重建实例"
CLOSE_POSITION_CHANGED_KIND = "close_unknown_position_changed"  # 告警类型：未知的平仓单确认不存在，但持仓变了
MAX_CLOSE_RESENDS = 2  # 平仓单超时后最多重发几次（换新编号或沿用原编号都算；再多就留给人处理）
HELD_BEFORE_KEY = "held_before"  # params：发部分平仓单 / 碎仓条件单时的即时持仓（判断「持仓数量未变」）
SAME_RESEND_KEY = "same_id_resend"  # params：沿用原编号重发的记录 {cid, count, at_ms, absent_base}

LEG_OPEN, LEG_CLOSE, LEG_PARTIAL, LEG_STOP, LEG_DUST = "o", "c", "p", "s", "d"

TERMINAL = frozenset(S.terminal())
SHADOW_REASON = "影子记录：只留痕不下单"
FRESH = frozenset({S.CREATED, S.VALIDATED, S.RESERVED})  # 还没有建订单的状态
PRE_SEND = FRESH | {S.DISPATCH_READY}  # 还没有记下订单结果的状态
_FORWARD = {S.CREATED: S.VALIDATED, S.VALIDATED: S.DISPATCH_READY, S.RESERVED: S.DISPATCH_READY}
_UNCONFIRMED = (O.PENDING_SEND, O.SENT_UNKNOWN)  # 交易所还没有确认过的订单
_WORKING = (O.ACKNOWLEDGED, O.PARTIALLY_FILLED)
_MARKET_PURPOSES = {
    IntentAction.OPEN: frozenset({OrderPurpose.OPEN}),
    IntentAction.CLOSE: frozenset({OrderPurpose.CLOSE, OrderPurpose.DUST_CLOSE}),
    IntentAction.PARTIAL_CLOSE: frozenset({OrderPurpose.PARTIAL_CLOSE}),
}
_STOP_ACTIONS = (IntentAction.STOP_PLACE, IntentAction.STOP_REPLACE)
_CLOSE_ACTIONS = (IntentAction.CLOSE, IntentAction.PARTIAL_CLOSE)
_ORDER_ALERT_KEY = "coordinator_internal_error"
_AUTO = object()  # 参数没给（与 None 区分）


# ---------------------------------------------------------------- 公开类型


class CoordinatorStopped(RuntimeError):
    """协调者不再受理新意图：还没启动、已经停止，或已因崩溃点停止。"""


@dataclass(frozen=True)
class SubmitResult:
    intent_id: int
    created: bool  # False = 幂等键已存在，没有新建
    state: IntentState


# ---------------------------------------------------------------- 内部控制流


class _Reject(Exception):
    """放弃本次执行，意图置 REJECTED。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Finish(Exception):
    """不用（再）发请求，意图直接置给定的终态（COMPLETED 或 EXPIRED）。"""

    def __init__(self, to: IntentState, reason: str) -> None:
        super().__init__(reason)
        self.to = to
        self.reason = reason


class _Defer(Exception):
    """暂时无法确定：意图置 RECONCILING，稍后由 ``resolve_unknown_orders`` 处理。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Gone(Exception):
    """意图已被别处结束或不存在：当前派发步骤安静地结束。"""


@dataclass(frozen=True)
class _Verdict:
    to: IntentState  # ACTIVE = 仅确认订单存在（RECONCILING → ACTIVE）；其它是终态
    reason: str
    result: dict[str, Any] | None = None


class _Run:
    """一次派发的上下文：最新的意图记录，以及请求是否已经发出（出了意外错误时决定走向）。"""

    __slots__ = ("rec", "sent")

    def __init__(self, rec: IntentRecord) -> None:
        self.rec = rec
        self.sent = False


Job = Callable[[_Run], Awaitable[None]]


def _fmt(value: Decimal | None) -> str:
    return "—" if value is None else format(value.normalize(), "f")


def _is_system_id(cid: str | None) -> bool:
    return ids.parse_client_id(cid).origin is OrderOrigin.SYSTEM


# ---------------------------------------------------------------- 协调者


class Coordinator:
    def __init__(self, *, adapter: ExchangeAdapter, capability: WriteCapability, ledger: Ledger, bus: EventBus,
                 clock: Clock, config_get: Callable[[str], Any], modelstate: ModelStateService, registry: Registry,
                 intents: IntentService, orders: OrderService, riskgate: "RiskGate", limits: Limits,
                 rules: InstrumentRules, notifier: "Notifier | None", crash: CrashRegistry,
                 list_check: Callable[[str, PositionSide], tuple[bool, str]] | None = None) -> None:
        self._adapter = adapter
        self._cap = capability
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._config_get = config_get
        self._modelstate = modelstate
        self._registry = registry
        self._intents = intents
        self._orders = orders
        self._riskgate = riskgate
        self._limits = limits
        self._rules = rules
        self._notifier = notifier
        self._crash = crash
        self._list_check = list_check

        self._started = False
        self._stopping = False
        self._crashed: CrashSignal | None = None
        self._subscriptions: list[Subscription] = []
        self._tasks: dict[int, asyncio.Task] = {}
        self._tracked: dict[int, tuple[str, PositionSide, IntentAction]] = {}  # 未到终态的意图
        self._admission = asyncio.Lock()
        self._key_locks: dict[tuple[str, PositionSide], asyncio.Lock] = {}
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self._eval_locks: dict[int, asyncio.Lock] = {}
        self._resolve_lock = asyncio.Lock()
        self._waiters: list[asyncio.Future] = []
        self._pauses: dict[int, tuple[int, int]] = {}  # 意图号 -> (持仓号, 暂停到期毫秒)
        self._cancel_notes: dict[int, str] = {}
        self._deferred_evals: set[int] = set()  # 派发期间推迟的完成判定
        self._position_changed_warned: set[int] = set()  # 已发过「平仓单确认不存在但持仓变了」告警的意图（每个只发一次）

    # ================================================================ 生命周期

    async def start(self) -> None:
        """订阅订单与意图事件，并把账本里未到终态的意图记入「键是否忙」的登记。重复调用无害。

        协调者是一次性对象：``stop()`` 之后（含排空还没结束时）不能再启动，抛 ``RuntimeError``——订阅已取消、停止标志已立，
        悄悄返回会让它既不受理也不回查却看起来已启动。要重新启动请重建实例（新实例会接管账本里未到终态的意图）。
        特意不用 ``CoordinatorStopped``：那是关停期间「不再受理」的预期信号，调用方可能成片捕获它；重启停止后的实例是误用，
        不该被这种处理悄悄吞掉。"""
        if self._stopping:
            raise RuntimeError(RESTART_REFUSED)
        if self._started:
            return
        self._started = True
        self._subscriptions = [
            self._bus.subscribe(OrderStateChanged, self._on_order_event, name="coordinator.orders"),
            self._bus.subscribe(IntentStateChanged, self._on_intent_event, name="coordinator.intents"),
        ]
        for rec in await self._intents.non_terminal():
            self._track(rec)

    async def stop(self, drain_s: float) -> None:
        """不再受理新意图；等在途的派发步骤结束（最多 ``drain_s`` 秒），到点还没结束的取消，留给重启恢复。
        停止之后这个实例不能再 ``start()``（见 ``start``）。"""
        self._stopping = True
        pending = [task for task in self._tasks.values() if not task.done()]
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=max(float(drain_s), 0.0))
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions = []
        self._notify()

    @property
    def crashed(self) -> CrashSignal | None:
        """崩溃点触发过就是那个 ``CrashSignal``（协调者此后视为已死）。"""
        return self._crashed

    async def idle(self, timeout_s: float = 10.0) -> None:
        """等所有在途的派发任务结束（测试与关停用）；到点没结束就返回。"""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            pending = [task for task in self._tasks.values() if not task.done()]
            remaining = deadline - asyncio.get_running_loop().time()
            if not pending or remaining <= 0:
                return
            await asyncio.wait(pending, timeout=remaining)

    # ================================================================ 提交与等待

    async def submit(self, spec: "IntentSpec") -> SubmitResult:
        """先落盘（CREATED，幂等键去重），再进该键的串行队列；立即返回。
        幂等键已存在：不建新意图；还没发出订单的未到终态意图会（重新）派发，已经有订单的不会再发第二笔。"""
        self._require_alive()
        rec, created = await self._intents.create(spec)
        self._track(rec)
        if rec.state in PRE_SEND:
            self._spawn(rec)
        return SubmitResult(intent_id=rec.id, created=created, state=rec.state)

    async def wait_terminal(self, intent_id: int, timeout_s: float) -> IntentRecord:
        """等意图到终态；超时返回当前记录（``timeout_s`` 是真实时间）。意图不存在抛 ``KeyError``；
        协调者因崩溃点停止时重抛那个 ``CrashSignal``。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            waiter = self._new_waiter()
            rec = await self._intents.get(intent_id)
            if self._crashed is not None:
                self._drop_waiter(waiter)
                raise self._crashed
            if rec is None:
                self._drop_waiter(waiter)
                raise KeyError(f"意图 {intent_id} 不存在")
            remaining = deadline - loop.time()
            if rec.state in TERMINAL or remaining <= 0:
                self._drop_waiter(waiter)
                return rec
            await self._wait(waiter, remaining)

    def key_busy(self, symbol: str, side: PositionSide) -> bool:
        """该「合约 + 方向」有未到终态的系统意图（含排队中、状态未知的）。挂在交易所的止损单不算：
        止损意图受理后即完成。"""
        return any(s == symbol and sd == side for s, sd, _ in self._tracked.values())

    async def open_intents_settled(self, timeout_s: float) -> bool:
        """全部开仓意图到终态（F5 第 4 步、F6 第 5 步）。``timeout_s`` 是真实时间；到点还有未结束的返回 False
        （含状态未知、还在回查的开仓单）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            waiter = self._new_waiter()
            if not any(action is IntentAction.OPEN for _, _, action in self._tracked.values()):
                self._drop_waiter(waiter)
                return True
            remaining = deadline - loop.time()
            if remaining <= 0 or self._crashed is not None:
                self._drop_waiter(waiter)
                return False
            await self._wait(waiter, remaining)

    async def live_position(self, symbol: str, side: PositionSide) -> ReadResult[Decimal]:
        """即时持仓读数（直接读交易所）。读不到是 ``Unknown``，不是 0；该侧没有仓是 ``Known(0)``。"""
        result = await self._adapter.fetch_positions()
        if isinstance(result, Unknown):
            return result
        total = sum((p.qty for p in result.value if p.symbol == symbol and p.side is side), Decimal("0"))
        return Known(total)

    # ================================================================ 停止与切换时撤单

    async def cancel_unfilled_opens(self, *, model: str | None, reason: str) -> int:
        """撤掉未成交的开仓单（停止与切换，F6 第 4 步）。还没发出订单、仍在排队的开仓意图直接置 CANCELLED；
        已经挂在交易所、未成交的开仓单发撤单请求。返回撤掉的意图数 + 发出的撤单请求数。状态未知的订单不动，
        留给回查。"""
        self._require_alive()
        count = 0
        for rec in await self._intents.non_terminal(action=IntentAction.OPEN, model=model):
            try:
                async with self._key_lock(rec.symbol, rec.side):
                    count += await self._cancel_open_intent(rec.id, reason)
            except CrashSignal:
                raise
            except Exception:
                log.exception("撤开仓意图 %s 失败", rec.id)
        return count

    async def _cancel_open_intent(self, intent_id: int, reason: str) -> int:
        rec = await self._intents.get(intent_id)
        if rec is None or rec.state in TERMINAL:
            return 0
        if rec.state in FRESH:
            try:
                moved = await self._intents.transition(rec.id, S.CANCELLED, reason=f"已撤销：{reason}",
                                                       expect=set(FRESH))
            except InvalidTransition:
                return 0
            await self._after(moved)
            return 1
        sent = 0
        for order in await self._orders.for_intent(rec.id):
            if order.purpose is OrderPurpose.OPEN and order.state in _WORKING:
                self._cancel_notes[rec.id] = reason
                result = await self._adapter.cancel_order(self._cap, symbol=order.symbol,
                                                          client_order_id=order.client_order_id)
                await self._orders.apply_cancel_result(order.client_order_id, result)
                sent += 1
        return sent

    # ================================================================ 回查未知订单（调度器每 5 秒调用）

    async def resolve_unknown_orders(self) -> None:
        """回查命运未定的订单，并按 F1 第 9 步、F5 第 7 步判定：查到就按交易所的结果走；开仓单「请求发出已超过签名时间窗
        加宽限、连续 3 次不存在、该侧持仓没变」才置 REJECTED，绝不重发；平仓单同样的条件满足且允许重发时换新编号
        重发。另外：接手重启后无人派发却已有订单行的意图（``_adopt_orphans``）、重新派发读不到持仓而延期的平仓、
        轮询碎仓条件单、恢复到期的部分平仓暂停。协调者停止之后不再回查。"""
        if not self._started or self._stopping or self._crashed is not None or self._resolve_lock.locked():
            return
        async with self._resolve_lock:
            await self._adopt_orphans()
            try:
                await self._orders.requery_unknown()
            except Exception:
                log.exception("回查未知订单失败")
            for rec in await self._intents.non_terminal():
                if self._crashed is not None:
                    return
                task = self._tasks.get(rec.id)
                if task is not None and not task.done():
                    continue  # 正有派发任务在处理它
                try:
                    if rec.state is S.RECONCILING:
                        await self._resolve_reconciling(rec)
                    elif rec.state is S.ACTIVE:
                        await self._poll_active(rec)
                        # 兜底：订单已到终态却没收到（或没处理成）事件的，补上「完成」（只补完成，不替人拒绝）
                        await self._evaluate_completion(rec.id, only_complete=True)
                except CrashSignal as signal:
                    self._die(signal)
                    return
                except Exception:
                    log.exception("回查意图 %s 失败", rec.id)
            await self._release_due_pauses()

    async def _adopt_orphans(self) -> None:
        """重启（或排空超时被取消）之后没有人重新提交的意图：还没记下订单结果、没有派发任务、账本里却已有订单行的，
        转入回查——请求可能已经发出，按自定义编号查清之前不重发。没有订单行的什么也没发过，不动它。"""
        for rec in await self._intents.non_terminal():
            if rec.state not in PRE_SEND:
                continue
            task = self._tasks.get(rec.id)
            if task is None or task.done():
                await self._spawn_and_wait(rec, self._guard_replay)

    # ================================================================ 事件

    async def _on_order_event(self, event: OrderStateChanged) -> None:
        if self._crashed is not None or event.intent_id is None:
            return
        if event.state in (O.PENDING_SEND, O.SENT_UNKNOWN):
            return
        await self._evaluate_completion(event.intent_id)

    async def _on_intent_event(self, event: IntentStateChanged) -> None:
        if self._crashed is not None:
            return
        rec = await self._intents.get(event.intent_id)
        if rec is None:
            return
        await self._after(rec)
        if rec.state is S.ACTIVE:
            # 订单在 REST 应答里就是 FILLED 时意图先完成，成交回报随后入账，入账侧会把终态意图重开成 ACTIVE：
            # 订单已经全部成交的就再完成一次，不让它卡在 ACTIVE。只补「完成」，不替人再拒绝一个被重开的意图。
            await self._evaluate_completion(rec.id, only_complete=True)

    # ================================================================ 登记、等待与唤醒

    def _track(self, rec: IntentRecord) -> None:
        if rec.state in TERMINAL:
            self._tracked.pop(rec.id, None)
        else:
            self._tracked[rec.id] = (rec.symbol, rec.side, rec.action)

    async def _after(self, rec: IntentRecord) -> None:
        """意图状态变了之后的善后：键登记、名额释放、部分平仓暂停的恢复、唤醒等待者。"""
        self._track(rec)
        if rec.state in TERMINAL:
            self._limits.release(rec.id)
            lock = self._eval_locks.get(rec.id)
            if lock is not None and not lock.locked():
                self._eval_locks.pop(rec.id, None)
            self._position_changed_warned.discard(rec.id)
            await self._end_pause(rec.id)
        self._notify()

    def _new_waiter(self) -> asyncio.Future:
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        return waiter

    def _drop_waiter(self, waiter: asyncio.Future) -> None:
        if waiter in self._waiters:
            self._waiters.remove(waiter)
        if not waiter.done():
            waiter.cancel()

    async def _wait(self, waiter: asyncio.Future, timeout: float) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout)
        except TimeoutError:
            pass
        finally:
            self._drop_waiter(waiter)

    def _notify(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _require_alive(self) -> None:
        if self._crashed is not None:
            raise CoordinatorStopped(f"协调者已因崩溃点 {self._crashed.name} 停止")
        if not self._started:
            raise CoordinatorStopped("协调者还没有启动")
        if self._stopping:
            raise CoordinatorStopped("协调者正在停止，不再受理新意图")

    def _key_lock(self, symbol: str, side: PositionSide) -> asyncio.Lock:
        return self._key_locks.setdefault((symbol, side), asyncio.Lock())

    def _symbol_lock(self, symbol: str) -> asyncio.Lock:
        return self._symbol_locks.setdefault(symbol, asyncio.Lock())

    def _die(self, signal: CrashSignal) -> None:
        """崩溃点触发：视为进程被杀。其它在途任务取消，不再发任何请求。"""
        if self._crashed is None:
            self._crashed = signal
        current = asyncio.current_task()
        for task in list(self._tasks.values()):
            if task is not current and not task.done():
                task.cancel()
        self._notify()

    def _alert(self, level: AlertLevel, kind: str, message: str, key: str | None = None) -> None:
        log.log(logging.CRITICAL if level is AlertLevel.CRITICAL else logging.WARNING, "%s：%s", kind, message)
        if self._notifier is None:
            return
        try:
            self._notifier.raise_alert(level, kind, message, dedupe_key=key)
        except Exception:
            log.exception("提出告警失败：%s", kind)

    def _is_live(self) -> bool:
        """实盘环境？崩溃点注册表记着环境（``CrashRegistry.live``）；没有注册表时按实盘处理（更保守）。"""
        live = getattr(self._crash, "live", None)
        return True if live is None else bool(live)

    # ================================================================ 派发任务

    def _spawn(self, rec: IntentRecord, job: Job | None = None) -> asyncio.Task | None:
        existing = self._tasks.get(rec.id)
        if existing is not None and not existing.done():
            return None
        task = asyncio.get_running_loop().create_task(self._run(rec, job or self._dispatch),
                                                      name=f"coordinator:intent:{rec.id}")
        self._tasks[rec.id] = task
        task.add_done_callback(lambda done, intent_id=rec.id: self._task_done(intent_id, done))
        return task

    def _task_done(self, intent_id: int, task: asyncio.Task) -> None:
        if self._tasks.get(intent_id) is task:
            del self._tasks[intent_id]
        self._notify()

    async def _spawn_and_wait(self, rec: IntentRecord, job: Job) -> None:
        task = self._spawn(rec, job)
        if task is not None:
            await asyncio.wait([task])

    async def _run(self, rec: IntentRecord, job: Job) -> None:
        run = _Run(rec)
        try:
            try:
                await job(run)
            except (_Reject, _Finish, _Defer) as outcome:
                await self._settle(run, outcome)
        except _Gone:
            self._deferred_evals.discard(rec.id)
            await self._abandon_unsent(rec.id, "订单未发出：意图在请求发出之前已被别处结束（过期或撤销）")
            return
        except CrashSignal as signal:
            self._die(signal)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._internal_error(run, exc)
        await self._catch_up(rec.id)

    async def _catch_up(self, intent_id: int) -> None:
        """派发结束时补做完成判定：派发期间到达的订单事件被推迟了（见 ``_evaluate_completion``）。
        循环到没有新推迟为止；之后到任务结束之间没有挂起点，事件处理不会再漏掉。"""
        while self._crashed is None:
            self._deferred_evals.discard(intent_id)
            try:
                await self._evaluate_completion(intent_id)
            except Exception:
                log.exception("派发结束时判定意图 %s 失败", intent_id)
                return
            if intent_id not in self._deferred_evals:
                return

    async def _abandon_unsent(self, intent_id: int, reason: str) -> None:
        """意图在请求发出之前就结束了：落了盘却没发出的订单行（没有发出时间 = 请求从未发出）置为 REJECTED，
        免得它一直是 PENDING_SEND，被反复回查、被当成该键上在途的系统订单。发出过的订单一律不动。"""
        try:
            for order in await self._orders.for_intent(intent_id):
                if order.state is O.PENDING_SEND and order.sent_ms is None:
                    await self._orders.mark_rejected(order.client_order_id, reason=reason)
        except Exception:
            log.exception("关闭意图 %s 未发出的订单行失败", intent_id)

    async def _settle(self, run: _Run, outcome: _Reject | _Finish | _Defer) -> None:
        if isinstance(outcome, _Reject):
            log.info("意图 %s 放弃：%s", run.rec.id, outcome.reason)
            await self._move(run, S.REJECTED, outcome.reason)
        elif isinstance(outcome, _Finish):
            await self._conclude(run, outcome.to, outcome.reason)
        else:
            await self._to_reconciling(run, outcome.reason)

    async def _internal_error(self, run: _Run, exc: Exception) -> None:
        """派发中出了意外错误：请求还没发出就放弃；请求可能已发出就按未知处理，绝不重发。"""
        log.exception("协调者处理意图 %s 时出现意外错误", run.rec.id)
        try:
            rec = await self._intents.get(run.rec.id)
            if rec is None or rec.state in TERMINAL:
                return
            run.rec = rec
            text = f"{type(exc).__name__}：{exc}"
            sent = run.sent or any(o.sent_ms is not None for o in await self._orders.for_intent(rec.id))
            if sent:
                self._alert(AlertLevel.CRITICAL, _ORDER_ALERT_KEY,
                            f"意图 {rec.id}（{rec.symbol} {rec.side.value} {rec.action.value}）处理中出现内部错误，"
                            f"请求可能已发出，已转入回查、不会重发：{text}", key=f"{_ORDER_ALERT_KEY}:{rec.id}")
                await self._to_reconciling(run, f"内部错误：{text}；请求可能已发出，不重发，按自定义编号回查")
            else:
                self._alert(AlertLevel.WARNING, _ORDER_ALERT_KEY,
                            f"意图 {rec.id}（{rec.symbol} {rec.side.value} {rec.action.value}）处理中出现内部错误，"
                            f"已放弃（请求尚未发出）：{text}", key=f"{_ORDER_ALERT_KEY}:{rec.id}")
                await self._move(run, S.REJECTED, f"内部错误：{text}")
                await self._abandon_unsent(rec.id, f"订单未发出：处理意图时出现内部错误（{text}）")
        except _Gone:
            pass
        except Exception:
            log.exception("处理内部错误时再次出错（意图 %s）", run.rec.id)

    async def _shadow_intercept(self, run: _Run) -> None:
        """影子记录通道（模型 3 预留）：来源为 SHADOW 的意图在准入段之前就地拦截——写审计，意图置 REJECTED，
        不看开仓闸、名单、频率、不占名额，更不产生订单（调用方 ``_run`` 把抛出的 ``_Reject`` 落成 REJECTED）。"""
        rec = run.rec
        await self._ledger.write(
            repo_audit.append, at_ms=self._clock.now_ms(), operator=rec.operator or "system", action="shadow.intent",
            target=f"intent:{rec.id}", before=None,
            after={"idem_key": rec.idem_key, "action": rec.action.value, "model": rec.model, "symbol": rec.symbol,
                   "side": rec.side.value, "reason": rec.reason, "ai_eval_ref": rec.ai_eval_ref},
            result="recorded")
        raise _Reject(SHADOW_REASON)

    async def _dispatch(self, run: _Run) -> None:
        if run.rec.source is IntentSource.SHADOW:
            await self._shadow_intercept(run)
        action = run.rec.action
        if action is IntentAction.OPEN:
            await self._open(run)
        elif action is IntentAction.CLOSE:
            await self._guard_replay(run)
            await self._close(run, partial=False)
        elif action is IntentAction.PARTIAL_CLOSE:
            await self._guard_replay(run)
            await self._close(run, partial=True)
        elif action is IntentAction.STOP_PLACE:
            await self._guard_replay(run)
            await self._stop_place(run)
        elif action is IntentAction.STOP_REPLACE:
            await self._guard_replay(run)
            await self._stop_replace(run)
        elif action is IntentAction.STOP_CANCEL:
            await self._guard_replay(run)
            await self._stop_cancel(run)
        elif action is IntentAction.ORDER_CANCEL:
            await self._guard_replay(run)
            await self._order_cancel(run)
        else:
            raise _Reject(f"协调者不执行「{action.value}」类意图")

    async def _guard_replay(self, run: _Run) -> None:
        """同一个意图只产生一笔订单：还没记下结果的意图若已有订单行（重启后重放、崩溃点之后），不再发第二笔，转入回查。"""
        if run.rec.state not in PRE_SEND:
            return
        orders = await self._orders.for_intent(run.rec.id)
        if orders:
            names = "、".join(o.client_order_id for o in orders)
            raise _Defer(f"意图已有订单（{names}），是重复提交或重启后重放：不重发，按自定义编号回查")

    # ================================================================ 意图状态迁移

    async def _move(self, run: _Run, to: IntentState, reason: str = "") -> IntentRecord:
        """迁一步。检查与修改在账本同一事务里；别处已经改过（并发）就读回来：已结束则安静退出。"""
        try:
            rec = await self._intents.transition(run.rec.id, to, reason=reason, expect={run.rec.state})
        except InvalidTransition:
            fresh = await self._intents.get(run.rec.id)
            if fresh is not None:
                run.rec = fresh
                await self._after(fresh)
            if fresh is None or fresh.state in TERMINAL:
                raise _Gone() from None
            if fresh.state is to:
                return fresh
            raise
        run.rec = rec
        await self._after(rec)
        return rec

    async def _fresh(self, run: _Run) -> IntentRecord:
        """读回最新的意图：已结束的退出；还没发出订单却过期了就置 EXPIRED（过期的意图不会被执行）。"""
        rec = await self._intents.get(run.rec.id)
        if rec is None or rec.state in TERMINAL:
            raise _Gone()
        run.rec = rec
        if rec.state in FRESH and self._intents.is_expired(rec, self._clock.now_ms()):
            raise _Finish(S.EXPIRED, EXPIRED_REASON)
        return rec

    async def _ensure_ready(self, run: _Run) -> None:
        """CREATED/VALIDATED/RESERVED → DISPATCH_READY（逐步走，每步一个事件）。"""
        while run.rec.state in _FORWARD:
            await self._move(run, _FORWARD[run.rec.state])

    async def _ensure_active(self, run: _Run) -> None:
        await self._ensure_ready(run)
        if run.rec.state is S.DISPATCH_READY:
            await self._move(run, S.ACTIVE)

    async def _to_reconciling(self, run: _Run, reason: str) -> None:
        await self._ensure_ready(run)
        if run.rec.state is not S.RECONCILING:
            await self._move(run, S.RECONCILING, reason)

    async def _conclude(self, run: _Run, to: IntentState, reason: str) -> None:
        if to is S.COMPLETED:
            await self._ensure_active(run)
        await self._move(run, to, reason)

    async def _set_result(self, intent_id: int, result: dict[str, Any]) -> None:
        await self._ledger.write(repo_coordinator.set_result, intent_id, result, now_ms=self._clock.now_ms())

    async def _end_intent(self, intent_id: int, verdict: _Verdict) -> IntentRecord | None:
        """事件与回查路径上结束（或激活）意图：先写结果，再按期望状态迁移（被别处抢先则返回 None）。"""
        if verdict.result is not None:
            await self._set_result(intent_id, verdict.result)
        try:
            rec = await self._intents.transition(intent_id, verdict.to, reason=verdict.reason,
                                                 expect={S.ACTIVE, S.RECONCILING})
        except InvalidTransition:
            return None
        await self._after(rec)
        return rec

    # ================================================================ 完成判定

    async def _evaluate_completion(self, intent_id: int, *, only_complete: bool = False) -> None:
        """订单到了终态（或确认存在）后判定意图：FILLED → COMPLETED；作废但有部分成交 → COMPLETED（注明部分成交）；
        作废且无成交 → REJECTED；被拒 → REJECTED。还在 DISPATCH_READY 的意图由派发者自己处理。
        ``only_complete``：只接受「完成」的判定（被晚到成交重开的意图用，见 ``_on_intent_event``）。

        派发任务正在处理这个意图时（回查期的重发、−2022 后按新读数重发、−4164 改走碎仓），别处（订单事件）不下结论，
        只记下「待判定」，由派发者收尾时补做（``_catch_up``）：否则可能在派发者发下一张单之前就把意图结束掉。"""
        task = self._tasks.get(intent_id)
        if task is not None and not task.done() and task is not asyncio.current_task():
            self._deferred_evals.add(intent_id)
            return
        lock = self._eval_locks.setdefault(intent_id, asyncio.Lock())
        async with lock:
            rec = await self._intents.get(intent_id)
            if rec is None or rec.state not in (S.ACTIVE, S.RECONCILING):
                return
            verdict = self._judge(rec, await self._orders.for_intent(intent_id))
            if verdict is None or (verdict.to is S.ACTIVE and rec.state is S.ACTIVE):
                return
            if only_complete and verdict.to is not S.COMPLETED:
                return
            await self._end_intent(intent_id, verdict)

    def _judge(self, rec: IntentRecord, orders: list[OrderRecord]) -> _Verdict | None:
        action = rec.action
        if action in _STOP_ACTIONS:
            stops = [o for o in orders if o.purpose is OrderPurpose.STOP]
            if rec.state is S.ACTIVE:
                # 止损类意图受理（或回查确认）后即结束，不会停在 ACTIVE。ACTIVE 只能是止损触发后的成交入账时被入账侧
                # 当作「晚到成交」重开的：止损单已触发并成交就再完成一次（结果不改），免得该键永远算有在途动作。
                if any(o.state is O.FILLED for o in stops):
                    return _Verdict(S.COMPLETED, "止损条件单已触发并成交（意图被入账侧重开后再次完成）")
                return None
            if rec.state is not S.RECONCILING:  # 派发者自己在受理时结束止损类意图；这里只处理回查出结果的
                return None
            return self._judge_stop(rec, stops[-1]) if stops else None
        purposes = _MARKET_PURPOSES.get(action)
        if purposes is None:
            return None
        mine = [o for o in orders if o.purpose in purposes]
        if not mine:
            return None
        cur = mine[-1]
        if self._awaiting_retry(rec, cur):
            return None
        note = self._cancel_notes.pop(rec.id, "") if cur.state in (O.CANCELLED, O.REJECTED) else ""
        suffix = f"（{note}）" if note else ""
        what = "碎仓条件单" if cur.purpose is OrderPurpose.DUST_CLOSE else "订单"
        if cur.state is O.FILLED:
            if cur.qty is None:
                return _Verdict(S.COMPLETED, f"{what}已触发并成交（成交 {_fmt(cur.filled_qty)}）")
            return _Verdict(S.COMPLETED, f"{what}已全部成交（{_fmt(cur.filled_qty)}/{_fmt(cur.qty)}）")
        if cur.state is O.CANCELLED:
            if cur.filled_qty > 0:
                return _Verdict(S.COMPLETED, f"{what}被交易所作废，已部分成交（{_fmt(cur.filled_qty)}/"
                                             f"{_fmt(cur.qty)}）{suffix}")
            return _Verdict(S.REJECTED, f"{what}被撤销或作废，没有成交{suffix}")
        if cur.state is O.REJECTED:
            code = "" if cur.error_code is None else f"（错误码 {cur.error_code}）"
            return _Verdict(S.REJECTED, f"{what}被拒绝：{cur.error_msg or '无说明'}{code}")
        if rec.state is S.RECONCILING and cur.state in (O.ACKNOWLEDGED, O.PARTIALLY_FILLED, O.CANCEL_PENDING):
            return _Verdict(S.ACTIVE, f"回查确认{what}存在")
        return None

    def _judge_stop(self, rec: IntentRecord, cur: OrderRecord) -> _Verdict | None:
        """止损类意图在回查之后的判定：条件单存在 → 完成；被拒或作废 → 放弃。"""
        exists = cur.state in (O.ACKNOWLEDGED, O.PARTIALLY_FILLED, O.FILLED, O.CANCEL_PENDING)
        failed = cur.state in (O.REJECTED, O.CANCELLED)
        if not exists and not failed:
            return None
        if rec.action is IntentAction.STOP_PLACE:
            if exists:
                return _Verdict(S.COMPLETED, "回查确认止损条件单已挂上")
            return _Verdict(S.REJECTED, f"止损条件单被拒绝或作废：{cur.error_msg or '无说明'}")
        mode = self._replace_mode(rec)
        if exists:
            restored = cur.leg != LEG_STOP
            outcome = OUTCOME_RESTORED_OLD if restored else OUTCOME_REPLACED
            result = self._stop_result(outcome, mode, stop=cur,
                                       cleanup=[self._old_stop(rec)] if mode == "make_before_break" and not restored
                                       else [])
            if restored:
                return _Verdict(S.REJECTED, "新止损单未能挂上，回查确认已按旧止损价重挂", result)
            return _Verdict(S.COMPLETED, "回查确认新止损条件单已挂上", result)
        if mode == "make_before_break":
            result = self._stop_result(OUTCOME_KEPT_OLD, mode, in_place=True)
            return _Verdict(S.REJECTED, f"新止损单未能挂上，旧止损保留：{cur.error_msg or '无说明'}", result)
        result = self._stop_result(OUTCOME_UNPROTECTED, mode, in_place=False)
        return _Verdict(S.REJECTED, f"新止损单未能挂上，该方向现在没有止损：{cur.error_msg or '无说明'}", result)

    # ================================================================ 发请求的公共部分

    async def _transmit(self, run: _Run, order: OrderRecord, send: Callable[[], Awaitable[Any]], *,
                        crash: str | None = None) -> tuple[OrderRecord, Any]:
        """订单行已落盘之后：记发出时间 → 记一笔订单频率 → 发请求 → 崩溃点 → 把结果交给订单服务。
        返回（记录结果之后的订单，适配层的写结果）。"""
        cid = order.client_order_id
        await self._orders.record_sent(cid)
        self._limits.note_order(order.symbol, self._clock.now_ms())
        run.sent = True
        try:
            result = await send()
        except AuthConfigError as exc:
            reason = f"交易所认证失败：{exc}"
            await self._orders.mark_rejected(cid, reason=reason)
            self._alert(AlertLevel.CRITICAL, "exchange_auth_failed",
                        f"向交易所发请求时认证失败，意图 {order.intent_id} 的订单 {cid} 未被受理：{exc}",
                        key="exchange_auth_failed")
            raise _Reject(reason) from None
        except Exception as exc:  # 适配层抛出意外错误：请求到没到交易所不明，按未知处理
            log.exception("适配层写请求出现意外错误：%s", cid)
            self._alert(AlertLevel.CRITICAL, _ORDER_ALERT_KEY,
                        f"订单 {cid} 的写请求出现意外错误，结果未知，已转入回查、不会重发：{type(exc).__name__}：{exc}",
                        key=f"{_ORDER_ALERT_KEY}:{cid}")
            result = WriteUnknown(f"适配层异常：{type(exc).__name__}：{exc}")
        if crash is not None:
            self._crash.hit(crash)
        updated = await self._orders.apply_write_result(cid, result)
        return updated, result

    def _market_sender(self, order: OrderRecord) -> Callable[[], Awaitable[Any]]:
        return lambda: self._adapter.create_market_order(
            self._cap, symbol=order.symbol, side=order.side, position_side=order.position_side, qty=order.qty,
            client_order_id=order.client_order_id)

    def _stop_sender(self, order: OrderRecord, algo_type: AlgoType) -> Callable[[], Awaitable[Any]]:
        return lambda: self._adapter.create_stop_order(
            self._cap, symbol=order.symbol, side=order.side, position_side=order.position_side,
            trigger_price=order.trigger_price, working_type=order.working_type, client_algo_id=order.client_order_id,
            close_position=True, algo_type=algo_type)

    @staticmethod
    def _reject_text(what: str, result: Any, order: OrderRecord) -> str:
        if isinstance(result, WriteRejected):
            return f"交易所拒绝{what}：{result.message}（错误码 {result.code}）"
        code = "" if order.error_code is None else f"（错误码 {order.error_code}）"
        return f"{what}被拒绝：{order.error_msg or '无说明'}{code}"

    async def _after_send(self, run: _Run, order: OrderRecord, result: Any, what: str) -> None:
        """市价单发出并记下结果之后的统一走向：被拒 → 放弃；结果未知 → 回查；其它 → ACTIVE 并判定是否已完成。"""
        if order.state is O.REJECTED:
            raise _Reject(self._reject_text(what, result, order))
        if order.state in _UNCONFIRMED:
            reason = result.reason if isinstance(result, WriteUnknown) else (order.error_msg or "结果未知")
            raise _Defer(f"{what}结果未知（{reason}），不重发，按自定义编号回查")
        await self._ensure_active(run)
        await self._evaluate_completion(run.rec.id)

    async def _create_order(self, **kw: Any) -> OrderRecord:
        try:
            return await self._orders.create_pending(**kw)
        except DuplicateOrder as dup:
            raise _Defer(f"订单 {dup.existing.client_order_id} 已存在，不重复创建，按自定义编号回查") from None

    # ================================================================ 读交易所的小工具

    async def _last_price(self, symbol: str) -> Decimal | Unknown:
        result = await self._adapter.fetch_tickers([symbol])
        if isinstance(result, Unknown):
            return result
        ticker = result.value.get(symbol)
        if ticker is None or ticker.last <= 0:
            return Unknown(f"没有 {symbol} 的最新价")
        return ticker.last

    async def _open_algos(self, symbol: str) -> list[AlgoOrderInfo]:
        result = await self._adapter.fetch_open_algo_orders(symbol)
        if isinstance(result, Unknown):
            raise _Reject(f"条件单读数未知：{result.reason}，放弃开仓")
        return [a for a in result.value if a.symbol == symbol]

    async def _probe_algo(self, symbol: str, cid: str | None, algo_id: str | None) -> str:
        """直接问交易所一张条件单还在不在：``gone`` | ``present`` | ``unknown``。"""
        if cid and _is_system_id(cid):
            result = await self._adapter.fetch_algo_order(symbol, client_algo_id=cid, algo_id=None)
        elif algo_id:
            result = await self._adapter.fetch_algo_order(symbol, client_algo_id=None, algo_id=algo_id)
        else:
            return "unknown"
        if isinstance(result, Unknown):
            return "unknown"
        if result.value is None or not result.value.is_open:
            return "gone"
        return "present"

    async def _cancel_algo(self, run: _Run, symbol: str, cid: str | None, algo_id: str | None, *,
                           primary: bool = False) -> CancelResult:
        """撤一张条件单并把结果记进订单行（有这张订单的话）。系统编号才能按编号撤，别的只能按交易所单号。
        ``primary``：撤单就是本意图的动作（出了意外错误时按「请求已发出」处理）；开仓前清理旧止损不算。"""
        use_cid = cid if cid and _is_system_id(cid) else None
        if primary:
            run.sent = True
        result = await self._adapter.cancel_algo_order(self._cap, symbol=symbol, client_algo_id=use_cid,
                                                       algo_id=algo_id)
        if use_cid:
            try:
                await self._orders.apply_cancel_result(use_cid, result)
            except OrderNotFound:
                pass
        return result

    # ================================================================ OPEN（F1 第 5–9 步）

    async def _open(self, run: _Run) -> None:
        rec = run.rec
        async with self._admission:  # 第一个可能挂起的点：先提交的先进准入段
            await self._guard_replay(run)
            await self._admit(run)
        async with self._key_lock(rec.symbol, rec.side):
            await self._open_keyed(run)

    def _check_can_open(self, rec: IntentRecord) -> None:
        try:
            model = Model(rec.model)
        except ValueError:
            raise _Reject(f"不认识的模型：{rec.model}，不能开仓") from None
        allowed, why = self._modelstate.current().can_open(model)
        if not allowed:
            raise _Reject(why)

    def _check_list(self, rec: IntentRecord) -> None:
        if self._list_check is None or rec.model != Model.M1.value:
            return
        try:
            valid, why = self._list_check(rec.symbol, rec.side)
        except Exception as exc:
            log.exception("名单复核出错")
            raise _Reject(f"名单复核出错：{exc}") from None
        if not valid:
            raise _Reject(f"名单复核未通过：{why}")

    async def _admit(self, run: _Run) -> None:
        """全局串行的准入段：未过期 → 可开仓判定 → 名单 → 订单频率 → 占合约数名额（RESERVED）。"""
        rec = await self._fresh(run)
        now = self._clock.now_ms()
        test_entry = rec.source is IntentSource.TEST_ENTRY
        if test_entry and self._is_live():
            raise _Reject("测试专用入口只能在非实盘环境使用，当前是实盘环境，拒绝开仓")
        if not test_entry:
            self._check_can_open(rec)
            self._check_list(rec)
        if not self._limits.order_rate_ok(rec.symbol, now):
            raise _Reject(f"{rec.symbol} 最近 10 分钟内的订单数已达上限（{self._config_get('max_orders_per_symbol_10m')}"
                          f" 笔），放弃开仓")
        if rec.state is S.CREATED:
            await self._move(run, S.VALIDATED)
        degraded = self._modelstate.current().run_mode is RunMode.POLLING_DEGRADED
        decision = self._limits.try_reserve(rec.id, rec.symbol, rec.side, degraded=degraded, now_ms=now)
        if not decision.allowed:
            raise _Reject(decision.reason)
        if run.rec.state in (S.CREATED, S.VALIDATED):
            try:
                await self._move(run, S.RESERVED)
            except BaseException:
                self._limits.release(rec.id)
                raise

    async def _open_keyed(self, run: _Run) -> None:
        """分键串行段：即时持仓 → 残留的止损类与全平型条件单 → 杠杆与保证金模式 → 风控闸 → 数量 → 先落盘再发市价单。"""
        rec = await self._fresh(run)
        test_entry = rec.source is IntentSource.TEST_ENTRY
        if not test_entry:
            self._check_can_open(rec)  # 排队期间闸可能已经关了
        await self._require_flat(rec)
        await self._clear_residual_algos(run, rec)
        if rec.model == Model.M1.value:
            await self._ensure_leverage(run, rec)
        await self._check_risk(rec)
        qty = await self._open_quantity(rec)
        order = await self._create_order(
            intent=rec, leg=LEG_OPEN, purpose=OrderPurpose.OPEN, kind="market", order_type="MARKET",
            side=rec.side.open_side(), position_side=rec.side, qty=qty)
        await self._ensure_ready(run)
        order, result = await self._transmit(run, order, self._market_sender(order), crash="open_sent")
        await self._after_send(run, order, result, "开仓单")

    async def _require_flat(self, rec: IntentRecord) -> None:
        result = await self._adapter.fetch_positions()
        if isinstance(result, Unknown):
            raise _Reject(f"持仓读数未知：{result.reason}，放弃开仓")
        for side in (PositionSide.LONG, PositionSide.SHORT):
            held = sum((p.qty for p in result.value if p.symbol == rec.symbol and p.side is side), Decimal("0"))
            if held != 0:
                raise _Reject(f"{rec.symbol} 的{side.value}侧已有持仓 {_fmt(held)}，放弃开仓")

    async def _clear_residual_algos(self, run: _Run, rec: IntentRecord) -> None:
        """该合约该方向上不得有会作用到新开的仓上的未触发条件单：止损类（任何形式），以及全平型的（任何类型，含止盈：
        碎仓平仓用的全平型止盈 ``TAKE_PROFIT_MARKET`` 在仓位被别的途径平掉后残留，会在新仓上触发）。
        系统编号的先撤并确认不存在；用户自己的放弃这次开仓并留痕（不替用户撤；止盈单也一样，比报告字面多拦一类，更保守）。"""

        def blocking(algos: list[AlgoOrderInfo]) -> list[AlgoOrderInfo]:
            return [a for a in algos
                    if a.position_side is rec.side and a.is_open and (a.algo_type.is_stop_kind or a.close_position)]

        found = blocking(await self._open_algos(rec.symbol))
        if not found:
            return
        users = [a for a in found if not _is_system_id(a.client_algo_id)]
        if users:
            listed = "、".join(f"{a.algo_type.value} 条件单号 {a.algo_id}（{a.client_algo_id or '无编号'}）"
                              for a in users)
            raise _Reject(f"{rec.symbol} {rec.side.value}侧有用户自己的未触发止损类或全平型条件单（{listed}），放弃开仓："
                          f"仓位归零后残留的止损或全平型（含止盈）条件单会作用到新开的仓上，系统不替用户撤")
        for algo in found:
            await self._cancel_algo(run, rec.symbol, algo.client_algo_id, algo.algo_id)
        left = blocking(await self._open_algos(rec.symbol))
        if left:
            listed = "、".join(a.client_algo_id or a.algo_id for a in left)
            raise _Reject(f"撤销系统条件单后，该方向上仍然存在未触发的止损类或全平型条件单（{listed}），放弃开仓")

    async def _ensure_leverage(self, run: _Run, rec: IntentRecord) -> None:
        """杠杆与保证金模式要与配置一致（只对模型 1）。不一致时只有该合约两侧都无持仓和挂单才设置，否则放弃。
        检查与设置按合约加锁，两个方向并发开仓时只设置一次。"""
        want_leverage = self._config_get("leverage")
        want_mode = self._config_get("margin_mode")
        if want_leverage is None or want_mode is None:
            raise _Reject("杠杆或保证金模式的配置未设置（leverage / margin_mode），放弃开仓")
        async with self._symbol_lock(rec.symbol):
            info = await self._adapter.fetch_leverage(rec.symbol)
            if isinstance(info, Unknown):
                raise _Reject(f"杠杆读数未知：{info.reason}，放弃开仓")
            current = info.value
            if current.leverage == want_leverage and current.margin_mode == want_mode:
                return
            await self._require_flat(rec)
            orders = await self._adapter.fetch_open_orders(rec.symbol)
            if isinstance(orders, Unknown):
                raise _Reject(f"挂单读数未知：{orders.reason}，杠杆与保证金模式不符配置，无法确认可以设置，放弃开仓")
            algos = await self._open_algos(rec.symbol)
            if any(o.status in ("NEW", "PARTIALLY_FILLED") for o in orders.value) or any(a.is_open for a in algos):
                raise _Reject(f"{rec.symbol} 的杠杆或保证金模式与配置不符"
                              f"（交易所 {current.leverage} 倍/{current.margin_mode}，配置 {want_leverage} 倍/{want_mode}），"
                              f"但该合约上有挂单，不能改，放弃开仓")
            if current.margin_mode != want_mode:
                await self._must_accept(await self._adapter.set_margin_mode(self._cap, symbol=rec.symbol,
                                                                            margin_mode=want_mode), "保证金模式")
            if current.leverage != want_leverage:
                await self._must_accept(await self._adapter.set_leverage(self._cap, symbol=rec.symbol,
                                                                         leverage=int(want_leverage)), "杠杆")

    @staticmethod
    async def _must_accept(result: Any, what: str) -> None:
        if isinstance(result, WriteAccepted):
            return
        if isinstance(result, WriteRejected):
            raise _Reject(f"设置{what}被拒绝：{result.message}（错误码 {result.code}），放弃本次开仓")
        raise _Reject(f"设置{what}结果未知（{result.reason}），放弃本次信号")

    async def _check_risk(self, rec: IntentRecord) -> None:
        try:
            decision = await self._riskgate.evaluate(model=rec.model, action=rec.action, now_ms=self._clock.now_ms())
        except Exception as exc:
            log.exception("风控评估出错")
            raise _Reject(f"风控评估出错：{exc}，按不放行处理") from None
        if not decision.allowed:
            raise _Reject(f"风控拒绝：{decision.reason}")

    def _notional(self, rec: IntentRecord) -> Decimal:
        raw = rec.params["notional"] if "notional" in rec.params else self._config_get("trade_notional_usdt")
        try:
            value = Decimal(str(raw)) if raw is not None else None
        except (InvalidOperation, ValueError):
            value = None
        if value is None or not value.is_finite() or value <= 0:
            raise _Reject(f"单笔名义金额未设置或无效（{raw!r}），放弃开仓")
        return value

    async def _open_quantity(self, rec: IntentRecord) -> Decimal:
        """数量 = 单笔名义金额 / 最新价，按（市价单）数量步长向下取整；不足最小数量或最小名义金额放弃。"""
        notional = self._notional(rec)
        price = await self._last_price(rec.symbol)
        if isinstance(price, Unknown):
            raise _Reject(f"最新价读数未知：{price.reason}，放弃开仓")
        inst = self._rules.get(rec.symbol)
        if inst is None:
            raise _Reject(f"合约规则未知（{rec.symbol}），放弃开仓")
        sized = size_order(notional, price, inst.market_step_size or inst.step_size, inst.min_qty, inst.min_notional)
        if not sized.ok:
            raise _Reject(f"开仓数量不满足下单条件：{sized.reason}")
        return sized.qty

    # ================================================================ CLOSE / PARTIAL_CLOSE（F12、F5 第 5–7 步、F4 第 5–9 步）

    async def _close(self, run: _Run, *, partial: bool) -> None:
        rec = run.rec
        async with self._key_lock(rec.symbol, rec.side):
            rec = await self._fresh(run)
            retry = bool(rec.params.get("retry_until_done")) and not partial
            live = await self.live_position(rec.symbol, rec.side)
            if isinstance(live, Unknown):
                if retry:
                    raise _Defer(f"持仓读数未知（{live.reason}），稍后重试")
                raise _Reject(f"持仓读数未知：{live.reason}，未发出平仓单")
            held = live.value
            if held <= 0:
                raise _Finish(S.COMPLETED, "已由其它途径平掉：即时持仓为零，未发出平仓单")
            if partial:
                qty = self._partial_quantity(rec, held)
                await self._ledger.write(repo_coordinator.merge_params, rec.id, {HELD_BEFORE_KEY: format(held, "f")},
                                         now_ms=self._clock.now_ms())
                await self._begin_pause(rec)
            else:
                qty = held
                if await self._is_dust(rec, qty):
                    await self._close_dust(run, qty, retry=retry)
                    return
            await self._close_market(run, qty, held, partial=partial, retry=retry)

    def _partial_quantity(self, rec: IntentRecord, held: Decimal) -> Decimal:
        requested = rec.qty
        if requested is None or requested <= 0:
            raise _Reject("部分平仓缺少有效的请求数量")
        qty = min(requested, held)
        inst = self._rules.get(rec.symbol)
        if inst is not None:
            qty = floor_to_step(qty, inst.market_step_size or inst.step_size)
        if qty <= 0:
            raise _Reject("部分平仓的数量按步长取整后为 0，请改用「平掉此仓」")
        return qty

    async def _is_dust(self, rec: IntentRecord, qty: Decimal) -> bool:
        """碎仓：平仓单名义金额低于最小名义金额，且实测第 2 项的结果是「不豁免」。读不到价格或规则时不下结论
        （照常发市价单，交易所拒绝 −4164 再走碎仓路径）。"""
        if self._config_get("ex_close_min_notional_exempt"):
            return False
        inst = self._rules.get(rec.symbol)
        if inst is None:
            return False
        price = await self._last_price(rec.symbol)
        if isinstance(price, Unknown):
            return False
        return qty * price < inst.min_notional

    async def _close_market(self, run: _Run, qty: Decimal, held: Decimal, *, partial: bool, retry: bool,
                            resend_of: OrderRecord | None = None, reuse: OrderRecord | None = None) -> None:
        """发市价平仓单（买卖方向取反、带持仓方向、不带只减仓标记）。−2022 重读持仓后最多按新读数重发一次；
        −4164 改走碎仓路径（部分平仓不走：全平型条件单会平掉整个仓）。``resend_of`` 是被取代的上一张订单；
        ``reuse`` 是「沿用原编号重发」时的原订单。"""
        rec = run.rec
        base_leg = LEG_PARTIAL if partial else LEG_CLOSE
        purpose = OrderPurpose.PARTIAL_CLOSE if partial else OrderPurpose.CLOSE
        what = "部分平仓单" if partial else "平仓单"
        resent_for_2022 = False
        while True:
            if reuse is not None:
                order, reuse = reuse, None
            else:
                previous = [o for o in await self._orders.for_intent(rec.id) if o.purpose is purpose]
                leg = base_leg if not previous else ids.resend_leg(base_leg, len(previous))
                order = await self._create_order(
                    intent=rec, leg=leg, purpose=purpose, kind="market", order_type="MARKET",
                    side=rec.side.close_side(), position_side=rec.side, qty=qty,
                    resend_of=None if resend_of is None else resend_of.client_order_id)
                if resend_of is not None and resend_of.state in _UNCONFIRMED:
                    await self._orders.mark_rejected(
                        resend_of.client_order_id,
                        reason=f"回查确认交易所没有这张订单，已换新编号 {order.client_order_id} 重发")
                resend_of = None
            await self._ensure_ready(run)
            order, result = await self._transmit(run, order, self._market_sender(order),
                                                 crash="partial_close_sent" if partial else None)
            if order.state is O.REJECTED:
                kind = result.kind if isinstance(result, WriteRejected) else None
                if kind is RejectKind.REDUCE_REJECTED:
                    fresh = await self.live_position(rec.symbol, rec.side)
                    if isinstance(fresh, Unknown):
                        if retry:
                            raise _Defer(f"交易所答复无仓可平（−2022），重读持仓失败（{fresh.reason}），稍后重试")
                        raise _Reject(f"交易所答复无仓可平（−2022），重读持仓失败：{fresh.reason}")
                    if fresh.value <= 0:
                        raise _Finish(S.COMPLETED, "已由其它途径平掉：交易所答复无仓可平（−2022），重读持仓为零")
                    if partial:  # F4 第 9 步：非零才记失败；部分平仓不自动重发，由用户重新预览
                        raise _Reject(f"交易所拒绝{what}（−2022），重读持仓为 {_fmt(fresh.value)}（非零），"
                                      f"部分平仓失败、不自动重发，请重新预览")
                    if resent_for_2022 or fresh.value >= qty:
                        raise _Reject(f"交易所拒绝{what}（−2022），重读持仓为 {_fmt(fresh.value)}，"
                                      f"不是数量超出，失败")
                    qty = fresh.value
                    resent_for_2022, resend_of = True, order
                    continue
                if kind is RejectKind.MIN_NOTIONAL and not partial:
                    await self._close_dust(run, qty, retry=retry, previous=order)
                    return
                raise _Reject(self._reject_text(what, result, order))
            await self._after_send(run, order, result, what)
            return

    async def _close_dust(self, run: _Run, qty: Decimal, *, retry: bool, previous: OrderRecord | None = None) -> None:
        """碎仓：改用全平型止盈条件单，触发价贴近现价（多仓在最新价上方一个最小价位，空仓在下方）。
        下行方向由已有的保护止损覆盖。"""
        rec = run.rec
        price = await self._last_price(rec.symbol)
        inst = self._rules.get(rec.symbol)
        if isinstance(price, Unknown) or inst is None:
            why = price.reason if isinstance(price, Unknown) else f"合约规则未知（{rec.symbol}）"
            if retry:
                raise _Defer(f"碎仓需要最新价与合约规则（{why}），稍后重试")
            raise _Reject(f"碎仓改用全平型条件单需要最新价与合约规则：{why}")
        trigger = self._dust_trigger(price, inst.tick_size, rec.side)
        earlier = [o for o in await self._orders.for_intent(rec.id) if o.purpose is OrderPurpose.DUST_CLOSE]
        leg = LEG_DUST if not earlier else ids.resend_leg(LEG_DUST, len(earlier))
        order = await self._create_order(
            intent=rec, leg=leg, purpose=OrderPurpose.DUST_CLOSE, kind="algo", order_type=AlgoType.TAKE_PROFIT_MARKET.value,
            side=rec.side.close_side(), position_side=rec.side, qty=None, trigger_price=trigger,
            working_type=WorkingType.CONTRACT_PRICE, close_position=True,
            resend_of=None if previous is None else previous.client_order_id)
        if previous is not None and previous.state in _UNCONFIRMED:  # 回查确认不存在之后的重挂
            await self._orders.mark_rejected(
                previous.client_order_id, reason=f"回查确认交易所没有这张碎仓条件单，已换新编号 {order.client_order_id} 重挂")
        # 全平型条件单不带数量：记下此刻的持仓，结果未知时用它判断「持仓数量未变」
        await self._ledger.write(repo_coordinator.merge_params, rec.id, {HELD_BEFORE_KEY: format(qty, "f")},
                                 now_ms=self._clock.now_ms())
        await self._ensure_ready(run)
        order, result = await self._transmit(run, order, self._stop_sender(order, AlgoType.TAKE_PROFIT_MARKET))
        await self._after_send(run, order, result, "碎仓条件单")

    @staticmethod
    def _dust_trigger(price: Decimal, tick: Decimal, side: PositionSide) -> Decimal:
        below = floor_to_step(price, tick)
        if side is PositionSide.LONG:
            trigger = below + tick  # 最新价上方一个最小价位
        else:
            trigger = (below if below == price else below + tick) - tick  # 最新价下方一个最小价位
        if trigger <= 0:
            raise _Reject(f"碎仓条件单的触发价按价格步长 {_fmt(tick)} 算出来不为正（最新价 {_fmt(price)}）")
        return trigger

    # ---------------------------------------------------------------- 部分平仓暂停

    async def _begin_pause(self, rec: IntentRecord) -> None:
        """派发部分平仓时把托管仓置为平仓中（自动动作暂停）；订单到终态或 ``partial_close_pause_s`` 后恢复。"""
        position = (self._registry.by_id(rec.position_id) if rec.position_id is not None
                    else self._registry.get_active(rec.symbol, rec.side))
        if position is None or position.status not in (PositionStatus.MANAGED, PositionStatus.CLOSING):
            return
        try:
            await self._registry.set_closing(position.id, True, cause="partial_close")
        except Exception:
            log.warning("把持仓 %s 置为平仓中失败", position.id, exc_info=True)
            return
        pause_ms = int(self._config_get("partial_close_pause_s")) * 1000
        self._pauses[rec.id] = (position.id, self._clock.now_ms() + pause_ms)

    async def _end_pause(self, intent_id: int) -> None:
        item = self._pauses.pop(intent_id, None)
        if item is None:
            return
        try:
            await self._registry.set_closing(item[0], False, cause="partial_close_resume")
        except Exception:
            log.warning("恢复持仓 %s 的自动动作失败", item[0], exc_info=True)

    async def _release_due_pauses(self) -> None:
        now = self._clock.now_ms()
        for intent_id, (_, until_ms) in list(self._pauses.items()):
            if now >= until_ms:
                await self._end_pause(intent_id)

    # ================================================================ STOP_PLACE（F2 第 2 步）

    def _stop_params(self, params: Any, *, key: str = "trigger_price") -> tuple[Decimal, WorkingType]:
        try:
            trigger = Decimal(str(params[key]))
            working_type = WorkingType(params["working_type"])
        except (KeyError, ValueError, InvalidOperation, TypeError):
            raise _Reject("止损参数无效：需要 trigger_price（正数）与 working_type（CONTRACT_PRICE 或 MARK_PRICE）") from None
        if not trigger.is_finite() or trigger <= 0:
            raise _Reject(f"止损触发价无效：{params[key]!r}")
        return trigger, working_type

    async def _stop_place(self, run: _Run) -> None:
        rec = run.rec
        async with self._key_lock(rec.symbol, rec.side):
            rec = await self._fresh(run)
            trigger, working_type = self._stop_params(rec.params)
            order = await self._create_order(
                intent=rec, leg=LEG_STOP, purpose=OrderPurpose.STOP, kind="algo",
                order_type=AlgoType.STOP_MARKET.value, side=rec.side.close_side(), position_side=rec.side, qty=None,
                trigger_price=trigger, working_type=working_type, close_position=True)
            await self._ensure_ready(run)
            order, result = await self._transmit(run, order, self._stop_sender(order, AlgoType.STOP_MARKET))
            if order.state is O.REJECTED:
                if isinstance(result, WriteRejected) and result.kind is RejectKind.IMMEDIATE_TRIGGER:
                    raise _Reject(IMMEDIATE_TRIGGER_REASON)
                raise _Reject(self._reject_text("止损单", result, order))
            if order.state in _UNCONFIRMED:
                raise _Defer(f"止损单结果未知（{getattr(result, 'reason', '未知')}），按编号回查条件单")
            raise _Finish(S.COMPLETED, "止损条件单已挂上")

    # ================================================================ STOP_REPLACE（F2 第 7 步）

    def _replace_mode(self, rec: IntentRecord) -> str:
        return str(rec.params.get("mode") or self._config_get("ex_stop_update_mode"))

    @staticmethod
    def _old_stop(rec: IntentRecord) -> dict[str, Any]:
        old = rec.params.get("old") or {}
        return {"client_algo_id": old.get("client_algo_id"), "algo_id": old.get("algo_id")}

    @staticmethod
    def _stop_result(outcome: str, mode: str, *, stop: OrderRecord | None = None, in_place: Any = _AUTO,
                     gap_ms: int | None = None, cleanup: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """``in_place`` 不给时按有没有 ``stop`` 推断；``None`` 表示不知道。"""
        result: dict[str, Any] = {
            "outcome": outcome, "mode": mode,
            "stop_in_place": (stop is not None) if in_place is _AUTO else in_place,
            "cleanup": cleanup or [],
        }
        if gap_ms is not None:
            result["gap_ms"] = gap_ms
        if stop is not None:
            result["client_algo_id"] = stop.client_order_id
            result["algo_id"] = stop.algo_id
            result["trigger_price"] = None if stop.trigger_price is None else format(stop.trigger_price, "f")
        return result

    async def _stop_replace(self, run: _Run) -> None:
        rec = run.rec
        async with self._key_lock(rec.symbol, rec.side):
            rec = await self._fresh(run)
            old = rec.params.get("old") or {}
            if not old.get("client_algo_id") and not old.get("algo_id"):
                raise _Reject("移动止损缺少旧止损单的编号（old.client_algo_id 或 old.algo_id）")
            new_trigger, working_type = self._stop_params(rec.params)
            mode = self._replace_mode(rec)
            if mode == "make_before_break":
                await self._replace_make_before_break(run, new_trigger, working_type)
            elif mode == "gap":
                await self._replace_gap(run, new_trigger, working_type)
            else:
                raise _Reject(f"不认识的移动止损更新方式：{mode!r}")

    async def _finish_replace(self, run: _Run, outcome: str, mode: str, reason: str, *, completed: bool = False,
                              **fields: Any) -> None:
        """写结果，再结束意图。只有 ``replaced`` 是 COMPLETED，其它都是 REJECTED（结果里的 ``stop_in_place`` 说明实际情形）。"""
        await self._set_result(run.rec.id, self._stop_result(outcome, mode, **fields))
        if completed:
            raise _Finish(S.COMPLETED, reason)
        raise _Reject(reason)

    async def _replace_gap(self, run: _Run, new_trigger: Decimal, working_type: WorkingType) -> None:
        """先落盘本意图（含新单的订单行）→ 撤旧单（未知则回查，仍在就放弃）→ 崩溃点 → 挂新单；新单被拒按旧价重挂。"""
        rec = run.rec
        old = self._old_stop(rec)
        limit_ms = int(self._config_get("stop_gap_max_s")) * 1000
        new_order = await self._create_order(
            intent=rec, leg=LEG_STOP, purpose=OrderPurpose.STOP, kind="algo", order_type=AlgoType.STOP_MARKET.value,
            side=rec.side.close_side(), position_side=rec.side, qty=None, trigger_price=new_trigger,
            working_type=working_type, close_position=True)
        await self._ensure_ready(run)
        cancel_sent_ms = self._clock.now_ms()  # 空窗从撤单请求发出算起：旧单最早此刻消失，撤单应答的耗时也算在空窗里
        cancel = await self._cancel_algo(run, rec.symbol, old["client_algo_id"], old["algo_id"], primary=True)
        if cancel.outcome is CancelOutcome.UNKNOWN:
            seen = await self._probe_algo(rec.symbol, old["client_algo_id"], old["algo_id"])
            if seen != "gone":
                await self._orders.mark_rejected(new_order.client_order_id, reason="撤旧止损结果未知且未能确认已撤，放弃本次更新")
                in_place = True if seen == "present" else None
                await self._finish_replace(
                    run, OUTCOME_KEPT_OLD, "gap", f"撤旧止损结果未知，回查{'旧止损仍在' if seen == 'present' else '也读不到'}，"
                    f"放弃本次更新，下次再来", in_place=in_place)
        self._crash.hit("stop_cancelled_before_new")
        order, result = await self._transmit(run, new_order, self._stop_sender(new_order, AlgoType.STOP_MARKET))
        if order.state in _UNCONFIRMED:
            raise _Defer(f"新止损单结果未知（{getattr(result, 'reason', '未知')}），按编号回查条件单")
        gap_ms = self._clock.now_ms() - cancel_sent_ms
        if order.state is not O.REJECTED:  # 新单在位
            if gap_ms > limit_ms:
                await self._finish_replace(run, OUTCOME_GAP_EXCEEDED, "gap",
                                           f"从撤旧到新单存在用了 {gap_ms} 毫秒，超过上限 {limit_ms} 毫秒", stop=order,
                                           gap_ms=gap_ms)
            await self._finish_replace(run, OUTCOME_REPLACED, "gap", "新止损单已挂上，旧止损已撤", completed=True,
                                       stop=order, gap_ms=gap_ms)
        # 新单被拒：先看有没有超过无保护间隙的上限，再按旧止损价重挂
        new_why = self._reject_text("新止损单", result, order)
        if gap_ms > limit_ms:
            await self._finish_replace(run, OUTCOME_GAP_EXCEEDED, "gap",
                                       f"{new_why}；撤旧之后已 {gap_ms} 毫秒，超过上限 {limit_ms} 毫秒，不再重试",
                                       in_place=False, gap_ms=gap_ms)
        old_trigger = (rec.params.get("old") or {}).get("trigger_price")
        try:
            restore_trigger = Decimal(str(old_trigger))
            if not restore_trigger.is_finite() or restore_trigger <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            await self._finish_replace(run, OUTCOME_UNPROTECTED, "gap",
                                       f"{new_why}；没有旧止损价可以重挂，该方向现在没有止损", in_place=False,
                                       gap_ms=gap_ms)
        restore = await self._create_order(
            intent=rec, leg=ids.resend_leg(LEG_STOP, 1), purpose=OrderPurpose.STOP, kind="algo",
            order_type=AlgoType.STOP_MARKET.value, side=rec.side.close_side(), position_side=rec.side, qty=None,
            trigger_price=restore_trigger, working_type=working_type, close_position=True,
            resend_of=order.client_order_id)
        restore, result2 = await self._transmit(run, restore, self._stop_sender(restore, AlgoType.STOP_MARKET))
        if restore.state in _UNCONFIRMED:
            raise _Defer(f"{new_why}；按旧止损价重挂的结果未知，按编号回查条件单")
        gap_ms = self._clock.now_ms() - cancel_sent_ms
        if restore.state is O.REJECTED:
            if isinstance(result2, WriteRejected) and result2.kind is RejectKind.IMMEDIATE_TRIGGER:
                await self._finish_replace(run, OUTCOME_STOP_CROSSED, "gap",
                                           f"{new_why}；旧止损价也已被现价越过（−2021），视为止损已触发", in_place=False,
                                           gap_ms=gap_ms)
            await self._finish_replace(run, OUTCOME_UNPROTECTED, "gap",
                                       f"{new_why}；按旧止损价重挂也失败：{self._reject_text('旧价止损单', result2, restore)}",
                                       in_place=False, gap_ms=gap_ms)
        if gap_ms > limit_ms:
            await self._finish_replace(run, OUTCOME_GAP_EXCEEDED, "gap",
                                       f"{new_why}；已按旧止损价重挂，但无保护间隙 {gap_ms} 毫秒超过上限 {limit_ms} 毫秒",
                                       stop=restore, gap_ms=gap_ms)
        await self._finish_replace(run, OUTCOME_RESTORED_OLD, "gap", f"{new_why}；已按旧止损价重新挂上", stop=restore,
                                   gap_ms=gap_ms)

    async def _replace_make_before_break(self, run: _Run, new_trigger: Decimal, working_type: WorkingType) -> None:
        """先挂新单，确认存在后撤旧单；撤旧失败或未知 → 两张都保留，结果里列出待清理的旧单。"""
        rec = run.rec
        old = self._old_stop(rec)
        new_order = await self._create_order(
            intent=rec, leg=LEG_STOP, purpose=OrderPurpose.STOP, kind="algo", order_type=AlgoType.STOP_MARKET.value,
            side=rec.side.close_side(), position_side=rec.side, qty=None, trigger_price=new_trigger,
            working_type=working_type, close_position=True)
        await self._ensure_ready(run)
        order, result = await self._transmit(run, new_order, self._stop_sender(new_order, AlgoType.STOP_MARKET))
        if order.state in _UNCONFIRMED:
            raise _Defer(f"新止损单结果未知（{getattr(result, 'reason', '未知')}），按编号回查条件单")
        if order.state is O.REJECTED:
            await self._finish_replace(run, OUTCOME_KEPT_OLD, "make_before_break",
                                       f"{self._reject_text('新止损单', result, order)}；旧止损保留，本次更新放弃",
                                       in_place=True)
        cancel = await self._cancel_algo(run, rec.symbol, old["client_algo_id"], old["algo_id"], primary=True)
        gone = cancel.outcome in (CancelOutcome.CANCELLED, CancelOutcome.NOT_FOUND)
        reason = "新止损单已挂上，旧止损已撤" if gone else "新止损单已挂上，撤旧止损结果未知，两张都保留，旧单待清理"
        await self._finish_replace(run, OUTCOME_REPLACED, "make_before_break", reason, completed=True, stop=order,
                                   cleanup=[] if gone else [old])

    # ================================================================ STOP_CANCEL / ORDER_CANCEL

    async def _stop_cancel(self, run: _Run) -> None:
        rec = run.rec
        async with self._key_lock(rec.symbol, rec.side):
            rec = await self._fresh(run)
            cid, algo_id = rec.params.get("client_algo_id"), rec.params.get("algo_id")
            if not cid and not algo_id:
                raise _Reject("撤条件单缺少编号（client_algo_id 或 algo_id）")
            await self._ensure_ready(run)
            result = await self._cancel_algo(run, rec.symbol, cid, algo_id, primary=True)
            if result.outcome is CancelOutcome.CANCELLED:
                raise _Finish(S.COMPLETED, "条件单已撤销")
            if result.outcome is CancelOutcome.NOT_FOUND:
                raise _Finish(S.COMPLETED, "条件单已不存在（已撤销或已触发）")
            raise _Defer(f"撤条件单结果未知（{result.message or '无说明'}），按编号回查")

    async def _order_cancel(self, run: _Run) -> None:
        rec = run.rec
        async with self._key_lock(rec.symbol, rec.side):
            rec = await self._fresh(run)
            cid = rec.params.get("client_order_id")
            if not cid:
                raise _Reject("撤单缺少订单编号（client_order_id）")
            await self._ensure_ready(run)
            run.sent = True
            result = await self._adapter.cancel_order(self._cap, symbol=rec.symbol, client_order_id=cid)
            try:
                await self._orders.apply_cancel_result(cid, result)
            except OrderNotFound:
                pass
            if result.outcome is CancelOutcome.CANCELLED:
                raise _Finish(S.COMPLETED, "订单已撤销")
            if result.outcome is CancelOutcome.NOT_FOUND:
                raise _Finish(S.COMPLETED, "订单已不在挂单中（已成交或已撤销）")
            raise _Defer(f"撤单结果未知（{result.message or '无说明'}），按编号回查")

    # ================================================================ 回查之后的判定

    async def _resolve_reconciling(self, rec: IntentRecord) -> None:
        if rec.action is IntentAction.STOP_CANCEL:
            await self._resolve_stop_cancel(rec)
            return
        if rec.action is IntentAction.ORDER_CANCEL:
            await self._resolve_order_cancel(rec)
            return
        orders = await self._orders.for_intent(rec.id)
        purposes = _MARKET_PURPOSES.get(rec.action) or frozenset({OrderPurpose.STOP})
        mine = [o for o in orders if o.purpose in purposes]
        if not mine:
            if rec.action is IntentAction.CLOSE and rec.params.get("retry_until_done"):
                await self._spawn_and_wait(rec, self._retry_close)  # 读不到持仓而延期的平仓：重新派发
            return
        cur = mine[-1]
        if cur.state in _UNCONFIRMED:
            await self._judge_unconfirmed(rec, cur, len(mine))
        elif self._awaiting_retry(rec, cur):
            await self._spawn_and_wait(rec, self._retry_close)
        else:
            await self._evaluate_completion(rec.id)

    @staticmethod
    def _awaiting_retry(rec: IntentRecord, cur: OrderRecord) -> bool:
        """保护性平仓的订单被 −2022 或 −4164 拒绝，之后读不到持仓（或价格）而延期：订单已是 REJECTED，
        但意图要稍后重试，不能按订单的拒绝来结束。"""
        return (rec.action is IntentAction.CLOSE and bool(rec.params.get("retry_until_done"))
                and cur.state is O.REJECTED and cur.error_code in (-2022, -4164))

    async def _retry_close(self, run: _Run) -> None:
        await self._close(run, partial=False)

    @staticmethod
    def _held_before(rec: IntentRecord) -> Decimal | None:
        """派发部分平仓单 / 碎仓条件单时记下的即时持仓（``params.held_before``）；没有记录返回 None。"""
        raw = rec.params.get(HELD_BEFORE_KEY)
        try:
            return None if raw is None else Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _same_id_mark(rec: IntentRecord, cur: OrderRecord) -> dict[str, Any] | None:
        """这张订单沿用原编号重发过的记录（``params.same_id_resend``）；没有返回 None。"""
        mark = rec.params.get(SAME_RESEND_KEY)
        if isinstance(mark, dict) and mark.get("cid") == cur.client_order_id:
            return mark
        return None

    async def _judge_unconfirmed(self, rec: IntentRecord, cur: OrderRecord, attempts: int) -> None:
        """订单还是没得到交易所的确认：只有「请求发出已超过签名时间窗加宽限、连续 3 次不存在、持仓数量未变」才下结论。
        沿用原编号重发过的订单，从最近一次重发算起（重发时间与那之后的「不存在」次数），之前攒下的不算。"""
        now = self._clock.now_ms()
        action = rec.action
        grace = int(self._config_get("order_unknown_grace_s")) if action is IntentAction.OPEN else 0
        live = await self.live_position(rec.symbol, rec.side) if action not in _STOP_ACTIONS else Known(Decimal("0"))
        if isinstance(live, Unknown):
            return  # 读不到持仓：不下结论
        held = live.value
        expected: Decimal | None  # 订单若不存在，该侧持仓应当是多少
        if action is IntentAction.OPEN or action in _STOP_ACTIONS:
            expected = Decimal("0")
        elif action is IntentAction.CLOSE and cur.purpose is not OrderPurpose.DUST_CLOSE:
            expected = cur.qty
        else:  # PARTIAL_CLOSE、碎仓条件单（不带数量）：和派发时记下的持仓比
            expected = self._held_before(rec)
        unchanged = expected is not None and held == expected
        judged = cur
        record = rec.params.get(SAME_RESEND_KEY) if action is IntentAction.CLOSE else None
        resends = int(record.get("count", 0)) if isinstance(record, dict) else 0  # 沿用原编号的重发不新增订单行
        mark = self._same_id_mark(rec, cur) if action is IntentAction.CLOSE else None
        if mark is not None:
            judged = dataclasses.replace(cur, sent_ms=int(mark["at_ms"]),
                                         absent_count=cur.absent_count - int(mark.get("absent_base", 0)))
        if not self._orders.judge_absent(judged, now_ms=now, grace_s=grace, position_unchanged=unchanged):
            if action is IntentAction.CLOSE and held == 0 and self._orders.judge_absent(
                    judged, now_ms=now, grace_s=0, position_unchanged=True):
                await self._orders.mark_rejected(cur.client_order_id, reason="回查确认交易所没有这张平仓单，持仓已为零")
                await self._end_intent(rec.id, _Verdict(S.COMPLETED, "已由其它途径平掉：平仓单回查不存在，持仓已为零"))
            elif (action in _CLOSE_ACTIONS and expected is not None and held != 0 and held != expected
                  and self._orders.judge_absent(judged, now_ms=now, grace_s=grace, position_unchanged=True)):
                # 平仓单已确认不存在（过了签名时间窗、连续 3 次不存在），只差「持仓没变」这一条且持仓非零：按规格不重发，
                # 意图保持 RECONCILING；持仓可能被别的操作改了，也可能这张单其实部分成交过——留给人核对
                self._warn_position_changed(rec, cur, held, expected)
            return
        window = f"{SIGN_WINDOW_MS / 1000:g} 秒签名时间窗" + (f"加 {grace} 秒宽限" if grace else "")
        steady = "" if action in _STOP_ACTIONS else "，持仓没有变化"
        why = f"回查确认交易所没有这张订单：请求发出已超过{window}，连续 {judged.absent_count} 次查到不存在{steady}"
        resend = (action is IntentAction.CLOSE and bool(rec.params.get("resend_on_unknown", True))
                  and attempts + resends <= MAX_CLOSE_RESENDS)
        if not resend:
            await self._orders.mark_rejected(cur.client_order_id, reason=why)
            await self._evaluate_completion(rec.id)
            return
        same = self._config_get("ex_resend_client_id") == "same" and cur.purpose is not OrderPurpose.DUST_CLOSE

        async def job(run: _Run) -> None:
            await self._resend_close(run, cur.client_order_id, why, same=same)

        await self._spawn_and_wait(rec, job)

    def _warn_position_changed(self, rec: IntentRecord, cur: OrderRecord, held: Decimal, expected: Decimal) -> None:
        """未知的平仓单回查确认不存在，但该侧持仓已经变了（非零）：发一条 WARNING，提示人工核对。每个意图只发一次
        （协调者自己记着；通知器的去重键含意图号，另有时间窗去重）。重启之后记忆丢失，可能再发一次。"""
        if rec.id in self._position_changed_warned:
            return
        self._position_changed_warned.add(rec.id)
        if rec.action is IntentAction.PARTIAL_CLOSE:
            what = "部分平仓单"
        elif cur.purpose is OrderPurpose.DUST_CLOSE:
            what = "碎仓条件单"
        else:
            what = "平仓单"
        self._alert(AlertLevel.WARNING, CLOSE_POSITION_CHANGED_KIND,
                    f"意图 {rec.id}（{rec.symbol} {rec.side.value} {rec.action.value}）的{what} {cur.client_order_id} "
                    f"结果未知，回查确认交易所没有这张订单，但该侧持仓已变为 {_fmt(held)}（发出时是 {_fmt(expected)}）："
                    f"按规则不重发，意图保持「回查中」、该键一直算忙。请人工核对该持仓（这张单可能其实部分成交过，"
                    f"也可能是别的操作改了仓位）后处理",
                    key=f"{CLOSE_POSITION_CHANGED_KIND}:{rec.id}")

    async def _resend_close(self, run: _Run, old_cid: str, why: str, *, same: bool) -> None:
        """未知三次之后重发平仓单：重发前再读一次持仓，持仓数量未变才重发，数量取当刻持仓；默认换新编号（实测第 13 项）。
        碎仓条件单（全平型、不带数量）换新编号重挂。沿用原编号时记下重发时间与此刻的「不存在」次数，下一次判定从这里算起。"""
        rec = run.rec
        async with self._key_lock(rec.symbol, rec.side):
            rec = await self._fresh(run)
            old = await self._orders.get(old_cid)
            if old is None or old.state not in _UNCONFIRMED:
                return  # 这期间已经有了结论
            live = await self.live_position(rec.symbol, rec.side)
            if isinstance(live, Unknown):
                return
            if live.value <= 0:
                await self._orders.mark_rejected(old_cid, reason=f"{why}；持仓已为零")
                raise _Finish(S.COMPLETED, "已由其它途径平掉：平仓单回查不存在，持仓已为零")
            dust = old.purpose is OrderPurpose.DUST_CLOSE
            expected = self._held_before(rec) if dust else old.qty
            if expected is None or live.value != expected:
                return  # 持仓数量变了：不重发
            retry = bool(rec.params.get("retry_until_done"))
            if dust:
                await self._close_dust(run, live.value, retry=retry, previous=old)
            elif same:
                record = rec.params.get(SAME_RESEND_KEY)
                count = int(record.get("count", 0)) if isinstance(record, dict) else 0  # 本意图累计的沿用编号重发
                now = self._clock.now_ms()
                await self._ledger.write(repo_coordinator.merge_params, rec.id, {SAME_RESEND_KEY: {
                    "cid": old_cid, "count": count + 1, "at_ms": now, "absent_base": old.absent_count}}, now_ms=now)
                await self._close_market(run, live.value, live.value, partial=False, retry=retry, reuse=old)
            else:
                await self._close_market(run, live.value, live.value, partial=False, retry=retry, resend_of=old)

    async def _resolve_stop_cancel(self, rec: IntentRecord) -> None:
        cid, algo_id = rec.params.get("client_algo_id"), rec.params.get("algo_id")
        seen = await self._probe_algo(rec.symbol, cid, algo_id)
        if seen == "gone":
            await self._end_intent(rec.id, _Verdict(S.COMPLETED, "回查确认条件单已不存在"))
        elif seen == "present" and self._clock.now_ms() - rec.updated_ms > SIGN_WINDOW_MS:
            await self._end_intent(rec.id, _Verdict(S.REJECTED, "撤单请求已过签名时间窗，回查条件单仍然存在"))

    async def _resolve_order_cancel(self, rec: IntentRecord) -> None:
        cid = rec.params.get("client_order_id")
        order = await self._orders.get(cid) if cid else None
        if order is None:
            await self._end_intent(rec.id, _Verdict(S.REJECTED, f"撤单的目标订单 {cid} 不在账本里"))
            return
        if order.state not in O.terminal():
            answer = await self._adapter.fetch_order(rec.symbol, client_order_id=cid)
            if isinstance(answer, Unknown):
                return  # 读不到：不下结论
            if answer.value is None:
                await self._orders.apply_exchange_view(cid, None, source="query")
                await self._end_intent(rec.id, _Verdict(S.COMPLETED, "回查确认订单已不在挂单中"))
                return
            order = await self._orders.apply_exchange_view(cid, answer.value, source="query")
        if order.state in O.terminal():
            await self._end_intent(rec.id, _Verdict(S.COMPLETED, f"回查确认订单已是 {order.state.value}"))
        elif self._clock.now_ms() - rec.updated_ms > SIGN_WINDOW_MS:
            await self._end_intent(rec.id, _Verdict(S.REJECTED, "撤单请求已过签名时间窗，回查订单仍然存在"))

    async def _poll_active(self, rec: IntentRecord) -> None:
        """碎仓条件单挂着等触发：数据流不可用时靠这里轮询（触发后条件单变 FINISHED，订单合并为 FILLED）。"""
        if rec.action is not IntentAction.CLOSE:
            return
        for order in await self._orders.for_intent(rec.id):
            if order.purpose is OrderPurpose.DUST_CLOSE and order.state not in O.terminal():
                await self._orders.requery(order.client_order_id)
