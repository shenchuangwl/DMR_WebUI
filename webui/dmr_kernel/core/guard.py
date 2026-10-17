"""周期复核与旧入口互斥（第 5 层）。

- ``LegacyMutex``：旧入口 7 把单实例锁的内核侧（报告 5.1「第一层」），只在实盘启用。
- ``Guard``：启动自检（``startup``）、每 60 秒的周期复核（``periodic``）、人工确认互斥恢复（``confirm_mutex``），
  以及对数据流状态、风控状态、旧程序格式成交的事件映射（``start``/``stop`` 订阅与取消）。

调度约定（``core.scheduler``/``core.recovery``）：重启恢复的第 1 步先调 ``startup()``（在数据流启动之前，它会清掉上一次
运行留下的数据流状态），返回的问题清单非空就不进入可交易状态；``start()`` 订阅事件；之后每 60 秒调一次 ``periodic()``。
关停时 ``stop()`` 只取消订阅，不释放旧入口的锁：锁要一直持有到进程退出，由装配层在最后调 ``LegacyMutex.release_all()``。
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import (
    Event, EventBus, FillIngested, ReconcileCompleted, RiskStateChanged, StreamStatusChanged, Subscription,
)
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.types import (
    AlertLevel, Gate, Known, OrderOrigin, ReadResult, RestrictionInfo, RunMode, Suppression, Unknown,
)
from dmr_kernel.settings import Settings

if TYPE_CHECKING:
    from dmr_kernel.alerts.notifier import Notifier
    from dmr_kernel.exchange.adapter import ExchangeAdapter

log = logging.getLogger(__name__)

__all__ = ["Guard", "LegacyMutex", "MARKER_NAME", "PERIOD_S", "RESTRICTIONS_EVERY_S", "legacy_runtime_dir"]

MARKER_NAME = "LEGACY_DISABLED"  # 停用标记：由操作者建、删，内核只检查它在不在
PERIOD_S = 60  # 周期复核的间隔：Guard 自己没有定时器，由调度器按它调 periodic()
RESTRICTIONS_EVERY_S = 300  # 交易所限制每 5 分钟读一次
_SCHEDULE_JITTER_MS = 1_000  # 调度抖动容限：第 5 次调用与上次读取相隔 299.99x 秒时也读，免得每次都顺延到第 6 分钟
_DIR_MODE = 0o700
_LOCK_FILE_MODE = 0o600


def legacy_runtime_dir(settings: Settings) -> Path:
    """旧入口的锁目录与停用标记所在处：DMR_WebUI/data/runtime_state。只算路径，不碰文件系统。"""
    return settings.dmr_root / "data" / "runtime_state"


class LegacyMutex:
    """内核启动时取得并一直持有旧入口的 7 把单实例锁，旧入口会在取锁处被拒（退出码 3）。

    做法与旧入口的 ``common/atomic_state.single_instance`` 完全一致：对 ``<目录>/instance_<名>.lock`` 取独占 ``flock``，
    取得后把本进程号写进文件。进程退出即释放，不会残留。内核不导入 DMR 模块，不派生子进程。

    ``enabled=False``（替身、测试网、只读影子）时三个方法直接返回，构造时也不碰路径：连目录不存在都不会报错、不会创建。
    """

    LOCK_NAMES = ("bit_short_term", "bit_long_term", "first_short_term", "first_long_term",
                  "legacy_short_term", "legacy_long_term", "main")

    def __init__(self, runtime_dir: Path | str, *, enabled: bool) -> None:
        self._dir = Path(runtime_dir)
        self._enabled = bool(enabled)
        self._fds: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def held(self) -> tuple[str, ...]:
        """现在持有的锁名（按 ``LOCK_NAMES`` 的顺序）。不读文件系统。"""
        return tuple(name for name in self.LOCK_NAMES if name in self._fds)

    # ------------------------------------------------------------ 取锁与释放

    def acquire_all(self) -> list[str]:
        """对 7 个锁文件取独占锁并写入本进程号，返回问题清单（空 = 全部取得）。

        目录必须已经存在且权限正好是 0700（不存在或权限不对时一把锁也不取，更不创建目录）；锁文件不存在就建（0600）。
        某把锁取不到说明对应的旧入口正在运行，问题里带上锁文件里记录的进程号；其余的锁照常持有。
        可以重复调用：已经持有且仍是路径上那个文件的锁原样保留，过期的（文件被删除或替换）丢掉后对新文件重新取。
        """
        if not self._enabled:
            return []
        problems, _ = self._inspect_directory()
        if problems:
            return problems
        for name in self.LOCK_NAMES:
            problem = self._acquire(name)
            if problem is not None:
                problems.append(problem)
        return problems

    def release_all(self) -> None:
        if not self._enabled:
            return
        for name in list(self._fds):
            self._drop(name)

    # ------------------------------------------------------------ 周期复核

    def check(self, *, skip_unheld: bool = False) -> list[str]:
        """复核并返回问题清单（空 = 正常）；只读，不取锁、不建文件。

        目录必须是 0700；所持的每个文件与路径上的文件是同一个（fstat 与 stat 的设备号、inode 相同）——锁文件被删除重建后，
        旧入口可以取到新文件上的锁；停用标记 ``LEGACY_DISABLED`` 必须存在。``skip_unheld`` 不报没有持有的锁
        （刚调过 ``acquire_all`` 的启动自检用，取不到的锁那边已经报过了）。
        """
        if not self._enabled:
            return []
        problems, usable = self._inspect_directory()
        if not usable:
            return problems
        for name in self.LOCK_NAMES:
            path = self._lock_path(name)
            fd = self._fds.get(name)
            if fd is None:
                if not skip_unheld:
                    problems.append(f"旧入口锁 {name} 没有持有（{path}，锁文件记录的进程号 {self._recorded_pid(path) or '未知'}）")
                continue
            problem = self._identity_problem(name, fd)
            if problem is not None:
                problems.append(problem)
        problems.extend(self._marker_problems())
        return problems

    # ------------------------------------------------------------ 内部

    def _lock_path(self, name: str) -> Path:
        return self._dir / f"instance_{name}.lock"

    def _inspect_directory(self) -> tuple[list[str], bool]:
        """返回（问题, 目录是否存在且是目录）。权限不对时目录仍可用（其余检查照做）。"""
        try:
            info = os.stat(self._dir)
        except FileNotFoundError:
            return [f"旧入口锁目录不存在：{self._dir}（必须是 DMR_WebUI 的 data/runtime_state）"], False
        except OSError as exc:
            return [f"无法读取旧入口锁目录 {self._dir}：{exc.strerror or exc}"], False
        if not stat.S_ISDIR(info.st_mode):
            return [f"旧入口锁目录不是目录：{self._dir}"], False
        mode = stat.S_IMODE(info.st_mode)
        if mode != _DIR_MODE:
            return [f"旧入口锁目录权限必须是 0700，现在是 {mode:04o}：{self._dir}"], True
        return [], True

    def _marker_problems(self) -> list[str]:
        path = self._dir / MARKER_NAME
        try:
            os.stat(path)
        except FileNotFoundError:
            return [f"停用标记 {MARKER_NAME} 不存在（{path}）：旧入口和启动脚本不会拒绝执行"]
        except OSError as exc:
            return [f"无法读取停用标记 {MARKER_NAME}（{path}）：{exc.strerror or exc}"]
        return []

    def _identity_problem(self, name: str, fd: int) -> str | None:
        """所持的文件与路径上的文件不是同一个时给出问题；是同一个返回 None。"""
        path = self._lock_path(name)
        try:
            on_path = os.stat(path)
        except FileNotFoundError:
            return f"旧入口锁文件已被删除：{path}（旧入口现在可以重新建出它并取到锁）"
        except OSError as exc:
            return f"无法读取旧入口锁文件 {path}：{exc.strerror or exc}"
        held = os.fstat(fd)
        if (held.st_dev, held.st_ino) != (on_path.st_dev, on_path.st_ino):
            return f"旧入口锁文件已被替换（删除后重建）：{path}（所持的锁已不在路径上，旧入口现在可以取到新文件上的锁）"
        return None

    def _acquire(self, name: str) -> str | None:
        path = self._lock_path(name)
        fd = self._fds.get(name)
        if fd is not None:
            if self._identity_problem(name, fd) is None:
                return None  # 已持有，而且仍是路径上的那个文件
            self._drop(name)  # 过期的锁：丢掉，对路径上现在的文件重新取
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, _LOCK_FILE_MODE)
        except OSError as exc:
            return f"无法打开旧入口锁文件 {path}：{exc.strerror or exc}"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                return self._busy_message(name)
            return f"无法对旧入口锁 {name} 加锁（{path}）：{exc.strerror or exc}"
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, str(os.getpid()).encode(), 0)
        except OSError as exc:  # 锁已经拿到；进程号只是给旧入口的拒绝信息和排查用的
            log.warning("旧入口锁 %s 已取得，但写不进进程号：%s", path, exc)
        self._fds[name] = fd
        return None

    def _busy_message(self, name: str) -> str:
        path = self._lock_path(name)
        holder = self._recorded_pid(path)
        return f"旧入口锁 {name} 已被占用，对应的旧入口可能正在运行（{path}，锁文件记录的进程号 {holder or '未知'}）"

    @staticmethod
    def _recorded_pid(path: Path) -> str:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            return ""
        try:
            text = os.pread(fd, 64, 0).decode(errors="replace").strip()
        except OSError:
            return ""
        finally:
            os.close(fd)
        return text if text.isdigit() else ""

    def _drop(self, name: str) -> None:
        fd = self._fds.pop(name, None)
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# ====================================================================== Guard

_MODE_NOT_HEDGE = ("持仓模式不是对冲模式（现在是单向持仓）：本系统只支持对冲模式，开仓与止损都按持仓方向下单。"
                   "请在交易所把持仓模式改成对冲（Hedge Mode）")
_MUTEX_NOT_ENABLED = "旧入口互斥未启用（装配错误）：实盘环境必须启用，否则 7 把旧入口锁一把也没取，旧入口不会在取锁处被拒"


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


class Guard:
    """周期复核、旧入口互斥的内核侧、数据流与风控状态到模型状态的映射。

    - 启动自检（``startup``）：持仓模式必须是对冲（所有环境）；实盘还要取得 7 把旧入口锁、检查锁目录与停用标记；
      清掉上一次运行留下的数据流状态；交易所限制读一次。返回问题清单，非空 = 不进入可交易状态。
      互斥相关的问题同时置「互斥异常」抑制，即使调用方忽略返回值也启动不了。
    - 周期复核（``periodic``，每 60 秒）：持仓模式（只告警）；实盘的锁与停用标记（异常置「互斥异常」，要人工确认才解除，
      见 ``confirm_mutex``）；每 5 分钟一次交易所限制（置、解「交易所限制」）。三项互不影响，一项出错不挡其余。
    - 事件映射（``start`` 订阅，一个订阅按发布顺序处理四类事件）：
      数据流 interrupted → 置「数据流切换中」；仅轮询下完成一轮完整持仓对账 → 解除并进入「仅轮询降级」；
      recovered_stable → 退出降级。账户级风控触发 → 置「风控触发」，复位后解除（模型级不是抑制原因）。
      旧程序格式的成交 → 关上开仓闸并发最高级告警（运行期兜底）。

    旧入口互斥只看环境（``settings.legacy_mutex_enabled``，即实盘）：非实盘不碰互斥对象，更不会打开 data/runtime_state。
    ``risk_status`` 是可选的 ``RiskGate.status``：启动时用它与持久化的「风控触发」抑制对齐（风控闸的 load/start 不发事件）。
    """

    def __init__(self, *, settings: Settings, adapter: "ExchangeAdapter", modelstate: ModelStateService, bus: EventBus,
                 clock: Clock, config_get: Callable[[str], Any], notifier: "Notifier", legacy: LegacyMutex,
                 risk_status: Callable[[], Mapping[str, Any]] | None = None) -> None:
        self._adapter = adapter
        self._modelstate = modelstate
        self._bus = bus
        self._clock = clock
        self._config_get = config_get
        self._notifier = notifier
        self._legacy = legacy
        self._risk_status = risk_status
        self._live = bool(settings.legacy_mutex_enabled)
        self._lock = asyncio.Lock()  # startup、periodic、confirm_mutex 一次只跑一个
        self._subscription: Subscription | None = None
        # 数据流：自上一次中断以来是否已经 recovered_stable（中断时清零）
        self._stable = False
        self._stream_state: str | None = None
        self._stream_ms: int | None = None
        # 周期复核的观察
        self._position_mode = "unknown"  # hedge | one_way | unknown
        self._position_mode_ms: int | None = None
        self._restricted: bool | None = None
        self._restriction_detail: dict[str, str] = {}
        self._restrictions_ms: int | None = None
        self._mutex_problems: list[str] = []
        self._mutex_ms: int | None = None
        self._periodic_ms: int | None = None
        self._legacy_fills = 0
        self._legacy_fill_ms: int | None = None

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """订阅数据流状态、对账结果、风控状态和成交入账事件。可重复调用。"""
        if self._subscription is None:
            self._subscription = self._bus.subscribe(
                (StreamStatusChanged, ReconcileCompleted, RiskStateChanged, FillIngested), self._on_event, name="guard")

    async def stop(self) -> None:
        """取消订阅。不释放旧入口的锁：关停期间旧入口也不能起来，锁一直持有到进程退出。"""
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None

    # ------------------------------------------------------------ 启动自检

    async def startup(self) -> list[str]:
        """启动自检，返回问题清单（空 = 通过）。须在账户数据流启动之前调用（重启恢复的第 1 步）。

        持仓模式读不到或不是对冲都算不通过（读不到不能当成对冲）。实盘：取 7 把锁并检查目录（0700）与停用标记。
        可重复调用。上一次运行留下的「互斥异常」不会被它清掉（要人工确认）。
        """
        async with self._lock:
            problems: list[str] = []
            mode_problem = await self._check_position_mode()
            if mode_problem is not None:
                problems.append(mode_problem)
            if self._live:
                mutex_problems = self._startup_mutex()
                if mutex_problems:
                    await self._flag_mutex(mutex_problems)
                    problems.extend(mutex_problems)
            await self._reset_stream_state()
            await self._align_with_risk_gate()
            await self._check_restrictions(self._clock.now_ms(), force=True)
            return problems

    def _startup_mutex(self) -> list[str]:
        self._mutex_ms = self._clock.now_ms()
        if not self._legacy.enabled:
            self._mutex_problems = [_MUTEX_NOT_ENABLED]
        else:
            # 目录问题 acquire_all 与 check 各报一次，合并去重；没取到的锁 acquire_all 已报，check 不再重复
            self._mutex_problems = _unique(self._legacy.acquire_all() + self._legacy.check(skip_unheld=True))
        return list(self._mutex_problems)

    async def _reset_stream_state(self) -> None:
        """上一次运行留下的「数据流切换中」与「仅轮询降级」属于上一个数据流会话：新会话没有中断就不该带着它们
        （否则下一次完整对账会把运行方式永远留在降级，因为 recovered_stable 只在中断之后的重连才发）。
        配置里关掉了数据流（``ex_stream_enabled`` 为假）则一直是仅轮询降级。
        这次运行里已经收到过数据流状态（装配顺序错了，数据流先起来）就不动：那是这一次的状态，不是遗留的。"""
        if self._stream_state is not None:
            return
        self._stable = False
        await self._remove(Suppression.STREAM_SWITCHING, "重启：上一次运行的数据流状态作废")
        target = RunMode.NORMAL if self._stream_enabled() else RunMode.POLLING_DEGRADED
        await self._set_run_mode(target, "重启：数据流状态重新开始" if self._stream_enabled() else "配置里没有启用账户数据流")

    async def _align_with_risk_gate(self) -> None:
        """风控闸 load/start 不发事件：重启后它恢复出的账户级触发状态，在这里与持久化的「风控触发」抑制对齐。"""
        if self._risk_status is None:
            return
        try:
            status = self._risk_status()
        except Exception:
            log.exception("读取风控闸状态失败，「风控触发」抑制保持原样")
            return
        if not status.get("loaded", True):
            log.warning("风控闸还没加载，不与它对齐「风控触发」抑制")
            return
        if status.get("account_tripped"):
            await self._add(Suppression.RISK_TRIPPED, "启动对齐：风控闸恢复出账户级触发")
        else:
            await self._remove(Suppression.RISK_TRIPPED, "启动对齐：风控闸没有账户级触发")

    # ------------------------------------------------------------ 周期复核

    async def periodic(self) -> None:
        """每 60 秒一次。持仓模式、旧入口的锁与停用标记（实盘）、交易所限制（每 5 分钟）三项互相独立。
        一次没跑完时下一次排队等它，不会同时在途。"""
        async with self._lock:
            now = self._clock.now_ms()
            self._periodic_ms = now
            checks: tuple[tuple[str, Callable[[int], Awaitable[None]]], ...] = (
                ("持仓模式", self._periodic_position_mode),
                ("旧入口锁与停用标记", self._periodic_mutex),
                ("交易所限制", self._check_restrictions),
            )
            for name, check in checks:
                try:
                    await check(now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("周期复核「%s」出错，其余项照常复核", name)

    async def _periodic_position_mode(self, now: int) -> None:
        await self._check_position_mode()  # 运行期只告警（持仓模式没有对应的抑制原因）

    async def _periodic_mutex(self, now: int) -> None:
        if not self._live:
            return
        problems = [_MUTEX_NOT_ENABLED] if not self._legacy.enabled else _unique(self._legacy.check())
        self._mutex_problems = problems
        self._mutex_ms = now
        if problems:
            await self._flag_mutex(problems)

    # ------------------------------------------------------------ 人工确认

    async def confirm_mutex(self, *, operator: str) -> list[str]:
        """操作者确认旧入口互斥已恢复，解除「互斥异常」。

        实盘：先重新取锁（被删除重建的锁文件在这里对新文件重新加锁）再完整复核；还有问题就保持抑制并返回问题清单。
        非实盘不碰任何路径，只清掉遗留的抑制。没有「互斥异常」时什么也不改。
        """
        async with self._lock:
            problems: list[str] = []
            if self._live:
                if not self._legacy.enabled:
                    problems = [_MUTEX_NOT_ENABLED]
                else:
                    problems = _unique(self._legacy.acquire_all() + self._legacy.check(skip_unheld=True))
                self._mutex_problems = problems
                self._mutex_ms = self._clock.now_ms()
            if problems:
                await self._flag_mutex(problems)
                return problems
            if await self._remove(Suppression.MUTEX_ANOMALY, f"操作者 {operator} 确认旧入口互斥已恢复"):
                self._alert(AlertLevel.INFO, "legacy_mutex_restored",
                            f"操作者 {operator} 已确认旧入口互斥恢复，「互斥异常」抑制已解除。")
            return []

    async def _flag_mutex(self, problems: list[str]) -> None:
        text = "；".join(problems)
        await self._add(Suppression.MUTEX_ANOMALY, f"旧入口互斥异常：{text}")
        self._alert(AlertLevel.CRITICAL, "legacy_mutex_anomaly",
                    f"旧入口互斥异常，已置「互斥异常」抑制（锁和停用标记恢复后须人工确认才解除）：{text}",
                    dedupe_key="legacy_mutex_anomaly")

    # ------------------------------------------------------------ 持仓模式与交易所限制

    async def _check_position_mode(self) -> str | None:
        """读持仓模式。是对冲返回 None；单向时告警并返回问题；读不到（或读到非布尔值）返回问题但不告警，不当成对冲。"""
        result = await self._read(self._adapter.fetch_position_mode)
        now = self._clock.now_ms()
        if isinstance(result, Known) and isinstance(result.value, bool):
            self._position_mode = "hedge" if result.value else "one_way"
            self._position_mode_ms = now
            if result.value:
                return None
            self._alert(AlertLevel.CRITICAL, "position_mode_not_hedge", _MODE_NOT_HEDGE,
                        dedupe_key="position_mode_not_hedge")
            return _MODE_NOT_HEDGE
        self._position_mode = "unknown"
        reason = result.reason if isinstance(result, Unknown) else f"适配层返回了非布尔值 {result.value!r}"
        log.warning("无法确认持仓模式：%s", reason)
        return f"无法确认持仓模式：{reason}"

    async def _check_restrictions(self, now: int, *, force: bool = False) -> None:
        """第一次立即读，之后每 5 分钟读一次（读不到不计，下个周期再试）。受限置抑制，解除后自动解除；未知不改任何状态。"""
        due = self._restrictions_ms is None or now - self._restrictions_ms >= RESTRICTIONS_EVERY_S * 1000 - _SCHEDULE_JITTER_MS
        if not (force or due):
            return
        result = await self._read(self._adapter.fetch_restrictions)
        if not (isinstance(result, Known) and isinstance(result.value, RestrictionInfo)):
            reason = result.reason if isinstance(result, Unknown) else f"适配层返回了 {result.value!r}"
            log.warning("交易所限制读取失败（%s）：抑制原因保持原样，下个周期重试", reason)
            return
        info = result.value
        previous = self._restricted
        self._restricted = info.restricted
        self._restriction_detail = dict(info.detail)
        self._restrictions_ms = now
        if info.restricted:
            detail = "；".join(f"{key}={value}" for key, value in info.detail) or "交易所没有给出细节"
            await self._add(Suppression.EXCHANGE_RESTRICTED, f"交易所限制：{detail}")
            if previous is not True:
                self._alert(AlertLevel.WARNING, "exchange_restricted",
                            f"交易所对账户施加了限制，已置「交易所限制」抑制（不新开仓，平仓与保护照常）：{detail}")
        elif await self._remove(Suppression.EXCHANGE_RESTRICTED, "交易所限制已解除") or previous is True:
            self._alert(AlertLevel.INFO, "exchange_restriction_lifted", "交易所对账户的限制已解除，「交易所限制」抑制已解除。")

    # ------------------------------------------------------------ 事件

    async def _on_event(self, event: Event) -> None:
        if isinstance(event, StreamStatusChanged):
            await self._on_stream_status(event)
        elif isinstance(event, ReconcileCompleted):
            await self._on_reconcile(event)
        elif isinstance(event, RiskStateChanged):
            await self._on_risk(event)
        elif isinstance(event, FillIngested):
            await self._on_fill(event)

    async def _on_stream_status(self, event: StreamStatusChanged) -> None:
        self._stream_state, self._stream_ms = event.state, event.at_ms
        if event.state == "interrupted":
            self._stable = False
            await self._add(Suppression.STREAM_SWITCHING, f"账户数据流中断：{event.reason or '原因未知'}")
            self._alert(AlertLevel.WARNING, "stream_interrupted",
                        f"账户数据流中断（{event.reason or '原因未知'}）：已置「数据流切换中」抑制并切到仅轮询，"
                        f"完成一轮完整持仓对账后进入仅轮询降级运行。", dedupe_key="stream_interrupted")
        elif event.state == "recovered_stable":
            self._stable = True
            if not self._stream_enabled():
                return  # 配置里没有启用数据流：一直是仅轮询降级
            if self._modelstate.current().run_mode is RunMode.POLLING_DEGRADED:
                await self._set_run_mode(RunMode.NORMAL, "账户数据流已恢复并稳定")
                self._alert(AlertLevel.INFO, "stream_recovered", "账户数据流已恢复并稳定，退出仅轮询降级，运行方式回到正常。")

    async def _on_reconcile(self, event: ReconcileCompleted) -> None:
        """只有「完整的持仓对账成功」才结束数据流切换：对账未知、不一致，或只是挂单对账成功，都不算。"""
        if event.kind != "positions" or event.status != "ok":
            return
        if Suppression.STREAM_SWITCHING not in self._modelstate.current().suppressions:
            return
        # 先定运行方式再解除抑制：解除后马上可以开仓，那一刻的上限必须已经是降级的
        recovered = self._stream_enabled() and self._stable
        await self._set_run_mode(RunMode.NORMAL if recovered else RunMode.POLLING_DEGRADED,
                                 "仅轮询下已完成一轮完整持仓对账")
        await self._remove(Suppression.STREAM_SWITCHING, "仅轮询下已完成一轮完整持仓对账")

    async def _on_risk(self, event: RiskStateChanged) -> None:
        """账户级触发 → 「风控触发」；模型级只停模型 1 的新开仓，不是抑制原因。告警由风控闸自己发。"""
        if event.account_tripped:
            await self._add(Suppression.RISK_TRIPPED, "；".join(event.reasons) or "账户级风控触发")
        else:
            await self._remove(Suppression.RISK_TRIPPED, "账户级风控已复位")

    async def _on_fill(self, event: FillIngested) -> None:
        """运行期兜底（报告 5.1「三层互斥」第四行）：出现旧程序格式的成交，说明有漏网的旧进程在下单。"""
        if event.origin is not OrderOrigin.LEGACY:
            return
        trade = event.trade
        self._legacy_fills += 1
        self._legacy_fill_ms = event.at_ms
        cid = event.client_order_id or "无自定义编号"
        closed = True
        try:
            await self._modelstate.set_gate(Gate.STOPPED, reason=f"检测到旧程序格式的成交：{cid}")
        except asyncio.CancelledError:
            raise
        except Exception:
            closed = False
            log.exception("检测到旧程序格式的成交，但关闭开仓闸失败")
        self._alert(AlertLevel.CRITICAL, "legacy_fill_detected",
                    f"检测到旧程序格式的成交（编号 {cid}，合约 {trade.symbol}，{trade.side.value}/{trade.position_side.value}，"
                    f"数量 {trade.qty}，价格 {trade.price}）：说明旧入口或另一份 DMR 拷贝仍在下单。"
                    + ("已关上开仓闸；" if closed else "关闭开仓闸失败，请立即手动停止开仓；")
                    + "请立即核查旧进程、停用标记与密钥隔离，查清后再手动启动。",
                    dedupe_key="legacy_fill_detected")

    # ------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        """当前观察到的状态（可直接转 JSON）。读不到的项是 ``unknown``/``None``，不是「正常」。"""
        return {
            "position_mode": self._position_mode,
            "position_mode_checked_ms": self._position_mode_ms,
            "restricted": self._restricted,
            "restriction_detail": dict(self._restriction_detail),
            "restrictions_checked_ms": self._restrictions_ms,
            "legacy_mutex": {"enabled": self._live, "held": list(self._legacy.held) if self._live else [],
                             "problems": list(self._mutex_problems), "checked_ms": self._mutex_ms},
            "stream": {"configured": self._stream_enabled(), "state": self._stream_state, "state_ms": self._stream_ms,
                       "stable_since_interruption": self._stable},
            "legacy_fills": {"count": self._legacy_fills, "last_ms": self._legacy_fill_ms},
            "periodic_ms": self._periodic_ms,
        }

    # ------------------------------------------------------------ 小工具

    def _stream_enabled(self) -> bool:
        return bool(self._config_get("ex_stream_enabled"))

    async def _read(self, method: Callable[[], Awaitable[ReadResult[Any]]]) -> Any:
        """适配层读方法；它自己出错也当作「未知」，不让一个坏的读拖垮整轮复核。"""
        try:
            return await method()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("适配层读取出错")
            return Unknown(f"{type(exc).__name__}: {exc}")

    async def _add(self, s: Suppression, reason: str) -> None:
        if s not in self._modelstate.current().suppressions:
            await self._modelstate.add_suppression(s, reason=reason)

    async def _remove(self, s: Suppression, reason: str) -> bool:
        """解除抑制原因；原来就有返回 True。"""
        if s not in self._modelstate.current().suppressions:
            return False
        await self._modelstate.remove_suppression(s, reason=reason)
        return True

    async def _set_run_mode(self, mode: RunMode, reason: str) -> None:
        if self._modelstate.current().run_mode is not mode:
            await self._modelstate.set_run_mode(mode, reason=reason)

    def _alert(self, level: AlertLevel, kind: str, message: str, *, dedupe_key: str | None = None) -> None:
        try:
            self._notifier.raise_alert(level, kind, message, dedupe_key=dedupe_key)
        except Exception:  # 告警出错不能影响状态的处理
            log.exception("发送告警失败：%s", kind)
