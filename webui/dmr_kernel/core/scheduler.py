"""任务装配（第 7 层）：``build_kernel`` 把全部组件接在一起，``Kernel.start`` 按顺序启动，``run_kernel`` 是生产入口。

**启动顺序**（``Kernel.start``，规格 M1-波次5 第 1.1 节与 3.2 节）::

    加载配置、模型状态、登记簿、风控闸 → 总线与各订阅者（登记簿、意图、入账、风控闸、协调者、保护单管理、守护）
    → 调度器与「恢复之前」的两个作业（心跳先写一次；数据流判活）→ 刷新合约清单 → 控制接口（uvicorn 作为本循环的一个任务）
    → 重启恢复（``core.recovery``，由 ``recovery_factory`` 造；缺省延迟导入 ``Recovery``）→ 其余定时作业与「上限快照刷新」反应。

**M2 操作服务**：构造时按 ``ApiContext`` 建一套操作服务（``api.routes_ops.build_ops_services``，放进 ``app.state.ops_services``），
一键全平 ``ops.close_all.CloseAll`` 与切换 ``ops.switch.Switch`` 共用它的 ``OpsDeps``，用真实的意图服务、总线、崩溃点，
这一对放进 ``app.state.bulk_ops``（``ops.switch.BulkOps``）。启动时（订阅者之后）``ops.close_all.install(close_all)``
（``/ops/stop`` 带 close_all 用）并 ``switch.start()``；作业 ``close_all_tick``（恢复之后注册，每 ``CLOSE_ALL_TICK_S`` 秒）
调 ``close_all.tick()``；关停第一步撤下（``switch.stop()``，并在仍是本内核的实例时 ``install(None)``）。

恢复期间控制接口可用；其余定时作业在恢复返回之后才注册（避免与恢复里的全量对账并发）。守护在恢复之前订阅：恢复里会立刻起账户数据流，
订阅晚了会漏掉首次建流失败的「中断」事件（守护的 ``startup`` 对「订阅在前」是安全的：还没收到过数据流状态时照常重置）。

**关停顺序**（报告 4.4，``core.lifecycle.Lifecycle`` 的五个阶段）::

    stop_accepting      控制接口不再受理改变状态的请求；调度器停止触发新作业
    drain_intents       保护单管理停；协调者停（等在途意图到终态，最多 shutdown_drain_s 秒）
    close_stream        等在途作业收尾后关调度器；数据流停；总线排空；守护停
    close_exchange      适配层 close
    checkpoint_close_db 控制接口停；各订阅者退订；告警器停；总线停；账本检查点后关库；最后释放旧入口的锁

交易所侧的保护单不撤：关停过程不向交易所发任何写请求。整个关停另有期限（``stop_deadline_s``，小于守护脚本给的 30 秒）：
到点就不再等，只做最后两件小事（账本关库、释放旧入口的锁），其余交给下次启动的恢复。

**写权限令牌**：``build_kernel`` 只创建一张 ``WriteCapability("coordinator")``，交给协调者（出示）和适配层（出口比对），别处都拿不到。

**分层说明**：``api`` 与本模块同为第 7 层，按分层表互相不能静态导入，所以控制接口的几样东西（ApiContext、create_app、
write_local_token、日志抹令牌）经 ``importlib`` 按名字取（见 ``_api``）。把本模块挪到 ``api`` 之上后可改成普通导入。
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_RUNNING
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from dmr_kernel.alerts.heartbeat import Heartbeat
from dmr_kernel.alerts.notifier import Notifier
from dmr_kernel.core.clock import Clock, SystemClock, floor_to_interval, interval_ms
from dmr_kernel.core.config import ConfigStore
from dmr_kernel.core.coordinator import Coordinator
from dmr_kernel.core.crashpoints import CrashRegistry
from dmr_kernel.core.events import (
    ConfigChanged,
    EventBus,
    IntentStateChanged,
    OrderStateChanged,
    PositionChanged,
    PositionClosed,
    PositionRegistered,
)
from dmr_kernel.core.guard import Guard, LegacyMutex, legacy_runtime_dir
from dmr_kernel.core.ingest import Ingest
from dmr_kernel.core.intents import IntentService
from dmr_kernel.core.lifecycle import KernelLockError, Lifecycle, acquire_kernel_lock
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.orders import OrderService
from dmr_kernel.core.protection import Protection
from dmr_kernel.core.reconcile import Reconciler
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.riskgate import RiskGate
from dmr_kernel.core.types import AlertLevel, Environment, Model
from dmr_kernel.exchange.adapter import AccountStream, ExchangeAdapter, WriteCapability
from dmr_kernel.exchange.rules import InstrumentRules
from dmr_kernel.exchange.standin import StandinAdapter
from dmr_kernel.exchange.transport import HttpTransport, Transport
from dmr_kernel.ledger.repo_registry import EXTERNAL_ADJUST
from dmr_kernel.ledger.writer import Ledger
from dmr_kernel.marketdata.candles import CandleService
from dmr_kernel.marketdata.clock import ServerClock
from dmr_kernel.models.base import TradingModel
from dmr_kernel.models.m1_strategy import StrategyModel, StrategyService
from dmr_kernel.models.m2_takeover import TakeoverModel, TakeoverService
from dmr_kernel.models.m3_reserved import ReservedModel
from dmr_kernel.ops import close_all as close_all_module
from dmr_kernel.ops.close_all import CloseAll
from dmr_kernel.ops.common import OpsWiring
from dmr_kernel.ops.config_ops import ConfigOps
from dmr_kernel.ops.operations import Operations
from dmr_kernel.ops.switch import BulkOps, Switch
from dmr_kernel.ops.takeover_ops import TakeoverOps
from dmr_kernel.settings import Settings

log = logging.getLogger(__name__)

__all__ = [
    "JOB_NAMES", "BarCloseTrigger", "Kernel", "KernelError", "StartRefused", "UTC", "bar_close_trigger",
    "build_kernel", "last_closed_bar_open_ms", "refusal_reason", "run_kernel",
]

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# 规格 3.2 的 17 个作业名，加上 M2 的 close_all_tick（注册进调度器时也用它做作业号）
JOB_NAMES = (
    "stream_liveness", "resolve_unknown", "expire_intents", "reconcile_positions", "reconcile_orders",
    "backfill_trades", "backfill_income", "guard_periodic", "bar_close_short", "bar_close_long", "server_time",
    "rules_refresh", "heartbeat", "seat_expiry", "protection_tick", "ledger_backup", "ledger_checkpoint",
    "close_all_tick",
)
BEFORE_RECOVERY = ("heartbeat", "stream_liveness")  # 恢复之前就开始的两个作业

# 固定的间隔（秒）；对账与心跳的间隔取配置（apply=restart：启动时读一次）
STREAM_LIVENESS_S = 5
SHORT_TASK_S = 5  # 未知订单回查、意图过期、席位到期
PROTECTION_TICK_S = 2  # 保护单管理要求至少每 2 秒一次（T0.18）
CLOSE_ALL_TICK_S = 2  # 一键全平里状态未知而暂停的持仓，到期（partial_close_pause_s）后恢复托管
MINUTE_S = 60
HOUR_S = 3600

RULES_RETRY_S = 30  # 启动时读不到合约清单：30 秒后再试，不干等一小时
RECENT_MS = 10 * 60_000  # 「近期意图」的窗口：终态不久的意图，成交回报可能还在路上
MAX_DRAIN_S = 15  # 关停时等在途意图的上限（配置最大 60 秒，但守护脚本只给 30 秒）
DRAIN_MARGIN_S = 5  # 总的排空预算比协调者自己的等待多留的余量（让它先自己超时、取消、收尾）
HOOK_TIMEOUT_S = 6.0  # 关停里除排空之外每一步的时限
STOP_DEADLINE_S = 25.0  # 整个关停的期限（守护给 30 秒）
JOB_QUIESCE_S = 3.0  # 关调度器前等在途作业收尾
BUS_DRAIN_S = 3.0
HTTP_START_TIMEOUT_S = 15.0
HTTP_STOP_TIMEOUT_S = 5.0
HTTP_GRACEFUL_S = 3  # uvicorn 关停时等连接收尾
LAST_RESORT_S = 3.0
WAL_ALERT_BYTES = 64 * 1024 * 1024  # 检查点做完之后 WAL 文件还有这么大：有读事务一直没结束，截不断它（报告 4.4「WAL 文件过大时告警」）

# 数量有变化的持仓变化（其余如移动止损、清理旧止损、移交止损不值得每次都对一遍账）
QUANTITY_CAUSES = frozenset({"fill", "qty_change", EXTERNAL_ADJUST})


class KernelError(RuntimeError):
    """装配或启动失败（中文原因）。"""


class StartRefused(KernelError):
    """按设计拒绝启动（本轮只允许替身环境）。"""


def refusal_reason(settings: Settings) -> str | None:
    """能启动返回 None，否则返回拒绝的原因。本轮只实现替身环境。"""
    if settings.environment is Environment.STANDIN:
        return None
    return (f"本轮只能在替身环境（DMR_ENV=standin）下运行内核，当前环境是 {settings.environment.value}："
            "Binance 私有接口要在接入 Demo Trading 时实现，在那之前拒绝启动")


# ---------------------------------------------------------------- K 线作业的触发时刻


class BarCloseTrigger(BaseTrigger):
    """每个周期收盘后 ``delay_s`` 秒触发：UTC 纪元对齐（15 分钟 = 每刻钟的整点 + 延迟），与本机时区无关。
    整数微秒运算，不走浮点；取到的触发时刻永远在 ``now`` 及之后、并且严格晚于上一次。"""

    def __init__(self, timeframe: str, delay_s: int) -> None:
        if isinstance(delay_s, bool) or not isinstance(delay_s, int) or not 0 <= delay_s <= HOUR_S:
            raise ValueError(f"K 线延迟必须是 0 到 {HOUR_S} 的整数秒：{delay_s!r}")
        self.timeframe = timeframe
        self.delay_s = delay_s
        self.timezone = UTC
        self._interval_us = interval_ms(timeframe) * 1000  # 周期不认识会抛 ValueError
        self._delay_us = delay_s * 1_000_000

    def get_next_fire_time(self, previous_fire_time: datetime | None, now: datetime) -> datetime:
        floor_us = (now - EPOCH) // timedelta(microseconds=1)
        if previous_fire_time is not None:
            floor_us = max(floor_us, (previous_fire_time - EPOCH) // timedelta(microseconds=1) + 1)
        index = -((self._delay_us - floor_us) // self._interval_us)  # 向上取整：第一个不早于 floor 的边界
        return EPOCH + timedelta(microseconds=index * self._interval_us + self._delay_us)

    def __str__(self) -> str:
        return f"bar_close[{self.timeframe}+{self.delay_s}s UTC]"

    def __repr__(self) -> str:
        return f"<BarCloseTrigger timeframe={self.timeframe!r} delay_s={self.delay_s}>"


def bar_close_trigger(timeframe: str, delay_s: int) -> BarCloseTrigger:
    return BarCloseTrigger(timeframe, delay_s)


def last_closed_bar_open_ms(now_ms: int, timeframe: str, delay_s: int) -> int:
    """现在（扣掉取数延迟）最近一根已收盘的 K 线的开盘时间：边界收盘后 ``delay_s`` 秒才算那一根「该取了」。"""
    step = interval_ms(timeframe)
    return floor_to_interval(now_ms - delay_s * 1000, timeframe) - step


# ---------------------------------------------------------------- 与 api 的接缝


def _api(name: str) -> Any:
    """取 ``dmr_kernel.api.<name>``。api 与本模块同为第 7 层，分层检查不许静态互相导入，所以按名字动态取。"""
    return importlib.import_module(f"dmr_kernel.api.{name}")


def _default_recovery_factory() -> Callable[..., Any]:
    """缺省的恢复工厂：``core.recovery.Recovery``。延迟到启动时才导入（它由另一个任务开发，装配的测试一律传假恢复）。"""
    return importlib.import_module("dmr_kernel.core.recovery").Recovery


class _KernelServer(uvicorn.Server):
    """不让 uvicorn 接管 SIGTERM/SIGINT：终止信号归 ``Lifecycle``，否则它会先把控制接口关掉，打乱 4.4 的关停顺序。"""

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


# ---------------------------------------------------------------- 内核


@dataclass(eq=False, repr=False)
class Kernel:
    """装配好的内核。``start``/``stop`` 一次性：停止之后要重新装配（协调者是一次性对象）。"""

    settings: Settings
    clock: Clock
    bus: EventBus
    ledger: Ledger
    crash: CrashRegistry
    adapter: ExchangeAdapter
    stream: AccountStream | None
    notifier: Notifier
    heartbeat: Heartbeat
    config: ConfigStore
    modelstate: ModelStateService
    registry: Registry
    intents: IntentService
    orders: OrderService
    ingest: Ingest
    riskgate: RiskGate
    limits: Limits
    rules: InstrumentRules
    server_clock: ServerClock
    candles: CandleService
    coordinator: Coordinator
    reconciler: Reconciler
    protection: Protection
    legacy: LegacyMutex
    guard: Guard
    models: dict[Model, TradingModel]
    operations: Operations
    config_ops: ConfigOps
    app: Any  # FastAPI（fastapi 只允许在 api 包里导入）
    recovery_result: Any = None  # core.recovery.RecoveryResult；恢复返回之前是 None
    jobs: dict[str, Callable[[], Awaitable[None]]] = field(default_factory=dict)
    scheduler: AsyncIOScheduler | None = None
    lifecycle: Lifecycle | None = None
    stop_deadline_s: float = STOP_DEADLINE_S
    recovery_factory: Callable[..., Any] | None = None
    close_all: CloseAll | None = None  # M2 一键全平（与 switch 一起放在 app.state.bulk_ops）
    switch: Switch | None = None  # M2 模型切换
    takeover: TakeoverService | None = None  # M3 模型 2：识别未登记持仓、归类、自动接管
    takeover_ops: TakeoverOps | None = None  # M3 接管类操作（放进 app.state.takeover_ops）
    listsync: Any = None  # M4 名单同步（listsync.ListSync 或同接口的替身；还没有就是 None）
    m1: StrategyService | None = None  # M4 模型 1：按名单自动开仓、信号平仓与反手、信号注入

    def __post_init__(self) -> None:
        self._phase = "built"  # built → starting → running → stopping → stopped
        self._accepting = True
        self._stop_task: asyncio.Task | None = None
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()
        self._recent: dict[str, int] = {}  # 合约 → 最近一次意图或订单事件的时间
        self._kick_requested = False
        self._kick_task: asyncio.Task | None = None
        self._reactions: list[Any] = []
        if not self.jobs:
            self.jobs = {name: getattr(self, f"_job_{name}") for name in JOB_NAMES}
        if self.lifecycle is None:
            self.lifecycle = Lifecycle(shutdown_drain_s=MAX_DRAIN_S + DRAIN_MARGIN_S, hook_timeout_s=HOOK_TIMEOUT_S)
        life = self.lifecycle
        life.on_shutdown("stop_accepting", self._hook_stop_accepting, name="stop_accepting")
        life.on_shutdown("stop_accepting", self._hook_ops_stop, name="ops.stop")
        life.on_shutdown("drain_intents", self._hook_protection_stop, name="protection.stop")
        life.on_shutdown("drain_intents", self._hook_coordinator_stop, name="coordinator.stop")
        life.on_shutdown("close_stream", self._hook_quiesce_jobs, name="scheduler.shutdown")
        life.on_shutdown("close_stream", self._hook_stream_stop, name="stream.stop")
        life.on_shutdown("close_stream", self._hook_bus_drain, name="bus.drain")
        life.on_shutdown("close_stream", self._hook_guard_stop, name="guard.stop")
        life.on_shutdown("close_exchange", self._hook_adapter_close, name="adapter.close")
        life.on_shutdown("close_exchange", self._hook_listsync_close, name="listsync.close")
        life.on_shutdown("checkpoint_close_db", self._hook_http_stop, name="http.stop")
        life.on_shutdown("checkpoint_close_db", self._hook_unsubscribe, name="subscribers.stop")
        life.on_shutdown("checkpoint_close_db", self._hook_notifier_stop, name="notifier.stop")
        life.on_shutdown("checkpoint_close_db", self._hook_bus_stop, name="bus.stop")
        life.on_shutdown("checkpoint_close_db", self._hook_ledger_close, name="ledger.close")
        life.on_shutdown("checkpoint_close_db", self._hook_legacy_release, name="legacy.release_all")

    def __repr__(self) -> str:
        return f"Kernel(phase={self._phase!r}, environment={self.settings.environment.value!r})"

    # ================================================================ 启动

    async def start(self, *, serve_http: bool = True, run_jobs: bool = True) -> None:
        """按启动顺序启动。``serve_http=False`` 不起 uvicorn（测试在同一循环里访问 ``kernel.app``）；
        ``run_jobs=False`` 不建调度器、不订阅「上限快照刷新」（测试手动调 ``kernel.jobs[...]``）。
        失败（含恢复失败）时异常原样抛出，已经起来的部分由调用方 ``stop()`` 收尾。"""
        if self._phase != "built":
            if self._phase in ("stopping", "stopped"):
                raise RuntimeError("内核已经停止，不能重新启动：请重新装配一个内核（build_kernel）")
            raise RuntimeError("内核已经启动过（Kernel 是一次性对象）")
        self._phase = "starting"
        # 1 配置与状态（任何 config_get 之前）
        await self.config.load()
        self._apply_config()
        await self.modelstate.load()
        await self.registry.load()
        await self.riskgate.load()
        # 2 总线与各订阅者：登记簿、意图、入账、协调者之后才是保护单管理
        await self.bus.start()
        await self.notifier.start()
        self._reactions.append(self.bus.subscribe((IntentStateChanged, OrderStateChanged), self._on_symbol_event,
                                                  name="scheduler.recent_symbols"))
        await self.registry.start()
        await self.intents.start()
        await self.ingest.start()
        await self.riskgate.start()
        await self.coordinator.start()
        await self.protection.start()
        if self.takeover is not None:
            await self.takeover.start()
        if self.m1 is not None:
            await self.m1.start()
        self._ensure_list_job()
        await self.guard.start()
        await self._start_ops()
        # 3 调度器与「恢复之前」的作业
        if run_jobs:
            await self._start_scheduler()
        # 4 合约清单：协调者算数量、保护单管理取价格步长都要用它
        await self._refresh_rules_at_start()
        # 5 控制接口：恢复期间也可用
        if serve_http:
            await self._start_http()
        # 6 重启恢复；守护的启动自检（恢复第 1 步）读到的持仓模式与交易所限制马上登记给控制接口，不用等第一次周期复核
        self.recovery_result = await self._recover()
        self._note_guard_status()
        # 7 恢复之后：其余定时作业与上限快照的及时刷新
        if run_jobs:
            self._register_jobs_after_recovery()
            self._reactions.append(self.bus.subscribe(
                (PositionRegistered, PositionChanged, PositionClosed), self._on_position_event,
                name="scheduler.limits_refresh"))
            self._reactions.append(self.bus.subscribe(ConfigChanged, self._on_config_changed,
                                                      name="scheduler.config"))
        self._phase = "running"
        log.info("内核已启动：环境 %s，控制接口 %s，定时作业 %s", self.settings.environment.value,
                 f"{self.settings.kernel_host}:{self.settings.kernel_port}" if serve_http else "未起",
                 "已注册" if run_jobs else "未注册")

    async def _start_ops(self) -> None:
        """M2 操作服务：``/ops/stop`` 带 close_all 用的一键全平实例，切换的席位订阅与到期预览清理。"""
        if self.close_all is not None:
            close_all_module.install(self.close_all)
        if self.switch is not None:
            await self.switch.start()

    def _apply_config(self) -> None:
        if not bool(self.config.get("ex_stream_enabled")):
            self.stream = None  # 配置里不用账户数据流：不启动、不判活（Guard 一直是仅轮询降级）
        self.lifecycle.shutdown_drain_s = self._drain_seconds() + DRAIN_MARGIN_S

    def _drain_seconds(self) -> float:
        try:
            return float(min(int(self.config.get("shutdown_drain_s")), MAX_DRAIN_S))
        except RuntimeError:  # 配置还没加载（没启动过就关停）
            return 10.0

    async def rerun_recovery(self) -> Any:
        """重新跑一次重启恢复（``POST /ops/recover`` 用）：结果记进 ``recovery_result`` 并重新登记守护读到的状态。"""
        self.recovery_result = await self._recover()
        self._note_guard_status()
        return self.recovery_result

    async def _recover(self) -> Any:
        factory = self.recovery_factory if self.recovery_factory is not None else _default_recovery_factory()
        recovery = factory(
            settings=self.settings, adapter=self.adapter, stream=self.stream, ledger=self.ledger, bus=self.bus,
            clock=self.clock, config_get=self.config.get, modelstate=self.modelstate, registry=self.registry,
            intents=self.intents, orders=self.orders, ingest=self.ingest, coordinator=self.coordinator,
            reconciler=self.reconciler, protection=self.protection, guard=self.guard, candles=self.candles,
            operations=self.operations, notifier=self.notifier)
        result = await recovery.run()
        if getattr(result, "tradable", True):
            log.info("重启恢复完成（用时 %s 毫秒）", getattr(result, "duration_ms", "?"))
        else:
            problems = "；".join(str(p) for p in getattr(result, "problems", ())) or "未说明"
            log.warning("重启恢复完成，但内核不进入可交易状态：%s", problems)
        return result

    # ------------------------------------------------------------ 合约清单

    async def _refresh_rules_at_start(self) -> None:
        if not await self.rules.refresh(self.adapter):
            log.warning("启动时没有读到合约清单（交易所读不到或清单为空）")
            self._retry_rules_soon()

    def _retry_rules_soon(self) -> None:
        """没有任何旧清单可用时不能干等一小时：安排 30 秒后再试（有调度器才安排；有旧清单就等下一个整点）。"""
        if self.scheduler is None or not self.scheduler.running or self.rules.all():
            return
        self.scheduler.add_job(
            self._tracked("rules_refresh"), DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=RULES_RETRY_S),
                                                         timezone=UTC),
            id="rules_refresh_retry", name="rules_refresh_retry", max_instances=1, coalesce=True,
            misfire_grace_time=None, replace_existing=True)

    # ------------------------------------------------------------ 控制接口

    def _http_config(self) -> uvicorn.Config:
        """控制接口的 uvicorn 配置：标准库 asyncio（loop="none"：不调 uvloop 的自动选择）、单工作进程、不自动重载、
        回环地址。日志交给内核自己的处理器（log_config=None），访问日志关掉：事件流握手的令牌在查询串里。"""
        settings = self.settings
        return uvicorn.Config(
            self.app, host=settings.kernel_host, port=settings.kernel_port, loop="none", lifespan="on", workers=1,
            reload=False, log_config=None, access_log=False, timeout_graceful_shutdown=HTTP_GRACEFUL_S)

    async def _start_http(self) -> None:
        _api("logsafe").install("uvicorn", "uvicorn.error", "uvicorn.access")  # 令牌抹掉（双保险）
        settings = self.settings
        config = self._http_config()
        server = _KernelServer(config)

        async def serve() -> None:
            try:
                await server.serve()
            except SystemExit as exc:  # uvicorn 起不来（端口被占用等）时 sys.exit(1)
                raise KernelError(f"控制接口没有起来（{settings.kernel_host}:{settings.kernel_port}，退出码 "
                                  f"{exc.code}；端口被占用？详见日志）") from None

        loop = asyncio.get_running_loop()
        self._server = server
        self._server_task = loop.create_task(serve(), name="kernel-http")
        deadline = loop.time() + HTTP_START_TIMEOUT_S
        while not server.started:
            if self._server_task.done():
                self._server_task.result()  # 起不来的原因（KernelError）在这里抛出
                raise KernelError("控制接口提前退出了")
            if loop.time() > deadline:
                raise KernelError(f"控制接口 {HTTP_START_TIMEOUT_S:.0f} 秒内没有起来")
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------ 调度器与作业

    async def _start_scheduler(self) -> None:
        # 调度器每次触发作业都记两行 INFO（开始、成功）；几个 5 秒一档的作业一天就是几十兆日志。出错（ERROR）照常记
        logging.getLogger("apscheduler").setLevel(logging.WARNING)
        self.scheduler = AsyncIOScheduler(timezone=UTC)
        self.scheduler.start()
        await self.jobs["heartbeat"]()  # 第一次心跳马上写：看护方不用等 30 秒
        self._add_interval("heartbeat", int(self.config.get("heartbeat_s")))
        if self.stream is not None:
            self._add_interval("stream_liveness", STREAM_LIVENESS_S)

    def _register_jobs_after_recovery(self) -> None:
        plan = (
            ("resolve_unknown", SHORT_TASK_S),
            ("expire_intents", SHORT_TASK_S),
            ("reconcile_positions", int(self.config.get("reconcile_positions_s"))),
            ("reconcile_orders", int(self.config.get("reconcile_orders_s"))),
            ("backfill_trades", MINUTE_S),
            ("backfill_income", HOUR_S),
            ("guard_periodic", MINUTE_S),
            ("server_time", MINUTE_S),
            ("rules_refresh", HOUR_S),
            ("seat_expiry", SHORT_TASK_S),
            ("protection_tick", PROTECTION_TICK_S),
            ("ledger_backup", HOUR_S),
            ("ledger_checkpoint", HOUR_S),
            ("close_all_tick", CLOSE_ALL_TICK_S),
        )
        for name, seconds in plan:
            self._add_interval(name, seconds)
        for name, trigger in self._bar_triggers().items():
            self._add_job(name, trigger)
        if self.listsync is not None:
            self._add_interval("list_poll", int(self.config.get("list_poll_s")))
            self.scheduler.add_job(  # 启动后马上拉一次，不用等一个间隔
                self._tracked("list_poll"), DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=1), timezone=UTC),
                id="list_poll_first", name="list_poll_first", max_instances=1, coalesce=True, misfire_grace_time=None,
                replace_existing=True)

    def _bar_triggers(self) -> dict[str, BarCloseTrigger]:
        delay = int(self.config.get("kline_delay_s"))
        return {"bar_close_short": bar_close_trigger(self.config.get("timeframe_short"), delay),
                "bar_close_long": bar_close_trigger(self.config.get("timeframe_long"), delay)}

    def _add_interval(self, name: str, seconds: int) -> None:
        self._add_job(name, IntervalTrigger(seconds=seconds, timezone=UTC))

    def _add_job(self, name: str, trigger: BaseTrigger) -> None:
        # 同一时刻只跑一个实例，错过的触发合并成一次；晚了也要跑（不设宽限），不能因为循环忙了一秒就丢
        self.scheduler.add_job(self._tracked(name), trigger, id=name, name=name, max_instances=1, coalesce=True,
                               misfire_grace_time=None, replace_existing=True)

    def _tracked(self, name: str) -> Callable[[], Awaitable[None]]:
        """调度器跑的函数：记下在途的作业任务，关停时好等它们收尾。作业按名字在触发时才从 ``jobs`` 里取
        （内核起来之后替换 ``jobs[名字]``，调度器也跟着用新的）。"""

        async def run() -> None:
            task = asyncio.current_task()
            self._inflight.add(task)
            try:
                await self.jobs[name]()
            finally:
                self._inflight.discard(task)

        return run

    # ------------------------------------------------------------ 反应：配置变化、持仓变化、近期合约

    def _on_config_changed(self, event: ConfigChanged) -> None:
        """kline_delay_s 是「立即生效」的配置项：下一根 K 线起就按新的延迟取数。"""
        if event.key != "kline_delay_s" or self.scheduler is None or not self.scheduler.running:
            return
        for name, trigger in self._bar_triggers().items():
            if self.scheduler.get_job(name) is not None:
                self.scheduler.reschedule_job(name, trigger=trigger)

    def _on_symbol_event(self, event: IntentStateChanged | OrderStateChanged) -> None:
        self._recent[event.symbol] = max(self._recent.get(event.symbol, 0), event.at_ms)

    def _on_position_event(self, event: PositionRegistered | PositionChanged | PositionClosed) -> None:
        """持仓登记、数量变化、归零之后尽快对一次持仓账，让合约数上限的快照跟上，否则同一根 K 线上完成得快的开仓之后，
        后面的开仓可能短暂越过上限。不在订阅回调里直接对账（对账会等总线排空，回调里等自己），另起任务；
        一阵事件合并成最多两次对账（在途的一次加一次补位）。"""
        if not self._accepting:
            return
        if isinstance(event, PositionChanged) and event.cause not in QUANTITY_CAUSES:
            return
        self._kick_requested = True
        if self._kick_task is None or self._kick_task.done():
            self._kick_task = asyncio.get_running_loop().create_task(self._kick_loop(), name="kernel.limits_refresh")

    async def _kick_loop(self) -> None:
        while self._kick_requested and self._accepting:
            self._kick_requested = False
            try:
                await self.reconciler.reconcile_positions()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("持仓变化后的即时对账失败（定时对账照常进行）")

    async def idle(self, timeout_s: float = 10.0) -> None:
        """等装配自己的后台任务（持仓变化后的即时对账）做完；到点没结束就返回（测试用）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            task = self._kick_task
            remaining = deadline - loop.time()
            if task is None or task.done() or remaining <= 0:
                return
            await asyncio.wait({task}, timeout=remaining)

    # ------------------------------------------------------------ 名单同步（M4）

    def set_listsync(self, listsync: Any) -> None:
        """接入名单同步（装配层在 ``listsync.ListSync`` 可用时自动接；测试可以换成满足同样接口的替身）。必须在 ``start`` 之前。"""
        self.listsync = listsync
        deps = getattr(getattr(self.app.state, "ops_services", None), "deps", None)
        if deps is not None:  # 启动自检里「名单有效」
            deps.list_valid = self.list_valid
        self._ensure_list_job()

    def list_valid(self) -> bool:
        if self.listsync is None:
            return False
        return bool(self.listsync.current().valid)

    def _ensure_list_job(self) -> None:
        if self.listsync is not None and "list_poll" not in self.jobs:
            self.jobs["list_poll"] = self._job_list_poll

    async def _job_list_poll(self) -> None:
        try:
            await self.listsync.poll()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("名单拉取出错（下一个间隔再试）")

    # ------------------------------------------------------------ 监控集合与补拉集合

    async def monitored_symbols(self) -> list[str]:
        """K 线要取的合约：全部登记仓（策略仓、混合仓、接管仓）所在的合约 + 有挂单的合约 + 当前有效名单的成员。"""
        found = {rec.symbol for rec in self.registry.registered()}
        found |= {order.symbol for order in await self.orders.non_terminal()}
        if self.m1 is not None:
            found |= self.m1.list_symbols()
        return sorted(found)

    async def tracked_symbols(self) -> list[str]:
        """成交补拉要查的合约：有持仓（登记簿里的和交易所快照里的）、有挂单、有在途意图、近期意图的合约。"""
        now = self.clock.now_ms()
        found = {rec.symbol for rec in self.registry.all_active()}
        found |= {symbol for symbol, _side in self.limits.occupancy(degraded=False, now_ms=now).keys}
        found |= {order.symbol for order in await self.orders.non_terminal()}
        found |= {intent.symbol for intent in await self.intents.non_terminal()}
        for symbol, at_ms in list(self._recent.items()):
            if now - at_ms >= RECENT_MS:
                del self._recent[symbol]
            else:
                found.add(symbol)
        return sorted(found)

    # ================================================================ 作业

    async def _job_stream_liveness(self) -> None:
        if self.stream is not None:
            await self.stream.check_liveness()
            live = getattr(getattr(self.app.state, "services", None), "live", None)
            if live is not None:  # 状态条的「最近一帧」：心跳帧不发事件，取数据流自己记的时间
                live.note_frame(getattr(self.stream, "last_frame_ms", None))

    async def _job_resolve_unknown(self) -> None:
        await self.coordinator.resolve_unknown_orders()

    async def _job_expire_intents(self) -> None:
        await self.intents.expire_due(self.clock.now_ms())

    async def _job_reconcile_positions(self) -> None:
        await self.reconciler.reconcile_positions()

    async def _job_reconcile_orders(self) -> None:
        await self.reconciler.reconcile_orders()

    async def _job_backfill_trades(self) -> None:
        await self.ingest.backfill_trades(await self.tracked_symbols())

    async def _job_backfill_income(self) -> None:
        await self.ingest.backfill_income()

    async def _job_guard_periodic(self) -> None:
        await self.guard.periodic()
        self._note_guard_status()

    def _note_guard_status(self) -> None:
        """周期复核读到的持仓模式与交易所限制没有总线事件，登记给控制接口的实时缓存（/state 据此显示）。读不到的不登记。"""
        live = self.app.state.services.live
        status = self.guard.status()
        mode, mode_ms = status.get("position_mode"), status.get("position_mode_checked_ms")
        if mode in ("hedge", "one_way") and mode_ms is not None:
            live.note_position_mode(mode == "hedge", mode_ms)
        restricted, checked_ms = status.get("restricted"), status.get("restrictions_checked_ms")
        if restricted is not None and checked_ms is not None:
            live.note_restrictions(bool(restricted), tuple(dict(status.get("restriction_detail") or {}).items()),
                                   checked_ms)

    async def _job_bar_close_short(self) -> None:
        await self._bar_close("timeframe_short")

    async def _job_bar_close_long(self) -> None:
        await self._bar_close("timeframe_long")

    async def _bar_close(self, timeframe_key: str) -> None:
        timeframe = self.config.get(timeframe_key)
        bar_open_ms = last_closed_bar_open_ms(self.clock.now_ms(), timeframe, int(self.config.get("kline_delay_s")))
        await self.candles.on_bar_close(timeframe, bar_open_ms, await self.monitored_symbols())

    async def _job_server_time(self) -> None:
        await self.candles.sync_server_time()

    async def _job_rules_refresh(self) -> None:
        if not await self.rules.refresh(self.adapter):
            log.warning("合约清单刷新失败，继续使用旧清单" if self.rules.all() else "合约清单刷新失败，目前没有任何清单可用")
            self._retry_rules_soon()

    async def _job_heartbeat(self) -> None:
        try:
            self.heartbeat.beat()
        except OSError as exc:
            log.error("心跳写入失败：%s", exc)
            self.notifier.raise_alert(AlertLevel.WARNING, "heartbeat_write_failed", f"心跳文件写不进去：{exc}",
                                      dedupe_key="heartbeat_write_failed")

    async def _job_seat_expiry(self) -> None:
        await self.app.state.services.seat.expire_due()

    async def _job_protection_tick(self) -> None:
        await self.protection.tick()
        if self.takeover is not None:  # 模型 2：重试没判成的键、待确认仓的重复提醒（与保护周期同一个节拍）
            await self.takeover.tick()
        if self.m1 is not None:  # 模型 1：等着开仓的反手
            await self.m1.tick()

    async def _job_close_all_tick(self) -> None:
        if self.close_all is not None:
            await self.close_all.tick()

    async def _job_ledger_backup(self) -> None:
        try:
            await self.ledger.backup(self.settings.backup_dir)
        except Exception as exc:
            # 备份悄悄失败，等到要用时才发现一份都没有：除了日志（调度器也会记）还要有告警；异常照常交给调用方
            log.error("账本在线备份失败：%s: %s", type(exc).__name__, exc)
            self.notifier.raise_alert(AlertLevel.WARNING, "ledger_backup_failed",
                                      f"账本在线备份失败（{self.settings.backup_dir}）：{type(exc).__name__}: {exc}",
                                      dedupe_key="ledger_backup_failed")
            raise

    async def _job_ledger_checkpoint(self) -> None:
        await self.ledger.checkpoint()
        size = self._wal_bytes()  # 看检查点之后的大小：截断检查点都截不断，才说明有读事务一直没结束
        if size > WAL_ALERT_BYTES:
            limit_mb = WAL_ALERT_BYTES // (1024 * 1024)
            log.warning("账本的 WAL 文件检查点之后仍有 %.0f MB（超过 %d MB）", size / 1048576, limit_mb)
            self.notifier.raise_alert(
                AlertLevel.WARNING, "ledger_wal_large",
                f"账本的 WAL 文件检查点之后仍有 {size / 1048576:.0f} MB（超过 {limit_mb} MB）：有读事务一直没有结束？",
                dedupe_key="ledger_wal_large")

    def _wal_bytes(self) -> int:
        """账本 WAL 文件现在的大小；文件不存在（连接都关了，SQLite 已删掉它）就是 0。"""
        try:
            return os.stat(f"{self.settings.db_path}-wal").st_size
        except OSError:
            return 0

    # ================================================================ 关停

    def stop_accepting(self) -> None:
        """关停第一步（可重复调用）：控制接口不再受理改变状态的请求，调度器不再触发新作业，持仓变化的即时对账关掉。"""
        self._accepting = False
        self.app.state.accepting = False
        if self.scheduler is not None and self.scheduler.state == STATE_RUNNING:
            self.scheduler.pause()

    async def stop(self) -> None:
        """按 4.4 的顺序关停（见模块说明）。可重复调用、可并发调用；没启动过、启动失败后也能调用，收拾已经起来的部分。"""
        if self._stop_task is None:
            self._stop_task = asyncio.get_running_loop().create_task(self._stop(), name="kernel-stop")
        await asyncio.shield(self._stop_task)

    async def _stop(self) -> None:
        self._phase = "stopping"
        try:
            await asyncio.wait_for(self.lifecycle.shutdown(), self.stop_deadline_s)
        except asyncio.TimeoutError:
            log.error("关停超过期限 %.0f 秒，不再等待：只做最后两步（关账本、释放旧入口的锁），其余交给下次启动的恢复",
                      self.stop_deadline_s)
            await self._last_resort()
        self._phase = "stopped"

    async def _last_resort(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ledger.close(), LAST_RESORT_S)
        with contextlib.suppress(Exception):
            self.legacy.release_all()

    async def _hook_stop_accepting(self) -> None:
        self.stop_accepting()

    async def _hook_ops_stop(self) -> None:
        if self.switch is not None:
            await self.switch.stop()
        if self.close_all is not None and close_all_module._installed is self.close_all:
            close_all_module.install(None)  # 别的（更新的）内核装的不动

    async def _hook_protection_stop(self) -> None:
        if self.takeover is not None:
            await self.takeover.stop()
        if self.m1 is not None:
            await self.m1.stop()
        await self.protection.stop()

    async def _hook_coordinator_stop(self) -> None:
        # 协调者是一次性对象；排空到点还没结束的任务它会自己取消，留给重启恢复
        await self.coordinator.stop(self._drain_seconds())

    async def _hook_quiesce_jobs(self) -> None:
        task = self._kick_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        pending = [t for t in self._inflight if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=JOB_QUIESCE_S)
        if self.scheduler is not None and self.scheduler.running:
            self.scheduler.shutdown(wait=False)  # 还没收尾的作业在这里被取消

    async def _hook_stream_stop(self) -> None:
        if self.stream is not None:
            await self.stream.stop()

    async def _hook_bus_drain(self) -> None:
        try:
            await self.bus.drain(BUS_DRAIN_S)
        except asyncio.TimeoutError:
            log.warning("关停时总线 %.0f 秒内没有排空，剩下的事件放弃", BUS_DRAIN_S)

    async def _hook_guard_stop(self) -> None:
        await self.guard.stop()

    async def _hook_adapter_close(self) -> None:
        await self.adapter.close()

    async def _hook_listsync_close(self) -> None:
        """名单同步的长连接（作业已停，不会再拉）；替身没有 ``close`` 就不管。"""
        close = getattr(self.listsync, "close", None)
        if close is not None:
            await close()

    async def _hook_http_stop(self) -> None:
        server, task = self._server, self._server_task
        if server is None or task is None or task.done():  # 没起来过，或已经退出了（起不来的原因启动时已经抛出）
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), HTTP_STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("控制接口 %.0f 秒内没有关干净，强制退出", HTTP_STOP_TIMEOUT_S)
            server.force_exit = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except Exception:
            log.exception("控制接口退出时出错")

    async def _hook_unsubscribe(self) -> None:
        for service in (self.riskgate, self.ingest, self.intents, self.registry):
            await service.stop()
        for subscription in self._reactions:
            subscription.cancel()
        self._reactions = []

    async def _hook_notifier_stop(self) -> None:
        await self.notifier.stop()

    async def _hook_bus_stop(self) -> None:
        await self.bus.stop()

    async def _hook_ledger_close(self) -> None:
        await self.ledger.close()

    async def _hook_legacy_release(self) -> None:
        self.legacy.release_all()


