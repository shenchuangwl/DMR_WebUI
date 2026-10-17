"""告警（第 2 层 alerts）：先在本地留痕，再（配了渠道地址时）经 Apprise 对外发送。

- ``raise_alert`` 同步、不阻塞、不抛异常：只做去重判断并入队；``alert`` 表的写入、按级别写日志、发布 ``AlertRaised``
  都由工作协程完成，所以调用方（对账、保护单、风控……）永远不会因为告警而卡住或出错。只在事件循环线程里调用。
- 对外发送：每条告警一个投递任务，把发送函数放到专用线程池里执行（Apprise 是阻塞的，绝不在事件循环里调用）；
  失败（返回 False、抛异常、超时）记一次 ``attempts``，按 ``backoff_s`` 退避后重试，最多 ``max_attempts`` 次；
  多个地址时只重发还没送达的那几个。重试和卡住的渠道不会拖慢后面告警的落账与发布。
  没配地址（本轮）只做本地留痕，``delivered`` 记为 True（本地即送达），``attempts`` 为 0。
- 去重：同一 ``dedupe_key`` 在 ``dedupe_s`` 秒内只记一次（从最后一次「记下」起算，被丢掉的不延长窗口）；没给键的告警从不去重。
- 渠道地址里带令牌：地址、发送函数抛出的异常内容都不进日志，只记异常类型。
- 发送函数可以注入（``sender``），测试用假的；默认是 ``apprise_send``。本模块是内核里唯一导入 apprise 的地方。
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import AlertRaised, EventBus
from dmr_kernel.core.types import AlertLevel
from dmr_kernel.ledger import repo_alerts
from dmr_kernel.ledger.records import AlertRecord
from dmr_kernel.ledger.writer import Ledger

log = logging.getLogger(__name__)

__all__ = ["Notifier", "Sender", "apprise_send"]

# 阻塞的发送函数：(渠道地址, 标题, 正文, 级别) -> 是否送达。失败返回 False 或抛异常。只在线程池里调用。
Sender = Callable[[str, str, str, AlertLevel], bool]

NOTIFY_TYPES: dict[AlertLevel, str] = {
    AlertLevel.INFO: "info",
    AlertLevel.WARNING: "warning",
    AlertLevel.CRITICAL: "failure",
}
LEVEL_LABELS: dict[AlertLevel, str] = {
    AlertLevel.INFO: "提示",
    AlertLevel.WARNING: "警告",
    AlertLevel.CRITICAL: "严重",
}
LOG_LEVELS: dict[AlertLevel, int] = {
    AlertLevel.INFO: logging.INFO,
    AlertLevel.WARNING: logging.WARNING,
    AlertLevel.CRITICAL: logging.CRITICAL,
}
DEFAULT_BACKOFF_S: tuple[float, ...] = (2.0, 10.0, 30.0, 60.0, 120.0)
SEND_THREADS = 4
_DEDUPE_PRUNE_AT = 1024


def apprise_send(url: str, title: str, body: str, level: AlertLevel) -> bool:
    """经 Apprise 把一条消息发到一个渠道地址，只有确认送达才返回 True。阻塞调用，由 Notifier 放到线程池里执行。

    Apprise 的持久化存储用内存模式（不往用户目录写文件）；它的日志不外传（未解析的地址会原样出现在它的日志里）。
    """
    if not url:
        return False
    import apprise  # 延迟导入：没配地址时不加载

    apprise_logger = logging.getLogger("apprise")
    apprise_logger.propagate = False
    if not apprise_logger.handlers:
        apprise_logger.addHandler(logging.NullHandler())
    asset = apprise.AppriseAsset(storage_mode=apprise.PersistentStoreMode.MEMORY)
    channel = apprise.Apprise(asset=asset)
    if not channel.add(url):
        return False
    return channel.notify(body=body, title=title, notify_type=NOTIFY_TYPES[level]) is True


@dataclass(slots=True)
class _Item:
    at_ms: int
    level: AlertLevel
    kind: str
    message: str


class Notifier:
    def __init__(self, ledger: Ledger, bus: EventBus, clock: Clock, *, urls: tuple[str, ...] = (),
                 dedupe_s: int = 300, max_attempts: int = 5, sender: Sender | None = None,
                 backoff_s: Sequence[float] = DEFAULT_BACKOFF_S, send_timeout_s: float = 30.0,
                 stop_grace_s: float = 3.0, max_queue: int = 10_000):
        if max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        if dedupe_s < 0:
            raise ValueError("dedupe_s 不能为负")
        if not backoff_s or any(b < 0 for b in backoff_s):
            raise ValueError("backoff_s 至少给一个非负的等待时间")
        if send_timeout_s <= 0 or stop_grace_s < 0 or max_queue < 1:
            raise ValueError("send_timeout_s 必须为正，stop_grace_s 不能为负，max_queue 至少为 1")
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._urls = tuple(dict.fromkeys(str(u) for u in urls if str(u).strip()))  # 去掉空的和重复的
        self._dedupe_ms = int(dedupe_s) * 1000
        self._max_attempts = int(max_attempts)
        self._sender: Sender = sender if sender is not None else apprise_send
        self._backoff_s = tuple(float(b) for b in backoff_s)
        self._send_timeout_s = float(send_timeout_s)
        self._stop_grace_s = float(stop_grace_s)
        self._queue: asyncio.Queue[_Item] = asyncio.Queue(maxsize=int(max_queue))
        self._state = "new"  # new | running | stopped
        self._worker: asyncio.Task | None = None
        self._deliveries: set[asyncio.Task] = set()
        self._executor: cf.ThreadPoolExecutor | None = None
        self._stop_event = asyncio.Event()
        self._last_recorded: dict[str, int] = {}

    def __repr__(self) -> str:  # 不带渠道地址（里面有令牌）
        return f"Notifier(channels={len(self._urls)}, state={self._state!r})"

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._state == "running":
            return
        self._state = "running"
        self._stop_event = asyncio.Event()
        if self._urls and self._executor is None:
            self._executor = cf.ThreadPoolExecutor(max_workers=SEND_THREADS, thread_name_prefix="alert-send")
        self._worker = asyncio.get_running_loop().create_task(self._run(), name="alert-recorder")

    async def stop(self) -> None:
        """先把已入队的告警落账并发布，再收尾：叫停重试（正在进行的那次发送最多再等 ``stop_grace_s`` 秒），
        不等线程里卡住的发送函数。"""
        if self._state == "stopped":
            return
        await self._settle_queue()
        self._state = "stopped"
        self._stop_event.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if self._deliveries:
            _, pending = await asyncio.wait(set(self._deliveries), timeout=self._stop_grace_s)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    async def flush(self) -> None:
        """等已入队的告警全部落账、发布，并等对外发送（含重试）做完或放弃。没有 start 时就地处理。"""
        await self._settle_queue()
        while True:
            pending = [task for task in self._deliveries if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def recent(self, limit: int = 100) -> list[AlertRecord]:
        """最近的告警，新的在前。"""
        return await self._ledger.read(repo_alerts.recent, limit)

    # ------------------------------------------------------------ 提出告警

    def raise_alert(self, level: AlertLevel, kind: str, message: str, *, dedupe_key: str | None = None) -> None:
        """不阻塞、不抛异常：去重判断后入队，其余交给工作协程。队列满了（只会在落账长时间卡住时发生）丢弃并记日志。"""
        try:
            level = self._coerce_level(level)
            kind, message = str(kind), str(message)
            now = self._clock.now_ms()
            if self._state == "stopped":
                self._log_alert(level, kind, message, note="（告警器已停止，未写入账本）")
                return
            if dedupe_key is not None and self._is_duplicate(dedupe_key, now):
                return
            try:
                self._queue.put_nowait(_Item(now, level, kind, message))
            except asyncio.QueueFull:
                self._log_alert(level, kind, message)
                log.error("告警队列已满，丢弃这条告警（上面的日志行是唯一的记录）")
                return
            if dedupe_key is not None:
                self._mark_recorded(dedupe_key, now)
        except Exception:
            log.exception("提出告警时出错（告警未入队）")

    @staticmethod
    def _coerce_level(level: AlertLevel) -> AlertLevel:
        try:
            return AlertLevel(level)
        except ValueError:
            log.error("告警级别无效 %r，按 warning 处理", level)
            return AlertLevel.WARNING

    def _is_duplicate(self, key: str, now_ms: int) -> bool:
        if self._dedupe_ms <= 0:
            return False
        last = self._last_recorded.get(key)
        return last is not None and 0 <= now_ms - last < self._dedupe_ms

    def _mark_recorded(self, key: str, now_ms: int) -> None:
        self._last_recorded[key] = now_ms
        if len(self._last_recorded) > _DEDUPE_PRUNE_AT:  # 清掉窗口已过的键，防止无限增长
            self._last_recorded = {k: t for k, t in self._last_recorded.items() if now_ms - t < self._dedupe_ms}

    @staticmethod
    def _log_alert(level: AlertLevel, kind: str, message: str, note: str = "") -> None:
        log.log(LOG_LEVELS[level], "告警 [%s] %s：%s%s", LEVEL_LABELS[level], kind, message, note)

    # ------------------------------------------------------------ 工作协程：落账、发布

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._process(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("告警处理出错")
            finally:
                self._queue.task_done()

    async def _settle_queue(self) -> None:
        """队列里已有的告警全部处理完：有工作协程就等它，没有就在当前任务里处理。"""
        if self._state == "running":
            await self._queue.join()
            return
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self._process(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("告警处理出错")
            finally:
                self._queue.task_done()

    async def _process(self, item: _Item) -> None:
        self._log_alert(item.level, item.kind, item.message)  # 先写日志：账本写不进去时它仍是一份记录
        local_only = not self._urls
        try:
            record = await self._ledger.write(
                repo_alerts.insert, at_ms=item.at_ms, level=item.level, kind=item.kind, message=item.message,
                delivered=local_only, delivered_ms=self._clock.now_ms() if local_only else None, attempts=0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("告警写入账本失败（日志里的上一行是唯一的记录）：[%s] %s", item.level.value, item.kind)
            return
        self._bus.publish(AlertRaised(alert_id=record.id, level=item.level, kind=item.kind, message=item.message,
                                      at_ms=item.at_ms))
        if self._urls:
            task = asyncio.get_running_loop().create_task(self._deliver(record.id, item),
                                                          name=f"alert-deliver-{record.id}")
            self._deliveries.add(task)
            task.add_done_callback(self._deliveries.discard)

    # ------------------------------------------------------------ 对外发送

    async def _deliver(self, alert_id: int, item: _Item) -> None:
        title = f"[{LEVEL_LABELS[item.level]}] {item.kind}"
        pending = list(self._urls)  # 还没送达的地址
        attempt = 0
        while True:
            attempt += 1
            results = await asyncio.gather(*(self._send_one(url, title, item) for url in pending))
            pending = [url for url, ok in zip(pending, results) if not ok]
            delivered = not pending
            try:
                await self._ledger.write(repo_alerts.record_attempt, alert_id, delivered=delivered,
                                         at_ms=self._clock.now_ms())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("告警 #%d 的送达记录写入账本失败", alert_id)
            if delivered:
                return
            if attempt >= self._max_attempts or self._stop_event.is_set():
                break
            if await self._backoff(attempt):
                break
        log.warning("告警 #%d（%s）尝试 %d 次后仍有 %d 个外部渠道没送达；本地已留痕", alert_id, item.kind, attempt,
                    len(pending))

    async def _backoff(self, attempt: int) -> bool:
        """退避等待；等的过程中被叫停（stop）返回 True。"""
        delay = self._backoff_s[min(attempt - 1, len(self._backoff_s) - 1)]
        if delay <= 0:
            await asyncio.sleep(0)
            return self._stop_event.is_set()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return False
        return True

    async def _send_one(self, url: str, title: str, item: _Item) -> bool:
        executor = self._executor
        if executor is None:
            return False
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(executor, self._sender, url, title, item.message, item.level)
            return bool(await asyncio.wait_for(future, timeout=self._send_timeout_s))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("告警外部发送超过 %.0f 秒没有结果，按失败处理", self._send_timeout_s)
            return False
        except Exception as exc:  # 只记类型：异常内容里可能带着渠道地址
            log.warning("告警外部发送失败：%s", type(exc).__name__)
            return False
