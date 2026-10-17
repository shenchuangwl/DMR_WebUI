"""Guard 对数据流状态的映射（报告 4.4「数据流判活」、4.6「抑制原因」与「运行方式」）。

数据流中断 → 置「数据流切换中」；仅轮询下完成一轮完整持仓对账 → 解除抑制并进入「仅轮询降级」；
数据流恢复并稳定（recovered_stable）→ 退出降级。降级不是抑制原因，期间可以开仓。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import AlertLevel, Gate, Model, RunMode, Suppression
from guard_kit import reconciled, stream_event

SWITCHING = Suppression.STREAM_SWITCHING


# ---------------------------------------------------------------- 中断 → 对账 → 降级 → 恢复稳定


async def test_an_interruption_sets_the_stream_switching_suppression(make_env):
    env = make_env()
    await env.boot()

    await env.emit(stream_event("interrupted", "静默 90 秒"))

    assert env.state.suppressions == {SWITCHING}
    assert env.state.run_mode is RunMode.NORMAL  # 还没完成对账，不算进入降级运行


async def test_a_complete_positions_reconcile_ends_the_switching_and_enters_polling_degraded(make_env):
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"))

    await env.emit(reconciled("positions", "ok"))

    assert env.state.suppressions == frozenset()
    assert env.state.run_mode is RunMode.POLLING_DEGRADED


async def test_stable_recovery_leaves_the_degraded_mode(make_env):
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"), reconciled())
    assert env.state.run_mode is RunMode.POLLING_DEGRADED

    await env.emit(stream_event("connected"))
    assert env.state.run_mode is RunMode.POLLING_DEGRADED  # 刚连上还不算稳定
    await env.emit(stream_event("recovered_stable"))

    assert env.state.run_mode is RunMode.NORMAL
    assert env.state.suppressions == frozenset()


async def test_opening_is_blocked_while_switching_and_allowed_again_in_degraded_mode(make_env):
    env = make_env()
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")
    assert env.state.can_open(Model.M1) == (True, "")

    await env.emit(stream_event("interrupted"))
    allowed, why = env.state.can_open(Model.M1)
    assert not allowed and "数据流切换中" in why

    await env.emit(reconciled())
    assert env.state.can_open(Model.M1) == (True, "")  # 降级不是抑制原因：可以开仓（上限收紧由限额检查做）
    assert env.state.run_mode is RunMode.POLLING_DEGRADED

    await env.emit(stream_event("recovered_stable"))
    assert env.state.can_open(Model.M1) == (True, "")
    assert env.state.run_mode is RunMode.NORMAL


async def test_an_interruption_does_not_close_the_gate(make_env):
    env = make_env()
    await env.boot()
    await env.modelstate.set_gate(Gate.RUNNING, reason="启动")

    await env.emit(stream_event("interrupted"))

    assert env.state.gate is Gate.RUNNING  # 只是置抑制；对已有仓的保护与平仓照常


# ---------------------------------------------------------------- 只有「完整的持仓对账成功」才解除


@pytest.mark.parametrize("kind,status", [("positions", "unknown"), ("positions", "mismatch"),
                                         ("orders", "ok"), ("orders", "unknown"), ("orders", "mismatch")])
async def test_other_reconcile_results_do_not_end_the_switching(make_env, kind, status):
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"))
    version = env.state.version

    await env.emit(reconciled(kind, status))

    assert env.state.suppressions == {SWITCHING}
    assert env.state.run_mode is RunMode.NORMAL
    assert env.state.version == version


async def test_the_next_successful_positions_reconcile_ends_it_after_failed_ones(make_env):
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"), reconciled("positions", "unknown"), reconciled("positions", "unknown"))
    assert env.state.suppressions == {SWITCHING}

    await env.emit(reconciled("positions", "ok"))

    assert env.state.suppressions == frozenset()


async def test_a_successful_reconcile_while_not_switching_changes_nothing(make_env):
    env = make_env()
    await env.boot()
    version = env.state.version

    await env.emit(reconciled("positions", "ok"))

    assert env.state.version == version
    assert env.state.run_mode is RunMode.NORMAL


async def test_a_reconcile_after_the_switching_ended_does_not_toggle_anything(make_env):
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"), reconciled())
    version = env.state.version

    await env.emit(reconciled(), reconciled())

    assert env.state.version == version
    assert env.state.run_mode is RunMode.POLLING_DEGRADED


async def test_a_suppression_set_by_hand_is_also_ended_by_the_next_good_reconcile(make_env):
    """「正处于切换中」以模型状态里的抑制原因为准（重启后的残留、人工加的都算）。"""
    env = make_env()
    await env.boot()
    await env.modelstate.add_suppression(SWITCHING, reason="残留")

    await env.emit(reconciled())

    assert env.state.suppressions == frozenset()
    assert env.state.run_mode is RunMode.POLLING_DEGRADED


# ---------------------------------------------------------------- 其它状态、重复与乱序


@pytest.mark.parametrize("state", ["connected", "stopped"])
async def test_connected_and_stopped_events_change_nothing(make_env, state):
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"))
    version = env.state.version

    await env.emit(stream_event(state))

    assert env.state.suppressions == {SWITCHING}  # 连上了也要等对账完成才解除
    assert env.state.version == version


async def test_recovered_stable_without_a_degraded_mode_changes_nothing(make_env):
    env = make_env()
    await env.boot()
    version = env.state.version

    await env.emit(stream_event("recovered_stable"))

    assert env.state.version == version
    assert env.notifier.alerts == []


async def test_a_repeated_interruption_is_idempotent(make_env):
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"))
    version = env.state.version

    await env.emit(stream_event("interrupted"), stream_event("interrupted"))

    assert env.state.suppressions == {SWITCHING}
    assert env.state.version == version


async def test_events_are_applied_in_publish_order(make_env):
    env = make_env()
    await env.boot()

    env.bus.publish(stream_event("interrupted"))
    env.bus.publish(reconciled())
    env.bus.publish(stream_event("recovered_stable"))
    await env.bus.drain()

    assert env.state.suppressions == frozenset()
    assert env.state.run_mode is RunMode.NORMAL


async def test_another_interruption_during_the_degraded_mode_suppresses_again(make_env):
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"), reconciled(), stream_event("connected"))
    assert env.state.run_mode is RunMode.POLLING_DEGRADED

    await env.emit(stream_event("interrupted"))  # 没等到稳定又断了
    assert env.state.suppressions == {SWITCHING}
    assert env.state.run_mode is RunMode.POLLING_DEGRADED

    await env.emit(reconciled())
    assert env.state.suppressions == frozenset()
    assert env.state.run_mode is RunMode.POLLING_DEGRADED  # 仍是降级：稳定的恢复还没出现

    await env.emit(stream_event("recovered_stable"))
    assert env.state.run_mode is RunMode.NORMAL


async def test_stable_recovery_before_the_reconcile_goes_straight_back_to_normal(make_env):
    """数据流已经恢复并稳定，持仓对账却迟迟没成功：对账一成功就回到正常，不经降级。"""
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"), reconciled("positions", "unknown"))

    await env.emit(stream_event("recovered_stable"))
    assert env.state.suppressions == {SWITCHING}  # 完整对账还没做，抑制仍在
    assert env.state.run_mode is RunMode.NORMAL

    await env.emit(reconciled("positions", "ok"))

    assert env.state.suppressions == frozenset()
    assert env.state.run_mode is RunMode.NORMAL


async def test_a_new_interruption_forgets_an_earlier_stable_recovery(make_env):
    env = make_env()
    await env.boot()
    await env.emit(stream_event("interrupted"), stream_event("recovered_stable"))

    await env.emit(stream_event("interrupted"), reconciled())

    assert env.state.run_mode is RunMode.POLLING_DEGRADED  # 第二次中断之后还没稳定过


# ---------------------------------------------------------------- 数据流在配置里关掉：一直是仅轮询


async def test_with_the_stream_disabled_a_stable_recovery_never_leaves_polling_degraded(make_env):
    env = make_env(config={"ex_stream_enabled": False})
    await env.boot()
    await env.guard.startup()
    assert env.state.run_mode is RunMode.POLLING_DEGRADED

    await env.emit(stream_event("recovered_stable"))

    assert env.state.run_mode is RunMode.POLLING_DEGRADED


async def test_with_the_stream_disabled_the_reconcile_after_an_interruption_stays_degraded(make_env):
    env = make_env(config={"ex_stream_enabled": False})
    await env.boot()
    await env.guard.startup()

    await env.emit(stream_event("interrupted"), stream_event("recovered_stable"), reconciled())

    assert env.state.suppressions == frozenset()
    assert env.state.run_mode is RunMode.POLLING_DEGRADED


# ---------------------------------------------------------------- 告警


async def test_an_interruption_raises_a_warning_and_leaving_degraded_an_info(make_env):
    env = make_env()
    await env.boot()

    await env.emit(stream_event("interrupted", "静默 90 秒"))
    [warning] = env.alerts("stream_interrupted")
    assert warning.level is AlertLevel.WARNING and "静默 90 秒" in warning.message
    assert warning.dedupe_key == "stream_interrupted"

    await env.emit(reconciled(), stream_event("recovered_stable"))
    [info] = env.alerts("stream_recovered")
    assert info.level is AlertLevel.INFO


async def test_entering_the_degraded_mode_alone_raises_no_further_alert(make_env):
    env = make_env()
    await env.boot()

    await env.emit(stream_event("interrupted"), reconciled())

    assert [a.kind for a in env.alerts()] == ["stream_interrupted"]


# ---------------------------------------------------------------- 订阅的生命周期


async def test_nothing_happens_before_start_or_after_stop(make_env):
    env = make_env()
    await env.boot(subscribe=False)
    await env.emit(stream_event("interrupted"))
    assert env.state.suppressions == frozenset()

    await env.guard.start()
    await env.emit(stream_event("interrupted"))
    assert env.state.suppressions == {SWITCHING}

    await env.guard.stop()
    await env.emit(reconciled())
    assert env.state.suppressions == {SWITCHING}  # 停了就不再响应


async def test_start_twice_subscribes_once(make_env):
    env = make_env()
    await env.boot()
    await env.guard.start()

    await env.emit(stream_event("interrupted"))

    assert len(env.alerts("stream_interrupted")) == 1


async def test_stop_is_safe_before_start_and_when_repeated(make_env):
    env = make_env()
    await env.boot(subscribe=False)

    await env.guard.stop()
    await env.guard.start()
    await env.guard.stop()
    await env.guard.stop()


async def test_stop_keeps_the_legacy_locks(make_env):
    """关停时锁要一直持有到进程退出（旧入口在内核仍在收尾时不能起来），所以 stop 不释放它们。"""
    env = make_env(environment="mainnet_live")
    await env.boot()
    assert await env.guard.startup() == []

    await env.guard.stop()

    assert len(env.legacy.held) == 7
