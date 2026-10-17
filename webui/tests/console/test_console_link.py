"""与内核的连通状态机：第一次传输失败就算失联（宁可禁用按钮），第一次成功就恢复。"""
from __future__ import annotations

import time

import pytest

from console_kit import ManualClock
from dmr_console.clock import SystemClock
from dmr_console.link import LinkMonitor, LinkStatus


def test_initial_status_is_not_lost_and_has_no_history():
    status = LinkMonitor(ManualClock()).status
    assert status == LinkStatus(lost=False, reason="", kind="", lost_since_ms=None, last_ok_ms=None, failures=0)


def test_first_failure_is_lost_immediately_with_reason_and_kind():
    clock = ManualClock()
    link = LinkMonitor(clock)
    status = link.record_failure("请求内核超时", "timeout")
    assert status.lost and status.reason == "请求内核超时" and status.kind == "timeout"
    assert status.lost_since_ms == clock.now_ms() and status.failures == 1 and status.last_ok_ms is None
    assert link.status == status


def test_further_failures_keep_the_original_lost_since_and_count_up():
    clock = ManualClock()
    link = LinkMonitor(clock)
    link.record_ok()
    first = clock.now_ms()
    clock.advance(5)
    link.record_failure("无法连接内核", "connect")
    lost_at = clock.now_ms()
    clock.advance(5)
    status = link.record_failure("请求内核超时", "timeout")
    assert status.lost_since_ms == lost_at and status.last_ok_ms == first and status.failures == 2
    assert status.reason == "请求内核超时" and status.kind == "timeout"


def test_first_success_recovers_and_resets_the_counters():
    clock = ManualClock()
    link = LinkMonitor(clock)
    link.record_failure("x", "connect")
    link.record_failure("x", "connect")
    clock.advance(3)
    status = link.record_ok()
    assert not status.lost and status.failures == 0 and status.lost_since_ms is None and status.reason == ""
    assert status.last_ok_ms == clock.now_ms()


def test_recovery_after_loss_is_reported_by_the_returned_transition():
    link = LinkMonitor(ManualClock())
    assert link.record_ok().lost is False
    lost = link.record_failure("x", "connect")
    assert lost.lost
    assert link.record_ok().lost is False


def test_status_snapshots_are_immutable():
    status = LinkMonitor(ManualClock()).status
    with pytest.raises(Exception):
        status.lost = True  # type: ignore[misc]


def test_system_clock_reports_epoch_milliseconds():
    before = int(time.time() * 1000)
    now = SystemClock().now_ms()
    assert before <= now <= int(time.time() * 1000) + 1
    assert isinstance(now, int)
