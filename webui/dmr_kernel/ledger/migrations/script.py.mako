"""${message}

SQLite 的结构变更用批量模式（op.batch_alter_table）。批量模式重建 audit_log 时触发器会随旧表删掉，要重新建。
迁移是冻结的快照：不导入 models，列类型直接写库里的类型。

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
