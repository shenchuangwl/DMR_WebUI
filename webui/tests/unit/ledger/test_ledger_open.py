"""账本：建库与表结构、建库随机串、连接参数、目录与文件权限（计划 T0.3 用例 1、2）。"""
from __future__ import annotations

import re
import stat

import pytest
from sqlalchemy import text

from dmr_kernel.ledger import models, repo_meta
from dmr_kernel.ledger.coltypes import DecimalText, EnumText, JSONText
from dmr_kernel.ledger.writer import Ledger, LedgerError

# docs/设计约定.md 第 8 节：每张表、每一列，顺序照表抄录。
EXPECTED_COLUMNS: dict[str, list[str]] = {
    "meta": ["key", "value"],
    "instrument": ["symbol", "base", "quote", "contract_type", "status", "tick_size", "step_size", "min_qty",
                   "min_notional", "market_step_size", "market_max_qty", "onboard_ms", "updated_ms"],
    "position": ["id", "symbol", "side", "generation", "status", "ownership", "is_mixed", "pending_reason",
                 "pending_flags", "qty", "avg_price", "max_qty", "open_time_ms", "open_time_estimated", "anchor_ms",
                 "stop_cycle", "protection_state", "stop_price", "stop_client_id", "stop_algo_id", "stale_stops",
                 "handed_over_stop", "stop_note", "exit_ruleset", "signal_exit", "leverage", "margin_mode",
                 "liquidation_price", "exchange_qty", "exchange_seen_ms", "consistent", "origin_note",
                 "opened_by_intent_id", "list_snapshot_id", "list_direction", "list_score", "entry_reason",
                 "exit_reason", "closed_ms", "version", "created_ms", "updated_ms"],
    "intent": ["id", "idem_key", "action", "model", "source", "symbol", "side", "qty", "ratio", "params", "reason",
               "state", "state_reason", "created_ms", "expires_ms", "updated_ms", "operator", "op_id", "position_id",
               "position_generation", "parent_intent_id", "ai_eval_ref", "reserved_slot"],
    "orders": ["client_order_id", "intent_id", "leg", "purpose", "symbol", "side", "position_side", "kind",
               "order_type", "qty", "trigger_price", "working_type", "close_position", "exchange_order_id", "algo_id",
               "actual_order_id", "state", "raw_status", "filled_qty", "avg_price", "error_code", "error_msg",
               "sent_ms", "acked_ms", "last_query_ms", "absent_count", "resend_of", "created_ms", "updated_ms"],
    "fill": ["id", "trade_id", "exchange_order_id", "client_order_id", "symbol", "side", "position_side", "qty",
             "price", "commission", "commission_asset", "commission_usdt", "realized_pnl", "time_ms", "source",
             "origin", "position_id", "intent_id", "path", "created_ms"],
    "income": ["id", "income_id", "income_type", "symbol", "amount", "asset", "time_ms", "position_id", "info"],
    "trade": ["id", "position_id", "symbol", "side", "ownership", "model", "is_mixed", "open_ms", "close_ms",
              "open_time_estimated", "max_qty", "open_avg", "close_avg", "open_notional", "gross_pnl", "fees",
              "funding", "net_pnl", "entry_reason", "exit_reason", "list_snapshot_id", "list_direction",
              "list_score"],
    "list_snapshot": ["id", "fetched_ms", "http_status", "business_date", "scan_id", "valid", "valid_until_ms",
                      "status", "accepted", "reason", "member_count"],
    "list_member": ["id", "snapshot_id", "symbol", "direction", "score", "first_selected_ms", "tradable", "note"],
    "model_state": ["id", "active_model", "gate", "suppressions", "op_locks", "op_lock_owner", "op_lock_expires_ms",
                    "run_mode", "version", "m2_started_ms", "mismatch_since_ms", "risk_detail", "updated_ms"],
    "model_switch_log": ["id", "at_ms", "from_model", "to_model", "mode", "result", "op_id", "operator", "detail"],
    "operation": ["id", "op_id", "op_type", "scope", "status", "operator", "session_id", "state_version", "params",
                  "per_position", "result", "started_ms", "ended_ms"],
    "audit_log": ["id", "at_ms", "operator", "action", "target", "before", "after", "request_id", "result"],
    "reconcile_run": ["id", "at_ms", "kind", "status", "duration_ms", "detail"],
    "reconcile_diff": ["id", "run_id", "symbol", "side", "local_qty", "exchange_qty", "kind", "resolved_ms", "note"],
    "alert": ["id", "at_ms", "level", "kind", "message", "delivered", "delivered_ms", "attempts"],
    "config_item": ["key", "value", "updated_by", "updated_ms"],
    "operator": ["id", "username", "password_hash", "role", "created_ms", "disabled"],
    "session": ["id", "token_hash", "operator_id", "created_ms", "last_active_ms", "reauth_ms", "revoked",
                "client_info"],
    "seat": ["id", "session_id", "lease_until_ms", "acquired_ms"],
    "cooldown": ["symbol", "until_ms", "reason", "set_ms"],
    "candle": ["symbol", "timeframe", "open_time_ms", "open", "high", "low", "close", "volume"],
    "stop_history": ["id", "position_id", "price", "at_ms"],
}

