"""M2B（一键全平、模型切换）单元测试的公共帮手：假协调者、假登记簿、假意图与订单服务，加真实的模型状态、操作记录、账本与总线。

假世界只替换「与交易所有关」的部分，状态机（模型状态、操作记录）全是真的：
- ``FakeCoordinator``：``submit`` 记下平仓意图；每个「合约 + 方向」按剧本（``script``）决定平仓意图怎么收场——
  ``ok`` 成交且归零、``external`` 意图完成但原因是「已由其它途径平掉」（持仓本来就没了）、``reject`` 被拒、
  ``residual`` 意图完成但仍有仓、``stuck`` 永远到不了终态；``latency_ms`` 是模拟（ManualClock）的耗时。剧本可以是列表（第几次提交用第几项）。
- ``drive``：真实时间里每过一小段就把 ManualClock 推进一步，直到被测任务结束；这样「120 秒内出结果」是模拟时间，测试本身很快。
"""
from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.crashpoints import CrashRegistry
from dmr_kernel.core.types import (
    IntentAction,
    IntentState,
    Known,
    Ownership,
    PositionSide,
    PositionStatus,
    ProtectionState,
    Unknown,
)
from tests.unit.api.api_kit import position_record

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
TERMINAL = IntentState.terminal()
POLL_S = 0.002


@dataclass
class Behavior:
    kind: str = "ok"  # ok | external | reject | residual | stuck
    latency_ms: int = 0
    reason: str = ""


@dataclass
class FakeIntent:
    id: int
    state: IntentState
    state_reason: str = ""
    symbol: str = ""
    side: PositionSide = LONG
    action: IntentAction = IntentAction.CLOSE


class FakeCoordinator:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.exchange: dict[tuple[str, PositionSide], Decimal] = {}
        self.script: dict[tuple[str, PositionSide], Any] = {}
        self.submitted: list[Any] = []
        self.busy: set[tuple[str, PositionSide]] = set()
        self.unreadable: set[tuple[str, PositionSide]] = set()
        self.unreadable_counts: dict[tuple[str, PositionSide], int] = {}  # 前 n 次读数失败，之后正常
        self.cancel_calls: list[dict[str, Any]] = []
        self._jobs: dict[int, dict[str, Any]] = {}
        self._attempts: dict[tuple[str, PositionSide], int] = {}
        self._next = 1

    # ------------------------------------------------------------ 测试用的布置

    def add_position(self, symbol: str, side: PositionSide, qty: str = "1") -> None:
        self.exchange[(symbol, side)] = D(qty)

    def behave(self, symbol: str, side: PositionSide, *items: Behavior | str) -> None:
        self.script[(symbol, side)] = [Behavior(i) if isinstance(i, str) else i for i in items]

    def submits_for(self, symbol: str, side: PositionSide) -> list[Any]:
        return [spec for spec in self.submitted if (spec.symbol, spec.side) == (symbol, side)]

    # ------------------------------------------------------------ 协调者的接口

    def key_busy(self, symbol: str, side: PositionSide) -> bool:
        return (symbol, side) in self.busy

    async def submit(self, spec: Any):
        key = (spec.symbol, spec.side)
        n = self._attempts.get(key, 0)
        self._attempts[key] = n + 1
        items = self.script.get(key) or [Behavior("ok")]
        behavior = items[min(n, len(items) - 1)]
        intent_id = self._next
        self._next += 1
        self.submitted.append(spec)
        self._jobs[intent_id] = {"spec": spec, "behavior": behavior, "at": self.clock.now_ms(), "done": None}

        @dataclass(frozen=True)
        class Submitted:
            intent_id: int
            created: bool
            state: IntentState

        return Submitted(intent_id, True, IntentState.CREATED)

    async def wait_terminal(self, intent_id: int, timeout_s: float):
        job = self._jobs[intent_id]
        spec, behavior = job["spec"], job["behavior"]
        key = (spec.symbol, spec.side)
        if job["done"] is None and behavior.kind != "stuck" and self.clock.now_ms() >= job["at"] + behavior.latency_ms:
            if behavior.kind == "ok":
                self.exchange[key] = D(0)
                job["done"] = (IntentState.COMPLETED, behavior.reason)
            elif behavior.kind == "external":
                self.exchange[key] = D(0)
                job["done"] = (IntentState.COMPLETED, behavior.reason or "已由其它途径平掉：即时持仓为零，未发出平仓单")
            elif behavior.kind == "reject":
                job["done"] = (IntentState.REJECTED, behavior.reason or "交易所拒绝平仓单")
            elif behavior.kind == "residual":
                job["done"] = (IntentState.COMPLETED, behavior.reason)
            else:  # pragma: no cover
                raise AssertionError(behavior.kind)
        if job["done"] is None:
            await asyncio.sleep(min(timeout_s, POLL_S))
            return FakeIntent(intent_id, IntentState.RECONCILING, "", spec.symbol, spec.side)
        state, reason = job["done"]
        return FakeIntent(intent_id, state, reason, spec.symbol, spec.side)

    async def live_position(self, symbol: str, side: PositionSide):
        left = self.unreadable_counts.get((symbol, side), 0)
        if (symbol, side) in self.unreadable or left > 0:
            self.unreadable_counts[(symbol, side)] = max(0, left - 1)
            return Unknown("读数失败（测试）")
        return Known(self.exchange.get((symbol, side), D(0)))

    async def cancel_unfilled_opens(self, *, model, reason):
        self.cancel_calls.append({"model": model, "reason": reason})
        return 0


