"""账本：唯一约束、外键、只追加的审计日志、Decimal 精确往返、记录类型（计划 T0.3 用例 7、8、9、10）。"""
from __future__ import annotations

import dataclasses
import sqlite3
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import StatementError

from dmr_kernel.core.types import (
    FillSource,
    IntentAction,
    IntentSource,
    IntentState,
    OperationStatus,
    OperationType,
    OrderOrigin,
    OrderSide,
    Ownership,
    PositionSide,
    PositionStatus,
    ProtectionState,
)
from dmr_kernel.ledger import models, repo_audit
from dmr_kernel.ledger.errors import IntegrityError
from dmr_kernel.ledger.records import (
    RECORD_TYPES,
    AuditLogRecord,
    OperatorRecord,
    PositionRecord,
    SessionRecord,
)

T0 = 1_760_000_000_000


def _position(**over) -> models.Position:
    values = dict(symbol="BTCUSDT", side=PositionSide.LONG, generation=1, status=PositionStatus.MANAGED,
                  ownership=Ownership.STRATEGY, qty=Decimal("0.010"), avg_price=Decimal("60000.1"),
                  max_qty=Decimal("0.010"), created_ms=T0, updated_ms=T0)
    values.update(over)
    return models.Position(**values)


def _intent(n: int) -> models.Intent:
    return models.Intent(idem_key=f"open:m1:BTCUSDT:LONG:{n}", action=IntentAction.OPEN, model="m1",
                         source=IntentSource.SIGNAL, symbol="BTCUSDT", side=PositionSide.LONG,
                         state=IntentState.CREATED, created_ms=T0, updated_ms=T0)


def _fill(n: int) -> models.Fill:
    return models.Fill(trade_id=f"trade-{n}", exchange_order_id=f"ex-{n}", symbol="BTCUSDT", side=OrderSide.BUY,
                       position_side=PositionSide.LONG, qty=Decimal("0.01"), price=Decimal("60000"),
                       commission=Decimal("0.3"), commission_asset="USDT", realized_pnl=Decimal("0"), time_ms=T0,
                       source=FillSource.SYSTEM, origin=OrderOrigin.SYSTEM, path="stream", created_ms=T0)


def _income(n: int) -> models.Income:
    return models.Income(income_id=f"inc-{n}", income_type="FUNDING_FEE", symbol="BTCUSDT",
                         amount=Decimal("-0.0123"), asset="USDT", time_ms=T0)


def _operation(n: int) -> models.Operation:
    return models.Operation(op_id=f"op-{n}", op_type=OperationType.CLOSE_ALL, status=OperationStatus.ACCEPTED,
                            started_ms=T0)


def _operator(n: int) -> models.Operator:
    return models.Operator(username=f"user{n}", password_hash=f"scrypt${n}", role="admin", created_ms=T0)


def _session(n: int) -> models.OperatorSession:
    return models.OperatorSession(token_hash=f"hash-{n}", operator_id=1, created_ms=T0, last_active_ms=T0)


def _trade(n: int) -> models.Trade:
    return models.Trade(position_id=n, symbol="BTCUSDT", side=PositionSide.LONG, ownership=Ownership.STRATEGY,
                        close_ms=T0, max_qty=Decimal("0.01"), gross_pnl=Decimal("1"), fees=Decimal("0.1"),
                        funding=Decimal("0"), net_pnl=Decimal("0.9"), exit_reason="stop")


def _insert(session, factory, *args, **kwargs):
    obj = factory(*args, **kwargs)
    session.add(obj)
    session.flush()
    return getattr(obj, "id", None)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# ---------------------------------------------------------------- 用例 7：一仓一主（部分唯一索引）


def _close_active(session, symbol: str, side: PositionSide) -> None:
    session.execute(update(models.Position)
                    .where(models.Position.symbol == symbol, models.Position.side == side,
                           models.Position.status != PositionStatus.CLOSED)
                    .values(status=PositionStatus.CLOSED, closed_ms=T0))


