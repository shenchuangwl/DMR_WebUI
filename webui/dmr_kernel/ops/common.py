"""操作入口的统一步骤（第 6 层 ops）：启动、停止、部分平仓、平掉此仓、测试造仓（M2A）与一键全平、切换（M2B）共用。

每个改变状态的入口按计划第 1 节的顺序做（``admit`` 一次做完；顺序不能换）：

0. **操作号去重**：该操作号已登记过，直接返回第一次的记录（先于一切校验：重复提交不会因为状态已变而被拒绝）；
   操作号被另一种操作用过 → 422 ``invalid``；操作号格式不对 → 422 ``invalid``。
1. **允许表**：``core.modelstate.allowed_operations(状态)[key]`` 不允许 → 409 ``not_allowed``，``message`` 是表里的原因原文。
2. **席位**：会话主体必须持有席位（409 ``seat_required``）；本机令牌（cli）不占席位。
3. **重新输入口令**（``reauth=True`` 的操作）：会话最近 ``reauth_window_s`` 秒内校验过，或请求带了 ``password`` 当场校验
   （403 ``reauth_required`` / 401 ``bad_credentials``，后者由调用方给的 ``verify`` 抛出）；本机令牌免。
4. **状态版本**：模型级操作校验 ``model_version``，持仓级操作校验该持仓的 ``version``（409 ``stale_version``）。
5. **预检**（可选 ``precheck(state, position)``）：各操作自己的复核（例如部分平仓的 F4 第 3 步），不过就抛 ``OpRejected``。
6. **登记**：``Operations.begin``（并发的同一操作号只有一个是新建，其余拿到第一次的记录）。
被 1–5 步拒绝的请求不占用操作号、不进操作记录（客户端刷新后可以用同一个操作号重试）。

公共函数签名（M2B 复用）::

    class OpRejected(Exception): status, code, message, extra; body() -> {"code","message",**extra}
    not_allowed / stale_version / seat_required / reauth_required / invalid / not_found / unavailable / not_implemented (message, **extra)
    @dataclass(frozen=True) Caller(operator, session_id=None, is_session=False, holds_seat=False, reauth_ms=None); Caller.cli()
    @dataclass OpsDeps(clock, modelstate, registry, operations, config_get, models, coordinator, adapter, rules, orders,
                       is_live=False, list_valid=None, legacy_check=None, close_all_loader=None, wait_s=None)
    @dataclass OpsWiring(coordinator, adapter, rules, orders, list_valid=None, legacy_check=None); OpsWiring.from_kernel(kernel)
    require_allowed(state, key) -> Permission        require_seat(caller)
    await require_reauth(caller, *, now_ms, window_s, password, verify)
    require_model_version(state, version)            require_position_version(position, version)
    await admit(deps, caller, *, op_id, op_type, allowed_key=None, model_version=None, position_id=None,
                position_version=None, reauth=False, password=None, verify=None, scope=None, params=None,
                precheck=None) -> Admission(record, is_new, state, position)
    response_of(record) -> {"op_id","status","result"}      jsonable_result(value)      await finish_op(deps, op_id, status, result, per_position=None)
    close_outcome(intent_state, filled_qty) -> (outcome, OperationStatus)      await filled_close_qty(deps, intent_id)
    intent_wait_s(deps) -> 等意图终态的真实秒数      await run_protected(coro)  # 调用方被取消时操作照样做完
    await fail_op(deps, op_id, exc)  # 内部错误时尽力把进行中的操作记为失败（不盖掉原错误）
    await run_intent(deps, spec) -> IntentRecord  # 提交意图并等到终态（或 intent_wait_s 超时返回当前记录）
    intent_result(rec, filled_qty, outcome) -> 逐意图的结果字典 {"intent_id","state","filled_qty","outcome"[,"reason"]}
    load_close_all(deps) -> 模块或 None（M2B 的 ``dmr_kernel.ops.close_all``；``/ops/stop`` 带 close_all 时调
                                         ``start_close_all(deps, caller, *, op_id, model_version, scope, parent_op_id)``）

``verify`` 是 ``async def (password) -> None``，口令错误时抛错（api 层给的是 ``AuthService.reauth`` 的包装，错误原样向上传）。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.modelstate import ModelStateService, ModelStateSnapshot, Permission, allowed_operations
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.types import IntentState, Model, OperationStatus, OperationType, OrderPurpose, RunMode
from dmr_kernel.exchange.adapter import ExchangeAdapter
from dmr_kernel.exchange.rules import InstrumentRules
from dmr_kernel.ledger.records import OperationRecord, PositionRecord
from dmr_kernel.models.base import TradingModel
from dmr_kernel.ops.operations import Operations, check_op_id

log = logging.getLogger(__name__)

__all__ = [
    "Admission", "Caller", "OpRejected", "OpsDeps", "OpsWiring", "Verifier", "admit", "close_outcome", "fail_op",
    "filled_close_qty", "finish_op", "intent_result", "run_intent", "intent_wait_s", "invalid", "jsonable_result", "load_close_all", "not_allowed", "not_found",
    "not_implemented", "reauth_required", "require_allowed", "require_model_version", "require_position_version",
    "require_reauth", "require_seat", "response_of", "run_protected", "seat_required", "stale_version", "unavailable",
]

CLOSE_ALL_MODULE = "dmr_kernel.ops.close_all"
Verifier = Callable[[str], Awaitable[Any]]


# ---------------------------------------------------------------- 错误


class OpRejected(Exception):
    """操作被拒绝：HTTP 状态、英文代码、中文原因，附加字段并入响应体顶层（控制接口把它转成 4xx/5xx JSON）。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def not_allowed(message: str, **extra: Any) -> OpRejected:
    return OpRejected(409, "not_allowed", message, **extra)