class FakeIntents:
    """``non_terminal(action=OPEN)``：测试往 ``opens`` 里放没到终态的开仓意图。"""

    def __init__(self) -> None:
        self.opens: list[FakeIntent] = []

    async def non_terminal(self, *, symbol=None, side=None, action=None, model=None):
        return [i for i in self.opens if i.state not in TERMINAL and (action is None or i.action is action)]


class FakeOrders:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def non_terminal(self, **_: Any):
        return list(self.rows)


@dataclass
class FakeOrder:
    client_order_id: str
    state: Any
    symbol: str = "BTCUSDT"
    position_side: PositionSide = LONG
    purpose: Any = None
    qty: Decimal = D("1")


class FakeRegistry:
    def __init__(self) -> None:
        self.records: dict[int, Any] = {}
        self.closing_calls: list[tuple[int, bool, str]] = []
        self.exit_reasons: dict[int, Any] = {}  # fetch 返回的记录带上的出场原因
        self._next = 1

    def add(self, symbol: str, side: PositionSide = LONG, *, status=PositionStatus.MANAGED,
            ownership=Ownership.STRATEGY, is_mixed: bool = False, protection=ProtectionState.SYSTEM,
            qty: str = "1") -> Any:
        rec = position_record(id=self._next, symbol=symbol, side=side, status=status, ownership=ownership,
                              is_mixed=is_mixed, protection_state=protection, qty=D(qty))
        self._next += 1
        self.records[rec.id] = rec
        return rec

    def all_active(self):
        return sorted((r for r in self.records.values() if r.status is not PositionStatus.CLOSED),
                      key=lambda r: r.id)

    def registered(self):
        return [r for r in self.all_active() if r.status in PositionStatus.registered()]

    def by_id(self, position_id: int):
        return self.records.get(position_id)

    def get_active(self, symbol: str, side: PositionSide):
        return next((r for r in self.all_active() if (r.symbol, r.side) == (symbol, side)), None)

    async def fetch(self, position_id: int):
        rec = self.records.get(position_id)
        if rec is not None and position_id in self.exit_reasons:
            rec = dataclasses.replace(rec, exit_reason=self.exit_reasons[position_id])
        return rec

    async def set_closing(self, position_id: int, closing: bool, *, cause: str):
        self.closing_calls.append((position_id, closing, cause))
        rec = self.records[position_id]
        status = PositionStatus.CLOSING if closing else PositionStatus.MANAGED
        self.records[position_id] = dataclasses.replace(rec, status=status)
        return self.records[position_id]

    def mark_flat(self, position_id: int, exit_reason=None) -> None:
        self.records[position_id] = dataclasses.replace(self.records[position_id], status=PositionStatus.CLOSED,
                                                        exit_reason=exit_reason, qty=D(0))