def _reopen(session, position_id: int) -> None:
    session.execute(update(models.Position).where(models.Position.id == position_id)
                    .values(status=PositionStatus.MANAGED))


async def test_position_partial_unique_index(ledger):
    first = await ledger.write(_insert, _position, status=PositionStatus.MANAGED)
    with pytest.raises(IntegrityError):  # 同键第二条非 closed 记录被拒（不论什么状态）
        await ledger.write(_insert, _position, status=PositionStatus.PENDING, generation=2)
    await ledger.write(_insert, _position, side=PositionSide.SHORT)  # 另一方向是另一个键
    await ledger.write(_insert, _position, symbol="ETHUSDT")

    await ledger.write(_close_active, "BTCUSDT", PositionSide.LONG)
    await ledger.write(_insert, _position, generation=2)  # 一条 closed + 一条活动：允许
    await ledger.write(_close_active, "BTCUSDT", PositionSide.LONG)
    await ledger.write(_insert, _position, generation=3, status=PositionStatus.TAKING_OVER)  # 两条 closed 也不占键

    with pytest.raises(IntegrityError):  # 把已平的改回活动、而该键已有活动记录：同样被拒
        await ledger.write(_reopen, first)
    assert await ledger.read(_count, models.Position) == 5


# ---------------------------------------------------------------- 用例 8：唯一约束

UNIQUE_CASES = {
    "intent.idem_key": _intent,
    "fill.trade_id": _fill,
    "income.income_id": _income,
    "operation.op_id": _operation,
    "operator.username": _operator,
    "session.token_hash": _session,
    "trade.position_id": _trade,
}


@pytest.mark.parametrize("case", sorted(UNIQUE_CASES))
async def test_unique_constraints(ledger, case):
    factory = UNIQUE_CASES[case]
    if factory is _session:
        await ledger.write(_insert, _operator, 0)
    await ledger.write(_insert, factory, 1)
    await ledger.write(_insert, factory, 2)  # 不同的键可以
    with pytest.raises(IntegrityError):
        await ledger.write(_insert, factory, 1)  # 重复的键被拒
    model = type(factory(0))
    assert await ledger.read(_count, model) == 2


async def test_foreign_keys_are_enforced(ledger):
    def order(session, intent_id):
        session.add(models.Order(client_order_id=f"DKA1-1-{intent_id}-o", intent_id=intent_id, leg="o",
                                 purpose="open", symbol="BTCUSDT", side=OrderSide.BUY,
                                 position_side=PositionSide.LONG, kind="market", order_type="MARKET",
                                 qty=Decimal("0.01"), state="PENDING_SEND", created_ms=T0, updated_ms=T0))
        session.flush()

    with pytest.raises(IntegrityError):  # 引用不存在的意图
        await ledger.write(order, 999)
    intent_id = await ledger.write(_insert, _intent, 1)
    await ledger.write(order, intent_id)
    assert await ledger.read(_count, models.Order) == 1

    with pytest.raises(IntegrityError):  # 会话引用不存在的操作员
        await ledger.write(_insert, _session, 1)


# ---------------------------------------------------------------- 用例 9：审计日志只追加


async def test_audit_log_is_append_only(ledger, raw):
    record = await ledger.write(repo_audit.append, at_ms=T0, operator="alice", action="config.set",
                                target="leverage", before={"value": 2}, after={"value": 3}, request_id="r-1")
    assert isinstance(record, AuditLogRecord)

    def tamper_sql(session):
        session.execute(text("UPDATE audit_log SET result = 'tampered'"))

    def delete_sql(session):
        session.execute(text("DELETE FROM audit_log"))

    def tamper_orm(session, audit_id):
        session.get(models.AuditLog, audit_id).result = "tampered"
        session.flush()

    def delete_orm(session, audit_id):
        session.delete(session.get(models.AuditLog, audit_id))
        session.flush()

    with pytest.raises(IntegrityError, match="只追加"):
        await ledger.write(tamper_sql)
    with pytest.raises(IntegrityError, match="只追加"):
        await ledger.write(delete_sql)
    with pytest.raises(IntegrityError, match="只追加"):
        await ledger.write(tamper_orm, record.id)
    with pytest.raises(IntegrityError, match="只追加"):
        await ledger.write(delete_orm, record.id)

    outside = raw.connect()  # 绕过账本的连接同样被库里的触发器拒绝
    try:
        with pytest.raises(sqlite3.IntegrityError, match="只追加"):
            outside.execute("UPDATE audit_log SET operator = 'mallory'")
        with pytest.raises(sqlite3.IntegrityError, match="只追加"):
            outside.execute("DELETE FROM audit_log WHERE id = ?", (record.id,))
        outside.execute("INSERT INTO audit_log(at_ms, operator, action, target, result) "
                        "VALUES (?, 'bob', 'login', '', 'ok')", (T0 + 1,))  # 追加照常
    finally:
        outside.close()

    rows = await ledger.read(repo_audit.recent)
    assert [r.operator for r in rows] == ["bob", "alice"]
    assert rows[1] == record  # 原记录一字未改


