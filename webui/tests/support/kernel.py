"""内核装配帮手（集成测试与故障注入共用）：进程内替身交易所 + 完整装配的 ``Kernel``。

不开套接字（禁网包装下可用）；替身与内核共用一个 ``ManualClock``，推进它就同时推进双方的时间。

用法（协程测试）::

    async def test_x(tmp_path):
        async with started_kernel(tmp_path) as (kernel, standin):
            # serve_http=False、run_jobs=False：不起 uvicorn、不注册定时作业；手动调 kernel.jobs["reconcile_positions"]() 等
            await kernel.jobs["reconcile_positions"]()
        # 退出时自动 kernel.stop()

需要自己控制启动与关停时：``kernel, standin = make_kernel(tmp_path, ...)``（只构造）→ ``await kernel.start(...)`` → ``await kernel.stop()``。

模拟「进程被杀」后重启（丢弃整个内核实例、不走关停顺序，用同一账本与同一替身重新装配）::

    kernel, standin = make_kernel(tmp_path, crash=crash, recovery_factory=recovery)
    await kernel.start(serve_http=False, run_jobs=False)
    ...                                              # 崩溃点触发 CrashSignal
    await abandon(kernel)                            # 停掉后台任务、关账本连接；不撤任何保护单、不调 kernel.stop()
    kernel2, _ = make_kernel(tmp_path, standin=standin, recovery_factory=...)    # 同一个 tmp_path = 同一账本

要点：
- ``standin`` 缺省新建；传入则复用（同一个交易所状态、同一个时钟）。每个内核拿到一条**新的**传输（新进程 = 新连接）。
- ``recovery_factory`` 缺省 ``None`` = 真实的 ``core.recovery.Recovery``（延迟导入）；``started_kernel`` 缺省用 ``FakeRecovery()``。
- ``config_overrides`` 在启动前直接写进账本（Decimal 写成字符串）；跨字段不合法时 ``kernel.start()`` 会在加载配置时报错。
- 内核的要点属性：``kernel.jobs`` 具名作业、``kernel.scheduler``（APScheduler，``run_jobs=True`` 才有）、``kernel.app``（FastAPI，
  用 ``api_client(kernel)`` 在同一个事件循环里访问，不要用 starlette TestClient：它的事件循环在另一个线程里）。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.crashpoints import CrashRegistry
from dmr_kernel.core.scheduler import Kernel, build_kernel
from dmr_kernel.ledger import repo_config
from dmr_kernel.settings import Settings, load_settings
from dmr_standin.dispatcher import Dispatcher
from dmr_standin.engine import Engine
from tests.support.inproc import T0_MS, InProcessTransport, RecordingTransport, make_standin

__all__ = [
    "T0_MS", "FakeRecovery", "RecoveryStub", "Standin", "abandon", "api_client", "make_kernel", "make_settings",
    "make_standin_world", "settle", "started_kernel",
]

DEFAULT_PRICES = {"BTCUSDT": "50000", "ETHUSDT": "3000", "DOGEUSDT": "0.2"}


# ---------------------------------------------------------------- 进程内替身交易所


@dataclass
class Standin:
    """一个进程内的替身交易所（跨内核重启保持不变）。``transports`` 是每个内核各自的传输，``transport`` 是最近一条。"""

    clock: ManualClock
    disp: Dispatcher
    engine: Engine
    feed: Any
    kernel_token: str
    manual_token: str
    transports: list[RecordingTransport] = field(default_factory=list)

    def new_transport(self) -> RecordingTransport:
        transport = RecordingTransport(InProcessTransport(self.disp, token=self.kernel_token))
        self.transports.append(transport)
        return transport

    @property
    def transport(self) -> RecordingTransport:
        return self.transports[-1]


def make_standin_world(*, clock: ManualClock | None = None, equity: str | int | Decimal = "1000",
                       prices: Mapping[str, Any] | None = None, **standin_kwargs: Any) -> Standin:
    """造替身交易所。价格缺省 BTCUSDT 50000、ETHUSDT 3000、DOGEUSDT 0.2；时钟缺省 ``ManualClock(T0_MS)``。"""
    clock = ManualClock(T0_MS) if clock is None else clock
    price_map = dict(DEFAULT_PRICES)
    price_map.update(prices or {})
    disp, engine, kernel_token, manual_token = make_standin(equity=equity, clock=clock, prices=price_map,
                                                            **standin_kwargs)
    return Standin(clock=clock, disp=disp, engine=engine, feed=disp.feed, kernel_token=kernel_token,
                   manual_token=manual_token)


# ---------------------------------------------------------------- 设置与装配


def make_settings(tmp_path: Path, **env: str) -> Settings:
    """替身环境的设置：运行期目录、备份目录、DMR 根目录都在 tmp_path 里（旧入口互斥在替身下不启用，也就不会碰任何路径）。"""
    values = {"DMR_VAR_DIR": str(tmp_path / "var"), "DMR_BACKUP_DIR": str(tmp_path / "backup"), "DMR_ENV": "standin"}
    values.update(env)
    return load_settings(values, webui_root=tmp_path / "webui")


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return list(value) if isinstance(value, (list, tuple)) else value


def make_kernel(tmp_path: Path, *, clock: ManualClock | None = None, standin: Standin | None = None,
                crash: CrashRegistry | None = None, recovery_factory: Callable[..., Any] | None = None,
                config_overrides: Mapping[str, Any] | None = None,
                env: Mapping[str, str] | None = None) -> tuple[Kernel, Standin]:
    """只构造、不启动。``env`` 是额外的环境变量（例如 ``{"DMR_CRASHPOINT": "open_sent"}``）。"""
    if standin is None:
        standin = make_standin_world(clock=clock)
    clock = standin.clock if clock is None else clock
    settings = make_settings(tmp_path, **dict(env or {}))
    kernel = build_kernel(settings, clock=clock, transport=standin.new_transport(), crash=crash,
                          recovery_factory=recovery_factory)
    if config_overrides:
        for key, value in config_overrides.items():
            kernel.ledger.write_sync(repo_config.set_item, key, _encode(value), operator="test",
                                     at_ms=clock.now_ms())
    return kernel, standin


@asynccontextmanager
async def started_kernel(tmp_path: Path, *, serve_http: bool = False, run_jobs: bool = False,
                         recovery_factory: Any = "fake", **make_kwargs: Any):
    """启动一个内核，退出时 ``stop()``。``recovery_factory`` 缺省 ``FakeRecovery()``（传 ``None`` 用真实的恢复）。"""
    if recovery_factory == "fake":
        recovery_factory = FakeRecovery()
    kernel, standin = make_kernel(tmp_path, recovery_factory=recovery_factory, **make_kwargs)
    try:
        await kernel.start(serve_http=serve_http, run_jobs=run_jobs)
        yield kernel, standin
    finally:
        await kernel.stop()


async def settle(kernel: Kernel, rounds: int = 4) -> None:
    """让在途的事件、派发任务、保护周期都跑完（总线排空、协调者与保护单管理空闲、装配自己的后台任务做完）。"""
    for _ in range(rounds):
        for _ in range(6):
            await asyncio.sleep(0)
        await kernel.coordinator.idle(10.0)
        await kernel.bus.drain(10.0)
        await kernel.protection.idle(10.0)
        await kernel.idle(10.0)


async def abandon(kernel: Kernel) -> None:
    """模拟进程被杀：停掉这个内核实例的全部后台活动，不走关停顺序。

    不撤交易所侧的任何单子、不调 ``kernel.stop()``；只是让旧实例不再处理事件（否则它的订阅任务还会在同一个事件循环里
    继续干活，干扰新装配的内核），最后关掉它的账本连接（账本里的内容与被杀时一样，下一个内核照常读到）。
    """
    if kernel.scheduler is not None:
        with suppress(Exception):
            kernel.scheduler.shutdown(wait=False)
    with suppress(BaseException):
        await kernel.coordinator.stop(0)
    if kernel.stream is not None:
        with suppress(BaseException):
            await kernel.stream.stop()
    with suppress(BaseException):
        await kernel.bus.stop()
    with suppress(BaseException):
        await kernel.ledger.close()


@asynccontextmanager
async def api_client(kernel: Kernel, token: str | None = None):
    """在当前事件循环里访问 ``kernel.app``（httpx 的 ASGI 传输；进入时跑应用的 lifespan）。``token`` 给了就带 Bearer 头。"""
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with kernel.app.router.lifespan_context(kernel.app):
        transport = httpx.ASGITransport(app=kernel.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://kernel.test", headers=headers) as client:
            yield client


# ---------------------------------------------------------------- 恢复的替身


@dataclass(frozen=True)
class RecoveryStub:
    """与 ``core.recovery.RecoveryResult`` 同形的替身结果。"""

    tradable: bool = True
    problems: tuple[str, ...] = ()
    duration_ms: int = 0
    steps: Mapping[str, Any] = field(default_factory=dict)


class FakeRecovery:
    """``recovery_factory`` 的替身：记下每次构造时收到的关键字参数（``constructed``）和 ``run()`` 的次数（``runs``），
    ``run()`` 返回 ``result``（缺省 tradable=True）；``error`` 给了就抛它；``on_run`` 是 ``async def (kwargs)``，
    在 ``run()`` 里被调用（测试借此在「恢复进行中」检查内核的状态）。"""

    def __init__(self, *, result: Any = None, error: BaseException | None = None,
                 on_run: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> None:
        self.result = RecoveryStub() if result is None else result
        self.error = error
        self.on_run = on_run
        self.constructed: list[dict[str, Any]] = []
        self.runs = 0

    def __call__(self, **kwargs: Any) -> "_FakeRun":
        self.constructed.append(kwargs)
        return _FakeRun(self, kwargs)


class _FakeRun:
    def __init__(self, owner: FakeRecovery, kwargs: dict[str, Any]) -> None:
        self._owner = owner
        self._kwargs = kwargs

    async def run(self) -> Any:
        owner = self._owner
        owner.runs += 1
        if owner.on_run is not None:
            await owner.on_run(self._kwargs)
        if owner.error is not None:
            raise owner.error
        return owner.result
