"""持仓登记（position 表与 cooldown 表）的仓储函数。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录、本模块的 ``Change``/``FillResult``
或基本类型。每个写函数在调用方给的同一个事务里完成「读当前行 → 检查状态与版本 → 改 → 写审计 → 必要时生成逐笔交易」，
要么全成要么全不成；单写线程保证两个并发请求看到的一定是前一个提交后的结果。

- **一仓一主**：同一「合约 + 方向」同一时刻只有一条非 closed 的记录，由 ``position`` 表的部分唯一索引保证。
  插入时撞上索引（在保存点里）就抛 ``KeyInUse``，不先查再插。
- **持仓状态版本**：每次登记簿状态变化 +1；``expected_version`` 对不上抛 ``VersionConflict``，什么也不改。
  只更新对账观测值（交易所数量、强平价）不动版本。
- **幂等**：没有实际变化的调用（重复置位、同样的值）返回当前记录，``changed=False``，不加版本。
- 错误：持仓不存在 ``PositionNotFound``（LookupError）；状态不允许 ``IllegalTransition``（ValueError）；
  版本过期 ``VersionConflict``；键被占 ``KeyInUse``。上层（core.registry）把后两者翻译成自己的异常。
- 出场原因需要读意图的原因、来源和操作类型时，直接查 ``intent``、``operation`` 表（不经 core.intents）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import (
    ExitReason,
    ExitRuleset,
    IntentSource,
    OperationType,
    OrderOrigin,
    OrderPurpose,
    Ownership,
    PendingReason,
    PositionSide,
    PositionStatus,
    ProtectionState,
)
from dmr_kernel.ledger import repo_audit, roundtrip
from dmr_kernel.ledger.coltypes import json_dumps, json_loads
from dmr_kernel.ledger.errors import IntegrityError
from dmr_kernel.ledger.models import Cooldown, Fill, Intent, Operation, Position
from dmr_kernel.ledger.records import CooldownRecord, FillRecord, PositionRecord

__all__ = [
    "Change", "FillResult", "IllegalTransition", "KeyInUse", "PRESUMED_STOP_NOTE", "PositionNotFound",
    "VersionConflict", "add_stale_stop", "apply_fill", "begin_takeover", "get", "ignore", "list_cooldowns",
    "list_open", "load_state", "mark_closed", "mark_managed", "register_open", "register_pending",
    "remove_stale_stop", "set_closing", "set_cooldown", "set_handed_over", "set_pending", "set_protection",
    "update_exchange_view",
]

OPERATOR = "system"  # 审计里的操作者：登记簿自己的动作
PRESUMED_STOP_NOTE = "推定为系统止损触发，待条件单对账复核"
EXTERNAL_ADJUST = "外部调整"  # 非系统成交改了数量的 PositionChanged 原因
STOP_SLIP_LONG = Decimal("1.005")  # 多：成交价 ≤ 止损价 × 1.005 视为已越过止损价
STOP_SLIP_SHORT = Decimal("0.995")  # 空：成交价 ≥ 止损价 × 0.995
MARGIN_MODES = ("isolated", "cross")

# 意图原因的开头词 → 出场原因（「PROTECTIVE: 止损挂不上」这类带说明的写法也认）
_REASON_TOKEN = re.compile(r"^(signal|reverse|manual|close_all|protective)(?![0-9A-Za-z_])")
_TOKEN_REASON = {
    "signal": ExitReason.SIGNAL, "reverse": ExitReason.REVERSE, "manual": ExitReason.MANUAL,
    "close_all": ExitReason.CLOSE_ALL, "protective": ExitReason.PROTECTIVE,
}
_OPERATION_REASON = {
    OperationType.CLOSE_ALL: ExitReason.CLOSE_ALL, OperationType.STOP_AND_CLOSE_ALL: ExitReason.CLOSE_ALL,
    OperationType.CLOSE_ONE: ExitReason.MANUAL, OperationType.PARTIAL_CLOSE: ExitReason.MANUAL,
}
_SOURCE_REASON = {
    IntentSource.PROTECTION: ExitReason.PROTECTIVE, IntentSource.SIGNAL: ExitReason.SIGNAL,
    IntentSource.INJECTED: ExitReason.SIGNAL, IntentSource.OPERATOR: ExitReason.MANUAL,
    IntentSource.CLI: ExitReason.MANUAL, IntentSource.TEST_ENTRY: ExitReason.MANUAL,
}
_CODE_MODEL = {"0": "op", "1": "m1", "2": "m2", "3": "m3"}  # 订单编号里的模型代码


# ---------------------------------------------------------------- 结果与异常


class PositionNotFound(LookupError):
    """持仓编号不存在。"""


class IllegalTransition(ValueError):
    """当前状态不允许这个操作。"""


class VersionConflict(Exception):
    """调用方手里的持仓状态版本已经过期。"""


class KeyInUse(Exception):
    """同一「合约 + 方向」已有非 closed 的持仓（部分唯一索引拒绝了插入）。"""

    def __init__(self, symbol: str, side: PositionSide, occupant_id: int, occupant_status: PositionStatus) -> None:
        super().__init__(f"{symbol} {side.value} 方向已有活动持仓（编号 {occupant_id}，状态 {occupant_status.value}）")
        self.symbol = symbol
        self.side = side
        self.occupant_id = occupant_id
        self.occupant_status = occupant_status


@dataclass(frozen=True)
class Change:
    """一次写入的结果。``before`` 为空表示新建；``changed`` 为假表示版本没动（无变化，或只更新了观测值）。"""

    record: PositionRecord
    before: PositionRecord | None = None
    changed: bool = True


@dataclass(frozen=True)
class FillResult:
    """``apply_fill`` 的结果。outcome：

    - ``duplicate``：这笔成交已经应用过，``record`` 是它所属持仓的当前记录；
    - ``registered``：登记了新的策略仓/AI 仓；
    - ``reported``：开仓方向的成交没有对应记录也不是系统开仓，只入账，由调用方报告「未登记持仓」；
    - ``booked``：减仓方向的成交没有对应记录，只入账；
    - ``applied``：改了已有记录的数量（``cause`` 是 PositionChanged 的原因）；
    - ``closed``：数量归零，记录已平并生成了逐笔交易。
    """

    outcome: str
    fill: FillRecord
    record: PositionRecord | None = None
    before: PositionRecord | None = None
    cause: str = ""


# ---------------------------------------------------------------- 读


def get(session: Session, position_id: int) -> PositionRecord | None:
    row = session.get(Position, position_id)
    return None if row is None else PositionRecord.from_row(row)


def list_open(session: Session) -> list[PositionRecord]:
    """所有非 closed 的持仓（待确认、接管中、托管中、平仓中、已忽略），按编号升序。"""
    rows = session.scalars(select(Position).where(Position.status != PositionStatus.CLOSED).order_by(Position.id))
    return [PositionRecord.from_row(row) for row in rows]


def list_cooldowns(session: Session) -> list[CooldownRecord]:
    return [CooldownRecord.from_row(row) for row in session.scalars(select(Cooldown).order_by(Cooldown.symbol))]


def load_state(session: Session) -> tuple[list[PositionRecord], list[CooldownRecord]]:
    """登记簿启动时一次读全：非 closed 的持仓和冷却（同一个读快照）。"""
    return list_open(session), list_cooldowns(session)


# ---------------------------------------------------------------- 内部帮手


def _load(session: Session, position_id: int) -> Position:
    row = session.get(Position, position_id)
    if row is None:
        raise PositionNotFound(f"持仓 {position_id} 不存在")
    return row


def _active_row(session: Session, symbol: str, side: PositionSide) -> Position | None:
    return session.scalars(select(Position).where(
        Position.symbol == symbol, Position.side == side, Position.status != PositionStatus.CLOSED)).first()


def _check_version(pos: Position, expected_version: int | None) -> None:
    if expected_version is not None and pos.version != expected_version:
        raise VersionConflict(f"持仓 {pos.id} 的状态已变化（当前版本 {pos.version}，你看到的是 {expected_version}）")


def _set(pos: Position, **fields: Any) -> bool:
    """只改真正不同的字段（Decimal 按数值比较：95.00 与 95 相同）；返回是否改了。"""
    changed = False
    for name, value in fields.items():
        if getattr(pos, name) != value:
            setattr(pos, name, value)
            changed = True
    return changed


def _finish(session: Session, pos: Position, before: PositionRecord, now_ms: int, changed: bool) -> Change:
    """有变化就加版本、记更新时间；没有变化原样返回当前记录。"""
    if changed:
        pos.version += 1
        pos.updated_ms = now_ms
        session.flush()
    return Change(record=PositionRecord.from_row(pos), before=before, changed=changed)


def _audit(session: Session, now_ms: int, action: str, position_id: int, *, before: Mapping[str, Any] | None = None,
           after: Mapping[str, Any] | None = None) -> None:
    repo_audit.append(session, at_ms=now_ms, operator=OPERATOR, action=action, target=f"position:{position_id}",
                      before=None if before is None else dict(before), after=None if after is None else dict(after))


def _brief(rec: PositionRecord) -> dict[str, Any]:
    return {"status": rec.status.value, "ownership": rec.ownership.value, "qty": str(rec.qty),
            "avg_price": str(rec.avg_price), "is_mixed": rec.is_mixed,
            "exit_ruleset": None if rec.exit_ruleset is None else rec.exit_ruleset.value}


def _new_row(**fields: Any) -> Position:
    base: dict[str, Any] = dict(
        is_mixed=False, pending_reason=None, pending_flags=[], open_time_ms=None, open_time_estimated=False,
        anchor_ms=None, stop_cycle=0, protection_state=ProtectionState.NONE, stop_price=None, stop_client_id=None,
        stop_algo_id=None, stale_stops=[], handed_over_stop=None, stop_note="", exit_ruleset=None, signal_exit=False,
        leverage=None, margin_mode=None, liquidation_price=None, exchange_qty=None, exchange_seen_ms=None,
        consistent=None, origin_note="", opened_by_intent_id=None, list_snapshot_id=None, list_direction=None,
        list_score=None, entry_reason="", exit_reason=None, closed_ms=None, version=1)
    base.update(fields)
    return Position(**base)


def _insert(session: Session, *, symbol: str, side: PositionSide, now_ms: int, **fields: Any) -> Position:
    """插入新记录；代际 = 该键历史上最大代际 + 1。撞上一仓一主的部分唯一索引就抛 KeyInUse。"""
    generation = (session.scalar(select(func.max(Position.generation)).where(
        Position.symbol == symbol, Position.side == side)) or 0) + 1
    row = _new_row(symbol=symbol, side=side, generation=generation, created_ms=now_ms, updated_ms=now_ms, **fields)
    try:
        with session.begin_nested():  # 保存点：撞索引时只回滚这一次插入
            session.add(row)
            session.flush()
    except IntegrityError:
        occupant = _active_row(session, symbol, side)
        if occupant is None:
            raise
        raise KeyInUse(symbol, side, occupant.id, occupant.status) from None
    return row


# ---------------------------------------------------------------- 登记


def register_open(session: Session, *, symbol: str, side: PositionSide, qty: Decimal, avg_price: Decimal,
                  open_time_ms: int, ownership: Ownership, model: str, intent_id: int | None, entry_reason: str,
                  list_snapshot_id: int | None, list_direction: str | None, list_score: Decimal | None,
                  leverage: int | None, margin_mode: str | None, now_ms: int) -> Change:
    """新建托管中的策略仓/AI 仓：保护「无保护」、出场规则集 STRATEGY、``anchor_ms`` = 开仓时间。"""
    row = _insert(
        session, symbol=symbol, side=side, now_ms=now_ms, status=PositionStatus.MANAGED, ownership=ownership,
        qty=qty, avg_price=avg_price, max_qty=qty, open_time_ms=open_time_ms, anchor_ms=open_time_ms,
        exit_ruleset=ExitRuleset.STRATEGY, opened_by_intent_id=intent_id, entry_reason=entry_reason,
        list_snapshot_id=list_snapshot_id, list_direction=list_direction, list_score=list_score,
        leverage=leverage, margin_mode=margin_mode)
    record = PositionRecord.from_row(row)
    _audit(session, now_ms, "position.register", record.id, after={
        **_brief(record), "symbol": symbol, "side": side.value, "generation": record.generation, "model": model,
        "intent_id": intent_id})
    return Change(record=record)


def register_pending(session: Session, *, symbol: str, side: PositionSide, qty: Decimal, avg_price: Decimal,
                     reason: PendingReason, flags: Iterable[str], note: str, now_ms: int) -> Change:
    """新建待确认仓：没有归属、没有出场规则集、不知道开仓时间。"""
    row = _insert(
        session, symbol=symbol, side=side, now_ms=now_ms, status=PositionStatus.PENDING, ownership=Ownership.NONE,
        qty=qty, avg_price=avg_price, max_qty=qty, pending_reason=reason, pending_flags=list(flags),
        origin_note=note)
    record = PositionRecord.from_row(row)
    _audit(session, now_ms, "position.register", record.id, after={
        **_brief(record), "symbol": symbol, "side": side.value, "generation": record.generation,
        "reason": reason.value, "flags": list(record.pending_flags), "note": note})
    return Change(record=record)


def begin_takeover(session: Session, *, symbol: str, side: PositionSide, qty: Decimal, avg_price: Decimal,
                   open_time_ms: int, open_time_estimated: bool, anchor_ms: int, signal_exit: bool,
                   position_id: int | None, expected_version: int | None, now_ms: int) -> Change:
    """待确认 → 接管中（给 position_id），或直接新建「接管中」；归属手动接管，出场规则集 TAKEOVER。"""
    if position_id is None:
        row = _insert(
            session, symbol=symbol, side=side, now_ms=now_ms, status=PositionStatus.TAKING_OVER,
            ownership=Ownership.TAKEOVER, qty=qty, avg_price=avg_price, max_qty=qty, open_time_ms=open_time_ms,
            open_time_estimated=open_time_estimated, anchor_ms=anchor_ms, exit_ruleset=ExitRuleset.TAKEOVER,
            signal_exit=signal_exit)
        record = PositionRecord.from_row(row)
        _audit(session, now_ms, "position.register", record.id, after={
            **_brief(record), "symbol": symbol, "side": side.value, "generation": record.generation,
            "signal_exit": signal_exit, "open_time_estimated": open_time_estimated})
        return Change(record=record)

    pos = _load(session, position_id)
    if pos.status is PositionStatus.CLOSED:
        raise IllegalTransition(f"持仓 {position_id} 已平，不能接管")
    if (pos.symbol, pos.side) != (symbol, side):
        raise IllegalTransition(f"持仓 {position_id} 是 {pos.symbol} {pos.side.value}，与 {symbol} {side.value} 对不上")
    _check_version(pos, expected_version)
    if pos.status is not PositionStatus.PENDING:
        raise IllegalTransition(f"持仓 {position_id} 的状态是 {pos.status.value}，只有待确认仓能转接管")
    before = PositionRecord.from_row(pos)
    _set(pos, status=PositionStatus.TAKING_OVER, ownership=Ownership.TAKEOVER, exit_ruleset=ExitRuleset.TAKEOVER,
         qty=qty, avg_price=avg_price, max_qty=max(pos.max_qty, qty), open_time_ms=open_time_ms,
         open_time_estimated=open_time_estimated, anchor_ms=anchor_ms, signal_exit=signal_exit,
         pending_reason=None, pending_flags=[])
    change = _finish(session, pos, before, now_ms, True)
    _audit(session, now_ms, "position.takeover", pos.id, before=_brief(before), after={
        **_brief(change.record), "signal_exit": signal_exit, "open_time_estimated": open_time_estimated})
    return change


# ---------------------------------------------------------------- 状态迁移与登记簿字段


def mark_managed(session: Session, position_id: int, *, now_ms: int) -> Change:
    """接管中 → 托管中；已是托管中就原样返回。"""
    pos = _load(session, position_id)
    before = PositionRecord.from_row(pos)
    if pos.status is PositionStatus.MANAGED:
        return Change(record=before, before=before, changed=False)
    if pos.status is not PositionStatus.TAKING_OVER:
        raise IllegalTransition(f"持仓 {position_id} 的状态是 {pos.status.value}，只有接管中能标为托管中")
    return _finish(session, pos, before, now_ms, _set(pos, status=PositionStatus.MANAGED))


def set_closing(session: Session, position_id: int, closing: bool, *, now_ms: int) -> Change:
    """托管中 ↔ 平仓中；已在目标状态就原样返回。"""
    pos = _load(session, position_id)
    before = PositionRecord.from_row(pos)
    if pos.status not in (PositionStatus.MANAGED, PositionStatus.CLOSING):
        raise IllegalTransition(f"持仓 {position_id} 的状态是 {pos.status.value}，只有托管中与平仓中能互相切换")
    target = PositionStatus.CLOSING if closing else PositionStatus.MANAGED
    return _finish(session, pos, before, now_ms, _set(pos, status=target))


def set_protection(session: Session, position_id: int, *, state: ProtectionState, stop_price: Decimal | None,
                   stop_client_id: str | None, stop_algo_id: str | None, stop_cycle: int | None, note: str | None,
                   clear_stop: bool, now_ms: int) -> Change:
    """改保护状态与止损信息：没给的字段不动；``clear_stop`` 清掉止损价与两个编号。已平记录也能改（清理要记录进度）。"""
    pos = _load(session, position_id)
    before = PositionRecord.from_row(pos)
    fields: dict[str, Any] = {"protection_state": state}
    if clear_stop:
        fields.update(stop_price=None, stop_client_id=None, stop_algo_id=None)
    else:
        if stop_price is not None:
            fields["stop_price"] = stop_price
        if stop_client_id is not None:
            fields["stop_client_id"] = stop_client_id
        if stop_algo_id is not None:
            fields["stop_algo_id"] = stop_algo_id
    if stop_cycle is not None:
        fields["stop_cycle"] = stop_cycle
    if note is not None:
        fields["stop_note"] = note
    return _finish(session, pos, before, now_ms, _set(pos, **fields))


def add_stale_stop(session: Session, position_id: int, *, client_algo_id: str | None, algo_id: str | None,
                   now_ms: int) -> Change:
    """登记一张待清理的旧止损；同样的一张不重复登记。"""
    pos = _load(session, position_id)
    before = PositionRecord.from_row(pos)
    entry = {"client_algo_id": client_algo_id, "algo_id": algo_id}
    if entry in pos.stale_stops:
        return Change(record=before, before=before, changed=False)
    return _finish(session, pos, before, now_ms, _set(pos, stale_stops=[*pos.stale_stops, entry]))


def remove_stale_stop(session: Session, position_id: int, *, client_algo_id: str | None, algo_id: str | None,
                      now_ms: int) -> Change:
    """记为已处理：自定义编号或条件单号任一对上就移除。"""
    pos = _load(session, position_id)
    before = PositionRecord.from_row(pos)

    def matches(entry: Mapping[str, Any]) -> bool:
        return ((client_algo_id is not None and entry.get("client_algo_id") == client_algo_id)
                or (algo_id is not None and entry.get("algo_id") == algo_id))

    kept = [entry for entry in pos.stale_stops if not matches(entry)]
    return _finish(session, pos, before, now_ms, _set(pos, stale_stops=kept))


def set_handed_over(session: Session, position_id: int, stop: Mapping[str, Any] | None, *, now_ms: int) -> Change:
    """记录（或清除）已移交给用户的止损单。"""
    pos = _load(session, position_id)
    before = PositionRecord.from_row(pos)
    # 先按库里的写法规范化（Decimal 变字符串），再比较有没有变化
    new = None if stop is None else _normalized(stop)
    return _finish(session, pos, before, now_ms, _set(pos, handed_over_stop=new))


def _normalized(value: Mapping[str, Any]) -> dict[str, Any]:
    return json_loads(json_dumps(dict(value)))


def update_exchange_view(session: Session, position_id: int, *, exchange_qty: Decimal,
                         liquidation_price: Decimal | None, seen_ms: int) -> Change:
    """记录对账读到的交易所数量与强平价，并据此标一致性。对账观测值不算登记簿状态变化：不动版本。"""
    pos = _load(session, position_id)
    if pos.status is PositionStatus.CLOSED:
        raise IllegalTransition(f"持仓 {position_id} 已平，不再更新交易所视图")
    before = PositionRecord.from_row(pos)
    if _set(pos, exchange_qty=exchange_qty, liquidation_price=liquidation_price, exchange_seen_ms=seen_ms,
            consistent=exchange_qty == pos.qty):
        session.flush()
    return Change(record=PositionRecord.from_row(pos), before=before, changed=False)


def ignore(session: Session, position_id: int, *, expected_version: int | None, now_ms: int) -> Change:
    """待确认/接管 → 外部（已忽略）。保留来源与止损编号（取消托管后要撤的止损还得认得出来）。"""
    pos = _load(session, position_id)
    _check_version(pos, expected_version)
    if pos.status is PositionStatus.CLOSED:
        raise IllegalTransition(f"持仓 {position_id} 已平，不能忽略")
    before = PositionRecord.from_row(pos)
    if pos.status is PositionStatus.IGNORED:
        return Change(record=before, before=before, changed=False)
    if pos.ownership in (Ownership.STRATEGY, Ownership.AI):
        raise IllegalTransition(f"持仓 {position_id} 是系统自己开的仓，不能当作外部仓忽略")
    _set(pos, status=PositionStatus.IGNORED, ownership=Ownership.NONE, exit_ruleset=None, signal_exit=False)
    change = _finish(session, pos, before, now_ms, True)
    _audit(session, now_ms, "position.ignore", pos.id, before=_brief(before), after=_brief(change.record))
    return change


def set_pending(session: Session, position_id: int, *, reason: PendingReason, flags: Iterable[str], note: str,
                now_ms: int) -> Change:
    """待确认仓重新归类，或把已忽略的恢复为待确认。"""
    pos = _load(session, position_id)
    if pos.status not in (PositionStatus.PENDING, PositionStatus.IGNORED):
        raise IllegalTransition(f"持仓 {position_id} 的状态是 {pos.status.value}，只有待确认与已忽略能设为待确认")
    before = PositionRecord.from_row(pos)
    changed = _set(pos, status=PositionStatus.PENDING, ownership=Ownership.NONE, exit_ruleset=None, signal_exit=False,
                   pending_reason=reason, pending_flags=list(flags), origin_note=note)
    change = _finish(session, pos, before, now_ms, changed)
    if changed and before.status is PositionStatus.IGNORED:
        _audit(session, now_ms, "position.restore", pos.id, before=_brief(before), after=_brief(change.record))
    return change


def _close(session: Session, pos: Position, *, exit_reason: ExitReason, closed_ms: int, now_ms: int,
           note: str = "") -> PositionRecord:
    """把一条非 closed 的记录置为已平：数量归零、记出场原因，生成逐笔交易，写审计。"""
    before = PositionRecord.from_row(pos)
    pos.status = PositionStatus.CLOSED
    pos.qty = Decimal(0)
    pos.exit_reason = exit_reason
    pos.closed_ms = closed_ms
    if note:
        pos.stop_note = note
    pos.version += 1
    pos.updated_ms = now_ms
    session.flush()
    trade = roundtrip.build_trade(session, pos.id)
    _audit(session, now_ms, "position.close", pos.id, before=_brief(before), after={
        "status": "closed", "exit_reason": exit_reason.value, "closed_ms": closed_ms, "cause": note,
        "trade_id": trade.id, "net_pnl": str(trade.net_pnl)})
    return PositionRecord.from_row(pos)


def mark_closed(session: Session, position_id: int, *, exit_reason: ExitReason, closed_ms: int, now_ms: int) -> Change:
    """任何非 closed 状态 → 已平；写 trade。已平的记录原样返回（出场原因以第一次为准）。"""
    pos = _load(session, position_id)
    before = PositionRecord.from_row(pos)
    if pos.status is PositionStatus.CLOSED:
        return Change(record=before, before=before, changed=False)
    return Change(record=_close(session, pos, exit_reason=exit_reason, closed_ms=closed_ms, now_ms=now_ms),
                  before=before)


# ---------------------------------------------------------------- 冷却


def set_cooldown(session: Session, *, symbol: str, until_ms: int, reason: str, set_ms: int) -> CooldownRecord:
    """每个合约一行；后一次覆盖前一次，哪怕更短。"""
    row = session.get(Cooldown, symbol)
    if row is None:
        row = Cooldown(symbol=symbol, until_ms=until_ms, reason=reason, set_ms=set_ms)
        session.add(row)
    else:
        row.until_ms, row.reason, row.set_ms = until_ms, reason, set_ms
    session.flush()
    return CooldownRecord.from_row(row)


# ---------------------------------------------------------------- 成交应用


def _int_param(params: Mapping[str, Any], key: str) -> int | None:
    """意图参数里的整数；格式不对或大得离谱（数据库整数装不下）就当没给，不能挡住登记。"""
    value = params.get(key)
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, int) or not -(2 ** 31) <= value <= 2 ** 31:
        return None
    return value


def _decimal_param(params: Mapping[str, Any], key: str) -> Decimal | None:
    value = params.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _text_param(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    return value if isinstance(value, str) and value else None


def apply_fill(session: Session, *, fill_id: int, origin: OrderOrigin, purpose: str | None, intent_id: int | None,
               model_code: str | None, now_ms: int, default_leverage: int | None,
               default_margin_mode: str | None) -> FillResult:
    """把一笔已入账的成交应用到持仓登记（规则见 core.registry.Registry.apply_fill）。

    ``purpose`` 是订单用途（open/close/partial_close/stop/dust_close；事件没带时由调用方按编号分腿推出），
    ``model_code`` 是订单编号里的模型代码（读不到意图行时用来判断归属）。成交不在账本里抛 LookupError。
    应用过的成交（``fill.position_id`` 已回填）再来一次不产生任何影响。
    """
    fill = session.get(Fill, fill_id)
    if fill is None:
        raise LookupError(f"成交 {fill_id} 不在账本里，无法应用到持仓登记")
    fill_rec = FillRecord.from_row(fill)
    if fill.position_id is not None:
        owner = session.get(Position, fill.position_id)
        return FillResult("duplicate", fill_rec, record=None if owner is None else PositionRecord.from_row(owner))

    side = fill.position_side
    opening = fill.side == side.open_side()
    pos = _active_row(session, fill.symbol, side)
    if pos is None:
        if not opening:
            return FillResult("booked", fill_rec)
        if origin is OrderOrigin.SYSTEM and purpose == OrderPurpose.OPEN.value:
            return _register_from_fill(session, fill, fill_rec, intent_id=intent_id, model_code=model_code,
                                       now_ms=now_ms, default_leverage=default_leverage,
                                       default_margin_mode=default_margin_mode)
        return FillResult("reported", fill_rec)

    before = PositionRecord.from_row(pos)
    system = origin is OrderOrigin.SYSTEM
    cause = "qty_change" if pos.status is PositionStatus.PENDING else ("fill" if system else EXTERNAL_ADJUST)
    if opening:
        qty = pos.qty + fill.qty
        fields: dict[str, Any] = dict(
            qty=qty, avg_price=(pos.qty * pos.avg_price + fill.qty * fill.price) / qty, max_qty=max(pos.max_qty, qty))
        mixed_now = (not system and pos.ownership in (Ownership.STRATEGY, Ownership.AI) and not pos.is_mixed)
        if mixed_now:  # 策略仓被非系统成交加仓：成了混合仓，不再反手、不加仓
            fields.update(is_mixed=True, exit_ruleset=ExitRuleset.STRATEGY_NO_REVERSE)
        _set(pos, **fields)
        fill.position_id = pos.id
        change = _finish(session, pos, before, now_ms, True)
        if mixed_now:
            _audit(session, now_ms, "position.mixed", pos.id, before=_brief(before),
                   after={**_brief(change.record), "fill_id": fill.id})
        return FillResult("applied", fill_rec, record=change.record, before=before, cause=cause)

    remaining = pos.qty - fill.qty
    fill.position_id = pos.id
    if remaining > 0:
        _set(pos, qty=remaining)
        change = _finish(session, pos, before, now_ms, True)
        return FillResult("applied", fill_rec, record=change.record, before=before, cause=cause)
    session.flush()  # 先把 position_id 写进成交行：逐笔交易按它汇总
    exit_reason, note = _exit_reason(session, pos, fill, origin=origin, purpose=purpose, intent_id=intent_id)
    record = _close(session, pos, exit_reason=exit_reason, closed_ms=fill.time_ms, now_ms=now_ms, note=note)
    return FillResult("closed", fill_rec, record=record, before=before)


def _register_from_fill(session: Session, fill: Fill, fill_rec: FillRecord, *, intent_id: int | None,
                        model_code: str | None, now_ms: int, default_leverage: int | None,
                        default_margin_mode: str | None) -> FillResult:
    """系统开仓成交、该键没有记录：登记策略仓（m3 → AI 仓）。晚到的成交即使意图已到终态也登记。"""
    intent = None if intent_id is None else session.get(Intent, intent_id)
    params: Mapping[str, Any] = intent.params if intent is not None and isinstance(intent.params, dict) else {}
    model = intent.model if intent is not None else _CODE_MODEL.get(model_code or "", "")
    margin_mode = _text_param(params, "margin_mode")
    change = register_open(
        session, symbol=fill.symbol, side=fill.position_side, qty=fill.qty, avg_price=fill.price,
        open_time_ms=fill.time_ms, ownership=Ownership.AI if model == "m3" else Ownership.STRATEGY, model=model,
        intent_id=intent_id, entry_reason=intent.reason if intent is not None else "",
        list_snapshot_id=_int_param(params, "list_snapshot_id"), list_direction=_text_param(params, "list_direction"),
        list_score=_decimal_param(params, "list_score"),
        leverage=_int_param(params, "leverage") or default_leverage,
        margin_mode=margin_mode if margin_mode in MARGIN_MODES else default_margin_mode, now_ms=now_ms)
    fill.position_id = change.record.id
    session.flush()
    return FillResult("registered", fill_rec, record=change.record)


def _exit_reason(session: Session, pos: Position, fill: Fill, *, origin: OrderOrigin, purpose: str | None,
                 intent_id: int | None) -> tuple[ExitReason, str]:
    """归零那笔成交对应的出场原因（和要记下的说明）。

    强平/自动减仓/应急按来源；系统止损单 → STOP；系统平仓单按意图的原因；其它（非系统编号）先看是不是
    「推定为系统止损触发」，都不是就是外部平仓。
    """
    if origin is OrderOrigin.LIQUIDATION:
        return ExitReason.LIQUIDATION, ""
    if origin is OrderOrigin.ADL:
        return ExitReason.ADL, ""
    if origin is OrderOrigin.EMERGENCY:
        return ExitReason.EMERGENCY, ""
    if origin is OrderOrigin.SYSTEM:
        if purpose == OrderPurpose.STOP.value:
            return ExitReason.STOP, ""
        return _intent_exit_reason(session, intent_id), ""
    if _presumed_system_stop(pos, fill):
        return ExitReason.STOP, PRESUMED_STOP_NOTE
    return ExitReason.EXTERNAL, ""


def _presumed_system_stop(pos: Position, fill: Fill) -> bool:
    """实测第 12 项「触发后的订单号不可关联」时的保守做法：非系统编号的平仓成交，若该仓是系统保护且有止损价、
    成交价已越过止损价（多：≤ 止损价 × 1.005；空：≥ 止损价 × 0.995）、成交数量等于持仓，按系统止损处理。"""
    if pos.protection_state is not ProtectionState.SYSTEM or pos.stop_price is None or fill.qty != pos.qty:
        return False
    if pos.side is PositionSide.LONG:
        return fill.price <= pos.stop_price * STOP_SLIP_LONG
    return fill.price >= pos.stop_price * STOP_SLIP_SHORT


def _intent_exit_reason(session: Session, intent_id: int | None) -> ExitReason:
    """系统平仓单：意图的原因里的开头词 → 意图所属操作的类型 → 意图来源；读不到意图就是「未知」。"""
    intent = None if intent_id is None else session.get(Intent, intent_id)
    if intent is None:
        return ExitReason.UNKNOWN
    token = _REASON_TOKEN.match((intent.reason or "").strip().lower())
    if token:
        return _TOKEN_REASON[token.group(1)]
    if intent.op_id:
        operation = session.scalars(select(Operation).where(Operation.op_id == intent.op_id)).first()
        if operation is not None and operation.op_type in _OPERATION_REASON:
            return _OPERATION_REASON[operation.op_type]
    return _SOURCE_REASON.get(intent.source, ExitReason.UNKNOWN)
