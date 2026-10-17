"""操作席位（第 7 层 api）：同一时刻只有一个会话持有，租约 ``seat_lease_s`` 秒，到期自动释放。

- 数据在账本的 ``seat`` 表（单行），判定与改写都在 ``ledger.repo_auth`` 的写事务里，所以不会出现两个持有者；
  租约到期（恰好到点就算）等于没人持有：读的时候直接按到期算，申请的时候也不需要先清理。
- ``expire_due`` 是给定时任务（装配的「席位与切换预览到期」）调用的清理：清掉到期的行、写 ``seat.expired`` 审计、发布事件。
- 席位变化发布 ``SeatChanged``（api 内的本地事件，事件流把它映射成 ``state`` 帧）：acquire、release、force、expired；
  续租、同一持有者重复申请不发布。退出登录放席位由登录服务发布（cause = logout）。
- 本机令牌（主体 ``cli``）不占席位：申请、续租、释放、强制接管一律 409 ``not_allowed``，只能查看。
- 强制接管的口令校验由调用方先做（``AuthService.reauth(..., via="seat_force")``），这里只管换人与留痕。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dmr_kernel.api import errors
from dmr_kernel.api.common import Principal, SeatChanged
from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import EventBus
from dmr_kernel.ledger import repo_auth
from dmr_kernel.ledger.writer import Ledger

__all__ = ["SeatService", "seat_view"]


def seat_view(state: repo_auth.SeatState, principal: Principal | None) -> dict[str, Any]:
    """报文里的 ``seat``：没人持有时前三项为 null、``mine`` 为 false。"""
    mine = (principal is not None and principal.is_session and state.holder_session_id is not None
            and state.holder_session_id == principal.session_id)
    return {"holder_session_id": state.holder_session_id, "holder_operator": state.holder_operator,
            "lease_until_ms": state.lease_until_ms, "mine": mine}


class SeatService:
    def __init__(self, ledger: Ledger, clock: Clock, bus: EventBus, *, config_get: Callable[[str], Any]) -> None:
        self._ledger = ledger
        self._clock = clock
        self._bus = bus
        self._config_get = config_get

    def _lease_ms(self) -> int:
        return int(self._config_get("seat_lease_s")) * 1000

    @staticmethod
    def _need_session(principal: Principal) -> int:
        if not principal.is_session:
            raise errors.not_allowed("本机令牌不占用操作席位，只能查看")
        return principal.session_id

    def _announce(self, holder_session_id: int | None, cause: str, now: int) -> None:
        self._bus.publish(SeatChanged(holder_session_id=holder_session_id, cause=cause, at_ms=now))

    async def view(self, principal: Principal | None = None) -> dict[str, Any]:
        state = await self._ledger.read(repo_auth.read_seat, now_ms=self._clock.now_ms())
        return seat_view(state, principal)

    async def acquire(self, principal: Principal) -> dict[str, Any]:
        """申请席位。没人持有（含租约到期）就给；持有者再申请等于续租；别人持有 409 ``seat_taken``。"""
        session_id = self._need_session(principal)
        now = self._clock.now_ms()
        try:
            outcome = await self._ledger.write(repo_auth.acquire_seat, session_id=session_id, now_ms=now,
                                               lease_ms=self._lease_ms())
        except repo_auth.SessionInactive:
            raise errors.unauthorized("会话已失效，请重新登录") from None
        if not outcome.ok:
            raise errors.seat_taken(outcome.state.holder_operator, outcome.state.lease_until_ms)
        if outcome.changed:
            self._announce(session_id, "acquire", now)
        return seat_view(outcome.state, principal)

    async def renew(self, principal: Principal) -> dict[str, Any]:
        """续租：只对未到期的持有者有效，否则 409 ``seat_required``。"""
        session_id = self._need_session(principal)
        outcome = await self._ledger.write(repo_auth.renew_seat, session_id=session_id, now_ms=self._clock.now_ms(),
                                           lease_ms=self._lease_ms())
        if not outcome.ok:
            raise errors.seat_required("你没有持有操作席位（未申请，或租约已到期）：请先申请席位")
        return seat_view(outcome.state, principal)

    async def release(self, principal: Principal) -> dict[str, Any]:
        """释放席位；不是持有者就什么都不改，返回当前状态。"""
        session_id = self._need_session(principal)
        now = self._clock.now_ms()
        outcome = await self._ledger.write(repo_auth.release_seat, session_id=session_id, now_ms=now)
        if outcome.ok and outcome.changed:
            self._announce(None, "release", now)
        return seat_view(outcome.state, principal)

    async def force(self, principal: Principal, *, request_id: str | None = None) -> dict[str, Any]:
        """强制接管（调用方已当场校验过口令）。不管谁持有都给申请者，写 ``seat.force`` 审计（含接管前后的持有者）。"""
        session_id = self._need_session(principal)
        now = self._clock.now_ms()
        try:
            outcome = await self._ledger.write(repo_auth.force_seat, session_id=session_id, now_ms=now,
                                               lease_ms=self._lease_ms(), request_id=request_id)
        except repo_auth.SessionInactive:
            raise errors.unauthorized("会话已失效，请重新登录") from None
        if outcome.changed:
            self._announce(session_id, "force", now)
        return seat_view(outcome.state, principal)

    async def expire_due(self) -> int | None:
        """清掉到期或持有者已失效的席位，返回丢掉席位的会话号；没有可清的返回 None。"""
        now = self._clock.now_ms()
        lost = await self._ledger.write(repo_auth.expire_seat, now_ms=now)
        if lost is not None:
            self._announce(None, "expired", now)
        return lost
