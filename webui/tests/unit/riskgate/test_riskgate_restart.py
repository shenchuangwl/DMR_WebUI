"""风控闸：状态存账本 meta 的 risk_state，重启后恢复（日初权益、峰值、锁存的触发与时间原因）。

「重启」= 关掉账本、用同一个库文件重新打开、新建一个闸。事件总线与告警记录器沿用，便于断言重启后发出了什么。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import Decimal

import pytest

from dmr_kernel.core.types import IntentAction
from dmr_kernel.ledger import repo_audit, repo_meta
from dmr_kernel.ledger.errors import LedgerClosed
from tests.support.ledger import open_ledger

D = Decimal
OPEN = IntentAction.OPEN
HOUR = 3_600_000


@contextmanager
def restarted(env):
    """关掉当前账本并重新打开同一个库文件；用完关闭。"""
    env.ledger.close_sync()
    reopened = open_ledger(env.tmp_path, env.clock)
    try:
        yield reopened
    finally:
        reopened.close_sync()


# ---------------------------------------------------------------- 存哪里、存什么


async def test_the_state_is_stored_under_meta_risk_state(env, utc):
    env.config["max_drawdown"] = D("90")
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    await gate.on_account_snapshot(D("940"), t + 10_000)  # 日亏损触发
    saved = json.loads(await env.ledger.read(repo_meta.get, "risk_state"))
    assert saved["day_start_equity"] == "1000" and saved["peak_equity"] == "1000"
    assert saved["daily_loss"]["tripped"] is True and saved["daily_loss"]["at_ms"] == t + 10_000
    assert "日亏损" in saved["daily_loss"]["reason"]
    assert saved["drawdown"]["tripped"] is False


async def test_nothing_is_stored_before_the_first_valid_reading(env):
    await env.loaded_gate()
    assert await env.ledger.read(repo_meta.get, "risk_state") is None


# ---------------------------------------------------------------- 恢复


async def test_a_restart_keeps_the_day_start_equity_within_the_same_utc_day(env, utc):
    env.config["max_drawdown"] = D("90")
    t = utc(2025, 10, 9, 12)
    await env.fresh_gate(t, "1000")
    with restarted(env) as ledger:
        gate = env.new_gate(ledger=ledger)
        await gate.load()
        await gate.on_account_snapshot(D("960"), t + HOUR)  # 重启后的第一个读数不能当成新的日初
        assert gate.status()["day_start_equity"] == "1000"
        assert gate.status()["daily_loss_tripped"] is False
        await gate.on_account_snapshot(D("950"), t + HOUR + 10_000)  # 相对持久化的日初亏 50
        assert gate.status()["daily_loss_tripped"] is True


async def test_a_restart_keeps_the_peak(env, utc):
    env.config["daily_loss_limit"] = D("500")
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    await gate.on_account_snapshot(D("1100"), t + 10_000)
    with restarted(env) as ledger:
        gate = env.new_gate(ledger=ledger)
        await gate.load()
        assert gate.status()["peak_equity"] == "1100"
        await gate.on_account_snapshot(D("990"), t + HOUR)  # 相对峰值 1100 回撤 10%
        assert gate.status()["drawdown_tripped"] is True


async def test_a_restart_keeps_a_tripped_drawdown_until_a_manual_reset(env, utc):
    env.config["daily_loss_limit"] = D("500")
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    await gate.on_account_snapshot(D("890"), t + 10_000)
    assert gate.status()["drawdown_tripped"] is True
    with restarted(env) as ledger:
        gate = env.new_gate(ledger=ledger)
        await gate.load()
        status = gate.status()
        assert status["drawdown_tripped"] is True and status["drawdown_tripped_ms"] == t + 10_000
        assert status["account_tripped"] is True and "回撤" in status["reasons"][0]
        decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + HOUR)
        assert decision.allowed is False and decision.level == "account" and "回撤" in decision.reason
        await gate.on_account_snapshot(D("2000"), t + HOUR)  # 创新高也不会自动解除
        assert gate.status()["drawdown_tripped"] is True
        await gate.reset_drawdown(operator="alice")
        assert gate.status()["drawdown_tripped"] is False
    with restarted(env) as ledger:  # 复位本身也要落盘
        gate = env.new_gate(ledger=ledger)
        await gate.load()
        assert gate.status()["drawdown_tripped"] is False and gate.status()["peak_equity"] == "2000"


async def test_a_restart_keeps_a_tripped_daily_loss_within_the_day_and_clears_it_on_a_new_utc_day(env, utc):
    env.config["max_drawdown"] = D("90")
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    await gate.on_account_snapshot(D("940"), t + 10_000)
    with restarted(env) as ledger:
        gate = env.new_gate(ledger=ledger)
        await gate.load()
        assert gate.status()["daily_loss_tripped"] is True
        await gate.on_account_snapshot(D("1000"), t + HOUR)  # 同一天回升：仍然锁着
        assert gate.status()["daily_loss_tripped"] is True
        await gate.on_account_snapshot(D("1001"), utc(2025, 10, 10, 0, 0, 2))  # 新的 UTC 日
        status = gate.status()
        assert status["daily_loss_tripped"] is False and status["day_start_equity"] == "1001"
    events = await env.flush()
    assert [e.account_tripped for e in events] == [True, False]


async def test_a_restart_on_a_later_day_rebases_the_day_but_keeps_peak_and_the_drawdown_latch(env, utc):
    env.config["daily_loss_limit"] = D("500")
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    await gate.on_account_snapshot(D("890"), t + 10_000)
    with restarted(env) as ledger:
        gate = env.new_gate(ledger=ledger)
        await gate.load()
        await gate.on_account_snapshot(D("950"), utc(2025, 10, 12, 9))
        status = gate.status()
        assert status["day"] == "2025-10-12" and status["day_start_equity"] == "950"
        assert status["peak_equity"] == "1000" and status["drawdown_tripped"] is True


async def test_after_a_restart_a_reading_stamped_before_the_stored_day_is_ignored(env, utc):
    """重启后第一个读数的时间早于账本里记的 UTC 日（时钟回拨，或迟到的旧读数）：忽略，不能把日期拨回去重新起算。"""
    await env.fresh_gate(utc(2025, 10, 10, 1), "1000")
    with restarted(env) as ledger:
        gate = env.new_gate(ledger=ledger)
        await gate.load()
        await gate.on_account_snapshot(D("700"), utc(2025, 10, 9, 23, 59, 50))  # 若被接受，日亏损 300 立刻触发
        status = gate.status()
        assert status["equity"] is None and status["day"] == "2025-10-10"
        assert status["day_start_equity"] == "1000" and status["account_tripped"] is False
        decision = await gate.evaluate(model="m1", action=OPEN, now_ms=utc(2025, 10, 10, 1, 0, 5))
        assert decision.level == "stale"  # 被忽略的读数不算「收到过读数」


async def test_a_manual_reset_right_after_a_restart_needs_a_reading_first(env, utc):
    """复位要以当前权益作新的峰值；刚重启还没有读数时没有「当前权益」：拒绝，不改状态、不留审计，等读数来了再复位。"""
    env.config["daily_loss_limit"] = D("500")
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    await gate.on_account_snapshot(D("890"), t + 10_000)
    with restarted(env) as ledger:
        gate = env.new_gate(ledger=ledger)
        await gate.load()
        with pytest.raises(RuntimeError, match="权益读数"):
            await gate.reset_drawdown(operator="alice")
        assert gate.status()["drawdown_tripped"] is True
        assert await ledger.read(repo_audit.recent, action="risk.reset_drawdown") == []
        await gate.on_account_snapshot(D("905"), t + HOUR)
        await gate.reset_drawdown(operator="alice")
        status = gate.status()
        assert status["drawdown_tripped"] is False and status["peak_equity"] == "905"
        assert [r.operator for r in await ledger.read(repo_audit.recent, action="risk.reset_drawdown")] == ["alice"]


async def test_after_a_restart_new_risk_waits_for_the_first_snapshot(env, utc):
    t = utc(2025, 10, 9, 12)
    await env.fresh_gate(t, "1000")
    with restarted(env) as ledger:
        gate = env.new_gate(ledger=ledger)
        await gate.load()
        decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000)  # 持久化的读数不算「新鲜」
        assert decision.allowed is False and decision.level == "stale" and "尚未收到" in decision.reason
        assert (await gate.evaluate(model="m1", action=IntentAction.CLOSE, now_ms=t + 1000)).allowed is True
        await gate.on_account_snapshot(D("1000"), t + 5_000)
        assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 6_000)).allowed is True


async def test_load_reads_the_ledger_again_each_time(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    other = env.new_gate()
    await other.load()
    await other.on_account_snapshot(D("1500"), t + 10_000)  # 另一个实例写了更高的峰值
    await gate.load()
    assert gate.status()["peak_equity"] == "1500"
    status = gate.status()  # 加载不带回旧的权益读数：要等第一个新读数才放行新增风险
    assert status["equity"] is None and status["stale"] is True
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 11_000)
    assert decision.allowed is False and decision.level == "stale" and "尚未收到" in decision.reason


# ---------------------------------------------------------------- 落盘失败时偏向保守


async def test_a_trip_is_effective_in_memory_even_when_the_state_cannot_be_written(env, utc):
    env.config["max_drawdown"] = D("90")
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    env.ledger.close_sync()
    with pytest.raises(LedgerClosed):
        await gate.on_account_snapshot(D("900"), t + 10_000)
    assert gate.status()["daily_loss_tripped"] is True
    decision = await gate.evaluate(model="m2", action=OPEN, now_ms=t + 11_000)
    assert decision.allowed is False and decision.level == "account"
    events = await env.flush()  # 触发照样发出去（只会更保守）
    assert [e.account_tripped for e in events] == [True]
    assert env.notifier.kinds() == ["risk_daily_loss_tripped"]


class _FlakyLedger:
    """转发给真实账本，但前 ``fail_writes`` 次写入抛异常（读不受影响）：模拟账本暂时写不进去，之后恢复。"""

    def __init__(self, inner, fail_writes: int) -> None:
        self._inner = inner
        self.fail_writes = fail_writes
        self.write_attempts = 0

    async def read(self, fn, *args, **kwargs):
        return await self._inner.read(fn, *args, **kwargs)

    async def write(self, fn, *args, **kwargs):
        self.write_attempts += 1
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise OSError("账本暂时写不进去")
        return await self._inner.write(fn, *args, **kwargs)


async def test_an_unsaved_change_is_retried_by_the_next_reading_even_if_that_reading_changes_nothing(env, utc):
    t = utc(2025, 10, 9, 12)
    flaky = _FlakyLedger(env.ledger, fail_writes=1)
    gate = env.new_gate(ledger=flaky)
    await gate.load()
    with pytest.raises(OSError):
        await gate.on_account_snapshot(D("1000"), t)  # 日初权益与峰值是新的，但没写进去
    assert await env.ledger.read(repo_meta.get, "risk_state") is None
    assert gate.status()["day_start_equity"] == "1000"  # 内存里已经生效
    await gate.on_account_snapshot(D("990"), t + 10_000)  # 这个读数本身不改日初、峰值、锁存，但要把上次没写的补上
    saved = json.loads(await env.ledger.read(repo_meta.get, "risk_state"))
    assert saved["day_start_equity"] == "1000" and saved["peak_equity"] == "1000"
    assert flaky.write_attempts == 2
    await gate.on_account_snapshot(D("985"), t + 20_000)  # 写成功以后不再重复写
    assert flaky.write_attempts == 2


async def test_a_reset_is_not_applied_when_it_cannot_be_written(env, utc):
    env.config["daily_loss_limit"] = D("500")
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    await gate.on_account_snapshot(D("890"), t + 10_000)
    env.ledger.close_sync()
    with pytest.raises(LedgerClosed):
        await gate.reset_drawdown(operator="alice")
    assert gate.status()["drawdown_tripped"] is True  # 没落盘的复位不生效：否则重启后锁又回来，内存与账本不一致


# ---------------------------------------------------------------- 损坏的状态：拒绝启动，不能悄悄当成「没有」


def _valid(**over):
    state = {"v": 1, "day_ms": 1_760_000_000_000 - 1_760_000_000_000 % 86_400_000, "day_start_equity": "1000",
             "peak_equity": "1000", "daily_loss": {"tripped": False, "at_ms": None, "reason": ""},
             "drawdown": {"tripped": False, "at_ms": None, "reason": ""}}
    state.update(over)
    return state


async def test_a_valid_state_written_by_hand_loads(env):
    await env.ledger.write(repo_meta.put_json, "risk_state", _valid())
    gate = await env.loaded_gate()
    assert gate.status()["day_start_equity"] == "1000"
    await env.ledger.write(repo_meta.put_json, "risk_state", {"v": 1})  # 最小的合法状态：什么都还没记
    gate = await env.loaded_gate()
    assert gate.status()["day_start_equity"] is None and gate.status()["account_tripped"] is False


@pytest.mark.parametrize("payload", [
    "just a string",
    [1, 2, 3],
    {},
    {"v": 2},
    _valid(day_ms="yesterday"),
    _valid(day_ms=-1),
    _valid(day_start_equity="abc"),
    _valid(day_start_equity="-5"),
    _valid(day_start_equity="NaN"),
    _valid(peak_equity="Infinity"),
    _valid(peak_equity=1000.5),
    _valid(day_ms=None),  # 日初权益有而日期没有：前后不一致
    _valid(day_start_equity=None),
    _valid(drawdown={"tripped": "yes", "at_ms": None, "reason": ""}),
    _valid(drawdown="tripped"),
    _valid(daily_loss={"tripped": True, "at_ms": "noon", "reason": ""}),
    _valid(daily_loss={"tripped": True, "at_ms": 1, "reason": 5}),
])
async def test_a_malformed_state_is_refused_at_load(env, payload):
    await env.ledger.write(repo_meta.put_json, "risk_state", payload)
    gate = env.new_gate()
    with pytest.raises(ValueError, match="risk_state"):
        await gate.load()


async def test_a_state_that_is_not_json_is_refused_at_load(env):
    await env.ledger.write(repo_meta.put, "risk_state", "{not json")
    with pytest.raises(ValueError, match="risk_state"):
        await env.new_gate().load()
