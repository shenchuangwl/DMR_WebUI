"""任务装配测试的公共夹具：手动时钟（与进程内替身共用）、本地时区临时改成 +07。"""
from __future__ import annotations

import os
import time

import pytest

from dmr_kernel.core.clock import ManualClock
from tests.support.kernel import T0_MS


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0_MS)


@pytest.fixture
def bangkok():
    """把进程的本地时区临时改成 +07（Asia/Bangkok，与本机一致），测试完恢复。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Bangkok"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
