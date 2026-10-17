"""初始结构：docs/设计约定.md 第 8 节的全部表、索引与审计日志只追加的触发器。

迁移是冻结的快照：不导入 models，列类型直接写库里的类型（Decimal、枚举、JSON 都是 TEXT）。
约束名与 models 的命名约定生成的一致；``tests/unit/ledger/test_ledger_migrations.py`` 比对两边建出来的库
（sqlite_master 里的建表语句逐字相同）。

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# 一仓一主：同一「合约 + 方向」同一时刻只有一条非 closed 的持仓记录
POSITION_ACTIVE_KEY = "uq_position_active_key"
POSITION_ACTIVE_WHERE = "status != 'closed'"
# 审计日志只追加：拒绝一切 UPDATE 与 DELETE（与 models.AUDIT_LOG_TRIGGERS 逐字相同）
AUDIT_LOG_TRIGGERS = (
    "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log 只追加，不允许修改'); END",
    "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log 只追加，不允许删除'); END",
)


def upgrade() -> None:
    op.create_table(
        "meta",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_meta")),
    )
    op.create_table(
        "instrument",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("base", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("contract_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tick_size", sa.Text(), nullable=False),
        sa.Column("step_size", sa.Text(), nullable=False),
        sa.Column("min_qty", sa.Text(), nullable=False),
        sa.Column("min_notional", sa.Text(), nullable=False),
        sa.Column("market_step_size", sa.Text(), nullable=True),
        sa.Column("market_max_qty", sa.Text(), nullable=True),
        sa.Column("onboard_ms", sa.Integer(), nullable=True),
        sa.Column("updated_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", name=op.f("pk_instrument")),
    )
    op.create_table(
        "position",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("ownership", sa.Text(), nullable=False),
        sa.Column("is_mixed", sa.Boolean(), nullable=False),
        sa.Column("pending_reason", sa.Text(), nullable=True),
        sa.Column("pending_flags", sa.Text(), nullable=False),
        sa.Column("qty", sa.Text(), nullable=False),
        sa.Column("avg_price", sa.Text(), nullable=False),
        sa.Column("max_qty", sa.Text(), nullable=False),
        sa.Column("open_time_ms", sa.Integer(), nullable=True),
        sa.Column("open_time_estimated", sa.Boolean(), nullable=False),
        sa.Column("anchor_ms", sa.Integer(), nullable=True),
        sa.Column("stop_cycle", sa.Integer(), nullable=False),
        sa.Column("protection_state", sa.Text(), nullable=False),
        sa.Column("stop_price", sa.Text(), nullable=True),
        sa.Column("stop_client_id", sa.Text(), nullable=True),
        sa.Column("stop_algo_id", sa.Text(), nullable=True),
        sa.Column("stale_stops", sa.Text(), nullable=False),
        sa.Column("handed_over_stop", sa.Text(), nullable=True),
        sa.Column("stop_note", sa.Text(), nullable=False),
        sa.Column("exit_ruleset", sa.Text(), nullable=True),
        sa.Column("signal_exit", sa.Boolean(), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=True),
        sa.Column("margin_mode", sa.Text(), nullable=True),
        sa.Column("liquidation_price", sa.Text(), nullable=True),
        sa.Column("exchange_qty", sa.Text(), nullable=True),
        sa.Column("exchange_seen_ms", sa.Integer(), nullable=True),
        sa.Column("consistent", sa.Boolean(), nullable=True),
        sa.Column("origin_note", sa.Text(), nullable=False),
        sa.Column("opened_by_intent_id", sa.Integer(), nullable=True),
        sa.Column("list_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("list_direction", sa.Text(), nullable=True),
        sa.Column("list_score", sa.Text(), nullable=True),
        sa.Column("entry_reason", sa.Text(), nullable=False),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("closed_ms", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_ms", sa.Integer(), nullable=False),
        sa.Column("updated_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position")),
    )
    op.create_index(POSITION_ACTIVE_KEY, "position", ["symbol", "side"], unique=True,
                    sqlite_where=sa.text(POSITION_ACTIVE_WHERE))
    op.create_table(
        "intent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("idem_key", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("qty", sa.Text(), nullable=True),
        sa.Column("ratio", sa.Text(), nullable=True),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("state_reason", sa.Text(), nullable=False),
        sa.Column("created_ms", sa.Integer(), nullable=False),
        sa.Column("expires_ms", sa.Integer(), nullable=True),
        sa.Column("updated_ms", sa.Integer(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=True),
        sa.Column("op_id", sa.Text(), nullable=True),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("position_generation", sa.Integer(), nullable=True),
        sa.Column("parent_intent_id", sa.Integer(), nullable=True),
        sa.Column("ai_eval_ref", sa.Text(), nullable=True),
        sa.Column("reserved_slot", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intent")),
        sa.UniqueConstraint("idem_key", name=op.f("uq_intent_idem_key")),
    )
    op.create_table(
        "orders",
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("intent_id", sa.Integer(), nullable=True),
        sa.Column("leg", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("position_side", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("qty", sa.Text(), nullable=True),
        sa.Column("trigger_price", sa.Text(), nullable=True),
        sa.Column("working_type", sa.Text(), nullable=True),
        sa.Column("close_position", sa.Boolean(), nullable=False),
        sa.Column("exchange_order_id", sa.Text(), nullable=True),
        sa.Column("algo_id", sa.Text(), nullable=True),
        sa.Column("actual_order_id", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("raw_status", sa.Text(), nullable=True),
        sa.Column("filled_qty", sa.Text(), nullable=False),
        sa.Column("avg_price", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("sent_ms", sa.Integer(), nullable=True),
        sa.Column("acked_ms", sa.Integer(), nullable=True),
        sa.Column("last_query_ms", sa.Integer(), nullable=True),
        sa.Column("absent_count", sa.Integer(), nullable=False),
        sa.Column("resend_of", sa.Text(), nullable=True),
        sa.Column("created_ms", sa.Integer(), nullable=False),
        sa.Column("updated_ms", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["intent_id"], ["intent.id"], name=op.f("fk_orders_intent_id_intent")),
        sa.PrimaryKeyConstraint("client_order_id", name=op.f("pk_orders")),
    )
    op.create_index(op.f("ix_orders_algo_id"), "orders", ["algo_id"], unique=False)
    op.create_index(op.f("ix_orders_exchange_order_id"), "orders", ["exchange_order_id"], unique=False)
    op.create_table(
        "fill",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.Text(), nullable=False),
        sa.Column("exchange_order_id", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("position_side", sa.Text(), nullable=False),
        sa.Column("qty", sa.Text(), nullable=False),
        sa.Column("price", sa.Text(), nullable=False),
        sa.Column("commission", sa.Text(), nullable=False),
        sa.Column("commission_asset", sa.Text(), nullable=False),
        sa.Column("commission_usdt", sa.Text(), nullable=True),
        sa.Column("realized_pnl", sa.Text(), nullable=False),
        sa.Column("time_ms", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("intent_id", sa.Integer(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("created_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fill")),
        sa.UniqueConstraint("trade_id", name=op.f("uq_fill_trade_id")),
    )
    op.create_index(op.f("ix_fill_exchange_order_id"), "fill", ["exchange_order_id"], unique=False)
    op.create_table(
        "income",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("income_id", sa.Text(), nullable=False),
        sa.Column("income_type", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("amount", sa.Text(), nullable=False),
        sa.Column("asset", sa.Text(), nullable=False),
        sa.Column("time_ms", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("info", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_income")),
        sa.UniqueConstraint("income_id", name=op.f("uq_income_income_id")),
    )
    op.create_table(
        "trade",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("ownership", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("is_mixed", sa.Boolean(), nullable=False),
        sa.Column("open_ms", sa.Integer(), nullable=True),
        sa.Column("close_ms", sa.Integer(), nullable=False),
        sa.Column("open_time_estimated", sa.Boolean(), nullable=False),
        sa.Column("max_qty", sa.Text(), nullable=False),
        sa.Column("open_avg", sa.Text(), nullable=True),
        sa.Column("close_avg", sa.Text(), nullable=True),
        sa.Column("open_notional", sa.Text(), nullable=True),
        sa.Column("gross_pnl", sa.Text(), nullable=False),
        sa.Column("fees", sa.Text(), nullable=False),
        sa.Column("funding", sa.Text(), nullable=False),
        sa.Column("net_pnl", sa.Text(), nullable=False),
        sa.Column("entry_reason", sa.Text(), nullable=False),
        sa.Column("exit_reason", sa.Text(), nullable=False),
        sa.Column("list_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("list_direction", sa.Text(), nullable=True),
        sa.Column("list_score", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trade")),
        sa.UniqueConstraint("position_id", name=op.f("uq_trade_position_id")),
    )
    op.create_table(
        "list_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fetched_ms", sa.Integer(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("business_date", sa.Text(), nullable=True),
        sa.Column("scan_id", sa.Text(), nullable=True),
        sa.Column("valid", sa.Boolean(), nullable=True),
        sa.Column("valid_until_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_list_snapshot")),
    )
    op.create_table(
        "list_member",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=True),
        sa.Column("score", sa.Text(), nullable=True),
        sa.Column("first_selected_ms", sa.Integer(), nullable=True),
        sa.Column("tradable", sa.Boolean(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["list_snapshot.id"],
                                name=op.f("fk_list_member_snapshot_id_list_snapshot")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_list_member")),
    )
    op.create_table(
        "model_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("active_model", sa.Text(), nullable=False),
        sa.Column("gate", sa.Text(), nullable=False),
        sa.Column("suppressions", sa.Text(), nullable=False),
        sa.Column("op_locks", sa.Text(), nullable=False),
        sa.Column("op_lock_owner", sa.Text(), nullable=True),
        sa.Column("op_lock_expires_ms", sa.Integer(), nullable=True),
        sa.Column("run_mode", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("m2_started_ms", sa.Integer(), nullable=True),
        sa.Column("mismatch_since_ms", sa.Integer(), nullable=True),
        sa.Column("risk_detail", sa.Text(), nullable=True),
        sa.Column("updated_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_model_state_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_state")),
    )
    op.create_table(
        "model_switch_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at_ms", sa.Integer(), nullable=False),
        sa.Column("from_model", sa.Text(), nullable=True),
        sa.Column("to_model", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=True),
        sa.Column("operator", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_switch_log")),
    )
    op.create_table(
        "operation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("op_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=True),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("per_position", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("started_ms", sa.Integer(), nullable=False),
        sa.Column("ended_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operation")),
        sa.UniqueConstraint("op_id", name=op.f("uq_operation_op_id")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at_ms", sa.Integer(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_table(
        "reconcile_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at_ms", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconcile_run")),
    )
    op.create_table(
        "reconcile_diff",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("local_qty", sa.Text(), nullable=True),
        sa.Column("exchange_qty", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("resolved_ms", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["reconcile_run.id"], name=op.f("fk_reconcile_diff_run_id_reconcile_run")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconcile_diff")),
    )
    op.create_table(
        "alert",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at_ms", sa.Integer(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=False),
        sa.Column("delivered_ms", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert")),
    )
    op.create_table(
        "config_item",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("updated_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_config_item")),
    )
    op.create_table(
        "operator",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_ms", sa.Integer(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operator")),
        sa.UniqueConstraint("username", name=op.f("uq_operator_username")),
    )
    op.create_table(
        "session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=False),
        sa.Column("created_ms", sa.Integer(), nullable=False),
        sa.Column("last_active_ms", sa.Integer(), nullable=False),
        sa.Column("reauth_ms", sa.Integer(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("client_info", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_session_token_hash")),
        sa.ForeignKeyConstraint(["operator_id"], ["operator.id"], name=op.f("fk_session_operator_id_operator")),
    )
    op.create_table(
        "seat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("lease_until_ms", sa.Integer(), nullable=True),
        sa.Column("acquired_ms", sa.Integer(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_seat_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seat")),
    )
    op.create_table(
        "cooldown",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("until_ms", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("set_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", name=op.f("pk_cooldown")),
    )
    op.create_table(
        "candle",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        sa.Column("open_time_ms", sa.Integer(), nullable=False),
        sa.Column("open", sa.REAL(), nullable=False),
        sa.Column("high", sa.REAL(), nullable=False),
        sa.Column("low", sa.REAL(), nullable=False),
        sa.Column("close", sa.REAL(), nullable=False),
        sa.Column("volume", sa.REAL(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "timeframe", "open_time_ms", name=op.f("pk_candle")),
    )
    for statement in AUDIT_LOG_TRIGGERS:  # 审计日志只追加
        op.execute(statement)


def downgrade() -> None:
    # 删表时 SQLite 一并删掉表上的索引与触发器；先删引用别人的表
    op.drop_table("candle")
    op.drop_table("cooldown")
    op.drop_table("seat")
    op.drop_table("session")
    op.drop_table("operator")
    op.drop_table("config_item")
    op.drop_table("alert")
    op.drop_table("reconcile_diff")
    op.drop_table("reconcile_run")
    op.drop_table("audit_log")
    op.drop_table("operation")
    op.drop_table("model_switch_log")
    op.drop_table("model_state")
    op.drop_table("list_member")
    op.drop_table("list_snapshot")
    op.drop_table("trade")
    op.drop_table("income")
    op.drop_index(op.f("ix_fill_exchange_order_id"), table_name="fill")
    op.drop_table("fill")
    op.drop_index(op.f("ix_orders_algo_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_exchange_order_id"), table_name="orders")
    op.drop_table("orders")
    op.drop_table("intent")
    op.drop_index(POSITION_ACTIVE_KEY, table_name="position")
    op.drop_table("position")
    op.drop_table("instrument")
    op.drop_table("meta")
