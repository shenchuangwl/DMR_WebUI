"""build_kernel：只构造、不启动；环境检查；唯一的写权限令牌；本机令牌；实例号；默认传输与默认恢复。"""
from __future__ import annotations

import dataclasses
import sys
import threading
import types
from pathlib import Path

import pytest

from dmr_kernel.core import scheduler
from dmr_kernel.core.crashpoints import CrashRegistry
from dmr_kernel.core.scheduler import Kernel, KernelError, StartRefused, build_kernel
from dmr_kernel.core.types import Model
from dmr_kernel.exchange.adapter import CapabilityError, WriteCapability
from dmr_kernel.exchange.transport import HttpTransport
from dmr_kernel.ledger.writer import WRITER_THREAD_NAME
from tests.support.kernel import FakeRecovery, make_kernel, make_settings, make_standin_world

# 规格 3.2 的 Kernel 字段（除 recovery_result、jobs 外，构造完就都有值）
COMPONENTS = (
    "settings", "clock", "bus", "ledger", "crash", "adapter", "stream", "notifier", "heartbeat", "config",
    "modelstate", "registry", "intents", "orders", "ingest", "riskgate", "limits", "rules", "server_clock", "candles",
    "coordinator", "reconciler", "protection", "legacy", "guard", "models", "operations", "config_ops", "app", "jobs",
)


def writer_threads() -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == WRITER_THREAD_NAME and thread.is_alive())


async def test_build_constructs_every_component_and_starts_nothing(tmp_path):
    kernel, standin = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        assert isinstance(kernel, Kernel)
        for name in COMPONENTS:
            assert getattr(kernel, name) is not None, name
        assert set(kernel.models) == {Model.M1, Model.M2, Model.M3}
        assert kernel.recovery_result is None
        assert kernel.scheduler is None
        assert kernel.ledger.nonce and not kernel.ledger.closed
        assert kernel.stream.state == "stopped"  # 数据流对象在，但没有启动
        assert standin.transport.requests == []  # 一个请求都没有发给交易所
        assert not kernel.settings.kernel_lock_path.exists()  # 锁是 run_kernel 的事
    finally:
        await kernel.stop()


def test_the_fields_of_the_kernel_follow_the_agreed_interface():
    names = {field.name for field in dataclasses.fields(Kernel)}
    assert set(COMPONENTS) | {"recovery_result"} <= names


@pytest.mark.parametrize("env", ["testnet", "mainnet_shadow", "mainnet_live"])
async def test_every_other_environment_is_refused_before_anything_is_created(tmp_path, env):
    settings = make_settings(tmp_path, DMR_ENV=env, DMR_ALLOW_LIVE="1")
    threads = writer_threads()
    with pytest.raises(StartRefused, match="Demo Trading") as info:
        build_kernel(settings, transport=make_standin_world().new_transport())
    assert env in str(info.value)
    assert not (tmp_path / "var").exists()  # 账本、本机令牌都没有建
    assert writer_threads() == threads


async def test_the_instance_in_order_ids_is_the_setting_plus_the_ledger_nonce(tmp_path):
    kernel, _ = make_kernel(tmp_path, env={"DMR_INSTANCE_ID": "B"}, recovery_factory=FakeRecovery())
    try:
        expected = "B" + kernel.ledger.nonce
        assert kernel.orders._instance == expected
        assert kernel.ingest._instance == expected
        assert kernel.reconciler._instance == expected  # 对账只认本实例的系统编号
    finally:
        await kernel.stop()


# ---------------------------------------------------------------- 唯一的写权限令牌


def holds(obj: object, target: object) -> bool:
    return any(value is target for value in getattr(obj, "__dict__", {}).values())


