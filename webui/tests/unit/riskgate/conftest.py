"""风控闸测试的公共夹具：临时账本、手动时钟、事件总线、可改的配置字典、记录告警的假通知器、账本造数据。

约定：
- 时间用显式毫秒：快照的 ``at_ms`` 与 ``evaluate`` 的 ``now_ms`` 由用例给出；``utc(...)`` 夹具把 UTC 日期时间换成毫秒。
- 事件总线在用例里用 ``await env.flush()`` 启动并等待处理完（总线没启动时事件只排队）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus, RiskStateChanged
from dmr_kernel.core.riskgate import RiskGate
from dmr_kernel.core.types import AlertLevel, Ownership, PositionSide, PositionStatus
from dmr_kernel.ledger import models
from dmr_kernel.ledger.writer import Ledger
from tests.support.ledger import temp_ledger

D = Decimal

DEFAULT_CONFIG: dict[str, Any] = {
    "reconcile_positions_s": 10,
    "daily_loss_limit": D("50"),
    "max_drawdown": D("10"),
    "daily_trade_limit": 50,
}


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0, ms: int = 0) -> int:
    moment = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(moment.timestamp()) * 1000 + ms


T0 = _utc(2025, 10, 9, 12)


class Alert(NamedTuple):
    level: AlertLevel
    kind: str
    message: str
    dedupe_key: str | None


class FakeNotifier:
    """与 alerts.notifier.Notifier 同形的记录器：只记下 raise_alert 的参数。"""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def raise_alert(self, level: AlertLevel, kind: str, message: str, *, dedupe_key: str | None = None) -> None:
        self.alerts.append(Alert(level, kind, message, dedupe_key))

    def kinds(self) -> list[str]:
        return [a.kind for a in self.alerts]


class Seeder:
    """直接往账本里写 position / trade 行（风控闸只读它们，不经登记簿）。"""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self._n = 0

    async def trade(self, *, close_ms: int, net_pnl: str | Decimal, ownership: Ownership = Ownership.STRATEGY,
                    model: str | None = "m1") -> None:
        self._n += 1
        n = self._n

        def write(session: Any) -> None:
            session.add(models.Trade(
                position_id=10_000 + n, symbol="BTCUSDT", side=PositionSide.LONG, ownership=ownership, model=model,
                close_ms=close_ms, max_qty=D("0.01"), gross_pnl=D(net_pnl), fees=D("0"), funding=D("0"),
                net_pnl=D(net_pnl), exit_reason="stop"))
            session.flush()

        await self.ledger.write(write)

    async def position(self, *, open_ms: int | None, ownership: Ownership = Ownership.STRATEGY,
                       status: PositionStatus = PositionStatus.CLOSED, created_ms: int | None = None,
                       symbol: str = "BTCUSDT", side: PositionSide = PositionSide.LONG) -> None:
        self._n += 1
        n = self._n

        def write(session: Any) -> None:
            session.add(models.Position(
                symbol=symbol, side=side, generation=n, status=status, ownership=ownership, qty=D("0.01"),
                avg_price=D("60000"), max_qty=D("0.01"), open_time_ms=open_ms,
                created_ms=created_ms if created_ms is not None else (open_ms or 0),
                updated_ms=created_ms if created_ms is not None else (open_ms or 0)))
            session.flush()

        await self.ledger.write(write)

    async def strategy_opens(self, count: int, *, at_ms: int, ownership: Ownership = Ownership.STRATEGY) -> None:
        """已平仓的仓 count 个（默认策略仓），开仓时间都在 at_ms；一次事务写完（同键多条已平记录不违反部分唯一索引）。"""
        self._n += 1
        base = self._n * 1000

        def write(session: Any) -> None:
            for i in range(count):
                session.add(models.Position(
                    symbol="BTCUSDT", side=PositionSide.LONG, generation=base + i, status=PositionStatus.CLOSED,
                    ownership=ownership, qty=D("0.01"), avg_price=D("60000"), max_qty=D("0.01"), open_time_ms=at_ms,
                    created_ms=at_ms, updated_ms=at_ms))
            session.flush()

        await self.ledger.write(write)


@dataclass
class Env:
    tmp_path: Path
    clock: ManualClock
    ledger: Ledger
    bus: EventBus
    config: dict[str, Any]
    notifier: FakeNotifier
    events: list[RiskStateChanged]
    seed: Seeder

    def new_gate(self, *, ledger: Ledger | None = None, notifier: bool = True) -> RiskGate:
        return RiskGate(ledger or self.ledger, self.bus, self.clock, config_get=self.config.__getitem__,
                        notifier=self.notifier if notifier else None)

    async def loaded_gate(self, **kwargs: Any) -> RiskGate:
        gate = self.new_gate(**kwargs)
        await gate.load()
        return gate

    async def fresh_gate(self, at_ms: int, equity: str = "1000", **kwargs: Any) -> RiskGate:
        """加载好的闸，并在 at_ms 收到第一个读数（日初权益与峰值都是它）。"""
        gate = await self.loaded_gate(**kwargs)
        await gate.on_account_snapshot(D(equity), at_ms)
        return gate

    async def flush(self) -> list[RiskStateChanged]:
        await self.bus.start()
        await self.bus.drain()
        return list(self.events)


@pytest.fixture
def utc():
    return _utc


@pytest.fixture
def env(tmp_path):
    clock = ManualClock(T0)
    bus = EventBus()
    events: list[RiskStateChanged] = []
    bus.subscribe(RiskStateChanged, events.append, name="test-events")
    with temp_ledger(tmp_path, clock) as ledger:
        yield Env(tmp_path, clock, ledger, bus, dict(DEFAULT_CONFIG), FakeNotifier(), events, Seeder(ledger))


@pytest.fixture
def local_tz_plus7():
    """把本进程的本地时区改成 UTC+7（POSIX 写法 ICT-7，不依赖时区数据库），验证风控闸不看本机时区。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "ICT-7"
    time.tzset()
    try:
        assert time.localtime(0).tm_hour == 7  # 夹具自检：时区真的变了，否则下面的用例是空转
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
