"""风控闸（第 3 层）：开仓前的账户级与模型级风控判定（报告 4.6「风控闸的口径」）。

数值口径在 ``algo.riskeval``；这里负责数据来源、锁存与复位、持久化、事件与告警。

- 只拦新增风险（``IntentAction.adds_risk``，目前只有开仓）。平仓、部分平仓、止损类（挂、改、撤）、撤单、改杠杆与保证金模式
  一律放行，连账本都不碰，所以账本出了问题也不会挡住减仓与保护。
- 账户级：数据来自持仓对账读回的账户权益（``AccountSnapshotTaken``）。UTC 日（不看本机时区）的第一个有效读数是日初权益，
  峰值取历史最大值。日亏损触发后锁存，到下一个 UTC 日的第一个读数自动复位；回撤触发后一直锁存，只有人工复位
  （``reset_drawdown``，写审计）能解除，复位时以当时的权益作新的峰值。账户级触发拦住所有模型的新增风险。
- 权益快照过期：最近一个有效读数超过 2 × ``reconcile_positions_s`` 秒（或还没收到过）不放行（level="stale"）。
  账户级触发排在过期前面：触发要人来处理，过期只是等下一个读数。
- 模型级（只约束模型 1）：当日（UTC）策略仓已平交易的净盈亏 ≤ −日亏损上限，或当日策略开仓次数 ≥ 日交易次数上限。
  不锁存，每次从账本汇总（``repo_risk.model_day_stats``）；接管仓、模型 3 的仓、待确认仓与外部仓的盈亏和开仓都不计入。
- 限额没配置（None）时没法评估：新增风险一律不放行（账户级限额缺失报 level="account"，模型级限额缺失报 level="model"），
  快照照常记日初权益与峰值，补上限额后的下一个读数按已记下的数值正常判定。
- 状态（UTC 日零点、日初权益、峰值、两个锁存的触发时间与原因）存账本 meta 的 ``risk_state``，重启后恢复；
  读到的状态不合法时 ``load`` 报 ValueError（不能悄悄当成「没有」）。权益读数本身不存：重启后要等第一个新读数才放行新增风险。
- 落盘失败时偏向保守：触发在内存里立即生效，照常发事件和告警，落盘的异常再抛出，下一个读数重试落盘；
  人工复位先落盘（与审计同一个事务）再生效，落盘失败就不生效。
- 事件与告警：账户级或模型级的触发状态变化时发布一条 ``RiskStateChanged``（reasons 是当前全部原因）并告警，
  同一状态不重复发。``load``/``start`` 不发事件；``core.guard`` 启动时应读 ``status()`` 对齐。

``RiskDecision`` 是共享类型（``core.types``），与 ``core.limits`` 返回的是同一个类，上层可以统一判断；本模块里保留这个名字，
``from dmr_kernel.core.riskgate import RiskDecision`` 照常可用。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import TYPE_CHECKING, Any, NamedTuple

from dmr_kernel.algo.riskeval import ModelRisk, evaluate_account, evaluate_model
from dmr_kernel.algo.rounding import EXACT, finite_decimal, to_decimal
from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import AccountSnapshotTaken, EventBus, RiskStateChanged, Subscription
from dmr_kernel.core.types import AlertLevel, IntentAction, Model, RiskDecision
from dmr_kernel.ledger import repo_audit, repo_risk
from dmr_kernel.ledger.writer import Ledger

if TYPE_CHECKING:
    from dmr_kernel.alerts.notifier import Notifier

log = logging.getLogger(__name__)

__all__ = ["DAY_MS", "RESET_DRAWDOWN_ACTION", "RiskDecision", "RiskGate"]

DAY_MS = 86_400_000
RESET_DRAWDOWN_ACTION = "risk.reset_drawdown"  # 审计日志里的动作名
STATE_VERSION = 1
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_HUNDRED = Decimal(100)
_CENT = Decimal("0.01")
_ZERO = Decimal(0)


_ALLOWED = RiskDecision(True)


@dataclass(frozen=True)
class _Latch:
    """一个锁存：是否触发、触发时间、触发时的原因（原因在触发那一刻写定，之后不随权益变化）。"""

    tripped: bool = False
    at_ms: int | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"tripped": self.tripped, "at_ms": self.at_ms, "reason": self.reason}


@dataclass(frozen=True)
class _State:
    """账本里恢复出来的、需要持久化的那部分状态。"""

    day_ms: int | None = None
    day_start: Decimal | None = None
    peak: Decimal | None = None
    daily_loss: _Latch = _Latch()
    drawdown: _Latch = _Latch()


@dataclass(frozen=True)
class _ModelView:
    """最近一次从账本汇总出的模型 1 当日数字与判定（risk 为 None 表示限额没配置，没法判定）。"""

    day_ms: int
    net_pnl: Decimal
    opens: int
    risk: ModelRisk | None

    @property
    def tripped(self) -> bool:
        return self.risk is not None and self.risk.tripped


class _Flags(NamedTuple):
    """已经对外公布的触发状态；任何一项变化就发一次事件。"""

    daily_loss: bool
    drawdown: bool
    model_loss: bool
    model_trades: bool

    @property
    def account(self) -> bool:
        return self.daily_loss or self.drawdown

    @property
    def model(self) -> bool:
        return self.model_loss or self.model_trades


# ---------------------------------------------------------------- 纯函数


def _utc_day_start(ms: int) -> int:
    """UTC 日零点（毫秒）。只做整数运算，和本机时区无关。"""
    return ms - ms % DAY_MS


def _day_text(day_ms: int) -> str:
    return date.fromordinal(_EPOCH_ORDINAL + day_ms // DAY_MS).isoformat()


def _check_ms(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数毫秒：{value!r}")
    return value


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _plain(value: Decimal) -> str:
    """去掉多余的小数零的写法（20.0 -> 20），不用科学计数法。"""
    return format(value.normalize(), "f")


def _decimal(value: Any, what: str) -> Decimal:
    """配置值（Decimal、整数、浮点或数字字符串）换成有限的 Decimal。"""
    if isinstance(value, str):
        try:
            out = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"{what}不是数值：{value!r}") from exc
        if not out.is_finite():
            raise ValueError(f"{what}不是有限数值：{value!r}")
        return out
    return finite_decimal(value, what)


def _valid_equity(value: Any) -> Decimal | None:
    """权益读数：None、NaN、无穷、不为正都是无效读数（交易所读不到时常给 0）。"""
    if value is None:
        return None
    out = to_decimal(value)
    return out if out.is_finite() and out > 0 else None


def _bad_state(what: str) -> ValueError:
    return ValueError(f"meta 里的 risk_state 不合法：{what}")


def _parse_equity(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad_state(f"{name} 必须是数字字符串：{value!r}")
    try:
        out = Decimal(value)
    except InvalidOperation as exc:
        raise _bad_state(f"{name} 不是数字：{value!r}") from exc
    if not out.is_finite() or out <= 0:
        raise _bad_state(f"{name} 必须是有限的正数：{value!r}")
    return out


def _parse_latch(value: Any, name: str) -> _Latch:
    if value is None:
        return _Latch()
    if not isinstance(value, dict):
        raise _bad_state(f"{name} 必须是对象：{value!r}")
    tripped, at_ms, reason = value.get("tripped", False), value.get("at_ms"), value.get("reason", "")
    if not isinstance(tripped, bool):
        raise _bad_state(f"{name}.tripped 必须是布尔值：{tripped!r}")
    if at_ms is not None and (isinstance(at_ms, bool) or not isinstance(at_ms, int)):
        raise _bad_state(f"{name}.at_ms 必须是整数毫秒或空：{at_ms!r}")
    if not isinstance(reason, str):
        raise _bad_state(f"{name}.reason 必须是字符串：{reason!r}")
    if tripped and at_ms is None:
        raise _bad_state(f"{name} 已触发但没有触发时间")
    return _Latch(tripped, at_ms, reason)


def _parse_state(raw: Any) -> _State:
    """校验账本里的 risk_state；任何不合法都报 ValueError（消息里带 risk_state）。"""
    if not isinstance(raw, dict):
        raise _bad_state(f"必须是对象，得到 {type(raw).__name__}")
    version = raw.get("v")
    if type(version) is not int or version != STATE_VERSION:
        raise _bad_state(f"版本 {version!r} 不被支持（需要 {STATE_VERSION}）")
    day_ms = raw.get("day_ms")
    if day_ms is not None and (isinstance(day_ms, bool) or not isinstance(day_ms, int) or day_ms < 0
                               or day_ms % DAY_MS):
        raise _bad_state(f"day_ms 必须是 UTC 日零点的整数毫秒或空：{day_ms!r}")
    day_start = _parse_equity(raw.get("day_start_equity"), "day_start_equity")
    peak = _parse_equity(raw.get("peak_equity"), "peak_equity")
    present = (day_ms is not None, day_start is not None, peak is not None)
    if any(present) and not all(present):
        raise _bad_state("day_ms、day_start_equity、peak_equity 必须同时有或同时没有")
    return _State(day_ms, day_start, peak, _parse_latch(raw.get("daily_loss"), "daily_loss"),
                  _parse_latch(raw.get("drawdown"), "drawdown"))


class RiskGate:
    def __init__(self, ledger: Ledger, bus: EventBus, clock: Clock, *, config_get: Callable[[str], Any],
                 notifier: Notifier | None = None) -> None:
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._config_get = config_get
        self._notifier = notifier
        self._lock = asyncio.Lock()  # 串行化「改状态并落盘」：快照处理、人工复位、加载
        self._subscription: Subscription | None = None
        self._loaded = False
        self._day_ms: int | None = None
        self._day_start: Decimal | None = None
        self._peak: Decimal | None = None
        self._daily_loss = _Latch()
        self._drawdown = _Latch()
        self._equity: Decimal | None = None
        self._equity_at_ms: int | None = None
        self._model: _ModelView | None = None
        self._announced: _Flags | None = None
        self._unsaved = False  # 内存里有还没成功落盘的变化

    # ------------------------------------------------------------ 生命周期

    async def load(self) -> None:
        """从账本恢复状态（每次都重新读）。状态不合法时报 ValueError。恢复后要等第一个新读数才放行新增风险。"""
        async with self._lock:
            raw = await self._ledger.read(repo_risk.load_state)
            state = _State() if raw is None else _parse_state(raw)
            self._day_ms, self._day_start, self._peak = state.day_ms, state.day_start, state.peak
            self._daily_loss, self._drawdown = state.daily_loss, state.drawdown
            self._equity = self._equity_at_ms = None
            self._model = None
            self._unsaved = False
            self._announced = self._flags()
            self._loaded = True
        log.info("风控闸已加载：日期=%s 日亏损锁存=%s 回撤锁存=%s", None if self._day_ms is None else _day_text(self._day_ms),
                 self._daily_loss.tripped, self._drawdown.tripped)

    async def start(self) -> None:
        """订阅账户权益读数。"""
        self._require_loaded()
        if self._subscription is None:
            self._subscription = self._bus.subscribe(AccountSnapshotTaken, self._on_snapshot_event, name="riskgate")

    async def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None

    async def _on_snapshot_event(self, event: AccountSnapshotTaken) -> None:
        await self.on_account_snapshot(event.equity, event.at_ms)

    # ------------------------------------------------------------ 账户权益读数

    async def on_account_snapshot(self, equity: Decimal, at_ms: int) -> None:
        """处理一个账户权益读数：日切、日初权益、峰值、两个锁存，再顺手刷新模型 1 的当日数字，变化时发事件和告警。

        无效的读数（NaN、无穷、不为正）和比已收到的更旧的读数直接忽略，不刷新「最近更新」。
        """
        self._require_loaded()
        _check_ms(at_ms, "at_ms")
        async with self._lock:
            changed = self._apply_reading(equity, at_ms)
            if changed is None:
                return
            self._unsaved = self._unsaved or changed
            assert self._day_ms is not None
            try:
                await self._refresh_model(self._day_ms)
            except Exception:  # 模型级只是顺手刷新：账本读不出来不能耽误账户级的公布与落盘（evaluate 会自己再读）
                log.warning("刷新模型 1 当日汇总失败，沿用上一次的数字", exc_info=True)
            self._announce(at_ms)
            if self._unsaved:
                await self._persist()

    def _apply_reading(self, equity: Decimal, at_ms: int) -> bool | None:
        """把读数记进内存。返回需要持久化的字段是否变了；读数被忽略时返回 None。"""
        reading = _valid_equity(equity)
        if reading is None:
            log.warning("忽略无效的权益读数 %r（时间 %s）", equity, at_ms)
            return None
        if self._equity_at_ms is not None and at_ms < self._equity_at_ms:
            log.info("忽略乱序到达的旧读数（时间 %s 早于已收到的 %s）", at_ms, self._equity_at_ms)
            return None
        day_ms = _utc_day_start(at_ms)
        if self._day_ms is not None and day_ms < self._day_ms:
            log.warning("忽略早于已记日期的读数（时间 %s，已记 %s；时钟回拨？）", at_ms, _day_text(self._day_ms))
            return None
        changed = False
        if self._day_ms is None or day_ms > self._day_ms:  # 当天第一个读数：日初权益，日亏损锁存自动复位
            self._day_ms, self._day_start = day_ms, reading
            if self._daily_loss.tripped:
                self._daily_loss = _Latch()
            changed = True
        if self._peak is None or reading > self._peak:
            self._peak = reading
            changed = True
        self._equity, self._equity_at_ms = reading, at_ms
        return self._update_latches(reading, at_ms) or changed

    def _update_latches(self, equity: Decimal, at_ms: int) -> bool:
        """按当前限额判定；新触发的写进锁存（已触发的不动，原因保持触发那一刻的）。限额没配置就不判定。"""
        loss_limit, max_dd = self._limit("daily_loss_limit"), self._limit("max_drawdown")
        start, peak = self._day_start, self._peak
        if loss_limit is None or max_dd is None or start is None or peak is None:
            return False
        risk = evaluate_account(equity, start, peak, loss_limit, max_dd)
        changed = False
        # riskeval 把两项原因合在一条里；各取一条时，把另一项的上限放到不可能达到：
        # 日亏损 = 日初 − 当前 < 日初（当前权益为正），回撤 < 100%。
        if risk.daily_loss_tripped and not self._daily_loss.tripped:
            reason = evaluate_account(equity, start, peak, loss_limit, _HUNDRED).reason
            self._daily_loss = _Latch(True, at_ms, reason)
            changed = True
        if risk.drawdown_tripped and not self._drawdown.tripped:
            reason = evaluate_account(equity, start, peak, start, max_dd).reason
            self._drawdown = _Latch(True, at_ms, reason)
            changed = True
        return changed

    # ------------------------------------------------------------ 判定

    async def evaluate(self, *, model: str, action: IntentAction, now_ms: int) -> RiskDecision:
        """新增风险的动作能不能做。不新增风险的动作一律放行（不碰账本）。

        顺序：账户级触发 → 账户级限额没配置 → 权益快照没收到或过期 → 模型 1 的模型级（读账本，账本出错时异常原样抛出）。
        """
        action = IntentAction(action)
        _check_ms(now_ms, "now_ms")
        if not action.adds_risk:
            return _ALLOWED
        if not self._loaded:
            return RiskDecision(False, "风控闸尚未加载，不能评估账户级限额，暂不新增风险", "stale")
        if self._daily_loss.tripped or self._drawdown.tripped:
            return RiskDecision(False, "；".join(self._account_reasons()), "account")
        missing = self._missing_limits("daily_loss_limit", "max_drawdown")
        if missing:
            return RiskDecision(False, f"账户级风控限额未配置（{'、'.join(missing)}），不能评估，暂不新增风险", "account")
        if self._equity_at_ms is None:
            return RiskDecision(False, "尚未收到账户权益读数，不能评估账户级限额，暂不新增风险", "stale")
        stale_ms = self._stale_ms()
        age_ms = now_ms - self._equity_at_ms
        if age_ms > stale_ms:
            return RiskDecision(False, f"账户权益快照已过期（最近读数在 {age_ms / 1000:.1f} 秒前，"
                                       f"超过 {_plain(stale_ms / 1000)} 秒），暂不新增风险", "stale")
        if str(model) == Model.M1.value:
            if self._missing_limits("daily_trade_limit"):  # 日亏损上限缺失的话，上面账户级那一步已经拦下了
                return RiskDecision(False, "模型级风控限额未配置（daily_trade_limit），不能评估，模型 1 暂不新增风险", "model")
            view = await self._refresh_model(_utc_day_start(now_ms))
            self._announce(now_ms)
            if view.risk is not None and view.risk.tripped:
                return RiskDecision(False, view.risk.reason, "model")
        return _ALLOWED

    async def _refresh_model(self, day_ms: int) -> _ModelView:
        """从账本汇总模型 1 在该 UTC 日的策略仓净盈亏与开仓次数，并按限额判定。"""
        stats = await self._ledger.read(repo_risk.model_day_stats, day_start_ms=day_ms, day_end_ms=day_ms + DAY_MS)
        pnl, opens = stats["strategy_net_pnl"], stats["strategy_opens"]
        loss_limit, trade_limit = self._limit("daily_loss_limit"), self._trade_limit()
        risk = None if loss_limit is None or trade_limit is None else evaluate_model(pnl, opens, loss_limit, trade_limit)
        self._model = view = _ModelView(day_ms, pnl, opens, risk)
        return view

    # ------------------------------------------------------------ 人工复位

    async def reset_drawdown(self, *, operator: str) -> None:
        """人工复位回撤锁存：以当前权益作新的峰值，状态与审计记录在同一个事务里落盘，落盘成功才生效。

        没有触发时什么都不改（峰值也不动，不能用复位把峰值拉回来绕过回撤限额），只留一条结果为 noop 的审计。
        """
        self._require_loaded()
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("复位回撤必须有明确的操作员")
        operator = operator.strip()
        async with self._lock:
            now = self._clock.now_ms()
            before = self._state_dict()
            if not self._drawdown.tripped:
                await self._ledger.write(repo_audit.append, at_ms=now, operator=operator, action=RESET_DRAWDOWN_ACTION,
                                         target="risk", before=before, after=before, result="noop")
                return
            if self._equity is None:
                raise RuntimeError("还没有收到账户权益读数，无法以当前权益作新的峰值；等下一个读数后再复位")
            after = {**before, "peak_equity": _fmt(self._equity), "drawdown": _Latch().as_dict()}
            await self._ledger.write(repo_risk.save_state_with_audit, after, at_ms=now, operator=operator,
                                     action=RESET_DRAWDOWN_ACTION, before=before, after=after)
            self._peak = self._equity
            self._drawdown = _Latch()
            self._unsaved = False  # 刚落盘的 after 就是当前内存状态（日初与日亏损锁存在加锁期间没变）
            self._announce(now, reset_by=operator)

    # ------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        """当前风控状态（可直接转 JSON）：日初权益、峰值、当前权益、各项余量、触发标志与原因。数字一律是字符串。

        余量 = 距离触发还差多少，不会是负数（已触发的是 0；限额在两个读数之间被调低、还没重新判定时也是 0）。
        """
        loss_limit, max_dd, trade_limit = self._peek_limits()
        equity, start, peak = self._equity, self._day_start, self._peak
        daily_loss: Decimal | None = None
        drawdown: Decimal | None = None  # 精确值；显示时取两位
        if equity is not None and start is not None and peak is not None:
            with localcontext(EXACT):
                daily_loss = start - equity
                drawdown = max((peak - equity) / peak * _HUNDRED, _ZERO)
        loss_left: Decimal | None = None
        dd_left: Decimal | None = None
        with localcontext(EXACT):
            if self._daily_loss.tripped:
                loss_left = _ZERO
            elif daily_loss is not None and loss_limit is not None:
                loss_left = max(loss_limit - daily_loss, _ZERO)
            if self._drawdown.tripped:
                dd_left = _ZERO
            elif drawdown is not None and max_dd is not None:
                dd_left = max(max_dd - drawdown, _ZERO)
            shown_dd = None if drawdown is None else drawdown.quantize(_CENT)
            shown_dd_left = None if dd_left is None else dd_left.quantize(_CENT)
        view = self._model
        trades_left: int | None = None
        model_loss_left: Decimal | None = None
        if view is not None:
            if trade_limit is not None:
                trades_left = max(trade_limit - view.opens, 0)
            if loss_limit is not None:
                with localcontext(EXACT):  # 模型级按净盈亏判定（净盈亏 ≤ −上限才触发），所以余量 = 上限 + 净盈亏，赚了会变大
                    model_loss_left = max(loss_limit + view.net_pnl, _ZERO)
        now = self._clock.now_ms()
        return {
            "loaded": self._loaded,
            "day": None if self._day_ms is None else _day_text(self._day_ms),
            "day_start_equity": _fmt(start),
            "peak_equity": _fmt(peak),
            "equity": _fmt(equity),
            "equity_at_ms": self._equity_at_ms,
            "stale": self._equity_at_ms is None or now - self._equity_at_ms > self._stale_ms(),
            "daily_loss": _fmt(daily_loss),
            "daily_loss_limit": _fmt(loss_limit),
            "daily_loss_remaining": _fmt(loss_left),
            "drawdown_pct": _fmt(shown_dd),
            "max_drawdown_pct": _fmt(max_dd),
            "drawdown_remaining_pct": _fmt(shown_dd_left),
            "strategy_net_pnl_today": None if view is None else _fmt(view.net_pnl),
            "strategy_opens_today": None if view is None else view.opens,
            "model_day": None if view is None else _day_text(view.day_ms),
            "trades_remaining": trades_left,
            "model_loss_remaining": _fmt(model_loss_left),
            "account_tripped": self._daily_loss.tripped or self._drawdown.tripped,
            "daily_loss_tripped": self._daily_loss.tripped,
            "daily_loss_tripped_ms": self._daily_loss.at_ms,
            "drawdown_tripped": self._drawdown.tripped,
            "drawdown_tripped_ms": self._drawdown.at_ms,
            "model_tripped": view is not None and view.tripped,
            "reasons": self._reasons(),
            "limits_missing": self._missing_limits("daily_loss_limit", "max_drawdown", "daily_trade_limit"),
        }

    # ------------------------------------------------------------ 公布：事件与告警

    def _flags(self) -> _Flags:
        risk = None if self._model is None else self._model.risk
        return _Flags(self._daily_loss.tripped, self._drawdown.tripped, risk is not None and risk.loss_tripped,
                      risk is not None and risk.trades_tripped)

    def _account_reasons(self) -> list[str]:
        out = []
        if self._daily_loss.tripped:
            out.append(self._daily_loss.reason or "日亏损已触发（原因没有记录）")
        if self._drawdown.tripped:
            out.append(self._drawdown.reason or "权益峰值回撤已触发（原因没有记录）")
        return out

    def _reasons(self) -> list[str]:
        out = self._account_reasons()
        if self._model is not None and self._model.risk is not None and self._model.risk.tripped:
            out.append(self._model.risk.reason)
        return out

    def _announce(self, at_ms: int, *, reset_by: str | None = None) -> None:
        """触发状态和上次公布的不同就发一条事件，并按变化的项告警；状态没变什么都不做。"""
        now, before = self._flags(), self._announced
        if before is None or now == before:
            return
        self._announced = now
        reasons = tuple(self._reasons())
        log.warning("风控状态变化：账户级=%s 模型级=%s 原因=%s", now.account, now.model, "；".join(reasons) or "无")
        self._bus.publish(RiskStateChanged(account_tripped=now.account, model_tripped=now.model, reasons=reasons,
                                           at_ms=at_ms))
        if now.daily_loss != before.daily_loss:
            if now.daily_loss:
                self._alert(AlertLevel.CRITICAL, "risk_daily_loss_tripped",
                            f"账户级风控触发：{self._daily_loss.reason}。所有模型的新开仓已停止，平仓与保护动作不受影响；"
                            f"到下一个 UTC 日的第一个权益读数自动复位。")
            else:
                self._alert(AlertLevel.INFO, "risk_daily_loss_reset",
                            f"UTC 日切，日亏损锁存已自动复位，新的日初权益 {_fmt(self._day_start)} USDT。")
        if now.drawdown != before.drawdown:
            if now.drawdown:
                self._alert(AlertLevel.CRITICAL, "risk_drawdown_tripped",
                            f"账户级风控触发：{self._drawdown.reason}。所有模型的新开仓已停止，须人工复位后才恢复；"
                            f"平仓与保护动作不受影响。")
            else:
                self._alert(AlertLevel.INFO, "risk_drawdown_reset",
                            f"{reset_by or '系统'} 已人工复位权益回撤锁存，新的权益峰值 {_fmt(self._peak)} USDT。")
        if now.model != before.model:
            if now.model:
                assert self._model is not None and self._model.risk is not None
                self._alert(AlertLevel.WARNING, "risk_model_tripped",
                            f"模型级风控触发（只停模型 1 的新开仓）：{self._model.risk.reason}。")
            else:
                self._alert(AlertLevel.INFO, "risk_model_reset", "模型级风控已恢复，模型 1 可以继续新开仓。")

    def _alert(self, level: AlertLevel, kind: str, message: str) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.raise_alert(level, kind, message)
        except Exception:  # 告警出错不能影响风控状态的处理
            log.exception("发送风控告警失败：%s", kind)

    # ------------------------------------------------------------ 持久化

    def _state_dict(self) -> dict[str, Any]:
        return {"v": STATE_VERSION, "day_ms": self._day_ms, "day_start_equity": _fmt(self._day_start),
                "peak_equity": _fmt(self._peak), "daily_loss": self._daily_loss.as_dict(),
                "drawdown": self._drawdown.as_dict()}

    async def _persist(self) -> None:
        await self._ledger.write(repo_risk.save_state, self._state_dict())
        self._unsaved = False

    # ------------------------------------------------------------ 配置与杂项

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("风控闸尚未加载：先调用 load()")

    def _limit(self, key: str) -> Decimal | None:
        raw = self._config_get(key)
        return None if raw is None else _decimal(raw, key)

    def _trade_limit(self) -> int | None:
        value = self._limit("daily_trade_limit")
        if value is None:
            return None
        if value != value.to_integral_value():
            raise ValueError(f"daily_trade_limit 必须是整数：{value}")
        return int(value)

    def _peek_limits(self) -> tuple[Decimal | None, Decimal | None, int | None]:
        return self._limit("daily_loss_limit"), self._limit("max_drawdown"), self._trade_limit()

    def _missing_limits(self, *keys: str) -> list[str]:
        return [key for key in keys if self._config_get(key) is None]

    def _stale_ms(self) -> Decimal:
        """权益快照多久没更新算过期：2 × 持仓对账间隔。"""
        return _decimal(self._config_get("reconcile_positions_s"), "reconcile_positions_s") * 2000
