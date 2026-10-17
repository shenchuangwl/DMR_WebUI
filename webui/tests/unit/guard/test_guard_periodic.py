"""Guard.periodic（每 60 秒）：持仓模式、旧入口锁与停用标记（只在实盘）、每 5 分钟一次的交易所限制；以及 confirm_mutex。

时间全用 ManualClock 推进；锁与标记用 tmp_path 下模拟的 data/runtime_state；另一个进程持锁用 subprocess。
"""
from __future__ import annotations

import asyncio
import os

import pytest

from dmr_kernel.core.types import AlertLevel, RestrictionInfo, Suppression, Unknown
from lock_kit import MARKER, NAMES, lock_path, put_marker
from guard_kit import build_env
from tests.support.fake_adapter import FakeAdapter

MINUTE = 60_000
FIVE_MINUTES = 300_000
JITTER = 1_000  # 调度抖动容限（core.guard._SCHEDULE_JITTER_MS）


async def run_minutes(env, minutes: int) -> None:
    """走 minutes 个周期：每次推进一分钟再 periodic。"""
    for _ in range(minutes):
        env.clock.advance(MINUTE)
        await env.guard.periodic()


# ---------------------------------------------------------------- 持仓模式


async def test_the_position_mode_is_read_on_every_periodic_run(make_env):
    env = make_env()
    await env.boot()

    for _ in range(3):
        await env.guard.periodic()
        env.clock.advance(MINUTE)

    assert len(env.adapter.calls("fetch_position_mode")) == 3
    assert env.alerts() == []


async def test_a_position_mode_that_is_not_hedge_raises_a_critical_alert_but_no_suppression(make_env):
    env = make_env()
    env.adapter.set_read("fetch_position_mode", False)
    await env.boot()

    await env.guard.periodic()

    [alert] = env.alerts("position_mode_not_hedge")
    assert alert.level is AlertLevel.CRITICAL and alert.dedupe_key == "position_mode_not_hedge"
    assert env.state.suppressions == frozenset()  # 持仓模式没有对应的抑制原因：运行期只告警


async def test_a_persistent_wrong_position_mode_keeps_reminding_through_the_notifier_dedupe(make_env):
    env = make_env()
    env.adapter.set_read("fetch_position_mode", False)
    await env.boot()

    await run_minutes(env, 3)

    assert len(env.alerts("position_mode_not_hedge")) == 3  # 去重由告警器按 dedupe_key 做
    assert {a.dedupe_key for a in env.alerts()} == {"position_mode_not_hedge"}


async def test_a_restored_position_mode_stops_the_alerts(make_env):
    env = make_env()
    env.adapter.set_read("fetch_position_mode", False)
    await env.boot()
    await env.guard.periodic()
    env.adapter.set_read("fetch_position_mode", True)

    await run_minutes(env, 2)

    assert len(env.alerts("position_mode_not_hedge")) == 1


async def test_an_unknown_position_mode_neither_alerts_nor_raises(make_env):
    env = make_env()
    env.adapter.set_read("fetch_position_mode", Unknown("超时"))
    await env.boot()

    await env.guard.periodic()

    assert env.alerts() == []
    assert env.guard.status()["position_mode"] == "unknown"


# ---------------------------------------------------------------- 交易所限制：第一次立即读，之后每 5 分钟


async def test_restrictions_are_read_on_the_first_run_and_then_every_five_minutes(make_env):
    env = make_env()
    await env.boot()

    def reads() -> int:
        return len(env.adapter.calls("fetch_restrictions"))

    await env.guard.periodic()
    assert reads() == 1
    for minute in range(1, 5):
        env.clock.advance(MINUTE)
        await env.guard.periodic()
        assert reads() == 1, f"第 {minute} 分钟不该再读"
    env.clock.advance(MINUTE)
    await env.guard.periodic()
    assert reads() == 2


async def test_the_five_minute_boundary_allows_one_second_of_scheduler_jitter(make_env):
    """第 5 次调用与上次读取相隔 299.x 秒是调度抖动，不能因此顺延到第 6 分钟；但再早就不读。"""
    env = make_env()
    await env.boot()
    await env.guard.periodic()

    env.clock.advance(FIVE_MINUTES - JITTER - 1)
    await env.guard.periodic()
    assert len(env.adapter.calls("fetch_restrictions")) == 1

    env.clock.advance(1)
    await env.guard.periodic()
    assert len(env.adapter.calls("fetch_restrictions")) == 2


async def test_scheduler_jitter_does_not_stretch_the_cycle_to_six_minutes(make_env):
    env = make_env()
    await env.boot()

    for _ in range(11):  # 每次比 60 秒早 0.1 秒：第 0、5、10 次调用各读一次
        await env.guard.periodic()
        env.clock.advance(MINUTE - 100)

    assert len(env.adapter.calls("fetch_restrictions")) == 3


