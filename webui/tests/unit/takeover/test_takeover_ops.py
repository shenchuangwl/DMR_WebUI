"""接管类操作的统一步骤与拒绝（计划 M3 第 3 节）：报文、版本、操作号去重、各入口的前置条件、混合仓取消托管。"""
from __future__ import annotations

from decimal import Decimal

from takeover_kit import LONG, m2_rig

from dmr_kernel.core.intents import IntentSpec, key_op
from dmr_kernel.core.types import (
    IntentAction,
    IntentSource,
    IntentState,
    Ownership,
    PendingReason,
    PositionStatus,
    ProtectionState,
)

D = Decimal
SYM = "BTCUSDT"


async def _pending(rig):
    await rig.manual_open("ETHUSDT", "0.05")  # 清单外 → 待确认
    rec = rig.pos("ETHUSDT")
    assert rec.status is PositionStatus.PENDING
    return rec


async def _taken(rig):
    await rig.manual_open(SYM, "0.002")
    rec = rig.pos()
    assert rec.ownership is Ownership.TAKEOVER
    return rec


async def test_response_shape_and_integer_ids(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _pending(rig)
        body = (await rig.takeover_op("ignore", rec.id)).json()
        assert set(body) == {"op_id", "status", "result"} and body["status"] == "succeeded"
        result = body["result"]
        assert result["position_id"] == rec.id and isinstance(result["position_id"], int)
        assert result["status"] == "ignored" and result["ownership"] == "none" and result["protection_state"] == "none"


async def test_string_ids_are_rejected_as_a_bad_request(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _pending(rig)
        response = await rig.post("/ops/ignore", {"op_id": "x1", "position_id": "abc", "position_version": rec.version})
        assert response.status_code == 422


async def test_stale_version_is_refused_and_does_not_burn_the_op_id(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _pending(rig)
        stale = await rig.post("/ops/ignore", {"op_id": "same", "position_id": rec.id, "position_version": rec.version + 5})
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        retry = await rig.post("/ops/ignore", {"op_id": "same", "position_id": rec.id, "position_version": rec.version})
        assert retry.status_code == 200


async def test_the_same_op_id_returns_the_first_result_without_acting_twice(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _pending(rig)
        body = {"op_id": "dup-1", "position_id": rec.id, "position_version": rec.version}
        first = await rig.post("/ops/ignore", body)
        second = await rig.post("/ops/ignore", body)  # 版本已变，但重复提交先于一切校验
        assert first.status_code == second.status_code == 200 and first.json() == second.json()
        reused = await rig.post("/ops/takeover", body)  # 同一个操作号用于另一种操作
        assert reused.status_code == 422


async def test_unknown_position_is_404(tmp_path):
    async with m2_rig(tmp_path) as rig:
        response = await rig.post("/ops/ignore", {"op_id": "x", "position_id": 99999, "position_version": 1})
        assert response.status_code == 404


async def test_ignore_is_refused_for_managed_positions_and_idempotent_for_ignored_ones(tmp_path):
    async with m2_rig(tmp_path) as rig:
        managed = await _taken(rig)
        refused = await rig.takeover_op("ignore", managed.id)
        assert refused.status_code == 409 and "取消托管" in refused.json()["message"]
        rec = await _pending(rig)
        assert (await rig.takeover_op("ignore", rec.id)).status_code == 200
        assert (await rig.takeover_op("ignore", rec.id)).status_code == 200  # 已忽略再忽略：无害


async def test_takeover_is_refused_for_non_pending_positions(tmp_path):
    async with m2_rig(tmp_path) as rig:
        managed = await _taken(rig)
        response = await rig.takeover_op("takeover", managed.id)
        assert response.status_code == 409 and response.json()["code"] == "not_allowed"


async def test_takeover_of_a_listed_pending_position_fills_the_standard_result(tmp_path):
    async with m2_rig(tmp_path) as rig:
        # 清单外 → 待确认；把它加进清单（只影响之后新出现的仓）后手动接管
        rec = await _pending(rig)
        response = await rig.takeover_op("takeover", rec.id)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["ownership"] == "takeover" and result["protection_state"] == "system"
        assert result["status"] == "managed"


async def test_restore_is_only_for_origin_to_verify(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _pending(rig)
        response = await rig.takeover_op("restore", rec.id)
        assert response.status_code == 409 and "来源待核对" in response.json()["message"]


async def test_restore_without_an_opening_order_falls_back_to_a_manual_takeover(tmp_path):
    from tests.support.kernel import FakeRecovery

    async with m2_rig(tmp_path, start=False, recovery_factory=FakeRecovery()) as rig:
        rig.engine_order(SYM, "BUY", "LONG", "0.002", "web_x")
        rec = await rig.kernel.registry.register_pending(
            symbol=SYM, side=LONG, qty=D("0.002"), avg_price=D("50000"), reason=PendingReason.ORIGIN_TO_VERIFY)
        response = await rig.takeover_op("restore", rec.id)  # 模型 2 未运行也可用
        assert response.status_code == 200, response.text
        assert response.json()["result"]["restored_as"] == "takeover"
        await rig.settle()
        assert rig.pos().ownership is Ownership.TAKEOVER and rig.pos().open_time_estimated is True


async def test_cancel_takeover_validates_mode_and_state(tmp_path):
    async with m2_rig(tmp_path) as rig:
        managed = await _taken(rig)
        bad = await rig.takeover_op("cancel_takeover", managed.id, mode="whatever")
        assert bad.status_code == 422
        pending = await _pending(rig)
        response = await rig.takeover_op("cancel_takeover", pending.id)
        assert response.status_code == 409 and response.json()["code"] == "not_allowed"


async def test_cancel_takeover_of_a_mixed_position_moves_it_to_external(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        op = "seed-mixed"
        spec = IntentSpec(action=IntentAction.OPEN, model="m1", source=IntentSource.TEST_ENTRY, symbol=SYM, side=LONG,
                          idem_key=key_op(op, SYM, LONG), params={"notional": "100"}, reason="seed", operator="t", op_id=op)
        sub = await rig.kernel.coordinator.submit(spec)
        assert (await rig.kernel.coordinator.wait_terminal(sub.intent_id, 5)).state is IntentState.COMPLETED
        await rig.settle()
        await rig.manual_open(SYM, "0.001")  # 外部加仓 → 混合仓
        mixed = rig.pos()
        assert mixed.is_mixed and mixed.ownership is Ownership.STRATEGY
        response = await rig.takeover_op("cancel_takeover", mixed.id, mode="handover")
        assert response.status_code == 200, response.text
        await rig.settle()
        rec = rig.pos()
        assert rec.status is PositionStatus.IGNORED and rec.ownership is Ownership.NONE and not rec.is_mixed
        assert rec.handed_over_stop is not None and len(await rig.algos()) == 1


async def test_cancel_takeover_with_cancel_stop_fails_cleanly_when_the_cancel_does_not_succeed(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _taken(rig)
        # 撤系统止损没成功：状态不改，系统止损仍在
        real = rig.kernel.protection._cancel_stop

        async def failing(*args, **kwargs):
            return False

        rig.kernel.protection._cancel_stop = failing
        try:
            response = await rig.takeover_op("cancel_takeover", rec.id, mode="cancel_stop")
        finally:
            rig.kernel.protection._cancel_stop = real
        assert response.status_code == 503 and response.json()["code"] == "unavailable"
        now = rig.pos()
        assert now.status is PositionStatus.MANAGED and now.ownership is Ownership.TAKEOVER
        assert len(await rig.algos()) == 1


async def test_hand_stop_is_refused_for_system_protected_and_pending_positions(tmp_path):
    async with m2_rig(tmp_path) as rig:
        managed = await _taken(rig)
        assert managed.protection_state is ProtectionState.SYSTEM
        assert (await rig.takeover_op("hand_stop_to_system", managed.id)).status_code == 409
        pending = await _pending(rig)
        assert (await rig.takeover_op("hand_stop_to_system", pending.id)).status_code == 409


async def test_endpoints_answer_501_when_the_takeover_ops_are_not_wired(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rig.kernel.app.state.takeover_ops = None
        for path in ("/ops/takeover", "/ops/ignore", "/ops/restore", "/ops/cancel_takeover", "/ops/hand_stop_to_system"):
            response = await rig.post(path, {"op_id": "x"})  # 不看请求体
            assert response.status_code == 501 and response.json()["code"] == "not_implemented", path
