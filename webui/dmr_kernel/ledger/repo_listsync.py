"""list_snapshot 与 list_member 表的仓储函数：名单每次拉取的留痕。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录。

- ``insert_snapshot``：一次拉取一行快照（成功或失败都记），同一事务里写入成员行。成员行包含被拒收的原始代码
  （``tradable=False``，原因写在 ``note``），方便名单页展示「为什么这个币没进来」。
- ``get_snapshot``、``members_of``、``recent_snapshots``：读。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.ledger.models import ListMember, ListSnapshot
from dmr_kernel.ledger.records import ListMemberRecord, ListSnapshotRecord


@dataclass(frozen=True)
class MemberRow:
    symbol: str
    direction: str | None
    score: Decimal | None
    first_selected_ms: int | None
    tradable: bool
    note: str = ""


def insert_snapshot(session: Session, *, fetched_ms: int, http_status: int | None, business_date: str | None,
                    scan_id: str | None, valid: bool | None, valid_until_ms: int | None, status: str | None,
                    accepted: bool, reason: str, members: Sequence[MemberRow] = ()) -> ListSnapshotRecord:
    row = ListSnapshot(fetched_ms=fetched_ms, http_status=http_status, business_date=business_date, scan_id=scan_id,
                       valid=valid, valid_until_ms=valid_until_ms, status=status, accepted=accepted, reason=reason,
                       member_count=len(members))
    session.add(row)
    session.flush()
    for m in members:
        session.add(ListMember(snapshot_id=row.id, symbol=m.symbol, direction=m.direction, score=m.score,
                               first_selected_ms=m.first_selected_ms, tradable=m.tradable, note=m.note))
    session.flush()
    return ListSnapshotRecord.from_row(row)


def get_snapshot(session: Session, snapshot_id: int) -> ListSnapshotRecord | None:
    row = session.get(ListSnapshot, snapshot_id)
    return None if row is None else ListSnapshotRecord.from_row(row)


def members_of(session: Session, snapshot_id: int) -> list[ListMemberRecord]:
    rows = session.scalars(select(ListMember).where(ListMember.snapshot_id == snapshot_id).order_by(ListMember.id))
    return [ListMemberRecord.from_row(r) for r in rows]


def recent_snapshots(session: Session, limit: int = 50) -> list[ListSnapshotRecord]:
    rows = session.scalars(select(ListSnapshot).order_by(ListSnapshot.id.desc()).limit(limit))
    return [ListSnapshotRecord.from_row(r) for r in rows]
