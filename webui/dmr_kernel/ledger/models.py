"""账本的表结构（第 1 层，SQLAlchemy 2 声明式 ORM）。严格照 docs/设计约定.md 第 8 节。

约定：
- 主键 ``id INTEGER``；时间 ``*_ms INTEGER``（UTC 毫秒）；Decimal、枚举、JSON 都存 TEXT（见 coltypes）；
  K 线的开高低收量是 REAL。属性名与列名相同，records.py 的字段与列一一对应。
- 只在 ledger/ 里使用。上层经 ``repo_*.py`` 的函数读写，拿到的是 records.py 的记录，不是这里的对象。
- JSON 列不跟踪原地修改：改列表或字典要整体重新赋值（``pos.pending_flags = [*pos.pending_flags, "x"]``）。
- 默认值只在 Python 侧（插入一律经 ORM）。约束名由命名约定生成，迁移里写同样的名字。
- 改表结构要同时加一个迁移（批量模式）。``tests/unit/ledger/test_ledger_migrations.py`` 比对模型与迁移建出来的库。
  注意：批量模式重建 ``audit_log`` 时，触发器会随旧表一起删掉，迁移里要重新建。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import DDL, REAL, Boolean, CheckConstraint, ForeignKey, Index, Integer, MetaData, Text, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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
from dmr_kernel.ledger.coltypes import DecimalText, EnumText, JSONText

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# 审计日志只追加：库里的触发器拒绝一切 UPDATE 与 DELETE（迁移 0001 里有同样的两句）
AUDIT_LOG_TRIGGERS = (
    "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log 只追加，不允许修改'); END",
    "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log 只追加，不允许删除'); END",
)
# 一仓一主：同一「合约 + 方向」同一时刻只有一条非 closed 的持仓记录
POSITION_ACTIVE_KEY = "uq_position_active_key"
POSITION_ACTIVE_WHERE = "status != 'closed'"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _empty_list() -> list[Any]:
    return []


def _empty_dict() -> dict[str, Any]:
    return {}


class Meta(Base):
    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Instrument(Base):
    """合约规则，运行时从交易所读取。"""

    __tablename__ = "instrument"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    base: Mapped[str] = mapped_column(Text)
    quote: Mapped[str] = mapped_column(Text)
    contract_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    tick_size: Mapped[Decimal] = mapped_column(DecimalText)
    step_size: Mapped[Decimal] = mapped_column(DecimalText)
    min_qty: Mapped[Decimal] = mapped_column(DecimalText)
    min_notional: Mapped[Decimal] = mapped_column(DecimalText)
    market_step_size: Mapped[Decimal | None] = mapped_column(DecimalText)
    market_max_qty: Mapped[Decimal | None] = mapped_column(DecimalText)
    onboard_ms: Mapped[int | None] = mapped_column(Integer)
    updated_ms: Mapped[int] = mapped_column(Integer)


class Position(Base):
    """持仓登记。generation 是该「合约 + 方向」第几次从零开仓；version 是持仓状态版本。"""

    __tablename__ = "position"
    __table_args__ = (
        Index(POSITION_ACTIVE_KEY, "symbol", "side", unique=True, sqlite_where=text(POSITION_ACTIVE_WHERE)),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[PositionSide] = mapped_column(EnumText(PositionSide))
    generation: Mapped[int] = mapped_column(Integer)
    status: Mapped[PositionStatus] = mapped_column(EnumText(PositionStatus))
    ownership: Mapped[Ownership] = mapped_column(EnumText(Ownership))
    is_mixed: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_reason: Mapped[PendingReason | None] = mapped_column(EnumText(PendingReason))
    pending_flags: Mapped[list[Any]] = mapped_column(JSONText, default=_empty_list)
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    avg_price: Mapped[Decimal] = mapped_column(DecimalText)
    max_qty: Mapped[Decimal] = mapped_column(DecimalText)
    open_time_ms: Mapped[int | None] = mapped_column(Integer)
    open_time_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    anchor_ms: Mapped[int | None] = mapped_column(Integer)  # 6 根 K 线计数的起点
    stop_cycle: Mapped[int] = mapped_column(Integer, default=0)
    protection_state: Mapped[ProtectionState] = mapped_column(EnumText(ProtectionState), default=ProtectionState.NONE)
    stop_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    stop_client_id: Mapped[str | None] = mapped_column(Text)
    stop_algo_id: Mapped[str | None] = mapped_column(Text)
    stale_stops: Mapped[list[Any]] = mapped_column(JSONText, default=_empty_list)  # [{"client_algo_id","algo_id"}]
    handed_over_stop: Mapped[dict[str, Any] | None] = mapped_column(JSONText)
    stop_note: Mapped[str] = mapped_column(Text, default="")
    exit_ruleset: Mapped[ExitRuleset | None] = mapped_column(EnumText(ExitRuleset))
    signal_exit: Mapped[bool] = mapped_column(Boolean, default=False)
    leverage: Mapped[int | None] = mapped_column(Integer)
    margin_mode: Mapped[str | None] = mapped_column(Text)
    liquidation_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    exchange_qty: Mapped[Decimal | None] = mapped_column(DecimalText)
    exchange_seen_ms: Mapped[int | None] = mapped_column(Integer)
    consistent: Mapped[bool | None] = mapped_column(Boolean)  # 空表示还没对过账
    origin_note: Mapped[str] = mapped_column(Text, default="")
    opened_by_intent_id: Mapped[int | None] = mapped_column(Integer)
    list_snapshot_id: Mapped[int | None] = mapped_column(Integer)
    list_direction: Mapped[str | None] = mapped_column(Text)
    list_score: Mapped[Decimal | None] = mapped_column(DecimalText)
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    exit_reason: Mapped[ExitReason | None] = mapped_column(EnumText(ExitReason))
    closed_ms: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_ms: Mapped[int] = mapped_column(Integer)
    updated_ms: Mapped[int] = mapped_column(Integer)


class Intent(Base):
    """交易意图（10 态）。model 取 m1/m2/m3/op；ai_eval_ref 是模型 3 的接入位。"""

    __tablename__ = "intent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idem_key: Mapped[str] = mapped_column(Text, unique=True)
    action: Mapped[IntentAction] = mapped_column(EnumText(IntentAction))
    model: Mapped[str] = mapped_column(Text)
    source: Mapped[IntentSource] = mapped_column(EnumText(IntentSource))
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[PositionSide] = mapped_column(EnumText(PositionSide))
    qty: Mapped[Decimal | None] = mapped_column(DecimalText)
    ratio: Mapped[Decimal | None] = mapped_column(DecimalText)
    params: Mapped[dict[str, Any]] = mapped_column(JSONText, default=_empty_dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[IntentState] = mapped_column(EnumText(IntentState))
    state_reason: Mapped[str] = mapped_column(Text, default="")
    created_ms: Mapped[int] = mapped_column(Integer)
    expires_ms: Mapped[int | None] = mapped_column(Integer)
    updated_ms: Mapped[int] = mapped_column(Integer)
    operator: Mapped[str | None] = mapped_column(Text)
    op_id: Mapped[str | None] = mapped_column(Text)
    position_id: Mapped[int | None] = mapped_column(Integer)
    position_generation: Mapped[int | None] = mapped_column(Integer)
    parent_intent_id: Mapped[int | None] = mapped_column(Integer)
    ai_eval_ref: Mapped[str | None] = mapped_column(Text)
    reserved_slot: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否已预占持仓合约数名额


class Order(Base):
    """订单与条件单（8 态 + 交易所原始状态）。表名不用 order（SQL 保留字）。"""

    __tablename__ = "orders"

    client_order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    intent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("intent.id"))
    leg: Mapped[str] = mapped_column(Text)
    purpose: Mapped[OrderPurpose] = mapped_column(EnumText(OrderPurpose))
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[OrderSide] = mapped_column(EnumText(OrderSide))
    position_side: Mapped[PositionSide] = mapped_column(EnumText(PositionSide))
    kind: Mapped[str] = mapped_column(Text)  # market | algo
    order_type: Mapped[str] = mapped_column(Text)
    qty: Mapped[Decimal | None] = mapped_column(DecimalText)  # 全平型条件单为空
    trigger_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    working_type: Mapped[WorkingType | None] = mapped_column(EnumText(WorkingType))
    close_position: Mapped[bool] = mapped_column(Boolean, default=False)
    exchange_order_id: Mapped[str | None] = mapped_column(Text, index=True)
    algo_id: Mapped[str | None] = mapped_column(Text, index=True)
    actual_order_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[OrderState] = mapped_column(EnumText(OrderState))
    raw_status: Mapped[str | None] = mapped_column(Text)
    filled_qty: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"))
    avg_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    error_code: Mapped[int | None] = mapped_column(Integer)
    error_msg: Mapped[str | None] = mapped_column(Text)
    sent_ms: Mapped[int | None] = mapped_column(Integer)
    acked_ms: Mapped[int | None] = mapped_column(Integer)
    last_query_ms: Mapped[int | None] = mapped_column(Integer)
    absent_count: Mapped[int] = mapped_column(Integer, default=0)
    resend_of: Mapped[str | None] = mapped_column(Text)
    created_ms: Mapped[int] = mapped_column(Integer)
    updated_ms: Mapped[int] = mapped_column(Integer)


class Fill(Base):
    """成交。数据流与补拉两条路径到达的同一笔成交只入账一次（trade_id 唯一）。"""

    __tablename__ = "fill"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[str] = mapped_column(Text, unique=True)
    exchange_order_id: Mapped[str] = mapped_column(Text, index=True)
    client_order_id: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[OrderSide] = mapped_column(EnumText(OrderSide))
    position_side: Mapped[PositionSide] = mapped_column(EnumText(PositionSide))
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    price: Mapped[Decimal] = mapped_column(DecimalText)
    commission: Mapped[Decimal] = mapped_column(DecimalText)
    commission_asset: Mapped[str] = mapped_column(Text)
    commission_usdt: Mapped[Decimal | None] = mapped_column(DecimalText)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalText)
    time_ms: Mapped[int] = mapped_column(Integer)
    source: Mapped[FillSource] = mapped_column(EnumText(FillSource))
    origin: Mapped[OrderOrigin] = mapped_column(EnumText(OrderOrigin))
    position_id: Mapped[int | None] = mapped_column(Integer)
    intent_id: Mapped[int | None] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(Text)  # stream | rest
    created_ms: Mapped[int] = mapped_column(Integer)


class Income(Base):
    """收益流水（资金费等）。"""

    __tablename__ = "income"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    income_id: Mapped[str] = mapped_column(Text, unique=True)
    income_type: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(DecimalText)
    asset: Mapped[str] = mapped_column(Text)
    time_ms: Mapped[int] = mapped_column(Integer)
    position_id: Mapped[int | None] = mapped_column(Integer)
    info: Mapped[str] = mapped_column(Text, default="")


class Trade(Base):
    """逐笔交易：一笔 = 仓位从零变非零到再次归零。"""

    __tablename__ = "trade"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(Integer, unique=True)
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[PositionSide] = mapped_column(EnumText(PositionSide))
    ownership: Mapped[Ownership] = mapped_column(EnumText(Ownership))
    model: Mapped[str | None] = mapped_column(Text)
    is_mixed: Mapped[bool] = mapped_column(Boolean, default=False)
    open_ms: Mapped[int | None] = mapped_column(Integer)
    close_ms: Mapped[int] = mapped_column(Integer)
    open_time_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    max_qty: Mapped[Decimal] = mapped_column(DecimalText)
    open_avg: Mapped[Decimal | None] = mapped_column(DecimalText)
    close_avg: Mapped[Decimal | None] = mapped_column(DecimalText)
    open_notional: Mapped[Decimal | None] = mapped_column(DecimalText)
    gross_pnl: Mapped[Decimal] = mapped_column(DecimalText)
    fees: Mapped[Decimal] = mapped_column(DecimalText)
    funding: Mapped[Decimal] = mapped_column(DecimalText)
    net_pnl: Mapped[Decimal] = mapped_column(DecimalText)
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    exit_reason: Mapped[ExitReason] = mapped_column(EnumText(ExitReason))
    list_snapshot_id: Mapped[int | None] = mapped_column(Integer)
    list_direction: Mapped[str | None] = mapped_column(Text)
    list_score: Mapped[Decimal | None] = mapped_column(DecimalText)


class StopHistory(Base):
    """止损价变化历史，只追加（M5，迁移 0002）：登记簿每次把某个持仓的止损价改成新值就记一行；
    ``price`` 为空表示止损被清掉（那一刻起到下一行之前没有止损线）。"""

    __tablename__ = "stop_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(Integer, index=True)
    price: Mapped[Decimal | None] = mapped_column(DecimalText)
    at_ms: Mapped[int] = mapped_column(Integer)


class ListSnapshot(Base):
    """名单留痕：每次拉取（成功或失败）一行。"""

    __tablename__ = "list_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fetched_ms: Mapped[int] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer)
    business_date: Mapped[str | None] = mapped_column(Text)
    scan_id: Mapped[str | None] = mapped_column(Text)
    valid: Mapped[bool | None] = mapped_column(Boolean)
    valid_until_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    member_count: Mapped[int] = mapped_column(Integer, default=0)


class ListMember(Base):
    __tablename__ = "list_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, ForeignKey("list_snapshot.id"))
    symbol: Mapped[str] = mapped_column(Text)
    direction: Mapped[str | None] = mapped_column(Text)
    score: Mapped[Decimal | None] = mapped_column(DecimalText)
    first_selected_ms: Mapped[int | None] = mapped_column(Integer)
    tradable: Mapped[bool | None] = mapped_column(Boolean)
    note: Mapped[str] = mapped_column(Text, default="")


class ModelState(Base):
    """模型状态（单行，id = 1）：生效模型、开仓闸、抑制原因、操作锁、运行方式与模型状态版本。"""

    __tablename__ = "model_state"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active_model: Mapped[Model] = mapped_column(EnumText(Model))
    gate: Mapped[Gate] = mapped_column(EnumText(Gate), default=Gate.STOPPED)
    suppressions: Mapped[list[Any]] = mapped_column(JSONText, default=_empty_list)
    op_locks: Mapped[list[Any]] = mapped_column(JSONText, default=_empty_list)
    op_lock_owner: Mapped[str | None] = mapped_column(Text)
    op_lock_expires_ms: Mapped[int | None] = mapped_column(Integer)
    run_mode: Mapped[RunMode] = mapped_column(EnumText(RunMode), default=RunMode.NORMAL)
    version: Mapped[int] = mapped_column(Integer, default=1)
    m2_started_ms: Mapped[int | None] = mapped_column(Integer)
    mismatch_since_ms: Mapped[int | None] = mapped_column(Integer)
    risk_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONText)
    updated_ms: Mapped[int] = mapped_column(Integer)


class ModelSwitchLog(Base):
    __tablename__ = "model_switch_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at_ms: Mapped[int] = mapped_column(Integer)
    from_model: Mapped[str | None] = mapped_column(Text)
    to_model: Mapped[str] = mapped_column(Text)
    mode: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    op_id: Mapped[str | None] = mapped_column(Text)
    operator: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")


class Operation(Base):
    """改变状态的操作：同一操作号只执行一次（op_id 唯一）。"""

    __tablename__ = "operation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    op_id: Mapped[str] = mapped_column(Text, unique=True)
    op_type: Mapped[OperationType] = mapped_column(EnumText(OperationType))
    scope: Mapped[str | None] = mapped_column(Text)
    status: Mapped[OperationStatus] = mapped_column(EnumText(OperationStatus))
    operator: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[int | None] = mapped_column(Integer)
    state_version: Mapped[int | None] = mapped_column(Integer)
    params: Mapped[dict[str, Any]] = mapped_column(JSONText, default=_empty_dict)
    per_position: Mapped[Any] = mapped_column(JSONText, nullable=True)
    result: Mapped[Any] = mapped_column(JSONText, nullable=True)
    started_ms: Mapped[int] = mapped_column(Integer)
    ended_ms: Mapped[int | None] = mapped_column(Integer)


class AuditLog(Base):
    """审计日志，只追加：仓储层只提供插入与查询，库里的触发器拒绝 UPDATE 与 DELETE。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at_ms: Mapped[int] = mapped_column(Integer)
    operator: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text, default="")
    before: Mapped[Any] = mapped_column(JSONText, nullable=True)
    after: Mapped[Any] = mapped_column(JSONText, nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)