def test_audit_repo_offers_no_update_or_delete():
    public = [name for name in vars(repo_audit) if not name.startswith("_") and callable(getattr(repo_audit, name))
              and getattr(getattr(repo_audit, name), "__module__", "") == repo_audit.__name__]
    assert set(public) == {"append", "recent"}


# ---------------------------------------------------------------- 用例 10：Decimal 精确往返


def _get_position(session, position_id: int) -> PositionRecord:
    return PositionRecord.from_row(session.get(models.Position, position_id))


async def test_decimal_round_trip_exact(ledger, raw):
    values = {
        "qty": Decimal("0.1") + Decimal("0.2"),
        "avg_price": Decimal("123456789012345678901234567890.123456789"),
        "max_qty": Decimal("0.0000001"),
        "stop_price": Decimal("-0.000500"),
        "liquidation_price": Decimal("1E+3"),
    }
    position_id = await ledger.write(_insert, _position, **values)
    record = await ledger.read(_get_position, position_id)
    for name, value in values.items():
        got = getattr(record, name)
        assert isinstance(got, Decimal), name
        assert got == value and str(got) == str(value), name  # 精确相等，连写法也不变
    assert record.qty == Decimal("0.3")
    assert raw.query("SELECT typeof(qty), qty FROM position WHERE id = ?", (position_id,)) == [("text", "0.3")]


@pytest.mark.parametrize("bad, error", [(0.3, TypeError), (True, TypeError), ("0.3", TypeError),
                                        (Decimal("NaN"), ValueError), (Decimal("Infinity"), ValueError)])
async def test_decimal_column_rejects_float_and_non_finite(ledger, bad, error):
    with pytest.raises(StatementError) as info:  # 不经 float；非有限值是程序错误
        await ledger.write(_insert, _position, qty=bad)
    assert isinstance(info.value.orig, error)
    assert await ledger.read(_count, models.Position) == 0


async def test_decimal_column_accepts_int(ledger):
    position_id = await ledger.write(_insert, _position, qty=3)
    record = await ledger.read(_get_position, position_id)
    assert record.qty == Decimal(3) and isinstance(record.qty, Decimal)


# ---------------------------------------------------------------- 枚举与 JSON 列


async def test_enum_and_json_columns_round_trip(ledger, raw):
    position_id = await ledger.write(
        _insert, _position, side="SHORT", status="pending", pending_flags=["notional", "中文标记"],
        stale_stops=[{"client_algo_id": "DKA1-0-1-s", "algo_id": "77"}], handed_over_stop={"price": Decimal("1.5")})
    record = await ledger.read(_get_position, position_id)
    assert record.side is PositionSide.SHORT
    assert record.status is PositionStatus.PENDING
    assert record.protection_state is ProtectionState.NONE  # 默认无保护
    assert record.pending_flags == ["notional", "中文标记"]
    assert record.stale_stops == [{"client_algo_id": "DKA1-0-1-s", "algo_id": "77"}]
    assert record.handed_over_stop == {"price": "1.5"}  # JSON 里的 Decimal 一律存成字符串
    stored = raw.query("SELECT side, status, pending_flags FROM position WHERE id = ?", (position_id,))
    assert stored == [("SHORT", "pending", '["notional","中文标记"]')]

    other_id = await ledger.write(_insert, _position, symbol="ETHUSDT")
    other = await ledger.read(_get_position, other_id)
    assert other.pending_flags == [] and other.stale_stops == [] and other.handed_over_stop is None


