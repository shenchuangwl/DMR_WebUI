"""影子记录通道的入口（第 6 层 ops；模型 3 预留，计划 M5 T5.2）。

``record_shadow_intent(coordinator, spec) -> IntentRecord``：把意图的来源强制改成 ``shadow`` 后交给协调者；协调者在准入段之前拦截——
意图写成 REJECTED（原因「影子记录：只留痕不下单」）并写一条审计（动作 ``shadow.intent``），**绝不产生订单**。
幂等键重复不再留痕第二次，返回已有记录。返回的是已到终态的意图记录。

``shadow_key(ref)``：控制接口 ``/dev/shadow_intent`` 的幂等键 ``shadow:<ref>``（ref 是调用方给的操作号）。
"""
from __future__ import annotations

from dataclasses import replace

from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.types import IntentSource
from dmr_kernel.ledger.records import IntentRecord

__all__ = ["SHADOW_KEY_PREFIX", "record_shadow_intent", "shadow_key"]

SHADOW_KEY_PREFIX = "shadow:"
WAIT_S = 10.0


def shadow_key(ref: str) -> str:
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("影子意图的引用不能为空")
    return SHADOW_KEY_PREFIX + ref


async def record_shadow_intent(coordinator: Coordinator, spec: IntentSpec, *, wait_s: float = WAIT_S) -> IntentRecord:
    shadow_spec = replace(spec, source=IntentSource.SHADOW)
    submitted = await coordinator.submit(shadow_spec)
    return await coordinator.wait_terminal(submitted.intent_id, wait_s)
