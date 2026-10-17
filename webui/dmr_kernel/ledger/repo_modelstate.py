"""模型状态（``model_state``，单行 id = 1）与模型切换日志（``model_switch_log``）的仓储函数（第 1 层）。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回 ``records.py`` 的记录或基本类型。

- ``startup``：内核每次启动时调用一次。没有行就建默认行；有行就按「重启规则」重置（闸已停止、操作锁清除、版本 +1），
  行里有读不出的内容时回退到已停止并留下标记。读原始列用文本查询（不经枚举与 JSON 的类型转换），所以损坏的行也读得出来。
- ``apply``：事务内「读当前行 → 交给 ``decide`` 判定 → 写回」。``decide(record)`` 由 ``core.modelstate`` 提供，运行在写线程里、
  事务之内：只做计算（可以抛异常让整个事务回滚），不做 I/O，也不能再调用账本。它返回 ``Change`` 或 ``None``（无变化）。
  版本列与更新时间只由本模块维护；每次真有变化：版本 +1，同一事务里追加一条 ``audit_log``（可选再写一条 ``model_switch_log``）。
- 审计记录的 ``before``/``after`` 只含真正变化的列（枚举写成取值，JSON 写成规范化后的样子）加上 ``version``；``after`` 里另有 ``reason``。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from dmr_kernel.core.types import Gate, Model, OpLock, RunMode, Suppression
from dmr_kernel.ledger import repo_audit
from dmr_kernel.ledger.coltypes import json_dumps, json_loads
from dmr_kernel.ledger.errors import LedgerError
from dmr_kernel.ledger.models import ModelState, ModelSwitchLog
from dmr_kernel.ledger.records import ModelStateRecord, ModelSwitchLogRecord

SINGLE_ID = 1
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_TEXT = 200  # 损坏的内容写进审计与原因之前截断，免得把日志撑大

COLUMNS = ("active_model", "gate", "suppressions", "op_locks", "op_lock_owner", "op_lock_expires_ms", "run_mode",
           "version", "m2_started_ms", "mismatch_since_ms", "risk_detail", "updated_ms")
# apply 可以写的列；version、updated_ms、id 只由本模块维护
UPDATABLE = frozenset({"active_model", "gate", "suppressions", "op_locks", "op_lock_owner", "op_lock_expires_ms",
                       "run_mode", "m2_started_ms", "mismatch_since_ms", "risk_detail"})

ACTION_INIT = "model_state.init"
ACTION_STARTUP = "model_state.startup"
ACTION_CORRUPT = "model_state.corrupt_recovered"
ACTION_INTERRUPTED = "model_state.switch_interrupted"
RESULT_INTERRUPTED = "切换中断"
RESULT_CORRUPT = "已回退到停止"
TARGET = "model_state"
SYSTEM = "system"


@dataclass(frozen=True)
class SwitchLogEntry:
    """要写进 ``model_switch_log`` 的一行（``at_ms`` 由仓储填）。"""

    from_model: str | None
    to_model: str
    mode: str | None
    result: str
    op_id: str | None
    operator: str | None
    detail: str = ""


@dataclass(frozen=True)
class Change:
    """``decide`` 算出的一次变化。``updates`` 里是新值（枚举用 core.types 的成员，列表存取值字符串，JSON 列用普通对象）。

    带 ``switch_log`` 的变化一定提交（哪怕列没变）：切换提交本身就是一件要留痕的事。
    """

    updates: Mapping[str, Any]
    action: str
    reason: str = ""
    operator: str = SYSTEM
    request_id: str | None = None
    switch_log: SwitchLogEntry | None = None


Decide = Callable[[ModelStateRecord], "Change | None"]


# ---------------------------------------------------------------- 读


def read_state(session: Session) -> ModelStateRecord | None:
    """读健康的状态行；没有行返回 None。行里有非法取值时抛异常（启动时的 ``startup`` 负责修复）。"""
    row = session.get(ModelState, SINGLE_ID)
    return None if row is None else ModelStateRecord.from_row(row)


def recent_switch_log(session: Session, *, limit: int = DEFAULT_LIMIT) -> list[ModelSwitchLogRecord]:
    """按编号从新到旧取模型切换日志。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_LIMIT} 之间的整数：{limit!r}")
    stmt = select(ModelSwitchLog).order_by(ModelSwitchLog.id.desc()).limit(limit)
    return [ModelSwitchLogRecord.from_row(row) for row in session.scalars(stmt)]