# ---------------------------------------------------------------- 装配


def _read_standin_token(settings: Settings) -> str:
    path = Path(settings.standin_token_path)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise KernelError(f"读不到替身交易所给内核的访问令牌文件 {path}（{exc.strerror}）："
                          "先启动替身交易所（deploy/dev_start.sh 会先起它）") from None
    if not token:
        raise KernelError(f"替身交易所的访问令牌文件 {path} 是空的")
    return token


def build_kernel(settings: Settings, *, clock: Clock | None = None, transport: Transport | None = None,
                 crash: CrashRegistry | None = None, recovery_factory: Callable[..., Any] | None = None,
                 listsync: Any = "auto") -> Kernel:
    """只构造、不启动。打开账本（迁移前自动在线备份）、写本机令牌，别的什么也不做：不连交易所、不起任务。

    ``listsync`` 缺省 ``"auto"``：生产路径（没有传 ``transport``）造真正的 ``listsync.ListSync``（只拉 ``settings.list_url``），
    传了 ``transport``（测试）就不造，要名单的测试用 ``kernel.set_listsync(替身)``；也可以直接传一个名单同步对象或 ``None``。
    ``transport`` 缺省是连 ``settings.standin_url`` 的 HTTP 传输（令牌读 ``settings.standin_token_path``）；测试传
    ``tests/support/inproc.py`` 的进程内传输。``recovery_factory`` 缺省是 ``core.recovery.Recovery``，启动时才导入。"""
    reason = refusal_reason(settings)
    if reason is not None:
        raise StartRefused(reason)
    clock = clock if clock is not None else SystemClock()
    production = transport is None
    if transport is None:
        transport = HttpTransport(settings.standin_url, token=_read_standin_token(settings))
    ledger = Ledger.open(settings.db_path, backup_dir=settings.backup_dir, clock=clock)
    try:
        return _assemble(settings, clock, transport, crash, recovery_factory, ledger,
                         production=production and listsync == "auto", listsync=listsync)
    except BaseException:
        ledger.close_sync()
        raise