async def test_exactly_one_write_capability_is_created_and_only_the_coordinator_uses_it(tmp_path, monkeypatch):
    created: list[WriteCapability] = []

    class Counting(WriteCapability):
        __slots__ = ()

        def __init__(self, owner: str):
            super().__init__(owner)
            created.append(self)

    monkeypatch.setattr(scheduler, "WriteCapability", Counting)
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        assert len(created) == 1
        capability = created[0]
        assert capability.owner == "coordinator"
        holders = sorted(name for name in COMPONENTS if holds(getattr(kernel, name), capability))
        # 协调者出示它；适配层在写方法出口拿它比对。别的组件（保护单管理、对账、守护、行情、操作、控制接口）都没有
        assert holders == ["adapter", "coordinator"]
        assert not holds(kernel, capability)
        assert kernel.coordinator._cap is capability
    finally:
        await kernel.stop()


async def test_the_adapter_refuses_writes_without_the_coordinators_capability(tmp_path):
    kernel, standin = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        for presented in (None, WriteCapability("coordinator"), WriteCapability("emergency")):
            with pytest.raises(CapabilityError):
                await kernel.adapter.cancel_order(presented, symbol="BTCUSDT", client_order_id="DKA1b-1-1-o")
        assert standin.transport.sent("DELETE") == []
    finally:
        await kernel.stop()


# ---------------------------------------------------------------- 本机令牌、心跳、配置操作


