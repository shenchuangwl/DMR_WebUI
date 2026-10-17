"""进程内传输与替身交易所工厂（集成测试用；不开任何套接字，禁网包装下可用）。

``InProcessTransport`` 实现与 ``exchange.transport.HttpTransport`` 同一个 ``Transport`` 协议，但请求直接交给
``dmr_standin`` 的分发器（``Dispatcher.handle`` / ``Dispatcher.subscribe``），走的是与真实 HTTP 外壳同一套请求解析与撮合代码。

- 请求体编码成 JSON 字节、响应体经 JSON 往返，与真实 HTTP 一样只剩 JSON 类型；查询参数一律转成字符串，值为 ``None`` 的不发。
- 替身的「扣住不答」类故障（``order_timeout`` 的 hang、``position_query_fail`` 的 hang）按请求的 ``timeout_s`` 表现为
  ``TransportTimeout``；``Disconnect``（``order_timeout`` 的 drop 在扣住之后断开）表现为 ``TransportConnectionError``。
- 数据流：``ws_connect("/ws/stream", params={"listen_key": ...})`` 订阅分发器；被拒绝（listenKey 无效、``stream_available=false``、
  断线故障窗口内）表现为 ``TransportConnectionError``；``receive`` 取下一帧的 JSON 文本，服务端断开且队列取空后返回 ``None``。
- 时间：``clock`` 可以直接传内核的 ``ManualClock``（也可以是返回毫秒的可调用对象；缺省用系统时间）。测试里把同一个
  ``ManualClock`` 同时交给替身和被测对象，推进它就同时推进了双方的时间（断线故障窗口、listenKey 有效期等都按它计）。

``make_standin`` 返回 ``(分发器, 引擎, kernel 令牌, manual 令牌)``。

用法::

    clock = ManualClock(T0_MS)
    disp, engine, kernel_token, manual_token = make_standin(clock=clock)
    transport = InProcessTransport(disp, token=kernel_token)
"""
from __future__ import annotations

import asyncio
import json
import secrets
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

from dmr_kernel.exchange.transport import (
    Response,
    TransportConnectionError,
    TransportTimeout,
    WsConnection,
)
from dmr_standin.dispatcher import Disconnect, Dispatcher, build_standin, system_clock
from dmr_standin.engine import Engine
from dmr_standin.feed import InstrumentSpec, StaticFeed, load_sample
from dmr_standin.stream import StreamClosed, StreamRefused, Subscription

# 2026-10-14 02:00:00 UTC：不在资金费结算时刻（00:00、08:00、16:00）上，向后推几个小时也不会碰到
T0_MS = 1_791_943_200_000
AUTH_HEADER = "X-Standin-Key"
WS_PATH = "/ws/stream"


def ms_clock(clock: Any) -> Callable[[], int]:
    """把 ``ManualClock``（带 ``now_ms``）或返回毫秒的可调用对象统一成可调用对象；``None`` 用系统时间。"""
    if clock is None:
        return system_clock
    now_ms = getattr(clock, "now_ms", None)
    if callable(now_ms):
        return now_ms
    if callable(clock):
        return clock
    raise TypeError("clock 必须是返回毫秒的可调用对象，或带 now_ms() 的对象")


