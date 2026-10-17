"""账本：meta 与审计日志的仓储函数。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.ledger import repo_audit, repo_meta
from dmr_kernel.ledger.records import AuditLogRecord, MetaRecord

T0 = 1_760_000_000_000


async def test_meta_get_put_and_items(ledger):
    assert await ledger.read(repo_meta.get, "missing") is None
    await ledger.write(repo_meta.put, "b_key", "第一版")
    await ledger.write(repo_meta.put, "b_key", "第二版")  # 覆盖
    await ledger.write(repo_meta.put, "a_key", "x")
    assert await ledger.read(repo_meta.get, "b_key") == "第二版"
    items = await ledger.read(repo_meta.items)
    assert [r.key for r in items] == ["a_key", "b_key", "created_ms", "ledger_nonce"]
    assert all(isinstance(r, MetaRecord) for r in items)


async def test_meta_json_values(ledger):
    state = {"day_start_equity": Decimal("1000.50"), "tripped": False, "reasons": ["回撤"]}
    await ledger.write(repo_meta.put_json, "risk_state", state)
    assert await ledger.read(repo_meta.get_json, "risk_state") == {
        "day_start_equity": "1000.50", "tripped": False, "reasons": ["回撤"]}
    assert await ledger.read(repo_meta.get_json, "absent", {"default": 1}) == {"default": 1}


@pytest.mark.parametrize("key", ["ledger_nonce", "created_ms"])
async def test_meta_identity_keys_cannot_be_overwritten(ledger, key):
    before = await ledger.read(repo_meta.get, key)
    with pytest.raises(ValueError):
        await ledger.write(repo_meta.put, key, "zz")
    with pytest.raises(ValueError):
        await ledger.write(repo_meta.put_json, key, "zz")
    assert await ledger.read(repo_meta.get, key) == before


async def test_ensure_identity_is_idempotent(ledger, clock):
    assert await ledger.read(repo_meta.nonce) == ledger.nonce
    clock.advance(10_000)
    assert await ledger.write(repo_meta.ensure_identity, now_ms=clock.now_ms()) == ledger.nonce
    assert await ledger.read(repo_meta.get, "created_ms") == str(clock.now_ms() - 10_000)


async def test_audit_append_and_recent(ledger):
    first = await ledger.write(repo_audit.append, at_ms=T0, operator="alice", action="config.set",
                               target="leverage", before={"value": 2}, after={"value": Decimal("3")},
                               request_id="req-1")
    assert isinstance(first, AuditLogRecord)
    assert first.id > 0 and first.result == "ok"
    assert first.before == {"value": 2} and first.after == {"value": "3"}
    for i in range(1, 6):
        await ledger.write(repo_audit.append, at_ms=T0 + i, operator="bob" if i % 2 else "alice",
                           action="ops.start" if i < 3 else "ops.stop", result="rejected" if i == 5 else "ok")

    newest = await ledger.read(repo_audit.recent, limit=3)
    assert [r.at_ms for r in newest] == [T0 + 5, T0 + 4, T0 + 3]  # 新的在前
    older = await ledger.read(repo_audit.recent, limit=3, before_id=newest[-1].id)
    assert [r.at_ms for r in older] == [T0 + 2, T0 + 1, T0]  # 按编号往前翻页
    assert [r.at_ms for r in await ledger.read(repo_audit.recent, operator="alice")] == [T0 + 4, T0 + 2, T0]
    assert [r.at_ms for r in await ledger.read(repo_audit.recent, action="ops.start")] == [T0 + 2, T0 + 1]
    assert [r.id for r in await ledger.read(repo_audit.recent, target="leverage")] == [first.id]
    assert (await ledger.read(repo_audit.recent, limit=1))[0].result == "rejected"


async def test_audit_recent_rejects_bad_limit(ledger):
    with pytest.raises(ValueError):
        await ledger.read(repo_audit.recent, limit=0)
