"""保护单管理为 F13 提供的串行接口（``Protection.cancel_takeover``）与「交给系统管理止损」选旧单的次序。"""
from __future__ import annotations

import asyncio

import pytest
from takeover_kit import LONG, m2_rig

from dmr_kernel.core.protection import _Tracked
from dmr_kernel.core.types import Ownership, PositionStatus, ProtectionState

SYM = "BTCUSDT"


async def _taken(rig):
    await rig.manual_open(SYM, "0.002")
    rec = rig.pos()
    assert rec.ownership is Ownership.TAKEOVER and rec.protection_state is ProtectionState.SYSTEM
    return rec


async def _finalize_ignore(rig, rec):
    async def finalize():
        await rig.kernel.registry.ignore(rec.id, expected_version=None)

    return finalize


async def test_unknown_mode_is_a_programming_error(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _taken(rig)
        with pytest.raises(ValueError):
            await rig.kernel.protection.cancel_takeover(rec.id, mode="x", finalize=await _finalize_ignore(rig, rec))


async def test_it_waits_for_an_in_flight_stop_change_and_gives_up_when_it_never_resolves(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _taken(rig)
        protection = rig.kernel.protection
        protection._tracked[10_001] = _Tracked(kind="replace", position_id=rec.id, started_ms=rig.clock.now_ms())
        word = await protection.cancel_takeover(rec.id, mode="handover", finalize=await _finalize_ignore(rig, rec),
                                                wait_s=0.1)
        assert word == "busy"
        now = rig.pos()
        assert now.status is PositionStatus.MANAGED and now.handed_over_stop is None  # 什么也没做
        # 在途的换止损出了结论：接着就能做
        task = asyncio.ensure_future(protection.cancel_takeover(
            rec.id, mode="handover", finalize=await _finalize_ignore(rig, rec), wait_s=5))
        await asyncio.sleep(0.1)
        assert not task.done()
        protection._tracked.pop(10_001)
        assert await asyncio.wait_for(task, 5) == "done"
        assert rig.pos().status is PositionStatus.IGNORED


async def test_other_positions_and_pending_ones_are_skipped_and_closed_ones_reported(tmp_path):
    async with m2_rig(tmp_path) as rig:
        await rig.manual_open("ETHUSDT", "0.05")  # 待确认
        pending = rig.pos("ETHUSDT")

        async def finalize():
            raise AssertionError("不该调用")

        assert await rig.kernel.protection.cancel_takeover(pending.id, mode="handover", finalize=finalize) == "skipped"
        assert await rig.kernel.protection.cancel_takeover(424242, mode="handover", finalize=finalize) == "closed"


async def test_cancel_stop_clears_the_registered_stop_after_the_status_change(tmp_path):
    async with m2_rig(tmp_path) as rig:
        rec = await _taken(rig)
        word = await rig.kernel.protection.cancel_takeover(rec.id, mode="cancel_stop",
                                                           finalize=await _finalize_ignore(rig, rec))
        assert word == "done" and await rig.algos() == []
        now = rig.pos()
        assert now.status is PositionStatus.IGNORED and not now.stop_client_id and now.protection_state is ProtectionState.NONE


async def test_users_external_stop_is_never_touched_by_cancel_stop(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        await rig.manual_open(SYM, "0.002")
        user = await rig.manual_stop(SYM, "48000")
        rig.advance(1_000)
        await rig.kernel.jobs["reconcile_orders"]()
        await rig.takeover_op("takeover", rig.pos().id)
        await rig.settle()
        rec = rig.pos()
        assert rec.protection_state is ProtectionState.EXTERNAL
        word = await rig.kernel.protection.cancel_takeover(rec.id, mode="cancel_stop",
                                                           finalize=await _finalize_ignore(rig, rec))
        assert word == "done"
        assert [a.client_algo_id for a in await rig.algos()] == [user["client_algo_id"]]


async def test_hand_stop_to_system_prefers_the_users_full_close_stop_as_the_old_one(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        await rig.manual_open(SYM, "0.002")
        qty_stop = await rig.manual_stop(SYM, "47000", close_position=False, qty="0.002")  # 数量型，先挂
        full = await rig.manual_stop(SYM, "48000")  # 全平型
        rig.advance(1_000)
        await rig.kernel.jobs["reconcile_orders"]()
        await rig.takeover_op("takeover", rig.pos().id)
        await rig.settle()
        rec = rig.pos()
        assert rec.protection_state is ProtectionState.EXTERNAL
        sent_before = len(rig.standin.transport.sent("POST", "/api/private/algo_order"))
        deletes_before = len(rig.standin.transport.sent("DELETE", "/api/private/algo_order"))
        word = await rig.kernel.protection.hand_stop_to_system(rec.id)
        await rig.settle()
        assert word == "protected"
        rec = rig.pos()
        assert rec.protection_state is ProtectionState.SYSTEM
        remaining = await rig.algos()
        assert [a.client_algo_id for a in remaining] == [rec.stop_client_id]  # 两张用户的止损都撤掉了，只剩系统的
        assert len(rig.standin.transport.sent("POST", "/api/private/algo_order")) == sent_before + 1
        deletes = rig.standin.transport.sent("DELETE", "/api/private/algo_order")[deletes_before:]

        def named(params, order):
            return params.get("client_algo_id") == order["client_algo_id"] or params.get("algo_id") == str(order["algo_id"])

        assert named(deletes[0][2], full)  # 旧单是全平型的那张（先撤/被替换），数量型的随后撤
        assert any(named(d[2], qty_stop) for d in deletes[1:])
