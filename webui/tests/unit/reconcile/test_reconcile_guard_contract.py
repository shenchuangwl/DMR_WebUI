"""对账与周期复核（core.guard）的约定：只有 ``ReconcileCompleted(kind="positions", status="ok")`` 才结束「数据流切换中」，
所以 ok 必须代表一轮完整、成功的持仓对账——未知、不一致、没能复核清楚的轮次都不能是 ok。
"""
from __future__ import annotations

from dmr_kernel.core.events import StreamStatusChanged
from dmr_kernel.core.guard import Guard, LegacyMutex
from dmr_kernel.core.types import RunMode, Suppression
from dmr_kernel.settings import load_settings
from reconcile_kit import LONG, SYM, T0, pos


async def test_only_a_complete_successful_positions_round_ends_the_stream_switching(make_env, tmp_path, monkeypatch):
    env = await make_env().boot()
    settings = load_settings({"DMR_ENV": "standin"}, webui_root=tmp_path / "DMR_WebUI" / "webui")
    legacy = LegacyMutex(tmp_path / "must_not_exist" / "runtime_state", enabled=False)
    values = {**env.config, "ex_stream_enabled": True}
    guard = Guard(settings=settings, adapter=env.adapter, modelstate=env.modelstate, bus=env.bus, clock=env.clock,
                  config_get=values.__getitem__, notifier=env.notifier, legacy=legacy)
    await guard.start()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1")]

    env.bus.publish(StreamStatusChanged(state="interrupted", reason="测试", at_ms=T0))
    await env.settle()
    assert Suppression.STREAM_SWITCHING in env.suppressions()

    env.fail("fetch_positions")  # 读不到：未知
    assert await env.reconciler.reconcile_positions() == "unknown"
    await env.settle()
    assert Suppression.STREAM_SWITCHING in env.suppressions()
    env.heal()

    env.positions = [pos(SYM, LONG, "2")]  # 无法解释的数量差异：不一致
    assert await env.reconciler.reconcile_positions() == "mismatch"
    await env.settle()
    assert Suppression.STREAM_SWITCHING in env.suppressions()

    async def broken(symbols):
        raise RuntimeError("账本忙")

    monkeypatch.setattr(env.ingest, "backfill_trades", broken)  # 补拉不成：这一轮对这个键不下结论
    env.positions = [pos(SYM, LONG, "3")]
    assert await env.reconciler.reconcile_positions() == "unknown"
    await env.settle()
    assert Suppression.STREAM_SWITCHING in env.suppressions()

    env.positions = [pos(SYM, LONG, "1")]  # 一致了：完整、成功的一轮
    assert await env.reconciler.reconcile_positions() == "ok"
    await env.settle()
    assert Suppression.STREAM_SWITCHING not in env.suppressions()
    assert env.modelstate.current().run_mode is RunMode.POLLING_DEGRADED  # 数据流还没恢复稳定：进入仅轮询降级
    assert Suppression.RECONCILE_MISMATCH in env.suppressions()  # 「对账不一致」仍要人工确认，与此无关


async def test_an_orders_round_never_ends_the_stream_switching(make_env, tmp_path):
    env = await make_env().boot()
    settings = load_settings({"DMR_ENV": "standin"}, webui_root=tmp_path / "DMR_WebUI" / "webui")
    guard = Guard(settings=settings, adapter=env.adapter, modelstate=env.modelstate, bus=env.bus, clock=env.clock,
                  config_get={**env.config, "ex_stream_enabled": True}.__getitem__, notifier=env.notifier,
                  legacy=LegacyMutex(tmp_path / "must_not_exist" / "runtime_state", enabled=False))
    await guard.start()
    env.bus.publish(StreamStatusChanged(state="interrupted", reason="测试", at_ms=T0))
    await env.settle()

    assert await env.reconciler.reconcile_orders() == "ok"
    await env.settle()
    assert Suppression.STREAM_SWITCHING in env.suppressions()