def stale_version(message: str = "状态版本已过期，请刷新后重试", **extra: Any) -> OpRejected:
    return OpRejected(409, "stale_version", message, **extra)


def seat_required(message: str = "需要持有操作席位：请先申请席位", **extra: Any) -> OpRejected:
    return OpRejected(409, "seat_required", message, **extra)


def reauth_required(message: str = "需要重新输入口令", **extra: Any) -> OpRejected:
    return OpRejected(403, "reauth_required", message, **extra)


def invalid(message: str, **extra: Any) -> OpRejected:
    return OpRejected(422, "invalid", message, **extra)


def not_found(message: str = "不存在", **extra: Any) -> OpRejected:
    return OpRejected(404, "not_found", message, **extra)


def unavailable(message: str, **extra: Any) -> OpRejected:
    return OpRejected(503, "unavailable", message, **extra)


def not_implemented(message: str = "尚未实现", **extra: Any) -> OpRejected:
    return OpRejected(501, "not_implemented", message, **extra)


# ---------------------------------------------------------------- 调用方与依赖


@dataclass(frozen=True)
class Caller:
    """发起操作的主体（api 层从登录主体转来；ops 不依赖 api）。``holds_seat`` 由 api 层在受理时查好。"""

    operator: str
    session_id: int | None = None
    is_session: bool = False
    holds_seat: bool = False
    reauth_ms: int | None = None  # 会话上最近一次口令校验的时间

    @classmethod
    def cli(cls) -> "Caller":
        return cls(operator="cli")


@dataclass
class OpsWiring:
    """api 层自己拿不到、要装配层交进来的东西（``ApiContext.ops_wiring``）。``list_valid``：名单是否有效（M4 接入前没有）；
    ``legacy_check``：实盘时旧入口互斥的问题清单（空表示通过）。"""

    coordinator: Coordinator
    adapter: ExchangeAdapter
    rules: InstrumentRules
    orders: OrderService
    list_valid: Callable[[], bool | None] | None = None
    legacy_check: Callable[[], list[str]] | None = None
    recover: Callable[[], Awaitable[Any]] | None = None  # 重新跑一次重启恢复，返回 RecoveryResult（``POST /ops/recover``）
    last_recovery: Callable[[], Any] | None = None  # 上一次恢复的结果（RecoveryResult 或 None）

    @classmethod
    def from_kernel(cls, kernel: Any) -> "OpsWiring":
        return cls(coordinator=kernel.coordinator, adapter=kernel.adapter, rules=kernel.rules, orders=kernel.orders,
                   recover=getattr(kernel, "rerun_recovery", None),
                   last_recovery=(lambda: getattr(kernel, "recovery_result", None)))


@dataclass
class OpsDeps:
    """各操作入口共用的依赖。适配层只用读方法（写只经协调者）。``wait_s`` 覆盖等意图终态的真实秒数（测试用）；
    ``close_all_loader`` 覆盖 close_all 模块的加载（测试用）。"""

    clock: Clock
    modelstate: ModelStateService
    registry: Registry
    operations: Operations
    config_get: Callable[[str], Any]
    models: Mapping[Model, TradingModel]
    coordinator: Coordinator
    adapter: ExchangeAdapter
    rules: InstrumentRules
    orders: OrderService
    is_live: bool = False
    list_valid: Callable[[], bool | None] | None = None
    legacy_check: Callable[[], list[str]] | None = None
    close_all_loader: Callable[[], Any] | None = None
    wait_s: float | None = None
    recover: Callable[[], Awaitable[Any]] | None = None
    last_recovery: Callable[[], Any] | None = None


