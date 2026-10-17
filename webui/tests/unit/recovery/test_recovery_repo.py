"""ledger/repo_recovery.py 的账本读取函数。"""
from __future__ import annotations

from dmr_kernel.core.types import (
    ExitReason,
    IntentAction,
    IntentSource,
    IntentState,
    OperationStatus,
    OperationType,
)
from dmr_kernel.ledger import repo_recovery
from recovery_kit import LONG, SHORT, SYM, SYM2, T0


async def test_active_operations_are_the_ones_without_a_final_state(make_env):
    env = await make_env().boot()
    for op_id, status in (("a", OperationStatus.ACCEPTED), ("b", OperationStatus.RUNNING), ("c", OperationStatus.RUNNING)):
        await env.operations.begin(op_id, OperationType.CLOSE_ALL, operator="x", session_id=None, status=status)
    await env.operations.finish("c", OperationStatus.SUCCEEDED, result={})
    found = await env.ledger.read(repo_recovery.active_operations)
    assert [op.op_id for op in found] == ["a", "b"]


async def test_closed_with_leftovers_finds_each_kind_of_leftover_and_nothing_else(make_env):
    env = await make_env().boot()
    plain = await env.add_position(symbol="AUSDT")
    with_stop = await env.add_position(symbol="BUSDT", stop=("DKT-0-1-s", "1", "95"))
    with_stale = await env.add_position(symbol="CUSDT")
    with_handed = await env.add_position(symbol="DUSDT")
    still_open = await env.add_position(symbol="EUSDT", stop=("DKT-0-2-s", "2", "95"))
    await env.registry.add_stale_stop(with_stale.id, client_algo_id="DKT-0-3-s", algo_id=None)
    await env.registry.set_handed_over(with_handed.id, {"client_algo_id": "u1", "algo_id": "9"})
    for rec in (plain, with_stop, with_stale, with_handed):
        await env.registry.mark_closed(rec.id, exit_reason=ExitReason.MANUAL, closed_ms=T0)
    found = await env.ledger.read(repo_recovery.closed_with_leftovers)
    assert [rec.id for rec in found] == [with_stop.id, with_stale.id, with_handed.id]
    assert still_open.id not in [rec.id for rec in found]  # 还没平的不算


async def test_closed_with_leftovers_looks_only_at_the_most_recent_closed_records(make_env):
    env = await make_env().boot()
    old = await env.add_position(symbol="AUSDT", stop=("DKT-0-1-s", "1", "95"))
    await env.registry.mark_closed(old.id, exit_reason=ExitReason.MANUAL, closed_ms=T0)
    new = await env.add_position(symbol="BUSDT", stop=("DKT-0-2-s", "2", "95"))
    await env.registry.mark_closed(new.id, exit_reason=ExitReason.MANUAL, closed_ms=T0)
    found = await env.ledger.read(repo_recovery.closed_with_leftovers, limit=1)
    assert [rec.id for rec in found] == [new.id]


async def test_intents_of_operation_and_symbols_and_fills_helpers(make_env):
    env = await make_env().boot()
    mine = await env.new_intent(action=IntentAction.CLOSE, model="op", state=IntentState.ACTIVE, op_id="op-1")
    await env.new_intent(action=IntentAction.CLOSE, model="op", state=IntentState.ACTIVE, op_id="op-2")
    assert [i.id for i in await env.ledger.read(repo_recovery.intents_of_operation, "op-1")] == [mine.id]
    await env.new_intent(symbol=SYM2)
    assert await env.ledger.read(repo_recovery.recent_intent_symbols, since_ms=T0) == sorted({SYM, SYM2})
    assert await env.ledger.read(repo_recovery.recent_intent_symbols, since_ms=T0 + 1) == []


async def test_last_close_intent_picks_the_latest_close_or_partial_close_of_the_key(make_env):
    env = await make_env().boot()
    await env.new_intent(action=IntentAction.CLOSE, model="op")
    last = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op")
    await env.new_intent(action=IntentAction.STOP_PLACE, model="op")
    await env.new_intent(action=IntentAction.CLOSE, side=SHORT, model="op")
    await env.new_intent(action=IntentAction.CLOSE, symbol=SYM2, model="op")
    found = await env.ledger.read(repo_recovery.last_close_intent, SYM, LONG)
    assert found is not None and found.id == last.id
    assert await env.ledger.read(repo_recovery.last_close_intent, "NOPEUSDT", LONG) is None


async def test_cancel_intents_since_only_returns_the_reconcile_cancels(make_env):
    env = await make_env().boot()
    await env.new_intent(action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.RECONCILE, key="o1")
    await env.new_intent(action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.PROTECTION, key="o2")
    await env.new_intent(action=IntentAction.STOP_PLACE, model="op", source=IntentSource.RECONCILE, key="o3")
    found = await env.ledger.read(repo_recovery.cancel_intents_since, since_ms=T0)
    assert [i.idem_key for i in found] == ["o1"]
