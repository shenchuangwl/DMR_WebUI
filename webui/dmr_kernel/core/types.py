"""内核共享的枚举与数据类型（第 0a 层：纯定义，无 I/O，不导入任何内核模块）。

约定（docs/设计约定.md 第 6 节）：
- 价格、数量、金额一律 Decimal；只有交给 pandas 计算的 K 线字段用 float。
- 时间一律 UTC 毫秒时间戳（int），字段名以 ``_ms`` 结尾。
- 合约一律用交易所原始代码（如 ``BTCUSDT``），ccxt 的统一写法只在适配层内部出现。
- 读方法的结果是 ``Known`` 或 ``Unknown``，「未知」永远不能当成「空」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Generic, TypeVar, Union

# ------------------------------------------------------------------ 方向与订单


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    def opposite(self) -> "PositionSide":
        return PositionSide.SHORT if self is PositionSide.LONG else PositionSide.LONG

    def open_side(self) -> OrderSide:
        return OrderSide.BUY if self is PositionSide.LONG else OrderSide.SELL

    def close_side(self) -> OrderSide:
        return OrderSide.SELL if self is PositionSide.LONG else OrderSide.BUY


class WorkingType(StrEnum):
    CONTRACT_PRICE = "CONTRACT_PRICE"  # 最新价
    MARK_PRICE = "MARK_PRICE"  # 标记价


class AlgoType(StrEnum):
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    STOP = "STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP_MARKET = "TRAILING_STOP_MARKET"

    @property
    def is_stop_kind(self) -> bool:
        """止损类（F3 第 6 步「类型是止损类」）。"""
        return self in (AlgoType.STOP_MARKET, AlgoType.STOP, AlgoType.TRAILING_STOP_MARKET)


class Tri(StrEnum):
    """三态：存在、不存在、未知（F8）。"""

    EXISTS = "exists"
    ABSENT = "absent"
    UNKNOWN = "unknown"


# ------------------------------------------------------------------ 环境、模型与状态


class Environment(StrEnum):
    STANDIN = "standin"  # 替身环境（本轮默认）
    TESTNET = "testnet"  # Binance Demo Trading
    MAINNET_SHADOW = "mainnet_shadow"  # 主网只读影子
    MAINNET_LIVE = "mainnet_live"  # 主网实盘


class Model(StrEnum):
    M1 = "m1"  # 完全自动化量化交易
    M2 = "m2"  # 接管手动订单
    M3 = "m3"  # AI（本期只预留）


class Gate(StrEnum):
    """开仓闸。"""

    STOPPED = "stopped"
    RUNNING = "running"


class Suppression(StrEnum):
    """抑制原因（4.6）：闸开着但暂时不许新开仓。"""

    STREAM_SWITCHING = "stream_switching"  # 数据流切换中
    RECONCILE_UNKNOWN = "reconcile_unknown"  # 对账未知
    RECONCILE_MISMATCH = "reconcile_mismatch"  # 对账不一致
    EXCHANGE_RESTRICTED = "exchange_restricted"  # 交易所限制
    RISK_TRIPPED = "risk_tripped"  # 风控触发（账户级）
    UNPROTECTED = "unprotected"  # 存在无保护持仓
    RECOVERING = "recovering"  # 恢复中
    MUTEX_ANOMALY = "mutex_anomaly"  # 互斥异常
    CLOCK_SKEW = "clock_skew"  # 时钟偏差


class OpLock(StrEnum):
    """操作锁（4.6）：有任何操作锁时不新开仓，也不自动接管。"""

    SWITCH_PREVIEW = "switch_preview"  # 切换预览中
    SWITCHING = "switching"  # 切换中
    BULK_CLOSING = "bulk_closing"  # 批量平仓中


class RunMode(StrEnum):
    NORMAL = "normal"
    POLLING_DEGRADED = "polling_degraded"  # 仅轮询降级（不是抑制原因）


class IntentState(StrEnum):
    """交易意图 10 态（沿用项目设计文档的命名）。"""

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    RESERVED = "RESERVED"
    DISPATCH_READY = "DISPATCH_READY"
    ACTIVE = "ACTIVE"
    RECONCILING = "RECONCILING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

    @classmethod
    def terminal(cls) -> set["IntentState"]:
        return {cls.COMPLETED, cls.REJECTED, cls.EXPIRED, cls.CANCELLED}


class IntentAction(StrEnum):
    OPEN = "open"  # 开仓（模型 1、反手的开仓步、测试专用入口）
    CLOSE = "close"  # 平掉整个仓（信号平仓、平掉此仓、一键全平逐仓、保护性平仓）
    PARTIAL_CLOSE = "partial_close"
    STOP_PLACE = "stop_place"  # 挂止损（初始、补挂、重建、按现价重定）
    STOP_REPLACE = "stop_replace"  # 移动止损（更新）
    STOP_CANCEL = "stop_cancel"  # 撤止损（清理、待清理旧单、孤儿、取消托管时撤掉）
    ORDER_CANCEL = "order_cancel"  # 撤未成交的开仓单
    SET_LEVERAGE = "set_leverage"
    SET_MARGIN_MODE = "set_margin_mode"

    @property
    def adds_risk(self) -> bool:
        """只有开仓类意图新增风险，受开仓闸、抑制原因、风控闸约束。"""
        return self is IntentAction.OPEN


class IntentSource(StrEnum):
    SIGNAL = "signal"
    INJECTED = "injected"  # 替身环境的「信号注入」
    OPERATOR = "operator"
    PROTECTION = "protection"
    TAKEOVER = "takeover"
    RECONCILE = "reconcile"
    RECOVERY = "recovery"
    TEST_ENTRY = "test_entry"  # 非实盘环境的测试专用造仓入口
    CLI = "cli"
    SHADOW = "shadow"  # 影子记录通道（模型 3 预留）
    SYSTEM = "system"


class OrderState(StrEnum):
    """订单 8 态。另存交易所原始状态。"""

    PENDING_SEND = "PENDING_SEND"
    SENT_UNKNOWN = "SENT_UNKNOWN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @classmethod
    def terminal(cls) -> set["OrderState"]:
        return {cls.FILLED, cls.CANCELLED, cls.REJECTED}


class OrderPurpose(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    PARTIAL_CLOSE = "partial_close"
    STOP = "stop"  # 系统的全平型保护止损
    DUST_CLOSE = "dust_close"  # 碎仓改用全平型条件单平掉（F12）


class OrderOrigin(StrEnum):
    """按自定义编号识别的订单来源（4.6）。"""

    SYSTEM = "system"
    EMERGENCY = "emergency"
    LIQUIDATION = "liquidation"  # autoclose-
    ADL = "adl"  # adl_autoclose
    LEGACY = "legacy"  # 旧程序格式
    CCXT_DEFAULT = "ccxt_default"  # x-cvBPrNm9：系统漏传编号时 ccxt 自动生成，必须当作异常
    EXTERNAL = "external"
    UNKNOWN = "unknown"  # 没有编号（REST 成交记录不带编号时先记未知，再按订单号回查）


class FillSource(StrEnum):
    """成交来源（4.5 fill 表）。"""

    SYSTEM = "system"
    EXTERNAL = "external"
    LIQUIDATION = "liquidation"
    ADL = "adl"
    LEGACY = "legacy"
    EMERGENCY = "emergency"


class PositionStatus(StrEnum):
    PENDING = "pending"  # 待确认
    TAKING_OVER = "taking_over"  # 接管中
    MANAGED = "managed"  # 托管中
    CLOSING = "closing"  # 平仓中
    CLOSED = "closed"  # 已平
    IGNORED = "ignored"  # 外部（已忽略）

    @classmethod
    def registered(cls) -> set["PositionStatus"]:
        """登记仓：计入「无保护持仓」「对账不一致」（4.6）。"""
        return {cls.TAKING_OVER, cls.MANAGED, cls.CLOSING}


class Ownership(StrEnum):
    STRATEGY = "strategy"  # 策略开仓
    TAKEOVER = "takeover"  # 手动接管
    AI = "ai"
    NONE = "none"  # 待确认、外部（已忽略）


class ExitRuleset(StrEnum):
    STRATEGY = "strategy"  # 初始止损、移动止损、反向信号平仓、可反手
    STRATEGY_NO_REVERSE = "strategy_no_reverse"  # 混合仓：同上但不反手、不加仓
    TAKEOVER = "takeover"  # 初始止损、移动止损；反向信号平仓可选（默认关）；不反手不加仓


class ProtectionState(StrEnum):
    SYSTEM = "system"  # 系统保护
    EXTERNAL = "external"  # 外部保护
    REBUILDING = "rebuilding"  # 重建中
    NONE = "none"  # 无保护


class PendingReason(StrEnum):
    OUT_OF_SCOPE = "out_of_scope"  # 不在接管清单
    NOTIONAL_OUT_OF_RANGE = "notional_out_of_range"  # 名义金额不在上下限之间（数量变化后重判）
    PREEXISTING = "preexisting"  # 启动模型 2 时已有
    LEGACY = "legacy"  # 旧程序遗留
    UNKNOWN_ORIGIN = "unknown_origin"  # 来源不明
    ORIGIN_TO_VERIFY = "origin_to_verify"  # 来源待核对（F9 第 4 步：带系统前缀的止损）
    MODEL_NOT_ACTIVE = "model_not_active"  # 模型 2 未生效或未运行，或有操作锁

    @property
    def recheckable(self) -> bool:
        """只有「金额不在范围内」与「模型 2 未在运行」会在之后重新判定；其余只能逐个确认。"""
        return self in (PendingReason.NOTIONAL_OUT_OF_RANGE, PendingReason.MODEL_NOT_ACTIVE)


class ExitReason(StrEnum):
    STOP = "stop"  # 止损（含移动止损）
    SIGNAL = "signal"  # 反向信号平仓
    REVERSE = "reverse"  # 反手的平仓步
    MANUAL = "manual"  # 平掉此仓
    CLOSE_ALL = "close_all"  # 一键全平
    PARTIAL = "partial"  # 部分平仓（只用于成交，不用于整笔交易的出场）
    PROTECTIVE = "protective"  # 保护性平仓（止损挂不上等）
    EXTERNAL = "external"  # 外部平仓
    LIQUIDATION = "liquidation"
    ADL = "adl"
    SETTLEMENT = "settlement"  # 交易所结算
    EMERGENCY = "emergency"  # 命令行应急入口
    UNKNOWN = "unknown"


class OperationType(StrEnum):
    START = "start"
    STOP = "stop"
    STOP_AND_CLOSE_ALL = "stop_and_close_all"
    SWITCH = "switch"
    PARTIAL_CLOSE = "partial_close"
    CLOSE_ONE = "close_one"
    CLOSE_ALL = "close_all"
    ABORT = "abort"
    TAKEOVER = "takeover"
    IGNORE = "ignore"
    RESTORE = "restore"
    CANCEL_TAKEOVER = "cancel_takeover"
    HAND_STOP_TO_SYSTEM = "hand_stop_to_system"
    CONFIG = "config"
    SEAT_FORCE = "seat_force"
    RESET_RISK = "reset_risk"
    CONFIRM_MISMATCH = "confirm_mismatch"
    CONFIRM_MUTEX = "confirm_mutex"
    INJECT_SIGNAL = "inject_signal"
    TEST_OPEN = "test_open"
    INVENTORY = "inventory"
    RECOVER = "recover"  # 重新恢复（POST /ops/recover）；账本的 op_type 列是不带约束的文本，不需要迁移


class OperationStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    REJECTED = "rejected"
    ABORTED = "aborted"
    UNKNOWN = "unknown"


class PerPositionResult(StrEnum):
    """一键全平等批量操作的逐仓结果（F5 第 10 步）。"""

    CLOSED = "closed"  # 已平
    CLOSED_EXTERNALLY = "closed_externally"  # 已由外部平掉
    FAILED = "failed"
    UNKNOWN = "unknown"
    RESIDUAL = "residual"  # 残留


class AlertLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"  # 最高级


# ------------------------------------------------------------------ 交易所数据


@dataclass(frozen=True)
class Instrument:
    symbol: str
    base: str
    quote: str
    contract_type: str
    status: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    market_step_size: Decimal | None = None
    market_max_qty: Decimal | None = None
    onboard_ms: int | None = None

    @property
    def tradable(self) -> bool:
        return self.status == "TRADING" and self.contract_type == "PERPETUAL" and self.quote == "USDT"


@dataclass(frozen=True)
class Ticker:
    symbol: str
    last: Decimal
    mark: Decimal
    time_ms: int


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int


@dataclass(frozen=True)
class AccountInfo:
    equity: Decimal  # 保证金余额合计（含浮动盈亏）
    wallet_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal
    update_time_ms: int


@dataclass(frozen=True)
class PositionInfo:
    symbol: str
    side: PositionSide
    qty: Decimal  # 绝对值；0 表示该侧无仓
    entry_price: Decimal
    mark_price: Decimal | None = None
    liquidation_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    leverage: int | None = None
    margin_mode: str | None = None  # isolated | cross
    update_time_ms: int = 0


@dataclass(frozen=True)
class OrderInfo:
    symbol: str
    client_order_id: str | None
    exchange_order_id: str
    side: OrderSide
    position_side: PositionSide
    order_type: str
    qty: Decimal
    filled_qty: Decimal
    avg_price: Decimal | None
    status: str  # 归一：NEW | PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED | REJECTED
    raw_status: str  # 交易所原始状态（含 EXPIRED_IN_MATCH 等）
    close_position: bool = False
    reduce_only: bool = False
    update_time_ms: int = 0


@dataclass(frozen=True)
class AlgoOrderInfo:
    """条件单（Algo 接口）。判断是否存在时读 raw_status，不用归一后的状态（F2 第 8 步）。"""

    symbol: str
    client_algo_id: str | None
    algo_id: str
    side: OrderSide
    position_side: PositionSide
    algo_type: AlgoType
    trigger_price: Decimal
    working_type: WorkingType
    close_position: bool
    qty: Decimal | None
    raw_status: str  # NEW | TRIGGERING | TRIGGERED | FINISHED | CANCELED | EXPIRED | REJECTED
    actual_order_id: str | None  # 触发后生成的订单号；交易所不提供时为 None（实测第 12 项）
    create_time_ms: int
    update_time_ms: int

    OPEN_RAW_STATUSES = frozenset({"NEW", "TRIGGERING"})

    @property
    def is_open(self) -> bool:
        return self.raw_status in self.OPEN_RAW_STATUSES


@dataclass(frozen=True)
class TradeInfo:
    """一笔成交。REST 成交记录不带自定义编号，用交易所订单号关联。"""

    trade_id: str
    exchange_order_id: str
    symbol: str
    side: OrderSide
    position_side: PositionSide
    qty: Decimal
    price: Decimal
    commission: Decimal
    commission_asset: str
    realized_pnl: Decimal
    time_ms: int
    maker: bool = False


@dataclass(frozen=True)
class IncomeInfo:
    income_id: str
    income_type: str  # FUNDING_FEE | REALIZED_PNL | COMMISSION | TRANSFER | ...
    symbol: str | None
    amount: Decimal
    asset: str
    time_ms: int
    info: str = ""


@dataclass(frozen=True)
class LeverageInfo:
    symbol: str
    leverage: int
    margin_mode: str  # isolated | cross


@dataclass(frozen=True)
class RestrictionInfo:
    """交易所对账户的限制（量化规则等）。"""

    restricted: bool
    detail: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RiskDecision:
    """风控闸与合约数上限的判定结果。level：""（放行或一般原因）| "account" | "model" | "stale"。"""

    allowed: bool
    reason: str = ""
    level: str = ""


# ------------------------------------------------------------------ 读写结果

T = TypeVar("T")


@dataclass(frozen=True)
class Known(Generic[T]):
    value: T


@dataclass(frozen=True)
class Unknown:
    """查询失败或超时。没有 value 属性：调用方必须显式处理「未知」。"""

    reason: str
    code: int | None = None
    http_status: int | None = None


ReadResult = Union[Known[T], Unknown]


def is_known(result: object) -> bool:
    return isinstance(result, Known)


class RejectKind(StrEnum):
    IMMEDIATE_TRIGGER = "immediate_trigger"  # -2021 会立即触发
    DUAL_CLOSE_POSITION = "dual_close_position"  # -4130 同向已有全平型止盈止损
    REDUCE_REJECTED = "reduce_rejected"  # -2022 无仓可平或超出持仓
    MIN_NOTIONAL = "min_notional"  # -4164
    INSUFFICIENT_MARGIN = "insufficient_margin"  # -2019
    DUPLICATE_CLIENT_ID = "duplicate_client_id"  # -4116
    ORDER_NOT_FOUND = "order_not_found"  # -2011 / -2013
    STOP_LIMIT_REACHED = "stop_limit_reached"  # -4045 条件单数量到上限
    RATE_LIMITED = "rate_limited"  # HTTP 429 / 418 / -1003
    HAS_POSITION_OR_ORDERS = "has_position_or_orders"  # -4047/-4048 有仓或挂单时不能改杠杆或保证金模式
    INVALID_PARAMS = "invalid_params"
    WRITES_DISABLED = "writes_disabled"  # 只读影子：写方法在适配层出口禁用
    OTHER = "other"


@dataclass(frozen=True)
class WriteAccepted:
    order: OrderInfo | None = None
    algo: AlgoOrderInfo | None = None
    leverage: LeverageInfo | None = None


@dataclass(frozen=True)
class WriteRejected:
    kind: RejectKind
    code: int | None
    message: str
    http_status: int | None = None


@dataclass(frozen=True)
class WriteUnknown:
    """超时、连接中断、HTTP 503 Unknown error 等：执行状态未知，必须先回查。"""

    reason: str
    code: int | None = None
    http_status: int | None = None


WriteResult = Union[WriteAccepted, WriteRejected, WriteUnknown]


class CancelOutcome(StrEnum):
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CancelResult:
    outcome: CancelOutcome
    order: OrderInfo | None = None
    algo: AlgoOrderInfo | None = None
    code: int | None = None
    message: str = ""


# ------------------------------------------------------------------ 账户数据流（适配层归一后的事件载荷）


@dataclass(frozen=True)
class StreamOrder:
    order: OrderInfo
    execution: str  # NEW | TRADE | CANCELED | EXPIRED | REJECTED | AMENDMENT
    fill: TradeInfo | None
    event_time_ms: int


@dataclass(frozen=True)
class StreamAlgo:
    algo: AlgoOrderInfo
    event_time_ms: int


@dataclass(frozen=True)
class StreamAccount:
    positions: tuple[PositionInfo, ...]
    reason: str
    event_time_ms: int
    balances: tuple[tuple[str, Decimal], ...] = field(default=())
