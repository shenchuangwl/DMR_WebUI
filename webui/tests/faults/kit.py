"""九种故障注入（T0.27）共用的帮手：完整装配的内核（``tests/support/kernel.py``）+ 进程内替身交易所 + 一个按注入时钟走的「调度器」。

- ``Rig``：一次「进程生命」。``Rig.start(tmp_path)`` 用真实的重启恢复（``recovery_factory=None``）启动内核，不起 uvicorn、
  不建 APScheduler（``run_jobs=False``）；``step()`` 把 ManualClock 推进 1 秒，替身跑一轮（``disp.tick()``：数据流推送、条件单触发、
  断线窗口到期），再按生产调度器的间隔（``JOBS``）调到点的 ``kernel.jobs[...]``，最后等一切安静。30/90 秒这类标准都用这个时钟量。
- ``restart()``：模拟进程被杀（``abandon``：不走关停、不撤任何单）后用同一账本与同一替身重新装配、重新恢复。
- ``trail_up``：经生产路径让移动止损上移一次；``watch_gap``：量无保护间隙（移动止损的 ``stop_gap_max_s`` 标准）。
- 判据帮手：``assert_registry_matches_exchange``（持仓登记与交易所一致）、``assert_one_order_per_intent``（同一个交易意图在交易所
  没有第二张订单/条件单）、``system_stop``（交易所上该键的系统全平型止损）、``alerts``（账本里的告警）。

请求时限：内核的适配层缺省 10 秒（真实时间）才判超时。替身的「扣住不答」是真的扣住（真实时间），为了测试不用真等 10 秒，
``FaultStandin`` 给每个内核的传输套一层 ``CappedTransport``：每个请求的时限截到 ``REQUEST_CAP_S``（0.2 秒）。对替身来说正常请求都是
微秒级完成，截短只影响被故障扣住的请求，效果等同于「客户端超时」。
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.crashpoints import CrashRegistry
from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.scheduler import Kernel
from dmr_kernel.core.types import (
    IntentAction,
    IntentSource,
    IntentState,
    Ownership,
    PositionSide,
    PositionStatus,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.ids import OrderOrigin
from tests.support.inproc import AUTH_HEADER, T0_MS, InProcessTransport, RecordingTransport
from tests.support.kernel import Standin, abandon, make_kernel, make_standin_world, settle

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
SYM = "BTCUSDT"
TF, TF_MS = "15m", 900_000
CENTER, HALF = 50_000.0, 500.0  # 每根 K 线真实波幅 1000，ATR = 1000
REQUEST_CAP_S = 0.2

# 生产调度器的间隔（秒，core/scheduler.py 的 _register_jobs_after_recovery 与配置缺省值）；按此顺序在同一秒里调用
JOBS: tuple[tuple[str, int], ...] = (
    ("stream_liveness", 5),
    ("reconcile_positions", 10),  # reconcile_positions_s 缺省 10
    ("reconcile_orders", 60),  # reconcile_orders_s 缺省 60
    ("backfill_trades", 60),
    ("guard_periodic", 60),
    ("resolve_unknown", 5),
    ("expire_intents", 5),
    ("protection_tick", 2),
)

# protect_target_s：保护单管理等一个止损意图出结果的真实秒数（缺省 5）。被故障扣住的意图要靠后面的回查才结束，
# 等满 5 秒真实时间只会拖慢测试；取下限 1 秒（它只影响「挂止损慢」的提示，不影响重试节奏与判定）。
BASE_CONFIG: dict[str, Any] = {"trade_notional_usdt": D("100"), "protect_target_s": 1}


# ---------------------------------------------------------------- 传输：截短请求时限


class CappedTransport:
    """把每个请求、每次建流的时限截到 ``cap_s``（真实秒）。其余原样转交。"""

    def __init__(self, inner: Any, cap_s: float = REQUEST_CAP_S):
        self.inner = inner
        self.cap_s = cap_s

    async def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                      auth: bool = True, timeout_s: float):
        return await self.inner.request(method, path, params=params, body=body, auth=auth,
                                        timeout_s=min(timeout_s, self.cap_s))

    async def ws_connect(self, path: str, *, params: dict, timeout_s: float):
        return await self.inner.ws_connect(path, params=params, timeout_s=min(timeout_s, self.cap_s))

    async def close(self) -> None:
        await self.inner.close()


@dataclass
class FaultStandin(Standin):
    cap_s: float = REQUEST_CAP_S

    def new_transport(self) -> RecordingTransport:
        transport = RecordingTransport(CappedTransport(InProcessTransport(self.disp, token=self.kernel_token),
                                                       self.cap_s))
        self.transports.append(transport)
        return transport


def bar_rows(centers: list[float], start_ms: int, half: float = HALF) -> list[list[Any]]:
    return [[start_ms + i * TF_MS, c, c + half, c - half, c, 1.0, start_ms + (i + 1) * TF_MS - 1]
            for i, c in enumerate(centers)]


def make_fault_standin(*, history: int = 40) -> FaultStandin:
    """替身交易所（BTCUSDT 50000），并给 BTCUSDT 设好 15 分钟 K 线（开仓时刻之前 ``history`` 根收盘的，中心价 50000）。"""
    base = make_standin_world(clock=ManualClock(T0_MS))
    standin = FaultStandin(clock=base.clock, disp=base.disp, engine=base.engine, feed=base.feed,
                           kernel_token=base.kernel_token, manual_token=base.manual_token)
    set_bars(standin, [CENTER] * history, start_ms=T0_MS - history * TF_MS)
    return standin


def set_bars(standin: Standin, centers: list[float], *, start_ms: int, half: float = HALF) -> None:
    standin.feed.set_candles(SYM, TF, bar_rows(centers, start_ms, half))


# ---------------------------------------------------------------- 一次进程生命


_seq = 0


def next_key(prefix: str = "fault") -> str:
    global _seq
    _seq += 1
    return f"{prefix}:{_seq}"


@dataclass
class Rig:
    tmp_path: Path
    kernel: Kernel
    standin: FaultStandin
    crash: CrashRegistry | None = None
    job_errors: list[tuple[int, str, str]] = field(default_factory=list)
    _origin_ms: int = 0

    # ------------------------------------------------------------ 生命周期

    @classmethod
    async def start(cls, tmp_path: Path, *, standin: FaultStandin | None = None, config: dict[str, Any] | None = None,
                    crash: CrashRegistry | None = None, before_start: Callable[["Rig"], None] | None = None) -> "Rig":
        standin = make_fault_standin() if standin is None else standin
        overrides = {**BASE_CONFIG, **(config or {})}
        kernel, _ = make_kernel(tmp_path, standin=standin, crash=crash, recovery_factory=None,
                                config_overrides=overrides)
        rig = cls(tmp_path=tmp_path, kernel=kernel, standin=standin, crash=crash)
        if before_start is not None:
            before_start(rig)
        await kernel.start(serve_http=False, run_jobs=False)
        rig._origin_ms = standin.clock.now_ms()
        await rig.settle()
        await kernel.candles.refresh([SYM], TF)
        return rig

    async def kill(self) -> None:
        """进程被杀：不走关停、不撤任何单（交易所侧什么都不发）。"""
        await abandon(self.kernel)

    async def restart(self, *, config: dict[str, Any] | None = None, crash: CrashRegistry | None = None) -> "Rig":
        """被杀之后用同一账本、同一替身重新装配并跑真实的重启恢复。"""
        return await Rig.start(self.tmp_path, standin=self.standin, config=config, crash=crash)

    async def stop(self) -> None:
        await self.kernel.stop()

    async def settle(self) -> None:
        await settle(self.kernel)

    # ------------------------------------------------------------ 时间

    @property
    def clock(self) -> ManualClock:
        return self.standin.clock

    def now_ms(self) -> int:
        return self.clock.now_ms()

    async def run_jobs(self, *names: str) -> None:
        for name in names:
            await self._job(name)
        await self.settle()

    async def _job(self, name: str) -> None:
        try:
            await self.kernel.jobs[name]()
        except Exception as exc:  # 生产调度器记日志后照常继续；这里记下来，测试按需断言
            self.job_errors.append((self.now_ms(), name, f"{type(exc).__name__}: {exc}"))

    async def step(self, seconds: int = 1) -> None:
        """推进注入时钟 ``seconds`` 秒，每一秒：替身跑一轮，再调到点的作业，等安静。"""
        for _ in range(seconds):
            self.clock.advance(1000)
            self.standin.disp.tick()
            await self.settle()
            elapsed_s = (self.now_ms() - self._origin_ms) // 1000
            for name, every_s in JOBS:
                if elapsed_s % every_s == 0:
                    await self._job(name)
            await self.settle()

    async def run_until(self, predicate: Callable[[], Awaitable[bool] | bool], *, limit_s: int) -> int | None:
        """一秒一秒推进，直到 ``predicate`` 成立；返回从现在起用了多少秒（注入时钟），``limit_s`` 秒内没成立返回 ``None``。"""
        start = self.now_ms()
        while True:
            value = predicate()
            if not isinstance(value, bool):
                value = await value
            if value:
                return (self.now_ms() - start) // 1000
            if self.now_ms() - start >= limit_s * 1000:
                return None
            await self.step()

    # ------------------------------------------------------------ 造仓

    async def open_strategy(self, *, symbol: str = SYM, side: PositionSide = LONG, wait_s: float = 1.0):
        """测试专用入口（source=TEST_ENTRY 的 OPEN 意图）经协调者造一个策略仓；返回意图的记录（可能还没到终态）。"""
        result = await self.kernel.coordinator.submit(IntentSpec(
            action=IntentAction.OPEN, model="m1", source=IntentSource.TEST_ENTRY, symbol=symbol, side=side,
            idem_key=next_key("open"), ttl_s=120, reason="故障注入造仓"))
        record = await self.kernel.coordinator.wait_terminal(result.intent_id, wait_s)
        await self.settle()
        return record

    async def manual(self, path: str, body: dict) -> Any:
        status, payload, _ = await self.standin.disp.handle(
            "POST", path, {}, json.dumps(body).encode(), {AUTH_HEADER: self.standin.manual_token})
        assert status == 200, payload
        return payload

    async def take_over_manual(self, *, qty: str = "0.002", side: PositionSide = LONG):
        """用户在网页上手动开仓（``/dev/manual/order``），持仓对账把它登记为待确认，再「接管」（模型 2 的自动接管还没有，
        直接调登记簿的 ``begin_takeover``，与 tests/integration/test_protection_standin.py 一样）。返回接管后的记录。"""
        await self.manual("/dev/manual/order", {"symbol": SYM, "side": side.open_side().value,
                                                "position_side": side.value, "qty": qty})
        await self.run_jobs("backfill_trades", "reconcile_positions")
        registry = self.kernel.registry
        pending = registry.get_active(SYM, side)
        now = self.now_ms()
        kwargs = dict(symbol=SYM, side=side, qty=D(qty), avg_price=D(str(self.standin.feed.quote(SYM).last)),
                      open_time_ms=now, open_time_estimated=True, anchor_ms=now, signal_exit=False)
        if pending is not None:
            kwargs.update(position_id=pending.id, expected_version=pending.version)
        record = await registry.begin_takeover(**kwargs)
        await self.settle()
        return record

    # ------------------------------------------------------------ 交易所侧的真相

    def exchange_positions(self) -> dict[tuple[str, str], Decimal]:
        return {(p["symbol"], p["position_side"]): D(p["qty"]) for p in self.standin.engine.positions()}

    def open_algos(self, symbol: str = SYM) -> list[dict[str, Any]]:
        return self.standin.engine.open_algo_orders(symbol)

    def system_stops(self, symbol: str = SYM, side: PositionSide = LONG) -> list[dict[str, Any]]:
        return [a for a in self.open_algos(symbol)
                if a["position_side"] == side.value and a["type"] == "STOP_MARKET" and a["close_position"]
                and ids.is_system_id(a["client_algo_id"])]

    def system_stop(self, symbol: str = SYM, side: PositionSide = LONG) -> dict[str, Any] | None:
        stops = self.system_stops(symbol, side)
        assert len(stops) <= 1, f"同一个键上有 {len(stops)} 张系统止损：{stops}"
        return stops[0] if stops else None

    def has_stop(self, symbol: str = SYM, side: PositionSide = LONG) -> bool:
        return self.system_stop(symbol, side) is not None

    def position(self, symbol: str = SYM, side: PositionSide = LONG):
        return self.kernel.registry.get_active(symbol, side)

    def suppressions(self) -> frozenset:
        return self.kernel.modelstate.current().suppressions

    async def alerts(self, kind: str | None = None) -> list:
        await self.kernel.notifier.flush()
        rows = await self.kernel.notifier.recent(500)
        return [a for a in rows if kind is None or a.kind == kind]

    def exchange_orders(self) -> list[dict[str, Any]]:
        return list(self.standin.engine.to_snapshot()["orders"])

    def exchange_algos(self) -> list[dict[str, Any]]:
        return list(self.standin.engine.to_snapshot()["algos"])

    # ------------------------------------------------------------ 4.11 的通用标准

    def assert_registry_matches_exchange(self) -> None:
        """持仓登记与交易所一致：交易所上每个非零持仓都登记着（数量相同），登记簿里托管中/接管中/平仓中的仓在交易所都有同量持仓。"""
        exchange = self.exchange_positions()
        registered: dict[tuple[str, str], Decimal] = {}
        for rec in self.kernel.registry.registered():
            if rec.status in (PositionStatus.CLOSED, PositionStatus.IGNORED):
                continue
            registered[(rec.symbol, rec.side.value)] = rec.qty
        assert registered == exchange, f"登记簿 {registered} 与交易所 {exchange} 不一致"

    def assert_one_order_per_intent(self) -> None:
        """同一个交易意图在交易所没有第二张订单（市价单、条件单分开数；按客户编号里的意图号归类）。"""
        for label, rows, key in (("订单", self.exchange_orders(), "client_order_id"),
                                 ("条件单", self.exchange_algos(), "client_algo_id")):
            per_intent: Counter[int] = Counter()
            for row in rows:
                parsed = ids.parse_client_id(row.get(key))
                if parsed.origin is OrderOrigin.SYSTEM and parsed.intent_id is not None:
                    per_intent[parsed.intent_id] += 1
            doubled = {iid: n for iid, n in per_intent.items() if n > 1}
            assert not doubled, f"这些意图在交易所有不止一张{label}：{doubled}"

    def assert_general(self) -> None:
        self.assert_registry_matches_exchange()
        self.assert_one_order_per_intent()


def fill_time_ms(rig: Rig, intent_id: int) -> int:
    """交易所上这个意图的开仓单最后一次更新（成交）的时刻。"""
    times = [int(o["update_ms"]) for o in rig.exchange_orders()
             if ids.parse_client_id(o["client_order_id"]).intent_id == intent_id and D(str(o["filled"])) > 0]
    assert times, f"交易所上没有意图 {intent_id} 的成交订单"
    return max(times)


# ---------------------------------------------------------------- 移动止损与无保护间隙

STOP_GAP_MAX_S = 30  # stop_gap_max_s 缺省（core/configdefs.py）
TRAIL_PRICE = "53500"


async def trail_up(rig: Rig) -> None:
    """让移动止损上移一次（生产路径：``bar_close_short`` → ``CandlesReady`` → 保护单管理 ``trailing``）。

    前提：仓是在 ``T0_MS`` 那根 15 分钟 K 线里开的（``Rig.start`` 后不推进太久就开仓）。在那根之后放 6 根逐根走高的已收盘 K 线
    （中心价 51000…53500，ATR 1000：新止损约 49500，比初始的 48500 更有利），现价拉到 ``TRAIL_PRICE``，注入时钟直接推到第 6 根
    收盘之后（加上 K 线延迟），调一次 ``bar_close_short``。返回时更新（缺省 gap 模式：先撤旧、后挂新）已经提交并跑完一轮。"""
    rising = [CENTER + 1000 + 500 * i for i in range(6)]
    set_bars(rig.standin, [CENTER] * 41 + rising, start_ms=T0_MS - 40 * TF_MS)
    target = T0_MS + 7 * TF_MS + 10_000
    assert rig.now_ms() < target, "开仓太晚，trail_up 的 K 线布置对不上"
    rig.clock.advance(target - rig.now_ms())
    await rig.manual("/dev/price_override", {"symbol": SYM, "price": TRAIL_PRICE})
    await rig.step(1)
    await rig.run_jobs("bar_close_short")


async def watch_gap(rig: Rig, seconds: int, side: PositionSide = LONG) -> tuple[int, int]:
    """从现在起一秒一秒推进 ``seconds`` 秒，量这一侧没有系统止损的最长连续时长（注入时钟，1 秒分辨率）。
    现在这一刻就没有止损时从现在算起。返回 ``(最长间隙秒数, 结束时是否有止损 1/0)``。"""
    longest = 0
    missing_since: int | None = None if rig.has_stop(side=side) else rig.now_ms()
    for _ in range(seconds):
        await rig.step()
        if rig.has_stop(side=side):
            if missing_since is not None:
                longest = max(longest, (rig.now_ms() - missing_since) // 1000)
                missing_since = None
        elif missing_since is None:
            missing_since = rig.now_ms() - 1000  # 上一秒还在，这一秒不在：最多早 1 秒就没了
    if missing_since is not None:
        longest = max(longest, (rig.now_ms() - missing_since) // 1000)
    return longest, int(rig.has_stop(side=side))


TERMINAL = (IntentState.COMPLETED, IntentState.REJECTED, IntentState.EXPIRED, IntentState.CANCELLED)
__all__ = [
    "BASE_CONFIG", "CENTER", "D", "FaultStandin", "JOBS", "LONG", "Ownership", "Rig", "SHORT", "STOP_GAP_MAX_S", "SYM",
    "TERMINAL", "TF", "TF_MS", "TRAIL_PRICE", "bar_rows", "fill_time_ms", "make_fault_standin", "next_key", "set_bars",
    "trail_up", "watch_gap",
]
