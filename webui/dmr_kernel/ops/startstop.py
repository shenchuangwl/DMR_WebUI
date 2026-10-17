"""启动与停止（第 6 层 ops，报告 F7）。

``StartStop(deps)``；两个入口都走 ``ops.common.admit`` 的统一步骤，返回 ``{"op_id","status","result"}``，被拒绝抛 ``OpRejected``：

- ``await start(caller, *, op_id, model_version, password=None, verify=None)``：自检全部通过才开闸。
  自检（``self_check``）每一项都评估，结果是 ``[{"name","ok","detail"}]``，一项不过就 409 ``not_allowed``（报文带 ``checks``）、
  操作记为被拒绝、闸不开。读不到（未知）一律算不通过。项目名：
  ``position_mode`` 对冲模式、``equity`` 权益可读、``reconcile`` 对账无差异、``unprotected`` 没有无保护的登记仓、
  ``restrictions`` 交易所未限制、``legacy_orders`` 没有旧程序格式的新订单、``legacy_mutex`` 实盘时 7 把锁与停用标记（非实盘跳过）、
  ``stop_params`` 初始止损 A、B 已配置且 0<A<B、``risk_config`` 单笔名义金额与三项风控限额已配置、
  ``model`` 生效模型自己的项（每个问题一条；通过时一条 ok）。模型 1 的名单有效由 ``deps.list_valid`` 提供，
  没有来源（M4 之前）按「名单接口未接入」不通过。实盘启动需要重新输入口令。模型 3 由允许表直接拒绝。
- ``await stop(caller, *, op_id, model_version, close_all=False, password=None, verify=None)``：先关闸，再
  ``coordinator.cancel_unfilled_opens`` 撤未成交的开仓单；模型 2 的停止就是不再接管新仓（闸关着，自动接管不会触发）。
  结果 ``{"gate":"stopped","cancelled_open_orders":n,"managed_remaining":n}``。``close_all=True``（停止并全部平仓，需口令）：
  停止之后调用 M2B 的 ``dmr_kernel.ops.close_all.start_close_all(deps, caller, *, op_id, model_version, scope, parent_op_id)``
  （延迟导入；模块或函数不存在 501 ``not_implemented``，且什么都不改），操作号是 ``<op_id>:ca``，范围固定 ``all_managed``，
  ``model_version`` 是停止之后的新版本；结果另带 ``close_all_op_id``。撤单或一键全平启动失败时闸已经关了，操作记为
  ``partial`` 并在结果里写明原因，不抛错。
- ``await recover(caller, *, op_id)``：重新恢复（``POST /ops/recover``）。只在「恢复中」且上次恢复报了问题（``tradable`` 为假）时可用，
  需席位或本机令牌；再调一次 ``deps.recover()``（= ``Recovery.run()``），结果 ``{"tradable","problems","duration_ms"}`` 写进操作记录
  （全部解决 succeeded，仍有问题 partial）。没有 ``deps.recover`` 501。操作类型借用 ``OperationType.CONFIRM_MISMATCH``
  （共享枚举里还没有 RECOVER，已向主线提出追加；有了就自动改用），``scope="recover"``。
- 启动与停止串行（一把锁）；整个操作放进受保护的任务里，调用方断开也做完。
- 停止只关闸：已有仓的止损更新、信号平仓不经过开仓闸（协调者对非开仓意图不看闸），所以照常。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

from dmr_kernel.core import configdefs
from dmr_kernel.core.modelstate import ModelStateSnapshot, StaleVersion
from dmr_kernel.core.types import Gate, Known, Model, OperationStatus, OperationType, OrderOrigin, Suppression
from dmr_kernel.exchange import ids
from dmr_kernel.ops.common import (
    Caller,
    OpRejected,
    OpsDeps,
    Verifier,
    admit,
    finish_op,
    load_close_all,
    not_allowed,
    not_implemented,
    response_of,
    run_protected,
    stale_version,
)

log = logging.getLogger(__name__)

RECOVER_OP_TYPE = getattr(OperationType, "RECOVER", OperationType.CONFIRM_MISMATCH)
__all__ = ["StartStop"]

LIST_NOT_WIRED = "名单接口未接入（M4 接入）：模型 1 暂时不能启动"
STOP_PARAM_KEYS = ("init_stop_min_pct", "init_stop_max_pct")
RISK_KEYS = ("trade_notional_usdt", "daily_loss_limit", "max_drawdown", "daily_trade_limit")
OP_ID_ROOM = 61  # 操作号最长 64 位，后缀 ":ca" 占 3 位


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


class StartStop:
    def __init__(self, deps: OpsDeps) -> None:
        self._deps = deps
        self._lock = asyncio.Lock()

    # ================================================================ 自检（F7）

    async def self_check(self, state: ModelStateSnapshot | Any | None = None) -> list[dict[str, Any]]:
        """评估全部自检项（不短路）。``state`` 缺省取当前模型状态。"""
        state = state if state is not None else self._deps.modelstate.current()
        checks = [
            await self._position_mode(),
            await self._equity(),
            self._reconcile(state),
            self._unprotected(),
            await self._restrictions(),
            await self._legacy_orders(),
            self._legacy_mutex(),
            self._stop_params(),
            self._risk_config(),
        ]
        model = getattr(state, "active_model", None)
        if model is not None:
            checks.extend(self._model_checks(model))
        return checks

    def _cfg(self, key: str) -> Any:
        try:
            return self._deps.config_get(key)
        except KeyError:
            return None

    async def _position_mode(self) -> dict[str, Any]:
        result = await self._deps.adapter.fetch_position_mode()
        if not isinstance(result, Known):
            return _check("position_mode", False, f"读不到持仓模式：{result.reason}")
        if result.value is not True:
            return _check("position_mode", False, "账户是单向持仓模式，必须是对冲模式（双向持仓）")
        return _check("position_mode", True, "对冲模式")

    async def _equity(self) -> dict[str, Any]:
        result = await self._deps.adapter.fetch_account()
        if not isinstance(result, Known):
            return _check("equity", False, f"读不到账户权益：{result.reason}")
        return _check("equity", True, f"权益 {format(result.value.equity, 'f')} USDT")

    @staticmethod
    def _reconcile(state: Any) -> dict[str, Any]:
        problems = []
        suppressions = getattr(state, "suppressions", frozenset())
        if Suppression.RECONCILE_MISMATCH in suppressions:
            problems.append("对账不一致")
        if Suppression.RECONCILE_UNKNOWN in suppressions:
            problems.append("对账未知")
        if getattr(state, "mismatch_since_ms", None) is not None:
            problems.append("存在尚未解决的对账差异")
        if problems:
            return _check("reconcile", False, "；".join(problems))
        return _check("reconcile", True, "对账无差异")

    def _unprotected(self) -> dict[str, Any]:
        count = self._deps.registry.counts()["unprotected"]
        if count:
            return _check("unprotected", False, f"有 {count} 个登记仓没有保护（无保护持仓）")
        return _check("unprotected", True, "没有无保护的托管仓")

    async def _restrictions(self) -> dict[str, Any]:
        result = await self._deps.adapter.fetch_restrictions()
        if not isinstance(result, Known):
            return _check("restrictions", False, f"读不到交易所限制状态：{result.reason}")
        if result.value.restricted:
            detail = "、".join(f"{k}={v}" for k, v in result.value.detail)
            return _check("restrictions", False, "交易所限制了开仓" + (f"（{detail}）" if detail else ""))
        return _check("restrictions", True, "交易所未限制开仓")

    async def _legacy_orders(self) -> dict[str, Any]:
        adapter = self._deps.adapter
        orders, algos = await adapter.fetch_open_orders(), await adapter.fetch_open_algo_orders()
        for result in (orders, algos):
            if not isinstance(result, Known):
                return _check("legacy_orders", False, f"读不到挂单：{result.reason}")
        legacy = [o.client_order_id for o in orders.value if _is_legacy(o.client_order_id)]
        legacy += [a.client_algo_id for a in algos.value if _is_legacy(a.client_algo_id)]
        if legacy:
            return _check("legacy_orders", False, f"有 {len(legacy)} 张订单的编号是旧程序格式：{'、'.join(legacy[:3])}")
        return _check("legacy_orders", True, "没有旧程序格式的新订单")

    def _legacy_mutex(self) -> dict[str, Any]:
        deps = self._deps
        if not deps.is_live:
            return _check("legacy_mutex", True, "非实盘环境，不检查旧入口互斥")
        if deps.legacy_check is None:
            return _check("legacy_mutex", False, "旧入口互斥没有接入，实盘不能启动")
        problems = list(deps.legacy_check())
        if problems:
            return _check("legacy_mutex", False, "；".join(problems))
        return _check("legacy_mutex", True, "7 把旧入口锁都在内核手里，停用标记存在")

    def _validated(self, key: str) -> tuple[Any, str | None]:
        label = configdefs.DEFINITIONS[key].label
        value = self._cfg(key)
        if value is None:
            return None, f"{label}还没有配置"
        try:
            return configdefs.validate_value(key, value), None
        except configdefs.ConfigError as exc:
            return None, f"{label}无效：{exc}"
        except ArithmeticError:
            return None, f"{label}无效：不是有效的数值"

    def _stop_params(self) -> dict[str, Any]:
        values, problems = [], []
        for key in STOP_PARAM_KEYS:
            value, problem = self._validated(key)
            values.append(value)
            if problem:
                problems.append(problem)
        if not problems and not values[0] < values[1]:
            problems.append("初始止损距离必须 A < B")
        if problems:
            return _check("stop_params", False, "；".join(problems))
        return _check("stop_params", True, f"初始止损 A={values[0]}%、B={values[1]}%")

    def _risk_config(self) -> dict[str, Any]:
        problems = [problem for key in RISK_KEYS if (problem := self._validated(key)[1])]
        if problems:
            return _check("risk_config", False, "；".join(problems))
        return _check("risk_config", True, "单笔名义金额与三项风控限额已配置")

    def _model_checks(self, model: Any) -> list[dict[str, Any]]:
        deps = self._deps
        trading_model = deps.models[model]
        list_valid = deps.list_valid() if deps.list_valid is not None else None
        ctx = {"list_valid": list_valid, "config_get": deps.config_get}
        problems = trading_model.start_problems(ctx)
        if not problems:
            return [_check("model", True, f"模型 {model.value[1:]} 的专项自检通过")]
        if deps.list_valid is None:
            problems = [LIST_NOT_WIRED if p.startswith("名单") else p for p in problems]
        return [_check("model", False, problem) for problem in problems]

    # ================================================================ 启动

    async def start(self, caller: Caller, *, op_id: str, model_version: int, password: str | None = None,
                    verify: Verifier | None = None) -> dict[str, Any]:
        return await run_protected(self._start(caller, op_id, model_version, password, verify))

    async def _start(self, caller: Caller, op_id: str, model_version: int, password: str | None,
                     verify: Verifier | None) -> dict[str, Any]:
        deps = self._deps
        async with self._lock:
            model = deps.modelstate.current().active_model
            admission = await admit(deps, caller, op_id=op_id, op_type=OperationType.START, allowed_key="start",
                                    model_version=model_version, reauth=deps.is_live, password=password, verify=verify,
                                    scope=model.value, params={"model": model.value})
            if not admission.is_new:
                return response_of(admission.record)
            state = admission.state
            try:
                checks = await self.self_check(state)
            except BaseException as exc:
                await self._fail(op_id, exc)
                raise
            failed = [c for c in checks if not c["ok"]]
            if failed:
                await finish_op(deps, op_id, OperationStatus.REJECTED, {"checks": checks})
                raise not_allowed("启动自检未通过：" + "；".join(c["detail"] for c in failed), checks=checks)
            try:
                reason = f"启动（操作 {op_id}，{caller.operator}）"
                if state.active_model is Model.M2:  # 模型 2 的启动时刻与开闸同一事务（一个版本、一条审计）
                    await deps.modelstate.open_gate_recording_m2_start(deps.clock.now_ms(), reason=reason,
                                                                       expected_version=model_version)
                else:
                    await deps.modelstate.set_gate(Gate.RUNNING, reason=reason, expected_version=model_version)
            except StaleVersion as exc:
                await finish_op(deps, op_id, OperationStatus.REJECTED, {"message": str(exc)})
                raise stale_version(str(exc)) from None
            except BaseException as exc:
                await self._fail(op_id, exc)
                raise
            record = await finish_op(deps, op_id, OperationStatus.SUCCEEDED,
                                     {"gate": "running", "model": state.active_model.value, "checks": checks})
            return response_of(record)

    async def _fail(self, op_id: str, exc: BaseException) -> None:
        """尽力把进行中的操作记为失败；记不上也不能盖掉原来的错误。"""
        log.error("操作 %s 出现内部错误：%s", op_id, exc, exc_info=exc)
        try:
            await finish_op(self._deps, op_id, OperationStatus.FAILED,
                            {"message": f"内部错误（{type(exc).__name__}），详情见内核日志"})
        except Exception:
            log.exception("操作 %s 无法记为失败", op_id)

    # ================================================================ 停止

    async def stop(self, caller: Caller, *, op_id: str, model_version: int, close_all: bool = False,
                   password: str | None = None, verify: Verifier | None = None) -> dict[str, Any]:
        return await run_protected(self._stop(caller, op_id, model_version, close_all, password, verify))

    async def _stop(self, caller: Caller, op_id: str, model_version: int, close_all: bool, password: str | None,
                    verify: Verifier | None) -> dict[str, Any]:
        deps = self._deps
        module = None

        async def need_close_all_module(state: ModelStateSnapshot, position: Any) -> None:
            nonlocal module
            module = load_close_all(deps)
            if module is None:
                raise not_implemented("一键全平尚未提供，不能「停止并全部平仓」；可以只停止")

        async with self._lock:
            model = deps.modelstate.current().active_model
            op_type = OperationType.STOP_AND_CLOSE_ALL if close_all else OperationType.STOP
            admission = await admit(
                deps, caller, op_id=op_id, op_type=op_type, allowed_key="stop", model_version=model_version,
                reauth=close_all, password=password, verify=verify, scope=model.value, params={"close_all": close_all},
                precheck=need_close_all_module if close_all else None)
            if not admission.is_new:
                return response_of(admission.record)
            try:
                snapshot = await deps.modelstate.set_gate(
                    Gate.STOPPED, reason=f"停止（操作 {op_id}，{caller.operator}）", expected_version=model_version)
            except StaleVersion as exc:
                await finish_op(deps, op_id, OperationStatus.REJECTED, {"message": str(exc)})
                raise stale_version(str(exc)) from None
            except BaseException as exc:
                await self._fail(op_id, exc)
                raise
            result: dict[str, Any] = {"gate": "stopped", "cancelled_open_orders": 0,
                                      "managed_remaining": len(deps.registry.registered())}
            status = OperationStatus.SUCCEEDED
            try:
                result["cancelled_open_orders"] = await deps.coordinator.cancel_unfilled_opens(
                    model=None, reason=f"停止（操作 {op_id}）")
            except Exception as exc:
                log.exception("停止时撤未成交的开仓单出错（操作 %s）", op_id)
                result["cancel_error"] = f"撤未成交的开仓单没有做完（{type(exc).__name__}），闸已关闭，请到订单页核对"
                status = OperationStatus.PARTIAL
            if close_all:
                status = await self._hand_over(caller, module, op_id, snapshot, result, status)
            record = await finish_op(deps, op_id, status, result)
            return response_of(record)

    async def _hand_over(self, caller: Caller, module: Any, op_id: str, snapshot: ModelStateSnapshot,
                         result: dict[str, Any], status: OperationStatus) -> OperationStatus:
        """停止之后另起一键全平（它自己的操作号与记录）。口令刚在本次请求里校验过：给它一个新的校验时间。"""
        close_all_id = f"{op_id[:OP_ID_ROOM]}:ca"
        result["close_all_op_id"] = close_all_id
        fresh = dataclasses.replace(caller, reauth_ms=self._deps.clock.now_ms()) if caller.is_session else caller
        try:
            await module.start_close_all(self._deps, fresh, op_id=close_all_id, model_version=snapshot.version,
                                         scope="all_managed", parent_op_id=op_id)
        except OpRejected as exc:
            result["close_all_error"] = exc.message
            return OperationStatus.PARTIAL
        except Exception as exc:
            log.exception("停止后启动一键全平出错（操作 %s）", op_id)
            result["close_all_error"] = f"一键全平没有启动（{type(exc).__name__}），闸已关闭"
            return OperationStatus.PARTIAL
        return status

    # ================================================================ 重新恢复

    async def recover(self, caller: Caller, *, op_id: str) -> dict[str, Any]:
        return await run_protected(self._recover(caller, op_id))

    async def _recover(self, caller: Caller, op_id: str) -> dict[str, Any]:
        deps = self._deps

        async def only_when_stuck(state: ModelStateSnapshot, position: Any) -> None:
            if Suppression.RECOVERING not in state.suppressions:
                raise not_allowed("当前不在恢复中，无需重新恢复")
            last = deps.last_recovery() if deps.last_recovery is not None else None
            if last is None or getattr(last, "tradable", True):
                raise not_allowed("上次恢复没有报告问题，请等它完成")
            if deps.recover is None:
                raise not_implemented("重新恢复尚未接入")

        async with self._lock:
            admission = await admit(deps, caller, op_id=op_id, op_type=RECOVER_OP_TYPE, scope="recover",
                                    params={"kind": "recover"}, precheck=only_when_stuck)
            if not admission.is_new:
                return response_of(admission.record)
            try:
                outcome = await deps.recover()
            except Exception as exc:
                await self._fail(op_id, exc)
                raise
            tradable = bool(getattr(outcome, "tradable", False))
            result = {"tradable": tradable, "problems": list(getattr(outcome, "problems", ())),
                      "duration_ms": getattr(outcome, "duration_ms", None)}
            record = await finish_op(deps, op_id, OperationStatus.SUCCEEDED if tradable else OperationStatus.PARTIAL,
                                     result)
            return response_of(record)


def _is_legacy(client_id: str | None) -> bool:
    return ids.parse_client_id(client_id).origin is OrderOrigin.LEGACY
