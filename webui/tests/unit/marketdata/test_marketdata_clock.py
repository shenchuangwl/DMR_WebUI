"""marketdata.clock.ServerClock：本机时钟与交易所服务器时间的偏移（T0.7）。

偏移 = 服务器读数 − 往返的中点（(发出 + 收到) / 2），半毫秒向上取整；之后「服务器现在」= 本机现在 + 偏移。
"""
from __future__ import annotations

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.marketdata.clock import MAX_PLAUSIBLE_SKEW_MS, ServerClock

LOCAL = 1_760_000_000_000


def calibrated(local_ms: int = LOCAL, *, server_ms: int | None = None) -> tuple[ServerClock, ManualClock]:
    clock = ManualClock(local_ms)
    server_clock = ServerClock(clock)
    server_clock.update(server_ms if server_ms is not None else local_ms, local_ms, local_ms)
    return server_clock, clock


def test_a_new_server_clock_is_uncalibrated_and_falls_back_to_the_local_clock():
    clock = ManualClock(5_000)
    server_clock = ServerClock(clock)
    assert server_clock.skew_ms is None and server_clock.updated_ms is None
    assert server_clock.now_server_ms() == 5_000
    clock.advance(700)
    assert server_clock.now_server_ms() == 5_700


def test_update_sets_the_offset_to_server_time_minus_the_round_trip_midpoint():
    clock = ManualClock(1_200)
    server_clock = ServerClock(clock)
    server_clock.update(10_000, 1_000, 1_200)  # 中点 1100，服务器读数 10000
    assert server_clock.skew_ms == 8_900
    assert server_clock.updated_ms == 1_200  # 收到应答的本机时刻


def test_server_time_is_the_local_time_plus_the_offset_and_keeps_moving_with_the_local_clock():
    clock = ManualClock(1_200)
    server_clock = ServerClock(clock)
    server_clock.update(10_000, 1_000, 1_200)
    assert server_clock.now_server_ms() == 10_100
    clock.advance(300)
    assert server_clock.now_server_ms() == 10_400
    assert server_clock.skew_ms == 8_900  # 偏移是更新时定下来的，不随时间走


@pytest.mark.parametrize(("server_ms", "sent_ms", "recv_ms", "expected"), [
    (2_000, 1_000, 1_200, 900),  # 整毫秒
    (10, 0, 1, 10),  # 9.5 向上取整
    (11, 0, 1, 11),  # 10.5 向上取整
    (1_000, 1_001, 1_002, -1),  # -1.5 向上取整（朝 +∞ 的方向）
    (5, 0, 0, 5),  # 往返为 0
])
def test_the_offset_is_rounded_half_up(server_ms, sent_ms, recv_ms, expected):
    server_clock = ServerClock(ManualClock(recv_ms))
    server_clock.update(server_ms, sent_ms, recv_ms)
    assert server_clock.skew_ms == expected


def test_a_local_clock_that_is_ahead_gives_a_negative_offset():
    clock = ManualClock(LOCAL + 10_000)  # 本机快 10 秒
    server_clock = ServerClock(clock)
    server_clock.update(LOCAL, LOCAL + 10_000, LOCAL + 10_000)
    assert server_clock.skew_ms == -10_000
    assert server_clock.now_server_ms() == LOCAL


def test_a_later_update_replaces_the_earlier_one():
    server_clock, clock = calibrated(server_ms=LOCAL + 4_000)
    assert server_clock.skew_ms == 4_000
    clock.advance(60_000)
    server_clock.update(LOCAL + 60_000 + 150, LOCAL + 60_000, LOCAL + 60_000)
    assert server_clock.skew_ms == 150  # 不是平均，是最新一次
    assert server_clock.updated_ms == LOCAL + 60_000


def test_skew_and_updated_time_cannot_be_set_from_outside():
    server_clock, _ = calibrated()
    with pytest.raises(AttributeError):
        server_clock.skew_ms = 5  # type: ignore[misc]
    with pytest.raises(AttributeError):
        server_clock.updated_ms = 5  # type: ignore[misc]


def test_a_round_trip_that_goes_backwards_is_rejected_and_changes_nothing():
    server_clock, _ = calibrated(server_ms=LOCAL + 40)
    with pytest.raises(ValueError, match="倒退"):
        server_clock.update(LOCAL + 999, LOCAL + 500, LOCAL + 400)
    assert server_clock.skew_ms == 40 and server_clock.updated_ms == LOCAL


@pytest.mark.parametrize("bad", [1.5, "1760000000000", None, True])
def test_update_rejects_arguments_that_are_not_integers(bad):
    server_clock, _ = calibrated()
    for args in ((bad, LOCAL, LOCAL), (LOCAL, bad, LOCAL), (LOCAL, LOCAL, bad)):
        with pytest.raises(TypeError):
            server_clock.update(*args)
    assert server_clock.skew_ms == 0


@pytest.mark.parametrize("server_ms", [0, -5])
def test_a_non_positive_server_time_is_rejected(server_ms):
    server_clock = ServerClock(ManualClock(1_000))
    with pytest.raises(ValueError, match="服务器时间"):
        server_clock.update(server_ms, 1_000, 1_000)
    assert server_clock.skew_ms is None


def test_an_absurd_offset_is_rejected_at_the_limit_and_accepted_just_inside_it():
    clock = ManualClock(LOCAL)
    server_clock = ServerClock(clock)
    server_clock.update(LOCAL + MAX_PLAUSIBLE_SKEW_MS, LOCAL, LOCAL)  # 正好在上限：接受（例如时区设错）
    assert server_clock.skew_ms == MAX_PLAUSIBLE_SKEW_MS
    server_clock.update(LOCAL - MAX_PLAUSIBLE_SKEW_MS, LOCAL, LOCAL)
    assert server_clock.skew_ms == -MAX_PLAUSIBLE_SKEW_MS
    for server_ms in (LOCAL + MAX_PLAUSIBLE_SKEW_MS + 1, LOCAL - MAX_PLAUSIBLE_SKEW_MS - 1):
        with pytest.raises(ValueError, match="偏移"):
            server_clock.update(server_ms, LOCAL, LOCAL)
    assert server_clock.skew_ms == -MAX_PLAUSIBLE_SKEW_MS  # 拒绝的样本不改任何东西


def test_rounding_decides_the_limit_on_the_rounded_offset():
    # 偏移 = 上限 + 0.5：取整后是上限 + 1，要拒绝；偏移 = 上限 - 0.5：取整后正好是上限，要接受
    server_clock = ServerClock(ManualClock(LOCAL))
    with pytest.raises(ValueError):
        server_clock.update(LOCAL + MAX_PLAUSIBLE_SKEW_MS + 1, LOCAL, LOCAL + 1)
    server_clock.update(LOCAL + MAX_PLAUSIBLE_SKEW_MS, LOCAL, LOCAL + 1)
    assert server_clock.skew_ms == MAX_PLAUSIBLE_SKEW_MS