def make_standin(*, equity: str | int | Decimal = "1000", feed: Any = None, clock: Any = None,
                 prices: Mapping[str, Any] | None = None, instruments: Iterable[InstrumentSpec] | None = None,
                 sleep: Callable[[float], Any] | None = None,
                 seed: int | None = 7) -> tuple[Dispatcher, Engine, str, str]:
    """造一个进程内的替身交易所（不持久化，不起后台任务；测试自己调 ``disp.tick()`` 推进）。

    - ``feed``：不给就建 ``StaticFeed``（合约规则取 ``instruments``，缺省是内置样例 ``load_sample()``），每个合约的价格设成
      样例的 ``ref_price``；``prices`` 另设或覆盖若干价格。自备的 ``feed`` 原样使用（给了 ``prices`` 才替它设价）。
    - ``clock``：见模块说明；``sleep``：故障「扣住」用的等待函数（缺省真实的 ``asyncio.sleep``，配合短的 ``timeout_s``）。
    """
    now = ms_clock(clock)
    if feed is None:
        specs = list(instruments) if instruments is not None else load_sample()
        feed = StaticFeed(specs, clock=now)
        for spec in specs:
            if spec.ref_price is not None:
                feed.set_price(spec.symbol, spec.ref_price)
    for symbol, price in (prices or {}).items():
        feed.set_price(symbol, price)
    kernel_token, manual_token = secrets.token_hex(16), secrets.token_hex(16)
    extra: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
    disp = build_standin(feed=feed, clock=now, equity=equity if isinstance(equity, Decimal) else Decimal(str(equity)),
                         kernel_token=kernel_token, manual_token=manual_token, persist=False, seed=seed, **extra)
    return disp, disp.engine, kernel_token, manual_token


class InProcessWs:
    """``WsConnection`` 的进程内实现：包一个分发器的订阅。"""

    def __init__(self, sub: Subscription, owner: "InProcessTransport"):
        self._sub = sub
        self._owner = owner

    async def receive(self, timeout_s: float | None) -> str | None:
        try:
            if timeout_s is None:
                frame = await self._sub.get()
            else:
                frame = await asyncio.wait_for(self._sub.get(), timeout_s)
        except StreamClosed:
            return None
        except TimeoutError as exc:
            raise TransportTimeout(f"数据流 {timeout_s} 秒内没有消息") from exc
        return json.dumps(frame, ensure_ascii=False)

    async def close(self) -> None:
        self._sub.close()
        self._owner._forget(self)

    @property
    def subscription(self) -> Subscription:
        return self._sub


class InProcessTransport:
    def __init__(self, dispatcher: Dispatcher, *, token: str):
        self._dispatcher = dispatcher
        self._token = token
        self._conns: list[InProcessWs] = []

    async def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                      auth: bool = True, timeout_s: float) -> Response:
        query = {str(k): str(v) for k, v in (params or {}).items() if v is not None}
        raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {AUTH_HEADER: self._token} if auth else {}
        try:
            status, payload, out = await asyncio.wait_for(
                self._dispatcher.handle(method, path, query, raw, headers), timeout_s)
        except TimeoutError as exc:
            raise TransportTimeout(f"{method} {path} 超过 {timeout_s} 秒没有响应") from exc
        except Disconnect as exc:
            raise TransportConnectionError(f"{method} {path} 被对端断开：{exc}") from exc
        return Response(status, json.loads(json.dumps(payload)), dict(out))

    async def ws_connect(self, path: str, *, params: dict, timeout_s: float) -> WsConnection:
        if path != WS_PATH:
            raise TransportConnectionError(f"替身没有这个数据流路径：{path}")
        try:
            sub = self._dispatcher.subscribe(params.get("listen_key", ""))
        except StreamRefused as exc:
            raise TransportConnectionError(f"数据流被拒绝：{exc}") from exc
        conn = InProcessWs(sub, self)
        self._conns.append(conn)
        return conn

    def _forget(self, conn: InProcessWs) -> None:
        if conn in self._conns:
            self._conns.remove(conn)

    async def close(self) -> None:
        for conn in list(self._conns):
            await conn.close()


# ---------------------------------------------------------------- 组装帮手（集成测试共用）


