"""Guard 测试的装配与小工具：假通知器、事件构造、装配好的 Env（真实的模型状态 + 临时账本 + 可编程适配层）。

非实盘的 Env 里旧入口互斥指向一个**不存在**的目录：Guard 若碰了它（创建或打开），夹具收尾时会报错。
实盘的 Env 用 tmp_path 下模拟的 DMR_WebUI/data/runtime_state（路径由 ``settings.dmr_root`` 推出，与装配一致）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import (
    Event, EventBus, FillIngested, ReconcileCompleted, RiskStateChanged, StreamStatusChanged,
)
from dmr_kernel.core.guard import Guard, LegacyMutex, legacy_runtime_dir
from dmr_kernel.core.modelstate import ModelStateService, ModelStateSnapshot
from dmr_kernel.core.types import AlertLevel, OrderOrigin, OrderSide, PositionSide, RestrictionInfo, TradeInfo
from dmr_kernel.ledger.writer import Ledger
from dmr_kernel.settings import Settings, load_settings
from lock_kit import make_runtime_dir
from tests.support.fake_adapter import FakeAdapter
from tests.support.ledger import START_MS

LEGACY_CLIENT_ID = "DMR12_SHORT_BUY_LONG_1760000000000"


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


# ---------------------------------------------------------------- 事件


def stream_event(state: str, reason: str = "", at_ms: int = START_MS) -> StreamStatusChanged:
    return StreamStatusChanged(state=state, reason=reason, at_ms=at_ms)


def reconciled(kind: str = "positions", status: str = "ok", at_ms: int = START_MS) -> ReconcileCompleted:
    return ReconcileCompleted(kind=kind, status=status, at_ms=at_ms)


def risk_event(*, account: bool = False, model: bool = False, reasons: tuple[str, ...] = (),
               at_ms: int = START_MS) -> RiskStateChanged:
    return RiskStateChanged(account_tripped=account, model_tripped=model, reasons=reasons, at_ms=at_ms)


def fill_event(origin: OrderOrigin = OrderOrigin.LEGACY, *, client_order_id: str | None = LEGACY_CLIENT_ID,
               symbol: str = "BTCUSDT", fill_id: int = 1) -> FillIngested:
    trade = TradeInfo(trade_id=f"t{fill_id}", exchange_order_id=f"o{fill_id}", symbol=symbol, side=OrderSide.BUY,
                      position_side=PositionSide.LONG, qty=Decimal("0.01"), price=Decimal("60000"),
                      commission=Decimal("0.24"), commission_asset="USDT", realized_pnl=Decimal("0"),
                      time_ms=START_MS)
    return FillIngested(fill_id=fill_id, trade=trade, client_order_id=client_order_id, origin=origin, intent_id=None,
                        order_purpose=None, path="stream", at_ms=START_MS)


# ---------------------------------------------------------------- 装配


@dataclass
class Env:
    tmp_path: Path
    clock: ManualClock
    ledger: Ledger
    bus: EventBus
    settings: Settings
    runtime_dir: Path
    adapter: FakeAdapter
    notifier: FakeNotifier
    config: dict[str, Any]
    modelstate: ModelStateService
    legacy: LegacyMutex
    guard: Guard
    expect_untouched: bool  # 旧入口互斥不该碰 runtime_dir（夹具收尾时核对它的父目录仍不存在）

    async def boot(self, *, subscribe: bool = True) -> "Env":
        """加载模型状态；订阅事件（默认）；启动事件总线。"""
        await self.modelstate.load()
        if subscribe:
            await self.guard.start()
        await self.bus.start()
        return self

    async def emit(self, *events: Event) -> None:
        """按顺序发布事件并等总线处理完。"""
        for event in events:
            self.bus.publish(event)
        await self.bus.drain()

    @property
    def state(self) -> ModelStateSnapshot:
        return self.modelstate.current()

    def alerts(self, kind: str | None = None) -> list[Alert]:
        return [a for a in self.notifier.alerts if kind is None or a.kind == kind]


def build_env(tmp_path: Path, clock: ManualClock, ledger: Ledger, bus: EventBus, *, environment: str = "standin",
              mutex_enabled: bool | None = None, runtime: Path | None = None, create_runtime: bool = True,
              marker: bool = True, config: dict[str, Any] | None = None, adapter: FakeAdapter | None = None,
              risk_status: Callable[[], Mapping[str, Any]] | None = None) -> Env:
    """``environment``：standin | testnet | mainnet_shadow | mainnet_live。

    ``mutex_enabled`` 缺省按 ``settings.legacy_mutex_enabled``；``runtime`` 指定互斥用的目录（缺省：实盘是模拟的
    data/runtime_state，其余是一个不存在的目录）。``risk_status`` 是传给 Guard 的 ``RiskGate.status``（缺省不传）。
    """
    live = environment == "mainnet_live"
    env_vars = {"DMR_ENV": environment}
    if live:
        env_vars["DMR_ALLOW_LIVE"] = "1"
    settings = load_settings(env_vars, webui_root=tmp_path / "DMR_WebUI" / "webui")
    assert settings.legacy_mutex_enabled is live
    if runtime is not None:
        runtime_dir = runtime
    elif live:
        if create_runtime:
            runtime_dir = make_runtime_dir(tmp_path, marker=marker)
        else:
            runtime_dir = legacy_runtime_dir(settings)
        assert runtime_dir == legacy_runtime_dir(settings)
    else:
        runtime_dir = tmp_path / "must_not_exist" / "runtime_state"
    assert tmp_path in runtime_dir.parents  # 绝不指向真实的 DMR_WebUI/data/runtime_state

    adapter = adapter or FakeAdapter()
    adapter.set_read("fetch_position_mode", True)
    adapter.set_read("fetch_restrictions", RestrictionInfo(restricted=False))
    notifier = FakeNotifier()
    values: dict[str, Any] = {"ex_stream_enabled": True}
    values.update(config or {})
    modelstate = ModelStateService(ledger, bus, clock)
    legacy = LegacyMutex(runtime_dir, enabled=settings.legacy_mutex_enabled if mutex_enabled is None else mutex_enabled)
    guard = Guard(settings=settings, adapter=adapter, modelstate=modelstate, bus=bus, clock=clock,
                  config_get=values.__getitem__, notifier=notifier, legacy=legacy, risk_status=risk_status)
    return Env(tmp_path=tmp_path, clock=clock, ledger=ledger, bus=bus, settings=settings, runtime_dir=runtime_dir,
               adapter=adapter, notifier=notifier, config=values, modelstate=modelstate, legacy=legacy, guard=guard,
               expect_untouched=not live and runtime is None)
