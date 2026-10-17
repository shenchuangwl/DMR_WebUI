"""交易意图 10 态的迁移表：正反用例逐条；终态只能经 reopen_for_late_fill 回到 ACTIVE。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.events import IntentStateChanged
from dmr_kernel.core.intents import TRANSITIONS, InvalidTransition
from dmr_kernel.core.types import IntentState

S = IntentState

# 计划 W2e 里写明的迁移表（逐字照抄，作为独立的对照）
EXPECTED: dict[IntentState, set[IntentState]] = {
    S.CREATED: {S.VALIDATED, S.REJECTED, S.EXPIRED, S.CANCELLED},
    S.VALIDATED: {S.RESERVED, S.DISPATCH_READY, S.REJECTED, S.EXPIRED, S.CANCELLED},
    S.RESERVED: {S.DISPATCH_READY, S.REJECTED, S.EXPIRED, S.CANCELLED},
    S.DISPATCH_READY: {S.ACTIVE, S.RECONCILING, S.REJECTED},
    S.ACTIVE: {S.RECONCILING, S.COMPLETED, S.REJECTED, S.CANCELLED},
    S.RECONCILING: {S.ACTIVE, S.COMPLETED, S.REJECTED},
    S.COMPLETED: set(),
    S.REJECTED: set(),
    S.EXPIRED: set(),
    S.CANCELLED: set(),
}


def test_transition_table_matches_the_documented_edges():
    assert {state: set(targets) for state, targets in TRANSITIONS.items()} == EXPECTED


def test_every_state_has_an_entry_and_terminal_states_have_no_exit():
    assert set(TRANSITIONS) == set(IntentState)
    for state in IntentState.terminal():
        assert TRANSITIONS[state] == frozenset()
    assert all(isinstance(targets, frozenset) for targets in TRANSITIONS.values())


def test_the_table_has_no_self_loops_and_only_terminal_states_are_dead_ends():
    for state, targets in TRANSITIONS.items():
        assert state not in targets
        if state not in IntentState.terminal():
            assert targets, f"{state} 不是终态却没有出口"


async def test_every_pair_of_states_behaves_as_the_table_says(service, in_state):
    """10 × 10 全部组合：表里有的迁移成功，表里没有的抛 InvalidTransition 且状态不变。"""
    checked = 0
    for source in IntentState:
        for target in IntentState:
            rec = await in_state(source, f"pair:{source.value}:{target.value}")
            if target in EXPECTED[source]:
                moved = await service.transition(rec.id, target, reason="按表迁移")
                assert moved.state is target
                assert moved.state_reason == "按表迁移"
            else:
                with pytest.raises(InvalidTransition):
                    await service.transition(rec.id, target, reason="不该成功")
                assert (await service.get(rec.id)).state is source
            checked += 1
    assert checked == 100


async def test_invalid_transition_message_names_both_states(service, in_state):
    rec = await in_state(S.CREATED, "msg")
    with pytest.raises(InvalidTransition) as exc:
        await service.transition(rec.id, S.ACTIVE)
    assert "CREATED" in str(exc.value) and "ACTIVE" in str(exc.value)


async def test_terminal_state_cannot_be_left_by_a_plain_transition(service, in_state):
    for terminal in IntentState.terminal():
        rec = await in_state(terminal, f"term:{terminal.value}")
        for target in IntentState:
            with pytest.raises(InvalidTransition):
                await service.transition(rec.id, target)


async def test_transition_stores_the_reason_and_refreshes_updated_ms(service, in_state, clock):
    rec = await in_state(S.CREATED, "reason")
    clock.advance(2_500)
    moved = await service.transition(rec.id, S.VALIDATED, reason="预检通过")
    assert moved.state_reason == "预检通过"
    assert moved.updated_ms == clock.now_ms() == rec.updated_ms + 2_500
    assert moved.created_ms == rec.created_ms


async def test_transition_default_reason_is_empty(service, in_state):
    rec = await in_state(S.CREATED, "noreason")
    assert (await service.transition(rec.id, S.VALIDATED)).state_reason == ""


async def test_expect_guards_against_a_state_that_has_already_moved(service, in_state):
    rec = await in_state(S.VALIDATED, "expect")
    # 期望还在 CREATED，实际已经是 VALIDATED：即使 VALIDATED → REJECTED 本身合法，也不能执行
    with pytest.raises(InvalidTransition) as exc:
        await service.transition(rec.id, S.REJECTED, expect={S.CREATED})
    assert "VALIDATED" in str(exc.value)
    assert (await service.get(rec.id)).state is S.VALIDATED
    ok = await service.transition(rec.id, S.REJECTED, reason="风控不过", expect={S.CREATED, S.VALIDATED})
    assert ok.state is S.REJECTED


async def test_expect_does_not_make_an_illegal_edge_legal(service, in_state):
    rec = await in_state(S.CREATED, "expect2")
    with pytest.raises(InvalidTransition):
        await service.transition(rec.id, S.ACTIVE, expect={S.CREATED})


async def test_transition_of_unknown_intent_is_a_key_error(service):
    with pytest.raises(KeyError):
        await service.transition(424242, S.VALIDATED)


async def test_failed_transitions_publish_nothing_and_change_nothing(service, in_state, bus, collector):
    await bus.start()
    rec = await in_state(S.CREATED, "quiet")
    await bus.drain()
    before = len(collector.of(IntentStateChanged))
    with pytest.raises(InvalidTransition):
        await service.transition(rec.id, S.COMPLETED)
    await bus.drain()
    assert len(collector.of(IntentStateChanged)) == before
    assert await service.get(rec.id) == rec


async def test_each_successful_transition_publishes_one_state_change_event(service, in_state, bus, collector, clock):
    await bus.start()
    rec = await in_state(S.CREATED, "ev")
    clock.advance(1_000)
    await service.transition(rec.id, S.VALIDATED, reason="x")
    await service.transition(rec.id, S.DISPATCH_READY)
    await bus.drain()
    events = collector.of(IntentStateChanged)
    assert [e.state for e in events] == ["CREATED", "VALIDATED", "DISPATCH_READY"]
    last = events[-1]
    assert (last.intent_id, last.action, last.symbol, last.side, last.at_ms) == (
        rec.id, "open", "BTCUSDT", rec.side, clock.now_ms())


async def test_reserved_slot_is_taken_on_reserved_and_released_on_any_terminal_state(service, in_state):
    rec = await in_state(S.VALIDATED, "slot")
    assert rec.reserved_slot is False
    rec = await service.transition(rec.id, S.RESERVED)
    assert rec.reserved_slot is True
    rec = await service.transition(rec.id, S.DISPATCH_READY)
    assert rec.reserved_slot is True  # 名额一直占到终态，因为这时持仓还没进对账快照
    rec = await service.transition(rec.id, S.ACTIVE)
    assert rec.reserved_slot is True
    rec = await service.transition(rec.id, S.COMPLETED)
    assert rec.reserved_slot is False


@pytest.mark.parametrize("terminal", [S.REJECTED, S.EXPIRED, S.CANCELLED])
async def test_reserved_slot_is_released_when_a_reserved_intent_ends(service, in_state, terminal):
    rec = await in_state(S.RESERVED, f"slot:{terminal.value}")
    assert rec.reserved_slot is True
    ended = await service.transition(rec.id, terminal, reason="结束")
    assert ended.reserved_slot is False
