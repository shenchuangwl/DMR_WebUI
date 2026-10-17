"""故障 2：止损建单失败（替身 ``algo_create_fail``）。按报告 4.11 的特别标准：

- 策略仓：在 ``protect_retry_window_s + close_one_timeout_s`` 秒内（缺省 30 + 15）被市价平掉；
- 接管仓：在 ``protect_retry_window_s`` 秒内（缺省 30）转为「无保护」、置「存在无保护持仓」抑制、发最高级告警，之后持续重试
  （不自动平掉用户的仓）；故障消失后挂上止损、抑制解除。

两种失败形式都测：``reject``（400 −2010，确定没建）和 ``unknown``（503，不知道建没建；替身其实没建）。反例：只失败一次时
在重试窗口里挂上，不平仓。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import AlertLevel, ExitReason, Ownership, PositionStatus, ProtectionState, Suppression
from tests.faults.kit import Rig, fill_time_ms

ALGO = "/api/private/algo_order"
RETRY_WINDOW_S = 30  # protect_retry_window_s 缺省
CLOSE_TIMEOUT_S = 15  # close_one_timeout_s 缺省
LAST_RETRY_MS = RETRY_WINDOW_S * 1000 * 3 // 4  # protect_retries 缺省 3：重试在 7.5、15、22.5 秒


def algo_fail(mode: str, count: int | None = None) -> dict:
    return {"faults": {"algo_create_fail": {"enabled": True, "mode": mode, "count": count}}}


def algo_posts(rig: Rig) -> int:
    return len(rig.standin.transport.sent("POST", ALGO))


@pytest.mark.parametrize("mode", ["reject", "unknown"])
async def test_a_strategy_position_that_cannot_be_protected_is_closed_at_market_in_time(tmp_path, mode):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(algo_fail(mode))
        record = await rig.open_strategy()
        filled_ms = fill_time_ms(rig, record.id)
        position_id = rig.position().id
        assert rig.position().ownership is Ownership.STRATEGY
        flat = await rig.run_until(lambda: rig.exchange_positions() == {},
                                   limit_s=RETRY_WINDOW_S + CLOSE_TIMEOUT_S)
        assert flat is not None, "策略仓挂不上止损时应当在 45 秒内被市价平掉"
        assert rig.now_ms() - filled_ms <= (RETRY_WINDOW_S + CLOSE_TIMEOUT_S) * 1000
        # 不是一失败就平：先在窗口里重试（第 k 次在 k × 30/(3+1) 秒），最后一次（22.5 秒）也失败才平
        assert rig.now_ms() - filled_ms >= LAST_RETRY_MS
        await rig.step(5)
        closed = await rig.kernel.registry.fetch(position_id)
        assert closed.status is PositionStatus.CLOSED
        assert closed.exit_reason is not ExitReason.STOP  # 是保护性市价平仓，不是止损出场
        assert rig.open_algos() == []  # 没有残留的条件单
        assert await rig.alerts("protect_failed_closing")
        await rig.step(60)
        rig.assert_general()
    finally:
        await rig.stop()


@pytest.mark.parametrize("mode", ["reject", "unknown"])
async def test_a_takeover_position_that_cannot_be_protected_turns_unprotected_and_keeps_retrying(tmp_path, mode):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(algo_fail(mode))
        taken_ms = rig.now_ms()
        pos = await rig.take_over_manual()
        assert pos.ownership is Ownership.TAKEOVER

        async def verdict() -> bool:
            rec = rig.position()
            return (rec.protection_state is ProtectionState.NONE and Suppression.UNPROTECTED in rig.suppressions()
                    and any(a.level is AlertLevel.CRITICAL for a in await rig.alerts("protect_unprotected")))

        reached = await rig.run_until(verdict, limit_s=RETRY_WINDOW_S)
        assert reached is not None, "接管仓 30 秒内应当转为无保护、置抑制并发最高级告警"
        assert rig.now_ms() - taken_ms <= RETRY_WINDOW_S * 1000
        tries_at_verdict = algo_posts(rig)
        await rig.step(60)
        assert algo_posts(rig) > tries_at_verdict  # 之后持续重试
        assert rig.exchange_positions() == {("BTCUSDT", "LONG"): pos.qty}  # 接管仓不自动平掉
        assert rig.position().protection_state is ProtectionState.NONE
        rig.assert_general()

        rig.standin.disp.switches.update({"faults": {"algo_create_fail": {"enabled": False}}})
        healed = await rig.run_until(rig.has_stop, limit_s=RETRY_WINDOW_S)
        assert healed is not None, "故障消失后持续重试应当把止损挂上"
        await rig.step(4)
        assert rig.position().protection_state is ProtectionState.SYSTEM
        assert Suppression.UNPROTECTED not in rig.suppressions()
        assert len(rig.system_stops()) == 1
        rig.assert_general()
    finally:
        await rig.stop()


async def test_a_single_failure_is_retried_inside_the_window_and_nothing_is_closed(tmp_path):
    rig = await Rig.start(tmp_path)
    try:
        rig.standin.disp.switches.update(algo_fail("reject", count=1))
        record = await rig.open_strategy()
        filled_ms = fill_time_ms(rig, record.id)
        assert not rig.has_stop()
        waited = await rig.run_until(rig.has_stop, limit_s=RETRY_WINDOW_S)
        assert waited is not None and rig.now_ms() - filled_ms <= RETRY_WINDOW_S * 1000
        await rig.step(60)
        assert rig.exchange_positions() == {("BTCUSDT", "LONG"): rig.position().qty}  # 没平
        assert rig.position().protection_state is ProtectionState.SYSTEM
        assert Suppression.UNPROTECTED not in rig.suppressions()
        assert await rig.alerts("protect_failed_closing") == []
        rig.assert_general()
    finally:
        await rig.stop()


async def test_a_trailing_update_whose_new_stop_is_rejected_keeps_the_gap_within_stop_gap_max_s(tmp_path):
    """移动止损（缺省 gap 模式：先撤旧、后挂新）时新止损被拒两次（新单与恢复旧单都挂不上）：无保护间隙不超过
    ``stop_gap_max_s``，之后止损在、仓没被平。"""
    from tests.faults.kit import STOP_GAP_MAX_S, trail_up, watch_gap

    rig = await Rig.start(tmp_path)
    try:
        record = await rig.open_strategy()
        assert record.state is not None and rig.has_stop()
        old = rig.system_stop()
        qty = rig.position().qty
        rig.standin.disp.switches.update(algo_fail("reject", count=2))
        await trail_up(rig)
        assert rig.standin.disp.switches.fault("algo_create_fail")["enabled"] is False  # 故障确实打在这次更新上
        gap, present = await watch_gap(rig, STOP_GAP_MAX_S + 30)
        assert gap <= STOP_GAP_MAX_S, f"移动止损的无保护间隙 {gap} 秒超过 stop_gap_max_s"
        assert present == 1
        assert rig.exchange_positions() == {("BTCUSDT", "LONG"): qty}  # 挂得上：不平仓
        assert rig.system_stop()["client_algo_id"] != old["client_algo_id"]
        assert rig.position().protection_state is ProtectionState.SYSTEM
        rig.assert_general()
    finally:
        await rig.stop()
