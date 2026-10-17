"""对拍测试的公共设施。

原实现对不带时区的时间按本机时区换算（DMR_WebUI/common/timeutil.py 第 33、51 行），本机是 +07：
会话期间把进程时区固定为 UTC（``TZ=UTC`` + ``time.tzset()``），结束时恢复；传给原实现的时间也一律带 UTC 时区（报告 5.3）。
"""
from __future__ import annotations

import os
import time
from typing import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def utc_process_timezone() -> Iterator[None]:
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()
