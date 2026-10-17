"""Guard.startup：启动自检。持仓模式、实盘才有的 7 把锁与停用标记、数据流状态归零、交易所限制的初读。

约定：返回的问题清单非空 = 内核不进入可交易状态。互斥相关的问题同时置「互斥异常」抑制，这样即使调用方忽略返回值，
模型状态里也启动不了（allowed_operations 的 start 为否）。
"""
from __future__ import annotations

import os

import pytest

from dmr_kernel.core.modelstate import allowed_operations
from dmr_kernel.core.types import (
    AlertLevel, Gate, Model, RestrictionInfo, RunMode, Suppression, Unknown,
)
from guard_kit import reconciled, stream_event
from lock_kit import MARKER, NAMES, lock_path


# ---------------------------------------------------------------- 持仓模式（所有环境）


@pytest.mark.parametrize("environment", ["standin", "testnet", "mainnet_shadow", "mainnet_live"])
async def test_hedge_mode_passes_the_selfcheck_in_every_environment(make_env, environment):
    env = make_env(environment=environment)
    await env.boot()

    assert await env.guard.startup() == []

    assert env.adapter.calls("fetch_position_mode") == [{}]
    assert env.state.suppressions == frozenset()
    assert env.notifier.alerts == []


async def test_one_way_position_mode_fails_the_selfcheck_and_raises_a_critical_alert(make_env):
    env = make_env()
    env.adapter.set_read("fetch_position_mode", False)
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 1
    assert "对冲" in problems[0] and "单向" in problems[0]
    [alert] = env.alerts("position_mode_not_hedge")
    assert alert.level is AlertLevel.CRITICAL
    assert alert.dedupe_key == "position_mode_not_hedge"


async def test_unknown_position_mode_fails_closed(make_env):
    env = make_env()
    env.adapter.set_read("fetch_position_mode", Unknown("超时"))
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 1
    assert "无法确认持仓模式" in problems[0] and "超时" in problems[0]


async def test_an_adapter_error_while_reading_the_position_mode_fails_closed(make_env):
    env = make_env()

    def explode(**_):
        raise RuntimeError("适配层坏了")

    env.adapter.set_read("fetch_position_mode", explode)
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 1
    assert "无法确认持仓模式" in problems[0] and "适配层坏了" in problems[0]


async def test_a_wrong_position_mode_is_not_a_suppression(make_env):
    """持仓模式没有对应的抑制原因：它只是启动自检的不通过项（拒绝启动），运行期只告警。"""
    env = make_env()
    env.adapter.set_read("fetch_position_mode", False)
    await env.boot()

    await env.guard.startup()

    assert env.state.suppressions == frozenset()


# ---------------------------------------------------------------- 非实盘：旧入口互斥完全不碰


@pytest.mark.parametrize("environment", ["standin", "testnet", "mainnet_shadow"])
async def test_startup_outside_live_never_touches_the_legacy_runtime_directory(make_env, environment):
    env = make_env(environment=environment)  # 目录不存在；夹具收尾时核对它仍不存在
    await env.boot()

    assert await env.guard.startup() == []

    assert not env.runtime_dir.parent.exists()
    assert env.legacy.held == ()


async def test_the_environment_decides_not_the_mutex_flag(make_env, tmp_path):
    """装配错误：非实盘环境却拿到一个启用的互斥对象。Guard 以环境为准，不去打开 data/runtime_state。"""
    target = tmp_path / "elsewhere" / "runtime_state"
    env = make_env(mutex_enabled=True, runtime=target)
    await env.boot()

    assert await env.guard.startup() == []

    assert not target.parent.exists()
    assert env.legacy.held == ()


# ---------------------------------------------------------------- 实盘：7 把锁、目录、停用标记