async def test_restrictions_read_at_startup_are_not_read_again_within_five_minutes(make_env):
    env = make_env()
    await env.boot()
    await env.guard.startup()

    await run_minutes(env, 4)

    assert len(env.adapter.calls("fetch_restrictions")) == 1
    await run_minutes(env, 1)
    assert len(env.adapter.calls("fetch_restrictions")) == 2


async def test_a_restriction_sets_the_suppression_and_a_lift_clears_it(make_env):
    env = make_env()
    env.adapter.set_read("fetch_restrictions", RestrictionInfo(True, (("rule", "order_rate_10m"), ("until", "3600"))))
    await env.boot()

    await env.guard.periodic()

    assert Suppression.EXCHANGE_RESTRICTED in env.state.suppressions
    [alert] = env.alerts("exchange_restricted")
    assert alert.level is AlertLevel.WARNING and "order_rate_10m" in alert.message

    env.adapter.set_read("fetch_restrictions", RestrictionInfo(False))
    await run_minutes(env, 5)

    assert Suppression.EXCHANGE_RESTRICTED not in env.state.suppressions
    [lifted] = env.alerts("exchange_restriction_lifted")
    assert lifted.level is AlertLevel.INFO


async def test_a_continuing_restriction_alerts_only_when_it_starts(make_env):
    env = make_env()
    env.adapter.set_read("fetch_restrictions", RestrictionInfo(True))
    await env.boot()

    await run_minutes(env, 11)  # 读了 3 次（0、5、10 分钟之后）

    assert len(env.adapter.calls("fetch_restrictions")) == 3
    assert len(env.alerts("exchange_restricted")) == 1
    assert Suppression.EXCHANGE_RESTRICTED in env.state.suppressions


async def test_an_unknown_restriction_read_changes_nothing_and_is_retried_next_minute(make_env):
    env = make_env()
    env.adapter.set_read("fetch_restrictions", Unknown("超时"))
    await env.boot()

    await env.guard.periodic()
    assert env.state.suppressions == frozenset()  # 未知也不能当成「受限」
    await run_minutes(env, 1)

    assert len(env.adapter.calls("fetch_restrictions")) == 2  # 没读到不等 5 分钟


async def test_an_unknown_restriction_read_does_not_lift_an_existing_restriction(make_env):
    env = make_env()
    env.adapter.set_read("fetch_restrictions", RestrictionInfo(True))
    await env.boot()
    await env.guard.periodic()
    env.adapter.set_read("fetch_restrictions", Unknown("超时"))

    await run_minutes(env, 5)

    assert Suppression.EXCHANGE_RESTRICTED in env.state.suppressions  # 未知不能当成「已解除」
    assert env.alerts("exchange_restriction_lifted") == []


async def test_the_restriction_cycle_can_repeat(make_env):
    env = make_env()
    env.adapter.set_read("fetch_restrictions", RestrictionInfo(True))
    await env.boot()
    await env.guard.periodic()
    env.adapter.set_read("fetch_restrictions", RestrictionInfo(False))
    await run_minutes(env, 5)
    env.adapter.set_read("fetch_restrictions", RestrictionInfo(True))

    await run_minutes(env, 5)

    assert Suppression.EXCHANGE_RESTRICTED in env.state.suppressions
    assert len(env.alerts("exchange_restricted")) == 2


# ---------------------------------------------------------------- 实盘：锁与停用标记


