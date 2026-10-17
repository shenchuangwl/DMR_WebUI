"""Guard 与真实的风控闸（core.riskgate）接上：事件字段对得上，重启后无声恢复的触发状态在启动时对齐。"""
from __future__ import annotations

from decimal import Decimal

from dmr_kernel.core.riskgate import DAY_MS, RiskGate
from dmr_kernel.core.types import Suppression
from guard_kit import FakeNotifier
from tests.support.ledger import START_MS

RISK_CONFIG = {"reconcile_positions_s": 10, "daily_loss_limit": Decimal("50"), "max_drawdown": Decimal("10"),
               "daily_trade_limit": 50}
DAY_START = START_MS - START_MS % DAY_MS + 3_600_000  # START_MS 那个 UTC 日的 01:00，读数不会跨日


def make_gate(env) -> RiskGate:
    return RiskGate(env.ledger, env.bus, env.clock, config_get=RISK_CONFIG.__getitem__, notifier=FakeNotifier())


async def test_a_real_daily_loss_trip_and_its_utc_day_reset_flow_through_the_bus(make_env):
    env = make_env()
    await env.boot()
    gate = make_gate(env)
    await gate.load()

    await gate.on_account_snapshot(Decimal("1000"), DAY_START)
    await gate.on_account_snapshot(Decimal("940"), DAY_START + 10_000)  # 日亏损 60 > 50
    await env.bus.drain()
    assert env.state.suppressions == {Suppression.RISK_TRIPPED}

    await gate.on_account_snapshot(Decimal("940"), DAY_START + DAY_MS)  # 下一个 UTC 日的第一个读数：自动复位
    await env.bus.drain()
    assert env.state.suppressions == frozenset()


async def test_a_drawdown_latch_stays_until_the_manual_reset(make_env):
    env = make_env()
    await env.boot()
    gate = make_gate(env)
    await gate.load()
    await gate.on_account_snapshot(Decimal("1000"), DAY_START)
    await gate.on_account_snapshot(Decimal("890"), DAY_START + 10_000)  # 回撤 11% > 10%（同时日亏损 110 > 50）
    await env.bus.drain()

    await gate.on_account_snapshot(Decimal("890"), DAY_START + DAY_MS)  # 日亏损自动复位了，回撤还锁着
    await env.bus.drain()
    assert env.state.suppressions == {Suppression.RISK_TRIPPED}

    await gate.reset_drawdown(operator="alice")
    await env.bus.drain()
    assert env.state.suppressions == frozenset()


async def test_startup_aligns_with_a_latch_that_a_restarted_risk_gate_restored_silently(make_env):
    holder = {}
    env = make_env(risk_status=lambda: holder["gate"].status())
    await env.boot(subscribe=False)  # 上一次运行里 Guard 没在听（例如事件还没处理完进程就没了）
    before_restart = make_gate(env)
    await before_restart.load()
    await before_restart.on_account_snapshot(Decimal("1000"), DAY_START)
    await before_restart.on_account_snapshot(Decimal("890"), DAY_START + 10_000)
    after_restart = make_gate(env)
    await after_restart.load()  # 从账本恢复出锁存，但 load 不发事件
    holder["gate"] = after_restart
    assert after_restart.status()["account_tripped"] is True
    assert env.state.suppressions == frozenset()

    assert await env.guard.startup() == []

    assert env.state.suppressions == {Suppression.RISK_TRIPPED}