# 第 8 节标了 (JSON) 的列
JSON_COLUMNS = {
    ("position", "pending_flags"), ("position", "stale_stops"), ("position", "handed_over_stop"),
    ("intent", "params"), ("model_state", "suppressions"), ("model_state", "op_locks"),
    ("model_state", "risk_detail"), ("operation", "params"), ("operation", "per_position"),
    ("operation", "result"), ("audit_log", "before"), ("audit_log", "after"), ("config_item", "value"),
}
# 价格、数量、金额（Decimal 存 TEXT）
DECIMAL_COLUMNS = {
    ("instrument", "tick_size"), ("instrument", "step_size"), ("instrument", "min_qty"),
    ("instrument", "min_notional"), ("instrument", "market_step_size"), ("instrument", "market_max_qty"),
    ("position", "qty"), ("position", "avg_price"), ("position", "max_qty"), ("position", "stop_price"),
    ("position", "liquidation_price"), ("position", "exchange_qty"), ("position", "list_score"),
    ("intent", "qty"), ("intent", "ratio"),
    ("orders", "qty"), ("orders", "trigger_price"), ("orders", "filled_qty"), ("orders", "avg_price"),
    ("fill", "qty"), ("fill", "price"), ("fill", "commission"), ("fill", "commission_usdt"),
    ("fill", "realized_pnl"), ("income", "amount"),
    ("trade", "max_qty"), ("trade", "open_avg"), ("trade", "close_avg"), ("trade", "open_notional"),
    ("trade", "gross_pnl"), ("trade", "fees"), ("trade", "funding"), ("trade", "net_pnl"), ("trade", "list_score"),
    ("list_member", "score"), ("reconcile_diff", "local_qty"), ("reconcile_diff", "exchange_qty"),
}
# 取 core.types 枚举值的列（枚举存 TEXT）
ENUM_COLUMNS = {
    ("position", "side"), ("position", "status"), ("position", "ownership"), ("position", "pending_reason"),
    ("position", "protection_state"), ("position", "exit_ruleset"), ("position", "exit_reason"),
    ("intent", "action"), ("intent", "source"), ("intent", "side"), ("intent", "state"),
    ("orders", "purpose"), ("orders", "side"), ("orders", "position_side"), ("orders", "working_type"),
    ("orders", "state"), ("fill", "side"), ("fill", "position_side"), ("fill", "source"), ("fill", "origin"),
    ("trade", "side"), ("trade", "ownership"), ("trade", "exit_reason"),
    ("model_state", "active_model"), ("model_state", "gate"), ("model_state", "run_mode"),
    ("operation", "op_type"), ("operation", "status"), ("alert", "level"),
}