async def test_the_local_token_is_written_with_mode_0600_and_given_to_the_api(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        path = kernel.settings.local_token_path
        assert path == tmp_path / "var" / "kernel" / "local.token"
        assert path.stat().st_mode & 0o777 == 0o600
        token = path.read_text(encoding="utf-8").strip()
        assert token and kernel.app.state.ctx.local_token == token
    finally:
        await kernel.stop()


async def test_a_new_kernel_replaces_the_local_token(tmp_path):
    first, standin = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    old = first.settings.local_token_path.read_text(encoding="utf-8").strip()
    await first.stop()
    second, _ = make_kernel(tmp_path, standin=standin, recovery_factory=FakeRecovery())
    try:
        assert second.settings.local_token_path.read_text(encoding="utf-8").strip() != old
    finally:
        await second.stop()


async def test_the_heartbeat_file_is_under_var_kernel_and_the_api_reads_the_same_one(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        assert kernel.heartbeat.path == tmp_path / "var" / "kernel" / "heartbeat.json"
        assert kernel.app.state.ctx.heartbeat_file == kernel.heartbeat.path
    finally:
        await kernel.stop()


async def test_the_api_uses_the_kernels_own_config_operations(tmp_path):
    """控制接口的 PUT /config 与内核共用一个 ConfigOps（它那把串行锁只有全应用共用一个才起作用）。"""
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        assert kernel.app.state.config_ops is kernel.config_ops
        assert kernel.app.state.ctx.config is kernel.config
        assert kernel.app.state.ctx.models is kernel.models
    finally:
        await kernel.stop()


# ---------------------------------------------------------------- 崩溃点


async def test_the_crashpoint_in_the_settings_is_armed_and_the_default_registry_kills_the_process(tmp_path):
    kernel, _ = make_kernel(tmp_path, env={"DMR_CRASHPOINT": "open_sent"}, recovery_factory=FakeRecovery())
    try:
        assert kernel.crash.armed == {"open_sent"}
        assert kernel.crash._process_exit is True  # 进程级：走到崩溃点 os._exit(137)
        assert kernel.crash.live is False
    finally:
        await kernel.stop()


async def test_a_crash_registry_passed_in_is_used_as_it_is(tmp_path):
    mine = CrashRegistry(live=False)
    mine.arm("open_filled_before_stop")
    kernel, _ = make_kernel(tmp_path, crash=mine, recovery_factory=FakeRecovery())
    try:
        assert kernel.crash is mine
        assert kernel.coordinator._crash is mine and kernel.protection._crash is mine
        assert mine.armed == {"open_filled_before_stop"}
    finally:
        await kernel.stop()


async def test_without_a_crashpoint_nothing_is_armed(tmp_path):
    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        assert kernel.crash.armed == frozenset()
    finally:
        await kernel.stop()


# ---------------------------------------------------------------- 默认传输与默认恢复


async def test_the_default_transport_reads_the_standin_token_file(tmp_path):
    settings = make_settings(tmp_path)
    token_file: Path = settings.standin_token_path
    token_file.parent.mkdir(parents=True)
    token_file.write_text("standin-kernel-token\n", encoding="utf-8")
    kernel = build_kernel(settings, recovery_factory=FakeRecovery())
    try:
        transport = kernel.adapter._transport
        assert isinstance(transport, HttpTransport)
        assert transport._base_url == settings.standin_url
        assert transport._token == "standin-kernel-token"  # 去掉了行尾换行
    finally:
        await kernel.stop()


async def test_a_missing_standin_token_file_stops_the_build_with_a_clear_reason_and_leaves_no_ledger_thread(tmp_path):
    settings = make_settings(tmp_path)
    threads = writer_threads()
    with pytest.raises(KernelError) as info:
        build_kernel(settings, recovery_factory=FakeRecovery())
    assert str(settings.standin_token_path) in str(info.value)
    assert writer_threads() == threads  # 账本已经打开过，失败后要关掉，不留写线程


async def test_the_default_recovery_is_imported_from_core_recovery_only_when_the_kernel_starts(tmp_path, monkeypatch):
    """build 不导入 core.recovery（它是另一个任务在并行开发的）；start 时才取 Recovery。"""
    monkeypatch.delitem(sys.modules, "dmr_kernel.core.recovery", raising=False)
    kernel, _ = make_kernel(tmp_path)  # recovery_factory=None
    try:
        assert "dmr_kernel.core.recovery" not in sys.modules
        built: list[dict] = []
        stub = types.SimpleNamespace(tradable=True, problems=(), duration_ms=0, steps={})

        class Recovery:
            def __init__(self, **kwargs):
                built.append(kwargs)

            async def run(self):
                return stub

        monkeypatch.setitem(sys.modules, "dmr_kernel.core.recovery", types.SimpleNamespace(Recovery=Recovery))
        await kernel.start(serve_http=False, run_jobs=False)
        assert len(built) == 1 and kernel.recovery_result is stub
    finally:
        await kernel.stop()


# ---------------------------------------------------------------- 控制接口的 uvicorn 配置（真正起服务在 e2e 里，禁网包装下不能绑端口）


async def test_the_uvicorn_config_follows_the_agreed_settings(tmp_path):
    kernel, _ = make_kernel(tmp_path, env={"DMR_KERNEL_PORT": "18999"}, recovery_factory=FakeRecovery())
    try:
        config = kernel._http_config()
        assert (config.host, config.port) == ("127.0.0.1", 18999)
        assert config.app is kernel.app
        assert config.loop == "none"  # 标准库 asyncio，不让 uvicorn 自动选 uvloop
        assert config.lifespan == "on" and config.workers == 1 and config.reload is False
        assert config.log_config is None  # 日志归内核自己的处理器
        assert config.access_log is False  # 事件流握手的令牌在查询串里，访问日志会把它写出来
    finally:
        await kernel.stop()


async def test_the_http_server_does_not_take_over_the_termination_signals(tmp_path):
    """uvicorn 缺省会接管 SIGTERM/SIGINT，先于 4.4 的关停顺序把控制接口关掉；内核的服务器必须把它们留给 Lifecycle。"""
    import signal

    kernel, _ = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        server = scheduler._KernelServer(kernel._http_config())
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        with server.capture_signals():
            assert {sig: signal.getsignal(sig) for sig in before} == before
        assert server.should_exit is False
    finally:
        await kernel.stop()


async def test_the_candle_service_is_shared_with_the_api_for_the_candles_route(tmp_path):
    kernel, standin = make_kernel(tmp_path, recovery_factory=FakeRecovery())
    try:
        assert kernel.app.state.candles is kernel.candles
    finally:
        await kernel.stop()
