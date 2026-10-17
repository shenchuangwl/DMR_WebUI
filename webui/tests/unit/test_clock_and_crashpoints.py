"""core.clock 与 core.crashpoints。"""
from __future__ import annotations

import time

import pytest

from dmr_kernel.core import clock as c
from dmr_kernel.core import crashpoints as cp


def test_system_clock_is_utc_epoch_ms():
    now = c.SystemClock().now_ms()
    assert abs(now - int(time.time() * 1000)) < 2000


def test_manual_clock_advances():
    clk = c.ManualClock(1_000)
    clk.advance(500)
    assert clk.now_ms() == 1_500
    clk.set(10_000)
    assert clk.now_ms() == 10_000


@pytest.mark.parametrize("tf,ms", [("1m", 60_000), ("15m", 900_000), ("1h", 3_600_000), ("4h", 14_400_000)])
def test_interval_ms(tf, ms):
    assert c.interval_ms(tf) == ms


def test_floor_to_interval_is_utc_aligned():
    # 2026-10-14T03:51:33.522Z -> 15m 边界 03:45:00Z
    ts = 1791949893522
    assert c.floor_to_interval(ts, "15m") == 1791949500000
    assert c.ms_to_iso(1791949500000) == "2026-10-14T03:45:00Z"


def test_crashpoint_noop_when_not_armed():
    reg = cp.CrashRegistry(live=False)
    reg.hit("open_sent")  # 不抛异常


def test_armed_crashpoint_raises_in_process_signal():
    reg = cp.CrashRegistry(live=False)
    reg.arm("open_sent")
    with pytest.raises(cp.CrashSignal):
        reg.hit("open_sent")
    # 只触发一次
    reg.hit("open_sent")


def test_crashpoints_cannot_be_armed_in_live():
    reg = cp.CrashRegistry(live=True)
    with pytest.raises(RuntimeError):
        reg.arm("open_sent")


def test_known_crashpoint_names():
    for name in ("open_sent", "open_filled_before_stop", "stop_cancelled_before_new",
                 "close_filled_before_cleanup", "partial_close_sent", "close_all_midway",
                 "takeover_half_registered", "switch_midway"):
        assert name in cp.KNOWN_CRASHPOINTS
    with pytest.raises(ValueError):
        cp.CrashRegistry(live=False).arm("no_such_point")