for _sql in AUDIT_LOG_TRIGGERS:
    event.listen(AuditLog.__table__, "after_create", DDL(_sql))


class ReconcileRun(Base):
    __tablename__ = "reconcile_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at_ms: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text, default="")


class ReconcileDiff(Base):
    __tablename__ = "reconcile_diff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("reconcile_run.id"))
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[PositionSide | None] = mapped_column(EnumText(PositionSide))
    local_qty: Mapped[Decimal | None] = mapped_column(DecimalText)
    exchange_qty: Mapped[Decimal | None] = mapped_column(DecimalText)
    kind: Mapped[str] = mapped_column(Text)
    resolved_ms: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="")


class Alert(Base):
    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at_ms: Mapped[int] = mapped_column(Integer)
    level: Mapped[AlertLevel] = mapped_column(EnumText(AlertLevel))
    kind: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    delivered_ms: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class ConfigItem(Base):
    """运行期配置的当前值。默认值、范围、生效方式来自 core.configdefs；值为 None 存 SQL NULL。"""

    __tablename__ = "config_item"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONText, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_ms: Mapped[int] = mapped_column(Integer)


class Operator(Base):
    __tablename__ = "operator"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)  # 标准库 scrypt
    role: Mapped[str] = mapped_column(Text)
    created_ms: Mapped[int] = mapped_column(Integer)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)