async def test_periodic_in_live_is_quiet_when_every_lock_and_the_marker_are_in_place(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    assert await env.guard.startup() == []

    await run_minutes(env, 3)

    assert env.state.suppressions == frozenset()
    assert env.alerts() == []


async def test_a_lost_stop_marker_sets_the_mutex_anomaly(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    (env.runtime_dir / MARKER).unlink()  # 例如 git clean -x

    await env.guard.periodic()

    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    [alert] = env.alerts("legacy_mutex_anomaly")
    assert alert.level is AlertLevel.CRITICAL and MARKER in alert.message
    assert alert.dedupe_key == "legacy_mutex_anomaly"


@pytest.mark.parametrize("how", ["delete_then_create", "rename_away_then_create"])
async def test_a_recreated_lock_file_sets_the_mutex_anomaly(make_env, how):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    path = lock_path(env.runtime_dir, "bit_long_term")
    if how == "delete_then_create":
        path.unlink()
    else:
        path.rename(env.runtime_dir / "moved-away")
    path.write_text("")  # 旧入口 open('a+') 建出了新文件，并取到了它上面的锁

    await env.guard.periodic()

    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    [alert] = env.alerts("legacy_mutex_anomaly")
    assert "bit_long_term" in alert.message and "替换" in alert.message


async def test_a_deleted_lock_file_sets_the_mutex_anomaly(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    lock_path(env.runtime_dir, "main").unlink()

    await env.guard.periodic()

    assert Suppression.MUTEX_ANOMALY in env.state.suppressions


async def test_a_directory_that_lost_its_0700_mode_sets_the_mutex_anomaly(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    os.chmod(env.runtime_dir, 0o755)
    try:
        await env.guard.periodic()
    finally:
        os.chmod(env.runtime_dir, 0o700)

    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    [alert] = env.alerts("legacy_mutex_anomaly")
    assert "0700" in alert.message


async def test_a_removed_runtime_directory_sets_the_mutex_anomaly(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    for entry in env.runtime_dir.iterdir():
        entry.unlink()
    env.runtime_dir.rmdir()

    await env.guard.periodic()

    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    assert not env.runtime_dir.exists()  # 周期复核不重建目录


async def test_every_problem_goes_into_one_alert(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    (env.runtime_dir / MARKER).unlink()
    lock_path(env.runtime_dir, "first_short_term").unlink()

    await env.guard.periodic()

    [alert] = env.alerts("legacy_mutex_anomaly")
    assert MARKER in alert.message and "first_short_term" in alert.message


async def test_the_mutex_anomaly_stays_after_the_problem_goes_away(make_env):
    """互斥异常要人工确认才解除：标记补回来了也不自动清。"""
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    (env.runtime_dir / MARKER).unlink()
    await env.guard.periodic()
    put_marker(env.runtime_dir)

    await run_minutes(env, 3)

    assert Suppression.MUTEX_ANOMALY in env.state.suppressions


async def test_a_lock_that_was_never_obtained_stays_an_anomaly_on_every_run(make_env, holders):
    env = make_env(environment="mainnet_live")
    holder = holders(lock_path(env.runtime_dir, "legacy_short_term"))
    await env.boot()
    await env.guard.startup()

    await run_minutes(env, 2)

    assert len(env.alerts("legacy_mutex_anomaly")) == 3  # 启动 1 次 + 周期 2 次，由告警器按 dedupe_key 去重
    assert all(str(holder.pid) in a.message for a in env.alerts("legacy_mutex_anomaly"))


async def test_live_with_a_disabled_mutex_object_is_flagged_by_periodic_too(make_env):
    env = make_env(environment="mainnet_live", mutex_enabled=False)
    await env.boot()

    await env.guard.periodic()

    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    [alert] = env.alerts("legacy_mutex_anomaly")
    assert "未启用" in alert.message


# ---------------------------------------------------------------- 非实盘：不碰任何路径


async def test_periodic_outside_live_never_touches_the_legacy_runtime_directory(make_env):
    env = make_env()  # 目录不存在；夹具收尾时核对它仍不存在
    await env.boot()

    await run_minutes(env, 6)

    assert env.state.suppressions == frozenset()
    assert env.legacy.held == ()


async def test_periodic_outside_live_ignores_an_enabled_mutex_object(make_env, tmp_path):
    target = tmp_path / "elsewhere" / "runtime_state"
    env = make_env(mutex_enabled=True, runtime=target)
    await env.boot()

    await run_minutes(env, 2)

    assert not target.parent.exists()
    assert env.state.suppressions == frozenset()


# ---------------------------------------------------------------- 彼此独立、不重入


async def test_a_failing_check_does_not_stop_the_others(make_env):
    env = make_env(environment="mainnet_live")

    def explode(**_):
        raise RuntimeError("适配层坏了")

    env.adapter.set_read("fetch_position_mode", explode)
    await env.boot()
    assert env.legacy.acquire_all() == []
    (env.runtime_dir / MARKER).unlink()

    await env.guard.periodic()  # 不抛

    assert Suppression.MUTEX_ANOMALY in env.state.suppressions  # 持仓模式读失败，锁与标记照查
    assert len(env.adapter.calls("fetch_restrictions")) == 1  # 交易所限制照查


class SlowAdapter(FakeAdapter):
    """持仓模式的读取卡住，直到放行；记录同时在途的读取数。"""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def fetch_position_mode(self):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await self.release.wait()
            return await super().fetch_position_mode()
        finally:
            self.active -= 1


async def test_overlapping_periodic_runs_are_serialised(tmp_path, clock, ledger, bus):
    adapter = SlowAdapter()
    env = build_env(tmp_path, clock, ledger, bus, adapter=adapter)
    await env.boot()

    first = asyncio.create_task(env.guard.periodic())
    second = asyncio.create_task(env.guard.periodic())
    for _ in range(5):
        await asyncio.sleep(0)
    assert adapter.active == 1  # 第二次在等第一次
    adapter.release.set()
    await asyncio.gather(first, second)

    assert adapter.max_active == 1
    assert len(adapter.calls("fetch_position_mode")) == 2


# ---------------------------------------------------------------- confirm_mutex：人工确认后才解除


async def test_confirm_mutex_keeps_the_anomaly_while_a_problem_remains(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    (env.runtime_dir / MARKER).unlink()
    await env.guard.periodic()

    problems = await env.guard.confirm_mutex(operator="alice")

    assert len(problems) == 1 and MARKER in problems[0]
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    assert env.alerts("legacy_mutex_restored") == []


async def test_confirm_mutex_lifts_the_anomaly_once_everything_is_restored(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    (env.runtime_dir / MARKER).unlink()
    await env.guard.periodic()
    put_marker(env.runtime_dir)

    assert await env.guard.confirm_mutex(operator="alice") == []

    assert Suppression.MUTEX_ANOMALY not in env.state.suppressions
    [alert] = env.alerts("legacy_mutex_restored")
    assert alert.level is AlertLevel.INFO and "alice" in alert.message
    await run_minutes(env, 2)  # 之后的周期复核保持正常
    assert Suppression.MUTEX_ANOMALY not in env.state.suppressions


async def test_confirm_mutex_retakes_a_replaced_lock_file(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    path = lock_path(env.runtime_dir, "legacy_long_term")
    path.unlink()
    path.write_text("")
    await env.guard.periodic()
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions

    assert await env.guard.confirm_mutex(operator="alice") == []

    assert Suppression.MUTEX_ANOMALY not in env.state.suppressions
    assert env.legacy.held == NAMES
    assert path.read_text().strip() == str(os.getpid())  # 新文件上的锁也拿住了
    assert env.legacy.check() == []


async def test_confirm_mutex_refuses_while_an_old_entry_holds_the_new_lock_file(make_env, holders):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    path = lock_path(env.runtime_dir, "bit_short_term")
    path.unlink()
    holder = holders(path)
    await env.guard.periodic()

    problems = await env.guard.confirm_mutex(operator="alice")

    assert len(problems) == 1 and str(holder.pid) in problems[0]
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions

    holder.stop()  # 旧入口退出后再确认
    assert await env.guard.confirm_mutex(operator="alice") == []
    assert Suppression.MUTEX_ANOMALY not in env.state.suppressions


async def test_confirm_mutex_sets_the_anomaly_when_it_finds_a_problem_nobody_flagged_yet(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    (env.runtime_dir / MARKER).unlink()

    problems = await env.guard.confirm_mutex(operator="alice")

    assert len(problems) == 1
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions


async def test_confirm_mutex_without_any_anomaly_is_harmless(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    version = env.state.version

    assert await env.guard.confirm_mutex(operator="alice") == []

    assert env.state.version == version
    assert env.alerts("legacy_mutex_restored") == []


async def test_confirm_mutex_outside_live_clears_a_stale_anomaly_without_touching_any_path(make_env):
    env = make_env()  # 目录不存在；夹具收尾时核对它仍不存在
    await env.boot()
    await env.modelstate.add_suppression(Suppression.MUTEX_ANOMALY, reason="换环境前遗留")

    assert await env.guard.confirm_mutex(operator="alice") == []

    assert Suppression.MUTEX_ANOMALY not in env.state.suppressions


async def test_confirm_mutex_in_live_with_a_disabled_mutex_object_keeps_the_anomaly(make_env):
    env = make_env(environment="mainnet_live", mutex_enabled=False)
    await env.boot()
    await env.guard.startup()

    problems = await env.guard.confirm_mutex(operator="alice")

    assert len(problems) == 1 and "未启用" in problems[0]
    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    assert not list(env.runtime_dir.glob("instance_*.lock"))


# ---------------------------------------------------------------- 没有先 startup、读到怪东西


async def test_periodic_in_live_without_startup_reports_every_lock_as_not_held(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()

    await env.guard.periodic()

    assert Suppression.MUTEX_ANOMALY in env.state.suppressions
    [alert] = env.alerts("legacy_mutex_anomaly")
    assert all(name in alert.message for name in NAMES)


async def test_a_restriction_answer_that_is_not_restriction_info_changes_nothing(make_env):
    env = make_env()
    env.adapter.set_read("fetch_restrictions", "weird")
    await env.boot()

    await env.guard.periodic()

    assert env.state.suppressions == frozenset()
    assert env.guard.status()["restricted"] is None
