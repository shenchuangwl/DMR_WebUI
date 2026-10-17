"""账本的记录类型（第 1 层）：每张表一个冻结数据类，字段与列同名、同顺序（docs/设计约定.md 第 8 节）。

仓储函数（``repo_*.py``）只返回这些记录或基本类型，不返回 ORM 对象；``Ledger.write``/``Ledger.read`` 会检查。

- ``Record.from_row(行)``：行可以是 ORM 对象（``session.get``/``session.scalars``），也可以是按表或按列选出的
  Core 行（``session.execute(select(表))``）。取值统一成记录的类型：枚举列是枚举成员，Decimal 列是 Decimal
  （int 也转成 Decimal），布尔列是 bool，JSON 列是一份独立的副本，并按「存进库再取出来」的样子规范化
  （Decimal 变成字符串、元组变成列表），所以无论从哪种行生成、刷不刷新，记录都与重新读库得到的一致。
  不接受未声明类型的文本查询结果里的 JSON 文本（分不清 JSON 文本和字符串值）。
- 口令哈希、令牌哈希不出现在 repr 里（日志里不会带出来）。
- ``as_record(ORM 对象)``：按对象所在的表挑记录类型。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from functools import partial
from typing import Any, ClassVar

from sqlalchemy.types import Boolean

from dmr_kernel.core.types import (
    AlertLevel,
    ExitReason,
    ExitRuleset,
    FillSource,
    Gate,
    IntentAction,
    IntentSource,
    IntentState,
    Model,
    OperationStatus,
    OperationType,
    OrderOrigin,
    OrderPurpose,
    OrderSide,
    OrderState,
    Ownership,
    PendingReason,
    PositionSide,
    PositionStatus,
    ProtectionState,
    RunMode,
    WorkingType,
)
from dmr_kernel.ledger import models
from dmr_kernel.ledger.coltypes import DecimalText, EnumText, JSONText, json_dumps, json_loads

__all__ = [
    "RECORD_TYPES", "AlertRecord", "AuditLogRecord", "CandleRecord", "ConfigItemRecord", "CooldownRecord",
    "FillRecord", "IncomeRecord", "InstrumentRecord", "IntentRecord", "ListMemberRecord", "ListSnapshotRecord",
    "MetaRecord", "ModelStateRecord", "ModelSwitchLogRecord", "OperationRecord", "OperatorRecord", "OrderRecord",
    "PositionRecord", "ReconcileDiffRecord", "ReconcileRunRecord", "Record", "SeatRecord", "SessionRecord",
    "StopHistoryRecord", "TradeRecord", "as_record",
]


# ---------------------------------------------------------------- 取值规范化


def _identity(value: Any) -> Any:
    return value


def _to_enum(enum_type: type[Enum], value: Any) -> Any:
    if value is None or isinstance(value, enum_type):
        return value
    if isinstance(value, Enum):
        raise TypeError(f"{enum_type.__name__} 列里出现了 {type(value).__name__}.{value.name}")
    return enum_type(value)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"Decimal 列里出现了 {type(value).__name__}：{value!r}")
    if isinstance(value, (int, str)):
        return Decimal(value)  # int 来自 ORM 对象上的赋值；str 是库里存的文本
    raise TypeError(f"Decimal 列里出现了 {type(value).__name__}：{value!r}")


def _to_bool(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise TypeError(f"布尔列里出现了 {type(value).__name__}：{value!r}")


def _copy_json(value: Any) -> Any:
    return None if value is None else json_loads(json_dumps(value))


def _converter(column: Any) -> Callable[[Any], Any]:
    col_type = column.type
    if isinstance(col_type, EnumText):
        return partial(_to_enum, col_type.enum_type)
    if isinstance(col_type, DecimalText):
        return _to_decimal
    if isinstance(col_type, JSONText):
        return _copy_json
    if isinstance(col_type, Boolean):
        return _to_bool
    return _identity


class Record:
    """记录的公共部分（不是数据类本身）。子类用 ``@dataclass(frozen=True, slots=True)``，并设置 ``_table``。"""

    __slots__ = ()
    _table: ClassVar[str]
    _converters: ClassVar[tuple[tuple[str, Callable[[Any], Any]], ...]]

    @classmethod
    def _plan(cls) -> tuple[tuple[str, Callable[[Any], Any]], ...]:
        plan = cls.__dict__.get("_converters")
        if plan is None:
            table = models.Base.metadata.tables[cls._table]
            plan = tuple((column.name, _converter(column)) for column in table.columns)
            cls._converters = plan
        return plan

    @classmethod
    def from_row(cls, row: Any) -> Any:
        """从 ORM 对象或 Core 行生成记录（见模块说明）。"""
        if not isinstance(row, models.Base) and hasattr(row, "_mapping"):
            if len(row) == 1 and isinstance(row[0], models.Base):
                row = row[0]  # select(模型) 得到的单实体行
        if isinstance(row, models.Base):
            table_name = type(row).__table__.name
            if table_name != cls._table:
                raise TypeError(f"{cls.__name__} 是表 {cls._table} 的记录，不能用表 {table_name} 的对象生成")

            def get(name: str) -> Any:
                return getattr(row, name)
        else:
            mapping = row._mapping if hasattr(row, "_mapping") else row
            if not isinstance(mapping, Mapping):
                raise TypeError(f"{cls.__name__}.from_row 只接受 ORM 对象或数据库行，收到 {type(row).__name__}")

            def get(name: str) -> Any:
                try:
                    return mapping[name]
                except KeyError:
                    raise TypeError(f"{cls.__name__}.from_row：行里没有列 {name!r}") from None
        return cls(**{name: convert(get(name)) for name, convert in cls._plan()})


def as_record(obj: Any) -> Any:
    """按 ORM 对象所在的表挑记录类型生成记录。"""
    if not isinstance(obj, models.Base):
        raise TypeError(f"as_record 只接受账本的 ORM 对象，收到 {type(obj).__name__}")
    return RECORD_TYPES[type(obj).__table__.name].from_row(obj)


# ---------------------------------------------------------------- 每张表一个记录（字段顺序与列一致）


@dataclass(frozen=True, slots=True)
class MetaRecord(Record):
    _table = "meta"
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class InstrumentRecord(Record):
    _table = "instrument"
    symbol: str
    base: str
    quote: str
    contract_type: str
    status: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    market_step_size: Decimal | None
    market_max_qty: Decimal | None
    onboard_ms: int | None
    updated_ms: int


@dataclass(frozen=True, slots=True)
class PositionRecord(Record):
    _table = "position"
    id: int
    symbol: str
    side: PositionSide
    generation: int
    status: PositionStatus
    ownership: Ownership
    is_mixed: bool
    pending_reason: PendingReason | None
    pending_flags: list[Any]
    qty: Decimal
    avg_price: Decimal
    max_qty: Decimal
    open_time_ms: int | None
    open_time_estimated: bool
    anchor_ms: int | None
    stop_cycle: int
    protection_state: ProtectionState
    stop_price: Decimal | None
    stop_client_id: str | None
    stop_algo_id: str | None
    stale_stops: list[Any]
    handed_over_stop: dict[str, Any] | None
    stop_note: str
    exit_ruleset: ExitRuleset | None
    signal_exit: bool
    leverage: int | None
    margin_mode: str | None
    liquidation_price: Decimal | None
    exchange_qty: Decimal | None
    exchange_seen_ms: int | None
    consistent: bool | None
    origin_note: str
    opened_by_intent_id: int | None
    list_snapshot_id: int | None
    list_direction: str | None
    list_score: Decimal | None
    entry_reason: str
    exit_reason: ExitReason | None
    closed_ms: int | None
    version: int
    created_ms: int
    updated_ms: int


@dataclass(frozen=True, slots=True)
class IntentRecord(Record):
    _table = "intent"
    id: int
    idem_key: str
    action: IntentAction
    model: str
    source: IntentSource
    symbol: str
    side: PositionSide
    qty: Decimal | None
    ratio: Decimal | None
    params: dict[str, Any]
    reason: str
    state: IntentState
    state_reason: str
    created_ms: int
    expires_ms: int | None
    updated_ms: int
    operator: str | None
    op_id: str | None
    position_id: int | None
    position_generation: int | None
    parent_intent_id: int | None
    ai_eval_ref: str | None
    reserved_slot: bool


@dataclass(frozen=True, slots=True)
class OrderRecord(Record):
    _table = "orders"
    client_order_id: str
    intent_id: int | None
    leg: str
    purpose: OrderPurpose
    symbol: str
    side: OrderSide
    position_side: PositionSide
    kind: str
    order_type: str
    qty: Decimal | None
    trigger_price: Decimal | None
    working_type: WorkingType | None
    close_position: bool
    exchange_order_id: str | None
    algo_id: str | None
    actual_order_id: str | None
    state: OrderState
    raw_status: str | None
    filled_qty: Decimal
    avg_price: Decimal | None
    error_code: int | None
    error_msg: str | None
    sent_ms: int | None
    acked_ms: int | None
    last_query_ms: int | None
    absent_count: int
    resend_of: str | None
    created_ms: int
    updated_ms: int


@dataclass(frozen=True, slots=True)
class FillRecord(Record):
    _table = "fill"
    id: int
    trade_id: str
    exchange_order_id: str
    client_order_id: str | None
    symbol: str
    side: OrderSide
    position_side: PositionSide
    qty: Decimal
    price: Decimal
    commission: Decimal
    commission_asset: str
    commission_usdt: Decimal | None
    realized_pnl: Decimal
    time_ms: int
    source: FillSource
    origin: OrderOrigin
    position_id: int | None
    intent_id: int | None
    path: str
    created_ms: int


@dataclass(frozen=True, slots=True)
class IncomeRecord(Record):
    _table = "income"
    id: int
    income_id: str
    income_type: str
    symbol: str | None
    amount: Decimal
    asset: str
    time_ms: int
    position_id: int | None
    info: str


@dataclass(frozen=True, slots=True)
class StopHistoryRecord(Record):
    _table = "stop_history"
    id: int
    position_id: int
    price: Decimal | None
    at_ms: int



@dataclass(frozen=True, slots=True)
class TradeRecord(Record):
    _table = "trade"
    id: int
    position_id: int
    symbol: str
    side: PositionSide
    ownership: Ownership
    model: str | None
    is_mixed: bool
    open_ms: int | None
    close_ms: int
    open_time_estimated: bool
    max_qty: Decimal
    open_avg: Decimal | None
    close_avg: Decimal | None
    open_notional: Decimal | None
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    net_pnl: Decimal
    entry_reason: str
    exit_reason: ExitReason
    list_snapshot_id: int | None
    list_direction: str | None
    list_score: Decimal | None


@dataclass(frozen=True, slots=True)
class ListSnapshotRecord(Record):
    _table = "list_snapshot"
    id: int
    fetched_ms: int
    http_status: int | None
    business_date: str | None
    scan_id: str | None
    valid: bool | None
    valid_until_ms: int | None
    status: str | None
    accepted: bool
    reason: str
    member_count: int


@dataclass(frozen=True, slots=True)
class ListMemberRecord(Record):
    _table = "list_member"
    id: int
    snapshot_id: int
    symbol: str
    direction: str | None
    score: Decimal | None
    first_selected_ms: int | None
    tradable: bool | None
    note: str


@dataclass(frozen=True, slots=True)
class ModelStateRecord(Record):
    _table = "model_state"
    id: int
    active_model: Model
    gate: Gate
    suppressions: list[Any]
    op_locks: list[Any]
    op_lock_owner: str | None
    op_lock_expires_ms: int | None
    run_mode: RunMode
    version: int
    m2_started_ms: int | None
    mismatch_since_ms: int | None
    risk_detail: dict[str, Any] | None
    updated_ms: int


@dataclass(frozen=True, slots=True)
class ModelSwitchLogRecord(Record):
    _table = "model_switch_log"
    id: int
    at_ms: int
    from_model: str | None
    to_model: str
    mode: str | None
    result: str
    op_id: str | None
    operator: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class OperationRecord(Record):
    _table = "operation"
    id: int
    op_id: str
    op_type: OperationType
    scope: str | None
    status: OperationStatus
    operator: str | None
    session_id: int | None
    state_version: int | None
    params: dict[str, Any]
    per_position: Any
    result: Any
    started_ms: int
    ended_ms: int | None


@dataclass(frozen=True, slots=True)
class AuditLogRecord(Record):
    _table = "audit_log"
    id: int
    at_ms: int
    operator: str
    action: str
    target: str
    before: Any
    after: Any
    request_id: str | None
    result: str


@dataclass(frozen=True, slots=True)
class ReconcileRunRecord(Record):
    _table = "reconcile_run"
    id: int
    at_ms: int
    kind: str
    status: str
    duration_ms: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class ReconcileDiffRecord(Record):
    _table = "reconcile_diff"
    id: int
    run_id: int
    symbol: str
    side: PositionSide | None
    local_qty: Decimal | None
    exchange_qty: Decimal | None
    kind: str
    resolved_ms: int | None
    note: str


@dataclass(frozen=True, slots=True)
class AlertRecord(Record):
    _table = "alert"
    id: int
    at_ms: int
    level: AlertLevel
    kind: str
    message: str
    delivered: bool
    delivered_ms: int | None
    attempts: int


@dataclass(frozen=True, slots=True)
class ConfigItemRecord(Record):
    _table = "config_item"
    key: str
    value: Any
    updated_by: str | None
    updated_ms: int


@dataclass(frozen=True, slots=True)
class OperatorRecord(Record):
    _table = "operator"
    id: int
    username: str
    password_hash: str = field(repr=False)
    role: str
    created_ms: int
    disabled: bool


@dataclass(frozen=True, slots=True)
class SessionRecord(Record):
    _table = "session"
    id: int
    token_hash: str = field(repr=False)
    operator_id: int
    created_ms: int
    last_active_ms: int
    reauth_ms: int | None
    revoked: bool
    client_info: str


@dataclass(frozen=True, slots=True)
class SeatRecord(Record):
    _table = "seat"
    id: int
    session_id: int | None
    lease_until_ms: int | None
    acquired_ms: int | None


@dataclass(frozen=True, slots=True)
class CooldownRecord(Record):
    _table = "cooldown"
    symbol: str
    until_ms: int
    reason: str
    set_ms: int


@dataclass(frozen=True, slots=True)
class CandleRecord(Record):
    _table = "candle"
    symbol: str
    timeframe: str
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


RECORD_TYPES: dict[str, type[Record]] = {
    record_type._table: record_type
    for record_type in (
        MetaRecord, InstrumentRecord, PositionRecord, IntentRecord, OrderRecord, FillRecord, IncomeRecord,
        TradeRecord, ListSnapshotRecord, ListMemberRecord, ModelStateRecord, ModelSwitchLogRecord,
        OperationRecord, AuditLogRecord, ReconcileRunRecord, ReconcileDiffRecord, AlertRecord, ConfigItemRecord,
        OperatorRecord, SessionRecord, SeatRecord, CooldownRecord, CandleRecord, StopHistoryRecord,
    )
}
