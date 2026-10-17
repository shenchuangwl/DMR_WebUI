"""影子记录通道：source=SHADOW 的意图在协调者准入段之前被拦截，写 REJECTED + 审计，绝不产生订单。"""
from __future__ import annotations

from dmr_kernel.core.types import IntentAction, IntentSource, IntentState
from dmr_kernel.ledger import repo_audit
from dmr_kernel.ops import shadow

from coordinator_kit import SYM, spec

REASON = "影子记录：只留痕不下单"


async def test_shadow_open_is_rejected_before_admission_without_any_order(coord_rig):
    rig = coord_rig
    await rig.start()
    rec = await rig.go(spec(model="m3", source=IntentSource.SHADOW, ai_eval_ref="eval-1", reason="AI 看多"))
    assert rec.state is IntentState.REJECTED and rec.state_reason == REASON
    assert rec.ai_eval_ref == "eval-1" and rec.model == "m3"
    assert rig.names() == []                      # 适配层没有任何写调用
    assert await rig.order_rows(rec.id) == []     # 订单表里也没有
    assert rig.limits.occupancy(degraded=False, now_ms=rig.clock.now_ms()).reserved == 0  # 没占名额
    rows = await rig.ledger.read(repo_audit.recent, limit=50, action="shadow.intent")
    assert len(rows) == 1 and rows[0].target == f"intent:{rec.id}"
    assert rows[0].after["idem_key"] == rec.idem_key and rows[0].result == "recorded"


async def test_shadow_applies_to_every_action_even_when_gate_is_closed(coord_rig):
    rig = coord_rig
    await rig.start(running=False)
    recs = await rig.go_all(*(spec(model="m3", source=IntentSource.SHADOW, action=a, ttl_s=None)
                              for a in (IntentAction.OPEN, IntentAction.CLOSE, IntentAction.PARTIAL_CLOSE)))
    assert [r.state for r in recs] == [IntentState.REJECTED] * 3
    assert rig.names() == []


async def test_record_shadow_intent_forces_shadow_source_and_is_idempotent(coord_rig):
    rig = coord_rig
    await rig.start()
    base = spec(model="m3", source=IntentSource.SIGNAL, idem_key="shadow:m3:BTCUSDT:LONG:1")
    first = await shadow.record_shadow_intent(rig.coordinator, base)
    again = await shadow.record_shadow_intent(rig.coordinator, base)
    assert first.id == again.id
    assert first.source is IntentSource.SHADOW and first.state is IntentState.REJECTED
    assert rig.names() == []
    rows = await rig.ledger.read(repo_audit.recent, limit=50, action="shadow.intent")
    assert len(rows) == 1  # 重复提交不重复留痕
