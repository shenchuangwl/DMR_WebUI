"""交易意图的状态机、幂等键与过期处理（第 3 层；计划报告 4.6、F1 第 4 步、F3 第 2 步）。

- 意图 10 态，迁移表 ``TRANSITIONS``：终态没有出口，只能经 ``reopen_for_late_fill`` 回到 ACTIVE。
- 状态的检查与修改在账本的同一个写事务里完成（``repo_intents.set_state``），检查和修改之间没有空隙。
- 幂等键在 intent 表上唯一：同一个键再次 ``create`` 不写第二行，返回已有记录。
- 过期的意图不会被执行：``expire_due`` 把 CREATED/VALIDATED/RESERVED 且到期的置为 EXPIRED；协调者在派发前
  再用 ``is_expired`` 复核一次。
- 订阅 ``FillIngested``：系统成交命中已到终态的意图 → 重开并告警（带系统前缀的成交永远不算外部，F3 第 2 步末条）。
- 意图与订单之间靠事件和账本协作，本模块不导入 ``core.orders``。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import EventBus, FillIngested, IntentStateChanged, Subscription
from dmr_kernel.core.types import (
    AlertLevel,
    IntentAction,
    IntentSource,
    IntentState,
    OrderOrigin,
    PositionSide,
)
from dmr_kernel.ledger import repo_intents
from dmr_kernel.ledger.records import IntentRecord
from dmr_kernel.ledger.writer import Ledger

if TYPE_CHECKING:  # 只用于类型标注：通知器在 alerts（第 2 层），运行时不需要导入
    from dmr_kernel.alerts.notifier import Notifier

log = logging.getLogger(__name__)

S = IntentState

# ---------------------------------------------------------------- 迁移表

TRANSITIONS: dict[IntentState, frozenset[IntentState]] = {
    S.CREATED: frozenset({S.VALIDATED, S.REJECTED, S.EXPIRED, S.CANCELLED}),
    S.VALIDATED: frozenset({S.RESERVED, S.DISPATCH_READY, S.REJECTED, S.EXPIRED, S.CANCELLED}),
    S.RESERVED: frozenset({S.DISPATCH_READY, S.REJECTED, S.EXPIRED, S.CANCELLED}),
    S.DISPATCH_READY: frozenset({S.ACTIVE, S.RECONCILING, S.REJECTED}),
    S.ACTIVE: frozenset({S.RECONCILING, S.COMPLETED, S.REJECTED, S.CANCELLED}),
    S.RECONCILING: frozenset({S.ACTIVE, S.COMPLETED, S.REJECTED}),
    S.COMPLETED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.CANCELLED: frozenset(),
}
# 反过来查：到某个状态，允许从哪些状态来
_LEGAL_FROM: dict[IntentState, frozenset[IntentState]] = {
    to: frozenset(src for src, targets in TRANSITIONS.items() if to in targets) for to in IntentState
}
_TERMINAL = frozenset(IntentState.terminal())
# 还没有发出订单的状态：只有这些状态的意图谈得上「过期」
_NOT_YET_SENT = frozenset({S.CREATED, S.VALIDATED, S.RESERVED, S.DISPATCH_READY})
EXPIRED_REASON = "意图已过期，未执行"


class InvalidTransition(Exception):
    """状态迁移不在迁移表里，或者当前状态不是调用方期望的那个。属于调用方的错误，状态保持不变。"""


# ---------------------------------------------------------------- 意图描述

@dataclass(frozen=True)
class IntentSpec:
    action: IntentAction
    model: str
    source: IntentSource
    symbol: str
    side: PositionSide
    idem_key: str
    qty: Decimal | None = None
    ratio: Decimal | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    ttl_s: int | None = None
    operator: str | None = None
    op_id: str | None = None
    position_id: int | None = None
    position_generation: int | None = None
    parent_intent_id: int | None = None
    ai_eval_ref: str | None = None


def _validate(spec: IntentSpec) -> None:
    for name in ("idem_key", "symbol", "model"):
        value = getattr(spec, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"意图的 {name} 不能为空")
    if spec.ttl_s is not None and (isinstance(spec.ttl_s, bool) or not isinstance(spec.ttl_s, int) or spec.ttl_s <= 0):
        raise ValueError(f"意图有效期必须是正整数秒：{spec.ttl_s!r}")
    for name in ("qty", "ratio"):
        value = getattr(spec, name)
        if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
            raise ValueError(f"意图的 {name} 必须是非负的 Decimal：{value!r}")


# ---------------------------------------------------------------- 幂等键（纯函数）
# 键存在 intent.idem_key 里，格式一旦用过就不能改（改了等于换了一套幂等键）。

def _seg(name: str, value: object) -> str:
    text = str(getattr(value, "value", value))
    if not text.strip():
        raise ValueError(f"幂等键的 {name} 不能为空")
    return text.replace("%", "%25").replace(":", "%3A")  # 组成部分里的冒号转义，保证拼出来的键不会串味


def _side(side: object) -> str:
    return PositionSide(side).value


def _count(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"幂等键的 {name} 必须是非负整数：{value!r}")
    return value


def key_open(model, symbol, side, bar_open_ms, *, reverse_of_generation: int | None = None,
             injected_seq: int | None = None) -> str:
    """开仓：模型 + 合约 + 方向 + 信号 K 线的开盘时间。反手的开仓步另加被平仓位的代际；
    替身环境的信号注入另加注入序号（同一根 K 线可以注入多次）。"""
    key = f"open:{_seg('model', model)}:{_seg('symbol', symbol)}:{_side(side)}:{_count('bar_open_ms', bar_open_ms)}"
    if reverse_of_generation is not None:
        key += f":rev{_count('reverse_of_generation', reverse_of_generation)}"
    if injected_seq is not None:
        key += f":inj{_count('injected_seq', injected_seq)}"
    return key


def key_signal_close(symbol, side, generation, bar_open_ms) -> str:
    """反向信号平仓：合约 + 方向 + 持仓代际 + 信号 K 线的开盘时间。"""
    return (f"sigclose:{_seg('symbol', symbol)}:{_side(side)}:g{_count('generation', generation)}"
            f":{_count('bar_open_ms', bar_open_ms)}")


def key_op(op_id, symbol=None, side=None, leg=None) -> str:
    """操作员或命令行的操作：操作号，批量操作再加合约、方向、分腿（不给的部分留空位，保证不串键）。"""
    parts = [_seg("op_id", op_id),
             _seg("symbol", symbol) if symbol is not None else "",
             _side(side) if side is not None else "",
             _seg("leg", leg) if leg is not None else ""]
    while len(parts) > 1 and parts[-1] == "":
        parts.pop()
    return "op:" + ":".join(parts)


def key_protect(position_id, generation, purpose, seq) -> str:
    """保护动作：持仓编号 + 代际 + 用途 + 序号。"""
    return (f"protect:{_count('position_id', position_id)}:g{_count('generation', generation)}"
            f":{_seg('purpose', purpose)}:{_count('seq', seq)}")


# ---------------------------------------------------------------- 服务

# 保护性条件单的用途（OrderPurpose.STOP、OrderPurpose.DUST_CLOSE 的取值）：它们的成交不是「晚到成交」
_PROTECTIVE_PURPOSES = frozenset({"stop", "dust_close"})


class IntentService:
    def __init__(self, ledger: Ledger, bus: EventBus, clock: Clock, *, notifier: "Notifier | None" = None) -> None:
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._notifier = notifier
        self._subscription: Subscription | None = None

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """订阅 FillIngested（晚到成交）。重复调用无害。"""
        if self._subscription is None:
            self._subscription = self._bus.subscribe(FillIngested, self._on_fill, name="intents.late_fill")

    async def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None

    # ------------------------------------------------------------ 创建与迁移

    async def create(self, spec: IntentSpec) -> tuple[IntentRecord, bool]:
        """先落盘。幂等键重复：不写第二行，返回 ``(已有记录, False)``。"""
        _validate(spec)
        now = self._clock.now_ms()
        expires_ms = None if spec.ttl_s is None else now + spec.ttl_s * 1000
        record, created = await self._ledger.write(
            repo_intents.insert, idem_key=spec.idem_key, action=spec.action, model=spec.model, source=spec.source,
            symbol=spec.symbol, side=spec.side, qty=spec.qty, ratio=spec.ratio, params=spec.params,
            reason=spec.reason, created_ms=now, expires_ms=expires_ms, operator=spec.operator, op_id=spec.op_id,
            position_id=spec.position_id, position_generation=spec.position_generation,
            parent_intent_id=spec.parent_intent_id, ai_eval_ref=spec.ai_eval_ref)
        if created:
            self._publish(record, now)
        return record, created

    async def transition(self, intent_id: int, to: IntentState, *, reason: str = "",
                         expect: set[IntentState] | None = None) -> IntentRecord:
        """按迁移表改状态。``expect`` 给了就要求当前状态在其中（防止并发下基于过时状态做决定）。"""
        now = self._clock.now_ms()
        record, previous, status = await self._ledger.write(
            repo_intents.set_state, intent_id, to, legal_from=_LEGAL_FROM[to],
            expect=None if expect is None else frozenset(expect), reason=reason, now_ms=now)
        if status == repo_intents.MISSING:
            raise KeyError(f"意图 {intent_id} 不存在")
        if status == repo_intents.UNEXPECTED:
            wanted = "、".join(sorted(s.value for s in expect or ()))
            raise InvalidTransition(f"意图 {intent_id} 当前是 {previous.value}，不是期望的 {wanted}；未迁移到 {to.value}")
        if status == repo_intents.ILLEGAL:
            raise InvalidTransition(f"意图 {intent_id} 不允许从 {previous.value} 迁移到 {to.value}")
        self._publish(record, now)
        return record

    async def reopen_for_late_fill(self, intent_id: int, *, reason: str) -> IntentRecord:
        """终态 → ACTIVE（晚到的系统成交）。同一事务写审计记录。非终态的意图不能重开。"""
        now = self._clock.now_ms()
        record, previous, status = await self._ledger.write(
            repo_intents.reopen, intent_id, legal_from=_TERMINAL, reason=reason, now_ms=now)
        if status == repo_intents.MISSING:
            raise KeyError(f"意图 {intent_id} 不存在")
        if status != repo_intents.OK:
            raise InvalidTransition(f"意图 {intent_id} 当前是 {previous.value}，不是终态，不能按晚到成交重开")
        self._publish(record, now)
        return record

    async def expire_due(self, now_ms: int) -> list[IntentRecord]:
        """CREATED/VALIDATED/RESERVED 且已到期 → EXPIRED（过期的意图不会被执行），并释放预占的名额。"""
        expired = await self._ledger.write(repo_intents.expire_due, now_ms, reason=EXPIRED_REASON)
        for record in expired:
            self._publish(record, now_ms)
        return expired

    @staticmethod
    def is_expired(rec: IntentRecord, now_ms: int) -> bool:
        """还没发出订单的意图，在有效期满的那一毫秒起算过期（``now_ms >= expires_ms``）；
        没有有效期的永不过期；已经在执行或已结束的意图不谈过期。"""
        return rec.state in _NOT_YET_SENT and rec.expires_ms is not None and now_ms >= rec.expires_ms

    # ------------------------------------------------------------ 查询

    async def get(self, intent_id: int) -> IntentRecord | None:
        return await self._ledger.read(repo_intents.get, intent_id)

    async def get_by_key(self, idem_key: str) -> IntentRecord | None:
        return await self._ledger.read(repo_intents.get_by_key, idem_key)

    async def non_terminal(self, *, symbol: str | None = None, side: PositionSide | None = None,
                           action: IntentAction | None = None, model: str | None = None) -> list[IntentRecord]:
        return await self._ledger.read(repo_intents.non_terminal, symbol=symbol, side=side, action=action, model=model)

    # ------------------------------------------------------------ 内部

    def _publish(self, record: IntentRecord, at_ms: int) -> None:
        self._bus.publish(IntentStateChanged(intent_id=record.id, state=record.state.value,
                                             action=record.action.value, symbol=record.symbol, side=record.side,
                                             at_ms=at_ms))

    async def _on_fill(self, event: FillIngested) -> None:
        """真正的晚到成交：重开意图并告警（F3 第 2 步末条）。口径（实施记录 R17）：

        - 非系统来源、没有意图号、意图不存在或还没结束的都不管。
        - 保护性条件单（止损、碎仓条件单）本来就会在挂单意图结束之后才触发成交：永不重开。
        - 意图已完成：它自己订单的成交随后入账是常态（REST 应答里就是 FILLED），只有成交落在一张我们判为拒绝或撤销、
          并带来新成交量的订单上（``event.on_dead_order``）才算晚到。
        - 意图已放弃（REJECTED/EXPIRED/CANCELLED）后又收到它订单的成交：一律是晚到成交。
        """
        if event.origin is not OrderOrigin.SYSTEM or event.intent_id is None:
            return
        if event.order_purpose in _PROTECTIVE_PURPOSES:
            return
        record = await self.get(event.intent_id)
        if record is None or record.state not in _TERMINAL:
            return
        if record.state is IntentState.COMPLETED and not event.on_dead_order:
            return
        trade = event.trade
        reason = (f"晚到成交：意图已是 {record.state.value}，仍收到系统订单 {event.client_order_id} 的成交 "
                  f"{trade.trade_id}（{trade.qty}@{trade.price}）")
        try:
            await self.reopen_for_late_fill(record.id, reason=reason)
        except InvalidTransition:
            return  # 读到之后别处已经重开了：不重复告警
        message = (f"意图 {record.id}（{record.symbol} {record.side.value} {record.action.value}）已是 "
                   f"{record.state.value}，却收到系统订单 {event.client_order_id} 的成交 {trade.trade_id}"
                   f"（{trade.qty}@{trade.price}）；已重新打开该意图，请核对持仓与保护单")
        log.warning(message)
        if self._notifier is not None:
            self._notifier.raise_alert(AlertLevel.CRITICAL, "intent_late_fill", message,
                                       dedupe_key=f"intent_late_fill:{record.id}")
