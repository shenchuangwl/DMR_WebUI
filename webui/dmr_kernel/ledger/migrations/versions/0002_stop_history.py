"""止损价历史表 stop_history（M5：K 线页的止损线）。只追加；升级建表与索引，回退删表。

迁移是冻结的快照：不导入 models，列类型直接写库里的类型（Decimal 是 TEXT）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stop_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("price", sa.Text(), nullable=True),
        sa.Column("at_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stop_history")),
    )
    op.create_index(op.f("ix_stop_history_position_id"), "stop_history", ["position_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_stop_history_position_id"), table_name="stop_history")
    op.drop_table("stop_history")
