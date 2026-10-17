"""替身环境的账户数据流（第 1 层）：listenKey、读循环、判活与重连（计划报告 4.4；协议见 docs/设计约定.md 7.3）。

流程：``POST /api/private/listen_key`` 申请 listenKey -> 连 ``/ws/stream?listen_key=`` -> 读循环把每条消息解析成
``StreamOrder`` / ``StreamAlgo`` / ``StreamAccount``，发布 ``StreamOrderReceived`` / ``StreamAlgoReceived`` /
``StreamAccountReceived``。心跳（``heartbeat``）只用于判活，不发业务事件；解析不了的帧丢弃并计数（``dropped_frames``），
但它说明连接是活的，同样算「收到了帧」。

时间驱动的动作全部在 ``check_liveness()`` 里按注入的时钟评估（调度器每 5 秒调一次，测试推进 ``ManualClock`` 后直接调），
读循环只负责收帧：

- 静默：连接层超过 ``stream_silence_s`` 秒没有收到任何帧 -> ``StreamStatusChanged("interrupted")``；
- 续期：每 ``keepalive_s``（默认 20 分钟）``PUT`` 一次 listenKey；失败后每分钟重试；超过 ``listenkey_stale_min`` 分钟没有
  续期成功（建流的 POST 也算一次续期）-> ``interrupted``；
- 重连：中断后按 ``reconnect_backoff_s`` 退避（1、2、5、10、30、30…秒；成功后从头计）重试，重试失败不重复发 ``interrupted``；
  成功发布 ``connected``，连续稳定 ``stream_recover_stable_s`` 秒后发布 ``recovered_stable``
  （只在「中断之后的重连」之后发布；首次连接成功只有 ``connected``）；
- ``listen_key_expired`` 帧：发布 ``ListenKeyExpired`` 与 ``interrupted``，读循环立刻重新申请 listenKey 并重建（不等退避）；
- 拒绝建流（``stream_available=false``、断线故障窗口内、令牌错误……）同样是 ``interrupted``，``start()`` 不抛。

阈值每次判活时现读 ``config_get``。listenKey 是凭据：不进日志，也不进事件的原因文字。
``items()`` 是事件总线之外的另一种消费方式：从第一次取项起，按到达顺序收取之后解析出的业务记录；``stop()`` 使它们结束。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import (
    EventBus,
    ListenKeyExpired,
    StreamAccountReceived,
    StreamAlgoReceived,
    StreamOrderReceived,
    StreamStatusChanged,
)
from dmr_kernel.core.types import StreamAccount, StreamAlgo, StreamOrder
from dmr_kernel.exchange.adapter import AccountStream, StreamItem
from dmr_kernel.exchange.errors import parse_error_body
from dmr_kernel.exchange.standin import (
    DecodeError,
    decode_account_update,
    decode_algo_update,
    decode_order_update,
)
from dmr_kernel.exchange.transport import Response, Transport, TransportError, WsConnection

log = logging.getLogger(__name__)

LISTEN_KEY_PATH = "/api/private/listen_key"
WS_PATH = "/ws/stream"

KEEPALIVE_RETRY_S = 60.0  # 续期失败后的重试间隔（不超过正常的续期间隔）

_KEY_IN_TEXT = re.compile(r"listen_?key=[^&\s'\"]+", re.IGNORECASE)


# ====================================================================== 帧解析


@dataclass(frozen=True)
class Heartbeat:
    """``heartbeat`` 帧：只用于判活。"""


@dataclass(frozen=True)
class ListenKeyExpiredFrame:
    """``listen_key_expired`` 帧：listenKey 已失效，服务端随后断开。"""


Frame = Heartbeat | ListenKeyExpiredFrame | StreamOrder | StreamAlgo | StreamAccount


def parse_frame(text: str) -> Frame:
    """一条文本消息 -> 帧。不是 JSON 对象、类型不认识、字段缺失或取值不合法时抛 ``DecodeError``（不带原文，免得泄露）。"""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise DecodeError(f"帧不是合法的 JSON：{type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise DecodeError(f"帧应是 JSON 对象：{type(data).__name__}")
    kind = data.get("type")
    if kind == "heartbeat":
        return Heartbeat()
    if kind == "listen_key_expired":
        return ListenKeyExpiredFrame()
    if kind == "order_update":
        return decode_order_update(data)
    if kind == "algo_update":
        return decode_algo_update(data)
    if kind == "account_update":
        return decode_account_update(data)
    raise DecodeError(f"不认识的帧类型：{str(kind)[:40]!r}")


# ====================================================================== 数据流


class _AttemptFailed(Exception):
    """一次建流尝试失败（申请 listenKey 或连接数据流）；文字是面向用户的原因。"""


def _http_failure(what: str, response: Response) -> str:
    code, msg = parse_error_body(response.body)
    text = f"{what}：HTTP {response.status}" + (f"（代码 {code}）" if code is not None else "") + (f"：{msg}" if msg else "")
    if response.status in (401, 403):
        text += "（认证失败，属于配置错误：请检查替身交易所的令牌）"
        log.error("%s", text)
    return text


def _listen_key_of(body: Any) -> str:
    if not isinstance(body, dict):
        raise DecodeError(f"响应应是 JSON 对象：{type(body).__name__}")
    key = body.get("listen_key")
    if not isinstance(key, str) or not key.strip():
        raise DecodeError("缺少字段 listen_key")
    return key


class StandinAccountStream(AccountStream):
    def __init__(self, transport: Transport, *, bus: EventBus, clock: Clock, config_get: Callable[[str], Any],
                 keepalive_s: float = 1200.0, reconnect_backoff_s: tuple[float, ...] = (1, 2, 5, 10, 30),
                 request_timeout_s: float = 10.0):
        if not reconnect_backoff_s or any(delay <= 0 for delay in reconnect_backoff_s):
            raise ValueError("reconnect_backoff_s 至少要有一项，且每项都大于 0")
        if keepalive_s <= 0:
            raise ValueError("keepalive_s 必须大于 0")
        self._transport = transport
        self._bus = bus
        self._clock = clock
        self._config_get = config_get
        self._keepalive_ms = round(keepalive_s * 1000)
        self._retry_keepalive_ms = min(self._keepalive_ms, round(KEEPALIVE_RETRY_S * 1000))
        self._backoff_ms = tuple(round(delay * 1000) for delay in reconnect_backoff_s)
        self._timeout_s = request_timeout_s

        self._state = "stopped"
        self._epoch = 0  # 每次 start/stop/中断/建立连接都加一：过期的读循环与在途的建流尝试据此自己退出
        self._conn: WsConnection | None = None
        self._read_task: asyncio.Task | None = None
        self._listen_key: str | None = None
        self._key_seen: str | None = None  # 只用来从文字里抹掉凭据
        self._connected_at_ms: int | None = None
        self._next_keepalive_ms = 0
        self._next_attempt_ms = 0
        self._failures = 0  # 本次中断以来连续失败的建流尝试数（中断本身算第一次）
        self._recovering = False  # 当前连接是中断之后重连上的，还没发过 recovered_stable
        self._connecting_epoch: int | None = None
        self._checking = False
        self._queues: list[asyncio.Queue[StreamItem | None]] = []

        self.last_frame_ms: int | None = None
        self.last_keepalive_ok_ms: int | None = None
        self.dropped_frames = 0  # 解析不了而丢弃的帧
        self.last_error = ""  # 最近一次中断或建流失败的原因

    # ------------------------------------------------------------ 状态与事件

    @property
    def state(self) -> str:
        return self._state

    def _scrub(self, text: str) -> str:
        text = _KEY_IN_TEXT.sub("listen_key=***", text)
        if self._key_seen:
            text = text.replace(self._key_seen, "***")
        return text

    def _publish_status(self, state: str, reason: str) -> None:
        self._bus.publish(StreamStatusChanged(state=state, reason=self._scrub(reason), at_ms=self._clock.now_ms()))

    # ------------------------------------------------------------ 启动与停止

    async def start(self) -> None:
        """第一次建流（立刻尝试一次）。失败不抛：状态变成 interrupted，之后由 ``check_liveness`` 按退避重试。"""
        if self._state != "stopped":
            return
        self._epoch += 1
        self._state = "connecting"
        self._recovering = False
        self._failures = 0
        await self._connect()

    async def stop(self) -> None:
        if self._state == "stopped":
            return
        conn, task = self._conn, self._read_task
        self._epoch += 1
        self._conn = self._read_task = self._listen_key = None
        self._state = "stopped"
        self._recovering = False
        self._publish_status("stopped", "已停止")
        for queue in self._queues:
            queue.put_nowait(None)
        await self._dispose(conn, task)

    async def _dispose(self, conn: WsConnection | None, task: asyncio.Task | None) -> None:
        """收掉旧连接的读循环并关闭连接（可能从读循环自己调用，此时不取消自己）。"""
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if conn is not None:
            try:
                await conn.close()
            except Exception as exc:  # 关闭时的错误只记录
                log.debug("关闭数据流连接时出错：%s", self._scrub(str(exc)))

    # ------------------------------------------------------------ 建流

    async def _connect(self) -> None:
        """一次建流尝试：申请 listenKey，连数据流。成功转 connected；失败按退避安排下一次。"""
        epoch = self._epoch
        self._connecting_epoch = epoch
        try:
            conn, key = await self._open(epoch)
        except _AttemptFailed as exc:
            if epoch == self._epoch:
                self._attempt_failed(str(exc))
            return
        finally:
            if self._connecting_epoch == epoch:
                self._connecting_epoch = None
        if epoch != self._epoch or self._state not in ("connecting", "interrupted"):
            await self._dispose(conn, None)  # 在途期间被停止或重新启动了：不留下悬空的连接
            return
        self._install(conn, key)

    async def _open(self, epoch: int) -> tuple[WsConnection, str]:
        try:
            response = await self._transport.request("POST", LISTEN_KEY_PATH, auth=True, timeout_s=self._timeout_s)
        except TransportError as exc:
            raise _AttemptFailed(f"申请 listenKey 失败：{exc}") from None
        if response.status != 200:
            raise _AttemptFailed(_http_failure("申请 listenKey 被拒绝", response))
        try:
            key = _listen_key_of(response.body)
        except DecodeError as exc:
            raise _AttemptFailed(f"申请 listenKey 的响应无法解析：{exc}") from None
        self._key_seen = key
        if epoch != self._epoch:
            raise _AttemptFailed("已取消")
        try:
            conn = await self._transport.ws_connect(WS_PATH, params={"listen_key": key}, timeout_s=self._timeout_s)
        except TransportError as exc:
            raise _AttemptFailed(f"连接数据流失败：{exc}") from None
        return conn, key

    def _attempt_failed(self, reason: str) -> None:
        reason = self._scrub(reason)
        self._failures += 1
        self.last_error = reason
        delay_ms = self._backoff_ms[min(self._failures - 1, len(self._backoff_ms) - 1)]
        self._next_attempt_ms = self._clock.now_ms() + delay_ms
        log.warning("数据流建立失败（连续第 %d 次），%.0f 秒后重试：%s", self._failures, delay_ms / 1000, reason)
        if self._state == "connecting":  # 首次连接就失败：等同于中断
            self._state = "interrupted"
            self._recovering = True
            self._publish_status("interrupted", reason)

    def _install(self, conn: WsConnection, key: str) -> None:
        now = self._clock.now_ms()
        self._epoch += 1
        self._conn = conn
        self._listen_key = key
        self._state = "connected"
        self._connected_at_ms = now
        self.last_keepalive_ok_ms = now  # 申请 listenKey 的 POST 本身就是一次续期
        self._next_keepalive_ms = now + self._keepalive_ms
        self._failures = 0
        self._read_task = asyncio.get_running_loop().create_task(self._read_loop(conn, self._epoch),
                                                                 name="account-stream-reader")
        log.info("账户数据流已%s", "重新连接" if self._recovering else "连接")
        self._publish_status("connected", "已重新连接" if self._recovering else "已连接")

    # ------------------------------------------------------------ 中断

    def _detach(self, reason: str, *, rebuild: bool = False) -> tuple[WsConnection | None, asyncio.Task | None]:
        """connected -> interrupted。同步完成（改状态、安排下一次尝试、发事件）；返回要收掉的旧连接与读循环。"""
        if self._state != "connected":
            return None, None
        now = self._clock.now_ms()
        conn, task = self._conn, self._read_task
        reason = self._scrub(reason)
        self._epoch += 1
        self._conn = self._read_task = self._listen_key = None
        self._state = "interrupted"
        self._recovering = True
        self._failures = 0 if rebuild else 1  # listenKey 失效立刻重建，不算失败；其它中断本身算第一次
        self._next_attempt_ms = now if rebuild else now + self._backoff_ms[0]
        self.last_error = reason
        log.warning("账户数据流中断：%s", reason)
        self._publish_status("interrupted", reason)
        return conn, task

    async def _interrupt(self, reason: str, *, rebuild: bool = False) -> None:
        conn, task = self._detach(reason, rebuild=rebuild)
        await self._dispose(conn, task)

    # ------------------------------------------------------------ 读循环

    async def _read_loop(self, conn: WsConnection, gen: int) -> None:
        try:
            while gen == self._epoch:
                text = await conn.receive(None)
                if gen != self._epoch:
                    return
                if text is None:
                    await self._interrupt("连接被对端关闭")
                    return
                if not await self._on_text(text):
                    return
        except asyncio.CancelledError:
            raise
        except TransportError as exc:
            if gen == self._epoch:
                await self._interrupt(f"数据流读取失败：{exc}")
        except Exception:
            log.exception("账户数据流的读循环出错")
            if gen == self._epoch:
                await self._interrupt("数据流读循环出错")

    async def _on_text(self, text: str) -> bool:
        """处理一条消息；返回 False 表示这条连接已经换掉，读循环应当退出。"""
        now = self._clock.now_ms()
        self.last_frame_ms = now  # 任何帧都说明连接是活的
        try:
            frame = parse_frame(text)
        except DecodeError as exc:
            self.dropped_frames += 1
            log.warning("数据流帧无法解析，已丢弃（累计 %d 个）：%s", self.dropped_frames, self._scrub(str(exc)))
            return True
        if isinstance(frame, Heartbeat):
            return True
        if isinstance(frame, ListenKeyExpiredFrame):
            self._bus.publish(ListenKeyExpired(at_ms=now))
            await self._interrupt("listenKey 已失效，重新建立", rebuild=True)
            await self._reconnect_if_due()
            return False
        if isinstance(frame, StreamOrder):
            self._bus.publish(StreamOrderReceived(frame))
        elif isinstance(frame, StreamAlgo):
            self._bus.publish(StreamAlgoReceived(frame))
        else:
            self._bus.publish(StreamAccountReceived(frame))
        for queue in self._queues:
            queue.put_nowait(frame)
        return True

    # ------------------------------------------------------------ 判活（调度器每 5 秒调一次）

    async def check_liveness(self) -> None:
        """判活、续期、到点重连、稳定后的 recovered_stable。同一时刻只跑一份（重入直接返回，等同调度器的单实例）。"""
        if self._state in ("stopped", "connecting") or self._checking:
            return
        self._checking = True
        try:
            await asyncio.sleep(0)  # 让读循环先处理完已经到达的帧，免得把躺在队列里的心跳误判成静默
            if self._state == "connected":
                await self._judge()
            if self._state == "interrupted":
                await self._reconnect_if_due()
            self._maybe_stable()
        finally:
            self._checking = False

    async def _judge(self) -> None:
        silence_s = int(self._config_get("stream_silence_s"))
        now = self._clock.now_ms()
        quiet_ms = now - max(self.last_frame_ms or 0, self._connected_at_ms or 0)
        if quiet_ms >= silence_s * 1000:
            await self._interrupt(f"{quiet_ms / 1000:.0f} 秒没有收到任何帧（阈值 {silence_s} 秒）")
            return
        if now >= self._next_keepalive_ms:
            await self._keepalive()
            if self._state != "connected":
                return
        stale_min = int(self._config_get("listenkey_stale_min"))
        stale_ms = self._clock.now_ms() - (self.last_keepalive_ok_ms or 0)
        if stale_ms >= stale_min * 60_000:
            await self._interrupt(f"listenKey 已 {stale_ms / 60_000:.0f} 分钟没有续期成功（阈值 {stale_min} 分钟）")

    async def _keepalive(self) -> None:
        epoch, key, sent_ms = self._epoch, self._listen_key, self._clock.now_ms()
        failure = ""
        try:
            response = await self._transport.request("PUT", LISTEN_KEY_PATH, body={"listen_key": key}, auth=True,
                                                     timeout_s=self._timeout_s)
        except TransportError as exc:
            failure = f"请求失败：{exc}"
        else:
            if response.status != 200:
                failure = _http_failure("交易所拒绝了续期", response)
        if epoch != self._epoch:
            return  # 续期期间连接已经换掉或停止
        if not failure:
            self.last_keepalive_ok_ms = sent_ms
            self._next_keepalive_ms = sent_ms + self._keepalive_ms
            return
        self._next_keepalive_ms = self._clock.now_ms() + self._retry_keepalive_ms
        log.warning("listenKey 续期失败，%.0f 秒后重试：%s", self._retry_keepalive_ms / 1000, self._scrub(failure))

    async def _reconnect_if_due(self) -> None:
        if self._state != "interrupted" or self._connecting_epoch == self._epoch:
            return
        if self._clock.now_ms() < self._next_attempt_ms:
            return
        await self._connect()

    def _maybe_stable(self) -> None:
        if not self._recovering or self._state != "connected":
            return
        window_s = int(self._config_get("stream_recover_stable_s"))
        if self._clock.now_ms() - (self._connected_at_ms or 0) >= window_s * 1000:
            self._recovering = False
            self._publish_status("recovered_stable", f"连接已连续稳定 {window_s} 秒")

    # ------------------------------------------------------------ 另一种消费方式

    async def items(self) -> AsyncIterator[StreamItem]:
        """业务记录（订单、条件单、账户）的异步迭代：从第一次取项起收取之后到达的；``stop()`` 使它结束。心跳不产出。"""
        queue: asyncio.Queue[StreamItem | None] = asyncio.Queue()
        self._queues.append(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            if queue in self._queues:
                self._queues.remove(queue)