@pytest.mark.parametrize("field, bad", [("side", "UP"), ("status", "open"), ("ownership", "LONG")])
async def test_enum_column_rejects_unknown_value(ledger, field, bad):
    with pytest.raises(StatementError) as info:
        await ledger.write(_insert, _position, **{field: bad})
    assert isinstance(info.value.orig, ValueError)


async def test_json_column_rejects_unserializable_value(ledger):
    with pytest.raises(StatementError) as info:
        await ledger.write(_insert, _position, pending_flags=[object()])
    assert isinstance(info.value.orig, TypeError)
    with pytest.raises(StatementError):
        await ledger.write(_insert, _position, pending_flags=[float("nan")])


# ---------------------------------------------------------------- records.py


def test_every_table_has_a_frozen_record_with_the_same_fields():
    tables = models.Base.metadata.tables
    assert set(RECORD_TYPES) == set(tables)
    for name, table in tables.items():
        record_type = RECORD_TYPES[name]
        assert [f.name for f in dataclasses.fields(record_type)] == [c.name for c in table.columns], name
        assert record_type.__dataclass_params__.frozen, name
        assert callable(getattr(record_type, "from_row")), name


async def test_records_from_orm_objects_and_core_rows_agree(ledger):
    def both(session):
        # 赋值时用的是字符串和整数；记录里要统一成枚举和 Decimal
        session.add(_position(side="LONG", status="managed", qty=1, avg_price=Decimal("2.50")))
        session.flush()
        from_orm = PositionRecord.from_row(session.scalars(select(models.Position)).one())
        from_core = PositionRecord.from_row(session.execute(select(models.Position.__table__)).one())
        return from_orm, from_core

    from_orm, from_core = await ledger.write(both)
    assert from_orm == from_core
    assert from_orm.side is PositionSide.LONG
    assert from_orm.status is PositionStatus.MANAGED
    assert isinstance(from_orm.qty, Decimal) and from_orm.qty == 1
    assert from_orm.is_mixed is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        from_orm.qty = Decimal("2")  # type: ignore[misc]


async def test_record_json_values_are_copies(ledger):
    def build(session):
        obj = _position(pending_flags=["a"])
        session.add(obj)
        session.flush()
        record = PositionRecord.from_row(obj)
        obj.pending_flags.append("b")  # 改 ORM 对象不影响已生成的记录
        return record

    record = await ledger.write(build)
    assert record.pending_flags == ["a"]


def test_record_repr_hides_secrets():
    operator = OperatorRecord(id=1, username="alice", password_hash="scrypt$very-secret", role="admin",
                              created_ms=T0, disabled=False)
    session = SessionRecord(id=1, token_hash="token-hash-value", operator_id=1, created_ms=T0, last_active_ms=T0,
                            reauth_ms=None, revoked=False, client_info="")
    assert "very-secret" not in repr(operator)
    assert "token-hash-value" not in repr(session)
    assert "alice" in repr(operator)


async def test_record_helpers_check_the_table(ledger):
    from dmr_kernel.ledger.records import IntentRecord, MetaRecord, as_record

    def build(session):
        obj = _position()
        session.add(obj)
        session.flush()
        assert as_record(obj) == PositionRecord.from_row(obj)
        assert PositionRecord.from_row(session.execute(select(models.Position)).one()) == as_record(obj)  # 单实体行
        with pytest.raises(TypeError):
            IntentRecord.from_row(obj)  # 别的表的对象
        with pytest.raises(TypeError):
            as_record({"key": "x"})
        return MetaRecord.from_row({"key": "k", "value": "v"})  # 普通映射也可以

    assert await ledger.write(build) == MetaRecord(key="k", value="v")