async def test_live_startup_takes_all_seven_locks_and_passes(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()

    assert await env.guard.startup() == []

    assert env.legacy.held == NAMES
    for name in NAMES:
        assert lock_path(env.runtime_dir, name).read_text().strip() == str(os.getpid())
    assert env.state.suppressions == frozenset()
    assert env.notifier.alerts == []


async def test_a_lock_that_cannot_be_taken_keeps_the_kernel_out_of_the_tradable_state(make_env, holders):
    env = make_env(environment="mainnet_live")
    holder = holders(lock_path(env.runtime_dir, "first_long_term"))  # 另一个进程里的旧入口
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 1
    assert "first_long_term" in problems[0] and str(holder.pid) in problems[0]
    assert env.legacy.held == tuple(n for n in NAMES if n != "first_long_term")
    # 不进入可交易状态：抑制已置，启动不被允许；就算闸被硬打开，也不放行开仓
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    start = allowed_operations(env.state)["start"]
    assert not start.allowed and "互斥异常" in start.reason
    await env.modelstate.set_gate(Gate.RUNNING, reason="强行打开")
    assert env.state.can_open(Model.M1)[0] is False
    [alert] = env.alerts("legacy_mutex_anomaly")
    assert alert.level is AlertLevel.CRITICAL and str(holder.pid) in alert.message
    assert alert.dedupe_key == "legacy_mutex_anomaly"


@pytest.mark.parametrize("name", NAMES)
async def test_each_one_of_the_seven_locks_is_required(make_env, holders, name):
    env = make_env(environment="mainnet_live")
    holders(lock_path(env.runtime_dir, name))
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 1 and name in problems[0]
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions


async def test_all_busy_locks_are_reported_together(make_env, holders):
    env = make_env(environment="mainnet_live")
    running = [holders(lock_path(env.runtime_dir, name)) for name in NAMES]
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 7
    for holder in running:
        assert any(str(holder.pid) in p for p in problems)
    assert env.legacy.held == ()


async def test_a_missing_stop_marker_fails_the_selfcheck_and_sets_the_suppression(make_env):
    env = make_env(environment="mainnet_live", marker=False)
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 1 and MARKER in problems[0]
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    assert not (env.runtime_dir / MARKER).exists()  # 内核不创建停用标记
    assert env.legacy.held == NAMES  # 锁照常持有：旧入口至少在取锁处被拒


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o777])
async def test_a_directory_that_is_not_0700_is_refused(make_env, mode):
    env = make_env(environment="mainnet_live")
    os.chmod(env.runtime_dir, mode)
    try:
        await env.boot()

        problems = await env.guard.startup()
    finally:
        os.chmod(env.runtime_dir, 0o700)

    assert len(problems) == 1  # 启动时目录问题只报一次
    assert "0700" in problems[0] and f"{mode:04o}" in problems[0]
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    assert env.legacy.held == ()
    assert not list(env.runtime_dir.glob("instance_*.lock"))  # 权限不对就一把锁也不取、不建


async def test_a_missing_runtime_directory_is_refused_and_not_created(make_env):
    env = make_env(environment="mainnet_live", create_runtime=False)
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 1 and "不存在" in problems[0]
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    assert not env.runtime_dir.exists() and not env.runtime_dir.parent.exists()


async def test_live_without_an_enabled_mutex_is_an_assembly_error(make_env):
    """实盘环境下互斥对象却是停用的（装配错误）：不能静默地没有第一层，按互斥异常处理。"""
    env = make_env(environment="mainnet_live", mutex_enabled=False)
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 1 and "未启用" in problems[0]
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    assert env.legacy.held == ()
    assert not list(env.runtime_dir.glob("instance_*.lock"))