class OperatorSession(Base):
    """登录会话（表名 session）。只存令牌的哈希。"""

    __tablename__ = "session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    operator_id: Mapped[int] = mapped_column(Integer, ForeignKey("operator.id"))
    created_ms: Mapped[int] = mapped_column(Integer)
    last_active_ms: Mapped[int] = mapped_column(Integer)
    reauth_ms: Mapped[int | None] = mapped_column(Integer)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    client_info: Mapped[str] = mapped_column(Text, default="")


class Seat(Base):
    """操作席位租约（单行，id = 1）。"""

    __tablename__ = "seat"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int | None] = mapped_column(Integer)
    lease_until_ms: Mapped[int | None] = mapped_column(Integer)
    acquired_ms: Mapped[int | None] = mapped_column(Integer)


class Cooldown(Base):
    """平仓后冷却。"""

    __tablename__ = "cooldown"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    until_ms: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, default="")
    set_ms: Mapped[int] = mapped_column(Integer)


class Candle(Base):
    """已收盘的 K 线（开高低收量交给 pandas，用 REAL）。"""

    __tablename__ = "candle"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    timeframe: Mapped[str] = mapped_column(Text, primary_key=True)
    open_time_ms: Mapped[int] = mapped_column(Integer, primary_key=True)
    open: Mapped[float] = mapped_column(REAL)
    high: Mapped[float] = mapped_column(REAL)
    low: Mapped[float] = mapped_column(REAL)
    close: Mapped[float] = mapped_column(REAL)
    volume: Mapped[float] = mapped_column(REAL)


MODELS: dict[str, type[Base]] = {mapper.class_.__tablename__: mapper.class_ for mapper in Base.registry.mappers}