# ---------------------------------------------------------------- 写：事务内读-判-写


def apply(session: Session, decide: Decide, *, now_ms: int) -> tuple[ModelStateRecord, bool]:
    """返回 ``(写入后的记录, 是否有变化)``。``decide`` 抛出的异常原样抛出，事务整体回滚。"""
    row = session.get(ModelState, SINGLE_ID)
    if row is None:
        raise LedgerError("model_state 还没有记录：先调用 ModelStateService.load()")
    current = ModelStateRecord.from_row(row)
    change = decide(current)
    if change is None:
        return current, False
    unknown = set(change.updates) - UPDATABLE
    if unknown:
        raise ValueError(f"model_state 的这些列不能经 apply 写入：{sorted(unknown)}")
    changed = {key: new for key, new in change.updates.items() if _plain(_stored(current, key)) != _plain(new)}
    if not changed and change.switch_log is None:
        return current, False

    before: dict[str, Any] = {"version": current.version}
    before.update({key: _plain(_stored(current, key)) for key in changed})
    for key, new in changed.items():
        setattr(row, key, new)
    row.version = current.version + 1
    row.updated_ms = now_ms
    session.flush()
    updated = ModelStateRecord.from_row(row)

    after: dict[str, Any] = {"version": updated.version}
    after.update({key: _plain(getattr(updated, key)) for key in changed})
    if change.reason:
        after["reason"] = change.reason
    if change.switch_log is not None:
        _add_switch_log(session, now_ms, change.switch_log)
    repo_audit.append(session, at_ms=now_ms, operator=change.operator, action=change.action, target=TARGET,
                      before=before, after=after, request_id=change.request_id, result="ok")
    return updated, True


# ---------------------------------------------------------------- 启动：建默认行或按重启规则重置


def startup(session: Session, *, now_ms: int) -> tuple[ModelStateRecord, dict[str, Any]]:
    """内核每次启动时调用。返回 ``(重置后的记录, 说明)``。

    说明：``created``（本次新建）、``problems``（读不出的内容，中文）、``interrupted_switch``（启动时发现切换中）、
    ``previous_version``（启动前的版本，读不出或新建时为 None）。
    """
    raw = _read_raw(session)
    if raw is None:
        return _create_default(session, now_ms)
    parsed = _parse(raw)
    interrupted = OpLock.SWITCHING in parsed.op_locks
    previous = parsed.version if parsed.version is not None else 0
    active = parsed.active_model or Model.M1

    risk_detail = dict(parsed.risk_detail)
    if parsed.problems:
        risk_detail.update(corrupt_recovered=True, corrupt_reason="；".join(parsed.problems), corrupt_at_ms=now_ms)
    healed: dict[str, Any] = dict(
        active_model=active, gate=Gate.STOPPED, suppressions=sorted(s.value for s in set(parsed.suppressions)),
        op_locks=[], op_lock_owner=None, op_lock_expires_ms=None, run_mode=parsed.run_mode or RunMode.NORMAL,
        version=previous + 1, m2_started_ms=parsed.m2_started_ms, mismatch_since_ms=parsed.mismatch_since_ms,
        risk_detail=risk_detail, updated_ms=now_ms)
    table = ModelState.__table__
    session.execute(update(table).where(table.c.id == SINGLE_ID).values(**healed))
    record = _must_read(session)

    if parsed.problems:
        repo_audit.append(
            session, at_ms=now_ms, operator=SYSTEM, action=ACTION_CORRUPT, target=TARGET,
            before={"raw": {key: _jsonable(raw[key]) for key in COLUMNS}},
            after={**_summary(record), "problems": list(parsed.problems)}, result=RESULT_CORRUPT)
    else:
        reset = {"gate": parsed.gate, "op_locks": [lock.value for lock in parsed.op_locks],
                 "op_lock_owner": parsed.op_lock_owner, "op_lock_expires_ms": parsed.op_lock_expires_ms}
        now_values = {"gate": Gate.STOPPED, "op_locks": [], "op_lock_owner": None, "op_lock_expires_ms": None}
        moved = [key for key in reset if _plain(reset[key]) != _plain(now_values[key])]
        repo_audit.append(
            session, at_ms=now_ms, operator=SYSTEM, action=ACTION_STARTUP, target=TARGET,
            before={"version": previous, **{key: _plain(reset[key]) for key in moved}},
            after={"version": record.version, **{key: _plain(now_values[key]) for key in moved}}, result="ok")
    if interrupted:
        _record_interruption(session, now_ms, active, parsed, record)
    return record, {"created": False, "problems": list(parsed.problems), "interrupted_switch": interrupted,
                    "previous_version": parsed.version}


