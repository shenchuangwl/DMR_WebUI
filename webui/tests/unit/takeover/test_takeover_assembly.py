"""装配：内核里有模型 2 的识别服务与接管操作服务，保护节拍顺带驱动它，关停时它先退订。"""
from __future__ import annotations

from takeover_kit import m2_rig

from dmr_kernel.models.m2_takeover import TakeoverService
from dmr_kernel.ops.takeover_ops import TakeoverOps


async def test_the_kernel_wires_the_takeover_service_and_ops(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        kernel = rig.kernel
        assert isinstance(kernel.takeover, TakeoverService) and isinstance(kernel.takeover_ops, TakeoverOps)
        assert kernel.app.state.takeover_ops is kernel.takeover_ops
        assert kernel.takeover.crashed is None


async def test_the_protection_tick_job_drives_the_takeover_tick(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        calls = []
        real = rig.kernel.takeover.tick

        async def spy():
            calls.append(1)
            await real()

        rig.kernel.takeover.tick = spy
        await rig.kernel.jobs["protection_tick"]()
        assert calls == [1]


async def test_a_tick_after_stop_does_nothing(tmp_path):
    async with m2_rig(tmp_path, start=False) as rig:
        await rig.kernel.takeover.stop()
        await rig.kernel.takeover.tick()  # 停止之后的 tick 什么也不做、不抛错
