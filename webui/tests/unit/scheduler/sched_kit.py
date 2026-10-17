"""任务装配（core.scheduler）测试的公共帮手：给协程方法套记录器、造持仓与意图、读队列里的事件。

装配本身用 ``tests/support/kernel.py``（进程内替身交易所 + 完整的 Kernel + 假恢复）。
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from dmr_kernel.core.events import Event
from dmr_kernel.core.types import IntentAction, IntentSource, Ownership, PositionSide
from dmr_kernel.core.intents import IntentSpec
from dmr_kernel.core.scheduler import Kernel

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT

# 规格 3.2 的 17 个作业名，加上 M2 的一个（close_all_tick：一键全平暂停持仓的到期恢复）
JOB_NAMES = (
    "stream_liveness", "resolve_unknown", "expire_intents", "reconcile_positions", "reconcile_orders",
    "backfill_trades", "backfill_income", "guard_periodic", "bar_close_short", "bar_close_long", "server_time",
    "rules_refresh", "heartbeat", "seat_expiry", "protection_tick", "ledger_backup", "ledger_checkpoint",
    "close_all_tick",
)


def spy_async(obj: Any, name: str, *, order: list[str] | None = None, label: str | None = None,
              before: Callable[[], None] | None = None) -> list[tuple[tuple, dict]]:
    """把 ``obj.name``（协程方法）换成记录调用的包装，调用照常转给原方法。返回调用记录 ``[(args, kwargs), ...]``。
    ``order`` 给了就在调用开始时把 ``label`` 追加进去（看调用顺序）；``before`` 在调用开始时执行（抓当时的状态）。"""
    original = getattr(obj, name)
    calls: list[tuple[tuple, dict]] = []

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        if order is not None:
            order.append(label or name)
        if before is not None:
            before()
        return await original(*args, **kwargs)

    setattr(obj, name, wrapper)
    return calls


def collect(kernel: Kernel, *types: type) -> list[Event]:
    """订阅总线上的事件（缺省全部）并按发布顺序记下来；总线启动后才会有内容，用 ``settle`` 排空。"""
    events: list[Event] = []
    kernel.bus.subscribe(types or None, events.append, name="test-collector")
    return events


async def register_position(kernel: Kernel, symbol: str = "BTCUSDT", side: PositionSide = LONG,
                            qty: Decimal = D("0.01"), price: Decimal = D("50000")):
    """在登记簿里登记一个托管中的策略仓（不碰交易所）。"""
    intent, _ = await kernel.intents.create(IntentSpec(
        action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol=symbol, side=side,
        idem_key=f"test:open:{symbol}:{side.value}", params={"notional": "100"}))
    return await kernel.registry.register_open(
        symbol=symbol, side=side, qty=qty, avg_price=price, open_time_ms=kernel.clock.now_ms(),
        ownership=Ownership.STRATEGY, model="m1", intent_id=intent.id)


async def create_intent(kernel: Kernel, symbol: str, *, ttl_s: int | None = None, key: str | None = None):
    record, _ = await kernel.intents.create(IntentSpec(
        action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL, symbol=symbol, side=LONG,
        idem_key=key or f"test:intent:{symbol}", params={"notional": "100"}, ttl_s=ttl_s))
    return record