def _create_default(session: Session, now_ms: int) -> tuple[ModelStateRecord, dict[str, Any]]:
    session.add(ModelState(id=SINGLE_ID, active_model=Model.M1, gate=Gate.STOPPED, suppressions=[], op_locks=[],
                           op_lock_owner=None, op_lock_expires_ms=None, run_mode=RunMode.NORMAL, version=1,
                           m2_started_ms=None, mismatch_since_ms=None, risk_detail={}, updated_ms=now_ms))
    session.flush()
    record = _must_read(session)
    repo_audit.append(session, at_ms=now_ms, operator=SYSTEM, action=ACTION_INIT, target=TARGET, before=None,
                      after=_summary(record), result="ok")
    return record, {"created": True, "problems": [], "interrupted_switch": False, "previous_version": None}


def _record_interruption(session: Session, now_ms: int, active: Model, parsed: "_Parsed",
                         record: ModelStateRecord) -> None:
    detail = (f"切换中断：内核重启时发现切换尚未提交，生效模型仍为 {active.value}；操作锁已清除，开仓闸已停止。"
              f"请核对持仓后重新发起切换")
    _add_switch_log(session, now_ms, SwitchLogEntry(
        from_model=active.value, to_model="unknown", mode=None, result=RESULT_INTERRUPTED, op_id=None,
        operator=parsed.op_lock_owner, detail=detail))
    repo_audit.append(
        session, at_ms=now_ms, operator=SYSTEM, action=ACTION_INTERRUPTED, target=TARGET,
        before={"op_locks": [lock.value for lock in parsed.op_locks], "op_lock_owner": parsed.op_lock_owner},
        after={"active_model": active.value, "gate": record.gate.value, "op_locks": [], "reason": detail},
        result=RESULT_INTERRUPTED)


def _add_switch_log(session: Session, now_ms: int, entry: SwitchLogEntry) -> None:
    session.add(ModelSwitchLog(at_ms=now_ms, from_model=entry.from_model, to_model=entry.to_model, mode=entry.mode,
                               result=entry.result, op_id=entry.op_id, operator=entry.operator, detail=entry.detail))
    session.flush()


def _must_read(session: Session) -> ModelStateRecord:
    record = read_state(session)
    if record is None:  # pragma: no cover - 刚写完不会读不到
        raise LedgerError("model_state 写入后读不到记录")
    return record


# ---------------------------------------------------------------- 读原始列并容错解析


def _read_raw(session: Session) -> dict[str, Any] | None:
    """用文本查询读原始列：不经枚举与 JSON 的类型转换，损坏的取值也读得出来。"""
    sql = text(f"SELECT {', '.join(COLUMNS)} FROM model_state WHERE id = {SINGLE_ID}")
    row = session.execute(sql).mappings().first()
    return None if row is None else dict(row)