def _assemble(settings: Settings, clock: Clock, transport: Transport, crash: CrashRegistry | None,
              recovery_factory: Callable[..., Any] | None, ledger: Ledger, *, production: bool = False,
              listsync: Any = None) -> Kernel:
    bus = EventBus()
    if crash is None:
        crash = CrashRegistry(live=settings.is_live, process_exit=True)  # 进程级：走到崩溃点 os._exit(137)
    if settings.crashpoint:
        crash.arm(settings.crashpoint)
    notifier = Notifier(ledger, bus, clock, urls=settings.alert_urls)
    heartbeat = Heartbeat(settings.var_dir / "kernel" / "heartbeat.json", clock, pid=os.getpid())
    config = ConfigStore(ledger, bus, clock, settings.environment, deploy_switch=settings.model3_deploy_switch)
    cfg = config.get
    instance = settings.instance_id + ledger.nonce  # 订单编号里的实例号；对账只认本实例的系统编号
    modelstate = ModelStateService(ledger, bus, clock)
    registry = Registry(ledger, bus, clock, config_get=cfg)
    riskgate = RiskGate(ledger, bus, clock, config_get=cfg, notifier=notifier)
    intents = IntentService(ledger, bus, clock, notifier=notifier)

    # 唯一的一张写权限令牌：协调者出示它，适配层在写方法出口拿它比对；别的组件拿不到
    capability = WriteCapability("coordinator")
    adapter = StandinAdapter(transport, capability=capability, bus=bus, clock=clock, config_get=cfg)
    stream = adapter.account_stream()

    orders = OrderService(ledger, bus, clock, adapter, instance=instance)
    ingest = Ingest(ledger, bus, clock, adapter, instance=instance, config_get=cfg)
    limits = Limits(clock, config_get=cfg)
    rules = InstrumentRules(clock)
    server_clock = ServerClock(clock)
    candles = CandleService(adapter, ledger, bus, clock, server_clock, config_get=cfg)
    boxed: dict[str, Any] = {}  # 协调者先于模型 1 造出来：名单复核钩子经这里取到后造的模型 1

    def list_check(symbol: str, side: Any) -> tuple[bool, str]:
        service = boxed.get("m1")
        return (False, "模型 1 还没有装配") if service is None else service.list_check(symbol, side)

    coordinator = Coordinator(
        adapter=adapter, capability=capability, ledger=ledger, bus=bus, clock=clock, config_get=cfg,
        modelstate=modelstate, registry=registry, intents=intents, orders=orders, riskgate=riskgate, limits=limits,
        rules=rules, notifier=notifier, crash=crash, list_check=list_check)
    reconciler = Reconciler(
        adapter=adapter, registry=registry, modelstate=modelstate, coordinator=coordinator, ingest=ingest,
        orders=orders, limits=limits, ledger=ledger, bus=bus, clock=clock, config_get=cfg, notifier=notifier,
        instance=instance)
    protection = Protection(
        coordinator=coordinator, registry=registry, modelstate=modelstate, candles=candles, rules=rules,
        adapter=adapter, ledger=ledger, bus=bus, clock=clock, config_get=cfg, notifier=notifier, crash=crash,
        limits=limits)
    legacy = LegacyMutex(legacy_runtime_dir(settings), enabled=settings.legacy_mutex_enabled)
    guard = Guard(settings=settings, adapter=adapter, modelstate=modelstate, bus=bus, clock=clock, config_get=cfg,
                  notifier=notifier, legacy=legacy, risk_status=riskgate.status)
    models: dict[Model, TradingModel] = {Model.M1: StrategyModel(), Model.M2: TakeoverModel(),
                                         Model.M3: ReservedModel()}
    operations = Operations(ledger, clock, bus)
    config_ops = ConfigOps(operations, config)

    local_token = _api("auth").write_local_token(settings.local_token_path)  # 0600
    context = _api("context").ApiContext(
        settings=settings, clock=clock, ledger=ledger, bus=bus, modelstate=modelstate, registry=registry,
        config=config, riskgate=riskgate, limits=limits, notifier=notifier, models=models, local_token=local_token,
        heartbeat_path=heartbeat.path,
        ops_wiring=OpsWiring(coordinator=coordinator, adapter=adapter, rules=rules, orders=orders))
    app = _api("app").create_app(context)
    app.state.config_ops = config_ops  # 控制接口的 PUT /config 与内核共用一个 ConfigOps（它那把串行锁才起作用）
    app.state.candles = candles  # /candles 在账本不足时用它补数

    kernel = Kernel(
        settings=settings, clock=clock, bus=bus, ledger=ledger, crash=crash, adapter=adapter, stream=stream,
        notifier=notifier, heartbeat=heartbeat, config=config, modelstate=modelstate, registry=registry,
        intents=intents, orders=orders, ingest=ingest, riskgate=riskgate, limits=limits, rules=rules,
        server_clock=server_clock, candles=candles, coordinator=coordinator, reconciler=reconciler,
        protection=protection, legacy=legacy, guard=guard, models=models, operations=operations,
        config_ops=config_ops, app=app, recovery_factory=recovery_factory)
    context.ops_wiring.recover = kernel.rerun_recovery  # 重新恢复要用内核自己的恢复工厂
    context.ops_wiring.last_recovery = lambda: kernel.recovery_result

    # M2 操作服务：全应用一套（锁才起作用），一键全平与切换共用它的 deps
    ops_services = _api("routes_ops").build_ops_services(context, operations=operations)
    app.state.ops_services = ops_services
    kernel.close_all = CloseAll(ops_services.deps, intents=intents, bus=bus, crash=crash)
    kernel.switch = Switch(ops_services.deps, intents=intents, close_all=kernel.close_all, bus=bus, crash=crash)
    app.state.bulk_ops = BulkOps(kernel.close_all, kernel.switch)

    # M3 模型 2：识别与自动接管，以及接管类操作（经保护单管理串行）
    kernel.takeover = TakeoverService(
        registry=registry, modelstate=modelstate, coordinator=coordinator, adapter=adapter, ledger=ledger, bus=bus,
        clock=clock, config_get=cfg, notifier=notifier, crash=crash)
    kernel.takeover_ops = TakeoverOps(ops_services.deps, service=kernel.takeover, protection=protection, ledger=ledger)
    app.state.takeover_ops = kernel.takeover_ops

    # M4 模型 1：名单同步（并行任务实现；还没有就留空，由 set_listsync 接入）与按名单自动交易
    kernel.m1 = StrategyService(
        registry=registry, modelstate=modelstate, coordinator=coordinator, candles=candles, ledger=ledger, bus=bus,
        clock=clock, config_get=cfg, notifier=notifier, rules=rules, limits=limits, listsync_get=lambda: kernel.listsync)
    boxed["m1"] = kernel.m1
    app.state.m1 = kernel.m1
    # 状态条的「选币名单」：每次请求现取名单同步的当前视图（没有接入时是 None）
    context.list_view = lambda: None if kernel.listsync is None else kernel.listsync.current()
    if production:
        listsync = _make_listsync(settings, ledger=ledger, bus=bus, clock=clock, config_get=cfg, rules=rules,
                                  notifier=notifier)
    if listsync is not None and listsync != "auto":
        kernel.set_listsync(listsync)
    return kernel


