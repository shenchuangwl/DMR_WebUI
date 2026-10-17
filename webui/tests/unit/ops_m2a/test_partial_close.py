"""ops.partial_close：部分平仓的预览与确认（F4）。

覆盖：预览的取整与返回字段；取整为 0、剩余太小、平仓单自身太小（实测第 2 项「不豁免」与「豁免」）都拒绝并提示改用「平掉此仓」；
读数未知不当零；确认的全部复核（版本、即时持仓与预览相差不超过一个步长——含边界、键上有别的动作在途、剩余名义金额按当刻标记价重算）；
预览后仓位被外部减掉要求重新预览；提交的 PARTIAL_CLOSE 意图的内容；同一操作号只执行一次；并发确认只有一个生效；
席位与允许表；超时不重发、结果「未知」；中断点 4（partial_close_sent）重启后不重发。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from dmr_kernel.core.crashpoints import CrashRegistry, CrashSignal
from dmr_kernel.core.types import (
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    OperationStatus,
    OperationType,
    PositionSide,
    PositionStatus,
    ProtectionState,
    Suppression,
    Unknown,
)
from dmr_kernel.ops.common import Caller, OpRejected
from dmr_kernel.ops.partial_close import PartialClose
from tests.support.kernel import FakeRecovery, abandon, make_kernel, settle
from tests.unit.ops_m2a.m2a_kit import CoordinatorProxy, Rig, StubAdapter, make_deps, rig

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
SESSION = Caller("alice", 7, True, True, None)
NOSEAT = Caller("bob", 8, True, False, None)
CLI = Caller.cli()
PREVIEW_KEYS = {"position_id", "position_version", "live_qty", "close_qty", "remaining_qty", "remaining_notional",
                "min_notional", "mark_price", "warnings"}


async def seeded(r, notional="400", side=LONG, **deps_kw):
    """开一个 0.008 BTC 的仓（名义 400，BTCUSDT 步长 0.001、最小名义金额 50、价格 50000）。
    需要压低价格的用例开空仓：替身对多仓会在价格大跌时强平。"""
    position = await r.open_position(notional=notional, side=side)
    return position, PartialClose(make_deps(r.kernel, **deps_kw))


async def preview_then(svc, position, op_id="pc-1", ratio="0.5", caller=SESSION):
    pv = await svc.preview(caller, position_id=position.id, ratio=D(ratio))
    return pv, dict(op_id=op_id, position_id=position.id, position_version=pv["position_version"],
                    preview_qty=D(pv["live_qty"]), close_qty=D(pv["close_qty"]))


async def reject(coro, status=None, code=None):
    with pytest.raises(OpRejected) as caught:
        await coro
    if status is not None:
        assert caught.value.status == status
    if code is not None:
        assert caught.value.code == code
    return caught.value


# ---------------------------------------------------------------- 预览


async def test_preview_rounds_down_to_the_step_and_reports_the_remainder(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv = await svc.preview(SESSION, position_id=position.id, ratio=D("0.5"))
        assert set(pv) == PREVIEW_KEYS
        assert (pv["position_id"], pv["position_version"]) == (position.id, position.version)
        assert [D(pv[k]) for k in ("live_qty", "close_qty", "remaining_qty")] == [D("0.008"), D("0.004"), D("0.004")]
        assert D(pv["remaining_notional"]) == D("200") and D(pv["mark_price"]) == D("50000")
        assert D(pv["min_notional"]) == D("50") and pv["warnings"] == []


@pytest.mark.parametrize("ratio,close", [("0.25", "0.002"), ("0.75", "0.006"), ("0.3333", "0.002")])
async def test_preview_ratios_round_down_never_up(tmp_path, ratio, close):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv = await svc.preview(SESSION, position_id=position.id, ratio=D(ratio))
        assert D(pv["close_qty"]) == D(close)


async def test_preview_accepts_a_quantity_instead_of_a_ratio_and_caps_it_at_the_live_position(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv = await svc.preview(SESSION, position_id=position.id, qty=D("0.0035"))
        assert D(pv["close_qty"]) == D("0.003")
        error = await reject(svc.preview(SESSION, position_id=position.id, qty=D("5")), 409, "not_allowed")
        assert "平掉此仓" in error.message  # 超过持仓按持仓算，等于全平：改用平掉此仓


async def test_preview_rounding_to_zero_is_refused_with_the_close_one_hint(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r, notional="150")  # 0.003
        error = await reject(svc.preview(SESSION, position_id=position.id, ratio=D("0.1")), 409, "not_allowed")
        assert "平掉此仓" in error.message


async def test_preview_full_ratio_leaves_nothing_and_is_refused(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        error = await reject(svc.preview(SESSION, position_id=position.id, ratio=D("1")), 409, "not_allowed")
        assert "平掉此仓" in error.message


async def test_preview_refuses_when_the_remainder_would_be_below_the_minimum_notional(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r, side=SHORT)
        await r.manual("/dev/price_override", {"symbol": "BTCUSDT", "price": "10000"})
        # 平 0.007（70）剩 0.001（10 < 50）
        error = await reject(svc.preview(SESSION, position_id=position.id, ratio=D("0.875")), 409, "not_allowed")
        assert "剩余" in error.message and "平掉此仓" in error.message


async def test_preview_refuses_a_close_order_below_the_minimum_notional_unless_exempt(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r, side=SHORT)
        await r.manual("/dev/price_override", {"symbol": "BTCUSDT", "price": "10000"})
        error = await reject(svc.preview(SESSION, position_id=position.id, qty=D("0.001")), 409, "not_allowed")
        assert "平仓单" in error.message and "最小名义金额" in error.message
    (tmp_path / "exempt").mkdir()
    async with rig(tmp_path / "exempt", config_overrides={"ex_close_min_notional_exempt": True}) as r:
        position, svc = await seeded(r, side=SHORT)
        await r.manual("/dev/price_override", {"symbol": "BTCUSDT", "price": "10000"})
        pv = await svc.preview(SESSION, position_id=position.id, qty=D("0.001"))  # 豁免：只要剩余够
        assert D(pv["close_qty"]) == D("0.001")


@pytest.mark.parametrize("kwargs", [
    {}, {"ratio": D("0.5"), "qty": D("0.001")}, {"ratio": D("0")}, {"ratio": D("-0.1")}, {"ratio": D("1.5")},
    {"qty": D("0")}, {"qty": D("-1")}, {"ratio": "abc"}, {"qty": "NaN"}, {"ratio": D("NaN")}])
async def test_preview_rejects_malformed_requests(tmp_path, kwargs):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        await reject(svc.preview(SESSION, position_id=position.id, **kwargs), 422, "invalid")


async def test_preview_with_an_unknown_live_position_is_refused_not_treated_as_zero(tmp_path):
    async with rig(tmp_path) as r:
        async def unknown(symbol, side):
            return Unknown("超时")

        position, svc = await seeded(r, coordinator=CoordinatorProxy(r.kernel.coordinator, live_position=unknown))
        error = await reject(svc.preview(SESSION, position_id=position.id, ratio=D("0.5")), 503, "unavailable")
        assert "未知" in error.message or "读不到" in error.message


async def test_preview_with_an_unknown_mark_price_is_refused(tmp_path):
    async with rig(tmp_path) as r:
        stub = StubAdapter(r.kernel.adapter)
        stub.set("fetch_tickers", Unknown("超时"))
        position, svc = await seeded(r, adapter=stub)
        await reject(svc.preview(SESSION, position_id=position.id, ratio=D("0.5")), 503, "unavailable")


async def test_preview_when_the_exchange_says_the_position_is_already_gone(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        await r.manual_reduce("BTCUSDT", LONG, "0.008")
        error = await reject(svc.preview(SESSION, position_id=position.id, ratio=D("0.5")), 409, "not_allowed")
        assert "其它途径平掉" in error.message or "为零" in error.message


async def test_preview_requires_a_managed_position(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        await reject(svc.preview(SESSION, position_id=9999, ratio=D("0.5")), 404, "not_found")
        await r.kernel.registry.set_closing(position.id, True, cause="test")
        error = await reject(svc.preview(SESSION, position_id=position.id, ratio=D("0.5")), 409, "not_allowed")
        assert "平仓" in error.message


async def test_preview_needs_the_seat_and_follows_the_status_table(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        await reject(svc.preview(NOSEAT, position_id=position.id, ratio=D("0.5")), 409, "seat_required")
        pv = await svc.preview(CLI, position_id=position.id, ratio=D("0.5"))  # 本机令牌不占席位
        assert D(pv["close_qty"]) == D("0.004")
        await r.kernel.modelstate.add_suppression(Suppression.RECOVERING, reason="t")
        error = await reject(svc.preview(SESSION, position_id=position.id, ratio=D("0.5")), 409, "not_allowed")
        assert "恢复中" in error.message


async def test_preview_warns_when_the_protection_is_external(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        await r.kernel.registry.set_protection(position.id, state=ProtectionState.EXTERNAL)
        pv = await svc.preview(SESSION, position_id=position.id, ratio=D("0.5"))
        assert pv["warnings"] and "止损" in pv["warnings"][0]


async def test_a_preview_changes_nothing(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        sent = len(r.standin.transport.sent("POST", "/api/private/order"))
        await svc.preview(SESSION, position_id=position.id, ratio=D("0.5"))
        assert len(r.standin.transport.sent("POST", "/api/private/order")) == sent
        assert r.kernel.registry.by_id(position.id).version == position.version
        assert await r.kernel.operations.get("pc-1") is None


# ---------------------------------------------------------------- 确认：成功路径


async def test_confirm_submits_a_partial_close_intent_and_reports_the_fill(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        response = await svc.confirm(SESSION, **body)
        assert response["op_id"] == "pc-1" and response["status"] == "succeeded"
        result = response["result"]
        assert set(result) >= {"intent_id", "state", "filled_qty", "outcome"}
        assert result["state"] == "COMPLETED" and result["outcome"] == "closed" and D(result["filled_qty"]) == D("0.004")
        assert await r.exchange_qty() == D("0.004")
        intent = await r.kernel.intents.get(result["intent_id"])
        assert intent.action is IntentAction.PARTIAL_CLOSE and intent.source is IntentSource.OPERATOR
        assert intent.qty == D("0.004") and intent.symbol == "BTCUSDT" and intent.side is LONG
        assert intent.operator == "alice" and intent.op_id == "pc-1" and intent.position_id == position.id
        assert intent.position_generation == position.generation
        record = await r.kernel.operations.get("pc-1")
        assert record.op_type is OperationType.PARTIAL_CLOSE and record.status is OperationStatus.SUCCEEDED
        assert record.state_version == position.version and record.operator == "alice"
        # 成交回报进来之后登记簿的数量跟着变
        await r.kernel.jobs["backfill_trades"]()
        await settle(r.kernel)
        assert r.kernel.registry.get_active("BTCUSDT", LONG).qty == D("0.004")


async def test_the_local_token_confirms_without_a_seat(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position, caller=CLI)
        response = await svc.confirm(CLI, **body)
        assert response["result"]["outcome"] == "closed"
        assert (await r.kernel.operations.get("pc-1")).operator == "cli"


# ---------------------------------------------------------------- 确认：复核（F4 第 3 步）


async def test_confirm_with_a_stale_position_version_is_refused_and_nothing_is_sent(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        await r.kernel.registry.set_protection(position.id, state=ProtectionState.SYSTEM, stop_price=D("49000"))
        error = await reject(svc.confirm(SESSION, **body), 409, "stale_version")
        assert "预览" in error.message
        assert await r.exchange_qty() == D("0.008")
        assert await r.kernel.operations.get("pc-1") is None  # 被拒绝的请求没有占用操作号


async def test_a_position_reduced_externally_after_the_preview_voids_the_confirmation(tmp_path):
    """预览后仓位被外部减掉：执行作废并要求重新预览（相差两个步长）。"""
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        await r.manual_reduce("BTCUSDT", LONG, "0.002")
        error = await reject(svc.confirm(SESSION, **body), 409, "stale_version")
        assert "重新预览" in error.message
        assert await r.exchange_qty() == D("0.006")  # 我们一笔也没发
        assert len(r.standin.transport.sent("POST", "/api/private/order")) == 1  # 只有种子开仓；手动减仓不经内核的传输
        again = await svc.preview(SESSION, position_id=position.id, ratio=D("0.5"))
        assert D(again["live_qty"]) == D("0.006") and D(again["close_qty"]) == D("0.003")


async def test_a_difference_of_exactly_one_step_is_tolerated_but_two_steps_are_not(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        await r.manual_reduce("BTCUSDT", LONG, "0.001")  # 正好一个步长
        response = await svc.confirm(SESSION, **body)
        assert response["result"]["outcome"] == "closed"
        assert await r.exchange_qty() == D("0.003")  # 0.007 - 0.004


async def test_a_position_added_to_externally_beyond_one_step_also_voids_it(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        await r.manual("/dev/manual/order", {"symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
                                             "qty": "0.003"})
        await reject(svc.confirm(SESSION, **body), 409, "stale_version")
        assert await r.exchange_qty() == D("0.011")


async def test_confirm_is_refused_while_another_reducing_or_protective_action_is_in_flight(tmp_path):
    async with rig(tmp_path) as r:
        proxy = CoordinatorProxy(r.kernel.coordinator, key_busy=lambda symbol, side: True)
        position, svc = await seeded(r, coordinator=proxy)
        pv, body = await preview_then(svc, position)
        error = await reject(svc.confirm(SESSION, **body), 409, "not_allowed")
        assert "在途" in error.message
        assert await r.exchange_qty() == D("0.008")


async def test_the_remainder_is_rechecked_at_the_current_mark_price(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r, side=SHORT)
        pv, body = await preview_then(svc, position, ratio="0.75")  # 平 0.006 剩 0.002（100）
        await r.manual("/dev/price_override", {"symbol": "BTCUSDT", "price": "20000"})  # 剩余 40 < 50
        error = await reject(svc.confirm(SESSION, **body), 409, "not_allowed")
        assert "剩余" in error.message and "平掉此仓" in error.message
        assert await r.exchange_qty(side=SHORT) == D("0.008")


async def test_the_close_order_itself_is_rechecked_when_the_exemption_is_off(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r, side=SHORT)
        pv, body = await preview_then(svc, position, ratio="0.125")  # 平 0.001（50），剩 0.007
        await r.manual("/dev/price_override", {"symbol": "BTCUSDT", "price": "30000"})  # 平仓单 30 < 50
        error = await reject(svc.confirm(SESSION, **body), 409, "not_allowed")
        assert "平仓单" in error.message


async def test_confirm_with_an_unknown_live_position_is_refused(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        svc2 = PartialClose(make_deps(r.kernel, coordinator=CoordinatorProxy(
            r.kernel.coordinator, live_position=lambda s, d: _unknown())))
        await reject(svc2.confirm(SESSION, **body), 503, "unavailable")
        assert await r.exchange_qty() == D("0.008")


async def _unknown():
    return Unknown("超时")


@pytest.mark.parametrize("close_qty", ["0", "-0.001", "0.0015", "0.008", "0.009"])
async def test_confirm_rejects_nonsensical_close_quantities(tmp_path, close_qty):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        body["close_qty"] = D(close_qty)
        await reject(svc.confirm(SESSION, **body), None, None)  # invalid 或 not_allowed（等于全平）都是拒绝
        assert await r.exchange_qty() == D("0.008")


async def test_confirm_with_a_missing_or_closing_position_is_refused(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        await reject(svc.confirm(SESSION, **{**body, "position_id": 9999}), 404, "not_found")
        closing = await r.kernel.registry.set_closing(position.id, True, cause="test")
        error = await reject(svc.confirm(SESSION, **{**body, "position_version": closing.version}), 409, "not_allowed")
        assert "平仓" in error.message


async def test_confirm_needs_the_seat_and_the_table_to_allow_it(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        await reject(svc.confirm(NOSEAT, **body), 409, "seat_required")
        await r.kernel.modelstate.add_suppression(Suppression.RECOVERING, reason="t")
        await reject(svc.confirm(SESSION, **body), 409, "not_allowed")
        assert await r.exchange_qty() == D("0.008")


# ---------------------------------------------------------------- 操作号去重与并发


async def test_the_same_operation_id_is_executed_only_once(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        first = await svc.confirm(SESSION, **body)
        again = await svc.confirm(SESSION, **body)  # 位置版本、数量都已变，照样返回第一次的结果
        assert again == first
        assert await r.exchange_qty() == D("0.004")  # 只减了一次
        assert len([o for o in await r.kernel.orders.for_intent(first["result"]["intent_id"])]) == 1


async def test_two_concurrent_confirmations_of_the_same_preview_let_only_one_through(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        pv, body = await preview_then(svc, position)
        outcomes = await asyncio.gather(
            svc.confirm(CLI, **{**body, "op_id": "pc-a"}), svc.confirm(CLI, **{**body, "op_id": "pc-b"}),
            return_exceptions=True)
        wins = [o for o in outcomes if isinstance(o, dict)]
        losses = [o for o in outcomes if isinstance(o, OpRejected)]
        assert len(wins) == 1 and len(losses) == 1
        assert losses[0].code in ("stale_version", "not_allowed")
        assert await r.exchange_qty() == D("0.004")  # 只减了一次


# ---------------------------------------------------------------- 超时与中断点 4


async def test_an_unknown_outcome_after_the_wait_is_reported_as_unknown_and_never_resent(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r, wait_s=0.2)
        r.standin.disp.switches.update({"order_timeout": {"enabled": True, "mode": "accept_then_hang", "scope": "market",
                                                          "count": 1, "hang_s": 30}})
        # 适配层请求超时缩短：让扣住的响应很快被判超时
        r.kernel.adapter._timeout_s = 0.1
        pv, body = await preview_then(svc, position)
        response = await svc.confirm(SESSION, **body)
        assert response["status"] == "unknown" and response["result"]["outcome"] == "unknown"
        assert response["result"]["state"] in ("RECONCILING", "ACTIVE", "DISPATCH_READY")
        record = await r.kernel.operations.get("pc-1")
        assert record.status is OperationStatus.UNKNOWN
        for _ in range(3):  # 回查期间部分平仓永不自动重发
            r.clock.advance(60_000)
            await r.kernel.coordinator.resolve_unknown_orders()
        assert len(r.standin.transport.sent("POST", "/api/private/order")) == 2  # 种子开仓 + 这一次


async def test_a_rejected_partial_close_is_reported_as_failed_and_leaves_the_position_alone(tmp_path):
    async with rig(tmp_path) as r:
        def break_the_reread(spec):
            # 复核都过了、意图提交的那一刻持仓查询坏掉：协调者读不到持仓，操作员发起的部分平仓直接失败
            r.standin.disp.switches.update({"position_query_fail": {"enabled": True, "mode": "error", "count": 1}})
            return r.kernel.coordinator.submit(spec)

        proxy = CoordinatorProxy(r.kernel.coordinator, submit=break_the_reread)
        position, svc = await seeded(r, coordinator=proxy)
        pv, body = await preview_then(svc, position)
        response = await svc.confirm(SESSION, **body)
        assert response["status"] == "failed" and response["result"]["outcome"] == "failed"
        assert response["result"]["state"] == "REJECTED" and response["result"]["reason"]
        assert D(response["result"]["filled_qty"]) == D("0")
        assert (await r.kernel.operations.get("pc-1")).status is OperationStatus.FAILED
        assert await r.exchange_qty() == D("0.008")


async def test_crash_point_4_the_sent_order_is_never_resent_and_the_operation_is_marked_interrupted(tmp_path):
    """确认中途进程被杀（部分平仓单已发出、回执未到）：重启后同一操作号不会再发第二张单，操作记为中断，持仓放回托管中。"""
    kernel, standin = make_kernel(tmp_path, crash=CrashRegistry(live=False), recovery_factory=FakeRecovery())
    await kernel.start(serve_http=False, run_jobs=False)
    r = Rig(kernel, standin, client=None)
    await kernel.jobs["reconcile_positions"]()
    await kernel.jobs["reconcile_orders"]()
    position = await r.open_position(notional="400")
    svc = PartialClose(make_deps(kernel))
    pv, body = await preview_then(svc, position)
    kernel.crash.arm("partial_close_sent")
    with pytest.raises(CrashSignal):
        await svc.confirm(SESSION, **body)
    assert (await kernel.operations.get("pc-1")).status is OperationStatus.RUNNING  # 进程“死”在中途：操作没有结局
    assert await r.exchange_qty() == D("0.004")  # 请求到了交易所并成交
    await abandon(kernel)

    kernel2, _ = make_kernel(tmp_path, standin=standin, recovery_factory=None)  # 真实的重启恢复
    await kernel2.start(serve_http=False, run_jobs=False)
    try:
        await settle(kernel2)
        interrupted = await kernel2.operations.get("pc-1")
        assert interrupted.status is OperationStatus.ABORTED and interrupted.result["interrupted"] is True
        replay = await PartialClose(make_deps(kernel2)).confirm(SESSION, **body)  # 同一操作号：返回第一次的记录，不再发单
        assert replay["op_id"] == "pc-1" and replay["status"] == "aborted"
        await kernel2.coordinator.idle(5)
        assert len(standin.transports[-1].sent("POST", "/api/private/order")) == 0  # 新内核一张单也没发
        recovered = kernel2.registry.get_active("BTCUSDT", LONG)
        assert recovered.status is PositionStatus.MANAGED and recovered.qty == D("0.004")
        assert (await kernel2.coordinator.live_position("BTCUSDT", LONG)).value == D("0.004")
    finally:
        await kernel2.stop()
