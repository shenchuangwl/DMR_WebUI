"""ops.startstop：启动（F7 自检全部通过才开闸）与停止（关闸、撤未成交开仓单、可带「同时全部平仓」）。

覆盖：自检每一项单独不过都不开闸且其余各项照常列出；读不到（未知）不当作通过；模型 1 / 模型 2 / 模型 3 各自的项；
实盘需要口令与旧入口互斥；状态版本过期；席位；同一操作号只执行一次；停止后不再产生开仓意图而已有仓的止损照常更新；
停止的撤单顺序（先关闸再撤）；close_all 的延迟导入（模块不存在 501）与调用约定。
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from dmr_kernel.core.events import OperationProgress
from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.types import (
    Gate,
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    Model,
    OpLock,
    OperationStatus,
    OperationType,
    OrderInfo,
    OrderSide,
    PositionSide,
    ProtectionState,
    RestrictionInfo,
    Suppression,
    Unknown,
)
from dmr_kernel.ops.common import Caller, OpRejected
from dmr_kernel.ops.startstop import StartStop
from tests.unit.ops_m2a.m2a_kit import StubAdapter, make_deps, rig

D = Decimal
LONG = PositionSide.LONG
SESSION = Caller("alice", 7, True, True, None)
NOSEAT = Caller("bob", 8, True, False, None)
CLI = Caller.cli()
CHECK_NAMES = {"position_mode", "equity", "reconcile", "unprotected", "restrictions", "legacy_orders", "legacy_mutex",
               "stop_params", "risk_config"}


def good_deps(r, **kw):
    """所有自检项都能过的依赖（模型 1 的名单有效由测试替身给）。"""
    return make_deps(r.kernel, list_valid=lambda: True, **kw)


def by_name(checks):
    out = {}
    for check in checks:
        out.setdefault(check["name"], []).append(check)
    return out


async def go_start(svc, op_id="s-1", caller=SESSION, version=None, **kw):
    state = svc._deps.modelstate.current()
    return await svc.start(caller, op_id=op_id, model_version=state.version if version is None else version, **kw)


async def go_stop(svc, op_id="p-1", caller=SESSION, version=None, **kw):
    state = svc._deps.modelstate.current()
    return await svc.stop(caller, op_id=op_id, model_version=state.version if version is None else version, **kw)


# ---------------------------------------------------------------- 启动：成功路径


async def test_start_opens_the_gate_when_every_check_passes(tmp_path):
    async with rig(tmp_path) as r:
        svc = StartStop(good_deps(r))
        version = r.kernel.modelstate.current().version
        response = await go_start(svc)
        assert response["op_id"] == "s-1" and response["status"] == "succeeded"
        result = response["result"]
        assert result["gate"] == "running" and result["model"] == "m1"
        assert {c["name"] for c in result["checks"]} >= CHECK_NAMES
        assert all(c["ok"] for c in result["checks"])
        assert all(isinstance(c["detail"], str) for c in result["checks"])
        state = r.kernel.modelstate.current()
        assert state.gate is Gate.RUNNING and state.version == version + 1
        record = await r.kernel.operations.get("s-1")
        assert record.op_type is OperationType.START and record.status is OperationStatus.SUCCEEDED
        assert record.operator == "alice" and record.state_version == version


async def test_start_for_model_2_needs_a_takeover_list_and_notional_bounds(tmp_path):
    async with rig(tmp_path, config_overrides={"takeover_symbols": ["BTCUSDT"]}) as r:
        await r.kernel.modelstate.set_active_model(Model.M2, expected_version=1, operator="t", op_id="sw-1")
        svc = StartStop(make_deps(r.kernel))  # 模型 2 不需要名单
        response = await go_start(svc)
        assert response["result"]["model"] == "m2" and r.kernel.modelstate.current().gate is Gate.RUNNING


async def _gate_audit(kernel) -> list[str]:
    from dmr_kernel.ledger import repo_audit

    rows = await kernel.ledger.read(repo_audit.recent, limit=1000)
    return [r.action for r in rows if r.action in ("model_state.open_gate_m2", "model_state.gate")]


async def test_starting_model_2_records_its_start_time_in_the_same_change_as_the_gate(tmp_path):
    # M4 收尾：模型 2 的启动时刻与开闸同一事务（一个版本、一条审计），不会有「闸已开、启动时刻还是上一次」的窗口
    async with rig(tmp_path, config_overrides={"takeover_symbols": ["BTCUSDT"]}) as r:
        await r.kernel.modelstate.set_active_model(Model.M2, expected_version=1, operator="t", op_id="sw-1")
        before = r.kernel.modelstate.current()
        assert before.m2_started_ms is None
        await go_start(StartStop(make_deps(r.kernel)))
        state = r.kernel.modelstate.current()
        assert state.gate is Gate.RUNNING and state.m2_started_ms == r.kernel.clock.now_ms()
        assert (await _gate_audit(r.kernel)).count("model_state.open_gate_m2") == 1
        record = await r.kernel.operations.get("s-1")
        assert record.status is OperationStatus.SUCCEEDED and record.state_version == before.version


async def test_starting_model_1_leaves_the_model_2_start_time_alone(tmp_path):
    async with rig(tmp_path) as r:
        await go_start(StartStop(good_deps(r)))
        state = r.kernel.modelstate.current()
        assert state.gate is Gate.RUNNING and state.m2_started_ms is None
        assert "model_state.open_gate_m2" not in await _gate_audit(r.kernel)


async def test_starting_model_2_with_a_stale_version_records_nothing(tmp_path):
    async with rig(tmp_path, config_overrides={"takeover_symbols": ["BTCUSDT"]}) as r:
        await r.kernel.modelstate.set_active_model(Model.M2, expected_version=1, operator="t", op_id="sw-1")
        version = r.kernel.modelstate.current().version
        with pytest.raises(OpRejected) as caught:
            await go_start(StartStop(make_deps(r.kernel)), version=version - 1)
        assert caught.value.code == "stale_version"
        state = r.kernel.modelstate.current()
        assert state.gate is Gate.STOPPED and state.m2_started_ms is None


async def test_start_publishes_operation_progress(tmp_path):
    async with rig(tmp_path) as r:
        seen = []
        r.kernel.bus.subscribe(OperationProgress, seen.append, name="t")
        await go_start(StartStop(good_deps(r)))
        await r.kernel.bus.drain()
        assert [(e.op_id, e.op_type, e.status) for e in seen] == [("s-1", "start", "running"),
                                                                  ("s-1", "start", "succeeded")]


# ---------------------------------------------------------------- 启动：自检每一项不过都不开闸

FAIL_CASES = {
    "hedge_off": ("position_mode", lambda r, d, a: a.set("fetch_position_mode", Known(False))),
    "hedge_unknown": ("position_mode", lambda r, d, a: a.set("fetch_position_mode", Unknown("超时"))),
    "equity_unknown": ("equity", lambda r, d, a: a.set("fetch_account", Unknown("超时"))),
    "restricted": ("restrictions", lambda r, d, a: a.set("fetch_restrictions", Known(RestrictionInfo(True, (("a", "b"),))))),
    "restrictions_unknown": ("restrictions", lambda r, d, a: a.set("fetch_restrictions", Unknown("超时"))),
    "legacy_order": ("legacy_orders", lambda r, d, a: a.set("fetch_open_orders", Known([OrderInfo(
        symbol="BTCUSDT", client_order_id="DMR_LONG_buy", exchange_order_id="1", side=OrderSide.BUY,
        position_side=LONG, order_type="LIMIT", qty=D("1"), filled_qty=D("0"), avg_price=None, status="NEW",
        raw_status="NEW")]))),
    "open_orders_unknown": ("legacy_orders", lambda r, d, a: a.set("fetch_open_orders", Unknown("超时"))),
}


@pytest.mark.parametrize("case", list(FAIL_CASES))
async def test_a_failed_check_keeps_the_gate_closed_and_lists_every_check(tmp_path, case):
    name, break_it = FAIL_CASES[case]
    async with rig(tmp_path) as r:
        stub = StubAdapter(r.kernel.adapter)
        break_it(r, None, stub)
        svc = StartStop(good_deps(r, adapter=stub))
        version = r.kernel.modelstate.current().version
        with pytest.raises(OpRejected) as caught:
            await go_start(svc)
        error = caught.value
        assert (error.status, error.code) == (409, "not_allowed")
        checks = error.extra["checks"]
        assert {c["name"] for c in checks} >= CHECK_NAMES  # 一项不过，其余照常列出
        assert [c["name"] for c in checks if not c["ok"]] == [name]
        assert all(c["detail"] for c in checks if not c["ok"])
        state = r.kernel.modelstate.current()
        assert state.gate is Gate.STOPPED and state.version == version
        record = await r.kernel.operations.get("s-1")
        assert record.status is OperationStatus.REJECTED and record.result["checks"] == checks


async def test_an_unprotected_registered_position_blocks_the_start(tmp_path):
    async with rig(tmp_path) as r:
        position = await r.open_position()
        await r.kernel.registry.set_protection(position.id, state=ProtectionState.NONE, clear_stop=True)
        with pytest.raises(OpRejected) as caught:
            await go_start(StartStop(good_deps(r)))
        failed = [c for c in caught.value.extra["checks"] if not c["ok"]]
        assert [c["name"] for c in failed] == ["unprotected"] and "1" in failed[0]["detail"]
        assert r.kernel.modelstate.current().gate is Gate.STOPPED


async def test_reconcile_differences_block_the_start_even_if_the_table_check_were_bypassed(tmp_path):
    async with rig(tmp_path) as r:
        svc = StartStop(good_deps(r))
        for suppression in (Suppression.RECONCILE_MISMATCH, Suppression.RECONCILE_UNKNOWN):
            state = SimpleNamespace(suppressions=frozenset({suppression}), mismatch_since_ms=None)
            check = by_name(await svc.self_check(state))["reconcile"][0]
            assert check["ok"] is False and check["detail"]
        mismatch = SimpleNamespace(suppressions=frozenset(), mismatch_since_ms=123)
        assert by_name(await svc.self_check(mismatch))["reconcile"][0]["ok"] is False
        clean = SimpleNamespace(suppressions=frozenset(), mismatch_since_ms=None)
        assert by_name(await svc.self_check(clean))["reconcile"][0]["ok"] is True


async def test_start_is_refused_by_the_table_when_a_suppression_is_present(tmp_path):
    async with rig(tmp_path) as r:
        await r.kernel.modelstate.add_suppression(Suppression.RECONCILE_MISMATCH, reason="t")
        with pytest.raises(OpRejected) as caught:
            await go_start(StartStop(good_deps(r)))
        assert caught.value.code == "not_allowed" and "对账不一致" in caught.value.message
        assert "checks" not in caught.value.extra  # 表挡在自检之前


def configured(r, **overrides):
    """config_get 替身：指定的键返回指定值（None = 没配置），其余取真实配置。"""
    real = r.kernel.config.get

    def get(key):
        return overrides[key] if key in overrides else real(key)

    return get


@pytest.mark.parametrize("overrides,name", [
    ({"init_stop_min_pct": None}, "stop_params"),
    ({"init_stop_max_pct": None}, "stop_params"),
    ({"init_stop_min_pct": D("6"), "init_stop_max_pct": D("5")}, "stop_params"),  # A 必须小于 B
    ({"trade_notional_usdt": None}, "risk_config"),
    ({"daily_loss_limit": None}, "risk_config"),
    ({"max_drawdown": None}, "risk_config"),
    ({"daily_trade_limit": None}, "risk_config")])
async def test_missing_or_invalid_money_settings_block_the_start(tmp_path, overrides, name):
    async with rig(tmp_path) as r:
        svc = StartStop(good_deps(r, config_get=configured(r, **overrides)))
        with pytest.raises(OpRejected) as caught:
            await go_start(svc)
        assert [c["name"] for c in caught.value.extra["checks"] if not c["ok"]] == [name]
        assert r.kernel.modelstate.current().gate is Gate.STOPPED


# ---------------------------------------------------------------- 启动：模型各自的项


async def test_model_1_without_a_list_source_fails_as_list_not_wired(tmp_path):
    async with rig(tmp_path) as r:
        with pytest.raises(OpRejected) as caught:
            await go_start(StartStop(make_deps(r.kernel)))  # 没有名单来源：M1 阶段名单接口未接入
        failed = [c for c in caught.value.extra["checks"] if not c["ok"]]
        assert [c["name"] for c in failed] == ["model"] and "名单接口未接入" in failed[0]["detail"]


async def test_model_1_with_an_invalid_list_fails_and_a_valid_empty_list_passes(tmp_path):
    async with rig(tmp_path) as r:
        with pytest.raises(OpRejected) as caught:
            await go_start(StartStop(make_deps(r.kernel, list_valid=lambda: False)))
        assert [c["name"] for c in caught.value.extra["checks"] if not c["ok"]] == ["model"]
        assert "接口未接入" not in [c for c in caught.value.extra["checks"] if not c["ok"]][0]["detail"]
        response = await go_start(StartStop(make_deps(r.kernel, list_valid=lambda: True)), op_id="s-2")
        assert response["result"]["gate"] == "running"


@pytest.mark.parametrize("overrides", [
    {"leverage": None}, {"margin_mode": None}])
async def test_model_1_needs_leverage_and_margin_mode(tmp_path, overrides):
    async with rig(tmp_path) as r:
        svc = StartStop(good_deps(r, config_get=configured(r, **overrides)))
        with pytest.raises(OpRejected) as caught:
            await go_start(svc)
        assert [c["name"] for c in caught.value.extra["checks"] if not c["ok"]] == ["model"]


@pytest.mark.parametrize("overrides", [
    {"takeover_symbols": []},
    {"takeover_min_notional": None},
    {"takeover_max_notional": None},
    {"takeover_min_notional": D("300"), "takeover_max_notional": D("200")}])
async def test_model_2_needs_a_takeover_list_and_ordered_notional_bounds(tmp_path, overrides):
    async with rig(tmp_path, config_overrides={"takeover_symbols": ["BTCUSDT"]}) as r:
        await r.kernel.modelstate.set_active_model(Model.M2, expected_version=1, operator="t", op_id="sw-1")
        svc = StartStop(make_deps(r.kernel, config_get=configured(r, **overrides)))
        with pytest.raises(OpRejected) as caught:
            await go_start(svc)
        assert [c["name"] for c in caught.value.extra["checks"] if not c["ok"]] == ["model"]
        assert r.kernel.modelstate.current().gate is Gate.STOPPED


async def test_model_3_start_is_always_refused(tmp_path):
    async with rig(tmp_path) as r:
        await r.kernel.modelstate.set_active_model(Model.M3, expected_version=1, operator="t", op_id="sw-3")
        with pytest.raises(OpRejected) as caught:
            await go_start(StartStop(good_deps(r)))
        assert caught.value.code == "not_allowed" and "模型 3" in caught.value.message
        assert r.kernel.modelstate.current().gate is Gate.STOPPED


# ---------------------------------------------------------------- 启动：前置步骤


async def test_start_refused_when_already_running_or_locked(tmp_path):
    async with rig(tmp_path) as r:
        svc = StartStop(good_deps(r))
        await go_start(svc)
        with pytest.raises(OpRejected) as caught:
            await go_start(svc, op_id="s-2")
        assert caught.value.code == "not_allowed" and "已在运行" in caught.value.message
        await r.kernel.modelstate.set_gate(Gate.STOPPED, reason="t")
        await r.kernel.modelstate.acquire_op_lock(OpLock.SWITCH_PREVIEW, owner="x")
        with pytest.raises(OpRejected) as caught:
            await go_start(svc, op_id="s-3")
        assert caught.value.code == "not_allowed" and "切换预览" in caught.value.message


async def test_start_with_a_stale_model_version_is_refused_and_nothing_changes(tmp_path):
    async with rig(tmp_path) as r:
        svc = StartStop(good_deps(r))
        version = r.kernel.modelstate.current().version
        with pytest.raises(OpRejected) as caught:
            await go_start(svc, version=version - 1)
        assert caught.value.code == "stale_version"
        assert r.kernel.modelstate.current().gate is Gate.STOPPED
        assert await r.kernel.operations.get("s-1") is None


async def test_start_needs_the_seat_for_a_session_but_not_for_the_local_token(tmp_path):
    async with rig(tmp_path) as r:
        svc = StartStop(good_deps(r))
        with pytest.raises(OpRejected) as caught:
            await go_start(svc, caller=NOSEAT)
        assert caught.value.code == "seat_required" and r.kernel.modelstate.current().gate is Gate.STOPPED
        response = await go_start(svc, caller=CLI, op_id="s-2")
        assert response["status"] == "succeeded"
        assert (await r.kernel.operations.get("s-2")).operator == "cli"


async def test_live_start_needs_the_password_and_the_legacy_mutex(tmp_path):
    async with rig(tmp_path) as r:
        verified = []

        async def verify(password):
            verified.append(password)

        mutex_ok = []
        deps = good_deps(r, is_live=True, legacy_check=lambda: list(mutex_ok))
        svc = StartStop(deps)
        with pytest.raises(OpRejected) as caught:
            await go_start(svc)  # 会话没验证过口令，请求里也没带
        assert caught.value.code == "reauth_required" and r.kernel.modelstate.current().gate is Gate.STOPPED
        mutex_ok.append("停用标记缺失")
        with pytest.raises(OpRejected) as caught:
            await go_start(svc, password="pw", verify=verify)
        assert verified == ["pw"]
        assert [c["name"] for c in caught.value.extra["checks"] if not c["ok"]] == ["legacy_mutex"]
        assert "停用标记缺失" in [c for c in caught.value.extra["checks"] if c["name"] == "legacy_mutex"][0]["detail"]
        mutex_ok.clear()
        response = await go_start(svc, op_id="s-2", password="pw", verify=verify)
        assert response["status"] == "succeeded" and r.kernel.modelstate.current().gate is Gate.RUNNING


async def test_live_start_without_a_mutex_source_fails_the_mutex_check_and_non_live_skips_it(tmp_path):
    async with rig(tmp_path) as r:
        live = StartStop(good_deps(r, is_live=True, legacy_check=None))
        check = by_name(await live.self_check())["legacy_mutex"][0]
        assert check["ok"] is False
        offline = StartStop(good_deps(r))
        check = by_name(await offline.self_check())["legacy_mutex"][0]
        assert check["ok"] is True and "非实盘" in check["detail"]


async def test_a_non_live_start_ignores_the_password_entirely(tmp_path):
    async with rig(tmp_path) as r:
        calls = []

        async def verify(password):
            calls.append(password)

        await go_start(StartStop(good_deps(r)), password="whatever", verify=verify)
        assert calls == []


async def test_a_repeated_operation_id_starts_only_once(tmp_path):
    async with rig(tmp_path) as r:
        svc = StartStop(good_deps(r))
        first = await go_start(svc)
        version = r.kernel.modelstate.current().version
        again = await svc.start(SESSION, op_id="s-1", model_version=version - 1)  # 即使版本已变、闸已开
        assert again == first
        assert r.kernel.modelstate.current().version == version


async def test_the_check_reads_use_the_live_exchange_not_stale_state(tmp_path):
    async with rig(tmp_path) as r:
        stub = StubAdapter(r.kernel.adapter)
        svc = StartStop(good_deps(r, adapter=stub))
        await go_start(svc)
        assert {"fetch_position_mode", "fetch_account", "fetch_restrictions", "fetch_open_orders"} <= set(stub.calls)


# ---------------------------------------------------------------- 停止


class FakeCoordinator:
    """记下 cancel_unfilled_opens 被调用时的闸位。"""

    def __init__(self, real, modelstate, count=3) -> None:
        self.real, self.modelstate, self.count, self.calls = real, modelstate, count, []

    async def cancel_unfilled_opens(self, *, model, reason):
        self.calls.append((model, reason, self.modelstate.current().gate))
        return self.count

    def __getattr__(self, name):
        return getattr(self.real, name)


async def running(r, **kw):
    svc = StartStop(good_deps(r, **kw))
    await go_start(svc, op_id="run-1")
    return svc


async def test_stop_closes_the_gate_before_cancelling_unfilled_opens(tmp_path):
    async with rig(tmp_path) as r:
        fake = FakeCoordinator(r.kernel.coordinator, r.kernel.modelstate, count=3)
        svc = await running(r, coordinator=fake)
        response = await go_stop(svc)
        assert response["status"] == "succeeded"
        assert response["result"] == {"gate": "stopped", "cancelled_open_orders": 3, "managed_remaining": 0}
        assert fake.calls and fake.calls[0][2] is Gate.STOPPED  # 先关闸，再撤
        assert r.kernel.modelstate.current().gate is Gate.STOPPED
        record = await r.kernel.operations.get("p-1")
        assert record.op_type is OperationType.STOP and record.status is OperationStatus.SUCCEEDED


async def test_stop_reports_how_many_positions_stay_managed(tmp_path):
    async with rig(tmp_path) as r:
        await r.open_position("BTCUSDT")
        await r.open_position("ETHUSDT")
        svc = await running(r)
        response = await go_stop(svc)
        assert response["result"]["managed_remaining"] == 2
        assert all(p.status.value == "managed" for p in r.kernel.registry.registered())  # 停止不动持仓


async def test_stop_when_already_stopped_is_refused_by_the_table(tmp_path):
    async with rig(tmp_path) as r:
        svc = StartStop(good_deps(r))
        with pytest.raises(OpRejected) as caught:
            await go_stop(svc)
        assert caught.value.code == "not_allowed" and "已停止" in caught.value.message


async def test_stop_is_allowed_under_a_suppression_even_if_the_gate_is_already_closed(tmp_path):
    async with rig(tmp_path) as r:
        svc = StartStop(good_deps(r))
        await r.kernel.modelstate.add_suppression(Suppression.RECOVERING, reason="t")
        version = r.kernel.modelstate.current().version
        response = await go_stop(svc)
        assert response["result"]["gate"] == "stopped"
        assert r.kernel.modelstate.current().version == version  # 闸本来就关着：不增版本


async def test_stop_is_refused_while_switching_and_allowed_during_a_bulk_close(tmp_path):
    async with rig(tmp_path) as r:
        svc = await running(r)
        await r.kernel.modelstate.acquire_op_lock(OpLock.SWITCHING, owner="x")
        with pytest.raises(OpRejected) as caught:
            await go_stop(svc)
        assert caught.value.code == "not_allowed" and "切换进行中" in caught.value.message
        await r.kernel.modelstate.release_op_lock(OpLock.SWITCHING)
        await r.kernel.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner="x")
        assert (await go_stop(svc))["status"] == "succeeded"


async def test_stop_with_a_stale_version_or_without_the_seat_changes_nothing(tmp_path):
    async with rig(tmp_path) as r:
        svc = await running(r)
        version = r.kernel.modelstate.current().version
        with pytest.raises(OpRejected) as caught:
            await go_stop(svc, version=version - 1)
        assert caught.value.code == "stale_version"
        with pytest.raises(OpRejected) as caught:
            await go_stop(svc, caller=NOSEAT)
        assert caught.value.code == "seat_required"
        assert r.kernel.modelstate.current().gate is Gate.RUNNING


async def test_a_repeated_stop_operation_id_cancels_only_once(tmp_path):
    async with rig(tmp_path) as r:
        fake = FakeCoordinator(r.kernel.coordinator, r.kernel.modelstate)
        svc = await running(r, coordinator=fake)
        first = await go_stop(svc)
        again = await svc.stop(SESSION, op_id="p-1", model_version=1)
        assert again == first and len(fake.calls) == 1


async def test_after_a_stop_no_new_open_intent_gets_through_but_the_existing_positions_stop_still_updates(tmp_path):
    async with rig(tmp_path) as r:
        position = await r.open_position()
        assert position.protection_state is ProtectionState.SYSTEM and position.stop_price is not None
        svc = await running(r)
        await go_stop(svc)
        assert r.kernel.modelstate.current().gate is Gate.STOPPED
        # 1) 策略的开仓意图被闸挡住
        spec = IntentSpec(action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol="ETHUSDT", side=LONG,
                          idem_key="after-stop-1", ttl_s=60)
        submitted = await r.kernel.coordinator.submit(spec)
        opened = await r.kernel.coordinator.wait_terminal(submitted.intent_id, 5)
        assert opened.state is IntentState.REJECTED and "开仓闸已停止" in opened.state_reason
        assert await r.exchange_qty("ETHUSDT") == D("0")
        # 2) 已有仓的止损照常更新（保护类意图不受闸约束）
        new_price = position.stop_price + D("100")
        replace = IntentSpec(
            action=IntentAction.STOP_REPLACE, model="op", source=IntentSource.PROTECTION, symbol=position.symbol,
            side=LONG, idem_key="after-stop-2", position_id=position.id, position_generation=position.generation,
            params={"old": {"client_algo_id": position.stop_client_id, "algo_id": position.stop_algo_id,
                            "trigger_price": str(position.stop_price)},
                    "trigger_price": str(new_price), "working_type": "CONTRACT_PRICE"})
        submitted = await r.kernel.coordinator.submit(replace)
        moved = await r.kernel.coordinator.wait_terminal(submitted.intent_id, 5)
        assert moved.state is IntentState.COMPLETED and moved.params["result"]["outcome"] == "replaced"


# ---------------------------------------------------------------- 停止并全部平仓


class FakeCloseAll:
    def __init__(self) -> None:
        self.calls = []

    async def start_close_all(self, deps, caller, *, op_id, model_version, scope, parent_op_id):
        self.calls.append(dict(op_id=op_id, model_version=model_version, scope=scope, parent_op_id=parent_op_id,
                               caller=caller, gate=deps.modelstate.current().gate))
        return {"status": "running"}


async def test_stop_with_close_all_needs_the_password_and_hands_over_to_the_bulk_close(tmp_path):
    async with rig(tmp_path) as r:
        module = FakeCloseAll()
        svc = await running(r, close_all_loader=lambda: module)
        with pytest.raises(OpRejected) as caught:
            await go_stop(svc, close_all=True)  # 会话没验证过口令
        assert caught.value.code == "reauth_required" and r.kernel.modelstate.current().gate is Gate.RUNNING

        async def verify(password):
            assert password == "pw"

        response = await go_stop(svc, close_all=True, password="pw", verify=verify)
        result = response["result"]
        assert result["gate"] == "stopped" and result["close_all_op_id"] == "p-1:ca"
        assert response["status"] == "succeeded"
        (call,) = module.calls
        assert call["op_id"] == "p-1:ca" and call["scope"] == "all_managed" and call["parent_op_id"] == "p-1"
        assert call["gate"] is Gate.STOPPED  # 先停再平
        assert call["model_version"] == r.kernel.modelstate.current().version  # 停止之后的新版本
        record = await r.kernel.operations.get("p-1")
        assert record.op_type is OperationType.STOP_AND_CLOSE_ALL


async def test_stop_with_close_all_is_not_implemented_when_the_bulk_module_is_missing(tmp_path):
    async with rig(tmp_path) as r:
        svc = await running(r, close_all_loader=lambda: None)
        with pytest.raises(OpRejected) as caught:
            await go_stop(svc, caller=CLI, close_all=True)
        assert (caught.value.status, caught.value.code) == (501, "not_implemented")
        assert r.kernel.modelstate.current().gate is Gate.RUNNING  # 什么也没停
        assert await r.kernel.operations.get("p-1") is None


async def test_the_default_close_all_loader_reports_not_implemented_until_the_module_exists(tmp_path):
    async with rig(tmp_path) as r:
        import importlib.util

        have = importlib.util.find_spec("dmr_kernel.ops.close_all") is not None
        svc = await running(r)
        if have:
            pytest.skip("M2B 的 close_all 已就绪：默认加载器会真的导入它")
        with pytest.raises(OpRejected) as caught:
            await go_stop(svc, caller=CLI, close_all=True)
        assert caught.value.code == "not_implemented"
