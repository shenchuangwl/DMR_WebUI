"""``emergency close`` 测试的帮手：假的交易所（内存里的持仓与条件单，市价单真的会减仓）、假的进程表与时钟。"""
from __future__ import annotations

import signal
from dataclasses import dataclass, field
from decimal import Decimal
from io import StringIO
from pathlib import Path

from cli_kit import Result, Rig
from dmr_kernel.cli.emergency import EmergencyDeps
from dmr_kernel.cli.main import main
from dmr_kernel.core.types import (
    AlgoOrderInfo, AlgoType, OrderSide, PositionInfo, PositionSide, WorkingType, WriteAccepted, WriteRejected,
    RejectKind, CancelOutcome, CancelResult, Unknown,
)
from support.fake_adapter import FakeAdapter

KERNEL_PID = 4242


def pos(symbol: str, side: str, qty: str = "1") -> PositionInfo:
    return PositionInfo(symbol=symbol, side=PositionSide(side), qty=Decimal(qty), entry_price=Decimal("100"),
                        mark_price=Decimal("101"))


def algo(symbol: str, side: str, cid: str | None, algo_id: str) -> AlgoOrderInfo:
    close_side = OrderSide.SELL if side == "LONG" else OrderSide.BUY
    return AlgoOrderInfo(symbol=symbol, client_algo_id=cid, algo_id=algo_id, side=close_side,
                         position_side=PositionSide(side), algo_type=AlgoType.STOP_MARKET,
                         trigger_price=Decimal("90"), working_type=WorkingType.MARK_PRICE, close_position=True, qty=None,
                         raw_status="NEW", actual_order_id=None, create_time_ms=1, update_time_ms=1)


class FakeExchange(FakeAdapter):
    """持仓与条件单是活的：市价单按成交减仓，撤单真的撤掉。"""

    def __init__(self, positions: list[PositionInfo], algos: list[AlgoOrderInfo] | None = None, *, capability=None,
                 fills: bool = True) -> None:
        super().__init__(capability=capability)
        self.positions = list(positions)
        self.algos = list(algos or [])
        self.fills = fills
        self.reject_market = False
        self.positions_unknown = False
        self.events: list[str] = []
        self.set_read("fetch_positions", self._positions)
        self.set_read("fetch_open_algo_orders", self._algos)
        self.set_read("fetch_open_orders", lambda symbol=None: [])

    def _positions(self):
        self.events.append("read_positions")
        return Unknown("交易所读不到") if self.positions_unknown else list(self.positions)

    def _algos(self, symbol=None):
        return [a for a in self.algos if symbol is None or a.symbol == symbol]

    async def _create_market_order(self, **kw):
        self.writes.append(("create_market_order", kw))
        self.events.append(f"market:{kw['symbol']}:{kw['position_side'].value}")
        if self.reject_market:
            return WriteRejected(RejectKind.REDUCE_REJECTED, -2022, "无仓可平")
        if self.fills:
            self.positions = [p for p in self.positions
                              if not (p.symbol == kw["symbol"] and p.side == kw["position_side"])]
        return WriteAccepted()

    async def _cancel_algo_order(self, **kw):
        self.writes.append(("cancel_algo_order", kw))
        self.events.append(f"cancel_algo:{kw['client_algo_id']}")
        self.algos = [a for a in self.algos if a.client_algo_id != kw["client_algo_id"]]
        return CancelResult(CancelOutcome.CANCELLED)


class World:
    """进程表 + 时钟。kernel 是内核进程：SIGTERM 能让它退出（term_works）、SIGKILL 一定能（kill_works）。"""

    def __init__(self, *, kernel_alive: bool = True, term_works: bool = True, kill_works: bool = True,
                 cmdline: str | None = "/venv/bin/python -m dmr_kernel") -> None:
        self.alive = {KERNEL_PID} if kernel_alive else set()
        self.term_works = term_works
        self.kill_works = kill_works
        self.cmdline = cmdline
        self.signals: list[tuple[int, int]] = []
        self.on_signal = None
        self.now = 0.0
        self.sleeps: list[float] = []

    def send_signal(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        if self.on_signal:
            self.on_signal(pid, sig)
        if sig == signal.SIGTERM and self.term_works:
            self.alive.discard(pid)
        if sig == signal.SIGKILL and self.kill_works:
            self.alive.discard(pid)

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive

    def cmdline_of(self, pid: int) -> str | None:
        return self.cmdline if pid in self.alive else None

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


@dataclass
class Emergency:
    rig: Rig
    exchange: FakeExchange
    world: World
    capabilities: list = field(default_factory=list)
    factory_calls: int = 0
    answers: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    clock_ms: int = 1_760_000_000_000

    @property
    def var(self) -> Path:
        return self.rig.var

    @property
    def nosupervise(self) -> Path:
        return self.var / "run" / "kernel.nosupervise"

    @property
    def pid_file(self) -> Path:
        return self.var / "run" / "kernel.pid"

    @property
    def log_file(self) -> Path:
        return self.var / "log" / "emergency.log"

    def write_pid(self, text: str | None = None) -> None:
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text((text if text is not None else str(KERNEL_PID)) + "\n")

    def factory(self, settings, capability):
        self.factory_calls += 1
        self.capabilities.append(capability)
        self.exchange._capability = capability
        return self.exchange

    def ask(self, question: str) -> str:
        self.asked.append(question)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def run(self, argv: list[str], *, env: dict[str, str] | None = None) -> Result:
        deps = EmergencyDeps(adapter_factory=self.factory, send_signal=self.world.send_signal,
                             pid_alive=self.world.pid_alive, cmdline_of=self.world.cmdline_of,
                             sleep=self.world.sleep, monotonic=self.world.monotonic, now_ms=lambda: self.clock_ms,
                             ask=self.ask)
        out, err = StringIO(), StringIO()
        code = main(["emergency", "close", *argv], env={**self.rig.env, **(env or {})}, stdout=out, stderr=err,
                    emergency_deps=deps)
        return Result(code, out.getvalue(), err.getvalue())
