"""复核补的用例（资金安全不变量）：启动时刻过期或启动快照没做完时不自动接管、账本缺开仓成交的启动时已有仓、
手动接管按操作员看到的版本、撤掉系统止损失败时当前止损不先丢。完整内核 + 替身（takeover_kit）。"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from takeover_kit import m2_rig

from dmr_kernel.core.registry import StaleVersion
from dmr_kernel.core.types import Ownership, PendingReason, PositionStatus, ProtectionState, Unknown
from dmr_kernel.ledger import repo_takeover

D = Decimal
SYM = "BTCUSDT"


class _FlakyPositions:
    """只让接下来的 ``fail`` 次 ``fetch_positions`` 读不到，其余原样转给真适配器。"""

    def __init__(self, real, fail: int) -> None:
        self._real, self.fail = real, fail

    async def fetch_positions(self, *args, **kwargs):
        if self.fail > 0:
            self.fail -= 1
            return Unknown(reason="test: unreadable")
        return await self._real.fetch_positions(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def _start_without_settle(rig) -> None:
    version = rig.kernel.modelstate.current().version
    await rig.ok("/ops/start", {"op_id": rig.op_id("start"), "model_version": version})


async def test_a_judgment_racing_the_start_of_model_2_does_not_use_the_previous_start_time(tmp_path):
    """上一次运行留下的 m2_started_ms 早于这个仓；闸打开后、启动快照之前到来的重判不能拿旧的启动时刻放行自动接管。"""
    async with m2_rig(tmp_path, start=False) as rig:
        await rig.start_model()  # 上一次运行
        rig.advance(1_000)
        await rig.stop_model()
        rig.advance(1_000)
        await rig.manual_open(SYM, "0.002")  # 模型 2 停着时开的
        rec = rig.pos()
        assert rec.pending_reason is PendingReason.MODEL_NOT_ACTIVE
        rig.advance(1_000)
        service = rig.kernel.takeover
        await service._lock.acquire()
        try:
            rejudge = asyncio.ensure_future(service._rejudge(rec.id))  # 数量变化的重判先排上
            for _ in range(5):
                await asyncio.sleep(0)
            await _start_without_settle(rig)  # 闸打开；启动处理排在它后面
            for _ in range(5):
                await asyncio.sleep(0)
        finally:
            service._lock.release()
        await rejudge
        await rig.settle()
        rec = rig.pos()
        assert rec.status is PositionStatus.PENDING and rec.pending_reason is PendingReason.PREEXISTING
        assert rec.ownership is Ownership.NONE


async def test_a_preexisting_position_without_its_opening_fill_is_not_taken_over_when_added_to(tmp_path, monkeypatch):
    """启动快照读不到交易所、账本里也没有它的开仓成交（程序之前就开着的仓）：快照补做完之前的加仓不能让它被自动接管。"""
    async with m2_rig(tmp_path, start=False) as rig:
        service = rig.kernel.takeover
        await service.stop()  # 让这个仓在登记簿里没有记录（模拟程序启动前就有的仓）
        await rig.manual_open(SYM, "0.002")
        await service.start()
        assert rig.pos() is None
        cutoff = rig.clock.now_ms()
        real_key_fills = repo_takeover.key_fills

        def without_history(session, symbol, side, **kwargs):  # 账本里没有这个仓的开仓成交
            return [f for f in real_key_fills(session, symbol, side, **kwargs) if f.time_ms > cutoff]

        monkeypatch.setattr(repo_takeover, "key_fills", without_history)
        flaky = _FlakyPositions(service._adapter, fail=1)
        service._adapter = flaky
        rig.advance(1_000)
        await rig.start_model()  # 启动快照读不到持仓
        assert flaky.fail == 0 and rig.pos() is None
        rig.advance(1_000)
        await rig.manual_open(SYM, "0.001")  # 加仓：账本里看起来像「启动之后从零开的」
        await service.tick()
        await rig.settle()
        rec = rig.pos()
        assert rec is not None and rec.status is PositionStatus.PENDING
        assert rec.pending_reason is PendingReason.PREEXISTING and rec.ownership is Ownership.NONE


async def test_manual_take_over_checks_the_version_the_operator_saw(tmp_path):
    async with m2_rig(tmp_path) as rig:
        await rig.manual_open("ETHUSDT", "0.05")  # 清单外 → 待确认
        rec = rig.pos("ETHUSDT")
        seen = rec.version
        await rig.kernel.registry.set_pending(rec.id, reason=PendingReason.OUT_OF_SCOPE, note="之后又改过")
        assert rig.pos("ETHUSDT").version != seen
        with pytest.raises(StaleVersion):
            await rig.kernel.takeover.take_over(rec.id, expected_version=seen)
        assert rig.pos("ETHUSDT").status is PositionStatus.PENDING


async def test_cancel_stop_failing_on_an_old_stop_keeps_the_current_system_stop(tmp_path):
    """撤掉系统止损（cancel_stop）先撤待清理的旧单：旧单撤不掉时当前止损还在，仓位不会在仍是托管中时没有止损。"""
    async with m2_rig(tmp_path) as rig:
        await rig.manual_open(SYM, "0.002")
        rec = rig.pos()
        assert rec.ownership is Ownership.TAKEOVER and rec.protection_state is ProtectionState.SYSTEM
        current = rec.stop_client_id
        await rig.kernel.registry.add_stale_stop(rec.id, client_algo_id="old-stuck", algo_id="999999")
        protection = rig.kernel.protection
        real = protection._cancel_stop

        async def stuck_old(position, cid, algo_id):
            if cid == "old-stuck":
                return False
            return await real(position, cid, algo_id)

        protection._cancel_stop = stuck_old
        try:
            response = await rig.takeover_op("cancel_takeover", rec.id, mode="cancel_stop")
        finally:
            protection._cancel_stop = real
        assert response.status_code == 503, response.text
        await rig.settle()
        now = rig.pos()
        assert now.status is PositionStatus.MANAGED and now.stop_client_id == current
        assert [a.client_algo_id for a in await rig.algos()] == [current]
