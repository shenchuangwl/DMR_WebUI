"""控制台的装配：把设置、内核客户端、会话存储、服务、探活拼在一起，并注册页面。

``ConsoleUi`` 是页面共用的上下文（页面函数里只通过它拿服务、时钟、时区、刷新节奏）；``build_console`` 按设置造出真实的一套，
测试里直接用假客户端和手动时钟自己拼 ``ConsoleUi``。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo

from nicegui import app as nicegui_app

from dmr_console.client import KernelApi, KernelClient
from dmr_console.devtools import StandinDev
from dmr_console.clock import Clock, SystemClock
from dmr_console.events import stream_factory_for
from dmr_console.labels import TITLE
from dmr_console.live import HealthProbe
from dmr_console.service import ConsoleService
from dmr_console.session import SessionStore
from dmr_console.settings import ConsoleSettings

LOGIN_PATH = "/login"


@dataclass(frozen=True)
class UiConfig:
    poll_interval_s: float = 5.0  # 定时拉取内核的间隔（事件流是提示，定时拉取兜底）
    tick_s: float = 1.0  # 界面刷新间隔：只读缓存，不请求内核


class ConsoleUi:
    def __init__(self, service: ConsoleService, probe: HealthProbe, clock: Clock, tz: tzinfo | None,
                 config: UiConfig | None = None, dev: StandinDev | None = None) -> None:
        self.service = service
        self.probe = probe
        self.clock = clock
        self.tz = tz
        self.config = config or UiConfig()
        self.dev = dev  # 替身开发工具的客户端（只在 env=standin 的页面用到）

    def browser_id(self) -> str | None:
        """浏览器会话编号（``app.storage.browser['id']``）。只能在构建页面时取；之后的事件回调里取不到，所以页面把它存进闭包。"""
        try:
            value = nicegui_app.storage.browser.get("id")
        except RuntimeError:  # 没配签名密钥，或不在页面请求里
            return None
        return value if isinstance(value, str) and value else None

    async def aclose(self) -> None:
        await self.service.aclose()
        await self.service.api.aclose()
        if self.dev is not None:
            await self.dev.aclose()


def local_timezone() -> tzinfo | None:
    return datetime.now().astimezone().tzinfo


def build_console(settings: ConsoleSettings, *, api: KernelApi | None = None, clock: Clock | None = None) -> ConsoleUi:
    clock = clock or SystemClock()
    api = api or KernelClient(settings.kernel_base_url, timeout_s=settings.request_timeout_s)
    tz = local_timezone()
    service = ConsoleService(api=api, clock=clock, sessions=SessionStore(clock), tz=tz,
                             stream_factory=stream_factory_for(settings.kernel_base_url))
    dev = StandinDev(settings.standin_url, settings.standin_token_path)
    return ConsoleUi(service, HealthProbe(api, clock), clock, tz, UiConfig(poll_interval_s=settings.poll_interval_s), dev=dev)


def register_pages(console: ConsoleUi) -> None:
    from dmr_console.pages import devpage, kline, login, oplog, overview, positions, settings, stats, trades, watchlist

    login.register(console)
    overview.register(console)
    positions.register(console)
    oplog.register(console)
    settings.register(console)
    watchlist.register(console)
    trades.register(console)
    stats.register(console)
    kline.register(console)
    devpage.register(console)
