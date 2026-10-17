"""ledger/repo_reconcile.py 的仓储函数：对账记录与差异、孤儿撤单的幂等键与在途判断、系统成交未入账的判断。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import IntentAction, IntentState
from dmr_kernel.ledger import repo_audit, repo_reconcile
from dmr_kernel.ledger.repo_reconcile import DiffSpec, ResolveSpec
from reconcile_kit import D, LONG, SHORT, SYM, SYM2, T0


def mismatch(symbol=SYM, side=LONG, local="1", exchange="1.5", note="") -> DiffSpec:
    return DiffSpec(symbol, side, "qty_mismatch", D(local), D(exchange), note)


async def round_(ledger, *, at_ms=T0, kind="positions", status="mismatch", diffs=(), resolves=(), detail="{}"):
    return await ledger.write(repo_reconcile.record_round, at_ms=at_ms, kind=kind, status=status, duration_ms=7,
                              detail=detail, diffs=list(diffs), resolves=list(resolves))


# ================================================================ 对账记录与差异


async def test_a_round_is_recorded_with_its_diffs(ledger):
    run, diffs = await round_(ledger, diffs=[mismatch(note="登记 1，交易所 1.5")], detail='{"n": 1}')

    assert (run.kind, run.status, run.at_ms, run.duration_ms, run.detail) == ("positions", "mismatch", T0, 7, '{"n": 1}')
    [diff] = diffs
    assert (diff.run_id, diff.symbol, diff.side, diff.kind) == (run.id, SYM, LONG, "qty_mismatch")
    assert (diff.local_qty, diff.exchange_qty, diff.resolved_ms, diff.note) == (D("1"), D("1.5"), None, "登记 1，交易所 1.5")
    assert await ledger.read(repo_reconcile.list_diffs, open_only=True) == [diff]


async def test_the_same_open_diff_is_updated_not_duplicated(ledger):
    run1, [first] = await round_(ledger, diffs=[mismatch()])
    run2, [second] = await round_(ledger, at_ms=T0 + 10_000, diffs=[mismatch(exchange="1.7", note="变了")])

    assert second.id == first.id and second.run_id == run1.id  # 同一行，记着是哪一轮最先发现的
    assert second.exchange_qty == D("1.7") and second.note == "变了"
    assert len(await ledger.read(repo_reconcile.list_diffs, open_only=False)) == 1
    assert run2.id != run1.id


async def test_diffs_of_different_keys_or_kinds_are_separate_rows(ledger):
    await round_(ledger, diffs=[mismatch(SYM, LONG), mismatch(SYM, SHORT), mismatch(SYM2, LONG),
                                DiffSpec(SYM, LONG, "unregistered", None, D("1"), "")])
    assert len(await ledger.read(repo_reconcile.list_diffs, open_only=True)) == 4


async def test_resolving_marks_the_rows_and_a_later_detection_starts_a_new_row(ledger):
    await round_(ledger, diffs=[mismatch(), DiffSpec(SYM, LONG, "unregistered", None, D("1"), "")])

    _, _ = await round_(ledger, at_ms=T0 + 5, status="ok", resolves=[ResolveSpec(SYM, LONG, ("qty_mismatch",), "已一致")])

    rows = await ledger.read(repo_reconcile.list_diffs, open_only=False)
    by_kind = {r.kind: r for r in rows}
    assert by_kind["qty_mismatch"].resolved_ms == T0 + 5 and "已一致" in by_kind["qty_mismatch"].note
    assert by_kind["unregistered"].resolved_ms is None  # 没点名的种类不动
    assert [r.kind for r in await ledger.read(repo_reconcile.list_diffs, open_only=True)] == ["unregistered"]

    await round_(ledger, at_ms=T0 + 9, diffs=[mismatch()])  # 差异又出现：新的一行
    assert len(await ledger.read(repo_reconcile.list_diffs, open_only=False)) == 3


async def test_resolving_without_kinds_resolves_every_kind_of_that_key(ledger):
    await round_(ledger, diffs=[mismatch(), DiffSpec(SYM, LONG, "unregistered", None, D("1"), ""), mismatch(SYM2)])
    await round_(ledger, at_ms=T0 + 1, status="ok", resolves=[ResolveSpec(SYM, LONG, (), "该键已平")])
    assert [(r.symbol) for r in await ledger.read(repo_reconcile.list_diffs, open_only=True)] == [SYM2]


async def test_recent_runs_are_newest_first_and_can_be_filtered(ledger):
    await round_(ledger, at_ms=T0, kind="positions", status="unknown")
    await round_(ledger, at_ms=T0 + 1, kind="orders", status="ok")
    await round_(ledger, at_ms=T0 + 2, kind="positions", status="ok")

    assert [(r.kind, r.status) for r in await ledger.read(repo_reconcile.recent_runs)] == [
        ("positions", "ok"), ("orders", "ok"), ("positions", "unknown")]
    assert [r.at_ms for r in await ledger.read(repo_reconcile.recent_runs, kind="positions", limit=1)] == [T0 + 2]
    with pytest.raises(ValueError):
        await ledger.read(repo_reconcile.recent_runs, limit=0)


async def test_confirming_a_mismatch_resolves_only_its_diffs_and_writes_the_audit(ledger):
    await round_(ledger, diffs=[mismatch(), mismatch(SYM2), DiffSpec(SYM, SHORT, "unregistered", None, D("1"), "")])

    count = await ledger.write(repo_reconcile.confirm_mismatch, at_ms=T0 + 3, operator="alice")

    assert count == 2
    assert [r.kind for r in await ledger.read(repo_reconcile.list_diffs, open_only=True)] == ["unregistered"]
    [audit] = await ledger.read(repo_audit.recent, action="reconcile.confirm_mismatch")
    assert (audit.operator, audit.at_ms, audit.target) == ("alice", T0 + 3, "reconcile")
    assert audit.after == {"resolved": 2}


# ================================================================ 撤单的幂等键与在途判断


async def test_cancel_key_state_walks_the_attempts(make_env):
    env = await make_env().boot()
    state = await env.ledger.read(repo_reconcile.cancel_key_state, "orphan:9001", max_attempts=3)
    assert (state.next_key, state.attempts, state.in_flight, state.exhausted) == ("orphan:9001", 0, False, False)

    first = await env.new_intent(key="orphan:9001", action=IntentAction.STOP_CANCEL, state=IntentState.RECONCILING)
    state = await env.ledger.read(repo_reconcile.cancel_key_state, "orphan:9001", max_attempts=3)
    assert (state.next_key, state.attempts, state.in_flight) == (None, 0, True)  # 上一次还没结束

    await env.intents.transition(first.id, IntentState.REJECTED, reason="仍然存在")
    state = await env.ledger.read(repo_reconcile.cancel_key_state, "orphan:9001", max_attempts=3)
    assert (state.next_key, state.attempts, state.in_flight) == ("orphan:9001:1", 1, False)

    for key in ("orphan:9001:1", "orphan:9001:2"):
        intent = await env.new_intent(key=key, action=IntentAction.STOP_CANCEL, state=IntentState.REJECTED)
        assert intent.state is IntentState.REJECTED
    state = await env.ledger.read(repo_reconcile.cancel_key_state, "orphan:9001", max_attempts=3)
    assert (state.next_key, state.attempts, state.in_flight, state.exhausted) == (None, 3, False, True)


# ================================================================ 在途意图是否引用某张条件单


async def referenced(env, stop, *, symbol=SYM, side=LONG, **ids):
    return await env.ledger.read(repo_reconcile.algo_in_flight, symbol=symbol, side=side,
                                 client_algo_id=ids.get("cid", stop.client_algo_id), algo_id=ids.get("algo_id", stop.algo_id))


async def test_an_order_row_whose_intent_is_still_open_is_in_flight(make_env):
    env = await make_env().boot()
    ended = await env.system_stop(algo_id="9001")
    assert await referenced(env, ended) is False

    open_ = await env.system_stop(algo_id="9002", intent_state=IntentState.ACTIVE)
    assert await referenced(env, open_) is True
    assert await referenced(env, open_, cid="nobody", algo_id="9002") is True  # 只凭条件单号也认得
    assert await referenced(env, open_, cid=open_.client_algo_id, algo_id=None) is True


async def test_open_cancel_and_replace_intents_are_found_by_their_params(make_env):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    await env.new_intent(action=IntentAction.STOP_CANCEL, state=IntentState.RECONCILING, model="op",
                         params={"algo_id": "9001"})
    assert await referenced(env, stop) is True

    other = await env.system_stop(algo_id="9002")
    await env.new_intent(action=IntentAction.STOP_REPLACE, state=IntentState.ACTIVE, model="op",
                         params={"old": {"client_algo_id": other.client_algo_id}})
    assert await referenced(env, other) is True
    assert await referenced(env, other, symbol=SYM2) is False  # 别的合约上的意图不算


async def test_ended_intents_do_not_count(make_env):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    await env.new_intent(action=IntentAction.STOP_CANCEL, state=IntentState.COMPLETED, model="op",
                         params={"algo_id": "9001"})
    assert await referenced(env, stop) is False


# ================================================================ 系统成交还没入账


async def test_system_fills_pending_until_the_fills_are_booked(make_env):
    env = await make_env().boot()
    assert await env.ledger.read(repo_reconcile.system_fills_pending, SYM, LONG, since_ms=0) is False

    order, fill = await env.system_open_fill(qty="0.5")
    assert await env.ledger.read(repo_reconcile.system_fills_pending, SYM, LONG, since_ms=0) is True
    assert await env.ledger.read(repo_reconcile.system_fills_pending, SYM, SHORT, since_ms=0) is False  # 别的方向
    assert await env.ledger.read(repo_reconcile.system_fills_pending, SYM2, LONG, since_ms=0) is False

    await env.ingest.ingest_trade(fill, client_order_id=order.client_order_id, path="rest")
    await env.settle()
    assert await env.ledger.read(repo_reconcile.system_fills_pending, SYM, LONG, since_ms=0) is False


async def test_system_fills_pending_only_looks_at_recent_orders(make_env):
    env = await make_env().boot()
    await env.system_open_fill(qty="0.5")
    now = env.clock.now_ms()
    assert await env.ledger.read(repo_reconcile.system_fills_pending, SYM, LONG, since_ms=now) is True
    assert await env.ledger.read(repo_reconcile.system_fills_pending, SYM, LONG, since_ms=now + 1) is False
