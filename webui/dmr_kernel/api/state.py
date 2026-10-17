"""``GET /state`` 的组装（第 7 层 api）：把各服务的现值拼成报文约定第 3.1 节的结构。

数据源暂时没有（名单同步没有接入、对账还没跑过、没收到过账户快照……）时，对应对象给 ``known: false`` 与原因，不省略字段、
不编造值。``list`` 取名单同步的当前视图（``ListSync.current()``）：装配层把取视图的函数挂在 ``ctx.list_view``（返回视图或 None），
每次请求现取。``model3`` 是模型 3 的形态、AI 档、执行档、总闸与部署级开关（启动一律被拒，``start_refusal`` 写明原因）。``allowed`` 原样来自 ``core.modelstate.allowed_operations``，``button_meanings`` 来自当前生效模型。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from dmr_kernel.alerts.heartbeat import Heartbeat
from dmr_kernel.api.common import Principal
from dmr_kernel.api.context import ApiContext, Services
from dmr_kernel.api.views import RUN_MODE_LABELS, jsonable, plain
from dmr_kernel.core.modelstate import (
    OP_LOCK_LABELS,
    SUPPRESSION_LABELS,
    ModelStateSnapshot,
    allowed_operations,
)
from dmr_kernel.core.configdefs import DEFINITIONS
from dmr_kernel.core.types import Environment, Model, RunMode

__all__ = ["build_state"]

COUNT_KEYS = ("managed", "unprotected", "taking_over", "closing", "pending", "external")
LIST_NOT_CONNECTED = "名单同步没有接入"
LIST_NOT_FETCHED = "还没有拉取过名单"


def _read_heartbeat(path: Path) -> int | None:
    data = Heartbeat.read(path)
    return None if data is None else data["at_ms"]


def _model(snap: ModelStateSnapshot) -> dict[str, Any]:
    return {
        "active": snap.active_model.value,
        "gate": snap.gate.value,
        "version": snap.version,
        "suppressions": [{"code": s.value, "label": label} for s, label in SUPPRESSION_LABELS.items()
                         if s in snap.suppressions],
        "op_locks": [{"code": lock.value, "label": label, "owner": snap.op_lock_owner,
                      "expires_ms": snap.op_lock_expires_ms}
                     for lock, label in OP_LOCK_LABELS.items() if lock in snap.op_locks],
        "run_mode": {"code": snap.run_mode.value, "label": RUN_MODE_LABELS[snap.run_mode]},
    }


def _allowed(snap: ModelStateSnapshot) -> dict[str, Any]:
    return {key: {"allowed": p.allowed, "reason": p.reason, "needs_live_read": p.needs_live_read, "note": p.note}
            for key, p in allowed_operations(snap).items()}


def _account(live: Any) -> dict[str, Any]:
    snap = live.account
    if snap is None:
        return {"known": False, "equity": None, "available": None, "wallet": None, "at_ms": None,
                "reason": "还没有收到账户快照（持仓对账还没跑过）"}
    return {"known": True, "equity": plain(snap.equity), "available": plain(snap.available),
            "wallet": plain(snap.wallet), "at_ms": snap.at_ms}


def _stream(live: Any) -> dict[str, Any]:
    status = live.stream
    if status is None:
        return {"known": False, "state": None, "last_frame_ms": None, "reason": "还没有收到数据流状态"}
    return {"known": True, "state": status.state, "last_frame_ms": live.last_frame_ms, "reason": status.reason}


def _reconcile(live: Any, kind: str, label: str) -> dict[str, Any]:
    done = live.reconcile(kind)
    if done is None:
        return {"known": False, "last_ms": None, "status": None, "reason": f"{label}对账还没跑过"}
    return {"known": True, "last_ms": done.at_ms, "status": done.status}


def _list(ctx: ApiContext) -> dict[str, Any]:
    """状态条的「选币名单」：有效、成员数、有效期、无效原因。无效时成员数是名单同步仍沿用的成员（上一份有效快照未过期时）。"""
    provider = getattr(ctx, "list_view", None)
    view = provider() if provider is not None else None
    if view is None:
        return {"known": False, "valid": False, "reason": LIST_NOT_CONNECTED, "member_count": 0,
                "valid_until_ms": None}
    valid = bool(view.valid)
    reason = "" if valid else (view.reason or (LIST_NOT_FETCHED if view.fetched_ms is None else "名单无效"))
    return {"known": True, "valid": valid, "reason": reason, "member_count": len(view.members),
            "valid_until_ms": view.valid_until_ms}


def _model3(ctx: ApiContext) -> dict[str, Any]:
    """模型 3（AI，本期只预留）的状态：形态、AI 档、执行档、总闸、部署级开关，以及能否启动与拒绝原因（启动一律拒绝）。"""
    def value(key: str) -> Any:
        try:
            return ctx.config.get(key)
        except KeyError:
            return DEFINITIONS[key].default

    problems = ctx.models[Model.M3].start_problems({})
    return {"form": value("m3_form"), "ai_tier": value("m3_ai_tier"), "exec_tier": value("m3_exec_tier"),
            "master_switch": bool(value("m3_master_switch")), "deploy_switch": ctx.settings.model3_deploy_switch,
            "startable": not problems, "start_refusal": "；".join(problems)}


def _flag(known: Any, key: str, value: Any, reason: str) -> dict[str, Any]:
    if known is None:
        return {"known": False, key: None, "reason": reason}
    return {"known": True, key: value}


async def build_state(ctx: ApiContext, services: Services, principal: Principal) -> dict[str, Any]:
    now = ctx.clock.now_ms()
    snap = ctx.modelstate.current()
    live = services.live
    heartbeat_ms = await asyncio.to_thread(_read_heartbeat, ctx.heartbeat_file)
    occupancy = ctx.limits.occupancy(degraded=snap.run_mode is RunMode.POLLING_DEGRADED, now_ms=now)
    counts = ctx.registry.counts()
    mode, restrictions = live.position_mode, live.restrictions
    state = {
        "env": ctx.settings.environment.value,
        "standin": ctx.settings.environment is Environment.STANDIN,
        "time_ms": now,
        "heartbeat_ms": heartbeat_ms,
        "kernel_version": ctx.kernel_version,
        "model": _model(snap),
        "model3": _model3(ctx),
        "allowed": _allowed(snap),
        "button_meanings": ctx.models[snap.active_model].button_meanings(),
        "account": _account(live),
        "counts": {key: counts[key] for key in COUNT_KEYS},
        "list": _list(ctx),
        "stream": _stream(live),
        "reconcile": {"positions": _reconcile(live, "positions", "持仓"), "orders": _reconcile(live, "orders", "挂单")},
        "position_mode": _flag(mode, "dual_side", None if mode is None else mode.dual_side, "还没有读过持仓模式"),
        "restrictions": _flag(restrictions, "restricted", None if restrictions is None else restrictions.restricted,
                              "还没有读过交易所限制"),
        "risk": ctx.riskgate.status(),
        "limits": {"used": occupancy.used, "reserved": occupancy.reserved, "cap": occupancy.cap,
                   "stale": occupancy.stale},
        "seat": await services.seat.view(principal),
        "me": {"operator": principal.operator, "principal": principal.kind,
               "reauth_valid_until_ms": services.auth.reauth_valid_until(principal)},
    }
    return jsonable(state)
