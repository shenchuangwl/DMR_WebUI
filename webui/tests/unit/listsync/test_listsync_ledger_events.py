"""每次拉取落账本、发布 ListSnapshotUpdated。"""
from __future__ import annotations

from decimal import Decimal

from dmr_kernel.core.events import ListSnapshotUpdated
from dmr_kernel.ledger import repo_listsync
from listsync_world import member, payload, sample, variant

MIN = 60_000


async def test_every_fetch_leaves_a_snapshot_row_and_the_members(make_sync, ledger, rules):
    sync, _ = make_sync((200, sample()))
    view = await sync.poll()
    snap = await ledger.read(repo_listsync.get_snapshot, view.snapshot_id)
    assert (snap.http_status, snap.valid, snap.accepted, snap.business_date, snap.status) == (200, True, True, "2026-10-16", "live")
    assert snap.scan_id == "20261016-088" and snap.valid_until_ms == view.valid_until_ms
    assert snap.fetched_ms == view.fetched_ms and snap.member_count == 6 and snap.reason == ""
    rows = await ledger.read(repo_listsync.members_of, view.snapshot_id)
    assert {r.symbol for r in rows} == {m.symbol for m in view.members}
    top = max(rows, key=lambda r: r.score)
    assert top.symbol == "LUMIAUSDT" and isinstance(top.score, Decimal) and top.direction == "SHORT"
    assert all(r.tradable is True and r.first_selected_ms is not None for r in rows)


async def test_failed_and_rejected_fetches_are_recorded_too(make_sync, ledger, clock):
    sync, _ = make_sync((503, {"detail": "x"}), (200, variant(valid=False)), ConnectionError("down"))
    for _ in range(3):
        await sync.poll()
        clock.advance(MIN)
    rows = await ledger.read(repo_listsync.recent_snapshots, 10)
    assert len(rows) == 3
    by_status = sorted(rows, key=lambda r: r.id)
    assert [(r.http_status, r.accepted) for r in by_status] == [(503, False), (200, False), (None, False)]
    assert all(r.reason for r in by_status) and by_status[1].valid is False


async def test_rejected_codes_are_stored_as_not_tradable_members(make_sync, ledger):
    sync, _ = make_sync((200, payload(onlycoin=[member("TIAUSDT", "LONG", 70.0), member("GHOSTUSDT", "SHORT", 60.0)])))
    view = await sync.poll()
    rows = {r.symbol: r for r in await ledger.read(repo_listsync.members_of, view.snapshot_id)}
    assert rows["TIAUSDT"].tradable is True and rows["GHOSTUSDT"].tradable is False and "查不到" in rows["GHOSTUSDT"].note


async def test_a_snapshot_updated_event_is_published_for_every_fetch(make_sync, bus):
    seen: list[ListSnapshotUpdated] = []

    async def handler(event):
        seen.append(event)

    bus.subscribe(ListSnapshotUpdated, handler, name="t")
    await bus.start()
    sync, _ = make_sync((200, sample()), (503, {}))
    good = await sync.poll()
    await sync.poll()
    await bus.drain()
    await bus.stop()
    assert [(e.valid, e.at_ms) for e in seen] == [(True, good.fetched_ms), (False, good.fetched_ms)]
    assert seen[0].snapshot_id == good.snapshot_id and seen[1].snapshot_id != seen[0].snapshot_id
