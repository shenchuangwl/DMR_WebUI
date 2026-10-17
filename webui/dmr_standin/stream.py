"""账户数据流的订阅中心（docs/设计约定.md 7.3、7.5）：listenKey、订阅、心跳，以及断线与静默两种故障。

与传输无关：``Subscription`` 是一个帧队列。HTTP 外壳（``server.py``）的 WebSocket 处理把它的帧逐条发出去；
内核集成测试的进程内传输直接 ``await sub.get()``。

- ``ListenKeys``：账户同时只有一把有效的 listenKey；有效期内再次 POST 返回同一把并续期；60 分钟不续期即失效。
- ``StreamHub.subscribe(key)``：key 无效或已过期、行为开关 ``stream_available=false``、``stream_drop`` 的 close 窗口内
  → ``StreamRefused``。silent 窗口内照常连上，但收不到任何帧。
- ``publish(frames)``：silent 窗口内整批丢弃（不补发）；否则推给全部打开的订阅。
- ``tick()``：失效的 listenKey 向它的订阅推 ``listen_key_expired`` 后断开；每个订阅每 10 秒一帧心跳（silent 窗口内不发）。
- 开关变化时：``stream_drop`` 的 close 窗口一打开、或 ``stream_available`` 关掉，就立即断开全部连接。

``Subscription``：``get()`` 等下一帧（已断开且队列取空后抛 ``StreamClosed``）；``drain()`` 取走已到的全部帧（同步）；
``close()`` 由客户端主动断开（丢弃未取的帧）；``closed`` / ``close_reason``。服务端断开时已排队的帧仍可取完。
"""
from __future__ import annotations

import asyncio
import secrets
import string
from collections import deque
from typing import Any, Callable

from .faults import Switches

HEARTBEAT_MS = 10_000
LISTEN_KEY_VALID_MS = 60 * 60_000
_KEY_ALPHABET = string.ascii_letters + string.digits


class StreamClosed(Exception):
    """订阅已断开（服务端断开或客户端关闭），且没有剩余的帧。"""


class StreamRefused(Exception):
    """拒绝建立数据流（listenKey 无效、数据流不可用、断线故障中）。"""


def _resolve(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.set_result(None)


class Subscription:
    def __init__(self, hub: StreamHub, listen_key: str, now_ms: int):
        self._hub = hub
        self.listen_key = listen_key
        self.created_ms = now_ms
        self.next_heartbeat_ms = now_ms + HEARTBEAT_MS
        self.closed = False
        self.close_reason: str | None = None
        self._frames: deque[dict[str, Any]] = deque()
        self._waiters: list[asyncio.Future] = []

    def push(self, frame: dict[str, Any]) -> None:
        if self.closed:
            return
        self._frames.append(frame)
        self._wake()

    def drain(self) -> list[dict[str, Any]]:
        frames = list(self._frames)
        self._frames.clear()
        return frames

    async def get(self) -> dict[str, Any]:
        while True:
            if self._frames:
                return self._frames.popleft()
            if self.closed:
                raise StreamClosed(self.close_reason or "closed")
            fut = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
            try:
                await fut
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)

    def close(self, reason: str = "client_closed") -> None:
        """客户端主动断开：不再收帧，未取的帧丢弃。"""
        self._frames.clear()
        self._shut(reason)
        self._hub.discard(self)

    def _shut(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        self._wake()

    def _wake(self) -> None:
        waiters, self._waiters = self._waiters, []
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for fut in waiters:
            if fut.done():
                continue
            loop = fut.get_loop()
            if loop is running:
                fut.set_result(None)
            elif not loop.is_closed():
                loop.call_soon_threadsafe(_resolve, fut)


class ListenKeys:
    def __init__(self, valid_ms: int = LISTEN_KEY_VALID_MS):
        self.valid_ms = valid_ms
        self._keys: dict[str, int] = {}  # key -> 失效时刻

    def create(self, now_ms: int) -> tuple[str, int]:
        """POST：有有效的 key 就续期并返回它，否则新建一把。"""
        for key, expires in self._keys.items():
            if expires > now_ms:
                self._keys[key] = now_ms + self.valid_ms
                return key, self._keys[key]
        key = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(64))
        self._keys[key] = now_ms + self.valid_ms
        return key, self._keys[key]

    def keepalive(self, key: Any, now_ms: int) -> int | None:
        """PUT：有效的 key 续期 60 分钟并返回新的失效时刻；无效或已失效返回 None。"""
        if self.valid(key, now_ms):
            self._keys[key] = now_ms + self.valid_ms
            return self._keys[key]
        return None

    def valid(self, key: Any, now_ms: int) -> bool:
        return isinstance(key, str) and self._keys.get(key, now_ms) > now_ms

    def active(self, now_ms: int) -> bool:
        return any(expires > now_ms for expires in self._keys.values())

    def expire_due(self, now_ms: int) -> list[str]:
        expired = [k for k, expires in self._keys.items() if expires <= now_ms]
        for k in expired:
            del self._keys[k]
        return expired


class StreamHub:
    def __init__(self, *, clock: Callable[[], int], switches: Switches, heartbeat_ms: int = HEARTBEAT_MS,
                 listen_key_valid_ms: int = LISTEN_KEY_VALID_MS):
        self._clock = clock
        self._switches = switches
        self._heartbeat_ms = heartbeat_ms
        self.keys = ListenKeys(listen_key_valid_ms)
        self._subs: list[Subscription] = []
        switches.add_listener(self._on_switches)

    # ------------------------------------------------------------ 订阅

    def subscribe(self, listen_key: Any) -> Subscription:
        now = self._clock()
        if not self._switches.behavior("stream_available"):
            raise StreamRefused("数据流不可用（行为开关 stream_available=false）")
        drop = self._switches.stream_drop()
        if drop is not None and drop[0] == "close":
            raise StreamRefused("数据流断线故障中，拒绝重连")
        if not self.keys.valid(listen_key, now):
            raise StreamRefused("listenKey 无效或已失效")
        sub = Subscription(self, listen_key, now)
        self._subs.append(sub)
        return sub

    def discard(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def count(self) -> int:
        return len(self._subs)

    def close_all(self, reason: str) -> None:
        subs, self._subs = self._subs, []
        for sub in subs:
            sub._shut(reason)

    # ------------------------------------------------------------ 推送

    def _silent(self) -> bool:
        drop = self._switches.stream_drop()
        return drop is not None and drop[0] == "silent"

    def publish(self, frames: list[dict[str, Any]]) -> None:
        if not frames or not self._subs or self._silent():
            return
        for sub in list(self._subs):
            for frame in frames:
                sub.push(frame)

    def tick(self) -> None:
        now = self._clock()
        silent = self._silent()
        for key in self.keys.expire_due(now):
            frame = {"type": "listen_key_expired", "event_time_ms": now}
            for sub in [s for s in self._subs if s.listen_key == key]:
                if not silent:
                    sub.push(frame)
                sub._shut("listen_key_expired")
                self.discard(sub)
        for sub in list(self._subs):
            if now >= sub.next_heartbeat_ms:
                if not silent:
                    sub.push({"type": "heartbeat", "server_time_ms": now})
                sub.next_heartbeat_ms = now + self._heartbeat_ms

    def _on_switches(self) -> None:
        if not self._subs:
            return
        drop = self._switches.stream_drop()
        if drop is not None and drop[0] == "close":
            self.close_all("stream_drop")
        elif not self._switches.behavior("stream_available"):
            self.close_all("stream_unavailable")
