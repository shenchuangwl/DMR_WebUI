"""操作执行器：把「点击 → 请求内核 → 结果」做成一处。每个控制器一个（令牌来自它的会话）。

- 操作号：每次点击一个（``OpIds``）；连不上内核、超时（结果未知）时保留，重试复用同一个，内核据此只执行一次；
  内核给出了明确答复（成功或拒绝）就用掉，下一次点击换新的。
- 结果统一成 ``OpOutcome``：成功、内核拒绝（带代码、中文原因、附加字段如 ``checks``）、失联。
- 会话令牌失效（401）时标记控制器失效，由页面送回登录页。
- 每次调用在控制台日志留一行（路径、操作号、结果；不含口令）。
"""
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dmr_console import operations as O
from dmr_console.client import KernelApi, KernelError, KernelUnreachable
from dmr_console.clock import Clock

log = logging.getLogger(__name__)

SEAT_ACTIONS = ("acquire", "renew", "release", "force")
REFRESH_PARTS = ("state", "positions", "external", "oplog")
HISTORY_LINES = 50


@dataclass(frozen=True)
class OpOutcome:
    ok: bool
    status: str = ""
    result: dict = field(default_factory=dict)
    code: str = ""
    message: str = ""
    extra: dict = field(default_factory=dict)
    unreachable: bool = False
    op_id: str = ""
    payload: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        """给操作员看的一句话（失败时）。"""
        return self.message if self.unreachable else O.error_text(self.code, self.message)


class Actions:
    def __init__(self, controller: Any, api: KernelApi, clock: Clock) -> None:
        self._controller = controller
        self._api = api
        self._clock = clock
        self.ids = O.OpIds()
        self.lease = O.LeaseTracker()
        self._renewed_ms: int | None = None
        self.history: deque[str] = deque(maxlen=HISTORY_LINES)
        self._inflight: set[str] = set()

    async def call(self, slot: str, path: str, build: Callable[[str], dict], *, with_op_id: bool = True) -> OpOutcome:
        # 同一浏览器的多个标签页共用这个控制器：同一个槽的请求还在途时，另一次点击用一个不登记的新号，由内核单独判定
        # （复用同一个号会被内核当成幂等重放，把第一次的结果原样回给第二个标签页）。
        concurrent = with_op_id and slot in self._inflight
        op_id = (O.new_op_id() if concurrent else self.ids.get(slot)) if with_op_id else ""
        body = build(op_id)
        if with_op_id and not concurrent:
            self._inflight.add(slot)
        try:
            outcome = await self._send(path, body, op_id)
        finally:
            if with_op_id and not concurrent:
                self._inflight.discard(slot)
        if with_op_id and not concurrent and not outcome.unreachable:
            self.ids.settle(slot)  # 内核明确答复了：这一次点击结束；结果未知（失联）才留着号给重试用
        if outcome.ok:
            await self._refresh()
        return outcome

    async def set_config(self, key: str, value: Any) -> OpOutcome:
        """改一项配置（``PUT /config/{key}``）。操作号按「键」一个：结果未知（失联）时重试复用，内核答复后换新。"""
        slot = f"config:{key}"
        op_id = self.ids.get(slot)
        outcome = await self._send(f"/config/{key}", {"op_id": op_id, "value": value}, op_id, method="PUT", key=key)
        if not outcome.unreachable:
            self.ids.settle(slot)
        return outcome

    async def fetch_config(self) -> tuple[list[dict] | None, str]:
        """``GET /config`` 的全部项；取不到时返回 ``(None, 中文原因)``。"""
        try:
            return await self._api.config(self._controller.session.token), ""
        except KernelUnreachable as exc:
            return None, exc.reason
        except KernelError as exc:
            self._note_kernel_error(exc)
            return None, O.error_text(exc.code, exc.message)

    async def seat(self, action: str, *, password: str | None = None) -> OpOutcome:
        if action not in SEAT_ACTIONS:
            raise ValueError(f"未知的席位动作：{action!r}")
        body = {"password": password} if action == "force" and password else None
        outcome = await self._send(f"/seat/{action}", body, "")
        if outcome.ok and action in ("acquire", "force", "renew"):
            self._renewed_ms = self._clock.now_ms()  # 刚取得或续过：从现在起算下一次续租
        if not outcome.unreachable:
            await self._refresh()  # 被拒（比如席位已被别人拿走）也要刷新，按钮据此变
        return outcome

    async def renew_if_due(self) -> OpOutcome | None:
        """持有席位时，每隔整条租约（``seat_lease_s``）的一半续一次。多个页面同时在线时共用这一个节拍，不会重复续。"""
        state, now = self._controller.state, self._clock.now_ms()
        self.lease.observe(state, now_ms=now)
        interval = self.lease.interval_s(state, now_ms=now)
        if interval is None:
            self._renewed_ms = None
            return None
        if self._renewed_ms is None:
            self._renewed_ms = now
            return None
        if now - self._renewed_ms < interval * 1000:
            return None
        self._renewed_ms = now
        return await self.seat("renew")

    async def operation(self, op_id: str) -> OpOutcome:
        try:
            data = await self._api.operation(self._controller.session.token, op_id)
        except KernelUnreachable as exc:
            return OpOutcome(False, message=exc.reason, unreachable=True, op_id=op_id)
        except KernelError as exc:
            self._note_kernel_error(exc)
            return OpOutcome(False, code=exc.code, message=exc.message, extra=exc.extra, op_id=op_id)
        return OpOutcome(True, status=O.status_of(data), result=dict(data), op_id=op_id)

    # ------------------------------------------------------------ 内部

    async def _send(self, path: str, body: Any, op_id: str, *, method: str = "POST", key: str = "") -> OpOutcome:
        try:
            if method == "PUT":
                data = await self._api.put_config(self._controller.session.token, key, body)
            else:
                data = await self._api.post(self._controller.session.token, path, body)
        except KernelUnreachable as exc:
            outcome = OpOutcome(False, message=exc.reason, unreachable=True, op_id=op_id)
        except KernelError as exc:
            self._note_kernel_error(exc)
            outcome = OpOutcome(False, code=exc.code, message=exc.message, extra=exc.extra, op_id=op_id)
        else:
            outcome = OpOutcome(True, status=O.status_of(data), result=O.result_of(data), op_id=op_id, payload=dict(data))
        self._note(path, op_id, outcome)
        return outcome

    def _note_kernel_error(self, exc: KernelError) -> None:
        if exc.is_session_invalid:
            self._controller._invalidate()

    def _note(self, path: str, op_id: str, outcome: OpOutcome) -> None:
        if outcome.ok:
            result = outcome.status or "ok"
        elif outcome.unreachable:
            result = "失联（结果未知）"
        else:
            result = f"被拒绝 {outcome.code}"
        line = f"{path} 操作号={op_id or '-'} 结果={result}"
        self.history.append(line)
        log.info("操作 %s", line)

    async def _refresh(self) -> None:
        try:
            await self._controller.refresh(REFRESH_PARTS)
        except Exception:  # noqa: BLE001 - 刷新失败不影响操作结果的呈现
            log.exception("操作后刷新失败")
