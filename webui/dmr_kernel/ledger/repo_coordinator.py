"""协调者用的仓储函数：把执行过程中产生的信息并入 intent 表的 ``params``。

签名一律 ``fn(session, ...)``，经 ``Ledger.write`` 调用；返回记录或基本类型。

- ``merge_params``：把几个键并入意图的 ``params``（整体替换为新字典，ORM 才会发现 JSON 列变了）；意图不存在返回 None。
- ``set_result``：协调者写「执行结果」的约定入口，结果放在 ``params["result"]``；上层（保护单管理）读它决定后续动作。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from dmr_kernel.ledger.models import Intent
from dmr_kernel.ledger.records import IntentRecord

RESULT_KEY = "result"


def merge_params(session: Session, intent_id: int, patch: Mapping[str, Any], *, now_ms: int) -> IntentRecord | None:
    row = session.get(Intent, intent_id)
    if row is None:
        return None
    merged = dict(row.params or {})
    merged.update(patch)
    row.params = merged
    row.updated_ms = now_ms
    session.flush()
    return IntentRecord.from_row(row)


def set_result(session: Session, intent_id: int, result: Mapping[str, Any], *, now_ms: int) -> IntentRecord | None:
    return merge_params(session, intent_id, {RESULT_KEY: dict(result)}, now_ms=now_ms)
