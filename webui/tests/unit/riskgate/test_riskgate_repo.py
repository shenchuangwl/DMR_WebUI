"""ledger.repo_risk：risk_state 的读写、与审计日志同一事务落盘、模型级判定用的当日汇总。"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy.exc import StatementError

from dmr_kernel.core.types import Ownership, PositionStatus
from dmr_kernel.ledger import repo_audit, repo_meta, repo_risk
from dmr_kernel.ledger.records import AuditLogRecord

D = Decimal
DAY = 86_400_000


# ---------------------------------------------------------------- risk_state


async def test_load_state_is_none_until_something_is_saved(env):
    assert await env.ledger.read(repo_risk.load_state) is None


async def test_state_lives_in_meta_under_the_risk_state_key_and_round_trips(env):
    state = {"v": 1, "day_ms": 5 * DAY, "day_start_equity": D("1000.50"), "peak_equity": D("1001"),
             "drawdown": {"tripped": True, "at_ms": 7, "reason": "回撤"}}
    await env.ledger.write(repo_risk.save_state, state)
    loaded = await env.ledger.read(repo_risk.load_state)
    # Decimal 按账本统一的 JSON 写法存成字符串；其余原样
    assert loaded == {"v": 1, "day_ms": 5 * DAY, "day_start_equity": "1000.50", "peak_equity": "1001",
                      "drawdown": {"tripped": True, "at_ms": 7, "reason": "回撤"}}
    assert repo_risk.RISK_STATE_KEY == "risk_state"
    assert json.loads(await env.ledger.read(repo_meta.get, "risk_state")) == loaded


async def test_save_state_overwrites_the_previous_value(env):
    await env.ledger.write(repo_risk.save_state, {"v": 1, "peak_equity": "1000"})
    await env.ledger.write(repo_risk.save_state, {"v": 1, "peak_equity": "1200"})
    assert await env.ledger.read(repo_risk.load_state) == {"v": 1, "peak_equity": "1200"}


async def test_save_state_refuses_a_non_mapping(env):
    with pytest.raises(TypeError):
        await env.ledger.write(repo_risk.save_state, ["not", "a", "mapping"])
    assert await env.ledger.read(repo_risk.load_state) is None


async def test_save_state_with_audit_writes_state_and_audit_row_together(env):
    record = await env.ledger.write(
        repo_risk.save_state_with_audit, {"v": 1, "peak_equity": D("900")}, at_ms=123, operator="alice",
        action="risk.reset_drawdown", before={"tripped": True}, after={"tripped": False})
    assert isinstance(record, AuditLogRecord)
    assert (record.at_ms, record.operator, record.action, record.target, record.result) == (
        123, "alice", "risk.reset_drawdown", "risk", "ok")
    assert record.before == {"tripped": True} and record.after == {"tripped": False}
    assert await env.ledger.read(repo_risk.load_state) == {"v": 1, "peak_equity": "900"}
    assert [r.id for r in await env.ledger.read(repo_audit.recent, action="risk.reset_drawdown")] == [record.id]


async def test_save_state_with_audit_is_one_transaction(env):
    """审计行写不进去（这里让 before 无法转成 JSON）时，状态也不能落盘：复位既留痕又生效，要么都有要么都没有。

    JSON 列在绑定参数时抛的 TypeError，按账本的惯例（见 test_ledger_schema 的 JSON 列用例）被 SQLAlchemy 包进
    StatementError，原始异常在 ``orig``。状态是先于审计行写入同一个会话的，所以这里同时证明了回滚会撤销已经写入的状态。
    """
    await env.ledger.write(repo_risk.save_state, {"v": 1, "peak_equity": "1000"})
    with pytest.raises(StatementError) as info:
        await env.ledger.write(repo_risk.save_state_with_audit, {"v": 1, "peak_equity": "900"}, at_ms=1,
                               operator="alice", action="risk.reset_drawdown", before=object())
    assert isinstance(info.value.orig, TypeError)
    assert await env.ledger.read(repo_risk.load_state) == {"v": 1, "peak_equity": "1000"}
    assert await env.ledger.read(repo_audit.recent, action="risk.reset_drawdown") == []


async def test_save_state_with_audit_writes_nothing_when_the_state_itself_cannot_be_stored(env):
    """反方向：状态写不进去（不是合法 JSON）时，审计行也不能留下，旧状态原样不动。"""
    await env.ledger.write(repo_risk.save_state, {"v": 1, "peak_equity": "1000"})
    with pytest.raises(TypeError):  # put_json 在 Python 里就拒绝了，没走到数据库
        await env.ledger.write(repo_risk.save_state_with_audit, {"v": 1, "peak_equity": object()}, at_ms=1,
                               operator="alice", action="risk.reset_drawdown", before={"tripped": True})
    assert await env.ledger.read(repo_risk.load_state) == {"v": 1, "peak_equity": "1000"}
    assert await env.ledger.read(repo_audit.recent, action="risk.reset_drawdown") == []


# ---------------------------------------------------------------- 当日汇总（模型级判定的输入）


def _stats(env, day_start: int):
    return env.ledger.read(repo_risk.model_day_stats, day_start_ms=day_start, day_end_ms=day_start + DAY)


async def test_day_stats_of_an_empty_ledger_are_zero(env, utc):
    stats = await _stats(env, utc(2025, 10, 9))
    assert stats == {"strategy_net_pnl": D("0"), "strategy_closed_trades": 0, "strategy_opens": 0}


async def test_day_stats_sum_strategy_trades_closed_in_the_day_exactly(env, utc):
    day = utc(2025, 10, 9)
    await env.seed.trade(close_ms=day - 1, net_pnl="-7")  # 前一天最后一毫秒：不算
    await env.seed.trade(close_ms=day, net_pnl="-10.25")  # 当天第一毫秒：算
    await env.seed.trade(close_ms=day + DAY - 1, net_pnl="3.5")  # 当天最后一毫秒：算
    await env.seed.trade(close_ms=day + DAY, net_pnl="-100")  # 次日第一毫秒：不算
    stats = await _stats(env, day)
    assert stats["strategy_net_pnl"] == D("-6.75")
    assert stats["strategy_closed_trades"] == 2


async def test_day_stats_ignore_takeover_and_ai_trades(env, utc):
    day = utc(2025, 10, 9)
    await env.seed.trade(close_ms=day + 1000, net_pnl="-5")
    await env.seed.trade(close_ms=day + 2000, net_pnl="-500", ownership=Ownership.TAKEOVER, model="m2")
    await env.seed.trade(close_ms=day + 3000, net_pnl="-700", ownership=Ownership.AI, model="m3")
    await env.seed.trade(close_ms=day + 4000, net_pnl="-900", ownership=Ownership.NONE, model=None)
    stats = await _stats(env, day)
    assert stats["strategy_net_pnl"] == D("-5") and stats["strategy_closed_trades"] == 1


async def test_day_stats_sum_is_exact_decimal_arithmetic(env, utc):
    day = utc(2025, 10, 9)
    for pnl in ("0.1", "0.2", "-0.3"):
        await env.seed.trade(close_ms=day + 1, net_pnl=pnl)
    assert (await _stats(env, day))["strategy_net_pnl"] == D("0")  # 浮点会得到 5.55e-17


async def test_day_stats_count_strategy_positions_opened_in_the_day(env, utc):
    day = utc(2025, 10, 9)
    await env.seed.position(open_ms=day - 1)  # 前一天：不算
    await env.seed.position(open_ms=day)  # 算
    await env.seed.position(open_ms=day + DAY - 1)  # 算
    await env.seed.position(open_ms=day + DAY)  # 次日：不算
    await env.seed.position(open_ms=day + 5, ownership=Ownership.TAKEOVER)  # 接管仓不算
    await env.seed.position(open_ms=day + 5, ownership=Ownership.AI)  # 模型 3 的仓不算
    await env.seed.position(open_ms=day + 5, ownership=Ownership.NONE, status=PositionStatus.PENDING)  # 待确认仓不算
    assert (await _stats(env, day))["strategy_opens"] == 2


async def test_day_stats_count_still_open_strategy_positions(env, utc):
    day = utc(2025, 10, 9)
    await env.seed.position(open_ms=day + 5, status=PositionStatus.MANAGED, symbol="ETHUSDT")
    await env.seed.position(open_ms=day + 6, status=PositionStatus.CLOSING, symbol="SOLUSDT")
    assert (await _stats(env, day))["strategy_opens"] == 2


async def test_day_stats_use_created_time_when_the_open_time_is_unknown(env, utc):
    day = utc(2025, 10, 9)
    await env.seed.position(open_ms=None, created_ms=day + 10)
    await env.seed.position(open_ms=None, created_ms=day - 10)
    assert (await _stats(env, day))["strategy_opens"] == 1
