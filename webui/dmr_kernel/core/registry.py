"""持仓登记簿（第 3 层）：所有「现在持有什么、归谁、处于什么状态」的唯一记录（计划 W2d / T0.12）。

- **账本为准，缓存为快**：全部非 closed 的持仓缓存在内存里，同步查询（``get_active``/``occupancy``/``counts`` 等）；
  每次写入都在账本里一个事务内完成（读当前行、检查状态与版本、改、写审计），写成功后再更新缓存、发布事件，
  所以订阅者收到事件时，登记簿里一定已经查得到。启动时 ``load`` 从账本重建缓存。
- **一仓一主**：同一「合约 + 方向」同一时刻只有一条非 closed 的记录，由 ``position`` 表的部分唯一索引保证；
  并发登记同一个键只有一条成功，其余得到 ``KeyOccupied``。``generation`` 是该键第几次从零开仓。
- **持仓状态版本**：每个持仓一个 ``version``，每次变化 +1 并发布 ``PositionChanged(cause, version)``；
  界面/操作带着看到的版本来，对不上得到 ``StaleVersion``（只有待确认转接管与忽略这类需要校验的入口接受版本）。
  对账读回的观测值（``update_exchange_view``）不算状态变化，不动版本、不发事件。
- **事件**：新建发布 ``PositionRegistered``；其余变化发布 ``PositionChanged``；归零发布 ``PositionClosed``
  （不另发 ``PositionChanged``）；保护状态或止损价变化再发 ``ProtectionChanged``；
  开仓方向的外部成交使该侧持仓增加而没有记录时发布 ``UnregisteredPositionFound(trigger="external_fill")``，由模型 2 归类。
- **成交**：``start()`` 后订阅 ``FillIngested`` 自动调用 ``apply_fill``（规则见该方法）。
- **暂缓应用成交**（重启恢复 F9 第 2 步与第 10 步）：``hold_fills()`` 之后经事件到达的成交只暂存、不改登记簿（成交照常入账，
  入账不归本模块管）；``release_fills(snapshot_ms)`` 时早于（含等于）快照时间的成交只入账、不改登记簿（快照已经反映了它们），
  之后的按成交时间顺序应用。直接调用 ``apply_fill`` 不受暂缓影响（恢复自己要回放一部分成交时用）。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import (
    EventBus,
    FillIngested,
    PositionChanged,
    PositionClosed,
    PositionRegistered,
    ProtectionChanged,
    Subscription,
    UnregisteredPositionFound,
)
from dmr_kernel.core.types import (
    ExitReason,
    OrderOrigin,
    Ownership,
    PendingReason,
    PositionSide,
    PositionStatus,
    ProtectionState,
)
from dmr_kernel.exchange import ids
from dmr_kernel.ledger import repo_registry, repo_stops, repo_takeover
from dmr_kernel.ledger.records import PositionRecord
from dmr_kernel.ledger.writer import Ledger

__all__ = ["FillRelease", "KeyOccupancy", "KeyOccupied", "PRESUMED_STOP_NOTE", "REVERSAL_BLOCK", "Registry",
           "StaleVersion"]

log = logging.getLogger(__name__)

PRESUMED_STOP_NOTE = repo_registry.PRESUMED_STOP_NOTE
REVERSAL_BLOCK = "对侧有策略仓：属于反手，由 F11 处理"
_SIDE_CN = {PositionSide.LONG: "多头", PositionSide.SHORT: "空头"}
_MARGIN_MODES = ("isolated", "cross")
# 订单编号分腿的第一个字母 → 订单用途（事件里没带用途时按编号推断）
_LEG_PURPOSE = {"o": "open", "c": "close", "p": "partial_close", "s": "stop", "d": "dust_close"}


class StaleVersion(Exception):
    """调用方看到的持仓状态版本已经过期。"""


class KeyOccupied(Exception):
    """同一「合约 + 方向」已有非 closed 的持仓（一仓一主）。"""

    def __init__(self, message: str, *, symbol: str | None = None, side: PositionSide | None = None) -> None:
        super().__init__(message)
        self.symbol = symbol
        self.side = side


@dataclass(frozen=True)
class FillRelease:
    """``release_fills`` 的结果：``applied`` 应用了几笔；``skipped`` 早于（含等于）快照时间、只入账没改登记簿的几笔；
    ``failed`` 应用时出错的几笔（已记日志，不挡后面的）。"""

    applied: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class KeyOccupancy:
    """某合约两个方向上的当前占用（登记簿的快照：之后登记簿再变，这份不变）。"""

    symbol: str
    long: PositionRecord | None
    short: PositionRecord | None

    def on(self, side: PositionSide) -> PositionRecord | None:
        return self.long if side is PositionSide.LONG else self.short

    def strategy_open_block(self, side: PositionSide) -> str | None:
        """F1 第 3 步第 5 条：策略能不能在 ``side`` 方向开仓；不能就返回原因，能返回 None。

        任一方向上有外部仓（已忽略）、待确认仓、接管仓、混合仓（以及 AI 仓）都不开；
        同方向已有策略仓不开；对侧有策略仓属于反手，由 F11 处理，这里也不开。
        """
        same, opposite = self.on(side), self.on(side.opposite())
        for rec in (same, opposite):
            if rec is not None:
                reason = self._foreign_block(rec)
                if reason is not None:
                    return reason
        if same is not None:
            return f"{self.symbol} 同方向已有策略仓，不重复开仓"
        if opposite is not None:
            return REVERSAL_BLOCK
        return None

    def _foreign_block(self, rec: PositionRecord) -> str | None:
        where = f"{self.symbol} {_SIDE_CN[rec.side]}方向"
        if rec.status is PositionStatus.IGNORED:
            return f"{where}有外部仓（已忽略），不开策略仓"
        if rec.status is PositionStatus.PENDING:
            return f"{where}有待确认仓，不开策略仓"
        if rec.status is PositionStatus.TAKING_OVER or rec.ownership is Ownership.TAKEOVER:
            return f"{where}有接管仓，不开策略仓"
        if rec.is_mixed:
            return f"{where}有混合仓，不开策略仓"
        if rec.ownership is Ownership.AI:
            return f"{where}有 AI 仓，不开策略仓"
        return None


class Registry:
    """持仓登记簿。协程方法写账本并更新缓存；同步方法只读缓存。"""

    def __init__(self, ledger: Ledger, bus: EventBus, clock: Clock, *, config_get: Callable[[str], Any]) -> None:
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._config_get = config_get
        self._by_id: dict[int, PositionRecord] = {}
        self._by_key: dict[tuple[str, PositionSide], PositionRecord] = {}
        self._cooldowns: dict[str, int] = {}
        self._loaded = False
        self._subscription: Subscription | None = None
        self._holding = False  # 暂缓应用成交（重启恢复期间）
        self._held: list[FillIngested] = []  # 暂缓期间经事件到达的成交，按到达顺序

    # ------------------------------------------------------------ 启动与订阅

    async def load(self) -> None:
        """从账本重建缓存：全部非 closed 的持仓与冷却。"""
        positions, cooldowns = await self._ledger.read(repo_registry.load_state)
        self._by_id = {rec.id: rec for rec in positions}
        self._by_key = {(rec.symbol, rec.side): rec for rec in positions}
        self._cooldowns = {row.symbol: row.until_ms for row in cooldowns}
        self._loaded = True

    async def start(self) -> None:
        """订阅 ``FillIngested``，自动应用成交。缓存还没加载就先加载。重复启动无害。"""
        if self._subscription is not None:
            return
        if not self._loaded:
            await self.load()
        self._subscription = self._bus.subscribe(FillIngested, self._on_fill, name="registry.fills")

    async def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None

    async def _on_fill(self, event: FillIngested) -> None:
        if self._holding:  # 暂缓中：成交已经入账，登记簿等恢复释放时再决定要不要应用
            self._held.append(event)
            return
        # shield：订阅被取消时，已经交给账本的这笔成交仍要走完「更新缓存、发事件」，缓存才不会和账本脱节
        await asyncio.shield(self.apply_fill(event))

    # ------------------------------------------------------------ 暂缓应用成交（重启恢复）

    def hold_fills(self) -> None:
        """从现在起，经 ``FillIngested`` 事件到达的成交只暂存、不应用到登记簿（成交本身照常入账）。重复调用无害，已暂存的不丢。
        同步完成：数据流启动之前调用，中间没有空隙。"""
        self._holding = True

    @property
    def holding(self) -> bool:
        return self._holding

    def held_fills(self) -> tuple[FillIngested, ...]:
        """暂缓期间暂存的成交，按到达顺序（只读副本）。恢复据此回放该回放的成交。"""
        return tuple(self._held)

    async def release_fills(self, snapshot_ms: int) -> FillRelease:
        """解除暂缓。早于（含等于）``snapshot_ms`` 的成交只入账、不改登记簿（快照已经反映了它们）；之后的按成交时间顺序应用
        （同一毫秒按到达顺序）。没有处于暂缓就什么也不做。

        释放期间到达的成交仍进缓冲，等当前这一批应用完再接着处理，所以应用顺序不会被插队；缓冲清空、标志翻转之间没有 await。
        单笔应用出错（比如账本里读不到这一行）只记日志、计入 ``failed``，不挡后面的。被取消时还没应用的退回缓冲、仍在暂缓，
        可以再次释放（已经应用的靠 ``apply_fill`` 的幂等不会重复）。"""
        if isinstance(snapshot_ms, bool) or not isinstance(snapshot_ms, int) or snapshot_ms < 0:
            raise ValueError(f"快照时间必须是非负整数毫秒：{snapshot_ms!r}")
        if not self._holding:
            return FillRelease()
        applied = skipped = failed = 0
        while self._held:
            batch, self._held = self._held, []
            pending = [event for event in batch if event.trade.time_ms > snapshot_ms]
            skipped += len(batch) - len(pending)
            pending.sort(key=lambda event: event.trade.time_ms)  # 稳定排序：同一毫秒保持到达顺序
            try:
                while pending:
                    event = pending[0]
                    try:
                        await asyncio.shield(self.apply_fill(event))
                        applied += 1
                    except Exception:
                        failed += 1
                        log.exception("释放暂缓的成交时应用失败：成交行 %s（%s %s）", event.fill_id, event.trade.symbol,
                                      event.trade.position_side.value)
                    pending.pop(0)
            finally:
                if pending:  # 被取消：没处理完的退回缓冲头部，仍在暂缓
                    self._held[:0] = pending
        self._holding = False
        log.info("释放暂缓的成交：应用 %s 笔，只入账 %s 笔，失败 %s 笔", applied, skipped, failed)
        return FillRelease(applied=applied, skipped=skipped, failed=failed)

    # ------------------------------------------------------------ 查询（同步，读缓存）

    def get_active(self, symbol: str, side: PositionSide) -> PositionRecord | None:
        return self._by_key.get((symbol, side))

    def all_active(self) -> list[PositionRecord]:
        return sorted(self._by_id.values(), key=lambda rec: rec.id)

    def registered(self) -> list[PositionRecord]:
        """登记仓：接管中、托管中、平仓中。"""
        wanted = PositionStatus.registered()
        return [rec for rec in self.all_active() if rec.status in wanted]

    def by_id(self, position_id: int) -> PositionRecord | None:
        return self._by_id.get(position_id)

    async def fetch(self, position_id: int) -> PositionRecord | None:
        """从账本读，含已平的。"""
        return await self._ledger.read(repo_registry.get, position_id)

    def occupancy(self, symbol: str) -> KeyOccupancy:
        return KeyOccupancy(symbol, self._by_key.get((symbol, PositionSide.LONG)),
                            self._by_key.get((symbol, PositionSide.SHORT)))

    def counts(self) -> dict[str, int]:
        """状态条用的计数。unprotected 只数登记仓里保护为「无保护」的（重建中、外部保护都不算）。"""
        recs = list(self._by_id.values())

        def number(status: PositionStatus) -> int:
            return sum(1 for rec in recs if rec.status is status)

        registered = PositionStatus.registered()
        return {
            "managed": number(PositionStatus.MANAGED),
            "unprotected": sum(1 for rec in recs if rec.status in registered
                               and rec.protection_state is ProtectionState.NONE),
            "pending": number(PositionStatus.PENDING),
            "external": number(PositionStatus.IGNORED),
            "taking_over": number(PositionStatus.TAKING_OVER),
            "closing": number(PositionStatus.CLOSING),
        }

    # ------------------------------------------------------------ 冷却

    async def set_cooldown(self, symbol: str, until_ms: int, reason: str) -> None:
        """每个合约一个冷却；后一次覆盖前一次，哪怕更短。"""
        row = await self._ledger.write(repo_registry.set_cooldown, symbol=symbol, until_ms=until_ms, reason=reason,
                                       set_ms=self._clock.now_ms())
        self._cooldowns[symbol] = row.until_ms

    def cooldown_until(self, symbol: str) -> int | None:
        """冷却结束时间；没有冷却或已到点（含恰好到点）返回 None。"""
        until = self._cooldowns.get(symbol)
        if until is None or self._clock.now_ms() >= until:
            return None
        return until

    # ------------------------------------------------------------ 登记

    async def register_open(self, *, symbol: str, side: PositionSide, qty: Decimal, avg_price: Decimal,
                            open_time_ms: int, ownership: Ownership, model: str, intent_id: int,
                            entry_reason: str = "", list_snapshot_id: int | None = None,
                            list_direction: str | None = None, list_score: Decimal | None = None,
                            leverage: int | None = None, margin_mode: str | None = None) -> PositionRecord:
        """新记录：托管中、保护「无保护」、出场规则集 STRATEGY（策略与 AI 一样）、代际 = 该键上次 + 1、
        ``anchor_ms`` = 开仓时间。键已被占用抛 ``KeyOccupied``。"""
        if ownership not in (Ownership.STRATEGY, Ownership.AI):
            raise ValueError(f"register_open 只登记策略仓与 AI 仓，收到归属 {ownership.value}")
        _require_positive(qty=qty, avg_price=avg_price)
        change = await self._write(
            repo_registry.register_open, symbol=symbol, side=side, qty=qty, avg_price=avg_price,
            open_time_ms=open_time_ms, ownership=ownership, model=model, intent_id=intent_id,
            entry_reason=entry_reason, list_snapshot_id=list_snapshot_id, list_direction=list_direction,
            list_score=list_score, leverage=leverage, margin_mode=margin_mode, now_ms=self._clock.now_ms())
        return self._registered(change.record)

    async def register_pending(self, *, symbol: str, side: PositionSide, qty: Decimal, avg_price: Decimal,
                               reason: PendingReason, flags: Iterable[str] = (), note: str = "") -> PositionRecord:
        _require_positive(qty=qty, avg_price=avg_price)
        change = await self._write(
            repo_registry.register_pending, symbol=symbol, side=side, qty=qty, avg_price=avg_price, reason=reason,
            flags=list(flags), note=note, now_ms=self._clock.now_ms())
        return self._registered(change.record)

    async def begin_takeover(self, *, symbol: str, side: PositionSide, qty: Decimal, avg_price: Decimal,
                             open_time_ms: int, open_time_estimated: bool, anchor_ms: int, signal_exit: bool,
                             position_id: int | None = None, expected_version: int | None = None) -> PositionRecord:
        """待确认 → 接管中（给 ``position_id``），或直接新建「接管中」；归属手动接管、出场规则集 TAKEOVER。
        ``expected_version`` 对不上抛 ``StaleVersion``。"""
        _require_positive(qty=qty, avg_price=avg_price)
        change = await self._write(
            repo_registry.begin_takeover, symbol=symbol, side=side, qty=qty, avg_price=avg_price,
            open_time_ms=open_time_ms, open_time_estimated=open_time_estimated, anchor_ms=anchor_ms,
            signal_exit=signal_exit, position_id=position_id, expected_version=expected_version,
            now_ms=self._clock.now_ms())
        if change.before is None:
            return self._registered(change.record)
        return self._changed(change, "takeover")

    # ------------------------------------------------------------ 状态迁移

    async def mark_managed(self, position_id: int) -> PositionRecord:
        """接管中 → 托管中。已是托管中无害（不改版本、不发事件）。"""
        change = await self._write(repo_registry.mark_managed, position_id, now_ms=self._clock.now_ms())
        return self._changed(change, "managed")

    async def set_closing(self, position_id: int, closing: bool, *, cause: str) -> PositionRecord:
        """托管中 ↔ 平仓中；已在目标状态无害。``cause`` 是 PositionChanged 的原因。"""
        change = await self._write(repo_registry.set_closing, position_id, closing, now_ms=self._clock.now_ms())
        return self._changed(change, cause)

    async def set_protection(self, position_id: int, *, state: ProtectionState, stop_price: Decimal | None = None,
                             stop_client_id: str | None = None, stop_algo_id: str | None = None,
                             stop_cycle: int | None = None, note: str | None = None,
                             clear_stop: bool = False) -> PositionRecord:
        """改保护状态；没给的字段不动，``clear_stop`` 清掉止损价与两个编号。已平记录也能改（清理要记进度）。
        保护状态或止损价变化时除 PositionChanged 外再发 ProtectionChanged。"""
        if clear_stop and (stop_price is not None or stop_client_id is not None or stop_algo_id is not None):
            raise ValueError("clear_stop 与止损价、止损编号不能同时给")
        change = await self._write(
            repo_stops.set_protection_recording, position_id, state=state, stop_price=stop_price,
            stop_client_id=stop_client_id, stop_algo_id=stop_algo_id, stop_cycle=stop_cycle, note=note,
            clear_stop=clear_stop, now_ms=self._clock.now_ms())
        rec = self._changed(change, "protection")
        before = change.before
        if change.changed and (before is None or (before.protection_state, before.stop_price)
                               != (rec.protection_state, rec.stop_price)):
            self._bus.publish(ProtectionChanged(position_id=rec.id, state=rec.protection_state.value,
                                                stop_price=rec.stop_price, at_ms=self._clock.now_ms()))
        return rec

    async def add_stale_stop(self, position_id: int, *, client_algo_id: str | None,
                             algo_id: str | None) -> PositionRecord:
        """登记一张待清理的旧止损；同样的一张不重复登记。"""
        if client_algo_id is None and algo_id is None:
            raise ValueError("旧止损至少要给自定义编号或条件单号之一")
        change = await self._write(repo_registry.add_stale_stop, position_id, client_algo_id=client_algo_id,
                                   algo_id=algo_id, now_ms=self._clock.now_ms())
        return self._changed(change, "stale_stops")

    async def remove_stale_stop(self, position_id: int, *, client_algo_id: str | None = None,
                                algo_id: str | None = None) -> PositionRecord:
        """记为已处理：自定义编号或条件单号任一对上就移除；都不给抛 ValueError。"""
        if client_algo_id is None and algo_id is None:
            raise ValueError("要移除的旧止损至少要给自定义编号或条件单号之一")
        change = await self._write(repo_registry.remove_stale_stop, position_id, client_algo_id=client_algo_id,
                                   algo_id=algo_id, now_ms=self._clock.now_ms())
        return self._changed(change, "stale_stops")

    async def set_handed_over(self, position_id: int, stop: Mapping[str, Any] | None) -> PositionRecord:
        """记录（或清除 None）已移交给用户的止损单。"""
        change = await self._write(repo_registry.set_handed_over, position_id, stop, now_ms=self._clock.now_ms())
        return self._changed(change, "handed_over")

    async def update_exchange_view(self, position_id: int, *, exchange_qty: Decimal,
                                   liquidation_price: Decimal | None, seen_ms: int) -> PositionRecord:
        """记录对账读回的交易所数量与强平价，并标一致性。不算状态变化：不动版本、不发事件。"""
        change = await self._write(
            repo_registry.update_exchange_view, position_id, exchange_qty=exchange_qty,
            liquidation_price=liquidation_price, seen_ms=seen_ms)
        self._absorb(change.record)
        return change.record

    async def ignore(self, position_id: int, *, expected_version: int | None) -> PositionRecord:
        """待确认/接管 → 外部（已忽略）。版本对不上抛 ``StaleVersion``；已平抛 ValueError。"""
        change = await self._write(repo_registry.ignore, position_id, expected_version=expected_version,
                                   now_ms=self._clock.now_ms())
        return self._changed(change, "ignored")

    async def ignore_mixed(self, position_id: int, *, expected_version: int | None) -> PositionRecord:
        """混合仓 → 外部（已忽略）。其它状态的仓用 ``ignore``。版本对不上抛 ``StaleVersion``。"""
        change = await self._write(repo_takeover.ignore_mixed, position_id, expected_version=expected_version,
                                   now_ms=self._clock.now_ms())
        return self._changed(change, "ignored")

    async def restore_original(self, position_id: int, *, ownership: Ownership, open_time_ms: int,
                               opened_by_intent_id: int | None, note: str,
                               expected_version: int | None) -> PositionRecord:
        """待确认仓 → 托管中的策略仓/AI 仓（F13 恢复托管，按开仓订单的原归属）。版本对不上抛 ``StaleVersion``。"""
        change = await self._write(
            repo_takeover.restore_original, position_id, ownership=ownership, open_time_ms=open_time_ms,
            opened_by_intent_id=opened_by_intent_id, note=note, expected_version=expected_version,
            now_ms=self._clock.now_ms())
        return self._changed(change, "restored")

    async def set_pending(self, position_id: int, *, reason: PendingReason, flags: Iterable[str] = (),
                          note: str = "") -> PositionRecord:
        """待确认仓重新归类，或把已忽略的恢复为待确认。"""
        change = await self._write(repo_registry.set_pending, position_id, reason=reason, flags=list(flags),
                                   note=note, now_ms=self._clock.now_ms())
        return self._changed(change, "pending")

    async def mark_closed(self, position_id: int, *, exit_reason: ExitReason, closed_ms: int) -> PositionRecord:
        """任何非 closed 状态 → 已平：写 trade、发布 PositionClosed。已平的原样返回（出场原因以第一次为准）。"""
        change = await self._write(repo_registry.mark_closed, position_id, exit_reason=exit_reason,
                                   closed_ms=closed_ms, now_ms=self._clock.now_ms())
        rec = change.record
        self._absorb(rec)
        if change.changed:
            self._announce_closed(rec)
        return rec

    # ------------------------------------------------------------ 成交

    async def apply_fill(self, fill: FillIngested) -> PositionRecord | None:
        """把一笔已入账的成交应用到登记簿，返回受影响的持仓记录（没有就是 None）。

        该键没有任何非 closed 记录：系统编号且订单用途为开仓 → 登记（归属按意图的模型：m3 → AI，其余 → 策略；
        意图行读不到时按编号里的模型代码；晚到的成交即使意图已到终态也登记）；其它使该侧持仓增加的成交 →
        发布 ``UnregisteredPositionFound(trigger="external_fill")``；减少方向的成交只入账不登记。

        该键有记录（任何非 closed 状态，含待确认与已忽略）：开仓方向加数量、按成交加权更新均价、更新最大数量；
        策略仓/AI 仓被非系统成交加仓 → 混合仓（出场规则集 STRATEGY_NO_REVERSE）；接管仓被加仓仍是接管仓。
        平仓方向减数量，归零 → 已平，出场原因：强平/自动减仓/应急按来源；系统止损单 → STOP；系统平仓单按意图的
        原因（signal/reverse/manual/close_all/protective）；非系统编号的平仓成交若满足「推定为系统止损触发」
        （系统保护、有止损价、成交价已越过止损价且数量等于持仓）→ STOP 并记说明；其它 → EXTERNAL。
        变化的 cause：待确认仓 ``qty_change``（供模型 2 重判）、非系统成交 ``外部调整``、系统成交 ``fill``。

        幂等：成交行上已回填 ``position_id`` 的再来一次不产生任何影响。成交不在账本里抛 LookupError。
        """
        parsed = ids.parse_client_id(fill.client_order_id)
        purpose = str(fill.order_purpose) if fill.order_purpose else None
        if purpose is None and parsed.origin is OrderOrigin.SYSTEM and parsed.leg:
            purpose = _LEG_PURPOSE.get(parsed.leg[0])
        result = await self._write(
            repo_registry.apply_fill, fill_id=fill.fill_id, origin=fill.origin, purpose=purpose,
            intent_id=fill.intent_id, model_code=parsed.model_code, now_ms=self._clock.now_ms(),
            default_leverage=self._default_leverage(), default_margin_mode=self._default_margin_mode())
        rec = result.record
        if result.outcome == "registered":
            return self._registered(rec)
        if result.outcome == "applied":
            return self._changed(repo_registry.Change(rec, result.before), result.cause)
        if result.outcome == "closed":
            self._absorb(rec)
            self._announce_closed(rec)
            return rec
        if result.outcome == "reported":
            booked = result.fill
            log.info("未登记的开仓方向成交：%s %s 数量 %s 价格 %s", booked.symbol, booked.position_side.value,
                     booked.qty, booked.price)
            self._bus.publish(UnregisteredPositionFound(
                symbol=booked.symbol, side=booked.position_side, qty=booked.qty, entry_price=booked.price,
                trigger="external_fill", at_ms=self._clock.now_ms()))
            return None
        if rec is not None:  # duplicate：成交早已应用过，顺手让缓存与账本对齐
            self._absorb(rec)
        return rec

    # ------------------------------------------------------------ 内部

    async def _write(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """在账本的写线程里执行仓储函数；把账本层的异常翻译成本模块的异常。"""
        try:
            return await self._ledger.write(fn, *args, **kwargs)
        except repo_registry.VersionConflict as exc:
            raise StaleVersion(str(exc)) from None
        except repo_registry.KeyInUse as exc:
            raise KeyOccupied(str(exc), symbol=exc.symbol, side=exc.side) from None

    def _absorb(self, rec: PositionRecord) -> None:
        """把账本里的最新记录放进缓存：已平的移出；缓存里已有更新的版本就不回退。"""
        key = (rec.symbol, rec.side)
        if rec.status is PositionStatus.CLOSED:
            self._by_id.pop(rec.id, None)
            if (cached := self._by_key.get(key)) is not None and cached.id == rec.id:
                del self._by_key[key]
            return
        cached = self._by_id.get(rec.id)
        if cached is not None and cached.version > rec.version:
            return
        self._by_id[rec.id] = rec
        self._by_key[key] = rec

    def _registered(self, rec: PositionRecord) -> PositionRecord:
        self._absorb(rec)
        log.info("持仓登记：编号 %s %s %s 状态 %s 归属 %s 数量 %s", rec.id, rec.symbol, rec.side.value,
                 rec.status.value, rec.ownership.value, rec.qty)
        self._bus.publish(PositionRegistered(
            position_id=rec.id, symbol=rec.symbol, side=rec.side, ownership=rec.ownership.value,
            status=rec.status.value, at_ms=self._clock.now_ms()))
        return rec

    def _changed(self, change: repo_registry.Change, cause: str) -> PositionRecord:
        rec = change.record
        self._absorb(rec)
        if change.changed:
            self._bus.publish(PositionChanged(position_id=rec.id, symbol=rec.symbol, side=rec.side, cause=cause,
                                              version=rec.version, at_ms=self._clock.now_ms()))
        return rec

    def _announce_closed(self, rec: PositionRecord) -> None:
        reason = rec.exit_reason if rec.exit_reason is not None else ExitReason.UNKNOWN
        log.info("持仓已平：编号 %s %s %s 出场原因 %s", rec.id, rec.symbol, rec.side.value, reason.value)
        self._bus.publish(PositionClosed(position_id=rec.id, symbol=rec.symbol, side=rec.side,
                                         exit_reason=reason.value, at_ms=self._clock.now_ms()))

    def _default_leverage(self) -> int | None:
        value = self._config("leverage")
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    def _default_margin_mode(self) -> str | None:
        value = self._config("margin_mode")
        return value if value in _MARGIN_MODES else None

    def _config(self, key: str) -> Any:
        try:
            return self._config_get(key)
        except LookupError:  # 配置里没有这一项：当作没配
            return None


def _require_positive(**values: Decimal) -> None:
    for name, value in values.items():
        if value <= 0:
            raise ValueError(f"{name} 必须大于 0：{value}")
