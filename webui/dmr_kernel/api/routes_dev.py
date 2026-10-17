"""开发接口的实现（仅替身环境；其它环境不注册这些路由，答 404）。

- ``POST /dev/test_open {op_id, symbol, side, notional}`` → ``{"op_id","status","result":{"intent_id","state"[,"reason"]}}``：
  测试专用造仓（TEST_ENTRY 开仓意图，绕过开仓闸与名单），实现在 ``ops.test_entry``；需要席位（本机令牌免）。
  操作服务没有装配时答 501（与 ``routes_ops.real`` 一致）。
- ``POST /dev/inject_signal {op_id, symbol, short_cross: "up"|"down", long_trend: "bullish"|"bearish"|"neutral"|"missing"}``
  → ``{"op_id","status","result":{"signal","bar_open_ms","submitted"[,"intent_id","injected_seq","state","reason"]}}``：
  构造一次「当根已收盘 K 线」的信号，走与自然信号完全相同的预检与提交（``models.m1_strategy.StrategyService.inject``，
  来源 ``injected``，幂等键另加注入序号），不改任何 K 线数据；结果写操作记录（类型 ``inject_signal``）并留痕。
  已提交且意图完成 → ``succeeded``；不构成信号、预检不过、意图被拒绝 → ``failed`` 并带原因；超时未到终态 → ``unknown``。
  合约不在合约清单、参数取值不对 422 ``invalid``；需要席位（本机令牌免）；模型 1 没有装配 501。
- ``POST /dev/shadow_intent {op_id, symbol, side[, action="open", reason, ai_eval_ref]}`` → ``{"intent_id","state","reason","idem_key"}``：
  影子记录通道的验收入口（``ops.shadow.record_shadow_intent``）：造一个模型 3 的影子意图，被协调者在准入段之前拦截——意图 REJECTED
  （原因「影子记录：只留痕不下单」）、写审计 ``shadow.intent``，绝不产生订单。幂等键 ``shadow:<op_id>``，同一个 op_id 重复提交返回同一个意图。
  合约不在合约清单、方向或动作不对、op_id 不合规 422 ``invalid``；需要登录，不占席位（不改变任何交易状态）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.types import (
    IntentAction,
    IntentSource,
    IntentState,
    OperationStatus,
    OperationType,
    PositionSide,
)

from dmr_kernel.api.common import Principal
from dmr_kernel.api.deps import ok, require_principal
from dmr_kernel.api.routes_ops import caller_of, ops_services_of, require_wired
from dmr_kernel.ops.operations import check_op_id
from dmr_kernel.api import errors
from dmr_kernel.ops import shadow
from dmr_kernel.ops.common import (
    admit,
    fail_op,
    finish_op,
    intent_wait_s,
    invalid,
    not_allowed,
    response_of,
    run_protected,
)

__all__ = ["router"]

router = APIRouter(dependencies=[Depends(require_principal), Depends(require_wired)])


class TestOpenBody(BaseModel):
    op_id: str
    symbol: str
    side: str
    notional: Any


@router.post("/dev/test_open")
async def test_open(body: TestOpenBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    return ok(await ops_services_of(request).test_entry.open(
        caller, op_id=body.op_id, symbol=body.symbol, side=body.side, notional=body.notional))


class InjectSignalBody(BaseModel):
    op_id: str
    symbol: str
    short_cross: str
    long_trend: str


@router.post("/dev/inject_signal")
async def inject_signal(body: InjectSignalBody, request: Request, principal: Principal = Depends(require_principal)):
    caller = await caller_of(request, principal)
    service = getattr(request.app.state, "m1", None)
    if service is None:
        raise errors.not_implemented("模型 1 没有装配，不能注入信号")
    deps = ops_services_of(request).deps
    return ok(await run_protected(_inject(deps, service, caller, body)))


async def _inject(deps, service, caller, body: InjectSignalBody) -> dict:
    if deps.is_live:
        raise not_allowed("信号注入仅非实盘环境可用")
    symbol = body.symbol.strip().upper()
    if deps.rules.get(symbol) is None:
        raise invalid(f"不认识的合约：{body.symbol!r}")
    cross, trend = body.short_cross.strip().lower(), body.long_trend.strip().lower()
    if cross not in ("up", "down"):
        raise invalid("short_cross 必须是 up 或 down")
    if trend not in ("bullish", "bearish", "neutral", "missing"):
        raise invalid("long_trend 必须是 bullish、bearish、neutral 或 missing")
    admission = await admit(deps, caller, op_id=body.op_id, op_type=OperationType.INJECT_SIGNAL, scope=symbol,
                            params={"symbol": symbol, "short_cross": cross, "long_trend": trend})
    if not admission.is_new:
        return response_of(admission.record)
    try:
        result = await service.inject(symbol=symbol, short_cross=cross, long_trend=trend, wait_s=intent_wait_s(deps))
        if not result["submitted"]:
            status = OperationStatus.FAILED
        elif result.get("state") == IntentState.COMPLETED.value:
            status = OperationStatus.SUCCEEDED
        elif result.get("state") in {s.value for s in IntentState.terminal()}:
            status = OperationStatus.FAILED
        else:
            status = OperationStatus.UNKNOWN
        record = await finish_op(deps, body.op_id, status, result)
    except Exception as exc:
        await fail_op(deps, body.op_id, exc)
        raise
    return response_of(record)


class ShadowIntentBody(BaseModel):
    op_id: str
    symbol: str
    side: str
    action: str = "open"
    reason: str = ""
    ai_eval_ref: str | None = None


@router.post("/dev/shadow_intent")
async def shadow_intent(body: ShadowIntentBody, request: Request, principal: Principal = Depends(require_principal)):
    deps = ops_services_of(request).deps
    try:
        check_op_id(body.op_id)
    except ValueError as exc:
        raise invalid(str(exc)) from None
    symbol = body.symbol.strip().upper()
    if deps.rules.get(symbol) is None:
        raise invalid(f"不认识的合约：{body.symbol!r}")
    try:
        side = PositionSide(body.side.strip().upper())
        action = IntentAction(body.action.strip().lower())
    except ValueError:
        raise invalid("side 必须是 LONG 或 SHORT，action 必须是意图动作之一") from None
    spec = IntentSpec(action=action, model="m3", source=IntentSource.SHADOW, symbol=symbol, side=side,
                      idem_key=shadow.shadow_key(body.op_id), reason=body.reason, operator=principal.operator,
                      op_id=body.op_id, ai_eval_ref=body.ai_eval_ref)
    rec = await run_protected(shadow.record_shadow_intent(deps.coordinator, spec))
    return ok({"intent_id": rec.id, "state": rec.state, "reason": rec.state_reason, "idem_key": rec.idem_key})

