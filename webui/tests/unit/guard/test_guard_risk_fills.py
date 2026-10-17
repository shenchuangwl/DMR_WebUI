"""Guard 对风控状态与旧程序格式成交的映射。

- RiskStateChanged：账户级触发 → 置「风控触发」，复位后解除；模型级触发不是抑制原因。
- FillIngested：来源为旧程序格式 → 关上开仓闸并发最高级告警（运行期兜底，报告 5.1「三层互斥」的第四行）。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import AlertLevel, Gate, Model, OrderOrigin, Suppression
from guard_kit import LEGACY_CLIENT_ID, fill_event, risk_event, stream_event

RISK = Suppression.RISK_TRIPPED


# ---------------------------------------------------------------- 风控触发映射


async def test_an_account_level_trip_sets_the_risk_suppression_and_a_reset_clears_it(make_env):
    env = make_env()
    await env.boot()

    await env.emit(risk_event(account=True, reasons=("日亏损 52 USDT 超过上限 50 USDT",)))
    assert env.state.suppressions == {RISK}

    await env.emit(risk_event(account=False))
    assert env.state.suppressions == frozenset()


async def test_a_model_level_trip_is_not_a_suppression(make_env):
    env = make_env()
    await env.boot()
    version = env.state.version

    await env.emit(risk_event(model=True, reasons=("模型 1 日交易次数达到上限",)))

    assert env.state.suppressions == frozenset()
    assert env.state.version == version


async def test_a_model_level_trip_does_not_clear_an_account_level_one(make_env):
    env = make_env()
    await env.boot()
    await env.emit(risk_event(account=True, model=True, reasons=("回撤", "次数")))

    await env.emit(risk_event(account=True, model=False))  # 模型级先恢复，账户级还在

    assert env.state.suppressions == {RISK}


async def test_both_levels_tripped_sets_the_suppression_once(make_env):
    env = make_env()
    await env.boot()

    await env.emit(risk_event(account=True, model=True, reasons=("回撤", "次数")))

    assert env.state.suppressions == {RISK}


async def test_repeated_risk_events_are_idempotent(make_env):
    env = make_env()
    await env.boot()
    await env.emit(risk_event(account=True))
    version = env.state.version

    await env.emit(risk_event(account=True), risk_event(account=True))
    assert env.state.version == version

    await env.emit(risk_event(account=False))
    version = env.state.version
    await env.emit(risk_event(account=False), risk_event(model=False))
    assert env.state.version == version


async def test_a_reset_without_a_suppression_changes_nothing(make_env):
    env = make_env()
    await env.boot()
    version = env.state.version

    await env.emit(risk_event(account=False))

    assert env.state.version == version


async def test_the_risk_suppression_blocks_opening_until_it_is_reset(make_env):
    env = make_env()
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")

    await env.emit(risk_event(account=True, reasons=("回撤",)))
    allowed, why = env.state.can_open(Model.M1)
    assert not allowed and "风控触发" in why
    assert env.state.gate is Gate.RUNNING  # 抑制不关闸

    await env.emit(risk_event(account=False))
    assert env.state.can_open(Model.M1) == (True, "")


async def test_the_risk_suppression_is_independent_of_the_other_reasons(make_env):
    env = make_env()
    await env.boot()
    await env.emit(risk_event(account=True))
    await env.modelstate.add_suppression(Suppression.UNPROTECTED, reason="别处加的")

    await env.emit(risk_event(account=False))

    assert env.state.suppressions == {Suppression.UNPROTECTED}


# ---------------------------------------------------------------- 旧程序格式成交


async def test_a_legacy_format_fill_closes_the_gate_and_raises_a_critical_alert(make_env):
    env = make_env()
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")

    await env.emit(fill_event(OrderOrigin.LEGACY, symbol="ETHUSDT"))

    assert env.state.gate is Gate.STOPPED
    [alert] = env.alerts("legacy_fill_detected")
    assert alert.level is AlertLevel.CRITICAL
    assert LEGACY_CLIENT_ID in alert.message and "ETHUSDT" in alert.message
    assert alert.dedupe_key == "legacy_fill_detected"


async def test_a_legacy_format_fill_sets_no_suppression(make_env):
    env = make_env()
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")

    await env.emit(fill_event(OrderOrigin.LEGACY))

    assert env.state.suppressions == frozenset()  # 只关闸；是否再开由操作者查清后决定


async def test_a_legacy_fill_with_the_gate_already_stopped_still_alerts(make_env):
    env = make_env()
    await env.boot()
    version = env.state.version

    await env.emit(fill_event(OrderOrigin.LEGACY))

    assert env.state.gate is Gate.STOPPED and env.state.version == version
    assert len(env.alerts("legacy_fill_detected")) == 1


async def test_every_legacy_fill_closes_the_gate_again_after_it_was_reopened(make_env):
    env = make_env()
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")
    await env.emit(fill_event(OrderOrigin.LEGACY, fill_id=1))
    await env.modelstate.set_gate(Gate.RUNNING, reason="操作者重新启动")

    await env.emit(fill_event(OrderOrigin.LEGACY, fill_id=2, client_order_id="DMR6_LONG_SELL_LONG_1760000000001"))

    assert env.state.gate is Gate.STOPPED
    assert len(env.alerts("legacy_fill_detected")) == 2  # 同一个 dedupe_key，由告警器合并


@pytest.mark.parametrize("origin", [o for o in OrderOrigin if o is not OrderOrigin.LEGACY])
async def test_fills_from_any_other_origin_leave_the_gate_alone(make_env, origin):
    env = make_env()
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")
    version = env.state.version

    await env.emit(fill_event(origin, client_order_id="DK-whatever"))

    assert env.state.gate is Gate.RUNNING
    assert env.state.version == version
    assert env.alerts() == []


async def test_a_legacy_fill_without_a_client_order_id_is_still_caught(make_env):
    env = make_env()
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")

    await env.emit(fill_event(OrderOrigin.LEGACY, client_order_id=None))

    assert env.state.gate is Gate.STOPPED
    assert len(env.alerts("legacy_fill_detected")) == 1


@pytest.mark.parametrize("environment", ["standin", "testnet", "mainnet_shadow", "mainnet_live"])
async def test_the_legacy_fill_mapping_works_in_every_environment(make_env, environment):
    env = make_env(environment=environment)
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")

    await env.emit(fill_event(OrderOrigin.LEGACY))

    assert env.state.gate is Gate.STOPPED
    assert len(env.alerts("legacy_fill_detected")) == 1


# ---------------------------------------------------------------- 兜底路径上的出错


async def test_a_failed_gate_close_is_alerted_loudly(make_env):
    env = make_env()
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")

    async def broken(*args, **kwargs):
        raise RuntimeError("账本坏了")

    env.modelstate.set_gate = broken

    await env.emit(fill_event(OrderOrigin.LEGACY))

    [alert] = env.alerts("legacy_fill_detected")
    assert alert.level is AlertLevel.CRITICAL
    assert "关闭开仓闸失败" in alert.message and "已关上开仓闸" not in alert.message
    assert env.state.gate is Gate.RUNNING


async def test_a_notifier_that_raises_does_not_stop_the_state_changes(make_env):
    env = make_env()

    def broken(*args, **kwargs):
        raise RuntimeError("通知器坏了")

    env.notifier.raise_alert = broken
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")

    await env.emit(fill_event(OrderOrigin.LEGACY), stream_event("interrupted"))

    assert env.state.gate is Gate.STOPPED
    assert env.state.suppressions == {Suppression.STREAM_SWITCHING}
