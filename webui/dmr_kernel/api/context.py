"""控制接口的装配数据：``ApiContext``（装配层交进来的服务）与 ``Services``（api 自己建的服务）。"""
from __future__ import annotations

from typing import Any, Callable

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dmr_kernel.alerts.notifier import Notifier
from dmr_kernel.api.auth import DEFAULT_SCRYPT, AuthService, LoginLimiter, ScryptParams
from dmr_kernel.api.common import KERNEL_VERSION
from dmr_kernel.api.events import EventHub
from dmr_kernel.api.livecache import LiveCache
from dmr_kernel.api.seat import SeatService
from dmr_kernel.core.clock import Clock
from dmr_kernel.core.config import ConfigStore
from dmr_kernel.core.limits import Limits
from dmr_kernel.core.modelstate import ModelStateService
from dmr_kernel.core.registry import Registry
from dmr_kernel.core.riskgate import RiskGate
from dmr_kernel.core.events import EventBus
from dmr_kernel.core.types import Model
from dmr_kernel.ledger.writer import Ledger
from dmr_kernel.models.base import TradingModel
from dmr_kernel.ops.common import OpsWiring
from dmr_kernel.settings import Settings

__all__ = ["ApiContext", "Services", "build_services"]


@dataclass
class ApiContext:
    """装配层交给 ``create_app`` 的东西。``local_token`` 是装配先用 ``auth.write_local_token`` 写进
    ``var/kernel/local.token`` 的那个令牌；``models`` 三个模型都要有（``/state`` 取生效模型的按钮含义）。"""

    settings: Settings
    clock: Clock
    ledger: Ledger
    bus: EventBus
    modelstate: ModelStateService
    registry: Registry
    config: ConfigStore
    riskgate: RiskGate
    limits: Limits
    notifier: Notifier
    models: Mapping[Model, TradingModel]
    local_token: str
    heartbeat_path: Path | None = None  # 默认 <var>/kernel/heartbeat.json
    ws_ping_s: float = 15.0
    ws_max_queue: int = 1000
    scrypt_params: ScryptParams = DEFAULT_SCRYPT
    login_limiter: LoginLimiter | None = None
    kernel_version: str = KERNEL_VERSION
    ops_wiring: OpsWiring | None = None  # 操作服务要的协调者、适配层等（装配层交；为空时操作类接口答 501）
    list_view: Callable[[], Any] | None = None  # 名单同步的当前视图（ListSync.current）；装配层提供，状态条的名单条目读它

    @property
    def heartbeat_file(self) -> Path:
        return self.heartbeat_path if self.heartbeat_path is not None else (
            self.settings.var_dir / "kernel" / "heartbeat.json")


@dataclass
class Services:
    auth: AuthService
    seat: SeatService
    live: LiveCache
    hub: EventHub


def build_services(ctx: ApiContext) -> Services:
    missing = [model.value for model in Model if model not in ctx.models]
    if missing:
        raise ValueError(f"ApiContext.models 缺少模型：{', '.join(missing)}")
    if ctx.ws_ping_s <= 0:
        raise ValueError("ws_ping_s 必须是正数")
    return Services(
        auth=AuthService(ctx.ledger, ctx.clock, ctx.bus, config_get=ctx.config.get, local_token=ctx.local_token,
                         params=ctx.scrypt_params, limiter=ctx.login_limiter),
        seat=SeatService(ctx.ledger, ctx.clock, ctx.bus, config_get=ctx.config.get),
        live=LiveCache(ctx.bus, ctx.clock),
        hub=EventHub(ctx.bus, ctx.clock, max_queue=ctx.ws_max_queue))