# ---------------------------------------------------------------- 单项校验


def require_allowed(state: ModelStateSnapshot, key: str) -> Permission:
    """第 1 步：允许表不允许 → 409 ``not_allowed``（原因原文）。``key`` 不在表里是程序错误（KeyError）。"""
    permission = allowed_operations(state)[key]
    if not permission.allowed:
        raise not_allowed(permission.reason)
    return permission


def require_seat(caller: Caller) -> None:
    """第 2 步：会话必须持有席位；本机令牌不占席位。"""
    if caller.is_session and not caller.holds_seat:
        raise seat_required()


async def require_reauth(caller: Caller, *, now_ms: int, window_s: int, password: str | None,
                         verify: Verifier | None) -> None:
    """第 3 步：窗口内校验过口令，或请求里带的口令当场校验通过。恰好到窗口边缘算过期。本机令牌免。"""
    if not caller.is_session:
        return
    if caller.reauth_ms is not None and now_ms < caller.reauth_ms + window_s * 1000:
        return
    if password:
        if verify is None:
            raise ValueError("带了口令却没有校验函数：调用方必须给 verify")
        await verify(password)
        return
    raise reauth_required()


def require_model_version(state: ModelStateSnapshot, version: int) -> None:
    if state.version != version:
        raise stale_version(f"模型状态版本已过期（请求基于 {version}，当前 {state.version}），请刷新后重试")


def require_position_version(position: PositionRecord, version: int) -> None:
    if position.version != version:
        raise stale_version(f"持仓状态版本已过期（请求基于 {version}，当前 {position.version}），请重新预览或刷新后重试")


# ---------------------------------------------------------------- 统一入口


@dataclass(frozen=True)
class Admission:
    record: OperationRecord
    is_new: bool  # False：同一操作号第一次的记录，不要再执行
    state: ModelStateSnapshot
    position: PositionRecord | None = None


PreCheck = Callable[[ModelStateSnapshot, PositionRecord | None], Awaitable[None]]


async def admit(deps: OpsDeps, caller: Caller, *, op_id: str, op_type: OperationType | str, allowed_key: str | None = None,
                model_version: int | None = None, position_id: int | None = None, position_version: int | None = None,
                reauth: bool = False, password: str | None = None, verify: Verifier | None = None,
                scope: str | None = None, params: Mapping[str, Any] | None = None,
                precheck: PreCheck | None = None) -> Admission:
    """统一步骤 0–6（见模块说明）。"""
    op_type = OperationType(op_type)
    try:
        op_id = check_op_id(op_id)
    except ValueError as exc:
        raise invalid(str(exc)) from None
    existing = await deps.operations.get(op_id)
    if existing is not None:
        return _replay(existing, op_type, deps)
    state = deps.modelstate.current()
    if allowed_key is not None:
        require_allowed(state, allowed_key)
    require_seat(caller)
    if reauth:
        await require_reauth(caller, now_ms=deps.clock.now_ms(), window_s=int(deps.config_get("reauth_window_s")),
                             password=password, verify=verify)
    if model_version is not None:
        require_model_version(state, model_version)
    position: PositionRecord | None = None
    if position_id is not None:
        position = deps.registry.by_id(position_id)
        if position is None:
            raise not_found(f"没有这个持仓（编号 {position_id}，或已平仓）")
        if position_version is not None:
            require_position_version(position, position_version)
    if precheck is not None:
        await precheck(state, position)
    state_version = model_version if model_version is not None else position_version
    record, is_new = await deps.operations.begin(
        op_id, op_type, operator=caller.operator, session_id=caller.session_id, scope=scope,
        state_version=state_version, params=params)
    if not is_new:
        return _replay(record, op_type, deps)
    return Admission(record, True, state, position)


def _replay(record: OperationRecord, op_type: OperationType, deps: OpsDeps) -> Admission:
    if record.op_type is not op_type:
        raise invalid(f"操作号 {record.op_id} 已用于另一种操作（{record.op_type.value}），请换一个新的操作号")
    return Admission(record, False, deps.modelstate.current())


# ---------------------------------------------------------------- 结果


def jsonable_result(value: Any) -> Any:
    """结果里的金额一律字符串（``format(x, 'f')``），枚举取值，其余原样；递归处理字典与列表。"""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): jsonable_result(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable_result(item) for item in value]
    return value


