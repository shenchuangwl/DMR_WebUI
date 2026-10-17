"""每个浏览器会话一个的「实时数据」控制器：拉取内核的只读接口、判定连通、保留失联前的数据、给页面提供视图。

规则（报告 4.10、设计约定第 11 节）：

- 任何一次传输层失败（连不上、超时、响应无法解析）就算与内核失联：状态条出现失联横幅、全部按钮禁用；
  **失联前的数据继续保留并标注「失联前」**，不清空、不假装还是最新的。第一次成功立刻恢复。
- 内核回了错误（含 503 暂时读不到）说明内核活着：链路正常，该部分的错误记在 ``errors`` 里；会话令牌失效（401）
  标记 ``session_invalid``，由页面送回登录页，之后不再拿死令牌敲内核。
- 同一批次里只要有一个部分传输失败，整个链路就判失联（宁可误判，不漏判），成功的部分照常入库。
- 版本号：每个部分的数据**真的变了**才加一，页面据此决定要不要重画表格；链路在失联/恢复时变一次。
- 事件流只是刷新提示：收到帧只重新拉对应的部分；每次（重）连上都全量刷新一次。事件流在有页面在线时才运行。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import tzinfo
from typing import Any

from dmr_console.actions import Actions
from dmr_console.client import KernelApi, KernelError, KernelUnreachable
from dmr_console.clock import Clock
from dmr_console.events import PARTS, refresh_parts_for_frame
from dmr_console.link import LinkMonitor
from dmr_console.session import ConsoleSession
from dmr_console.state import ButtonState, StatusBar, build_status_bar, button_states
from dmr_console.views import (OPLOG_CAP, PositionGroup, alert_rows, group_positions, merge_oplog, oplog_rows, rejected_rows,
                               watchlist_rows, watchlist_summary)

log = logging.getLogger(__name__)

DEFAULT_OPLOG_PAGE = 100
DEFAULT_ALERTS_LIMIT = 50
DEFAULT_STREAM_GRACE_S = 10.0  # 换页时旧页面先走、新页面随后到：宽限期内不重启事件流

_ATTRS = {"state": "state", "positions": "positions", "external": "external", "alerts": "alerts", "watchlist": "watchlist",
           "trades": "trades", "stats": "stats", "candles": "candles"}
STATS_GROUPS = ("model", "ownership", "side", "symbol")  # 统计页的四张分组表，各向内核要一次


def _item_id(item: Any) -> int | None:
    value = item.get("id") if isinstance(item, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class LiveController:
    def __init__(self, api: KernelApi, session: ConsoleSession, clock: Clock, *, tz: tzinfo | None = None,
                 oplog_page: int = DEFAULT_OPLOG_PAGE, alerts_limit: int = DEFAULT_ALERTS_LIMIT,
                 stream_factory: Callable[["LiveController"], Any] | None = None,
                 stream_grace_s: float = DEFAULT_STREAM_GRACE_S, env_hint: str | None = None,
                 operations_enabled: bool = False) -> None:
        self.api = api
        self.session = session
        self.tz = tz
        self.env_hint = env_hint
        self.operations_enabled = operations_enabled  # M1 只读：操作接口一律 501，按钮一律禁用
        self._clock = clock
        self._oplog_page = oplog_page
        self._alerts_limit = alerts_limit
        self._stream_factory = stream_factory
        self._grace_s = stream_grace_s
        self.actions = Actions(self, api, clock)

        self.state: dict | None = None
        self.positions: list | None = None
        self.external: list | None = None
        self.oplog: list[dict] = []
        self.alerts: list | None = None
        self.oplog_exhausted = False
        self.watchlist: dict | None = None  # 只有名单页在线时才拉（不属于每页都拉的 PARTS）
        self.trades: dict | None = None  # 逐笔交易、统计、K 线同理：只有对应页面在线时才拉
        self.stats: dict | None = None
        self.candles: dict | None = None
        self.trades_query: dict[str, Any] = {}
        self.stats_query: dict[str, Any] = {}
        self.candles_query: dict[str, Any] = {"symbol": "BTCUSDT", "tf": "15m", "limit": 200}

        self.fetched_ms: dict[str, int] = {}
        self.versions: dict[str, int] = {part: 0 for part in (*PARTS, "watchlist", "trades", "stats", "candles")}
        self.errors: dict[str, str] = {}
        self.link = LinkMonitor(clock)
        self.link_version = 0
        self.session_invalid = False
        self.closed = False

        self._attempt_ms: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._pending: list[tuple[asyncio.Future, frozenset[str]]] = []
        self._clients = 0
        self._stream_task: asyncio.Task | None = None
        self._stop_timer: asyncio.TimerHandle | None = None

    def __repr__(self) -> str:
        return f"LiveController(operator={self.session.operator!r}, lost={self.link.status.lost}, closed={self.closed})"

    def loaded(self, part: str) -> bool:
        return part in self.fetched_ms

    # ------------------------------------------------------------ 视图

    def status_bar(self) -> StatusBar:
        return build_status_bar(self.state, self.link.status, now_ms=self._clock.now_ms(), fetched_ms=self.fetched_ms.get("state"),
                                tz=self.tz, env_hint=self.env_hint)

    def buttons(self) -> dict[str, ButtonState]:
        return button_states(self.state, self.link.status, operations_enabled=self.operations_enabled)

    def position_groups(self) -> list[PositionGroup]:
        return group_positions(self.positions, self.external, tz=self.tz)

    def oplog_rows(self) -> list[dict]:
        return oplog_rows(self.oplog, tz=self.tz)

    def alert_rows(self) -> list[dict]:
        return alert_rows(self.alerts, tz=self.tz)

    def watchlist_rows(self) -> list[dict]:
        return watchlist_rows(self.watchlist, tz=self.tz)

    def rejected_rows(self) -> list[dict]:
        return rejected_rows(self.watchlist)

    def watchlist_summary(self) -> dict[str, str]:
        return watchlist_summary(self.watchlist, now_ms=self._clock.now_ms(), tz=self.tz)

    # ------------------------------------------------------------ 拉取

    async def refresh_watchlist(self) -> None:
        """名单页用：向内核要 ``GET /watchlist``。传输失败照样算失联；内核回错误（比如老版本没有这个接口）只记在 errors 里。"""
        if self.closed or self.session_invalid:
            return
        async with self._lock:
            if self.closed or self.session_invalid:
                return
            self._attempt_ms["watchlist"] = self._clock.now_ms()
            try:
                result: Any = await self.api.watchlist(self.session.token)
            except Exception as exc:  # noqa: BLE001 - 分类交给 _absorb
                result = exc
            if self.closed:
                return
            self._absorb([("watchlist", result, self._store)])

    async def refresh_watchlist_if_due(self, *, max_age_s: float) -> bool:
        last = self._attempt_ms.get("watchlist")
        if last is not None and self._clock.now_ms() - last < max_age_s * 1000:
            return False
        await self.refresh_watchlist()
        return True

    async def _refresh_extra(self, part: str, fetch: Callable[[], Any]) -> None:
        """页面专属数据（逐笔交易、统计、K 线）的拉取：与名单同一套规则（传输失败算失联，内核回错误只记在 errors 里）。"""
        if self.closed or self.session_invalid:
            return
        async with self._lock:
            if self.closed or self.session_invalid:
                return
            self._attempt_ms[part] = self._clock.now_ms()
            try:
                result: Any = await fetch()
            except Exception as exc:  # noqa: BLE001 - 分类交给 _absorb
                result = exc
            if self.closed:
                return
            self._absorb([(part, result, self._store)])

    async def _extra_if_due(self, part: str, refresh: Callable[[], Any], max_age_s: float) -> bool:
        last = self._attempt_ms.get(part)
        if last is not None and self._clock.now_ms() - last < max_age_s * 1000:
            return False
        await refresh()
        return True

    async def refresh_trades(self, query: dict[str, Any] | None = None) -> None:
        """逐笔交易页用：``query`` 是 ``GET /trades`` 的筛选参数；给了就记住，之后定时刷新沿用。"""
        if query is not None:
            self.trades_query = dict(query)
        wanted = dict(self.trades_query)
        await self._refresh_extra("trades", lambda: self.api.trades(self.session.token, **wanted))

    async def refresh_trades_if_due(self, *, max_age_s: float) -> bool:
        return await self._extra_if_due("trades", self.refresh_trades, max_age_s)

    async def refresh_stats(self, query: dict[str, Any] | None = None) -> None:
        """统计页用：``query`` 是时间范围（since_ms、until_ms）。按模型、归属、方向、合约各要一次分组，总览与混合仓取第一次的。"""
        if query is not None:
            self.stats_query = dict(query)
        wanted = dict(self.stats_query)

        async def fetch() -> dict:
            token = self.session.token
            results = await asyncio.gather(*(self.api.stats(token, group_by=g, **wanted) for g in STATS_GROUPS),
                                           return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException)]
            if failures:
                raise next((f for f in failures if isinstance(f, KernelUnreachable)), failures[0])
            first = results[0] if isinstance(results[0], dict) else {}
            return {"overall": first.get("overall"), "mixed": first.get("mixed"),
                    "groups": {g: (r.get("groups") if isinstance(r, dict) else None) for g, r in zip(STATS_GROUPS, results)}}

        await self._refresh_extra("stats", fetch)

    async def refresh_stats_if_due(self, *, max_age_s: float) -> bool:
        return await self._extra_if_due("stats", self.refresh_stats, max_age_s)

    async def refresh_candles(self, query: dict[str, Any] | None = None) -> None:
        """K 线页用：``query`` 是 ``{"symbol","tf"}``（可带 limit）。"""
        if query is not None:
            self.candles_query = dict(query)
        wanted = dict(self.candles_query)
        await self._refresh_extra("candles", lambda: self.api.candles(self.session.token, **wanted))

    async def refresh_candles_if_due(self, *, max_age_s: float) -> bool:
        return await self._extra_if_due("candles", self.refresh_candles, max_age_s)

    async def refresh(self, parts: Iterable[str] = PARTS) -> None:
        """重新拉取指定的部分。同一时刻只飞一个批次；要的部分已被排队/在飞的批次覆盖就直接等它。"""
        wanted = frozenset(p for p in parts if p in PARTS)
        if not wanted or self.closed or self.session_invalid:
            return
        task = self._joinable(wanted)
        if task is None:
            task = asyncio.ensure_future(self._locked_batch(wanted))
            self._pending.append((task, wanted))
            task.add_done_callback(self._forget)
        await asyncio.shield(task)  # 调用方被取消（比如页面关了）不影响这个批次跑完，链路状态照常更新

    async def refresh_if_due(self, parts: Iterable[str] = PARTS, *, max_age_s: float) -> bool:
        """只刷新「最近一次尝试距今已不短于 max_age_s」的部分（失败的尝试也算，避免内核挂了时连环敲）。返回是否发起了刷新。"""
        now = self._clock.now_ms()
        wanted = set(parts)
        due = [p for p in PARTS if p in wanted and (p not in self._attempt_ms or now - self._attempt_ms[p] >= max_age_s * 1000)]
        if not due:
            return False
        await self.refresh(due)
        return True

    async def load_more_oplog(self) -> None:
        """翻更旧的一页操作日志（按 before_id）。到头或已达上限就不再请求。"""
        ids = [i for i in map(_item_id, self.oplog) if i is not None]
        if self.closed or self.session_invalid or self.oplog_exhausted or not ids or len(self.oplog) >= OPLOG_CAP:
            return
        before_id = min(ids)

        async def fetch() -> None:
            try:
                page = await self.api.oplog(self.session.token, limit=self._oplog_page, before_id=before_id)
            except Exception as exc:  # noqa: BLE001 - 分类交给 _absorb
                self._absorb([("oplog", exc, self._store_older_oplog)])
            else:
                self._absorb([("oplog", page, self._store_older_oplog)])

        async with self._lock:
            if self.closed or self.session_invalid:
                return
            await fetch()

    async def handle_frame(self, frame: Any) -> None:
        """事件流的一帧：只重新拉它可能改变的部分。"""
        parts = refresh_parts_for_frame(frame)
        if parts:
            await self.refresh(parts)

    async def handle_connected(self) -> None:
        """事件流（重）连上：断线期间错过的帧靠这次全量刷新补上。"""
        await self.refresh(PARTS)

    # ------------------------------------------------------------ 批次

    def _joinable(self, wanted: frozenset[str]) -> asyncio.Future | None:
        for task, parts in reversed(self._pending):
            if not task.done() and wanted <= parts:
                return task
        return None

    def _forget(self, task: asyncio.Future) -> None:
        self._pending = [(t, p) for (t, p) in self._pending if t is not task]

    async def _locked_batch(self, wanted: frozenset[str]) -> None:
        async with self._lock:
            if self.closed or self.session_invalid:
                return
            parts = [p for p in PARTS if p in wanted]
            started = self._clock.now_ms()
            for part in parts:
                self._attempt_ms[part] = started
            results = await asyncio.gather(*(self._fetch(p) for p in parts), return_exceptions=True)
            if self.closed:
                return
            self._absorb([(p, r, self._store) for p, r in zip(parts, results)])

    async def _fetch(self, part: str) -> Any:
        token = self.session.token
        if part == "state":
            return await self.api.state(token)
        if part == "positions":
            return await self.api.positions(token)
        if part == "external":
            return await self.api.positions_external(token)
        if part == "oplog":
            return await self.api.oplog(token, limit=self._oplog_page)
        return await self.api.alerts(token, limit=self._alerts_limit)

    # ------------------------------------------------------------ 结果入库

    def _absorb(self, outcomes: list[tuple[str, Any, Callable[[str, Any], None]]]) -> None:
        unreachable: KernelUnreachable | None = None
        alive = False
        for part, result, store in outcomes:
            if isinstance(result, KernelUnreachable):
                unreachable = unreachable or result
            elif isinstance(result, KernelError):
                alive = True
                if result.is_session_invalid:
                    self._invalidate()
                else:
                    self.errors[part] = result.message or result.code
            elif isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                log.error("读取 %s 时出现未预期的错误", part, exc_info=result)
                self.errors[part] = f"内部错误（{type(result).__name__}）"
            else:
                alive = True
                store(part, result)
        if unreachable is not None:
            self._mark_link(lost=True, reason=unreachable.reason, kind=unreachable.kind)
        elif alive:
            self._mark_link(lost=False)
        if alive:
            self.session.last_seen_ms = self._clock.now_ms()  # 内核收到了带令牌的请求：它那边的闲置计时已重置，本地同步

    def _mark_link(self, *, lost: bool, reason: str = "", kind: str = "connect") -> None:
        before = self.link.status.lost
        after = self.link.record_failure(reason, kind) if lost else self.link.record_ok()
        if before != after.lost:
            self.link_version += 1

    def _store(self, part: str, data: Any) -> None:
        first = part not in self.fetched_ms
        self.fetched_ms[part] = self._clock.now_ms()
        self.errors.pop(part, None)
        if part == "oplog":
            merged = merge_oplog(self.oplog, data)
            changed = merged != self.oplog
            self.oplog = merged
            if first:
                self.oplog_exhausted = not isinstance(data, list) or len(data) < self._oplog_page
        else:
            attr = _ATTRS[part]
            changed = getattr(self, attr) != data
            setattr(self, attr, data)
            if part == "state" and isinstance(data, dict) and isinstance(data.get("env"), str) and data["env"]:
                self.env_hint = data["env"]
        if changed or first:
            self.versions[part] += 1

    def _store_older_oplog(self, part: str, page: Any) -> None:
        self.errors.pop(part, None)
        merged = merge_oplog(self.oplog, page)
        changed = merged != self.oplog
        self.oplog = merged
        self.oplog_exhausted = not isinstance(page, list) or len(page) < self._oplog_page or len(merged) >= OPLOG_CAP
        if changed:
            self.versions["oplog"] += 1

    def _invalidate(self) -> None:
        if not self.session_invalid:
            self.session_invalid = True
            self.link_version += 1
            self._stop_stream()

    # ------------------------------------------------------------ 事件流的生命周期

    def attach(self) -> None:
        """一个页面上线。第一个页面到达时启动事件流。"""
        self._clients += 1
        self._cancel_stop_timer()
        if self._stream_factory is not None and self._stream_task is None and not self.closed and not self.session_invalid:
            task = asyncio.get_running_loop().create_task(self._stream_factory(self).run(), name="console-events")
            task.add_done_callback(self._stream_done)
            self._stream_task = task

    def detach(self) -> None:
        """一个页面下线。最后一个走了之后过一小段宽限期再停事件流（换页不会反复重连）。"""
        self._clients = max(0, self._clients - 1)
        if self._clients > 0:
            return
        if self._grace_s <= 0:
            self._stop_stream()
        else:
            self._cancel_stop_timer()
            self._stop_timer = asyncio.get_running_loop().call_later(self._grace_s, self._stop_if_idle)

    def _stop_if_idle(self) -> None:
        self._stop_timer = None
        if self._clients == 0:
            self._stop_stream()

    def _cancel_stop_timer(self) -> None:
        if self._stop_timer is not None:
            self._stop_timer.cancel()
            self._stop_timer = None

    def _stop_stream(self) -> None:
        task, self._stream_task = self._stream_task, None
        if task is not None:
            task.cancel()

    def _stream_done(self, task: asyncio.Task) -> None:
        if self._stream_task is task:
            self._stream_task = None
        if not task.cancelled() and task.exception() is not None:
            log.error("事件流任务异常退出", exc_info=task.exception())

    def close_nowait(self) -> list[asyncio.Future]:
        """会话结束（退出登录、令牌失效、过期、控制台关闭）：停事件流、取消在途批次，之后不再拉取。返回被取消的任务，要等它们收尾就 await。"""
        self.closed = True
        self._cancel_stop_timer()
        tasks = [t for t in (self._stream_task, *(t for t, _ in self._pending)) if t is not None]
        self._stop_stream()
        for task in tasks:
            task.cancel()
        return tasks

    async def aclose(self) -> None:
        tasks = self.close_nowait()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class HealthProbe:
    """登录页用：没有令牌，只看 GET /health——内核在不在、是什么环境（失联时登录页也能标注「替身环境」）。"""

    def __init__(self, api: KernelApi, clock: Clock) -> None:
        self._api = api
        self.link = LinkMonitor(clock)
        self.env: str | None = None

    async def check(self) -> None:
        try:
            data = await self._api.health()
        except KernelUnreachable as exc:
            self.link.record_failure(exc.reason, exc.kind)
            return
        except KernelError:
            self.link.record_ok()  # 内核回了错误：它活着
            return
        except Exception:  # noqa: BLE001
            log.exception("探测内核时出现未预期的错误")
            return
        self.link.record_ok()
        env = data.get("env") if isinstance(data, dict) else None
        if isinstance(env, str) and env:
            self.env = env