@dataclass
class World:
    clock: ManualClock
    bus: Any
    ledger: Any
    modelstate: Any
    operations: Any
    coordinator: FakeCoordinator
    registry: FakeRegistry
    intents: FakeIntents
    orders: FakeOrders
    crash: CrashRegistry
    config: dict[str, Any]
    close_all: Any = None
    switch: Any = None
    deps: Any = None
    events: list = field(default_factory=list)

    def add_position(self, symbol: str, side: PositionSide = LONG, *, qty: str = "1", **kw: Any) -> Any:
        """登记簿里一条、替身交易所里同样数量的一条。"""
        rec = self.registry.add(symbol, side, qty=qty, **kw)
        self.coordinator.add_position(symbol, side, qty)
        return rec

    def sent_for(self, symbol: str, side: PositionSide = LONG) -> list[Any]:
        return self.coordinator.submits_for(symbol, side)


DEFAULT_CONFIG: dict[str, Any] = {
    "close_one_timeout_s": 15, "close_all_timeout_s": 120, "close_all_wait_open_s": 10,
    "partial_close_pause_s": 30, "switch_settle_s": 10, "switch_preview_ttl_s": 60, "reauth_window_s": 300,
    "seat_lease_s": 60,
}


async def drive(clock: ManualClock, awaitable: Awaitable[Any], *, step_ms: int = 1000, max_steps: int = 2000) -> Any:
    """把 awaitable 当任务跑，同时每隔一小段真实时间把 ManualClock 推进 ``step_ms``，直到任务结束。"""
    task = asyncio.ensure_future(awaitable)
    for _ in range(max_steps):
        done, _ = await asyncio.wait({task}, timeout=0.004)
        if done:
            return task.result()
        clock.advance(step_ms)
    task.cancel()
    raise AssertionError(f"被测任务在 {max_steps} 步（每步 {step_ms} 毫秒）内没有结束")


async def settle_tasks(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def caller(w: "World", *, operator: str = "alice", session_id: int | None = 7, seat: bool = True,
           checked: bool = True):
    """发起操作的主体：会话（默认持有席位、刚校验过口令）或本机令牌（``session_id=None``）。"""
    from dmr_kernel.ops.common import Caller

    if session_id is None:
        return Caller.cli()
    return Caller(operator=operator, session_id=session_id, is_session=True, holds_seat=seat,
                  reauth_ms=w.clock.now_ms() if checked else None)


async def build_world(ledger, clock: ManualClock, bus, **config: Any) -> World:
    """真实的模型状态与操作记录 + 假协调者等；``config`` 覆盖默认配置。关闭时调用方不用管（账本由夹具关）。"""
    from dmr_kernel.core.modelstate import ModelStateService
    from dmr_kernel.ops.close_all import CloseAll
    from dmr_kernel.ops.common import OpsDeps
    from dmr_kernel.ops.operations import Operations
    from dmr_kernel.ops.switch import Switch

    cfg = {**DEFAULT_CONFIG, **config}
    modelstate = ModelStateService(ledger, bus, clock)
    await modelstate.load()
    operations = Operations(ledger, clock, bus)
    world = World(clock=clock, bus=bus, ledger=ledger, modelstate=modelstate, operations=operations,
                  coordinator=FakeCoordinator(clock), registry=FakeRegistry(), intents=FakeIntents(),
                  orders=FakeOrders(), crash=CrashRegistry(live=False), config=cfg)
    bus.subscribe(None, world.events.append, name="m2b-world-events")
    deps = OpsDeps(clock=clock, modelstate=modelstate, registry=world.registry, operations=operations,
                   config_get=cfg.__getitem__, models={}, coordinator=world.coordinator, adapter=None, rules=None,
                   orders=world.orders)
    world.deps = deps
    world.close_all = CloseAll(deps, intents=world.intents, bus=bus, crash=world.crash, poll_s=POLL_S)
    world.switch = Switch(deps, intents=world.intents, close_all=world.close_all, bus=bus, crash=world.crash,
                          poll_s=POLL_S)
    return world
