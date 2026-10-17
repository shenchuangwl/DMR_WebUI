"""内核不可用时的命令行应急入口（第 7 层 cli，T1.7 第二条路径）：``python -m dmr_kernel.cli emergency close [--yes]``。

这是 4.1 结构规则 1、2 的唯一例外：写方法只在 ``core.coordinator`` 与 ``cli.emergency`` 调用，``WriteCapability`` 只在装配层与
这里构造。放在子包里而不是单个 ``emergency.py``，是因为 ``tests/unit/cli/test_cli_entry.py`` 对 ``cli/*.py`` 一刀切地禁止导入
内核内部；本包是唯一需要适配层的命令行模块，其余模块仍只用标准库与 ``settings``。

步骤（出错一律停下，不猜）：

1. 写停用标记 ``var/run/kernel.nosupervise``（守护看到它就不再拉起内核；**谁写谁删**：这里只写，删由操作者决定）。
2. 按 ``var/run/kernel.pid`` 给内核发 SIGTERM，等它退出；不退出就 SIGKILL 并确认进程已经不存在。发信号前先核对该进程的命令行
   确是内核（进程号可能已被别的进程复用）；核对不了或不是内核就拒绝，什么信号也不发。
3. 取 ``var/kernel.lock``（取不到就拒绝：说明内核或别的应急入口还在），运行期间一直持有。
4. 用 ``WriteCapability("emergency")`` 与替身的 kernel 令牌建适配层，读全账户持仓，逐仓列出，操作者逐个确认（``--yes`` 只给测试用）
   后发市价平仓单（编号 ``ids.make_emergency_id``）；确认某仓归零后，再撤该合约该方向上带系统前缀的条件单。先平后撤：
   没归零就不撤保护。
5. 结果打印并追加写 ``var/log/emergency.log``；**不打开数据库**；结束时提示停用标记仍在，不自动删。
   内核恢复后 F9 按成交记录补记（来源「应急入口」）。

退出码：0 全部处理完；1 有仓没处理（拒绝、未归零、操作者跳过）或前置步骤失败；3 读不到交易所的持仓。
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from dmr_kernel.core.clock import SystemClock
from dmr_kernel.core.configdefs import initial_values
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.lifecycle import KernelLockError, acquire_kernel_lock
from dmr_kernel.core.types import (
    CancelOutcome,
    Known,
    OrderSide,
    PositionInfo,
    PositionSide,
    WriteAccepted,
    WriteRejected,
    WriteUnknown,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.adapter import ExchangeAdapter, WriteCapability
from dmr_kernel.exchange.standin import StandinAdapter
from dmr_kernel.exchange.transport import HttpTransport
from dmr_kernel.settings import Settings

__all__ = ["EXIT_FAILED", "EXIT_OK", "EXIT_UNAVAILABLE", "MARKER_REMINDER", "EmergencyDeps", "run_emergency_close"]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNAVAILABLE = 3

TERM_WAIT_S = 30.0  # 与 dev_stop.sh、supervise.sh 的优雅停止时限一致
KILL_WAIT_S = 10.0
SETTLE_WAIT_S = 10.0  # 市价单发出后等持仓归零的最长时间
POLL_S = 0.5
MARKER_REMINDER = "停用标记仍在，确认后删除 var/run/kernel.nosupervise 再启动内核"
YES = frozenset({"y", "yes", "是"})
QUIT = frozenset({"q", "quit", "退出"})


def _real_pid_alive(pid: int) -> bool:
    """进程还在运行（僵尸不算：它已经退出，只是还没被回收）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def _real_cmdline_of(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return None


@dataclass
class EmergencyDeps:
    """可注入的外部依赖（测试用假的）；缺省全是真的。``ask``、``sleep``、``monotonic`` 缺省时取 main 传来的。"""

    adapter_factory: Callable[[Settings, WriteCapability], ExchangeAdapter] | None = None
    send_signal: Callable[[int, int], None] = os.kill
    pid_alive: Callable[[int], bool] = _real_pid_alive
    cmdline_of: Callable[[int], str | None] = _real_cmdline_of
    sleep: Callable[[float], None] | None = None
    monotonic: Callable[[], float] | None = None
    now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000
    ask: Callable[[str], str] | None = None
    term_wait_s: float = TERM_WAIT_S
    kill_wait_s: float = KILL_WAIT_S
    settle_wait_s: float = SETTLE_WAIT_S


class _StandinTokenError(Exception):
    pass


def _default_adapter(settings: Settings, capability: WriteCapability) -> ExchangeAdapter:
    path = Path(settings.standin_token_path)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise _StandinTokenError(f"读不到替身交易所给内核的访问令牌文件 {path}（{exc.strerror}）：应急平仓需要它；"
                                 "请确认替身交易所已在运行、DMR_VAR_DIR 与内核所用的一致。") from None
    if not token:
        raise _StandinTokenError(f"替身交易所的访问令牌文件 {path} 是空的")
    values = initial_values(settings.environment)
    return StandinAdapter(HttpTransport(settings.standin_url, token=token), capability=capability, bus=EventBus(),
                          clock=SystemClock(), config_get=values.__getitem__)


class _Log:
    """同时打印和追加写 var/log/emergency.log（每行带 UTC 时间）。日志写不了不影响应急动作，只提示一次。"""

    def __init__(self, path: Path, out: TextIO, err: TextIO) -> None:
        self._path, self._out, self._err = path, out, err
        self._warned = False

    def _write(self, text: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as handle:
                for line in text.splitlines() or [""]:
                    handle.write(f"{stamp} {line}\n")
        except OSError as exc:
            if not self._warned:
                self._warned = True
                print(f"警告：写不了应急日志 {self._path}（{exc.strerror}），结果只在终端显示。", file=self._err)

    def say(self, text: str) -> None:
        print(text, file=self._out)
        self._write(text)

    def problem(self, text: str) -> None:
        print(text, file=self._err)
        self._write(text)


def _side_text(side: PositionSide) -> str:
    return "多" if side is PositionSide.LONG else "空"


def _name(p: PositionInfo) -> str:
    return f"{p.symbol} {_side_text(p.side)}"


# ---------------------------------------------------------------- 第 1、2 步


def _write_marker(settings: Settings, log: _Log) -> Path | None:
    path = settings.var_dir / "run" / "kernel.nosupervise"
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        path.write_text(f"emergency close {stamp} pid={os.getpid()}\n", encoding="utf-8")
    except OSError as exc:
        log.problem(f"无法写停用标记 {path}（{exc.strerror}）：守护可能会把内核重新拉起来，所以不继续。")
        return None
    log.say(f"已写停用标记：{path}（守护看到它就不再拉起内核）。")
    return path


def _read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() and int(text) > 0 else None


def _wait_gone(pid: int, seconds: float, deps: EmergencyDeps, sleep: Callable[[float], None],
               mono: Callable[[], float]) -> bool:
    deadline = mono() + seconds
    while deps.pid_alive(pid):
        if mono() >= deadline:
            return False
        sleep(1.0)
    return True


def _stop_kernel(settings: Settings, deps: EmergencyDeps, log: _Log, sleep: Callable[[float], None],
                 mono: Callable[[], float]) -> bool:
    pid_file = settings.var_dir / "run" / "kernel.pid"
    pid = _read_pid(pid_file)
    if pid is None:
        log.say(f"没有可用的内核进程号文件（{pid_file}）：按内核已经不在运行处理，接下来用内核锁确认。")
        return True
    if not deps.pid_alive(pid):
        log.say(f"进程号文件里的内核（pid {pid}）已经不在运行。")
        return True
    cmdline = deps.cmdline_of(pid)
    if cmdline is None or "dmr_kernel" not in cmdline:
        log.problem(f"进程号文件里的进程（pid {pid}）{'读不到命令行，无法确认它是内核' if cmdline is None else '不是内核'}"
                    f"（{cmdline or '未知'}）：不发任何信号。请人工确认后处理 {pid_file}，再重新运行本命令。")
        return False
    log.say(f"向内核（pid {pid}）发 SIGTERM，最多等 {deps.term_wait_s:g} 秒……")
    try:
        deps.send_signal(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as exc:
        log.problem(f"无法向内核（pid {pid}）发 SIGTERM（{exc.strerror}）。")
        return False
    if _wait_gone(pid, deps.term_wait_s, deps, sleep, mono):
        log.say("内核已退出。")
        return True
    log.say(f"内核在 {deps.term_wait_s:g} 秒内没有退出，改发 SIGKILL。")
    try:
        deps.send_signal(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError as exc:
        log.problem(f"无法向内核（pid {pid}）发 SIGKILL（{exc.strerror}）。")
        return False
    if _wait_gone(pid, deps.kill_wait_s, deps, sleep, mono):
        log.say("内核已被强制结束。")
        return True
    log.problem(f"发了 SIGKILL 之后内核（pid {pid}）仍然存在：不继续，避免和内核同时下单。请人工处理（可能是不可中断的睡眠）。")
    return False


# ---------------------------------------------------------------- 第 4 步


def _confirm(deps: EmergencyDeps, text: str) -> str:
    """返回 "yes" | "no" | "quit"。输入结束按 no 处理。"""
    try:
        answer = deps.ask(text).strip().lower()  # type: ignore[misc]
    except EOFError:
        return "quit"
    if answer in YES:
        return "yes"
    return "quit" if answer in QUIT else "no"


async def _wait_zero(adapter: ExchangeAdapter, p: PositionInfo, deps: EmergencyDeps) -> bool | None:
    """True 归零；False 读到了但仍有仓；None 读不到。"""
    waited = 0.0
    verdict: bool | None = None
    while True:
        read = await adapter.fetch_positions()
        if isinstance(read, Known):
            left = [q for q in read.value if q.symbol == p.symbol and q.side is p.side and q.qty > 0]
            verdict = not left
            if verdict:
                return True
        else:
            verdict = None
        if waited >= deps.settle_wait_s:
            return verdict
        deps.sleep(POLL_S)  # type: ignore[misc]
        waited += POLL_S


async def _close_one(adapter: ExchangeAdapter, cap: WriteCapability, p: PositionInfo, seq: int,
                     deps: EmergencyDeps, log: _Log) -> bool:
    cid = ids.make_emergency_id(deps.now_ms(), seq)
    side = OrderSide.SELL if p.side is PositionSide.LONG else OrderSide.BUY
    log.say(f"平仓：{_name(p)} 数量 {p.qty}，市价 {side.value}，编号 {cid}")
    try:
        result = await adapter.create_market_order(cap, symbol=p.symbol, side=side, position_side=p.side, qty=p.qty,
                                                   client_order_id=cid)
    except Exception as exc:  # 适配层的校验错误等：这一仓记失败，别的仓照常处理
        log.problem(f"  {_name(p)}：下单失败（{type(exc).__name__}：{exc}）")
        return False
    if isinstance(result, WriteRejected):
        log.problem(f"  {_name(p)}：交易所拒绝了平仓单（{result.message}）")
    elif isinstance(result, WriteUnknown):
        log.problem(f"  {_name(p)}：平仓单的结果未知（{result.reason}），回查持仓确认")
    elif isinstance(result, WriteAccepted):
        log.say(f"  {_name(p)}：平仓单已受理，等持仓归零……")
    zero = await _wait_zero(adapter, p, deps)
    if zero is True:
        log.say(f"  {_name(p)}：已归零。")
        return True
    log.problem(f"  {_name(p)}：未归零（{'持仓仍在' if zero is False else '读不到持仓，无法确认'}）：不撤它的条件单，保护留在交易所。")
    return False


async def _cancel_protection(adapter: ExchangeAdapter, cap: WriteCapability, p: PositionInfo, log: _Log) -> bool:
    read = await adapter.fetch_open_algo_orders(p.symbol)
    if not isinstance(read, Known):
        log.problem(f"  {_name(p)}：读不到条件单（{getattr(read, 'reason', '未知')}），没有撤；请到交易所手工核对。")
        return False
    mine = [a for a in read.value if a.position_side is p.side and ids.is_system_id(a.client_algo_id)]
    ok = True
    for a in mine:
        cancelled = await adapter.cancel_algo_order(cap, symbol=a.symbol, client_algo_id=a.client_algo_id)
        if cancelled.outcome in (CancelOutcome.CANCELLED, CancelOutcome.NOT_FOUND):
            log.say(f"  {_name(p)}：条件单 {a.client_algo_id} 已撤{'（本来就不存在）' if cancelled.outcome is CancelOutcome.NOT_FOUND else ''}。")
        else:
            ok = False
            log.problem(f"  {_name(p)}：条件单 {a.client_algo_id} 撤销结果未知（{cancelled.message or '无说明'}），请到交易所核对。")
    if not mine:
        log.say(f"  {_name(p)}：没有带系统前缀的条件单需要撤。")
    return ok


async def _work(adapter: ExchangeAdapter, cap: WriteCapability, deps: EmergencyDeps, yes: bool, log: _Log) -> int:
    read = await adapter.fetch_positions()
    if not isinstance(read, Known):
        log.problem(f"读不到交易所的持仓（{getattr(read, 'reason', '未知')}）：没有下任何单。请稍后重试，或直接在 Binance 处置。")
        return EXIT_UNAVAILABLE
    positions = sorted((p for p in read.value if p.qty > 0), key=lambda q: (q.symbol, q.side.value))
    if not positions:
        log.say("交易所账户上没有持仓，无需平仓。")
        return EXIT_OK
    log.say(f"全账户持仓共 {len(positions)} 个（含外部与待确认的仓，应急入口不分来源）：")
    for p in positions:
        log.say(f"  {_name(p)} 数量 {p.qty} 均价 {p.entry_price} 标记价 {p.mark_price if p.mark_price is not None else '未知'}"
                f" 浮盈 {p.unrealized_pnl if p.unrealized_pnl is not None else '未知'}")
    done: list[PositionInfo] = []
    skipped: list[PositionInfo] = []
    failed: list[PositionInfo] = []
    quitting = False
    seq = 0
    for p in positions:
        if quitting:
            skipped.append(p)
            continue
        if not yes:
            answer = _confirm(deps, f"市价平掉 {_name(p)} 数量 {p.qty}？y 确认 / n 跳过 / q 退出：")
            if answer != "yes":
                skipped.append(p)
                quitting = answer == "quit"
                log.say(f"跳过：{_name(p)}（操作者{'选择退出' if quitting else '未确认'}）")
                continue
        seq += 1
        if await _close_one(adapter, cap, p, seq, deps, log) and await _cancel_protection(adapter, cap, p, log):
            done.append(p)
        else:
            failed.append(p)
    log.say(f"结果：已平并处理完 {len(done)} 个，跳过 {len(skipped)} 个，未成功 {len(failed)} 个。")
    for p in skipped:
        log.say(f"  未处理（跳过）：{_name(p)} 数量 {p.qty}，仍留在交易所。")
    for p in failed:
        log.say(f"  未成功：{_name(p)}，请到交易所核对。")
    return EXIT_OK if not skipped and not failed else EXIT_FAILED


# ---------------------------------------------------------------- 入口


async def _session(adapter: ExchangeAdapter, cap: WriteCapability, deps: EmergencyDeps, yes: bool, log: _Log) -> int:
    try:
        return await _work(adapter, cap, deps, yes, log)
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001 - 收尾失败不影响结果
            pass


def run_emergency_close(settings: Settings, *, yes: bool, out: TextIO, err: TextIO, deps: EmergencyDeps) -> int:
    log = _Log(settings.log_dir / "emergency.log", out, err)
    sleep = deps.sleep or time.sleep
    mono = deps.monotonic or time.monotonic
    deps.sleep, deps.monotonic = sleep, mono
    if deps.ask is None:
        deps.ask = input
    log.say("应急平仓开始" + ("（--yes：不逐个确认）" if yes else "") + f"，运行目录 {settings.var_dir}。")
    if _write_marker(settings, log) is None:
        return EXIT_FAILED
    if not _stop_kernel(settings, deps, log, sleep, mono):
        return EXIT_FAILED
    try:
        lock = acquire_kernel_lock(settings.kernel_lock_path)
    except KernelLockError as exc:
        log.problem(f"取不到内核锁，拒绝执行：{exc}。内核或另一个应急入口还在运行；停用标记已写，守护不会再拉起内核。")
        return EXIT_FAILED
    try:
        capability = WriteCapability("emergency")
        try:
            adapter = (deps.adapter_factory or _default_adapter)(settings, capability)
        except _StandinTokenError as exc:
            log.problem(str(exc))
            return EXIT_FAILED
        try:
            code = asyncio.run(_session(adapter, capability, deps, yes, log))
        except KeyboardInterrupt:
            log.problem("已中断：已发出的平仓单不会撤回，请用本命令重新运行核对。")
            code = 130
    finally:
        lock.release()
    log.say(MARKER_REMINDER + "。")
    log.say("内核恢复后会按成交记录补记这些平仓（来源：应急入口）。")
    return code
