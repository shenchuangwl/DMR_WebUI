"""K 线作业的触发时刻（UTC 周期边界 + kline_delay_s）与「刚收盘的是哪一根」的计算；环境检查。

纯函数测试：不起内核。本机时区是 +07，触发时刻必须与本机时区无关。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dmr_kernel.core.clock import interval_ms
from dmr_kernel.core.configdefs import TIMEFRAMES
from dmr_kernel.core.scheduler import UTC, bar_close_trigger, last_closed_bar_open_ms, refusal_reason
from tests.support.kernel import make_settings

NOW = datetime(2026, 10, 16, 10, 7, 30, tzinfo=timezone.utc)


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# ---------------------------------------------------------------- 触发时刻


def test_the_15m_trigger_fires_at_the_utc_boundary_plus_the_delay():
    trigger = bar_close_trigger("15m", 3)
    first = trigger.get_next_fire_time(None, NOW)
    assert first == datetime(2026, 10, 16, 10, 15, 3, tzinfo=timezone.utc)
    second = trigger.get_next_fire_time(first, first)
    assert second == datetime(2026, 10, 16, 10, 30, 3, tzinfo=timezone.utc)


def test_the_trigger_fires_at_the_instant_itself_but_not_a_moment_before_it():
    trigger = bar_close_trigger("15m", 3)
    due = datetime(2026, 10, 16, 10, 15, 3, tzinfo=timezone.utc)
    assert trigger.get_next_fire_time(None, due) == due  # 恰好到点：这一次就是它
    assert trigger.get_next_fire_time(None, due + timedelta(microseconds=1)) == due + timedelta(minutes=15)
    assert trigger.get_next_fire_time(None, due - timedelta(microseconds=1)) == due


@pytest.mark.parametrize("delay_s", [0, 3, 59, 60])
def test_the_delay_is_added_to_the_boundary_for_every_allowed_value(delay_s):
    """kline_delay_s 的取值范围是 0 到 60 秒（60 秒也要对）。"""
    trigger = bar_close_trigger("15m", delay_s)
    expected = datetime(2026, 10, 16, 10, 15, 0, tzinfo=timezone.utc) + timedelta(seconds=delay_s)
    assert trigger.get_next_fire_time(None, NOW) == expected


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_every_timeframe_is_aligned_to_the_utc_epoch(timeframe):
    """所有允许的周期：触发时刻减去延迟是该周期的整数倍（纪元对齐 = UTC 边界），相邻两次正好差一个周期。"""
    step = interval_ms(timeframe)
    trigger = bar_close_trigger(timeframe, 3)
    fire = trigger.get_next_fire_time(None, NOW)
    assert fire > NOW
    assert (ms(fire) - 3000) % step == 0
    assert ms(fire) - ms(NOW) <= step + 3000
    nxt = trigger.get_next_fire_time(fire, fire)
    assert ms(nxt) - ms(fire) == step


def test_the_trigger_does_not_depend_on_the_local_timezone(bangkok):
    assert datetime.now().astimezone().utcoffset() == timedelta(hours=7)  # 前提：本地确实是 +07
    trigger = bar_close_trigger("15m", 3)
    fire = trigger.get_next_fire_time(None, NOW)
    assert fire == datetime(2026, 10, 16, 10, 15, 3, tzinfo=timezone.utc)
    assert fire.utcoffset() == timedelta(0)
    assert trigger.timezone == UTC


def test_the_trigger_catches_up_with_a_stalled_clock_by_one_run_only():
    """错过很多个周期后，下一次触发是「现在之后的第一个边界」，不是被错过的那一堆（合并成一次由调度器的 coalesce 保证）。"""
    trigger = bar_close_trigger("15m", 3)
    late = NOW + timedelta(hours=5, minutes=2)
    assert trigger.get_next_fire_time(None, late) == datetime(2026, 10, 16, 15, 15, 3, tzinfo=timezone.utc)


# ---------------------------------------------------------------- 刚收盘的是哪一根


def at(hour: int, minute: int, second: float = 0.0) -> int:
    return ms(datetime(2026, 10, 16, hour, minute, 0, tzinfo=timezone.utc)) + int(second * 1000)


def test_the_bar_that_just_closed_is_the_one_before_the_boundary():
    # 10:15:03 触发：10:00 开盘的那根在 10:15:00 收盘
    assert last_closed_bar_open_ms(at(10, 15, 3), "15m", 3) == at(10, 0)
    assert last_closed_bar_open_ms(at(10, 15, 20), "15m", 3) == at(10, 0)  # 晚了一点也还是这一根
    assert last_closed_bar_open_ms(at(10, 29, 59.999), "15m", 3) == at(10, 0)
    assert last_closed_bar_open_ms(at(10, 30, 3), "15m", 3) == at(10, 15)


def test_before_the_delay_has_passed_the_previous_bar_is_still_the_latest():
    """还没到「边界 + 延迟」时，上一个边界收盘的那根还不算（交易所可能还没出）。"""
    assert last_closed_bar_open_ms(at(10, 15, 2.999), "15m", 3) == at(9, 45)
    assert last_closed_bar_open_ms(at(10, 15, 0), "15m", 3) == at(9, 45)


def test_the_long_timeframe_uses_its_own_boundary():
    assert last_closed_bar_open_ms(at(11, 0, 3), "1h", 3) == at(10, 0)
    assert last_closed_bar_open_ms(at(10, 59, 59), "1h", 3) == at(9, 0)
    assert last_closed_bar_open_ms(at(12, 0, 3), "2h", 3) == at(10, 0)


def test_the_maximum_delay_of_sixty_seconds():
    assert last_closed_bar_open_ms(at(10, 16, 0), "15m", 60) == at(10, 0)
    assert last_closed_bar_open_ms(at(10, 15, 59.999), "15m", 60) == at(9, 45)


def test_an_unknown_timeframe_is_a_programming_error():
    with pytest.raises(ValueError):
        last_closed_bar_open_ms(at(10, 15, 3), "7x", 3)
    with pytest.raises(ValueError):
        bar_close_trigger("7x", 3)


# ---------------------------------------------------------------- 环境检查


def test_only_the_standin_environment_is_allowed_to_start(tmp_path):
    assert refusal_reason(make_settings(tmp_path)) is None


@pytest.mark.parametrize("env", ["testnet", "mainnet_shadow", "mainnet_live"])
def test_every_other_environment_is_refused_with_the_reason(tmp_path, env):
    reason = refusal_reason(make_settings(tmp_path, DMR_ENV=env, DMR_ALLOW_LIVE="1"))
    assert reason is not None
    assert env in reason
    assert "Demo Trading" in reason  # 「Binance 私有接口要在接入 Demo Trading 时实现」
