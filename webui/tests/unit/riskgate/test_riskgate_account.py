"""风控闸：账户级（日亏损、权益峰值回撤）的锁存与复位、UTC 日切、事件与告警、数据流订阅。

判定本身（数值口径）由 algo.riskeval 的测试覆盖；这里测的是闸怎么用它：日初权益从哪来、什么时候换日、触发后锁存多久、
谁能复位、变化时发什么。默认配置：日亏损上限 50 USDT，回撤上限 10%，对账间隔 10 秒。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.events import AccountSnapshotTaken, RiskStateChanged
from dmr_kernel.core.riskgate import RiskGate
from dmr_kernel.core.types import AlertLevel, IntentAction
from dmr_kernel.ledger import repo_audit, repo_risk

D = Decimal
OPEN = IntentAction.OPEN


def _isolate_daily_loss(env):
    env.config["max_drawdown"] = D("90")  # 回撤上限放到够不着：只看日亏损


def _isolate_drawdown(env):
    env.config["daily_loss_limit"] = D("500")  # 日亏损上限放到够不着：只看回撤


# ---------------------------------------------------------------- 日初权益与峰值


async def test_first_reading_of_the_utc_day_becomes_the_day_start_equity(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("990"), t + 10_000)
    await gate.on_account_snapshot(D("1010"), t + 20_000)
    status = gate.status()
    assert status["day_start_equity"] == "1000"  # 当天第一个读数，之后不再变
    assert status["equity"] == "1010" and status["equity_at_ms"] == t + 20_000
    assert status["day"] == "2025-10-09"


async def test_peak_is_the_maximum_reading_and_never_goes_down(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    for i, equity in enumerate(["1000", "1030", "1010", "1020", "990"]):
        await gate.on_account_snapshot(D(equity), t + i * 10_000)
    assert gate.status()["peak_equity"] == "1030"


async def test_status_before_any_reading_has_no_numbers_and_is_stale(env):
    gate = await env.loaded_gate()
    status = gate.status()
    assert status["equity"] is None and status["day_start_equity"] is None and status["peak_equity"] is None
    assert status["daily_loss_remaining"] is None and status["drawdown_remaining_pct"] is None
    assert status["stale"] is True
    assert status["account_tripped"] is False and status["reasons"] == []


# ---------------------------------------------------------------- 日亏损


@pytest.mark.parametrize(("equity", "tripped"), [("950", True), ("949", True), ("950.01", False), ("1000", False),
                                                  ("1100", False)])
async def test_daily_loss_trips_at_the_limit_and_not_before(env, utc, equity, tripped):
    _isolate_daily_loss(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D(equity), t + 10_000)
    assert gate.status()["daily_loss_tripped"] is tripped
    assert gate.status()["drawdown_tripped"] is False


async def test_daily_loss_trip_publishes_one_event_and_one_critical_alert(env, utc):
    _isolate_daily_loss(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    assert await env.flush() == []  # 没触发：不发事件
    await gate.on_account_snapshot(D("940"), t + 10_000)
    await gate.on_account_snapshot(D("930"), t + 20_000)  # 已触发后继续亏：不重复发
    events = await env.flush()
    assert len(events) == 1
    assert isinstance(events[0], RiskStateChanged)
    assert events[0].account_tripped is True and events[0].model_tripped is False
    assert events[0].at_ms == t + 10_000
    assert len(events[0].reasons) == 1 and "日亏损" in events[0].reasons[0] and "60" in events[0].reasons[0]
    assert env.notifier.kinds() == ["risk_daily_loss_tripped"]
    assert env.notifier.alerts[0].level is AlertLevel.CRITICAL
    assert "日亏损" in env.notifier.alerts[0].message


async def test_the_daily_loss_trip_time_and_reason_are_fixed_at_the_moment_of_the_trip(env, utc):
    """触发时间与原因在触发那一刻写定：之后继续亏损或重新评估都不改写（status、判定的原因、落盘的状态三处一致）。"""
    _isolate_daily_loss(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("940"), t + 10_000)  # 亏 60：触发
    first = gate.status()
    assert first["daily_loss_tripped_ms"] == t + 10_000
    assert len(first["reasons"]) == 1 and "60" in first["reasons"][0]
    await gate.on_account_snapshot(D("900"), t + 20_000)  # 继续亏到 100
    later = gate.status()
    assert later["daily_loss_tripped_ms"] == t + 10_000
    assert later["reasons"] == first["reasons"]  # 原因里还是触发时的 60，不是现在的 100
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 25_000)
    assert decision.reason == first["reasons"][0]
    stored = await env.ledger.read(repo_risk.load_state)
    assert stored["daily_loss"] == {"tripped": True, "at_ms": t + 10_000, "reason": first["reasons"][0]}


async def test_daily_loss_stays_tripped_when_equity_recovers_within_the_day(env, utc):
    _isolate_daily_loss(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("940"), t + 10_000)
    await gate.on_account_snapshot(D("1005"), t + 20_000)  # 回到日初之上
    assert gate.status()["daily_loss_tripped"] is True
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 25_000)
    assert decision.allowed is False and decision.level == "account" and "日亏损" in decision.reason
    assert len(await env.flush()) == 1  # 没有因为回升而发出复位事件


async def test_utc_day_change_resets_the_daily_loss_and_rebases_the_day_start(env, utc):
    _isolate_daily_loss(env)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), utc(2025, 10, 9, 12))
    await gate.on_account_snapshot(D("940"), utc(2025, 10, 9, 12, 0, 10))
    assert gate.status()["daily_loss_tripped"] is True
    first_of_next_day = utc(2025, 10, 10, 0, 0, 3)
    await gate.on_account_snapshot(D("945"), first_of_next_day)
    status = gate.status()
    assert status["daily_loss_tripped"] is False and status["account_tripped"] is False
    assert status["day_start_equity"] == "945" and status["day"] == "2025-10-10"
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=first_of_next_day + 1000)).allowed is True
    events = await env.flush()
    assert [e.account_tripped for e in events] == [True, False] and events[1].reasons == ()
    assert env.notifier.kinds() == ["risk_daily_loss_tripped", "risk_daily_loss_reset"]
    assert env.notifier.alerts[1].level is AlertLevel.INFO


async def test_a_loss_after_the_day_change_is_measured_from_the_new_day_start(env, utc):
    _isolate_daily_loss(env)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), utc(2025, 10, 9, 23, 59, 50))
    await gate.on_account_snapshot(D("960"), utc(2025, 10, 10, 0, 0, 10))  # 新的一天从 960 起算，不是从 1000
    await gate.on_account_snapshot(D("915"), utc(2025, 10, 10, 0, 0, 20))  # 相对 960 亏 45：没到 50
    assert gate.status()["daily_loss_tripped"] is False
    await gate.on_account_snapshot(D("910"), utc(2025, 10, 10, 0, 0, 30))  # 亏 50
    assert gate.status()["daily_loss_tripped"] is True


# ---------------------------------------------------------------- UTC 日切（本机时区 +07 下也按 UTC 切）


async def test_day_change_follows_utc_and_not_the_local_time_zone(env, utc, local_tz_plus7):
    """本机 +07：UTC 17:00 是本地午夜，UTC 00:00 是本地早上 7 点。按本地日期切日的实现会在 17:00 UTC 换日、在 00:00 UTC 不换。"""
    _isolate_daily_loss(env)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), utc(2025, 10, 9, 12))
    await gate.on_account_snapshot(D("960"), utc(2025, 10, 9, 17, 0, 0))  # 本地午夜：UTC 还是同一天
    assert gate.status()["day_start_equity"] == "1000"
    assert gate.status()["day"] == "2025-10-09"
    await gate.on_account_snapshot(D("951"), utc(2025, 10, 9, 23, 59, 59, 999))  # 亏 49
    assert gate.status()["day_start_equity"] == "1000" and gate.status()["daily_loss_tripped"] is False
    await gate.on_account_snapshot(D("952"), utc(2025, 10, 10, 0, 0, 0, 0))  # UTC 午夜整：换日
    assert gate.status()["day_start_equity"] == "952"
    assert gate.status()["day"] == "2025-10-10"


async def test_day_change_is_exact_to_the_millisecond(env, utc):
    gate = await env.loaded_gate()
    midnight = utc(2025, 10, 10)
    await gate.on_account_snapshot(D("1000"), midnight - 1)
    await gate.on_account_snapshot(D("1001"), midnight - 1)
    assert gate.status()["day_start_equity"] == "1000"
    await gate.on_account_snapshot(D("1002"), midnight)
    assert gate.status()["day_start_equity"] == "1002"


# ---------------------------------------------------------------- 回撤


@pytest.mark.parametrize(("equity", "tripped"), [("901", False), ("900", True), ("899", True)])
async def test_drawdown_trips_at_the_limit_measured_from_the_peak(env, utc, equity, tripped):
    _isolate_drawdown(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D(equity), t + 10_000)
    assert gate.status()["drawdown_tripped"] is tripped
    assert gate.status()["daily_loss_tripped"] is False  # 日亏损 100 < 500


async def test_drawdown_trip_publishes_an_event_and_a_critical_alert(env, utc):
    _isolate_drawdown(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("890"), t + 10_000)
    events = await env.flush()
    assert len(events) == 1 and events[0].account_tripped is True
    assert "回撤" in events[0].reasons[0]
    assert env.notifier.kinds() == ["risk_drawdown_tripped"]
    assert env.notifier.alerts[0].level is AlertLevel.CRITICAL
    assert "人工复位" in env.notifier.alerts[0].message


async def test_the_drawdown_trip_time_and_reason_are_fixed_at_the_moment_of_the_trip(env, utc):
    _isolate_drawdown(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("890"), t + 10_000)  # 回撤 11%：触发
    first = gate.status()
    assert first["drawdown_tripped_ms"] == t + 10_000
    assert len(first["reasons"]) == 1 and "890" in first["reasons"][0]
    await gate.on_account_snapshot(D("850"), t + 20_000)  # 继续跌到 15%
    later = gate.status()
    assert later["drawdown_tripped_ms"] == t + 10_000
    assert later["reasons"] == first["reasons"] and "850" not in later["reasons"][0]
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 25_000)
    assert decision.reason == first["reasons"][0]
    stored = await env.ledger.read(repo_risk.load_state)
    assert stored["drawdown"] == {"tripped": True, "at_ms": t + 10_000, "reason": first["reasons"][0]}


async def test_drawdown_is_not_reset_by_recovery_a_new_day_or_a_new_peak(env, utc):
    _isolate_drawdown(env)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), utc(2025, 10, 9, 12))
    await gate.on_account_snapshot(D("890"), utc(2025, 10, 9, 12, 0, 10))
    assert gate.status()["drawdown_tripped"] is True
    await gate.on_account_snapshot(D("1200"), utc(2025, 10, 9, 13))  # 回升并创新高
    assert gate.status()["drawdown_tripped"] is True
    await gate.on_account_snapshot(D("1200"), utc(2025, 10, 10, 0, 0, 5))  # 换日
    assert gate.status()["drawdown_tripped"] is True
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=utc(2025, 10, 10, 0, 0, 6))
    assert decision.allowed is False and decision.level == "account" and "回撤" in decision.reason
    assert len(await env.flush()) == 1  # 只有触发那一次


async def test_drawdown_is_not_reset_by_raising_the_limit(env, utc):
    _isolate_drawdown(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("890"), t + 10_000)
    env.config["max_drawdown"] = D("50")
    await gate.on_account_snapshot(D("890"), t + 20_000)
    assert gate.status()["drawdown_tripped"] is True


async def test_reset_drawdown_clears_the_latch_rebases_the_peak_and_writes_the_audit_log(env, utc):
    _isolate_drawdown(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("900"), t + 10_000)
    assert gate.status()["drawdown_tripped"] is True
    env.clock.set(t + 15_000)
    await gate.reset_drawdown(operator="alice")
    status = gate.status()
    assert status["drawdown_tripped"] is False and status["account_tripped"] is False
    assert status["peak_equity"] == "900"  # 以复位时的权益为新的峰值，否则下一个读数立刻又触发
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 16_000)).allowed is True  # 读数还新，复位后放行
    await gate.on_account_snapshot(D("890"), t + 20_000)  # 相对新峰值回撤约 1.1%
    assert gate.status()["drawdown_tripped"] is False
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 21_000)).allowed is True
    await gate.on_account_snapshot(D("810"), t + 30_000)  # 相对新峰值回撤 10%
    assert gate.status()["drawdown_tripped"] is True

    rows = await env.ledger.read(repo_audit.recent, action="risk.reset_drawdown")
    assert len(rows) == 1
    audit = rows[0]
    assert audit.operator == "alice" and audit.at_ms == t + 15_000 and audit.target == "risk" and audit.result == "ok"
    assert audit.before["drawdown"]["tripped"] is True and audit.after["drawdown"]["tripped"] is False

    events = await env.flush()
    assert [e.account_tripped for e in events] == [True, False, True]
    assert env.notifier.kinds() == ["risk_drawdown_tripped", "risk_drawdown_reset", "risk_drawdown_tripped"]
    assert env.notifier.alerts[1].level is AlertLevel.INFO and "alice" in env.notifier.alerts[1].message


async def test_reset_drawdown_when_nothing_is_tripped_changes_nothing_but_is_audited(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("1100"), t + 10_000)
    await gate.on_account_snapshot(D("1050"), t + 20_000)  # 离峰值 4.5%，没到 10%：没触发，而且当前权益低于峰值
    before = gate.status()
    assert before["peak_equity"] == "1100" and before["drawdown_tripped"] is False
    await gate.reset_drawdown(operator="bob")
    assert gate.status() == before  # 峰值也不动：不能用复位把峰值拉低到当前权益，绕过回撤限额
    assert (await env.ledger.read(repo_risk.load_state))["peak_equity"] == "1100"
    rows = await env.ledger.read(repo_audit.recent, action="risk.reset_drawdown")
    assert [(r.operator, r.result) for r in rows] == [("bob", "noop")]
    assert await env.flush() == [] and env.notifier.alerts == []


async def test_reset_drawdown_does_not_clear_the_daily_loss(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("800"), t + 10_000)  # 日亏损 200 与回撤 20% 同时触发
    status = gate.status()
    assert status["daily_loss_tripped"] and status["drawdown_tripped"] and len(status["reasons"]) == 2
    await gate.reset_drawdown(operator="alice")
    status = gate.status()
    assert status["drawdown_tripped"] is False and status["daily_loss_tripped"] is True
    assert status["account_tripped"] is True and len(status["reasons"]) == 1 and "日亏损" in status["reasons"][0]
    events = await env.flush()
    assert [e.account_tripped for e in events] == [True, True]  # 回撤那一项变了，账户级仍触发
    assert len(events[0].reasons) == 2 and len(events[1].reasons) == 1


async def test_reset_drawdown_records_the_operator_name_without_surrounding_spaces(env, utc):
    _isolate_drawdown(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("890"), t + 10_000)
    await gate.reset_drawdown(operator="  alice \n")
    rows = await env.ledger.read(repo_audit.recent, action="risk.reset_drawdown")
    assert [r.operator for r in rows] == ["alice"]
    assert "alice" in env.notifier.alerts[-1].message and "  alice" not in env.notifier.alerts[-1].message


@pytest.mark.parametrize("operator", ["", "   "])
async def test_reset_drawdown_needs_a_named_operator(env, utc, operator):
    _isolate_drawdown(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("890"), t + 10_000)
    with pytest.raises(ValueError):
        await gate.reset_drawdown(operator=operator)
    assert gate.status()["drawdown_tripped"] is True
    assert await env.ledger.read(repo_audit.recent, action="risk.reset_drawdown") == []


# ---------------------------------------------------------------- 读数的处理


async def test_a_late_older_reading_is_ignored(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t + 20_000)
    await gate.on_account_snapshot(D("900"), t + 10_000)  # 乱序到达的旧读数
    status = gate.status()
    assert status["equity"] == "1000" and status["equity_at_ms"] == t + 20_000
    assert status["daily_loss_tripped"] is False and status["drawdown_tripped"] is False


async def test_a_late_reading_from_the_previous_day_cannot_roll_the_day_back(env, utc):
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), utc(2025, 10, 9, 23, 59, 40))
    await gate.on_account_snapshot(D("980"), utc(2025, 10, 10, 0, 0, 5))
    await gate.on_account_snapshot(D("700"), utc(2025, 10, 9, 23, 59, 55))
    status = gate.status()
    assert status["day"] == "2025-10-10" and status["day_start_equity"] == "980" and status["equity"] == "980"
    assert status["account_tripped"] is False


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), D("0"), D("-5")])
async def test_invalid_readings_are_ignored_and_do_not_poison_the_baseline(env, utc, bad):
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(bad, t)
    assert gate.status()["equity"] is None and gate.status()["day_start_equity"] is None
    await gate.on_account_snapshot(D("1000"), t + 10_000)
    await gate.on_account_snapshot(bad, t + 20_000)
    status = gate.status()
    assert status["equity"] == "1000" and status["equity_at_ms"] == t + 10_000  # 无效读数不刷新「最近更新」
    assert status["day_start_equity"] == "1000" and status["peak_equity"] == "1000"


async def test_invalid_reading_does_not_keep_the_snapshot_fresh(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("0"), t + 15_000)
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 25_000)  # 最近有效读数已 25 秒 > 2×10
    assert decision.allowed is False and decision.level == "stale"


# ---------------------------------------------------------------- 加载与订阅


async def test_snapshots_before_load_are_a_programming_error(env, utc):
    gate = env.new_gate()
    with pytest.raises(RuntimeError):
        await gate.on_account_snapshot(D("1000"), utc(2025, 10, 9, 12))
    with pytest.raises(RuntimeError):
        await gate.reset_drawdown(operator="alice")
    with pytest.raises(RuntimeError):
        await gate.start()  # 没加载就订阅：读数来了也没有状态可用
    t = utc(2025, 10, 9, 12)
    env.bus.publish(AccountSnapshotTaken(equity=D("1000"), available=D("900"), wallet=D("990"), at_ms=t))
    await env.flush()
    assert gate.status()["equity"] is None  # 没有订阅上


async def test_start_subscribes_to_account_snapshots_and_stop_unsubscribes(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.start()
    env.bus.publish(AccountSnapshotTaken(equity=D("1000"), available=D("900"), wallet=D("990"), at_ms=t))
    await env.flush()
    assert gate.status()["equity"] == "1000" and gate.status()["day_start_equity"] == "1000"
    await gate.stop()
    env.bus.publish(AccountSnapshotTaken(equity=D("500"), available=D("400"), wallet=D("490"), at_ms=t + 10_000))
    await env.flush()
    assert gate.status()["equity"] == "1000"


async def test_a_snapshot_event_that_trips_the_gate_publishes_the_risk_event(env, utc):
    _isolate_daily_loss(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.start()
    env.bus.publish(AccountSnapshotTaken(equity=D("1000"), available=D("900"), wallet=D("990"), at_ms=t))
    env.bus.publish(AccountSnapshotTaken(equity=D("920"), available=D("820"), wallet=D("910"), at_ms=t + 10_000))
    events = await env.flush()
    assert [(e.account_tripped, e.model_tripped) for e in events] == [(True, False)]
    await gate.stop()


async def test_the_gate_works_without_a_notifier(env, utc):
    _isolate_daily_loss(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate(notifier=False)
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("900"), t + 10_000)
    assert gate.status()["daily_loss_tripped"] is True
    assert len(await env.flush()) == 1 and env.notifier.alerts == []


class _BrokenNotifier:
    """告警通道坏了：每次提出告警都抛异常。"""

    def __init__(self) -> None:
        self.calls = 0

    def raise_alert(self, level, kind, message, *, dedupe_key=None):
        self.calls += 1
        raise RuntimeError("告警通道坏了")


async def test_a_failing_alert_channel_never_disturbs_the_risk_state(env, utc):
    """告警是附带的：它出错，触发、事件、落盘、人工复位都照常，不能让风控状态跟着出错。"""
    _isolate_drawdown(env)
    t = utc(2025, 10, 9, 12)
    notifier = _BrokenNotifier()
    gate = RiskGate(env.ledger, env.bus, env.clock, config_get=env.config.__getitem__, notifier=notifier)
    await gate.load()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("890"), t + 10_000)  # 回撤触发；告警抛异常也不能传出来
    assert gate.status()["drawdown_tripped"] is True
    assert (await env.ledger.read(repo_risk.load_state))["drawdown"]["tripped"] is True  # 照样落盘
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 11_000)
    assert decision.allowed is False and decision.level == "account"
    await gate.reset_drawdown(operator="alice")  # 复位的告警同样不能影响复位
    assert gate.status()["drawdown_tripped"] is False
    assert (await env.ledger.read(repo_risk.load_state))["drawdown"]["tripped"] is False
    assert [e.account_tripped for e in await env.flush()] == [True, False]
    assert notifier.calls == 2  # 两次告警都试过了


# ---------------------------------------------------------------- 限额没配置：按未知处理，不新增风险


async def test_unset_limits_do_not_break_snapshots_and_block_new_risk(env, utc):
    env.config["daily_loss_limit"] = None
    env.config["max_drawdown"] = None
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("500"), t + 10_000)
    status = gate.status()
    assert status["equity"] == "500" and status["day_start_equity"] == "1000" and status["peak_equity"] == "1000"
    assert status["daily_loss_remaining"] is None and status["drawdown_remaining_pct"] is None
    assert status["limits_missing"] == ["daily_loss_limit", "max_drawdown"]
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 11_000)
    assert decision.allowed is False and decision.level == "account" and "未配置" in decision.reason
    assert (await gate.evaluate(model="m1", action=IntentAction.CLOSE, now_ms=t + 11_000)).allowed is True
    # 补上限额后，下一个读数按已经记下的日初权益与峰值正常判定
    env.config["daily_loss_limit"] = D("50")
    env.config["max_drawdown"] = D("90")
    await gate.on_account_snapshot(D("500"), t + 20_000)
    assert gate.status()["daily_loss_tripped"] is True
    assert gate.status()["limits_missing"] == []


@pytest.mark.parametrize("limit", [50, "50", D("50")])
async def test_limits_may_come_as_int_str_or_decimal(env, utc, limit):
    env.config["daily_loss_limit"] = limit
    _isolate_daily_loss(env)
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    await gate.on_account_snapshot(D("1000"), t)
    await gate.on_account_snapshot(D("950"), t + 10_000)
    assert gate.status()["daily_loss_tripped"] is True