def _make_listsync(settings: Settings, **parts: Any) -> Any:
    """造名单同步（``dmr_kernel.listsync.ListSync``，只拉 ``settings.list_url``）。模块还没有就返回 None。"""
    try:
        cls = getattr(importlib.import_module("dmr_kernel.listsync"), "ListSync", None)
    except ImportError:
        cls = None
    if cls is None:
        log.warning("名单同步模块还没有提供 ListSync：模型 1 拿不到名单")
        return None
    return cls(url=settings.list_url, **parts)


# ---------------------------------------------------------------- 生产入口


def _complain(message: str) -> None:
    log.error("%s", message)
    print(f"内核没有启动：{message}", file=sys.stderr)


async def run_kernel(settings: Settings) -> int:
    """生产入口（``__main__`` 调用）：检查环境 → 取内核锁 → 装配 → 启动 → 等终止信号 → 关停 → 释放锁。

    退出码：0 正常关停；1 装配、启动或关停失败（含另一个内核正在运行）；2 按设计拒绝启动（非替身环境）。"""
    reason = refusal_reason(settings)
    if reason is not None:
        _complain(reason)
        return 2
    try:
        lock = acquire_kernel_lock(settings.kernel_lock_path)
    except KernelLockError as exc:
        _complain(str(exc))
        return 1
    try:
        try:
            kernel = build_kernel(settings)
        except StartRefused as exc:
            _complain(str(exc))
            return 2
        except KernelError as exc:
            _complain(str(exc))
            return 1
        except Exception:
            log.exception("内核装配失败")
            print("内核没有启动：装配失败，详见日志", file=sys.stderr)
            return 1
        return await _serve(kernel)
    finally:
        lock.release()


