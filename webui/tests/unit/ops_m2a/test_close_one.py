"""ops.close_one：平掉此仓（F12）：单次确认、不要口令、校验持仓状态版本、CLOSE 意图（未知后可重发）、策略仓冷却。

覆盖：成功路径与意图内容；冷却（策略仓有、AI 仓与冷却根数为 0 没有、失败没有）；持仓版本过期；持仓不存在、待确认仓与外部仓不归系统；
批量平仓处理中的仓不能点而部分平仓暂停中的仓可以；允许表（恢复中可以、需要先取即时读数）；席位；已由其它途径平掉；
读数未知拒绝；同一操作号只执行一次；碎仓走全平型条件单；超时结果「未知」。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.clock import interval_ms
from dmr_kernel.core.types import (
    IntentAction,
    IntentSource,
    OpLock,
    OperationStatus,
    OperationType,
    PendingReason,
    PositionSide,
    Suppression,
    Unknown,
)
from dmr_kernel.ops.close_one import CloseOne
from dmr_kernel.ops.common import Caller, OpRejected
from tests.unit.ops_m2a.m2a_kit import CoordinatorProxy, make_deps, rig

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
SESSION = Caller("alice", 7, True, True, None)
NOSEAT = Caller("bob", 8, True, False, None)
CLI = Caller.cli()


async def reject(coro, status=None, code=None):
    with pytest.raises(OpRejected) as caught:
        await coro
    if status is not None:
        assert caught.value.status == status
    if code is not None:
        assert caught.value.code == code
    return caught.value


async def seeded(r, model="m1", notional="400", side=LONG, **deps_kw):
    position = await r.open_position(notional=notional, model=model, side=side)
    return position, CloseOne(make_deps(r.kernel, **deps_kw))


def body(position, op_id="c-1"):
    return dict(op_id=op_id, position_id=position.id, position_version=position.version)


async def test_close_one_closes_the_whole_position_with_a_close_intent(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        response = await svc.confirm(SESSION, **body(position))
        assert response["op_id"] == "c-1" and response["status"] == "succeeded"
        result = response["result"]
        assert set(result) >= {"intent_id", "state", "outcome"}
        assert result["state"] == "COMPLETED" and result["outcome"] == "closed"
        assert await r.exchange_qty() == D("0")
        intent = await r.kernel.intents.get(result["intent_id"])
        assert intent.action is IntentAction.CLOSE and intent.source is IntentSource.OPERATOR
        assert intent.operator == "alice" and intent.op_id == "c-1" and intent.position_id == position.id
        assert intent.position_generation == position.generation
        assert intent.params.get("resend_on_unknown") is True
        assert intent.reason.startswith("manual")  # 出场原因记「手动」
        record = await r.kernel.operations.get("c-1")
        assert record.op_type is OperationType.CLOSE_ONE and record.status is OperationStatus.SUCCEEDED
        assert record.state_version == position.version and record.operator == "alice"


async def test_no_password_is_needed_but_the_seat_is(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        await reject(svc.confirm(NOSEAT, **body(position)), 409, "seat_required")
        assert await r.exchange_qty() == D("0.008")
        response = await svc.confirm(CLI, **body(position))
        assert response["result"]["outcome"] == "closed" and (await r.kernel.operations.get("c-1")).operator == "cli"


async def test_closing_a_strategy_position_by_hand_cools_the_symbol_down(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        assert r.kernel.registry.cooldown_until("BTCUSDT") is None
        await svc.confirm(SESSION, **body(position))
        bars = int(r.kernel.config.get("cooldown_bars"))
        expected = r.clock.now_ms() + bars * interval_ms(str(r.kernel.config.get("timeframe_short")))
        assert r.kernel.registry.cooldown_until("BTCUSDT") == expected
        assert r.kernel.registry.cooldown_until("ETHUSDT") is None


async def test_no_cooldown_for_an_ai_position_or_when_cooldown_bars_is_zero(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r, model="m3")
        await svc.confirm(SESSION, **body(position))
        assert r.kernel.registry.cooldown_until("BTCUSDT") is None
    (tmp_path / "zero").mkdir()
    async with rig(tmp_path / "zero", config_overrides={"cooldown_bars": 0}) as r:
        position, svc = await seeded(r)
        await svc.confirm(SESSION, **body(position))
        assert r.kernel.registry.cooldown_until("BTCUSDT") is None


async def test_no_cooldown_when_the_close_failed(tmp_path):
    async with rig(tmp_path) as r:
        def break_the_reread(spec):
            r.standin.disp.switches.update({"position_query_fail": {"enabled": True, "mode": "error", "count": 1}})
            return r.kernel.coordinator.submit(spec)

        position, svc = await seeded(r, coordinator=CoordinatorProxy(r.kernel.coordinator, submit=break_the_reread))
        response = await svc.confirm(SESSION, **body(position))
        assert response["status"] == "failed" and response["result"]["outcome"] == "failed"
        assert response["result"]["reason"] and await r.exchange_qty() == D("0.008")
        assert r.kernel.registry.cooldown_until("BTCUSDT") is None


async def test_stale_position_version_is_refused_and_nothing_is_sent(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        await r.kernel.registry.set_closing(position.id, True, cause="test")
        await r.kernel.registry.set_closing(position.id, False, cause="test")
        await reject(svc.confirm(SESSION, **body(position)), 409, "stale_version")
        assert await r.exchange_qty() == D("0.008") and await r.kernel.operations.get("c-1") is None


async def test_unknown_positions_and_positions_the_system_does_not_own_are_refused(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        await reject(svc.confirm(SESSION, op_id="c-1", position_id=9999, position_version=1), 404, "not_found")
        pending = await r.kernel.registry.register_pending(symbol="ETHUSDT", side=LONG, qty=D("1"),
                                                           avg_price=D("3000"), reason=PendingReason.OUT_OF_SCOPE)
        error = await reject(svc.confirm(SESSION, **body(pending, "c-2")), 409, "not_allowed")
        assert "待确认" in error.message or "外部" in error.message
        ignored = await r.kernel.registry.ignore(pending.id, expected_version=pending.version)
        await reject(svc.confirm(SESSION, **body(ignored, "c-3")), 409, "not_allowed")


async def test_positions_being_handled_by_a_bulk_close_cannot_be_clicked_but_a_partial_close_pause_can(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        closing = await r.kernel.registry.set_closing(position.id, True, cause="test")  # 部分平仓暂停中
        response = await svc.confirm(SESSION, **body(closing))
        assert response["result"]["outcome"] == "closed"
    (tmp_path / "bulk").mkdir()
    async with rig(tmp_path / "bulk") as r:
        position, svc = await seeded(r)
        closing = await r.kernel.registry.set_closing(position.id, True, cause="test")
        await r.kernel.modelstate.acquire_op_lock(OpLock.BULK_CLOSING, owner="x")
        error = await reject(svc.confirm(SESSION, **body(closing)), 409, "not_allowed")
        assert "批量平仓" in error.message
        assert await r.exchange_qty() == D("0.008")


async def test_close_one_is_allowed_during_a_suppression_but_needs_a_live_read(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        await r.kernel.modelstate.add_suppression(Suppression.RECOVERING, reason="t")
        response = await svc.confirm(SESSION, **body(position))
        assert response["result"]["outcome"] == "closed"
    (tmp_path / "unknown").mkdir()
    async with rig(tmp_path / "unknown") as r:
        async def unknown(symbol, side):
            return Unknown("超时")

        position, svc = await seeded(r, coordinator=CoordinatorProxy(r.kernel.coordinator, live_position=unknown))
        await r.kernel.modelstate.add_suppression(Suppression.RECOVERING, reason="t")
        await reject(svc.confirm(SESSION, **body(position)), 503, "unavailable")
        assert await r.exchange_qty() == D("0.008")


async def test_a_position_already_closed_on_the_exchange_is_reported_as_closed_externally(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        await r.manual_reduce("BTCUSDT", LONG, "0.008")
        response = await svc.confirm(SESSION, **body(position))
        assert response["status"] == "succeeded" and response["result"]["outcome"] == "closed_externally"
        assert len(r.standin.transport.sent("POST", "/api/private/order")) == 1  # 没有再发平仓单


async def test_the_same_operation_id_closes_only_once(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r)
        first = await svc.confirm(SESSION, **body(position))
        again = await svc.confirm(SESSION, **body(position))
        assert again == first
        assert len(r.standin.transport.sent("POST", "/api/private/order")) == 2  # 种子开仓 + 一次平仓


async def test_a_dust_position_is_closed_with_a_close_position_conditional_order(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r, notional="55", side=SHORT, wait_s=0.2)  # 0.001 BTC 空仓（替身对多仓大跌会强平）
        await r.manual("/dev/price_override", {"symbol": "BTCUSDT", "price": "40000"})  # 名义 40 < 最小名义金额 50
        response = await svc.confirm(SESSION, **body(position))
        algos = [req for req in r.standin.transport.sent("POST", "/api/private/algo_order")
                 if req[3] and req[3].get("type") == "TAKE_PROFIT_MARKET"]
        assert len(algos) == 1 and algos[0][3]["close_position"] is True
        assert response["result"]["outcome"] in ("closed", "unknown")


async def test_a_close_that_does_not_finish_in_time_is_reported_as_unknown(tmp_path):
    async with rig(tmp_path) as r:
        position, svc = await seeded(r, wait_s=0.2)
        r.standin.disp.switches.update({"order_timeout": {"enabled": True, "mode": "drop", "scope": "market",
                                                          "count": 1, "hang_s": 30}})
        r.kernel.adapter._timeout_s = 0.1
        response = await svc.confirm(SESSION, **body(position))
        assert response["status"] == "unknown" and response["result"]["outcome"] == "unknown"
        assert (await r.kernel.operations.get("c-1")).status is OperationStatus.UNKNOWN
        assert r.kernel.registry.cooldown_until("BTCUSDT") is None  # 结果未知不冷却