def response_of(record: OperationRecord) -> dict[str, Any]:
    """成功响应的外壳：``{"op_id","status","result"}``（还没有结果时 ``result`` 为空字典）。"""
    result = record.result if isinstance(record.result, Mapping) else {}
    return {"op_id": record.op_id, "status": record.status.value, "result": jsonable_result(result)}


async def finish_op(deps: OpsDeps, op_id: str, status: OperationStatus, result: Mapping[str, Any],
                    per_position: Any = None) -> OperationRecord:
    return await deps.operations.finish(op_id, status, result=jsonable_result(result),
                                        per_position=jsonable_result(per_position))


_FAILED_STATES = frozenset({IntentState.REJECTED, IntentState.EXPIRED, IntentState.CANCELLED})


def close_outcome(state: IntentState, filled_qty: Decimal) -> tuple[str, OperationStatus]:
    """平仓类意图的结局 → (``closed`` | ``closed_externally`` | ``failed`` | ``unknown``, 操作状态)。
    完成但一笔也没成交 = 已由其它途径平掉；还没到终态（超时、回查中）= 未知，不当成失败也不当成成功。"""
    if state is IntentState.COMPLETED:
        if filled_qty > 0:
            return "closed", OperationStatus.SUCCEEDED
        return "closed_externally", OperationStatus.SUCCEEDED
    if state in _FAILED_STATES:
        return "failed", OperationStatus.FAILED
    return "unknown", OperationStatus.UNKNOWN


async def filled_close_qty(deps: OpsDeps, intent_id: int) -> Decimal:
    """该意图名下平仓类订单（整仓平仓、部分平仓）的累计成交量。"""
    purposes = (OrderPurpose.CLOSE, OrderPurpose.PARTIAL_CLOSE)
    return sum((o.filled_qty for o in await deps.orders.for_intent(intent_id) if o.purpose in purposes), Decimal("0"))


def intent_wait_s(deps: OpsDeps) -> float:
    """等意图到终态的真实秒数：单仓平仓时限，仅轮询降级时放宽一倍。"""
    if deps.wait_s is not None:
        return float(deps.wait_s)
    seconds = float(deps.config_get("close_one_timeout_s"))
    return seconds * 2 if deps.modelstate.current().run_mode is RunMode.POLLING_DEGRADED else seconds


async def fail_op(deps: OpsDeps, op_id: str, exc: BaseException) -> None:
    """内部错误：尽力把进行中的操作记为失败；记不上也不能盖掉原来的错误。"""
    log.error("操作 %s 出现内部错误：%s", op_id, exc, exc_info=exc)
    try:
        await finish_op(deps, op_id, OperationStatus.FAILED, {"message": f"内部错误（{type(exc).__name__}），详情见内核日志"})
    except Exception:
        log.exception("操作 %s 无法记为失败", op_id)


async def run_intent(deps: OpsDeps, spec: Any) -> Any:
    """提交意图并等到终态；超过 ``intent_wait_s`` 返回当时的记录（状态未到终态 = 结果未知）。"""
    submitted = await deps.coordinator.submit(spec)
    return await deps.coordinator.wait_terminal(submitted.intent_id, intent_wait_s(deps))


def intent_result(rec: Any, filled_qty: Decimal, outcome: str) -> dict[str, Any]:
    result: dict[str, Any] = {"intent_id": rec.id, "state": rec.state.value, "filled_qty": filled_qty, "outcome": outcome}
    if outcome in ("failed", "unknown") and rec.state_reason:
        result["reason"] = rec.state_reason
    elif outcome == "failed":
        result["reason"] = "意图被拒绝"
    return result


# ---------------------------------------------------------------- 其它


def load_close_all(deps: OpsDeps) -> Any | None:
    """M2B 的一键全平模块（延迟导入）。不存在、或没有 ``start_close_all`` 返回 None。"""
    if deps.close_all_loader is not None:
        module = deps.close_all_loader()
    else:
        try:
            module = importlib.import_module(CLOSE_ALL_MODULE)
        except ModuleNotFoundError as exc:
            if exc.name != CLOSE_ALL_MODULE:
                raise
            return None
    return module if module is not None and callable(getattr(module, "start_close_all", None)) else None


def _late_failure(task: asyncio.Future) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("调用方已取消，操作在后台出错", exc_info=task.exception())


async def run_protected(coro: Awaitable[Any]) -> Any:
    """把一整个操作放进受保护的任务里做完：调用方被取消（例如连接断了）也不会留下做了一半的操作。"""
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_late_failure)
        raise
