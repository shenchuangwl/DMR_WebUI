"""交易意图约定（docs/交易意图约定.md）由测试固定：字段名与类型、幂等键格式样例、状态机、文档覆盖。"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from dmr_kernel.core import intents
from dmr_kernel.core.types import IntentSource, IntentState, PositionSide

DOC = Path(__file__).resolve().parents[3] / "docs" / "交易意图约定.md"

EXPECTED_FIELDS = {
    "action": "IntentAction", "model": "str", "source": "IntentSource", "symbol": "str", "side": "PositionSide",
    "idem_key": "str", "qty": "Decimal | None", "ratio": "Decimal | None", "params": "Mapping[str, Any]",
    "reason": "str", "ttl_s": "int | None", "operator": "str | None", "op_id": "str | None",
    "position_id": "int | None", "position_generation": "int | None", "parent_intent_id": "int | None",
    "ai_eval_ref": "str | None",
}


def test_intent_spec_fields_and_types_are_fixed():
    fields = {f.name: f.type for f in dataclasses.fields(intents.IntentSpec)}
    assert fields == EXPECTED_FIELDS  # 增删改字段都要同时改约定文档与这里
    required = {f.name for f in dataclasses.fields(intents.IntentSpec)
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
    assert required == {"action", "model", "source", "symbol", "side", "idem_key"}


def test_idempotency_key_samples():
    assert intents.key_open("m1", "BTCUSDT", PositionSide.LONG, 1700000000000) == "open:m1:BTCUSDT:LONG:1700000000000"
    assert intents.key_open("m1", "BTCUSDT", "SHORT", 5, reverse_of_generation=2) == "open:m1:BTCUSDT:SHORT:5:rev2"
    assert intents.key_open("m1", "BTCUSDT", "LONG", 5, injected_seq=3) == "open:m1:BTCUSDT:LONG:5:inj3"
    assert intents.key_signal_close("BTCUSDT", "LONG", 4, 99) == "sigclose:BTCUSDT:LONG:g4:99"
    assert intents.key_op("op-1") == "op:op-1"
    assert intents.key_op("op-1", "BTCUSDT", "LONG", "close") == "op:op-1:BTCUSDT:LONG:close"
    assert intents.key_protect(7, 2, "stop", 1) == "protect:7:g2:stop:1"
    assert intents.key_open("m:1", "BTC", "LONG", 1) == "open:m%3A1:BTC:LONG:1"  # 冒号转义


def test_state_machine_is_fixed():
    assert {s.value for s in IntentState} == {
        "CREATED", "VALIDATED", "RESERVED", "DISPATCH_READY", "ACTIVE", "RECONCILING", "COMPLETED", "REJECTED",
        "EXPIRED", "CANCELLED"}
    for terminal in IntentState.terminal():
        assert intents.TRANSITIONS[terminal] == frozenset()
    assert IntentState.REJECTED in intents.TRANSITIONS[IntentState.CREATED]


def test_shadow_source_exists():
    assert IntentSource.SHADOW.value == "shadow"


def test_document_covers_every_field_state_source_and_key_format():
    text = DOC.read_text(encoding="utf-8")
    for name in EXPECTED_FIELDS:
        assert f"`{name}`" in text, name
    for state in IntentState:
        assert state.value in text, state
    for source in IntentSource:
        assert f"`{source.value}`" in text, source
    for prefix in ("open:", "sigclose:", "op:", "protect:", "shadow:"):
        assert prefix in text, prefix
    assert "ai_eval_ref" in text and "影子记录：只留痕不下单" in text