@dataclass
class _Parsed:
    active_model: Model | None = None
    gate: Gate | None = None
    suppressions: list[Suppression] = field(default_factory=list)
    op_locks: list[OpLock] = field(default_factory=list)
    op_lock_owner: str | None = None
    op_lock_expires_ms: int | None = None
    run_mode: RunMode | None = None
    version: int | None = None
    m2_started_ms: int | None = None
    mismatch_since_ms: int | None = None
    risk_detail: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _parse(raw: Mapping[str, Any]) -> _Parsed:
    out = _Parsed()
    problems = out.problems
    out.active_model = _enum(raw["active_model"], Model, "active_model", problems)
    out.gate = _enum(raw["gate"], Gate, "gate", problems)
    out.suppressions = _enum_list(raw["suppressions"], Suppression, "suppressions", problems)
    out.op_locks = _enum_list(raw["op_locks"], OpLock, "op_locks", problems)
    owner = raw["op_lock_owner"]
    if owner is None or isinstance(owner, str):
        out.op_lock_owner = owner
    else:
        problems.append(f"op_lock_owner 不是文本：{_shown(owner)}")
    out.op_lock_expires_ms = _opt_int(raw["op_lock_expires_ms"], "op_lock_expires_ms", problems)
    out.run_mode = _enum(raw["run_mode"], RunMode, "run_mode", problems)
    version = raw["version"]
    if _is_int(version) and version >= 1:
        out.version = version
    else:
        problems.append(f"version 不是正整数：{_shown(version)}")
    out.m2_started_ms = _opt_int(raw["m2_started_ms"], "m2_started_ms", problems)
    out.mismatch_since_ms = _opt_int(raw["mismatch_since_ms"], "mismatch_since_ms", problems)
    out.risk_detail = _risk_detail(raw["risk_detail"], problems)
    if not _is_int(raw["updated_ms"]):
        problems.append(f"updated_ms 不是整数：{_shown(raw['updated_ms'])}")
    return out


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _enum(value: Any, enum_type: type[Enum], name: str, problems: list[str]) -> Any:
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            pass
    problems.append(f"{name} 取值非法：{_shown(value)}")
    return None


def _enum_list(value: Any, enum_type: type[Enum], name: str, problems: list[str]) -> list[Any]:
    """JSON 文本 → 枚举成员列表；非法的项丢掉并记问题，整个 JSON 读不出就是空列表。"""
    if not isinstance(value, str):
        problems.append(f"{name} 不是 JSON 文本：{_shown(value)}")
        return []
    try:
        data = json_loads(value)
    except ValueError:
        problems.append(f"{name} 不是合法的 JSON：{_shown(value)}")
        return []
    if not isinstance(data, list):
        problems.append(f"{name} 不是列表：{_shown(value)}")
        return []
    good: list[Any] = []
    bad: list[Any] = []
    for item in data:
        try:
            if not isinstance(item, str):
                raise ValueError(item)
            good.append(enum_type(item))
        except ValueError:
            bad.append(item)
    if bad:
        problems.append(f"{name} 含非法取值：{_shown(bad)}")
    return good


def _opt_int(value: Any, name: str, problems: list[str]) -> int | None:
    if value is None:
        return None
    if _is_int(value):
        return value
    problems.append(f"{name} 不是整数：{_shown(value)}")
    return None


def _risk_detail(value: Any, problems: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            data = json_loads(value)
        except ValueError:
            problems.append(f"risk_detail 不是合法的 JSON：{_shown(value)}")
            return {}
        if isinstance(data, dict):
            return data
        problems.append(f"risk_detail 不是对象：{_shown(value)}")
        return {}
    problems.append(f"risk_detail 不是 JSON 文本：{_shown(value)}")
    return {}


# ---------------------------------------------------------------- 取值的规范化与展示


def _stored(record: ModelStateRecord, key: str) -> Any:
    value = getattr(record, key)
    return {} if key == "risk_detail" and value is None else value  # 库里允许为空，等同于空字典


def _plain(value: Any) -> Any:
    """规范成「存进库再取出来」的样子：枚举取值，其余按账本统一的 JSON 写法转一圈（Decimal 变字符串、元组变列表）。"""
    if isinstance(value, Enum):
        return value.value
    return json_loads(json_dumps(value))


def _shown(value: Any) -> str:
    text_ = repr(value)
    return text_ if len(text_) <= MAX_TEXT else text_[:MAX_TEXT] + "…"


def _jsonable(value: Any) -> Any:
    """原始列值写进审计之前：整数、文本、空原样（文本截断），其余（浮点、字节等）写成展示文字。"""
    if value is None or _is_int(value):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…"
    return _shown(value)


def _summary(record: ModelStateRecord) -> dict[str, Any]:
    return {"version": record.version, "active_model": record.active_model.value, "gate": record.gate.value,
            "suppressions": list(record.suppressions), "op_locks": list(record.op_locks),
            "run_mode": record.run_mode.value}
