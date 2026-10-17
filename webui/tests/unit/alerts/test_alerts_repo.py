"""ledger.repo_alerts：alert 表的仓储函数。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import AlertLevel
from dmr_kernel.ledger import repo_alerts
from dmr_kernel.ledger.records import AlertRecord

NOW = 1_760_000_000_000


async def add(ledger, *, at_ms=NOW, level=AlertLevel.WARNING, kind="k", message="m", delivered=False,
              delivered_ms=None, attempts=0) -> AlertRecord:
    return await ledger.write(repo_alerts.insert, at_ms=at_ms, level=level, kind=kind, message=message,
                              delivered=delivered, delivered_ms=delivered_ms, attempts=attempts)


async def test_insert_returns_the_stored_record(ledger):
    rec = await add(ledger, level=AlertLevel.CRITICAL, kind="stream_down", message="数据流中断")
    assert isinstance(rec, AlertRecord) and rec.id >= 1
    assert (rec.at_ms, rec.level, rec.kind, rec.message) == (NOW, AlertLevel.CRITICAL, "stream_down", "数据流中断")
    assert (rec.delivered, rec.delivered_ms, rec.attempts) == (False, None, 0)
    assert await ledger.read(repo_alerts.get, rec.id) == rec


async def test_insert_local_delivery_is_stored_as_delivered(ledger):
    rec = await add(ledger, delivered=True, delivered_ms=NOW + 3)
    assert (rec.delivered, rec.delivered_ms, rec.attempts) == (True, NOW + 3, 0)


async def test_insert_rejects_an_unknown_level(ledger):
    with pytest.raises(Exception):
        await add(ledger, level="loud")  # type: ignore[arg-type]
    assert await ledger.read(repo_alerts.recent) == []


async def test_get_missing_alert_is_none(ledger):
    assert await ledger.read(repo_alerts.get, 12345) is None


async def test_record_attempt_counts_attempts_and_marks_delivery(ledger):
    rec = await add(ledger)
    failed = await ledger.write(repo_alerts.record_attempt, rec.id, delivered=False, at_ms=NOW + 1)
    assert (failed.attempts, failed.delivered, failed.delivered_ms) == (1, False, None)
    failed = await ledger.write(repo_alerts.record_attempt, rec.id, delivered=False, at_ms=NOW + 2)
    assert (failed.attempts, failed.delivered) == (2, False)
    ok = await ledger.write(repo_alerts.record_attempt, rec.id, delivered=True, at_ms=NOW + 3)
    assert (ok.attempts, ok.delivered, ok.delivered_ms) == (3, True, NOW + 3)
    assert await ledger.read(repo_alerts.get, rec.id) == ok


async def test_a_delivered_alert_is_never_marked_undelivered_again(ledger):
    rec = await add(ledger)
    await ledger.write(repo_alerts.record_attempt, rec.id, delivered=True, at_ms=NOW + 1)
    again = await ledger.write(repo_alerts.record_attempt, rec.id, delivered=False, at_ms=NOW + 2)
    assert again.delivered is True and again.delivered_ms == NOW + 1 and again.attempts == 2


async def test_record_attempt_on_a_missing_alert_is_an_error(ledger):
    with pytest.raises(LookupError):
        await ledger.write(repo_alerts.record_attempt, 999, delivered=True, at_ms=NOW)


async def test_recent_is_newest_first_with_limit_and_paging(ledger):
    ids = [(await add(ledger, kind=f"k{i}", at_ms=NOW + i)).id for i in range(5)]
    rows = await ledger.read(repo_alerts.recent)
    assert [r.id for r in rows] == list(reversed(ids))
    assert [r.id for r in await ledger.read(repo_alerts.recent, 2)] == [ids[4], ids[3]]
    assert [r.id for r in await ledger.read(repo_alerts.recent, 2, before_id=ids[3])] == [ids[2], ids[1]]


async def test_recent_can_filter_by_level(ledger):
    await add(ledger, level=AlertLevel.INFO)
    crit = await add(ledger, level=AlertLevel.CRITICAL)
    await add(ledger, level=AlertLevel.WARNING)
    rows = await ledger.read(repo_alerts.recent, 100, level=AlertLevel.CRITICAL)
    assert [r.id for r in rows] == [crit.id]


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5, "10"])
async def test_recent_rejects_a_bad_limit(ledger, limit):
    with pytest.raises(ValueError):
        await ledger.read(repo_alerts.recent, limit)
