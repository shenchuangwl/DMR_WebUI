"""重启恢复第 7 步：回补停机期间的 K 线（预热 + 刷新，短周期和长周期）。含「反向信号平仓」的仓的反向过零补判在 M4 接入。"""
from __future__ import annotations

from recovery_kit import SYM2, open_order, pos


async def test_candles_for_the_monitored_symbols_are_warmed_up_and_refreshed_for_both_timeframes(make_env):
    env = await make_env().boot()
    await env.add_position(symbol="BTCUSDT")
    env.positions = [pos("BTCUSDT"), pos(SYM2)]
    env.open_orders = [open_order("DOGEUSDT")]
    result = await env.run()
    symbols = ("BTCUSDT", "DOGEUSDT", "ETHUSDT")
    assert result.steps["candles"]["symbols"] == list(symbols)
    assert result.steps["candles"]["timeframes"] == ["15m", "1h"]
    assert (symbols, "15m") in env.candles.warmed and (symbols, "1h") in env.candles.warmed
    assert (symbols, "15m") in env.candles.refreshed and (symbols, "1h") in env.candles.refreshed
    assert env.candles.synced >= 1


async def test_the_server_time_is_synced_before_the_candles_are_read(make_env):
    env = await make_env().boot()
    env.positions = [pos()]
    await env.run()
    sync = env.index_of("candles.sync")
    warm = next(i for i, c in enumerate(env.calls) if isinstance(c, tuple) and c[0] == "candles.warm_up")
    assert sync < warm


async def test_nothing_is_fetched_when_there_is_nothing_to_monitor(make_env):
    env = await make_env().boot()
    result = await env.run()
    assert result.steps["candles"]["symbols"] == []
    assert env.candles.warmed == [] and env.candles.refreshed == [] and env.candles.synced == 0


async def test_the_reverse_cross_replay_is_left_for_m4(make_env):
    env = await make_env().boot()
    result = await env.run()
    assert "M4" in result.steps["candles"]["reverse_cross"]


async def test_a_candle_failure_is_a_problem_but_does_not_stop_later_steps(make_env):
    env = await make_env().boot()
    env.positions = [pos()]

    async def boom(symbols, timeframe, limit=500):
        raise RuntimeError("账本读不出 K 线")

    env.candles.warm_up = boom
    result = await env.run()
    assert "error" in result.steps["candles"] and result.tradable is False
    assert env.calls_named("reconcile_orders")  # 后面的孤儿清理照常做
