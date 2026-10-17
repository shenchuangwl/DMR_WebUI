"""登录、会话与操作席位的仓储函数：operator、session、seat 三张表（docs/设计约定.md 第 8、10 节）。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录、本模块的小数据类或基本类型。
谁是操作员一律取自账本里的会话与操作员行（不信调用方传来的名字），所以审计里的 operator 总是真的那一位。

- 操作员：``create_operator``（用户名区分大小写、唯一，写 ``operator.create`` 审计，审计里没有口令哈希）。
- 会话：只存令牌的哈希。``create_session`` 同一事务里写 ``auth.login`` 审计；``end_session`` 撤销会话、顺带放掉它持有的席位、
  写 ``auth.logout`` 审计（已撤销的再撤销不重复记）；``touch_session`` 只往前推活动时间；``set_reauth`` 记最近一次口令校验。
- 席位（单行表，id = 1）：租约到期（``lease_until_ms <= now_ms``，恰好到点就算到期）、持有会话已撤销或操作员已停用，都等于没人持有。
  「读当前行 → 判定 → 写回」整个在写事务里，申请由唯一的写线程串行，所以同一时刻只会有一个持有者。
  ``acquire_seat``（持有者再申请等于续租）、``renew_seat``（只对未到期的持有者）、``release_seat``、``force_seat``（强制接管，
  写 ``seat.force`` 审计，含接管前后的持有者）、``expire_seat``（清掉到期或失效的行，写 ``seat.expired`` 审计，返回丢掉席位的会话号）。
  续租不写审计；申请、释放、强制、到期各写一条，目标都是 ``seat``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from dmr_kernel.ledger import repo_audit
from dmr_kernel.ledger.models import Operator, OperatorSession, Seat
from dmr_kernel.ledger.records import OperatorRecord, SessionRecord

SEAT_ID = 1
SYSTEM = "system"
MAX_CLIENT_INFO = 200

__all__ = [
    "EndResult", "OperatorExists", "SEAT_ID", "SeatOutcome", "SeatState", "SessionContext", "SessionInactive",
    "acquire_seat", "create_operator", "create_session", "end_session", "expire_seat", "force_seat", "get_operator",
    "get_operator_by_name", "get_session_context", "list_operators", "read_seat", "release_seat", "renew_seat",
    "set_reauth", "touch_session",
]


class OperatorExists(ValueError):
    """用户名已被占用（区分大小写）。"""


class SessionInactive(LookupError):
    """会话不存在、已撤销，或它的操作员已停用。"""


@dataclass(frozen=True)
class SessionContext:
    session: SessionRecord
    operator: OperatorRecord


@dataclass(frozen=True)
class SeatState:
    """席位的当前状态；没人持有（含租约到期、持有者会话已失效）时四项都是 None。"""

    holder_session_id: int | None = None
    holder_operator: str | None = None
    lease_until_ms: int | None = None
    acquired_ms: int | None = None


@dataclass(frozen=True)
class SeatOutcome:
    """席位操作的结果。``ok`` 是请求有没有成功；``changed`` 是持有者有没有换人（续租不算）；
    ``previous_session_id`` 是被换掉的上一位有效持有者。``state`` 是操作之后的状态。"""

    ok: bool
    state: SeatState
    changed: bool = False
    previous_session_id: int | None = None


@dataclass(frozen=True)
class EndResult:
    revoked: bool
    seat_released: bool


# ---------------------------------------------------------------- 操作员


def create_operator(session: Session, *, username: str, password_hash: str, role: str, now_ms: int,
                    created_by: str = SYSTEM) -> OperatorRecord:
    if not isinstance(username, str) or not username:
        raise ValueError("用户名不能为空")
    if session.scalars(select(Operator).where(Operator.username == username)).first() is not None:
        raise OperatorExists(f"用户名已存在：{username}")
    row = Operator(username=username, password_hash=password_hash, role=role, created_ms=now_ms, disabled=False)
    session.add(row)
    session.flush()
    repo_audit.append(session, at_ms=now_ms, operator=created_by, action="operator.create",
                      target=f"operator:{username}", after={"username": username, "role": role}, result="ok")
    return OperatorRecord.from_row(row)


def get_operator(session: Session, operator_id: int) -> OperatorRecord | None:
    row = session.get(Operator, operator_id)
    return None if row is None else OperatorRecord.from_row(row)


def get_operator_by_name(session: Session, username: str) -> OperatorRecord | None:
    row = session.scalars(select(Operator).where(Operator.username == username)).first()
    return None if row is None else OperatorRecord.from_row(row)


def list_operators(session: Session) -> list[OperatorRecord]:
    return [OperatorRecord.from_row(row) for row in session.scalars(select(Operator).order_by(Operator.id))]


# ---------------------------------------------------------------- 会话


def _active(session: Session, session_id: int) -> tuple[OperatorSession, Operator] | None:
    """有效的会话和它的操作员：会话存在且没撤销，操作员存在且没停用。"""
    row = session.get(OperatorSession, session_id)
    if row is None or row.revoked:
        return None
    op = session.get(Operator, row.operator_id)
    if op is None or op.disabled:
        return None
    return row, op


def _require_active(session: Session, session_id: int) -> tuple[OperatorSession, Operator]:
    found = _active(session, session_id)
    if found is None:
        raise SessionInactive(f"会话 {session_id} 不存在或已失效")
    return found


def create_session(session: Session, *, token_hash: str, operator_id: int, now_ms: int,
                   client_info: str = "") -> SessionRecord:
    """登录成功后建会话（只存令牌的哈希），同一事务里写 ``auth.login`` 审计。操作员不存在或已停用抛 ``LookupError``。"""
    op = session.get(Operator, operator_id)
    if op is None or op.disabled:
        raise LookupError(f"操作员 {operator_id} 不存在或已停用")
    row = OperatorSession(token_hash=token_hash, operator_id=operator_id, created_ms=now_ms, last_active_ms=now_ms,
                          reauth_ms=None, revoked=False, client_info=client_info[:MAX_CLIENT_INFO])
    session.add(row)
    session.flush()
    repo_audit.append(session, at_ms=now_ms, operator=op.username, action="auth.login", target=f"session:{row.id}",
                      after={"session_id": row.id}, result="ok")
    return SessionRecord.from_row(row)


def get_session_context(session: Session, token_hash: str) -> SessionContext | None:
    row = session.scalars(select(OperatorSession).where(OperatorSession.token_hash == token_hash)).first()
    if row is None:
        return None
    op = session.get(Operator, row.operator_id)
    if op is None:
        return None
    return SessionContext(SessionRecord.from_row(row), OperatorRecord.from_row(op))


def touch_session(session: Session, session_id: int, *, now_ms: int) -> None:
    """推进最近活动时间；只往前（乱序到达的旧时间不倒退），已撤销的不动。"""
    session.execute(update(OperatorSession)
                    .where(OperatorSession.id == session_id, OperatorSession.revoked.is_(False),
                           OperatorSession.last_active_ms < now_ms)
                    .values(last_active_ms=now_ms))


def set_reauth(session: Session, session_id: int, *, now_ms: int, via: str) -> None:
    """记最近一次口令校验的时间并写 ``auth.reauth`` 审计（``via``：reauth 或 seat_force）。会话已失效抛 ``SessionInactive``。"""
    row, op = _require_active(session, session_id)
    row.reauth_ms = now_ms
    session.flush()
    repo_audit.append(session, at_ms=now_ms, operator=op.username, action="auth.reauth", target=f"session:{row.id}",
                      after={"via": via}, result="ok")


def end_session(session: Session, session_id: int, *, now_ms: int, reason: str) -> EndResult:
    """撤销会话；它持有席位就一并放掉。写 ``auth.logout`` 审计（``reason``：logout 或 idle）。
    会话不存在或早已撤销：什么都不做、不写审计。"""
    row = session.get(OperatorSession, session_id)
    if row is None or row.revoked:
        return EndResult(revoked=False, seat_released=False)
    op = session.get(Operator, row.operator_id)
    row.revoked = True
    seat = session.get(Seat, SEAT_ID)
    released = seat is not None and seat.session_id == session_id
    if released:
        _clear(seat)
    session.flush()
    after: dict[str, Any] = {"reason": reason}
    if released:
        after["seat_released"] = True
    repo_audit.append(session, at_ms=now_ms, operator=op.username if op is not None else SYSTEM,
                      action="auth.logout", target=f"session:{row.id}", after=after, result="ok")
    return EndResult(revoked=True, seat_released=released)


# ---------------------------------------------------------------- 席位


def _clear(seat: Seat) -> None:
    seat.session_id = seat.lease_until_ms = seat.acquired_ms = None


def _holder(session: Session, seat: Seat | None, now_ms: int) -> tuple[OperatorSession, Operator] | None:
    """席位当前的有效持有者；租约到期（含恰好到点）或会话失效就是没人。"""
    if seat is None or seat.session_id is None or seat.lease_until_ms is None or seat.lease_until_ms <= now_ms:
        return None
    return _active(session, seat.session_id)


def _state(seat: Seat | None, holder: tuple[OperatorSession, Operator] | None) -> SeatState:
    if seat is None or holder is None:
        return SeatState()
    return SeatState(holder_session_id=seat.session_id, holder_operator=holder[1].username,
                     lease_until_ms=seat.lease_until_ms, acquired_ms=seat.acquired_ms)


def _brief(state: SeatState) -> dict[str, Any]:
    return {"holder_session_id": state.holder_session_id, "holder_operator": state.holder_operator,
            "lease_until_ms": state.lease_until_ms}


def _take(session: Session, seat: Seat | None, *, session_id: int, lease_until_ms: int, acquired_ms: int) -> Seat:
    if seat is None:
        seat = Seat(id=SEAT_ID, session_id=session_id, lease_until_ms=lease_until_ms, acquired_ms=acquired_ms)
        session.add(seat)
    else:
        seat.session_id, seat.lease_until_ms, seat.acquired_ms = session_id, lease_until_ms, acquired_ms
    session.flush()
    return seat


def _check_lease(lease_ms: int) -> None:
    if isinstance(lease_ms, bool) or not isinstance(lease_ms, int) or lease_ms <= 0:
        raise ValueError(f"租约必须是正整数毫秒：{lease_ms!r}")


def read_seat(session: Session, *, now_ms: int) -> SeatState:
    seat = session.get(Seat, SEAT_ID)
    return _state(seat, _holder(session, seat, now_ms))


def acquire_seat(session: Session, *, session_id: int, now_ms: int, lease_ms: int) -> SeatOutcome:
    """申请席位。没人持有（含到期、失效）就给；持有者自己再申请等于续租；别人持有则失败，返回持有者。"""
    _check_lease(lease_ms)
    _, op = _require_active(session, session_id)
    seat = session.get(Seat, SEAT_ID)
    holder = _holder(session, seat, now_ms)
    current = _state(seat, holder)
    if holder is not None and seat.session_id != session_id:
        return SeatOutcome(ok=False, state=current)
    renewing = holder is not None  # 此时持有者就是申请者
    previous = seat.session_id if (seat is not None and seat.session_id not in (None, session_id)) else None
    seat = _take(session, seat, session_id=session_id, lease_until_ms=now_ms + lease_ms,
                 acquired_ms=seat.acquired_ms if renewing else now_ms)
    state = SeatState(session_id, op.username, seat.lease_until_ms, seat.acquired_ms)
    if not renewing:
        repo_audit.append(session, at_ms=now_ms, operator=op.username, action="seat.acquire", target="seat",
                          before=_brief(current), after=_brief(state), result="ok")
    return SeatOutcome(ok=True, state=state, changed=not renewing, previous_session_id=previous)


def renew_seat(session: Session, *, session_id: int, now_ms: int, lease_ms: int) -> SeatOutcome:
    """续租：只对未到期的持有者有效；到期后即使没人接手也要重新申请。"""
    _check_lease(lease_ms)
    seat = session.get(Seat, SEAT_ID)
    holder = _holder(session, seat, now_ms)
    if holder is None or seat.session_id != session_id:
        return SeatOutcome(ok=False, state=_state(seat, holder))
    seat.lease_until_ms = now_ms + lease_ms
    session.flush()
    return SeatOutcome(ok=True, state=_state(seat, holder))


def release_seat(session: Session, *, session_id: int, now_ms: int) -> SeatOutcome:
    """释放：只有有效持有者能放；别人调用或没人持有时什么都不改（ok=False）。"""
    seat = session.get(Seat, SEAT_ID)
    holder = _holder(session, seat, now_ms)
    current = _state(seat, holder)
    if holder is None or seat.session_id != session_id:
        return SeatOutcome(ok=False, state=current)
    _clear(seat)
    session.flush()
    repo_audit.append(session, at_ms=now_ms, operator=holder[1].username, action="seat.release", target="seat",
                      before=_brief(current), after=_brief(SeatState()), result="ok")
    return SeatOutcome(ok=True, state=SeatState(), changed=True)


def force_seat(session: Session, *, session_id: int, now_ms: int, lease_ms: int,
               request_id: str | None = None) -> SeatOutcome:
    """强制接管：不管谁持有都给申请者。写 ``seat.force`` 审计，含接管前后的持有者。口令校验在调用方（api 层）做。"""
    _check_lease(lease_ms)
    _, op = _require_active(session, session_id)
    seat = session.get(Seat, SEAT_ID)
    holder = _holder(session, seat, now_ms)
    current = _state(seat, holder)
    same = holder is not None and seat.session_id == session_id
    seat = _take(session, seat, session_id=session_id, lease_until_ms=now_ms + lease_ms,
                 acquired_ms=seat.acquired_ms if same else now_ms)
    state = SeatState(session_id, op.username, seat.lease_until_ms, seat.acquired_ms)
    repo_audit.append(session, at_ms=now_ms, operator=op.username, action="seat.force", target="seat",
                      before=_brief(current), after=_brief(state), request_id=request_id, result="ok")
    previous = current.holder_session_id if not same else None
    return SeatOutcome(ok=True, state=state, changed=not same, previous_session_id=previous)


def expire_seat(session: Session, *, now_ms: int) -> int | None:
    """清掉到期或持有者已失效的席位行，写 ``seat.expired`` 审计，返回丢掉席位的会话号；没有可清的返回 None。"""
    seat = session.get(Seat, SEAT_ID)
    if seat is None or seat.session_id is None or _holder(session, seat, now_ms) is not None:
        return None
    lost = seat.session_id
    before = {"holder_session_id": lost, "holder_operator": None, "lease_until_ms": seat.lease_until_ms}
    row = session.get(OperatorSession, lost)
    if row is not None:
        op = session.get(Operator, row.operator_id)
        before["holder_operator"] = None if op is None else op.username
    _clear(seat)
    session.flush()
    repo_audit.append(session, at_ms=now_ms, operator=SYSTEM, action="seat.expired", target="seat", before=before,
                      after=_brief(SeatState()), result="ok")
    return lost