async def test_startup_is_repeatable_in_live(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()

    assert await env.guard.startup() == []
    assert await env.guard.startup() == []

    assert env.legacy.held == NAMES
    assert env.state.suppressions == frozenset()


async def test_startup_does_not_lift_a_persisted_mutex_anomaly(make_env):
    """互斥异常要人工确认才解除：重启后一切正常也不自动清。"""
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.modelstate.add_suppression(Suppression.MUTEX_ANOMALY, reason="上次遗留")

    assert await env.guard.startup() == []

    assert Suppression.MUTEX_ANOMALY in env.state.suppressions


async def test_startup_returns_problems_from_position_mode_and_locks_together(make_env, holders):
    env = make_env(environment="mainnet_live")
    env.adapter.set_read("fetch_position_mode", False)
    holders(lock_path(env.runtime_dir, "main"))
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 2
    assert any("对冲" in p for p in problems) and any("main" in p for p in problems)


# ---------------------------------------------------------------- 数据流状态归零（重启后）


async def test_startup_resets_the_stream_state_left_by_the_previous_run(make_env):
    """上一次运行留下的「数据流切换中」与「仅轮询降级」属于上一个数据流会话；新会话没有中断就不该带着它们。"""
    env = make_env()
    await env.boot()
    await env.modelstate.add_suppression(Suppression.STREAM_SWITCHING, reason="上次遗留")
    await env.modelstate.set_run_mode(RunMode.POLLING_DEGRADED, reason="上次遗留")

    assert await env.guard.startup() == []

    assert Suppression.STREAM_SWITCHING not in env.state.suppressions
    assert env.state.run_mode is RunMode.NORMAL


async def test_startup_keeps_what_this_runs_stream_already_reported(make_env):
    """装配顺序错了（数据流先于 startup 起来）：这次运行里已经收到的中断不是「上次遗留」，不能被启动自检清掉。"""
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted", "首次连接失败"))
    assert env.state.suppressions == {Suppression.STREAM_SWITCHING}

    assert await env.guard.startup() == []

    assert env.state.suppressions == {Suppression.STREAM_SWITCHING}


async def test_startup_keeps_the_degraded_mode_this_run_already_entered(make_env):
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"), reconciled())
    assert env.state.run_mode is RunMode.POLLING_DEGRADED

    await env.guard.startup()

    assert env.state.run_mode is RunMode.POLLING_DEGRADED


async def test_startup_leaves_the_other_persisted_suppressions_alone(make_env):
    env = make_env()
    await env.boot()
    for s in (Suppression.RISK_TRIPPED, Suppression.RECONCILE_MISMATCH, Suppression.UNPROTECTED,
              Suppression.CLOCK_SKEW, Suppression.RECOVERING, Suppression.RECONCILE_UNKNOWN):
        await env.modelstate.add_suppression(s, reason="上次遗留")
    before = env.state.suppressions

    await env.guard.startup()

    assert env.state.suppressions == before


async def test_startup_with_the_stream_disabled_runs_in_polling_degraded_mode(make_env):
    env = make_env(config={"ex_stream_enabled": False})
    await env.boot()

    assert await env.guard.startup() == []

    assert env.state.run_mode is RunMode.POLLING_DEGRADED


async def test_startup_with_the_stream_enabled_does_not_touch_a_clean_state(make_env):
    env = make_env()
    await env.boot()
    version = env.state.version

    await env.guard.startup()

    assert env.state.version == version  # 没有任何变化就不增版本、不审计


# ---------------------------------------------------------------- 交易所限制的初读


async def test_startup_sets_the_exchange_restriction_suppression_when_restricted(make_env):
    env = make_env()
    env.adapter.set_read("fetch_restrictions", RestrictionInfo(True, (("rule", "order_rate"),)))
    await env.boot()

    assert await env.guard.startup() == []  # 受限是抑制原因，不是自检不通过

    assert Suppression.EXCHANGE_RESTRICTED in env.state.suppressions


async def test_startup_clears_a_stale_exchange_restriction_when_the_exchange_says_it_is_lifted(make_env):
    env = make_env()
    await env.boot()
    await env.modelstate.add_suppression(Suppression.EXCHANGE_RESTRICTED, reason="上次遗留")

    await env.guard.startup()

    assert Suppression.EXCHANGE_RESTRICTED not in env.state.suppressions


async def test_startup_keeps_a_persisted_exchange_restriction_when_the_read_fails(make_env):
    env = make_env()
    env.adapter.set_read("fetch_restrictions", Unknown("超时"))
    await env.boot()
    await env.modelstate.add_suppression(Suppression.EXCHANGE_RESTRICTED, reason="上次遗留")

    assert await env.guard.startup() == []

    assert Suppression.EXCHANGE_RESTRICTED in env.state.suppressions  # 未知不能当成「没有限制」


async def test_an_unknown_initial_restriction_read_is_retried_by_the_first_periodic_run(make_env):
    env = make_env()
    env.adapter.set_read("fetch_restrictions", Unknown("超时"))
    await env.boot()
    await env.guard.startup()
    env.adapter.set_read("fetch_restrictions", RestrictionInfo(True))

    await env.guard.periodic()  # 时钟没动：启动时没读到，不等 5 分钟

    assert len(env.adapter.calls("fetch_restrictions")) == 2
    assert Suppression.EXCHANGE_RESTRICTED in env.state.suppressions


# ---------------------------------------------------------------- 持仓模式读到的不是布尔值

@pytest.mark.parametrize("value", ["hedge", "true", 1, 0, None])
async def test_a_position_mode_that_is_not_a_boolean_is_never_taken_for_hedge(make_env, value):
    env = make_env()
    env.adapter.set_read("fetch_position_mode", value)
    await env.boot()

    problems = await env.guard.startup()

    assert len(problems) == 1 and "无法确认持仓模式" in problems[0] and repr(value) in problems[0]
    assert env.guard.status()["position_mode"] == "unknown"


# ---------------------------------------------------------------- 与风控闸对齐（风控闸的 load/start 不发事件）


def risk_gate_says(*, tripped: bool, loaded: bool = True):
    return lambda: {"loaded": loaded, "account_tripped": tripped}


async def test_startup_sets_the_risk_suppression_when_the_risk_gate_restored_an_account_level_trip(make_env):
    env = make_env(risk_status=risk_gate_says(tripped=True))
    await env.boot()

    assert await env.guard.startup() == []

    assert env.state.suppressions == {Suppression.RISK_TRIPPED}


async def test_startup_clears_a_stale_risk_suppression_when_the_risk_gate_has_no_trip(make_env):
    env = make_env(risk_status=risk_gate_says(tripped=False))
    await env.boot()
    await env.modelstate.add_suppression(Suppression.RISK_TRIPPED, reason="上次遗留")

    await env.guard.startup()

    assert env.state.suppressions == frozenset()


@pytest.mark.parametrize("persisted", [True, False])
async def test_startup_does_not_align_with_a_risk_gate_that_is_not_loaded(make_env, persisted):
    env = make_env(risk_status=risk_gate_says(tripped=not persisted, loaded=False))
    await env.boot()
    if persisted:
        await env.modelstate.add_suppression(Suppression.RISK_TRIPPED, reason="上次遗留")
    before = env.state.suppressions

    await env.guard.startup()

    assert env.state.suppressions == before


async def test_startup_survives_a_risk_status_that_raises(make_env):
    def explode():
        raise RuntimeError("风控闸坏了")

    env = make_env(risk_status=explode)
    await env.boot()
    await env.modelstate.add_suppression(Suppression.RISK_TRIPPED, reason="上次遗留")

    assert await env.guard.startup() == []

    assert env.state.suppressions == {Suppression.RISK_TRIPPED}  # 对不上就保持原样，不当成「没有触发」


async def test_without_a_risk_status_startup_leaves_the_risk_suppression_alone(make_env):
    env = make_env()
    await env.boot()
    await env.modelstate.add_suppression(Suppression.RISK_TRIPPED, reason="上次遗留")

    await env.guard.startup()

    assert env.state.suppressions == {Suppression.RISK_TRIPPED}