class RecordingTransport:
    """包一层传输，记下发出的每个请求（``requests``：``(方法, 路径, 查询参数, 请求体, 是否带认证)``）。"""

    def __init__(self, inner: Any):
        self.inner = inner
        self.requests: list[tuple[str, str, dict | None, dict | None, bool]] = []
        self.ws_paths: list[str] = []

    async def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                      auth: bool = True, timeout_s: float) -> Response:
        self.requests.append((method, path, None if params is None else dict(params),
                              None if body is None else dict(body), auth))
        return await self.inner.request(method, path, params=params, body=body, auth=auth, timeout_s=timeout_s)

    async def ws_connect(self, path: str, *, params: dict, timeout_s: float) -> WsConnection:
        self.ws_paths.append(path)
        return await self.inner.ws_connect(path, params=params, timeout_s=timeout_s)

    async def close(self) -> None:
        await self.inner.close()

    def sent(self, method: str | None = None, path: str | None = None) -> list[tuple[str, str, dict | None, dict | None, bool]]:
        return [r for r in self.requests if (method is None or r[0] == method) and (path is None or r[1] == path)]


def default_config(**overrides: Any) -> dict[str, Any]:
    """内核配置项的替身环境初始值（``core.configdefs``）；可用关键字覆盖。用法：``config.__getitem__`` 当 ``config_get``。"""
    from dmr_kernel.core.configdefs import DEFINITIONS
    from dmr_kernel.core.types import Environment

    config = {key: d.initial(Environment.STANDIN) for key, d in DEFINITIONS.items()}
    unknown = set(overrides) - set(config)
    if unknown:
        raise KeyError(f"未知的配置项：{sorted(unknown)}")
    config.update(overrides)
    return config


class Stack:
    """一整套进程内装配：替身、传输、总线、写权限令牌和 ``StandinAdapter``（共用同一个时钟）。"""

    def __init__(self, **parts: Any):
        self.__dict__.update(parts)

    # 下面只是给编辑器看的属性说明
    clock: Any
    disp: Dispatcher
    engine: Engine
    feed: Any
    kernel_token: str
    manual_token: str
    transport: RecordingTransport
    bus: Any
    cap: Any
    config: dict[str, Any]
    adapter: Any


def make_stack(*, clock: Any = None, equity: str | int | Decimal = "1000", prices: Mapping[str, Any] | None = None,
               config: Mapping[str, Any] | None = None, request_timeout_s: float = 0.2, token: str | None = None,
               capability: Any = "coordinator", **standin_kwargs: Any) -> Stack:
    """替身 + 进程内传输 + ``StandinAdapter``。``clock`` 缺省是 ``ManualClock(T0_MS)``（可用 ``stack.clock`` 推进）。

    ``prices`` 缺省 BTCUSDT 50000、ETHUSDT 3000、DOGEUSDT 0.2（其余合约仍是样例参考价）；``capability`` 缺省造一张
    owner 为 coordinator 的写权限令牌，传 ``None`` 就是没有写权限；``token`` 缺省是 kernel 令牌（传别的用来测认证失败）。
    """
    from dmr_kernel.core.clock import ManualClock
    from dmr_kernel.core.events import EventBus
    from dmr_kernel.exchange.adapter import WriteCapability
    from dmr_kernel.exchange.standin import StandinAdapter

    clock = ManualClock(T0_MS) if clock is None else clock
    price_map = {"BTCUSDT": "50000", "ETHUSDT": "3000", "DOGEUSDT": "0.2"}
    price_map.update(prices or {})
    disp, engine, kernel_token, manual_token = make_standin(equity=equity, clock=clock, prices=price_map,
                                                            **standin_kwargs)
    transport = RecordingTransport(InProcessTransport(disp, token=kernel_token if token is None else token))
    bus = EventBus()
    cap = WriteCapability(capability) if isinstance(capability, str) else capability
    cfg = default_config(**dict(config or {}))
    adapter = StandinAdapter(transport, capability=cap, bus=bus, clock=clock, config_get=cfg.__getitem__,
                             request_timeout_s=request_timeout_s)
    return Stack(clock=clock, disp=disp, engine=engine, feed=disp.feed, kernel_token=kernel_token,
                 manual_token=manual_token, transport=transport, bus=bus, cap=cap, config=cfg, adapter=adapter)