async def _serve(kernel: Kernel) -> int:
    lifecycle = kernel.lifecycle
    lifecycle.install_signal_handlers()
    start_task = asyncio.ensure_future(kernel.start(serve_http=True, run_jobs=True))
    stop_wanted = asyncio.ensure_future(lifecycle.wait_stop())
    code = 0
    try:
        await asyncio.wait({start_task, stop_wanted}, return_when=asyncio.FIRST_COMPLETED)
        if start_task.done():
            failure = None if start_task.cancelled() else start_task.exception()
            if failure is not None:
                log.error("内核启动失败", exc_info=failure)
                print(f"内核没有启动：{type(failure).__name__}: {failure}", file=sys.stderr)
                code = 1
            else:
                await stop_wanted  # 等终止信号
                log.info("收到终止信号（%s），开始关停", lifecycle.stop_reason or "未说明")
        else:
            # 恢复可能要几十秒，而守护只给 30 秒：启动中收到终止信号就放弃启动，直接按顺序关停
            log.warning("启动中收到终止信号（%s），放弃启动，直接关停", lifecycle.stop_reason or "未说明")
            start_task.cancel()
            await asyncio.gather(start_task, return_exceptions=True)
    finally:
        stop_wanted.cancel()
        await asyncio.gather(stop_wanted, return_exceptions=True)
        try:
            await kernel.stop()
        except Exception:
            log.exception("关停出错")
            code = 1
    return code
