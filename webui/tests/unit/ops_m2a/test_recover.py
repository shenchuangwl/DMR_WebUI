"""ops.startstop.recover：重新恢复（只在「恢复中」且上次恢复报了问题时可用）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import OperationStatus, Suppression
from dmr_kernel.ops.common import Caller, OpRejected
from dmr_kernel.ops.startstop import StartStop
from tests.support.kernel import RecoveryStub
from tests.unit.ops_m2a.m2a_kit import make_deps, rig

SESSION = Caller("alice", 7, True, True, None)
NOSEAT = Caller("bob", 8, True, False, None)
CLI = Caller.cli()


def svc_for(r, results, last):
    """results：每次 recover() 依次返回的结果；last：上次恢复的结果。"""
    calls = []

    async def recover():
        calls.append(1)
        outcome = results[len(calls) - 1]
        last[0] = outcome
        return outcome

    deps = make_deps(r.kernel, recover=recover, last_recovery=lambda: last[0])
    return StartStop(deps), calls


async def stuck(r):
    await r.kernel.modelstate.add_suppression(Suppression.RECOVERING, reason="t")


async def reject(coro, status, code):
    with pytest.raises(OpRejected) as caught:
        await coro
    assert (caught.value.status, caught.value.code) == (status, code)


async def test_recover_runs_recovery_again_and_records_the_outcome(tmp_path):
    async with rig(tmp_path) as r:
        await stuck(r)
        bad = RecoveryStub(tradable=False, problems=("快照读不到",))
        svc, calls = svc_for(r, [RecoveryStub(), ], [bad])
        response = await svc.recover(SESSION, op_id="r-1")
        assert len(calls) == 1 and response["status"] == "succeeded"
        assert response["result"]["tradable"] is True and response["result"]["problems"] == []
        record = await r.kernel.operations.get("r-1")
        assert record.status is OperationStatus.SUCCEEDED and record.operator == "alice"


async def test_recover_that_still_has_problems_is_partial(tmp_path):
    async with rig(tmp_path) as r:
        await stuck(r)
        bad = RecoveryStub(tradable=False, problems=("快照读不到",))
        svc, _ = svc_for(r, [bad], [bad])
        response = await svc.recover(CLI, op_id="r-1")
        assert response["status"] == "partial" and response["result"]["problems"] == ["快照读不到"]


async def test_recover_is_refused_when_not_recovering_or_the_last_run_had_no_problem(tmp_path):
    async with rig(tmp_path) as r:
        bad = RecoveryStub(tradable=False, problems=("x",))
        svc, calls = svc_for(r, [RecoveryStub()], [bad])
        await reject(svc.recover(CLI, op_id="r-1"), 409, "not_allowed")  # 不在恢复中
        await stuck(r)
        svc2, calls2 = svc_for(r, [RecoveryStub()], [RecoveryStub()])
        await reject(svc2.recover(CLI, op_id="r-2"), 409, "not_allowed")  # 上次没有问题
        svc3, calls3 = svc_for(r, [RecoveryStub()], [None])
        await reject(svc3.recover(CLI, op_id="r-3"), 409, "not_allowed")  # 还没有结果
        assert not calls and not calls2 and not calls3
        assert await r.kernel.operations.get("r-1") is None


async def test_recover_needs_the_seat_and_runs_once_per_operation_id(tmp_path):
    async with rig(tmp_path) as r:
        await stuck(r)
        bad = RecoveryStub(tradable=False, problems=("x",))
        svc, calls = svc_for(r, [bad, RecoveryStub()], [bad])
        await reject(svc.recover(NOSEAT, op_id="r-1"), 409, "seat_required")
        first = await svc.recover(SESSION, op_id="r-1")
        again = await svc.recover(SESSION, op_id="r-1")
        assert again == first and len(calls) == 1


async def test_recover_without_a_wired_recovery_is_not_implemented(tmp_path):
    async with rig(tmp_path) as r:
        await stuck(r)
        svc = StartStop(make_deps(r.kernel, last_recovery=lambda: RecoveryStub(tradable=False)))
        await reject(svc.recover(CLI, op_id="r-1"), 501, "not_implemented")
