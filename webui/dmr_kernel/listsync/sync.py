"""名单同步（F10 + 附录 C）：拉取 onlycoin 名单、判定有效、解析成员、转成交易所合约名、落账本、发事件。

采用条件三项同时成立：HTTP 200、``valid=true``、当前 UTC 早于 ``valid_until_utc``。方向取成员对象的 ``direction``；
``symbols`` 数组与成员自带的 ``expires_at_utc`` 不用；成员来自 ``onlycoin``、``long``、``short`` 三个数组（按代码去重）。
合约名用原始代码在交易所合约清单里精确查表，查不到或非交易状态的进 ``rejected``。分数取成员的 ``total_score``。

无效（含单次新增过多被拒收）时 ``current()`` 的 ``valid`` 为 False，``members`` 沿用上一份被采用的快照，直到它自己的
``valid_until_utc`` 过去，之后为空；这个过期判断每次 ``current()`` 都按当前时间重算，不依赖下一次拉取。
「单次新增」以上一份被采用的快照为基线，同一业务日内才比较（启动后第一份、换了业务日都没有基线，不拒收；业务日缺失时照样比较）。
设了成员有效时长（``list_member_ttl_h`` > 0）而成员没有首次入选时间时，该成员按过期处理。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import EventBus, ListSnapshotUpdated
from dmr_kernel.core.types import AlertLevel, PositionSide
from dmr_kernel.exchange.rules import InstrumentRules
from dmr_kernel.ledger import repo_listsync
from dmr_kernel.ledger.repo_listsync import MemberRow
from dmr_kernel.listsync.client import AiohttpListClient, ListFetchError

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 10.0
_ARRAYS = ("onlycoin", "long", "short")
_HOUR_MS = 3_600_000
_MIN_MS = 60_000


@dataclass(frozen=True)
class ListMember:
    symbol: str
    direction: PositionSide
    score: Decimal
    first_selected_ms: int | None
    raw_code: str


@dataclass(frozen=True)
class ListView:
    valid: bool
    reason: str
    snapshot_id: int | None
    fetched_ms: int | None
    valid_until_ms: int | None
    business_date: str | None
    status: str | None
    members: tuple[ListMember, ...]
    rejected: tuple[tuple[str, str], ...]


class HttpGetter(Protocol):
    async def get_json(self, url: str, *, timeout_s: float) -> tuple[int, Any]: ...

    async def close(self) -> None: ...


@dataclass
class _Adopted:
    snapshot_id: int | None
    valid_until_ms: int
    business_date: str | None
    status: str | None
    members: tuple[ListMember, ...]
    rejected: tuple[tuple[str, str], ...]
    raw_codes: frozenset[str]


@dataclass
class _Parsed:
    business_date: str | None = None
    scan_id: str | None = None
    status: str | None = None
    valid_until_ms: int | None = None
    members: list[ListMember] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    rows: list[MemberRow] = field(default_factory=list)
    raw_codes: set[str] = field(default_factory=set)


def _parse_utc_ms(text: Any) -> int | None:
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1000)


class ListSync:
    def __init__(self, *, url: str, ledger, bus: EventBus, clock: Clock, config_get: Callable[[str], Any],
                 rules: InstrumentRules, notifier, http: HttpGetter | None = None):
        if "?" in url or "#" in url:
            raise ValueError(f"名单接口不允许带查询参数：{url!r}")
        self._url = url
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._config_get = config_get
        self._rules = rules
        self._notifier = notifier
        self._http: HttpGetter = http if http is not None else AiohttpListClient()
        self._good: _Adopted | None = None
        self._reason = "尚未拉取名单"  # 最近一次拉取的无效原因；被采用则为空
        self._attempt_ms: int | None = None
        self._invalid_since_ms: int | None = None

    # ------------------------------------------------------------ 对外

    def current(self) -> ListView:
        now = self._clock.now_ms()
        good = self._good
        if good is None:
            return ListView(False, self._reason, None, self._attempt_ms, None, None, None, (), ())
        if now >= good.valid_until_ms:
            reason = (f"{self._reason}（沿用的快照已过 valid_until_utc）" if self._reason
                      else "已过 valid_until_utc")
            return ListView(False, reason, good.snapshot_id, self._attempt_ms, good.valid_until_ms,
                            good.business_date, good.status, (), good.rejected)
        members = tuple(m for m in good.members if self._alive(m, now))
        return ListView(not self._reason, self._reason, good.snapshot_id, self._attempt_ms, good.valid_until_ms,
                        good.business_date, good.status, members, good.rejected)

    def member(self, symbol: str) -> ListMember | None:
        for m in self.current().members:
            if m.symbol == symbol:
                return m
        return None

    async def poll(self) -> ListView:
        now = self._clock.now_ms()
        status_code: int | None = None
        body: Any = None
        reason = ""
        try:
            status_code, body = await self._http.get_json(self._url, timeout_s=FETCH_TIMEOUT_S)
        except (ListFetchError, OSError) as exc:
            reason = f"接口不可达：{exc}"
        except Exception as exc:  # 意外异常也必须让名单立刻无效，不能沿用上一份当有效
            log.error("名单拉取出现意外异常：%r", exc)
            reason = f"接口不可达：{type(exc).__name__}"
        parsed = _Parsed()
        response_valid: bool | None = None
        too_many: tuple[int, int] | None = None
        if not reason:
            reason, response_valid = self._check(status_code, body, now)
        if not reason:
            parsed = self._parse(body)
            too_many = self._new_overflow(parsed)
            if too_many is not None:
                reason = f"单次新增 {too_many[0]} 个币种，超过上限 {too_many[1]}，整份名单被拒收"
        elif isinstance(body, dict) and status_code == 200:
            parsed.business_date = body.get("business_date") if isinstance(body.get("business_date"), str) else None
            parsed.scan_id = body.get("as_of_scan_id") if isinstance(body.get("as_of_scan_id"), str) else None
            parsed.status = body.get("status") if isinstance(body.get("status"), str) else None
            parsed.valid_until_ms = _parse_utc_ms(body.get("valid_until_utc"))
        accepted = not reason

        snapshot_id = await self._persist(now, status_code, response_valid, parsed, accepted, reason)
        if accepted:
            self._good = _Adopted(snapshot_id, parsed.valid_until_ms, parsed.business_date, parsed.status,
                                  tuple(parsed.members), tuple(parsed.rejected), frozenset(parsed.raw_codes))
            self._invalid_since_ms = None
        else:
            if self._invalid_since_ms is None:
                self._invalid_since_ms = now
            if too_many is not None:
                self._alert("list_too_many_new", reason, f"list_too_many_new:{parsed.business_date}")
            self._alert_if_invalid_too_long(now, reason)
        self._reason = reason
        self._attempt_ms = now
        view = self.current()
        if snapshot_id is not None:
            self._bus.publish(ListSnapshotUpdated(snapshot_id=snapshot_id, valid=view.valid, at_ms=now))
        return view

    async def close(self) -> None:
        await self._http.close()

    # ------------------------------------------------------------ 判定与解析

    def _alive(self, member: ListMember, now: int) -> bool:
        ttl_h = int(self._config_get("list_member_ttl_h"))
        if ttl_h <= 0:
            return True  # 0 表示当日有效：由快照自己的 valid_until 约束
        if member.first_selected_ms is None:
            return False  # 设了有效时长却没有首次入选时间：无法判断，按过期处理
        return now < member.first_selected_ms + ttl_h * _HOUR_MS

    @staticmethod
    def _check(status_code: int | None, body: Any, now: int) -> tuple[str, bool | None]:
        if status_code != 200:
            return f"HTTP {status_code}", None
        if not isinstance(body, dict):
            return "响应不是有效的 JSON 对象", None
        flag = body.get("valid")
        valid = flag if isinstance(flag, bool) else None
        if valid is not True:
            return "接口返回 valid=false", valid
        until = _parse_utc_ms(body.get("valid_until_utc"))
        if until is None:
            return "响应缺少有效的 valid_until_utc", valid
        if now >= until:
            return "已过 valid_until_utc", valid
        return "", valid

    def _parse(self, body: dict) -> _Parsed:
        out = _Parsed(business_date=body.get("business_date") if isinstance(body.get("business_date"), str) else None,
                      scan_id=body.get("as_of_scan_id") if isinstance(body.get("as_of_scan_id"), str) else None,
                      status=body.get("status") if isinstance(body.get("status"), str) else None,
                      valid_until_ms=_parse_utc_ms(body.get("valid_until_utc")))
        seen: dict[str, dict] = {}
        conflicts: set[str] = set()
        for name in _ARRAYS:
            items = body.get(name)
            if not isinstance(items, list):
                continue
            for item in items:
                code = item.get("symbol") if isinstance(item, dict) else None
                if not isinstance(code, str) or not code:
                    continue
                if code not in seen:
                    seen[code] = item
                elif seen[code].get("direction") != item.get("direction"):
                    conflicts.add(code)
        for code, item in seen.items():
            out.raw_codes.add(code)
            direction_text = item.get("direction")
            direction = {"LONG": PositionSide.LONG, "SHORT": PositionSide.SHORT}.get(direction_text)
            score = self._score(item.get("total_score"))
            first_ms = _parse_utc_ms(item.get("first_selected_at"))
            problem = ""
            instrument = None
            if code in conflicts:
                problem = "多个数组里的方向不一致"
            elif direction is None:
                problem = f"方向无效：{direction_text!r}"
            elif score is None:
                problem = "缺少有效的 total_score"
            else:
                instrument = self._rules.lookup_list_code(code)
                if instrument is None:
                    known = self._rules.get(code)
                    problem = ("交易所合约清单里查不到" if known is None else
                               f"非交易状态（{known.status}/{known.contract_type}/{known.quote}）")
            if problem:
                out.rejected.append((code, problem))
            else:
                out.members.append(ListMember(instrument.symbol, direction, score, first_ms, code))
            out.rows.append(MemberRow(code, direction.value if direction else None, score, first_ms,
                                      not problem, problem))
        out.members.sort(key=lambda m: (-m.score, m.symbol))
        return out

    @staticmethod
    def _score(value: Any) -> Decimal | None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            score = Decimal(str(value))
        except InvalidOperation:
            return None
        return score if score.is_finite() else None

    def _new_overflow(self, parsed: _Parsed) -> tuple[int, int] | None:
        base = self._good
        if base is None:
            return None
        if (base.business_date is not None and parsed.business_date is not None
                and base.business_date != parsed.business_date):
            return None  # 换了业务日才不比较；业务日缺失时照样按上一份比较
        limit = int(self._config_get("list_max_new_per_poll"))
        new = len(parsed.raw_codes - base.raw_codes)
        return (new, limit) if new > limit else None

    # ------------------------------------------------------------ 落账与告警

    async def _persist(self, now: int, http_status: int | None, response_valid: bool | None, parsed: _Parsed,
                       accepted: bool, reason: str) -> int | None:
        try:
            record = await self._ledger.write(
                repo_listsync.insert_snapshot, fetched_ms=now, http_status=http_status,
                business_date=parsed.business_date, scan_id=parsed.scan_id, valid=response_valid,
                valid_until_ms=parsed.valid_until_ms, status=parsed.status, accepted=accepted, reason=reason,
                members=parsed.rows)
        except Exception as exc:  # 账本写失败不能让名单同步停摆：照常采用，留痕丢失用告警说明
            log.error("名单快照写账本失败：%s", exc)
            self._alert("list_ledger_write_failed", f"名单快照写账本失败：{type(exc).__name__}", "list_ledger_write_failed")
            return None
        return record.id

    def _alert_if_invalid_too_long(self, now: int, reason: str) -> None:
        since = self._invalid_since_ms
        minutes = int(self._config_get("list_invalid_alert_min"))
        if since is not None and now - since >= minutes * _MIN_MS:
            lasted = (now - since) // _MIN_MS
            self._alert("list_invalid", f"名单已连续无效 {lasted} 分钟：{reason}", "list_invalid")

    def _alert(self, kind: str, message: str, dedupe_key: str) -> None:
        try:
            self._notifier.raise_alert(AlertLevel.WARNING, kind, message, dedupe_key=dedupe_key)
        except Exception as exc:  # 告警器是尽力而为
            log.error("名单告警发送失败：%s", exc)
