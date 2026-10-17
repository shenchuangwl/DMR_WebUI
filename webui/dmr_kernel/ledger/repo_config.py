"""config_item 表的仓储函数：配置项的当前值（默认值、范围、生效方式来自 ``core.configdefs``，不在这里）。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录或基本类型。

- 值是 JSON：Decimal 存成字符串（与接口约定一致），列表原样；``None`` 存 SQL NULL——「待你定」的项是「行在、值为空」，
  和「键不存在」分得开。
- ``init_missing``：只补账本里还没有的键，已有的一律不动（首次启动写入初始值；新版本加了配置项时补上）。
- ``set_item``：同一事务里写 config_item 与 audit_log（经 ``repo_audit``），任何一步失败整体回滚。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.ledger import repo_audit
from dmr_kernel.ledger.models import ConfigItem
from dmr_kernel.ledger.records import ConfigItemRecord

AUDIT_ACTION = "config.set"
SYSTEM_OPERATOR = "system"


def load_all(session: Session) -> list[ConfigItemRecord]:
    """全部配置项，按键排序。"""
    return [ConfigItemRecord.from_row(row) for row in session.scalars(select(ConfigItem).order_by(ConfigItem.key))]


def get_item(session: Session, key: str) -> ConfigItemRecord | None:
    row = session.get(ConfigItem, key)
    return None if row is None else ConfigItemRecord.from_row(row)


def init_missing(session: Session, values: Mapping[str, Any], *, at_ms: int,
                 updated_by: str = SYSTEM_OPERATOR) -> list[str]:
    """把 ``values`` 里账本还没有的键写进去（已有的不覆盖），返回新增的键。不写审计：这不是操作员的改动。"""
    existing = set(session.scalars(select(ConfigItem.key)))
    added: list[str] = []
    for key, value in values.items():
        if key in existing:
            continue
        session.add(ConfigItem(key=key, value=value, updated_by=updated_by, updated_ms=at_ms))
        added.append(key)
    session.flush()
    return added


def set_item(session: Session, key: str, value: Any, *, operator: str, at_ms: int) -> ConfigItemRecord:
    """写入（或新建）一个配置项，并在同一事务里追加一条 ``config.set`` 审计记录
    （before/after 都是 ``{"value": ...}``，before 取自账本里的旧值；键原本不存在时 before 为空）。"""
    if not isinstance(key, str) or not key:
        raise TypeError(f"配置项的键必须是非空字符串：{key!r}")
    if not isinstance(operator, str) or not operator.strip():
        raise ValueError("operator 不能为空：配置改动必须留下是谁改的")
    row = session.get(ConfigItem, key)
    before: dict[str, Any] | None = None
    if row is None:
        row = ConfigItem(key=key, value=value, updated_by=operator, updated_ms=at_ms)
        session.add(row)
    else:
        before = {"value": row.value}
        row.value = value  # JSON 列不跟踪原地修改：整体重新赋值
        row.updated_by = operator
        row.updated_ms = at_ms
    session.flush()
    record = ConfigItemRecord.from_row(row)
    repo_audit.append(session, at_ms=at_ms, operator=operator, action=AUDIT_ACTION, target=key, before=before,
                      after={"value": record.value})
    return record