def test_open_creates_all_tables_and_nonce(db_path, backup_dir, clock, raw):
    created_at = clock.now_ms()
    ledger = Ledger.open(db_path, backup_dir=backup_dir, clock=clock)
    try:
        assert ledger.path == db_path
        nonce = ledger.nonce
        assert re.fullmatch(r"[0-9a-z]{2}", nonce)
        tables = {r[0] for r in raw.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables == set(EXPECTED_COLUMNS) | {"alembic_version"}
        for table, columns in EXPECTED_COLUMNS.items():
            assert raw.columns(table) == columns, table
        assert ledger.read_sync(repo_meta.get, "ledger_nonce") == nonce
        assert ledger.read_sync(repo_meta.get, "created_ms") == str(created_at)
    finally:
        ledger.close_sync()

    clock.advance(3_600_000)
    again = Ledger.open(db_path, backup_dir=backup_dir, clock=clock)
    try:
        assert again.nonce == nonce  # 再次打开不变
        assert again.read_sync(repo_meta.get, "created_ms") == str(created_at)
    finally:
        again.close_sync()


def test_new_databases_get_their_own_nonce(tmp_path, clock):
    nonces = set()
    for i in range(8):
        ledger = Ledger.open(tmp_path / f"db{i}" / "ledger.sqlite", backup_dir=None, clock=clock)
        try:
            nonces.add(ledger.nonce)
        finally:
            ledger.close_sync()
    assert len(nonces) > 1  # 随机生成，不是固定值（8 个库全撞上的概率约 1296^-7）


def test_column_types_follow_conventions(ledger, raw):
    tables = models.Base.metadata.tables
    for table in tables.values():
        declared = {row[1]: row[2] for row in raw.query(f"PRAGMA table_info('{table.name}')")}
        for column in table.columns:
            if column.name.endswith("_ms") or column.name == "id":
                assert declared[column.name] == "INTEGER", (table.name, column.name)
            if isinstance(column.type, (DecimalText, JSONText, EnumText)):
                assert declared[column.name] == "TEXT", (table.name, column.name)
    for table_name, column_name in JSON_COLUMNS:
        assert isinstance(tables[table_name].c[column_name].type, JSONText), (table_name, column_name)
    for table_name, column_name in DECIMAL_COLUMNS:
        assert isinstance(tables[table_name].c[column_name].type, DecimalText), (table_name, column_name)
    for table_name, column_name in ENUM_COLUMNS:
        assert isinstance(tables[table_name].c[column_name].type, EnumText), (table_name, column_name)
    candle = {row[1]: row[2] for row in raw.query("PRAGMA table_info('candle')")}
    assert [candle[c] for c in ("open", "high", "low", "close", "volume")] == ["REAL"] * 5
    pk = [row[1] for row in sorted(raw.query("PRAGMA table_info('candle')"), key=lambda r: r[5]) if row[5]]
    assert pk == ["symbol", "timeframe", "open_time_ms"]


def _pragmas(session) -> dict[str, object]:
    names = ("journal_mode", "synchronous", "foreign_keys", "busy_timeout")
    return {name: session.execute(text(f"PRAGMA {name}")).scalar() for name in names}


async def test_pragmas(ledger):
    assert await ledger.write(_pragmas) == {
        "journal_mode": "wal", "synchronous": 2, "foreign_keys": 1, "busy_timeout": 5000}
    on_read = await ledger.read(_pragmas)
    assert on_read["journal_mode"] == "wal"
    assert on_read["busy_timeout"] == 5000
    assert on_read["foreign_keys"] == 1


def test_open_creates_private_directory_and_files(tmp_path, clock):
    path = tmp_path / "fresh" / "kernel" / "ledger.sqlite"
    ledger = Ledger.open(path, backup_dir=None, clock=clock)
    try:
        ledger.write_sync(repo_meta.put, "probe", "1")
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        for suffix in ("-wal", "-shm"):
            side_file = path.with_name(path.name + suffix)
            assert side_file.exists()
            assert stat.S_IMODE(side_file.stat().st_mode) == 0o600
    finally:
        ledger.close_sync()


def test_open_refuses_unknown_schema_revision(db_path, backup_dir, clock, raw):
    Ledger.open(db_path, backup_dir=backup_dir, clock=clock).close_sync()
    with raw.connect() as conn:
        conn.execute("UPDATE alembic_version SET version_num = '9999'")
    conn.close()
    with pytest.raises(LedgerError, match="9999"):
        Ledger.open(db_path, backup_dir=backup_dir, clock=clock)
    assert not backup_dir.exists() or list(backup_dir.iterdir()) == []
    assert raw.query("SELECT version_num FROM alembic_version") == [("9999",)]


def test_open_refuses_foreign_database(db_path, clock, raw):
    db_path.parent.mkdir(parents=True)
    with raw.connect() as conn:
        conn.execute("CREATE TABLE t(x)")
    conn.close()
    with pytest.raises(LedgerError, match="不是本程序建的库"):
        Ledger.open(db_path, backup_dir=None, clock=clock)
    assert [r[0] for r in raw.query("SELECT name FROM sqlite_master")] == ["t"]  # 原样没动
