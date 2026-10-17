"""对账（第 5 层）：把交易所的持仓、挂单、条件单与登记簿核对（计划报告 F8、F3 触发条件、4.6 杠杆与持仓上限）。

调度器每 ``reconcile_positions_s`` 秒调 ``reconcile_positions``、每 ``reconcile_orders_s`` 秒调 ``reconcile_orders``。
本模块只读交易所；要撤条件单一律提交 STOP_CANCEL 意图交给协调者，要改登记簿一律经登记簿的公开方法。

**未知绝不当成空**。查询失败或超时是 Unknown：不清登记、不撤保护单、不补挂，只重查。
持仓对账成功 = 服务器时间、全账户持仓、账户权益三项都读到（缺一项这一轮就是「未知」）；全账户查询连续 3 次未知才置
「对账未知」抑制，一次成功即解除。服务器时间读到了就比对时钟偏差，哪怕这一轮持仓读不到（本机时钟偏了时签名请求会被
交易所拒绝，这时最需要它）。每轮持仓对账结束都发布 ``ReconcileCompleted(kind="positions")``，
``status="ok"`` 只代表一轮完整、成功的持仓对账（core.guard 据此结束「数据流切换中」）；挂单对账发布 ``kind="orders"``。

**持仓比对**（一个键 = 合约 + 方向）：

- 交易所有、登记簿没有：该键有系统意图在途（``coordinator.key_busy``），或系统开仓单已成交而成交明细还没入账，就先不判；
  系统开仓单已成交而成交没入账时先补拉这个合约的成交（数据流断着时成交回报到不了，不干等 60 秒一次的补拉），入账后登记簿
  已登记就算解决；
  否则发布 ``UnregisteredPositionFound(trigger="reconcile")`` 交给模型 2 归类（同一数量只报一次，数量变了再报）。
  外部仓不是「对账不一致」。
- 数量不一致：该键在途先不判；否则先补拉这些合约的成交（``ingest.backfill_trades``），等事件总线把成交交给登记簿，
  再读一次持仓复核：一致了（外部成交解释了差异，登记簿在入账时已处理）就算解决；仍不一致而这个键刚有系统意图开始在途、
  或有系统订单已成交而成交明细还没入账（成交接口还没跟上），同在途一样先不判；两次读数之间仓位又变了（含首读为零、
  复核却是别的数量，首读有仓、复核为零），这一轮不下结论；
  否则就是无法解释：记差异、登记仓置「对账不一致」（须人工确认才解除，超过 ``reconcile_mismatch_stop_s`` 自动关闸）。
  待确认仓与外部仓的差异只记录。
- 登记仓在交易所为零：每次零读数都补拉成交（成交记录显示过零，登记簿按成交平掉）；连续 3 次明确的零且复核读数仍为零，
  才判已平（出场原因「未知」）。未知的一轮打断连续计数。
- 补拉没成（读成交返回未知、补拉出错或没补完——``backfill_trades`` 返回的 ``failed`` 里有这个合约；读失败不是「没有新成交」）、
  补拉后的复核读数读不到、核对期间仓位在变、查不清系统成交是否已入账、或事件总线迟迟处理不完：
  这一轮对这些键不下结论，整轮记为 ``unknown``
  （不算完整对账，不会让 core.guard 结束「数据流切换中」），但不累计「连续未知」的次数（读本身是成功的）。

**挂单与条件单**：普通挂单与条件单分两次查询，任一未知这一轮就不下任何结论；成功时更新合约数上限的快照、发布
``OrdersSnapshotTaken``（时间取查询发出的时间：保护单管理据此判断快照是否早于它最近一次挂/换止损）、处理待清理的旧止损
（还在交易所就撤，已不在就从登记里移走）、清孤儿条件单、回查状态未知的订单。
孤儿条件单（F8）五条同时满足才经协调者 STOP_CANCEL 撤：带系统前缀；登记簿里没有活动持仓、在途意图引用它；没有被标为已移交；
建立已超过一个条件单对账周期；该合约该方向在最近一次（新鲜的）持仓快照里明确为零。持仓非零或未知时只告警。
零读数要真能说明这张单没有在保护仓位：早于这张单建立的零读数、或登记簿仍认为这一侧有仓时的零读数都不作数，先等（不告警）。
订单行停在 CANCEL_PENDING 而交易所仍挂着的条件单视为仍然有效：以交易所的快照为准，不看订单行。
靠系统止损保护的登记仓，它登记的当前止损不在这份快照里时（例如被人手动撤了、正等保护单管理补挂），旧止损可能是它
唯一的止损：先留着，等当前止损回到交易所再撤；外部保护的登记仓，这份快照里这一侧看不到用户的止损类条件单时同样先留着。

**其它**：服务器时间偏差超过 ``clock_skew_max_ms`` 置「时钟偏差」并告警，回到阈值内解除；强平价比止损价更靠近现价告警。

撤单意图的幂等键是 ``orphan:<条件单号>`` / ``stale:<持仓编号>:<条件单号>``；之前的尝试已结束而条件单还在时加序号
（``:1``…），最多 ``MAX_CANCEL_ATTEMPTS`` 次，试满就告警、不再自动尝试。上一次尝试还没结束就不另起一次；但它若落了盘、
超过一个条件单对账周期还停在未派发的状态（多半是提交后进程被杀：协调者重启后不会自己派发没有订单行的意图，
等提交者重新提交），就用同一个幂等键再交给协调者一次。提交之前再核对一次登记簿：准备期间这张单被认领了就不撤。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import (
    AccountSnapshotTaken,
    EventBus,
    OrdersSnapshotTaken,
    PositionSnapshotTaken,
    ReconcileCompleted,
    UnregisteredPositionFound,
)
from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.types import (
    AccountInfo,
    AlertLevel,
    AlgoOrderInfo,
    ExitReason,
    Gate,
    IntentAction,
    IntentSource,
    IntentState,
    OrderOrigin,
    PositionInfo,
    PositionSide,
    PositionStatus,
    ProtectionState,
    Suppression,
    Tri,
    Unknown,
)
from dmr_kernel.exchange import ids
from dmr_kernel.ledger import repo_reconcile
from dmr_kernel.ledger.records import PositionRecord
from dmr_kernel.ledger.repo_reconcile import QTY_MISMATCH, UNREGISTERED, DiffSpec, ResolveSpec

if TYPE_CHECKING:  # 只用于类型标注：这些协作者都在更低的层，运行时由装配层传进来
    from dmr_kernel.alerts.notifier import Notifier
    from dmr_kernel.core.coordinator import Coordinator
    from dmr_kernel.core.ingest import Ingest
    from dmr_kernel.core.limits import Limits
    from dmr_kernel.core.modelstate import ModelStateService
    from dmr_kernel.core.orders import OrderService
    from dmr_kernel.core.registry import Registry
    from dmr_kernel.exchange.adapter import ExchangeAdapter
    from dmr_kernel.ledger.writer import Ledger

__all__ = ["Reconciler"]

log = logging.getLogger(__name__)

Key = tuple[str, PositionSide]
ZERO = Decimal("0")
# 意图还没派发出去的状态（协调者的 PRE_SEND）：落了盘、还没有任何请求的结果
_UNDISPATCHED = frozenset({IntentState.CREATED, IntentState.VALIDATED, IntentState.RESERVED, IntentState.DISPATCH_READY})


@dataclass(frozen=True)
class _Bundle:
    """一轮持仓对账读到的东西：服务器时间、全账户持仓、账户。"""

    server_ms: int
    positions: list[PositionInfo]
    account: AccountInfo
    taken_ms: int


@dataclass(frozen=True)
class _Snapshot:
    at_ms: int
    by_key: dict[Key, PositionInfo]
    server_ms: int  # 读持仓之前读到的服务器时间：持仓读数反映的是这之后的情况（与条件单的建立时间同一个时钟）


@dataclass(frozen=True)
class _DiffInfo:
    local_qty: Decimal | None
    exchange_qty: Decimal | None


@dataclass
class _Suspect:
    rec: PositionRecord
    seen: PositionInfo | None  # 首读时交易所的这一侧；None = 明确为零


@dataclass
class _Findings:
    diffs: list[DiffSpec] = field(default_factory=list)  # 要写的差异（新增或有变化的）
    resolves: list[ResolveSpec] = field(default_factory=list)
    alerts: list[DiffSpec] = field(default_factory=list)  # 登记仓上新发现（或有变化）的无法解释的差异
    mismatch: bool = False
    undecided: int = 0
    notes: list[str] = field(default_factory=list)  # 值得留痕的事（判已平、差异被成交解释……）
    deferred: list[str] = field(default_factory=list)  # 因为在途等原因先不判的键：只在要写记录时附上，不单独触发记录


class Reconciler:
    UNKNOWN_STREAK = 3  # 全账户查询连续这么多次未知才置「对账未知」
    ZERO_STREAK = 3  # 连续这么多次明确的零持仓才判已平
    FRESH_PERIODS = 2  # 持仓快照最多算新鲜这么多个持仓对账周期（与合约数上限的过期口径一致）
    MAX_CANCEL_ATTEMPTS = 5  # 同一张条件单的撤单意图最多提交这么多次
    DRAIN_TIMEOUT_S = 5.0  # 等事件总线把补拉的成交交给登记簿的上限（真实秒）
    FILL_GAP_LOOKBACK_MS = 3_600_000  # 查「系统成交还没入账」时往前看多久的订单

    def __init__(self, *, adapter: "ExchangeAdapter", registry: "Registry", modelstate: "ModelStateService",
                 coordinator: "Coordinator", ingest: "Ingest", orders: "OrderService", limits: "Limits",
                 ledger: "Ledger", bus: EventBus, clock: Clock, config_get: Callable[[str], Any],
                 notifier: "Notifier", instance: str | None = None) -> None:
        self._adapter = adapter
        self._registry = registry
        self._modelstate = modelstate
        self._coordinator = coordinator
        self._ingest = ingest
        self._orders = orders
        self._limits = limits
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._config_get = config_get
        self._notifier = notifier
        self._instance = instance  # 给了就只认本实例的系统编号；不给则任何系统前缀都算
        self._pos_lock = asyncio.Lock()
        self._ord_lock = asyncio.Lock()
        self._loaded = False
        self._unknown_streak = 0
        self._orders_unknown_streak = 0
        self._zero: dict[int, int] = {}  # 登记记录编号 -> 连续明确零读数的次数
        self._reported: dict[Key, Decimal] = {}  # 已经报告过的未登记持仓（只在内存里：重启后会再报一次）
        self._diffs: dict[tuple[str, PositionSide | None, str], _DiffInfo] = {}  # 未解决的差异（与账本同步）
        self._snapshot: _Snapshot | None = None  # 最近一次成功的持仓快照
        self._last_pos_ok = False  # 最近一次持仓对账是否成功
        self._skew_ms: int | None = None
        self._last: dict[str, tuple[int, str]] = {}
        self._ok_ms: dict[str, int] = {}

    # ================================================================ 公开接口

    async def reconcile_positions(self) -> str:
        """一轮持仓对账，返回 ``"ok"`` | ``"unknown"`` | ``"mismatch"``。"""
        async with self._pos_lock:
            await self._ensure_loaded()
            started = self._clock.now_ms()
            await self._enforce_mismatch_deadline(started)
            skew_ms, bundle = await self._read_bundle()
            if skew_ms is not None:  # 服务器时间读到了就比对，哪怕持仓读不到（时钟偏了时签名请求正会被拒）
                await self._apply_clock_skew(skew_ms)
            if isinstance(bundle, Unknown):
                return await self._positions_unknown(bundle, started)
            return await self._positions_ok(bundle, started)

    async def reconcile_orders(self) -> str:
        """一轮挂单与条件单对账，返回 ``"ok"`` | ``"unknown"``。"""
        async with self._ord_lock:
            await self._ensure_loaded()
            started = self._clock.now_ms()
            open_orders, algos = await asyncio.gather(
                self._safe(self._adapter.fetch_open_orders, "挂单"),
                self._safe(self._adapter.fetch_open_algo_orders, "条件单"))
            if isinstance(open_orders, Unknown) or isinstance(algos, Unknown):
                return await self._orders_unknown(open_orders, algos, started)
            return await self._orders_ok(list(open_orders.value), list(algos.value), started)

    async def reconcile_key(self, symbol: str, side: PositionSide) -> Tri:
        """对一个「合约 + 方向」即时读一次交易所并做同样的比对；返回交易所这一侧的存在性（读不到是未知）。
        不发布整轮级别的事件、不动合约数上限的快照、不累计连续未知与连续零读数。"""
        key: Key = (symbol, PositionSide(side))
        async with self._pos_lock:
            await self._ensure_loaded()
            started = self._clock.now_ms()
            result = await self._safe(self._adapter.fetch_positions, "持仓")
            if isinstance(result, Unknown):
                return Tri.UNKNOWN
            now = self._clock.now_ms()
            exchange = {(p.symbol, p.side): p for p in result.value if p.qty > 0}
            findings = await self._compare(exchange, now, keys={key}, count_zero=False)
            await self._commit("key", "mismatch" if findings.mismatch else "ok", started, now, findings, [])
            return Tri.EXISTS if key in exchange else Tri.ABSENT

    async def confirm_mismatch(self, *, operator: str) -> bool:
        """人工确认「对账不一致」：解除抑制、清掉不一致的起算时间、把未解决的数量差异记为已解决，并留审计记录。
        返回当时是否真有「对账不一致」可确认。差异若仍在，下一轮对账会重新发现并重新置抑制。"""
        async with self._pos_lock:
            await self._ensure_loaded()
            if Suppression.RECONCILE_MISMATCH not in self._modelstate.current().suppressions:
                return False
            now = self._clock.now_ms()
            await self._ledger.write(repo_reconcile.confirm_mismatch, at_ms=now, operator=operator)
            for key in [k for k in self._diffs if k[2] == QTY_MISMATCH]:
                del self._diffs[key]
            await self._unsuppress(Suppression.RECONCILE_MISMATCH, reason=f"人工确认（{operator}）")
            await self._modelstate.set_mismatch_since(None)
            self._alert(AlertLevel.INFO, "reconcile_mismatch_confirmed", f"「对账不一致」已由 {operator} 人工确认解除")
            return True

    def status(self) -> dict[str, Any]:
        """最近一次时间与结果、连续未知次数、差异列表；可直接转 JSON。"""
        try:
            state = self._modelstate.current()
            suppressions, mismatch_since = set(state.suppressions), state.mismatch_since_ms
        except RuntimeError:  # 模型状态还没加载
            suppressions, mismatch_since = set(), None
        positions, orders = self._last.get("positions"), self._last.get("orders")
        diffs = [{"symbol": symbol, "side": None if side is None else side.value, "kind": kind,
                  "local_qty": None if info.local_qty is None else str(info.local_qty),
                  "exchange_qty": None if info.exchange_qty is None else str(info.exchange_qty)}
                 for (symbol, side, kind), info in sorted(self._diffs.items(), key=lambda kv: (kv[0][0], str(kv[0][1]), kv[0][2]))]
        return {
            "positions_at_ms": None if positions is None else positions[0],
            "positions_status": None if positions is None else positions[1],
            "positions_ok_ms": self._ok_ms.get("positions"),
            "orders_at_ms": None if orders is None else orders[0],
            "orders_status": None if orders is None else orders[1],
            "orders_ok_ms": self._ok_ms.get("orders"),
            "unknown_streak": self._unknown_streak,
            "orders_unknown_streak": self._orders_unknown_streak,
            "reconcile_unknown": Suppression.RECONCILE_UNKNOWN in suppressions,
            "reconcile_mismatch": Suppression.RECONCILE_MISMATCH in suppressions,
            "mismatch_since_ms": mismatch_since,
            "clock_skew_ms": self._skew_ms,
            "clock_skew_exceeded": Suppression.CLOCK_SKEW in suppressions,
            "diffs": diffs,
        }

    # ================================================================ 持仓对账：读

    async def _read_bundle(self) -> tuple[int | None, _Bundle | Unknown]:
        """服务器时间先读（取往返的中点算偏差，也是快照的「截止时间」），再同时读全账户持仓与账户权益。
        返回（与服务器时间的偏差，读不到服务器时间是 None；这一轮的读数，缺任何一项就是 Unknown）。"""
        sent = self._clock.now_ms()
        server = await self._safe(self._adapter.server_time, "服务器时间")
        received = self._clock.now_ms()
        skew_ms = None if isinstance(server, Unknown) else server.value - (sent + received) // 2
        positions, account = await asyncio.gather(self._safe(self._adapter.fetch_positions, "持仓"),
                                                  self._safe(self._adapter.fetch_account, "账户权益"))
        failed = [f"{name}（{r.reason}）" for name, r in
                  (("服务器时间", server), ("持仓", positions), ("账户权益", account)) if isinstance(r, Unknown)]
        if failed:
            return skew_ms, Unknown("、".join(failed))
        return skew_ms, _Bundle(server_ms=server.value, positions=list(positions.value), account=account.value,
                                taken_ms=self._clock.now_ms())

    async def _safe(self, call: Callable[[], Awaitable[Any]], what: str) -> Any:
        """适配层的读方法失败返回 Unknown；万一抛出意外的异常也一样当作未知（取消照常往外传）。"""
        try:
            return await call()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("对账读取%s出错：%r", what, exc, exc_info=True)
            return Unknown(f"读取{what}出错：{exc!r}")

    # ================================================================ 持仓对账：未知与成功

    async def _positions_unknown(self, why: Unknown, started: int) -> str:
        now = self._clock.now_ms()
        self._unknown_streak += 1
        self._zero.clear()  # 没读到不是零：连续零读数的计数被打断
        self._last_pos_ok = False
        notes = [f"持仓对账读取失败（连续第 {self._unknown_streak} 次）：{why.reason}"]
        log.warning("%s", notes[0])
        if self._unknown_streak >= self.UNKNOWN_STREAK:
            reason = f"全账户查询连续 {self._unknown_streak} 次未知：{why.reason}"
            if await self._suppress(Suppression.RECONCILE_UNKNOWN, reason=reason):
                self._alert(AlertLevel.WARNING, "reconcile_unknown",
                            f"持仓对账连续 {self._unknown_streak} 次读不到（{why.reason}），已置「对账未知」：暂不新开仓，继续重查",
                            dedupe_key="reconcile_unknown")
        await self._commit("positions", "unknown", started, now, _Findings(), notes)
        self._bus.publish(ReconcileCompleted(kind="positions", status="unknown", at_ms=self._clock.now_ms()))
        return "unknown"

    async def _positions_ok(self, b: _Bundle, started: int) -> str:
        now = b.taken_ms
        notes: list[str] = []
        self._unknown_streak = 0
        self._last_pos_ok = True
        if await self._unsuppress(Suppression.RECONCILE_UNKNOWN, reason="持仓对账读取恢复"):
            self._alert(AlertLevel.INFO, "reconcile_unknown_cleared", "持仓对账读取已恢复，「对账未知」解除")
        exchange = {(p.symbol, p.side): p for p in b.positions if p.qty > 0}
        self._snapshot = _Snapshot(now, exchange, b.server_ms)
        self._limits.update_positions(b.positions, now)
        findings = await self._compare(exchange, now, keys=None, count_zero=True)
        self._check_liquidation(exchange)
        # ok 只代表一轮完整、成功的持仓对账：有可疑的键没能复核清楚（补拉失败、复核读数读不到、总线处理不完）就不算
        status = "mismatch" if findings.mismatch else "unknown" if findings.undecided else "ok"
        await self._commit("positions", status, started, now, findings, notes)
        self._bus.publish(PositionSnapshotTaken(positions=tuple(p for p in b.positions if p.qty > 0),
                                                server_time_ms=b.server_ms, at_ms=now))
        self._bus.publish(AccountSnapshotTaken(equity=b.account.equity, available=b.account.available_balance,
                                               wallet=b.account.wallet_balance, at_ms=now))
        self._bus.publish(ReconcileCompleted(kind="positions", status=status, at_ms=self._clock.now_ms()))
        return status

    async def _apply_clock_skew(self, skew_ms: int) -> None:
        self._skew_ms = skew_ms
        limit = int(self._config_get("clock_skew_max_ms"))
        if abs(skew_ms) > limit:
            if await self._suppress(Suppression.CLOCK_SKEW, reason=f"与交易所服务器时间相差 {skew_ms} 毫秒（上限 {limit}）"):
                self._alert(AlertLevel.WARNING, "clock_skew",
                            f"本机与交易所服务器时间相差 {skew_ms} 毫秒，超过上限 {limit} 毫秒，已置「时钟偏差」：暂不新开仓；"
                            "请校准本机时钟")
        elif await self._unsuppress(Suppression.CLOCK_SKEW, reason=f"时间偏差 {skew_ms} 毫秒，回到阈值内"):
            self._alert(AlertLevel.INFO, "clock_skew_cleared", f"本机与交易所服务器时间的偏差回到 {skew_ms} 毫秒，「时钟偏差」解除")

    async def _enforce_mismatch_deadline(self, now: int) -> None:
        """「对账不一致」超过 reconcile_mismatch_stop_s 秒没人确认，自动关闸（不管这一轮交易所读不读得到）。"""
        state = self._modelstate.current()
        if (Suppression.RECONCILE_MISMATCH not in state.suppressions or state.mismatch_since_ms is None
                or state.gate is not Gate.RUNNING):
            return
        limit_s = int(self._config_get("reconcile_mismatch_stop_s"))
        if now - state.mismatch_since_ms < limit_s * 1000:
            return
        await self._modelstate.set_gate(Gate.STOPPED, reason=f"对账不一致超过 {limit_s} 秒未解决，自动关闸")
        self._alert(AlertLevel.CRITICAL, "reconcile_mismatch_stop",
                    f"「对账不一致」已超过 {limit_s} 秒没有解决，开仓闸已自动关闭；请核对持仓后人工确认",
                    dedupe_key="reconcile_mismatch_stop")

    # ================================================================ 持仓比对

    async def _compare(self, exchange: dict[Key, PositionInfo], now: int, *, keys: set[Key] | None,
                       count_zero: bool) -> _Findings:
        f = _Findings()
        suspects: list[_Suspect] = []
        for rec in self._registry.all_active():
            key = (rec.symbol, rec.side)
            if keys is not None and key not in keys:
                continue
            seen = exchange.get(key)
            if seen is None:  # 读成功了而这一侧没有仓：明确的零
                if count_zero:
                    self._zero[rec.id] = self._zero.get(rec.id, 0) + 1
                await self._record_view(rec, ZERO, None, now)
                suspects.append(_Suspect(rec, None))
                continue
            self._zero.pop(rec.id, None)
            await self._record_view(rec, seen.qty, seen.liquidation_price, now)
            if seen.qty == rec.qty:
                continue
            if self._coordinator.key_busy(*key):
                f.deferred.append(f"{rec.symbol} {rec.side.value} 数量对不上（登记 {rec.qty}，交易所 {seen.qty}），"
                                  "有系统意图在途，先不判")
            elif self._persisting(rec, seen.qty):
                f.mismatch |= rec.status in PositionStatus.registered()  # 同样的差异上一轮已查过：不重复补拉
            else:
                suspects.append(_Suspect(rec, seen))
        latest = dict(exchange)
        if suspects:
            await self._settle(suspects, now, f, latest)
        await self._report_unregistered(exchange, now, keys, f)
        self._sweep_diffs(latest, keys, f)
        if keys is None:
            for rec_id in [i for i in self._zero if self._registry.by_id(i) is None]:
                del self._zero[rec_id]
        return f

    def _persisting(self, rec: PositionRecord, exchange_qty: Decimal) -> bool:
        known = self._diffs.get((rec.symbol, rec.side, QTY_MISMATCH))
        return known is not None and known.local_qty == rec.qty and known.exchange_qty == exchange_qty

    async def _settle(self, suspects: list[_Suspect], now: int, f: _Findings, latest: dict[Key, PositionInfo]) -> None:
        """可疑的键：补拉成交 → 等事件总线交给登记簿 → 再读一次持仓复核 → 判定。任何一步没做成就不下结论。"""
        failed: set[str] = set()
        for symbol in dict.fromkeys(s.rec.symbol for s in suspects):
            try:
                result = await self._ingest.backfill_trades([symbol])
            except Exception:
                log.exception("补拉成交失败：%s", symbol)
                failed.add(symbol)
            else:
                # 读成交返回未知、补拉出错、没补完：不是「没有新成交」（只返回笔数的替身没有 failed，按没有失败算）
                if symbol in getattr(result, "failed", ()):
                    failed.add(symbol)
            if symbol in failed:
                f.notes.append(f"{symbol} 补拉成交没有成功（读成交失败、补拉出错或没补完），这一轮不对它的持仓下结论")
        if not await self._drain():
            f.undecided += len(suspects)
            return
        fresh = await self._safe(self._adapter.fetch_positions, "持仓（复核）")
        if isinstance(fresh, Unknown):
            f.undecided += len(suspects)
            f.notes.append(f"补拉成交后的复核读数失败：{fresh.reason}")
            return
        again = {(p.symbol, p.side): p for p in fresh.value if p.qty > 0}
        for s in suspects:
            key = (s.rec.symbol, s.rec.side)
            now_rec = self._registry.get_active(*key)
            if again.get(key) is None:
                latest.pop(key, None)
            else:
                latest[key] = again[key]
            if s.rec.symbol in failed:  # 这个合约的成交没补拉成：不拿没跟上的登记簿去下结论
                f.undecided += 1
                continue
            if now_rec is None or now_rec.id != s.rec.id:  # 补拉的成交已经把它平掉了（登记簿按成交处理）
                self._zero.pop(s.rec.id, None)
                f.notes.append(f"{key[0]} {key[1].value} 已由补拉的成交平掉")
                continue
            info = again.get(key)
            if info is None:
                await self._judge_zero(s, now_rec, now, f)
            elif s.seen is None:
                self._zero.pop(now_rec.id, None)  # 首读为零、复核又有了：读数在跳，不判已平
                await self._record_view(now_rec, info.qty, info.liquidation_price, now)
                if info.qty != now_rec.qty:  # 而且与登记对不上：仓位正在变，这一轮不下结论（不算完整的一轮）
                    f.undecided += 1
                    f.notes.append(f"{key[0]} {key[1].value} 核对期间仓位在变（0 → {info.qty}，登记 {now_rec.qty}），"
                                   "这一轮不下结论")
            else:
                await self._judge_quantity(now_rec, s.seen, info, now, f)

    async def _judge_zero(self, s: _Suspect, rec: PositionRecord, now: int, f: _Findings) -> None:
        if s.seen is not None:  # 首读有仓、复核没有：仓位正在变，这一轮不下结论（不算完整的一轮），下一轮再看
            await self._record_view(rec, ZERO, None, now)
            f.undecided += 1
            f.notes.append(f"{rec.symbol} {rec.side.value} 核对期间仓位在变（{s.seen.qty} → 0，登记 {rec.qty}），"
                           "这一轮不下结论")
            return
        streak = self._zero.get(rec.id, 0)
        if streak < self.ZERO_STREAK:
            return
        try:
            await self._registry.mark_closed(rec.id, exit_reason=ExitReason.UNKNOWN, closed_ms=now)
        except (ValueError, LookupError):
            log.warning("对账判已平时登记记录 %s 已不可改", rec.id, exc_info=True)
            return
        self._zero.pop(rec.id, None)
        message = (f"{rec.symbol} {rec.side.value} 登记仓（编号 {rec.id}）在交易所连续 {streak} 次明确为零，"
                   "补拉成交也没有解释，已按已平处理（出场原因未知）")
        f.notes.append(message)
        # 登记仓（托管中、接管中、平仓中）悄悄没了值得提醒；待确认仓与外部仓（用户自己的仓）没了只是知会一声
        level = AlertLevel.WARNING if rec.status in PositionStatus.registered() else AlertLevel.INFO
        self._alert(level, "position_closed_by_reconcile", message)

    async def _judge_quantity(self, rec: PositionRecord, seen: PositionInfo, info: PositionInfo, now: int,
                              f: _Findings) -> None:
        """``seen`` 是首读、``info`` 是补拉成交之后的复核读数，``rec`` 是补拉之后的登记。"""
        await self._record_view(rec, info.qty, info.liquidation_price, now)
        if rec.qty == info.qty:
            f.notes.append(f"{rec.symbol} {rec.side.value} 数量差异由补拉的成交解释，登记簿已更新为 {rec.qty}")
            return
        if info.qty != seen.qty:  # 两次读数之间仓位又变了：登记簿可能还没跟上，这一轮不下结论（不算完整的一轮）
            f.undecided += 1
            f.notes.append(f"{rec.symbol} {rec.side.value} 核对期间仓位在变（{seen.qty} → {info.qty}，登记 {rec.qty}），"
                           "这一轮不下结论")
            return
        if self._coordinator.key_busy(rec.symbol, rec.side):  # 核对期间这个键上开始有系统意图在途：同在途一样先不判
            f.deferred.append(f"{rec.symbol} {rec.side.value} 数量对不上（登记 {rec.qty}，交易所 {info.qty}），"
                              "核对期间有系统意图开始在途，先不判")
            return
        pending = await self._fills_pending((rec.symbol, rec.side), now)
        if pending is None:
            f.undecided += 1
            f.notes.append(f"{rec.symbol} {rec.side.value} 数量对不上，查不清系统成交是否已入账，先不判")
            return
        if pending:  # 系统订单已成交、成交明细还没入账（成交接口还没跟上）：差异多半是它，同在途一样先不判
            f.deferred.append(f"{rec.symbol} {rec.side.value} 数量对不上（登记 {rec.qty}，交易所 {info.qty}），"
                              "有系统订单已成交而成交明细还没入账，先不判")
            return
        registered = rec.status in PositionStatus.registered()
        spec = DiffSpec(rec.symbol, rec.side, QTY_MISMATCH, rec.qty, info.qty,
                        f"登记 {rec.qty}，交易所 {info.qty}，补拉成交后仍无法解释")
        fresh = self._want_diff(f, spec)
        if registered:
            f.mismatch = True
            if fresh:
                f.alerts.append(spec)

    async def _record_view(self, rec: PositionRecord, qty: Decimal, liquidation: Decimal | None, now: int) -> None:
        try:
            await self._registry.update_exchange_view(rec.id, exchange_qty=qty, liquidation_price=liquidation,
                                                      seen_ms=now)
        except (ValueError, LookupError):
            log.debug("记录 %s 已不可更新交易所视图（可能刚刚平掉）", rec.id, exc_info=True)

    async def _drain(self) -> bool:
        try:
            await self._bus.drain(self.DRAIN_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("事件总线 %s 秒内没有处理完，这一轮对账对可疑的键不下结论", self.DRAIN_TIMEOUT_S)
            return False
        return True

    # ---------------------------------------------------------------- 未登记持仓

    async def _report_unregistered(self, exchange: dict[Key, PositionInfo], now: int, keys: set[Key] | None,
                                   f: _Findings) -> None:
        backfilled: set[str] = set()
        for key, info in exchange.items():
            if keys is not None and key not in keys:
                continue
            if self._registry.get_active(*key) is not None:
                self._reported.pop(key, None)
                continue
            if self._reported.get(key) == info.qty:
                continue
            if self._coordinator.key_busy(*key):
                f.deferred.append(f"{key[0]} {key[1].value} 有系统意图在途，先不判外部")
                continue
            pending = await self._fills_pending(key, now)
            if pending and key[0] not in backfilled:
                # 系统订单已成交而成交明细没入账（数据流断着时成交回报到不了）：先补拉这个合约的成交，让登记簿按成交登记，
                # 不干等 60 秒一次的补拉（报告 4.11：数据流断着，成交后 30 秒内也要挂上止损）
                backfilled.add(key[0])
                if await self._backfill_for_unregistered(key[0], f):
                    if self._registry.get_active(*key) is not None:
                        self._reported.pop(key, None)
                        continue
                    pending = await self._fills_pending(key, now)
            if pending is None:  # 查不清不是「没有」：不能因此把系统自己的仓当成外部仓
                f.undecided += 1
                f.notes.append(f"{key[0]} {key[1].value} 查不清系统成交是否已入账，先不判外部")
                continue
            if pending:
                f.deferred.append(f"{key[0]} {key[1].value} 有系统订单已成交而成交明细还没入账，先不判外部")
                continue
            self._bus.publish(UnregisteredPositionFound(symbol=key[0], side=key[1], qty=info.qty,
                                                        entry_price=info.entry_price, trigger="reconcile", at_ms=now))
            self._reported[key] = info.qty
            self._want_diff(f, DiffSpec(key[0], key[1], UNREGISTERED, None, info.qty, "交易所有仓而登记簿没有登记，等待归类"))
        gone = [k for k in self._reported if (keys is None or k in keys) and k not in exchange]
        for key in gone:
            del self._reported[key]

    async def _backfill_for_unregistered(self, symbol: str, f: _Findings) -> bool:
        """补拉一个合约的成交并等事件总线交给登记簿；补拉没成或总线没排空返回 False（照旧先不判）。"""
        try:
            result = await self._ingest.backfill_trades([symbol])
        except Exception:
            log.exception("补拉成交失败：%s", symbol)
            return False
        if symbol in getattr(result, "failed", ()):
            f.notes.append(f"{symbol} 补拉成交没有成功（读成交失败、补拉出错或没补完）")
            return False
        return await self._drain()

    async def _fills_pending(self, key: Key, now: int) -> bool | None:
        """该键上有没有系统订单已成交而成交明细还没入账；查不出来返回 None（未知，不能当成「没有」）。"""
        try:
            return await self._ledger.read(repo_reconcile.system_fills_pending, key[0], key[1],
                                           since_ms=now - self.FILL_GAP_LOOKBACK_MS)
        except Exception:
            log.exception("查询系统成交是否已入账失败：%s", key)
            return None

    # ---------------------------------------------------------------- 差异的登记

    def _want_diff(self, f: _Findings, spec: DiffSpec) -> bool:
        """差异是新的或数量变了才需要写；同样的差异不重复写。"""
        known = self._diffs.get((spec.symbol, spec.side, spec.kind))
        if known is not None and known.local_qty == spec.local_qty and known.exchange_qty == spec.exchange_qty:
            return False
        f.diffs.append(spec)
        return True

    def _sweep_diffs(self, latest: dict[Key, PositionInfo], keys: set[Key] | None, f: _Findings) -> None:
        """未解决的差异里，已经不成立的记为已解决：未登记的已登记或已消失；数量差异已一致、已平或已消失。"""
        writing = {(d.symbol, d.side, d.kind) for d in f.diffs}
        for (symbol, side, kind), _ in list(self._diffs.items()):
            if side is None or (keys is not None and (symbol, side) not in keys) or (symbol, side, kind) in writing:
                continue
            rec, seen = self._registry.get_active(symbol, side), latest.get((symbol, side))
            if kind == UNREGISTERED:
                why = "已登记" if rec is not None else "交易所已无此仓" if seen is None else ""
            else:
                why = ("登记已平" if rec is None else "交易所已无此仓" if seen is None
                       else "数量已一致" if rec.qty == seen.qty else "")
            if why:
                f.resolves.append(ResolveSpec(symbol, side, (kind,), why))

    # ---------------------------------------------------------------- 提交一轮的结果

    async def _commit(self, kind: str, status: str, started: int, now: int, f: _Findings, notes: list[str]) -> None:
        """无法解释的差异：置抑制、告警；再把值得留痕的一轮（非 ok、有变化、状态变了）写进账本。"""
        if f.mismatch:
            await self._raise_mismatch(f, now)
        notes = [*notes, *f.notes]
        notable = (status != "ok" or bool(notes) or bool(f.diffs) or bool(f.resolves)
                   or (kind != "key" and self._last.get(kind, (0, None))[1] != status))
        if notable:
            await self._write_round(kind, status, started, now, f, notes)
        if kind != "key":
            self._last[kind] = (now, status)
            if status == "ok":
                self._ok_ms[kind] = now

    async def _raise_mismatch(self, f: _Findings, now: int) -> None:
        for spec in f.alerts:
            side = "" if spec.side is None else spec.side.value
            self._alert(AlertLevel.CRITICAL, "reconcile_mismatch",
                        f"{spec.symbol} {side} 登记数量 {spec.local_qty} 与交易所 {spec.exchange_qty} 对不上，"
                        "补拉成交后仍无法解释，已置「对账不一致」：暂不新开仓；请核对后人工确认",
                        dedupe_key=f"reconcile_mismatch:{spec.symbol}:{side}")
        added = await self._suppress(Suppression.RECONCILE_MISMATCH, reason="登记仓与交易所数量对不上，无法解释")
        state = self._modelstate.current()
        if added or state.mismatch_since_ms is None:
            await self._modelstate.set_mismatch_since(now)

    async def _write_round(self, kind: str, status: str, started: int, now: int, f: _Findings,
                           notes: list[str]) -> None:
        detail = json.dumps({"notes": notes, "deferred": f.deferred, "undecided": f.undecided,
                             "diffs": [{"symbol": d.symbol, "side": None if d.side is None else d.side.value,
                                        "kind": d.kind, "local": None if d.local_qty is None else str(d.local_qty),
                                        "exchange": None if d.exchange_qty is None else str(d.exchange_qty)}
                                       for d in f.diffs]}, ensure_ascii=False)
        try:
            _, written = await self._ledger.write(
                repo_reconcile.record_round, at_ms=now, kind=kind, status=status,
                duration_ms=max(0, self._clock.now_ms() - started), detail=detail, diffs=f.diffs,
                resolves=f.resolves)
        except Exception:
            log.exception("写对账记录失败（本轮差异下一轮会重新发现）")
            return
        for spec in f.resolves:
            for key in [k for k in self._diffs if k[0] == spec.symbol and k[1] == spec.side
                        and (not spec.kinds or k[2] in spec.kinds)]:
                del self._diffs[key]
        for rec in written:
            self._diffs[(rec.symbol, rec.side, rec.kind)] = _DiffInfo(rec.local_qty, rec.exchange_qty)

    async def _ensure_loaded(self) -> None:
        """第一次对账之前从账本读回未解决的差异（重启后继续认得它们，并在它们不成立时记为已解决）。"""
        if self._loaded:
            return
        try:
            rows = await self._ledger.read(repo_reconcile.list_diffs, open_only=True)
        except Exception:
            log.exception("读取未解决的对账差异失败（下一轮再读）")
            return
        for row in rows:
            self._diffs[(row.symbol, row.side, row.kind)] = _DiffInfo(row.local_qty, row.exchange_qty)
        self._loaded = True

    # ---------------------------------------------------------------- 强平价

    def _check_liquidation(self, exchange: dict[Key, PositionInfo]) -> None:
        """强平价比止损价更靠近现价：止损挡不住强平（4.6 杠杆与保证金模式末条）。只看登记仓与系统记下的止损价。"""
        for rec in self._registry.registered():
            info = exchange.get((rec.symbol, rec.side))
            if info is None or info.liquidation_price is None or rec.stop_price is None:
                continue
            liq, stop, mark = info.liquidation_price, rec.stop_price, info.mark_price
            if mark is not None and ((rec.side is PositionSide.LONG and liq >= mark)
                                     or (rec.side is PositionSide.SHORT and liq <= mark)):
                continue  # 强平价应在现价的亏损一侧；不在就是数据不合理，不告警
            closer = liq > stop if rec.side is PositionSide.LONG else liq < stop
            if closer:
                self._alert(AlertLevel.CRITICAL, "liquidation_before_stop",
                            f"{rec.symbol} {rec.side.value} 强平价 {liq} 比止损价 {stop} 更靠近现价"
                            f"（标记价 {mark}）：止损挡不住强平，请降低杠杆或调整止损",
                            dedupe_key=f"liq_before_stop:{rec.id}")

    # ================================================================ 挂单与条件单对账

    async def _orders_unknown(self, open_orders: Any, algos: Any, started: int) -> str:
        now = self._clock.now_ms()
        self._orders_unknown_streak += 1
        why = "；".join(f"{name}：{r.reason}" for name, r in (("挂单", open_orders), ("条件单", algos))
                       if isinstance(r, Unknown))
        notes = [f"挂单与条件单对账读取失败（连续第 {self._orders_unknown_streak} 次）：{why}"]
        log.warning("%s", notes[0])
        if self._orders_unknown_streak == self.UNKNOWN_STREAK:
            self._alert(AlertLevel.WARNING, "reconcile_orders_unknown",
                        f"挂单与条件单对账连续 {self.UNKNOWN_STREAK} 次读不到（{why}）：保护单是否还在无法确认",
                        dedupe_key="reconcile_orders_unknown")
        await self._commit("orders", "unknown", started, now, _Findings(), notes)
        self._bus.publish(ReconcileCompleted(kind="orders", status="unknown", at_ms=self._clock.now_ms()))
        return "unknown"

    async def _orders_ok(self, open_orders: list, algos: list[AlgoOrderInfo], started: int) -> str:
        now = self._clock.now_ms()
        if self._orders_unknown_streak >= self.UNKNOWN_STREAK:
            self._alert(AlertLevel.INFO, "reconcile_orders_recovered", "挂单与条件单对账读取已恢复")
        self._orders_unknown_streak = 0
        # 快照的时间取查询发出的时间：保护单管理拿它与最近一次挂/换止损的时间比，查询发出之后才挂上的单可能不在这份快照里
        self._limits.update_orders(open_orders, algos, started)
        self._bus.publish(OrdersSnapshotTaken(open_orders=tuple(open_orders), algo_orders=tuple(algos), at_ms=started))
        notes: list[str] = []
        await self._sweep_stale(algos, now, notes)
        await self._sweep_orphans(algos, now, notes)
        try:
            await self._orders.requery_unknown()
        except Exception:
            log.exception("回查状态未知的订单失败")
        await self._commit("orders", "ok", started, now, _Findings(), notes)
        self._bus.publish(ReconcileCompleted(kind="orders", status="ok", at_ms=self._clock.now_ms()))
        return "ok"

    # ---------------------------------------------------------------- 待清理的旧止损

    async def _sweep_stale(self, algos: list[AlgoOrderInfo], now: int, notes: list[str]) -> None:
        """登记里的待清理旧止损：交易所未结条件单里还在就撤，已不在就从列表移走。当前止损、已移交的止损绝不当旧单撤；
        靠系统止损保护的登记仓，它登记的当前止损不在这份快照里时，旧止损可能是它唯一的止损，先留着。"""
        open_algos = [a for a in algos if a.is_open]
        for rec in self._registry.all_active():
            for entry in list(rec.stale_stops):
                cid, algo_id = entry.get("client_algo_id"), entry.get("algo_id")
                if cid is None and algo_id is None:
                    continue
                if (cid is not None and cid == rec.stop_client_id) or (algo_id is not None and algo_id == rec.stop_algo_id):
                    log.warning("持仓 %s 的待清理旧止损与当前止损是同一张（%s / %s），不处理", rec.id, cid, algo_id)
                    continue
                if rec.handed_over_stop and _is_entry(rec.handed_over_stop, cid, algo_id):  # 已移交的：仓位非零时不动它
                    log.warning("持仓 %s 的待清理旧止损就是已移交给用户的止损（%s / %s），不处理", rec.id, cid, algo_id)
                    continue
                live = next((a for a in open_algos if a.symbol == rec.symbol and _is(a, cid, algo_id)), None)
                if live is None:
                    try:
                        await self._registry.remove_stale_stop(rec.id, client_algo_id=cid, algo_id=algo_id)
                    except (ValueError, LookupError):
                        log.warning("移除待清理旧止损失败：持仓 %s %s/%s", rec.id, cid, algo_id, exc_info=True)
                    else:
                        notes.append(f"{rec.symbol} 的旧止损 {algo_id or cid} 已不在交易所，从待清理列表移除")
                    continue
                if not self._stale_cancel_safe(rec, open_algos):
                    log.warning("持仓 %s 登记的当前止损不在交易所，旧止损 %s 先留着", rec.id, live.algo_id)
                    notes.append(f"{rec.symbol} {rec.side.value} 持仓 {rec.id} 登记的当前止损不在交易所，"
                                 f"旧止损 {live.algo_id} 可能是它唯一的止损，先留着不撤")
                    continue
                await self._submit_cancel(
                    f"stale:{rec.id}:{_seg(live.algo_id)}", live, rec, f"清理待清理的旧止损（持仓 {rec.id}）", now, notes,
                    still=lambda pid=rec.id, a=live: self._stale_still_cancellable(pid, a, open_algos))

    @staticmethod
    def _stale_cancel_safe(rec: PositionRecord, open_algos: list[AlgoOrderInfo]) -> bool:
        """撤这个仓的旧止损不会让它失去保护：它不是登记仓（不靠系统保护）；或是外部保护，而这份快照里这一侧还看得到
        用户的止损类条件单（用户的止损若已被撤掉，旧的系统止损就是它唯一的止损，等保护单管理认领或补挂）；
        或者它登记的当前止损就在这份快照里还挂着。"""
        if rec.status not in PositionStatus.registered():
            return True
        same_side = [a for a in open_algos if a.symbol == rec.symbol and a.position_side is rec.side]
        if rec.protection_state is ProtectionState.EXTERNAL:
            return any(a.algo_type.is_stop_kind and ids.parse_client_id(a.client_algo_id).origin is not OrderOrigin.SYSTEM
                       for a in same_side)
        return any(_is(a, rec.stop_client_id, rec.stop_algo_id) for a in same_side)

    def _stale_still_cancellable(self, position_id: int, algo: AlgoOrderInfo, open_algos: list[AlgoOrderInfo]) -> bool:
        """提交撤旧止损之前再看一次最新的登记：仍在待清理列表里、没有成为当前止损，撤了也不会让仓位失去保护。
        已平的不管（归零后的清理归保护单管理）。"""
        rec = self._registry.by_id(position_id)
        if rec is None or not any(_is_entry(entry, algo.client_algo_id, algo.algo_id) for entry in rec.stale_stops):
            return False
        if rec.handed_over_stop and _is_entry(rec.handed_over_stop, algo.client_algo_id, algo.algo_id):
            return False
        return not _is(algo, rec.stop_client_id, rec.stop_algo_id) and self._stale_cancel_safe(rec, open_algos)

    # ---------------------------------------------------------------- 孤儿条件单

    async def _sweep_orphans(self, algos: list[AlgoOrderInfo], now: int, notes: list[str]) -> None:
        period_ms = int(self._config_get("reconcile_orders_s")) * 1000
        for algo in sorted((a for a in algos if a.is_open), key=lambda a: a.algo_id):
            if not self._is_ours(algo.client_algo_id):  # 条件 1：不带系统前缀的一律不撤，也不告警
                continue
            if self._referenced_by_registry(algo):  # 条件 2（活动持仓引用，含当前止损、待清理旧单）与条件 3（已移交）
                continue
            base_key = f"orphan:{_seg(algo.algo_id)}"
            if await self._in_flight(algo, own_key=base_key):  # 条件 2（别的在途意图引用；自己之前的撤单尝试另行处理）
                continue
            if algo.create_time_ms <= 0 or now - algo.create_time_ms <= period_ms:  # 条件 4
                continue
            state = self._orphan_state(algo, now)  # 条件 5
            if state == "wait":
                continue
            if state == "zero":
                await self._submit_cancel(
                    base_key, algo, None, f"孤儿条件单：{algo.symbol} {algo.position_side.value} 持仓为零，没有任何登记引用它",
                    now, notes, still=lambda a=algo: self._orphan_still_cancellable(a, now))
                continue
            where = f"{algo.symbol} {algo.position_side.value}"
            detail = "持仓读数未知" if state == "unknown" else f"持仓不为零（{state}）"
            self._alert(AlertLevel.WARNING, "orphan_stop_kept",
                        f"发现疑似孤儿条件单 {algo.client_algo_id}（条件单号 {algo.algo_id}，{where}），但{detail}，不撤："
                        "多半是登记簿落后于交易所，撤掉会让仓位失去保护；请核对登记簿",
                        dedupe_key=f"orphan_kept:{algo.algo_id}")

    def _orphan_still_cancellable(self, algo: AlgoOrderInfo, now: int) -> bool:
        """提交撤孤儿之前再看一次（前面读账本时让出了执行权）：仍没有任何活动持仓引用它，最新的持仓快照里这一侧
        仍新鲜地明确为零（并发的一轮持仓对账可能刚读到这一侧有仓了）。"""
        return not self._referenced_by_registry(algo) and self._orphan_state(algo, now) == "zero"

    def _orphan_state(self, algo: AlgoOrderInfo, now: int) -> str:
        """条件 5：``"zero"``（可以撤）、``"wait"``（先不撤、不告警）、数量文本或 ``"unknown"``（不撤，告警）。

        零读数要真能说明这张单现在没有在保护仓位。登记簿认为这一侧还有仓时不作数：交易所为零多半是登记簿还没跟上
        （连续零读数之后会判已平），也可能是读数抖了一下（复核读数正看到这个仓），而它没记着的这张系统止损可能正是
        它唯一的止损。早于这张单建立的零读数也说明不了什么（新鲜窗口 2×持仓周期可以长于条件单周期）：等建立之后的快照。"""
        state = self._position_state(algo.symbol, algo.position_side, now)
        if state != "zero":
            return state
        if self._registry.get_active(algo.symbol, algo.position_side) is not None:
            return "wait"
        snap = self._snapshot
        if snap is None or snap.server_ms <= algo.create_time_ms:
            return "wait"
        return "zero"

    def _is_ours(self, client_algo_id: str | None) -> bool:
        parsed = ids.parse_client_id(client_algo_id)
        return parsed.origin is OrderOrigin.SYSTEM and (self._instance is None or parsed.instance == self._instance)

    def _referenced_by_registry(self, algo: AlgoOrderInfo) -> bool:
        """登记簿里的活动持仓（含待确认与已忽略）有没有记着这张单：当前止损、待清理旧单、已移交的止损。"""
        cid, algo_id = algo.client_algo_id, algo.algo_id
        for rec in self._registry.all_active():
            if (cid is not None and rec.stop_client_id == cid) or rec.stop_algo_id == algo_id:
                return True
            if any(_is_entry(entry, cid, algo_id) for entry in rec.stale_stops):
                return True
            if rec.handed_over_stop and _is_entry(rec.handed_over_stop, cid, algo_id):
                return True
        return False

    async def _in_flight(self, algo: AlgoOrderInfo, *, own_key: str | None = None) -> bool:
        """有没有未到终态的意图在引用这张单；``own_key`` 为底的撤单尝试（我们自己的）不算。"""
        try:
            return await self._ledger.read(repo_reconcile.algo_in_flight, symbol=algo.symbol, side=algo.position_side,
                                           client_algo_id=algo.client_algo_id, algo_id=algo.algo_id,
                                           exclude_key=own_key)
        except Exception:
            log.exception("查询在途意图失败：%s", algo.algo_id)
            return True  # 查不清就当作有，宁可不撤

    def _position_state(self, symbol: str, side: PositionSide, now: int) -> str:
        """最近一次持仓快照里这一侧的情况：``"zero"``（新鲜且明确为零）、数量文本、或 ``"unknown"``。"""
        snap = self._snapshot
        window = self.FRESH_PERIODS * int(self._config_get("reconcile_positions_s")) * 1000
        if snap is None or not self._last_pos_ok or now - snap.at_ms > window:
            return "unknown"
        info = snap.by_key.get((symbol, side))
        return "zero" if info is None else str(info.qty)

    async def _submit_cancel(self, base_key: str, algo: AlgoOrderInfo, rec: PositionRecord | None, reason: str,
                             now: int, notes: list[str], *, still: Callable[[], bool]) -> None:
        """经协调者提交 STOP_CANCEL。有别的在途意图在引用这张单就不提交；试满上限就放弃并告警。

        上一次尝试还没结束就不另起一次；但它若落了盘、超过一个条件单对账周期还停在未派发的状态（多半是提交后进程
        被杀：协调者重启后不会自己派发没有订单行的意图，等提交者重新提交），就用同一个幂等键再交一次，协调者据此派发它
        （正在派发的不会重复派发）。``still``：提交前的最后核对（前面读账本时让出了执行权，登记簿可能已经变了）。"""
        try:
            keys = await self._ledger.read(repo_reconcile.cancel_key_state, base_key,
                                           max_attempts=self.MAX_CANCEL_ATTEMPTS)
            if keys.in_flight:
                period_ms = int(self._config_get("reconcile_orders_s")) * 1000
                if (keys.pending_state in _UNDISPATCHED and keys.pending_since_ms is not None
                        and now - keys.pending_since_ms > period_ms and still()):
                    await self._coordinator.submit(self._cancel_spec(keys.pending_key, algo, rec, reason))
                    notes.append(f"撤单意图（幂等键 {keys.pending_key}）落了盘却一直没有派发，已用同一个幂等键再交给协调者")
                return
            if keys.exhausted:
                self._alert(AlertLevel.CRITICAL, "cancel_attempts_exhausted",
                            f"条件单 {algo.client_algo_id}（条件单号 {algo.algo_id}，{algo.symbol} {algo.position_side.value}）"
                            f"已试撤 {keys.attempts} 次仍在交易所，不再自动尝试；请人工处理",
                            dedupe_key=f"cancel_stuck:{algo.algo_id}")
                return
            if rec is not None and await self._in_flight(algo, own_key=base_key):
                return
            if not still():
                log.info("条件单 %s 在准备撤单期间被登记簿认领（或已不需要撤），不撤", algo.algo_id)
                return
            await self._coordinator.submit(self._cancel_spec(keys.next_key, algo, rec, reason))
        except Exception:
            log.exception("提交撤条件单意图失败：%s", algo.algo_id)
            return
        notes.append(f"已提交撤单：{reason}（条件单号 {algo.algo_id}，幂等键 {keys.next_key}）")

    @staticmethod
    def _cancel_spec(idem_key: str | None, algo: AlgoOrderInfo, rec: PositionRecord | None, reason: str) -> IntentSpec:
        return IntentSpec(action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.RECONCILE,
                          symbol=algo.symbol, side=algo.position_side, idem_key=idem_key or "",
                          params={"client_algo_id": algo.client_algo_id, "algo_id": algo.algo_id}, reason=reason,
                          position_id=None if rec is None else rec.id,
                          position_generation=None if rec is None else rec.generation)

    # ================================================================ 抑制与告警

    async def _suppress(self, s: Suppression, *, reason: str) -> bool:
        """置抑制；本来就有返回 False（不重复写、不重复告警）。"""
        if s in self._modelstate.current().suppressions:
            return False
        await self._modelstate.add_suppression(s, reason=reason)
        return True

    async def _unsuppress(self, s: Suppression, *, reason: str) -> bool:
        if s not in self._modelstate.current().suppressions:
            return False
        await self._modelstate.remove_suppression(s, reason=reason)
        return True

    def _alert(self, level: AlertLevel, kind: str, message: str, *, dedupe_key: str | None = None) -> None:
        log.log(logging.WARNING if level is not AlertLevel.INFO else logging.INFO, "[%s] %s", kind, message)
        try:
            self._notifier.raise_alert(level, kind, message, dedupe_key=dedupe_key)
        except Exception:
            log.exception("提出告警失败：%s", kind)


# ---------------------------------------------------------------- 小工具


def _seg(value: str) -> str:
    """幂等键里的一段：冒号与百分号转义，保证拼出来的键不会串味。"""
    return str(value).replace("%", "%25").replace(":", "%3A")


def _is(algo: AlgoOrderInfo, client_algo_id: str | None, algo_id: str | None) -> bool:
    return ((client_algo_id is not None and algo.client_algo_id == client_algo_id)
            or (algo_id is not None and algo.algo_id == algo_id))


def _is_entry(entry: Any, client_algo_id: str | None, algo_id: str | None) -> bool:
    """登记里的一条（待清理旧单或已移交的止损）有没有点名这张单：任何一个取值等于它的自定义编号或条件单号。"""
    if not isinstance(entry, dict):
        return False
    values = {v for v in entry.values() if isinstance(v, str)}
    return (client_algo_id is not None and client_algo_id in values) or algo_id in values
