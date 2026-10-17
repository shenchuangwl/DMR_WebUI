"""重启恢复的编排（F9 第 1、2、10 步与整体）：置「恢复中」、守卫自检、先暂缓成交再起数据流、出错照样释放、结果摘要。"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from dmr_kernel.core.recovery import Recovery, RecoveryResult
from dmr_kernel.core.registry import FillRelease
from dmr_kernel.core.types import AlertLevel, Gate, Suppression
from recovery_kit import LONG, SYM, pos


# ================================================================ 第 1 步与第 10 步：置与解「恢复中」


async def test_a_quiet_start_completes_and_lifts_the_recovering_suppression(make_env):
    env = await make_env().boot()
    result = await env.run()
    assert isinstance(result, RecoveryResult)
    assert result.tradable is True and result.problems == ()
    assert isinstance(result.steps, Mapping)
    assert Suppression.RECOVERING not in env.suppressions()
    assert env.modelstate.current().gate is Gate.STOPPED  # 恢复完状态仍是已停止，不替人开闸


async def test_recovering_is_set_before_the_guard_check_runs(make_env):
    env = await make_env().boot()
    await env.run()
    assert env.calls_named("guard.startup") == [("guard.startup", "recovering")]


async def test_the_suppression_is_set_for_the_whole_run(make_env):
    """恢复进行中（比如正在补保护时）「恢复中」一直在。"""
    env = await make_env().boot()
    seen: list[bool] = []
    original = env.protection.rebuild

    async def spy(position_id, *, reason):
        seen.append(Suppression.RECOVERING in env.suppressions())
        return await original(position_id, reason=reason)

    env.protection.rebuild = spy
    await env.add_position()  # 登记仓，交易所有仓、没有止损 → 会补保护
    env.positions = [pos()]
    await env.run()
    assert seen == [True]


async def test_the_first_steps_run_in_the_order_the_spec_requires(make_env):
    """第 1 步守卫自检要在账户数据流启动之前；暂缓成交要在数据流启动之前；快照在数据流之后；释放最后。"""
    env = await make_env().boot()
    await env.run()
    guard_at = env.index_of("guard.startup")
    stream_at = env.index_of("stream.start")
    first_read = env.index_of("read")
    release_at = env.index_of("registry.release")
    assert guard_at < stream_at < first_read < release_at
    assert env.calls[stream_at] == ("stream.start", True)  # 数据流起来的时候登记簿已经在暂缓
    assert all(not (isinstance(c, tuple) and c[0] == "read") for c in env.calls[release_at:])  # 释放之后不再读交易所


async def test_the_guard_is_started_after_its_check_and_before_the_stream(make_env):
    """守卫要在数据流启动前开始订阅事件，才收得到数据流的中断；自检（startup）又必须先于它。"""
    env = await make_env().boot()
    await env.run()
    assert env.index_of("guard.startup") < env.index_of("guard.start") < env.index_of("stream.start")


async def test_the_registry_is_released_with_the_snapshot_time_and_only_at_the_end(make_env):
    env = await make_env().boot()
    env.skew_ms = 4_000
    await env.run()
    (released,) = env.released
    assert released == env.clock.now_ms() + 4_000  # 服务器时间就是快照时间
    assert env.registry.holding is False


# ================================================================ 守卫自检有问题：保持抑制、告警、不自动清「互斥异常」


async def test_guard_problems_keep_the_suppression_and_report_not_tradable(make_env):
    env = await make_env().boot()
    env.guard.problems = ["持仓模式不是对冲模式", "旧入口互斥：锁 instance_x.lock 被进程 4242 持有"]
    result = await env.run()
    assert result.tradable is False
    assert result.problems[:2] == tuple(env.guard.problems)
    assert Suppression.RECOVERING in env.suppressions()  # 不进入可交易状态：保持「恢复中」
    (alert,) = env.alerts("recovery_blocked")
    assert alert.level is AlertLevel.CRITICAL and "持仓模式" in alert.message


async def test_the_other_steps_still_run_when_the_guard_reports_problems(make_env):
    """守卫有问题不等于不管持仓：快照、补保护、释放暂缓的成交照常做（只是不放开交易）。"""
    env = await make_env().boot()
    env.guard.problems = ["旧入口互斥异常"]
    await env.add_position()
    env.positions = [pos()]
    result = await env.run()
    assert env.protection.rebuilt == [(1, "recovery")]
    assert env.released and env.registry.holding is False
    assert result.steps["snapshot"]["ok"] is True


@pytest.mark.parametrize("problems", [[], ["旧入口互斥异常"]])
async def test_recovery_never_clears_a_mutex_anomaly(make_env, problems):
    env = await make_env().boot()
    await env.modelstate.add_suppression(Suppression.MUTEX_ANOMALY, reason="测试：上次运行遗留")
    env.guard.problems = problems
    await env.run()
    assert Suppression.MUTEX_ANOMALY in env.suppressions()  # 只有人工确认才解除


async def test_a_previous_runs_leftover_recovering_is_lifted_by_a_clean_run(make_env):
    env = await make_env().boot()
    await env.modelstate.add_suppression(Suppression.RECOVERING, reason="上次恢复中被杀")
    result = await env.run()
    assert result.tradable is True and Suppression.RECOVERING not in env.suppressions()


# ================================================================ 第 2 步：数据流


async def test_the_stream_is_not_started_when_it_is_switched_off_in_the_config(make_env):
    env = await make_env(ex_stream_enabled=False).boot()
    result = await env.run()
    assert env.stream.started == 0
    assert result.steps["snapshot"]["stream_started"] is False


async def test_the_stream_is_started_once_when_enabled(make_env):
    env = await make_env().boot()
    result = await env.run()
    assert env.stream.started == 1
    assert result.steps["snapshot"]["stream_started"] is True


async def test_a_missing_stream_object_is_tolerated(make_env):
    env = await make_env().boot()
    result = await env.run(stream=None)
    assert result.tradable is True and result.steps["snapshot"]["stream_started"] is False


# ================================================================ 出错的步骤：记下来、不挡后面的、照样释放、不放开交易


async def test_a_step_that_blows_up_is_reported_and_the_fills_are_still_released(make_env):
    env = await make_env().boot()
    env.reconciler.positions_result = RuntimeError("对账炸了")
    env.positions = []
    result = await env.run()
    assert "error" in result.steps["reconcile"] and "对账炸了" in result.steps["reconcile"]["error"]
    assert result.tradable is False and any("reconcile" in p or "对账" in p for p in result.problems)
    assert Suppression.RECOVERING in env.suppressions()  # 没做完整就不放开
    assert env.released and env.registry.holding is False  # 但暂缓的成交一定要释放，否则登记簿会一直停着
    assert env.alerts("recovery_step_failed")


async def test_fills_that_fail_to_apply_at_the_release_keep_the_suppression(make_env, monkeypatch):
    env = await make_env().boot()

    async def release(snapshot_ms):
        return FillRelease(applied=1, skipped=0, failed=2)

    monkeypatch.setattr(env.registry, "release_fills", release)
    result = await env.run()
    assert result.steps["release"]["failed"] == 2 and result.steps["release"]["recovering_lifted"] is False
    assert result.tradable is False and any("2 笔应用失败" in p for p in result.problems)
    assert Suppression.RECOVERING in env.suppressions()
    assert env.alerts("recovery_incomplete")[0].level is AlertLevel.CRITICAL


async def test_a_release_that_raises_is_reported_and_keeps_the_suppression(make_env, monkeypatch):
    env = await make_env().boot()

    async def release(snapshot_ms):
        raise RuntimeError("释放炸了")

    monkeypatch.setattr(env.registry, "release_fills", release)
    result = await env.run()
    assert "释放炸了" in result.steps["release"]["error"] and result.tradable is False
    assert Suppression.RECOVERING in env.suppressions()


async def test_a_failure_to_lift_the_suppression_is_reported(make_env, monkeypatch):
    env = await make_env().boot()

    async def lift(*args, **kwargs):
        raise RuntimeError("账本写失败")

    monkeypatch.setattr(env.modelstate, "remove_suppression", lift)
    result = await env.run()
    assert "账本写失败" in result.steps["release"]["lift_error"] and result.tradable is False
    assert Suppression.RECOVERING in env.suppressions()


async def test_later_steps_run_even_when_an_earlier_step_fails(make_env):
    env = await make_env().boot()
    env.reconciler.positions_result = RuntimeError("对账炸了")
    await env.add_position()
    env.positions = [pos()]
    await env.run()
    assert env.protection.rebuilt == [(1, "recovery")]  # 补保护照样做
    assert env.calls_named("reconcile_orders")  # 孤儿清理照样做


async def test_one_failing_rebuild_does_not_stop_the_others(make_env):
    env = await make_env().boot()
    first = await env.add_position(symbol="BTCUSDT")
    second = await env.add_position(symbol="ETHUSDT")
    env.positions = [pos("BTCUSDT"), pos("ETHUSDT")]
    env.protection.results[first.id] = RuntimeError("保护单管理出错")
    result = await env.run()
    assert {pid for pid, _ in env.protection.rebuilt} == {first.id, second.id}
    assert result.tradable is False


async def test_a_cancelled_recovery_releases_the_fills_but_never_lifts_the_suppression(make_env):
    """恢复被取消（比如装配给它设了启动时限）：暂缓的成交要放开，但没做完就不能解除「恢复中」。"""
    env = await make_env().boot()
    await env.add_position()
    env.positions = [pos()]
    entered = asyncio.Event()

    async def hang() -> None:
        entered.set()
        await asyncio.Event().wait()

    env.protection.gate = hang
    task = asyncio.create_task(env.run())
    await asyncio.wait_for(entered.wait(), 5)
    assert env.registry.holding is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.registry.holding is False and env.released  # 暂缓的成交放开了
    assert Suppression.RECOVERING in env.suppressions()  # 但没做完：不解除


async def test_a_run_that_completes_normally_is_not_mistaken_for_an_incomplete_one(make_env):
    env = await make_env().boot()
    result = await env.run()
    assert result.tradable is True and Suppression.RECOVERING not in env.suppressions()


# ================================================================ 重复运行、摘要、时长


async def test_running_twice_is_safe(make_env):
    env = await make_env().boot()
    await env.add_position()
    env.positions = [pos()]
    recovery = env.recovery()
    first = await recovery.run()
    second = await recovery.run()
    assert first.tradable and second.tradable
    assert Suppression.RECOVERING not in env.suppressions() and env.registry.holding is False
    assert len(env.released) == 2


async def test_the_result_has_a_summary_for_every_step(make_env):
    env = await make_env().boot()
    result = await env.run()
    for step in ("guard", "snapshot", "orders", "fills", "reconcile", "protection", "closing", "candles",
                 "orphans", "operations", "release", "summary"):
        assert step in result.steps, step
    summary = result.steps["summary"]
    for key in ("stops_rebuilt", "closed_by_fills", "pending_registered", "orphans_cleaned", "operations_interrupted"):
        assert key in summary, key
    assert result.steps["release"]["recovering_lifted"] is True


async def test_the_duration_is_measured_with_the_injected_clock(make_env):
    env = await make_env().boot()
    await env.add_position()
    env.positions = [pos()]
    env.protection.advance_ms = 2_000  # 补保护这一步「花了」两秒
    result = await env.run()
    assert result.duration_ms == 2_000


def test_the_result_is_a_frozen_dataclass():
    assert hasattr(Recovery, "run")
    result = RecoveryResult(tradable=True, problems=(), duration_ms=0, steps={})
    with pytest.raises(AttributeError):
        result.tradable = False  # type: ignore[misc]  # 冻结的数据类


async def test_the_summary_counts_what_was_done(make_env):
    env = await make_env().boot()
    await env.add_position()
    env.positions = [pos(), pos(SYM, qty="2", entry="90", mark="90", side=LONG.opposite())]
    result = await env.run()
    summary = result.steps["summary"]
    assert summary["stops_rebuilt"] == 1  # 登记仓补了保护
    assert summary["pending_registered"] == 1  # 交易所上另一侧的仓登记簿没有：进入待确认


async def test_only_positions_that_ended_up_with_a_system_stop_count_as_protected(make_env):
    """rebuild 的其它结论（用户自己的止损、还在重试、保护性平仓已发出）如实写在步骤摘要里，但不算「补上了止损」。"""
    env = await make_env().boot()
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
    recs = [await env.add_position(symbol=symbol) for symbol in symbols]
    env.positions = [pos(symbol) for symbol in symbols]
    for rec, outcome in zip(recs, ["protected", "external", "unprotected", "closing"]):
        env.protection.results[rec.id] = outcome
    result = await env.run()
    assert [e["result"] for e in result.steps["protection"]["rebuilt"]] == ["protected", "external", "unprotected",
                                                                            "closing"]
    assert result.steps["summary"]["stops_rebuilt"] == 1
    (done,) = env.alerts("recovery_done")
    assert "补保护 1 个仓" in done.message
