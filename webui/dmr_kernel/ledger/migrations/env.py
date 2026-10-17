"""Alembic 迁移环境（程序内配置，不读 alembic.ini）。

只能经 ``dmr_kernel.ledger.migrate`` 在线执行：它打开迁移连接、开好事务（``BEGIN IMMEDIATE``），把连接放在
``config.attributes["connection"]``。连接已在事务里时 ``context.begin_transaction()`` 什么也不做，所以全部迁移
与版本号的更新都在这一个事务里，提交或回滚由 migrate 决定：一次升级要么全部生效，要么什么都没改。
SQLite 的结构变更用批量模式（``render_as_batch``，写迁移时用 ``op.batch_alter_table``）。
"""
from __future__ import annotations

from alembic import context

from dmr_kernel.ledger.models import Base

connection = context.config.attributes.get("connection")
if context.is_offline_mode() or connection is None:
    raise RuntimeError("账本迁移只能经 dmr_kernel.ledger.migrate 在线执行（不生成离线 SQL，不读 alembic.ini）")

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=True,
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
