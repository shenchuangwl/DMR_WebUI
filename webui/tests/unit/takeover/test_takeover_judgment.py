"""模型 2 的识别与归类（F3 第 2–4 步）：排除、原因优先级、延后重判、提醒、启动快照。完整内核 + 替身（takeover_kit）。"""
from __future__ import annotations

from decimal import Decimal

from takeover_kit import LONG, SHORT, m2_rig

from dmr_kernel.core.events import UnregisteredPositionFound
from dmr_kernel.core.types import Model, Ownership, PendingReason, PositionStatus, ProtectionState
from tests.support.kernel import settle

D = Decimal
SYM = "BTCUSDT"


async def _alerts(rig, kind="position_pending"):
    await rig.kernel.notifier.flush()
    body = (await rig.get("/alerts?limit=200")).json()
    return [a for a in body["items"] if a["kind"] == kind]


async def test_starting_model_2_records_the_start_time_and_the_gate_opens_only_after_the_check(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        assert rig.kernel.modelstate.current().m2_started_ms is None
        rig.advance(5_000)
        await rig.start_model()
        assert rig.kernel.modelstate.current().m2_started_ms == rig.clock.now_ms()


async def test_a_listed_position_inside_the_range_is_taken_over_with_the_exchange_average_price(tmp_path):
    async with m2_rig(tmp_path, config={"takeover_signal_exit": True, "takeover_min_notional": "50"}) as rig:
        await rig.manual_open(SYM, "0.001")  # 名义 50 USDT：恰好等于下限，算在范围内
        rec = rig.pos()
        assert rec.ownership is Ownership.TAKEOVER and rec.signal_exit is True
        assert rec.avg_price == D("50000")


async def test_a_position_above_the_upper_limit_is_pending_with_an_alert(tmp_path):
    async with m2_rig(tmp_path, config={"takeover_max_notional": "100"}) as rig:
        await rig.manual_open(SYM, "0.003")  # 150 > 100
        rec = rig.pos()
        assert rec.status is PositionStatus.PENDING and rec.pending_reason is PendingReason.NOTIONAL_OUT_OF_RANGE
        assert any(SYM in a["message"] and a["level"] == "warning" for a in await _alerts(rig))


async def test_short_positions_are_taken_over_too(tmp_path):
    async with m2_rig(tmp_path) as rig:
        await rig.manual_open(SYM, "0.002", SHORT)
        rec = rig.pos(SYM, SHORT)
        assert rec.ownership is Ownership.TAKEOVER and rec.protection_state is ProtectionState.SYSTEM


async def test_reason_priority_model_not_active_comes_before_out_of_scope(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        await rig.manual_open("ETHUSDT", "0.05")  # 既不在清单、模型 2 也没运行
        assert rig.pos("ETHUSDT").pending_reason is PendingReason.MODEL_NOT_ACTIVE


async def test_pending_positions_waiting_on_model_2_become_preexisting_when_it_starts(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        await rig.manual_open(SYM, "0.002")
        assert rig.pos().pending_reason is PendingReason.MODEL_NOT_ACTIVE
        rig.advance(1_000)
        await rig.start_model()
        rec = rig.pos()
        assert rec.status is PositionStatus.PENDING and rec.pending_reason is PendingReason.PREEXISTING  # 启动之前开的


async def test_a_lock_released_later_lets_a_pending_position_opened_after_the_start_be_taken_over(tmp_path):
    from dmr_kernel.core.types import OpLock

    async with m2_rig(tmp_path) as rig:
        await rig.kernel.modelstate.acquire_op_lock(OpLock.SWITCH_PREVIEW, owner="t")
        await rig.manual_open(SYM, "0.002")
        assert rig.pos().pending_reason is PendingReason.MODEL_NOT_ACTIVE
        await rig.kernel.modelstate.release_op_lock(OpLock.SWITCH_PREVIEW, owner="t")
        await rig.settle()
        rec = rig.pos()
        assert rec.ownership is Ownership.TAKEOVER and rec.status is PositionStatus.MANAGED


async def test_a_key_with_a_system_intent_in_flight_is_not_judged_until_it_is_done(tmp_path):
    async with m2_rig(tmp_path) as rig:
        busy = {"on": True}
        real = rig.kernel.coordinator.key_busy
        rig.kernel.coordinator.key_busy = lambda s, side: busy["on"] or real(s, side)
        await rig.manual_open(SYM, "0.002")
        assert rig.pos() is None  # 自己的意图在途：先不判，不能当成外部成交
        busy["on"] = False
        await rig.kernel.takeover.tick()
        await rig.settle()
        assert rig.pos().ownership is Ownership.TAKEOVER


async def test_a_position_found_by_reconcile_without_any_fill_is_unknown_origin(tmp_path):
    from tests.support.kernel import FakeRecovery  # 假恢复：不起数据流，成交不会自己进账本

    async with m2_rig(tmp_path, recovery_factory=FakeRecovery()) as rig:
        rig.engine_order(SYM, "BUY", "LONG", "0.002", "web_nofill")
        await rig.kernel.jobs["reconcile_positions"]()
        await rig.settle()
        rec = rig.pos()
        assert rec.status is PositionStatus.PENDING and rec.pending_reason is PendingReason.UNKNOWN_ORIGIN
        # 手动接管：取不到开仓时间 → 用当刻并标注为估计，初始止损照常当刻挂上
        taken = await rig.takeover_op("takeover", rec.id)
        assert taken.status_code == 200, taken.text
        await rig.settle()
        rec = rig.pos()
        assert rec.open_time_estimated is True and rec.open_time_ms == rig.clock.now_ms() == rec.anchor_ms
        assert rec.protection_state is ProtectionState.SYSTEM and len(await rig.algos()) == 1


async def test_a_pending_position_is_reminded_again_after_pending_remind_min(tmp_path):
    async with m2_rig(tmp_path, config={"pending_remind_min": 1}) as rig:
        await rig.manual_open("ETHUSDT", "0.05")
        first = len(await _alerts(rig))
        assert first >= 1
        rig.advance(30_000)
        await rig.kernel.takeover.tick()
        await rig.settle()
        assert len(await _alerts(rig)) == first  # 还不到 1 分钟
        rig.advance(31_000)
        await rig.kernel.takeover.tick()
        await rig.settle()
        assert len(await _alerts(rig)) == first + 1


async def test_unregistered_event_for_an_already_registered_key_is_ignored(tmp_path):
    async with m2_rig(tmp_path) as rig:
        await rig.manual_open(SYM, "0.002")
        rec = rig.pos()
        rig.kernel.bus.publish(UnregisteredPositionFound(symbol=SYM, side=LONG, qty=D("0.002"), entry_price=D("50000"),
                                                         trigger="reconcile", at_ms=rig.clock.now_ms()))
        await rig.settle()
        assert rig.pos().id == rec.id and rig.pos().version == rec.version
