"""webui 测试的公共设施。

- 协程测试：``async def test_...`` 直接写，每个测试在独立的事件循环里用 ``asyncio.run`` 执行
  （不引入 pytest-asyncio，星数不达门槛）。超时默认 60 秒，可用 ``@pytest.mark.timeout_s(秒)`` 调整。
- 禁网：除 ``tests/e2e_local`` 外，测试必须经 ``./run_tests.sh`` 在禁网包装下运行。
  包装器会设置 ``DMR_WEBUI_NO_NETWORK=1``；没有这个变量时整个会话直接报错退出。
"""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path

import pytest

WEBUI_ROOT = Path(__file__).resolve().parents[1]
DMR_ROOT = WEBUI_ROOT.parent  # DMR_WebUI；只允许只读引用源码与 CSV（对拍）


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "timeout_s(seconds): 协程测试的超时秒数")
    e2e = os.environ.get("DMR_WEBUI_E2E") == "1"
    guarded = os.environ.get("DMR_WEBUI_NO_NETWORK") == "1"
    if not e2e and not guarded:
        raise pytest.UsageError(
            "webui 测试必须经 ./run_tests.sh 运行（禁网包装）；不要直接调用 pytest。"
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    e2e = os.environ.get("DMR_WEBUI_E2E") == "1"
    for item in items:
        in_e2e_dir = "e2e_local" in Path(str(item.fspath)).parts
        if in_e2e_dir and not e2e:
            item.add_marker(pytest.mark.skip(reason="回环端到端测试只能经 ./run_tests.sh e2e 运行"))
        if e2e and not in_e2e_dir:
            item.add_marker(pytest.mark.skip(reason="e2e 模式只运行 tests/e2e_local"))


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function):
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    marker = pyfuncitem.get_closest_marker("timeout_s")
    timeout = float(marker.args[0]) if marker else 60.0
    argnames = pyfuncitem._fixtureinfo.argnames
    kwargs = {name: pyfuncitem.funcargs[name] for name in argnames}

    async def runner():
        return await asyncio.wait_for(func(**kwargs), timeout)

    asyncio.run(runner())
    return True


@pytest.fixture
def webui_root() -> Path:
    return WEBUI_ROOT


@pytest.fixture
def dmr_root() -> Path:
    return DMR_ROOT
