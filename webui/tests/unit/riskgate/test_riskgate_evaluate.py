"""风控闸 evaluate：只拦新增风险（逐个动作）、权益快照过期、账户级与模型级分开计、接管仓盈亏不计入模型级、status 的余量。

默认配置：日亏损上限 50 USDT，回撤上限 10%，日交易次数上限 50，对账间隔 10 秒（快照 20 秒没更新就算过期）。
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from dmr_kernel.core import limits as limits_module
from dmr_kernel.core import types as shared_types
from dmr_kernel.core.riskgate import RiskDecision
from dmr_kernel.core.types import AlertLevel, IntentAction, Model, Ownership

D = Decimal
OPEN = IntentAction.OPEN
MODELS = ["m1", "m2", "m3", "op"]
REDUCING = [a for a in IntentAction if not a.adds_risk]
ALLOWED = RiskDecision(True, "", "")


# ---------------------------------------------------------------- 判定的类型


async def test_the_decision_class_is_the_shared_one_from_core_types(env, utc):
    """风控闸与合约数上限返回同一个 RiskDecision（来自 core.types），上层可以统一判断；模块里仍保留这个名字。"""
    assert RiskDecision is shared_types.RiskDecision
    assert limits_module.RiskDecision is shared_types.RiskDecision
    t = utc(2025, 10, 9, 12)
    gate = await env.loaded_gate()
    refused = await gate.evaluate(model="m1", action=OPEN, now_ms=t)  # 还没收到任何读数：不放行
    await gate.on_account_snapshot(D("1000"), t)
    granted = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1_000)
    assert refused.allowed is False and granted.allowed is True
    assert type(refused) is shared_types.RiskDecision and type(granted) is shared_types.RiskDecision
    assert granted == limits_module.RiskDecision(True)  # 两个模块的放行判定可以直接比较


# ---------------------------------------------------------------- 只拦新增风险


def test_the_action_table_has_exactly_one_risk_adding_action():
    assert [a for a in IntentAction if a.adds_risk] == [OPEN]
    assert len(REDUCING) == len(IntentAction) - 1


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("action", REDUCING, ids=lambda a: a.value)
async def test_reducing_and_protective_actions_are_never_blocked(env, utc, model, action):
    """平仓、部分平仓、止损类（挂、改、撤）、撤单、改杠杆与保证金模式：账户级触发、快照过期、模型级触发、
    连账本都不可用了，也一律放行，而且不碰账本。"""
    env.config["daily_trade_limit"] = 3
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await gate.on_account_snapshot(D("700"), t + 10_000)  # 日亏损与回撤同时触发
    await env.seed.strategy_opens(3, at_ms=t)  # 模型 1 的日交易次数也到了
    assert gate.status()["account_tripped"] is True
    env.ledger.close_sync()
    far_later = t + 3_600_000  # 快照早已过期
    assert await gate.evaluate(model=model, action=action, now_ms=far_later) == ALLOWED


@pytest.mark.parametrize("model", MODELS)
async def test_new_risk_is_allowed_when_nothing_is_tripped(env, utc, model):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    assert await gate.evaluate(model=model, action=OPEN, now_ms=t + 5_000) == ALLOWED


@pytest.mark.parametrize("model", ["protection", "cli", "system"])
async def test_models_other_than_the_three_strategies_are_account_level_only(env, utc, model):
    env.config["daily_trade_limit"] = 1
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await env.seed.strategy_opens(1, at_ms=t)
    assert await gate.evaluate(model=model, action=OPEN, now_ms=t + 5_000) == ALLOWED
    await gate.on_account_snapshot(D("940"), t + 6_000)
    assert (await gate.evaluate(model=model, action=OPEN, now_ms=t + 7_000)).level == "account"


async def test_the_action_may_be_given_as_its_string_value(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    assert (await gate.evaluate(model="m1", action="close", now_ms=t + 1000)).allowed is True
    assert (await gate.evaluate(model="m1", action="open", now_ms=t + 1000)).allowed is True
    with pytest.raises(ValueError):
        await gate.evaluate(model="m1", action="teleport", now_ms=t + 1000)


async def test_the_model_may_be_given_as_the_model_enum_or_its_string_value(env, utc):
    """调用方可能传 Model.M1，也可能传 "m1"：模型级的约束对两种写法一样，不能因为写法不同就放过模型 1。"""
    env.config["daily_trade_limit"] = 1
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await env.seed.strategy_opens(1, at_ms=t)
    assert (await gate.evaluate(model=Model.M1, action=OPEN, now_ms=t + 1_000)).level == "model"
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1_000)).level == "model"
    assert await gate.evaluate(model=Model.M2, action=OPEN, now_ms=t + 1_000) == ALLOWED


async def test_an_unloaded_gate_blocks_new_risk_but_never_reduction(env, utc):
    gate = env.new_gate()  # 没有 load
    t = utc(2025, 10, 9, 12)
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t)
    assert decision.allowed is False and decision.level == "stale" and "尚未加载" in decision.reason
    assert await gate.evaluate(model="m1", action=IntentAction.CLOSE, now_ms=t) == ALLOWED


# ---------------------------------------------------------------- 账户级触发拦住所有模型的新增风险


@pytest.mark.parametrize("model", MODELS)
async def test_an_account_level_trip_blocks_new_risk_of_every_model(env, utc, model):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await gate.on_account_snapshot(D("940"), t + 10_000)
    decision = await gate.evaluate(model=model, action=OPEN, now_ms=t + 11_000)
    assert decision.allowed is False and decision.level == "account"
    assert "日亏损" in decision.reason


async def test_a_tripped_account_is_reported_before_a_stale_snapshot(env, utc):
    """触发是锁存的、需要人处理；过期只是等下一个读数。两者同时成立时报「账户级」，操作员才知道要去复位。"""
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await gate.on_account_snapshot(D("940"), t + 10_000)
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 3_600_000)
    assert decision.allowed is False and decision.level == "account"


# ---------------------------------------------------------------- 权益快照过期


async def test_new_risk_is_blocked_before_the_first_snapshot_arrives(env, utc):
    gate = await env.loaded_gate()
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=utc(2025, 10, 9, 12))
    assert decision.allowed is False and decision.level == "stale" and "尚未收到" in decision.reason


@pytest.mark.parametrize(("age_ms", "stale"), [(0, False), (5_000, False), (20_000, False), (20_001, True),
                                                (3_600_000, True)])
async def test_a_snapshot_older_than_twice_the_position_reconcile_interval_is_stale(env, utc, age_ms, stale):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + age_ms)
    assert decision.allowed is not stale
    assert decision.level == ("stale" if stale else "")
    if stale:
        assert "过期" in decision.reason


@pytest.mark.parametrize("model", MODELS)
async def test_a_stale_snapshot_blocks_new_risk_of_every_model(env, utc, model):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    decision = await gate.evaluate(model=model, action=OPEN, now_ms=t + 60_000)
    assert decision.allowed is False and decision.level == "stale"


async def test_the_stale_threshold_follows_the_configured_reconcile_interval(env, utc):
    env.config["reconcile_positions_s"] = 30
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 60_000)).allowed is True
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 60_001)).level == "stale"


async def test_a_new_snapshot_makes_the_gate_fresh_again(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 30_000)).level == "stale"
    await gate.on_account_snapshot(D("1000"), t + 30_000)
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 31_000)).allowed is True


async def test_status_reports_staleness_from_the_injected_clock(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    env.clock.set(t + 20_000)
    assert gate.status()["stale"] is False
    env.clock.set(t + 20_001)
    assert gate.status()["stale"] is True


# ---------------------------------------------------------------- 模型级：模型 1 的日交易次数


async def test_the_daily_trade_limit_blocks_model_1_at_the_limit(env, utc):
    env.config["daily_trade_limit"] = 3
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await env.seed.strategy_opens(2, at_ms=t - 3_600_000)
    assert await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000) == ALLOWED  # 2 次，还差 1 次
    await env.seed.strategy_opens(1, at_ms=t)
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 2000)
    assert decision.allowed is False and decision.level == "model"
    assert "日交易次数" in decision.reason and "3" in decision.reason


async def test_yesterdays_opens_are_not_counted_the_utc_day_decides(env, utc):
    env.config["daily_trade_limit"] = 2
    now = utc(2025, 10, 9, 0, 0, 30)
    gate = await env.fresh_gate(now - 10_000)
    await env.seed.strategy_opens(5, at_ms=utc(2025, 10, 8, 23, 59, 59, 999))  # 前一天最后一毫秒
    assert await gate.evaluate(model="m1", action=OPEN, now_ms=now) == ALLOWED
    await env.seed.strategy_opens(1, at_ms=utc(2025, 10, 9))  # 当天第一毫秒
    assert await gate.evaluate(model="m1", action=OPEN, now_ms=now) == ALLOWED  # 1 < 2
    await env.seed.strategy_opens(1, at_ms=utc(2025, 10, 9, 0, 0, 20))
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=now)).level == "model"


async def test_only_strategy_positions_count_as_opens(env, utc):
    env.config["daily_trade_limit"] = 2
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await env.seed.strategy_opens(4, at_ms=t, ownership=Ownership.TAKEOVER)
    await env.seed.strategy_opens(4, at_ms=t, ownership=Ownership.AI)
    await env.seed.strategy_opens(1, at_ms=t)
    assert await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000) == ALLOWED


# ---------------------------------------------------------------- 模型级：模型 1 当日策略仓已平交易的净盈亏


@pytest.mark.parametrize(("losses", "blocked"), [
    (["-30", "-20"], True),  # 合计 -50 ≤ -50
    (["-30", "-19.99"], False),
    (["-60", "10"], True),  # 盈利抵消后合计 -50
    (["-60", "10.01"], False),
    (["-49.99"], False),
    (["-50"], True),
    ([], False),
])
async def test_the_model_1_daily_loss_limit_uses_the_net_pnl_of_strategy_trades_closed_today(env, utc, losses, blocked):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    for i, pnl in enumerate(losses):
        await env.seed.trade(close_ms=t - 1000 * (i + 1), net_pnl=pnl)
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000)
    assert decision.allowed is not blocked
    if blocked:
        assert decision.level == "model" and "净亏损" in decision.reason


async def test_takeover_and_ai_pnl_is_not_counted_in_the_model_level(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await env.seed.trade(close_ms=t - 1000, net_pnl="-500", ownership=Ownership.TAKEOVER, model="m2")
    await env.seed.trade(close_ms=t - 2000, net_pnl="-500", ownership=Ownership.AI, model="m3")
    assert await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000) == ALLOWED
    await env.seed.trade(close_ms=t - 3000, net_pnl="-50")
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000)).level == "model"


async def test_yesterdays_pnl_is_not_counted_and_the_boundary_is_the_utc_midnight(env, utc):
    now = utc(2025, 10, 9, 0, 0, 30)
    gate = await env.fresh_gate(now - 10_000)
    await env.seed.trade(close_ms=utc(2025, 10, 8, 23, 59, 59, 999), net_pnl="-500")
    assert await gate.evaluate(model="m1", action=OPEN, now_ms=now) == ALLOWED
    await env.seed.trade(close_ms=utc(2025, 10, 9), net_pnl="-50")
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=now)).level == "model"


@pytest.mark.parametrize(("closed_at", "now", "blocked"), [
    # 本地 +07：UTC 16:30 是本地 23:30（10-09），UTC 17:30 是本地 00:30（10-10）。UTC 上同一天；按本地日期算就不是同一天
    ((2025, 10, 9, 16, 30), (2025, 10, 9, 17, 30), True),
    # UTC 23:30 与次日 00:30 在 UTC 上是两天；按本地日期算（本地 06:30 与 07:30，都是 10-10）就成了同一天
    ((2025, 10, 9, 23, 30), (2025, 10, 10, 0, 30), False),
])
async def test_the_model_level_day_is_utc_even_when_the_local_zone_is_plus_7(env, utc, local_tz_plus7, closed_at, now,
                                                                              blocked):
    now_ms = utc(*now)
    gate = await env.fresh_gate(now_ms - 10_000)
    await env.seed.trade(close_ms=utc(*closed_at), net_pnl="-50")
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=now_ms)
    assert decision.allowed is not blocked
    assert decision.level == ("model" if blocked else "")


async def test_the_model_level_resets_with_the_utc_day(env, utc):
    env.config["daily_trade_limit"] = 1
    t = utc(2025, 10, 9, 23, 59, 30)
    gate = await env.fresh_gate(t)
    await env.seed.strategy_opens(1, at_ms=t)
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000)).level == "model"
    new_day = utc(2025, 10, 10, 0, 0, 5)
    await gate.on_account_snapshot(D("1000"), new_day)
    assert await gate.evaluate(model="m1", action=OPEN, now_ms=new_day + 1000) == ALLOWED


async def test_the_model_level_day_follows_the_decision_time_not_the_last_reading(env, utc):
    """最近的读数还没过期，但判定时间已经跨过 UTC 午夜、新读数还没来：模型级按判定时间所在的日子算。"""
    env.config["daily_trade_limit"] = 2
    gate = await env.fresh_gate(utc(2025, 10, 9, 23, 59, 55), "1000")
    await env.seed.strategy_opens(2, at_ms=utc(2025, 10, 9, 23, 59, 56))
    before_midnight = await gate.evaluate(model="m1", action=OPEN, now_ms=utc(2025, 10, 9, 23, 59, 58))
    assert before_midnight.level == "model"  # 10 月 9 日已经开满 2 次
    after_midnight = await gate.evaluate(model="m1", action=OPEN, now_ms=utc(2025, 10, 10, 0, 0, 5))
    assert after_midnight == ALLOWED  # 读数只有 10 秒前（没过期）；10 月 10 日从 0 次起算
    assert gate.status()["model_day"] == "2025-10-10"


async def test_the_model_level_reads_the_ledger_on_every_call(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    assert await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000) == ALLOWED
    await env.seed.trade(close_ms=t + 2000, net_pnl="-80")
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 3000)).level == "model"


# ---------------------------------------------------------------- 账户级与模型级分开计


@pytest.mark.parametrize("model", ["m2", "m3", "op"])
async def test_a_model_1_trip_does_not_block_other_models(env, utc, model):
    env.config["daily_trade_limit"] = 1
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await env.seed.strategy_opens(1, at_ms=t)
    await env.seed.trade(close_ms=t, net_pnl="-500")
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000)).level == "model"
    assert await gate.evaluate(model=model, action=OPEN, now_ms=t + 1000) == ALLOWED


async def test_a_model_trip_is_not_an_account_trip_and_the_other_way_round(env, utc):
    env.config["daily_trade_limit"] = 1
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await env.seed.strategy_opens(1, at_ms=t)
    await gate.on_account_snapshot(D("1000"), t + 10_000)  # 刷新模型级
    status = gate.status()
    assert status["model_tripped"] is True and status["account_tripped"] is False
    await gate.on_account_snapshot(D("940"), t + 20_000)  # 再触发账户级
    status = gate.status()
    assert status["model_tripped"] is True and status["account_tripped"] is True
    assert len(status["reasons"]) == 2
    # 账户级触发时，模型 1 先被账户级拦下（报「账户级」）；模型 2 也被拦
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 21_000)).level == "account"
    assert (await gate.evaluate(model="m2", action=OPEN, now_ms=t + 21_000)).level == "account"


async def test_an_equity_drop_within_the_account_limits_does_not_trip_the_model_level(env, utc):
    """账户权益大跌（但没到账户级限额）不影响模型级；模型级只看已平交易与开仓次数。"""
    env.config["daily_loss_limit"] = D("5000")
    env.config["max_drawdown"] = D("99")
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "10000")
    await gate.on_account_snapshot(D("9000"), t + 10_000)
    assert await gate.evaluate(model="m1", action=OPEN, now_ms=t + 11_000) == ALLOWED


# ---------------------------------------------------------------- 模型级的事件与告警


async def test_a_model_level_trip_found_by_evaluate_publishes_one_event_and_one_warning(env, utc):
    env.config["daily_trade_limit"] = 1
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await env.seed.strategy_opens(1, at_ms=t)
    for i in range(3):
        assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000 + i)).level == "model"
    events = await env.flush()
    assert len(events) == 1
    assert events[0].account_tripped is False and events[0].model_tripped is True
    assert len(events[0].reasons) == 1 and "模型 1" in events[0].reasons[0]
    assert env.notifier.kinds() == ["risk_model_tripped"]
    assert env.notifier.alerts[0].level is AlertLevel.WARNING


async def test_a_snapshot_refreshes_the_model_level_and_publishes_changes_without_any_evaluate_call(env, utc):
    env.config["daily_trade_limit"] = 1
    t = utc(2025, 10, 9, 23, 59, 40)
    gate = await env.fresh_gate(t)
    await env.seed.strategy_opens(1, at_ms=t)
    await gate.on_account_snapshot(D("1000"), t + 5_000)
    events = await env.flush()
    assert [(e.account_tripped, e.model_tripped) for e in events] == [(False, True)]
    await gate.on_account_snapshot(D("1000"), utc(2025, 10, 10, 0, 0, 5))  # 换日：模型级自然恢复
    events = await env.flush()
    assert [(e.account_tripped, e.model_tripped) for e in events] == [(False, True), (False, False)]
    assert events[1].reasons == ()
    assert env.notifier.kinds() == ["risk_model_tripped", "risk_model_reset"]
    assert env.notifier.alerts[1].level is AlertLevel.INFO


async def test_the_model_level_follows_the_ledger_both_ways_without_a_latch(env, utc):
    """模型级不是锁存：当日净盈亏回升到上限之内（比如一笔盈利的策略仓平掉）就恢复，按账本实时判定。"""
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await env.seed.trade(close_ms=t, net_pnl="-55")
    assert (await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000)).level == "model"
    await env.seed.trade(close_ms=t + 500, net_pnl="10")
    assert await gate.evaluate(model="m1", action=OPEN, now_ms=t + 2000) == ALLOWED
    events = await env.flush()
    assert [e.model_tripped for e in events] == [True, False]


# ---------------------------------------------------------------- 模型级限额没配置


async def test_an_unset_daily_trade_limit_blocks_model_1_only(env, utc):
    env.config["daily_trade_limit"] = None
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    decision = await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1000)
    assert decision.allowed is False and decision.level == "model" and "未配置" in decision.reason
    assert await gate.evaluate(model="m2", action=OPEN, now_ms=t + 1000) == ALLOWED
    assert await gate.evaluate(model="m1", action=IntentAction.CLOSE, now_ms=t + 1000) == ALLOWED
    assert gate.status()["limits_missing"] == ["daily_trade_limit"]


async def test_a_fractional_daily_trade_limit_is_a_configuration_error_not_silently_rounded(env, utc):
    env.config["daily_trade_limit"] = D("2.5")
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)  # 读数照常处理：模型级只是顺手刷新，配置错误不能挡住账户级
    with pytest.raises(ValueError, match="daily_trade_limit"):
        await gate.evaluate(model="m1", action=OPEN, now_ms=t + 1_000)
    assert await gate.evaluate(model="m2", action=OPEN, now_ms=t + 1_000) == ALLOWED  # 其它模型不看这一项


# ---------------------------------------------------------------- status 的余量


async def test_status_reports_equities_margins_and_counters(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    await gate.on_account_snapshot(D("1020"), t + 10_000)
    await env.seed.strategy_opens(2, at_ms=t)
    await env.seed.trade(close_ms=t + 1000, net_pnl="-12.5")
    await env.seed.trade(close_ms=t + 2000, net_pnl="2")
    await gate.on_account_snapshot(D("990"), t + 20_000)
    status = gate.status()
    assert status["day"] == "2025-10-09"
    assert (status["day_start_equity"], status["peak_equity"], status["equity"]) == ("1000", "1020", "990")
    assert status["daily_loss"] == "10" and status["daily_loss_limit"] == "50" and status["daily_loss_remaining"] == "40"
    assert status["drawdown_pct"] == "2.94" and status["max_drawdown_pct"] == "10"
    assert status["drawdown_remaining_pct"] == "7.06"
    assert status["strategy_opens_today"] == 2 and status["trades_remaining"] == 48
    assert status["strategy_net_pnl_today"] == "-10.5" and status["model_loss_remaining"] == "39.5"
    assert (status["account_tripped"], status["daily_loss_tripped"], status["drawdown_tripped"],
            status["model_tripped"]) == (False, False, False, False)
    assert status["reasons"] == [] and status["limits_missing"] == []


async def test_status_model_loss_margin_is_the_distance_to_the_trip_so_profits_widen_it(env, utc):
    """模型级按当日净盈亏判定（净盈亏 ≤ −上限才触发），所以余量 = 上限 + 净盈亏：先赚了 30，要再亏 80 才触发。"""
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    await env.seed.trade(close_ms=t + 1_000, net_pnl="30")
    await gate.on_account_snapshot(D("1000"), t + 10_000)
    status = gate.status()
    assert status["strategy_net_pnl_today"] == "30" and status["model_loss_remaining"] == "80"
    await env.seed.trade(close_ms=t + 2_000, net_pnl="-79.99")  # 净亏 49.99：还差 0.01
    await gate.on_account_snapshot(D("1000"), t + 20_000)
    status = gate.status()
    assert status["strategy_net_pnl_today"] == "-49.99" and status["model_loss_remaining"] == "0.01"
    assert status["model_tripped"] is False
    await env.seed.trade(close_ms=t + 3_000, net_pnl="-0.01")  # 正好 −50：触发，余量 0
    await gate.on_account_snapshot(D("1000"), t + 30_000)
    status = gate.status()
    assert status["model_tripped"] is True and D(status["model_loss_remaining"]) == 0


async def test_status_margins_never_go_negative_when_the_limits_are_lowered_between_readings(env, utc):
    """限额在两个读数之间调低：锁存要等下一个读数才按新限额判定，余量却已经按新限额算 —— 不能出现负数。"""
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t, "1000")
    await env.seed.trade(close_ms=t + 1_000, net_pnl="-30")
    await gate.on_account_snapshot(D("950.01"), t + 10_000)  # 日亏损 49.99、回撤 4.999%、模型净亏 30：都没到
    env.config["daily_loss_limit"] = D("20")
    env.config["max_drawdown"] = D("2")
    status = gate.status()
    assert status["daily_loss_tripped"] is False and status["drawdown_tripped"] is False  # 还没有新读数，没重新判定
    assert status["daily_loss_remaining"] == "0" and status["drawdown_remaining_pct"] == "0.00"
    assert status["model_loss_remaining"] == "0"


async def test_status_margins_are_zero_when_tripped_and_the_reasons_are_listed(env, utc):
    env.config["daily_trade_limit"] = 1
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await env.seed.strategy_opens(3, at_ms=t)
    await gate.on_account_snapshot(D("900"), t + 10_000)  # 日亏损 100 与回撤 10% 都触发
    status = gate.status()
    assert D(status["daily_loss_remaining"]) == 0 and D(status["drawdown_remaining_pct"]) == 0
    assert status["trades_remaining"] == 0
    assert status["account_tripped"] and status["daily_loss_tripped"] and status["drawdown_tripped"]
    assert status["model_tripped"] is True
    assert len(status["reasons"]) == 3
    assert status["daily_loss_tripped_ms"] == t + 10_000 and status["drawdown_tripped_ms"] == t + 10_000


async def test_status_is_json_friendly(env, utc):
    t = utc(2025, 10, 9, 12)
    gate = await env.fresh_gate(t)
    await gate.on_account_snapshot(D("900"), t + 10_000)
    assert json.loads(json.dumps(gate.status())) == gate.status()
