"""替身交易所的撮合引擎（docs/设计约定.md 7.2、7.6）。

纯内存；时钟（返回 UTC 毫秒的可调用对象）、行情源（``feed`` 模块的接口）、开关（``faults.Switches``）都由构造参数注入。
引擎不做任何 I/O，也不自己计时：分发器每秒调 ``check_triggers()`` 与 ``maybe_settle_funding()``，测试直接调。

- 账户：单一 USDT 钱包；对冲模式（LONG/SHORT 两侧独立）；默认杠杆 20、全仓。保证金模式只是记录的属性
  （逐仓不单独核算保证金，只用于「有仓/有挂单不能改」的校验）。
- 市价单按当前最新价（价格覆盖优先于行情源）成交，手续费为名义金额的 0.05%；可用余额 = 权益 − Σ(数量 × 标记价 ÷ 杠杆)。
- 条件单只支持平仓方向（多仓 SELL、空仓 BUY）的 STOP_MARKET / TAKE_PROFIT_MARKET；触发后生成市价单（``reduce_only``），
  按最新价成交。状态流转：NEW →（TRIGGERING → TRIGGERED → FINISHED | TRIGGERING → EXPIRED | TRIGGERING → REJECTED）| CANCELED。
- 部分成交（``PartialFill``）：先按比例成交（向下取到数量步长），余量在 ``delay_ms`` 之后的 ``check_triggers()`` 里
  成交（later）或作废（expire，原始状态 EXPIRED_IN_MATCH）。
- 数据流事件：每次变化都把 7.3 的帧（order_update / account_update / algo_update）放进发件箱，分发器用
  ``drain_events()`` 取走后推给订阅者。帧的先后按行为开关 ``fill_event_order``；algo_update 只在 ``push_algo_events`` 打开时产生。
- 查询窗口：``trades`` / ``income`` 带 ``start_ms`` 时从窗口起点升序取前 ``limit`` 条，否则取最近的 ``limit`` 条（升序返回）。
- 所有数值字段输出为字符串（``feed.fmt``），时间是毫秒整数，编号是字符串。
- 快照：``to_snapshot()`` 是可存成 JSON 的字典；``Engine.from_snapshot(...)`` 原样恢复（含编号计数器与未到期的部分成交余量）。
  故障开关与行为开关不在引擎快照里（行为开关由分发器另存）。
"""
from __future__ import annotations

import os
import random
import re
import string
from dataclasses import dataclass, fields
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from .faults import Switches
from .feed import InstrumentSpec, fmt, is_multiple, round_step, to_decimal

FEE_RATE = Decimal("0.0005")  # 0.05%
DEFAULT_LEVERAGE = 20
MAX_LEVERAGE = 125
LIQ_BUFFER = Decimal("0.005")  # 强平价估算里的 0.5%
FUNDING_PERIOD_MS = 8 * 3_600_000  # 00:00、08:00、16:00 UTC
FALLBACK_FUNDING_RATE = Decimal("0.0001")
ASSET = "USDT"
SIDES = ("BUY", "SELL")
POSITION_SIDES = ("LONG", "SHORT")
ALGO_TYPES = ("STOP_MARKET", "TAKE_PROFIT_MARKET")
WORKING_TYPES = ("CONTRACT_PRICE", "MARK_PRICE")
MARGIN_MODES = ("cross", "isolated")
INCOME_TYPES = ("FUNDING_FEE", "REALIZED_PNL", "COMMISSION")
OPEN_ORDER_STATUSES = ("NEW", "PARTIALLY_FILLED")
CLIENT_ID_RE = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")  # Binance newClientOrderId 的规则
HISTORY_KEPT = 20_000  # 已结束的订单、条件单，以及成交与收益流水各保留这么多条
_ALNUM = string.ascii_letters + string.digits
SNAPSHOT_VERSION = 1


class EngineError(Exception):
    """交易所拒绝：Binance 风格的 ``code`` 与中文 ``msg``；``http_status`` 缺省 400。"""

    def __init__(self, code: int, msg: str, http_status: int = 400):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status


@dataclass(frozen=True)
class PartialFill:
    """部分成交：先成交 ``ratio``（向下取到数量步长），余量 ``delay_ms`` 后按 ``remainder`` 成交（later）或作废（expire）。"""

    ratio: Decimal
    remainder: str = "later"
    delay_ms: int = 5000


# ---------------------------------------------------------------- 内部记录（快照按字段原样存取）


@dataclass
class _Position:
    symbol: str
    side: str
    qty: Decimal
    entry: Decimal
    update_ms: int


@dataclass
class _Order:
    order_id: str
    symbol: str
    client_order_id: str
    side: str
    position_side: str
    qty: Decimal
    filled: Decimal
    cost: Decimal
    status: str
    raw_status: str
    close_position: bool
    reduce_only: bool
    create_ms: int
    update_ms: int
    source: str  # api | manual | trigger
    pending_due_ms: int | None = None
    pending_mode: str | None = None


@dataclass
class _Algo:
    algo_id: str
    symbol: str
    client_algo_id: str
    side: str
    position_side: str
    algo_type: str
    trigger_price: Decimal
    working_type: str
    close_position: bool
    qty: Decimal | None
    status: str
    actual_order_id: str | None
    create_ms: int
    update_ms: int
    source: str


@dataclass
class _Trade:
    trade_id: str
    order_id: str
    symbol: str
    side: str
    position_side: str
    qty: Decimal
    price: Decimal
    commission: Decimal
    realized_pnl: Decimal
    time_ms: int


@dataclass
class _Income:
    tran_id: str
    type: str
    symbol: str
    amount: Decimal
    time_ms: int
    info: str


@dataclass
class _Override:
    price: Decimal
    time_ms: int


def _dump(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        out[f.name] = str(value) if isinstance(value, Decimal) else value
    return out


def _load(cls: type, data: dict[str, Any]) -> Any:
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        value = data[f.name]
        if "Decimal" in str(f.type) and value is not None:
            value = Decimal(value)
        kwargs[f.name] = value
    return cls(**kwargs)


def _is_opening(side: str, position_side: str) -> bool:
    return (side == "BUY") == (position_side == "LONG")


def _closing_side(position_side: str) -> str:
    return "SELL" if position_side == "LONG" else "BUY"


def _hit(algo_type: str, side: str, trigger: Decimal, price: Decimal) -> bool:
    """止损：多仓（SELL）价 ≤ 触发价、空仓（BUY）价 ≥ 触发价；止盈相反。"""
    if algo_type == "STOP_MARKET":
        return price <= trigger if side == "SELL" else price >= trigger
    return price >= trigger if side == "SELL" else price <= trigger


def _limit(value: Any, default: int, maximum: int) -> int:
    if value is None:
        return default
    if type(value) is not int or value < 1:
        raise EngineError(-1102, f"limit 必须是正整数（最大 {maximum}）")
    return min(value, maximum)


class Engine:
    """撮合引擎。``seed`` 只影响交易所生成的随机编号（测试用固定值，使编号可复现）。"""

    def __init__(self, *, feed: Any, switches: Switches, clock: Callable[[], int], equity: Decimal | str = "1000",
                 seed: int | None = None):
        self.feed = feed
        self.switches = switches
        self._clock = clock
        self._rng = random.Random(seed if seed is not None else int.from_bytes(os.urandom(8), "big"))
        now = clock()
        # 编号从创建时刻（毫秒）起数：不持久化运行时重启也不会和上一次撞号；四类编号错开量级，互不相同
        self._next = {"order": now, "algo": now + 3 * 10**12, "trade": now + 6 * 10**12, "tran": now + 9 * 10**12}
        self.version = 0
        self._events: list[dict[str, Any]] = []
        self._reset_state(self._positive(equity, "equity"))

    def _reset_state(self, equity: Decimal) -> None:
        now = self._clock()
        self._wallet = equity
        self._positions: dict[tuple[str, str], _Position] = {}
        self._leverage: dict[str, int] = {}
        self._margin_mode: dict[str, str] = {}
        self._orders: dict[str, _Order] = {}
        self._open_orders: dict[str, _Order] = {}
        self._cid_index: dict[tuple[str, str], str] = {}
        self._algos: dict[str, _Algo] = {}
        self._open_algos: dict[str, _Algo] = {}
        self._trades: list[_Trade] = []
        self._income: list[_Income] = []
        self._overrides: dict[str, _Override] = {}
        self._account_ms = now
        self._funding_ms = (now // FUNDING_PERIOD_MS) * FUNDING_PERIOD_MS

    # ------------------------------------------------------------ 小工具

    def _now(self) -> int:
        return self._clock()

    def _touch(self) -> None:
        self.version += 1

    def _new_id(self, kind: str) -> str:
        self._next[kind] += 1
        return str(self._next[kind])

    def _random(self, n: int) -> str:
        return "".join(self._rng.choice(_ALNUM) for _ in range(n))

    def _exchange_client_id(self) -> str:
        """交易所生成的自定义编号（没有带编号的订单、编号不可关联的触发单）。"""
        while True:
            cid = self._random(22)
            if not cid.startswith("DK"):
                return cid

    def new_web_id(self) -> str:
        """模拟网页端手动下单的编号：``web_`` + 20 位随机字母数字。"""
        return "web_" + self._random(20)

    @staticmethod
    def _positive(value: Any, name: str) -> Decimal:
        try:
            d = to_decimal(value)
        except ValueError:
            raise EngineError(-1102, f"{name} 必须是数值") from None
        if d <= 0:
            raise EngineError(-1102, f"{name} 必须为正")
        return d

    def _instrument(self, symbol: Any) -> InstrumentSpec:
        spec = self.feed.instrument(symbol) if isinstance(symbol, str) else None
        if spec is None:
            raise EngineError(-1121, f"未知的合约：{symbol}")
        return spec

    @staticmethod
    def _sides(side: Any, position_side: Any) -> tuple[str, str]:
        if side not in SIDES:
            raise EngineError(-1102, f"side 只能是 BUY 或 SELL：{side!r}")
        if position_side not in POSITION_SIDES:
            raise EngineError(-1102, f"position_side 只能是 LONG 或 SHORT（替身固定为对冲模式）：{position_side!r}")
        return side, position_side

    def _check_qty(self, spec: InstrumentSpec, qty: Any) -> Decimal:
        if isinstance(qty, bool) or not isinstance(qty, (Decimal, int, str)):
            raise EngineError(-1102, f"数量必须是数值：{qty!r}")
        q = self._positive(qty, "数量")
        step = spec.market_step
        if not is_multiple(q, step):
            raise EngineError(-1111, f"数量 {fmt(q)} 不是数量步长 {fmt(step)} 的整数倍")
        if q < spec.min_qty:
            raise EngineError(-1102, f"数量 {fmt(q)} 低于最小下单量 {fmt(spec.min_qty)}")
        if spec.market_max_qty is not None and q > spec.market_max_qty:
            raise EngineError(-1102, f"数量 {fmt(q)} 超过市价单最大数量 {fmt(spec.market_max_qty)}")
        return q

    def _check_price(self, spec: InstrumentSpec, price: Any) -> Decimal:
        if isinstance(price, bool) or not isinstance(price, (Decimal, int, str)):
            raise EngineError(-1102, f"触发价必须是数值：{price!r}")
        p = self._positive(price, "触发价")
        if not is_multiple(p, spec.tick_size):
            raise EngineError(-1111, f"触发价 {fmt(p)} 不是价格步长 {fmt(spec.tick_size)} 的整数倍")
        return p

    @staticmethod
    def _check_client_id(cid: Any) -> str:
        if not isinstance(cid, str) or not CLIENT_ID_RE.match(cid):
            raise EngineError(-4015, f"自定义编号不合法（1–36 位，只能含字母、数字与 .:/_-）：{cid!r}")
        return cid

    def _price(self, symbol: str) -> tuple[Decimal, Decimal, int] | None:
        """(最新价, 标记价, 时刻)：价格覆盖优先于行情源；没有行情时为 None。"""
        o = self._overrides.get(symbol)
        if o is not None:
            return o.price, o.price, o.time_ms
        q = self.feed.quote(symbol)
        if q is None:
            return None
        return q.last, q.mark, q.time_ms

    def _lev(self, symbol: str) -> int:
        return self._leverage.get(symbol, DEFAULT_LEVERAGE)

    def _mode(self, symbol: str) -> str:
        return self._margin_mode.get(symbol, "cross")

    def _pos(self, symbol: str, side: str) -> _Position:
        pos = self._positions.get((symbol, side))
        if pos is None:
            pos = _Position(symbol, side, Decimal(0), Decimal(0), self._now())
            self._positions[(symbol, side)] = pos
        return pos

    def _held(self, symbol: str, side: str) -> Decimal:
        pos = self._positions.get((symbol, side))
        return pos.qty if pos is not None else Decimal(0)

    # ------------------------------------------------------------ 输出格式

    def _mark_of(self, pos: _Position) -> Decimal:
        px = self._price(pos.symbol)
        return px[1] if px is not None else pos.entry

    def _upnl(self, pos: _Position, mark: Decimal) -> Decimal:
        if pos.qty == 0:
            return Decimal(0)
        diff = mark - pos.entry if pos.side == "LONG" else pos.entry - mark
        return diff * pos.qty

    def _position_json(self, pos: _Position) -> dict[str, Any]:
        mark = self._mark_of(pos)
        lev = self._lev(pos.symbol)
        liq = Decimal(0)
        if pos.qty > 0:
            k = Decimal(1) / lev
            factor = (1 - k + LIQ_BUFFER) if pos.side == "LONG" else (1 + k - LIQ_BUFFER)
            spec = self.feed.instrument(pos.symbol)
            liq = pos.entry * factor
            if spec is not None:
                liq = round_step(liq, spec.tick_size)
            liq = max(liq, Decimal(0))
        return {"symbol": pos.symbol, "position_side": pos.side, "qty": fmt(pos.qty), "entry_price": fmt(pos.entry),
                "mark_price": fmt(mark), "liquidation_price": fmt(liq), "unrealized_pnl": fmt(self._upnl(pos, mark)),
                "leverage": lev, "margin_mode": self._mode(pos.symbol), "update_time_ms": pos.update_ms}

    def _order_json(self, o: _Order) -> dict[str, Any]:
        avg = o.cost / o.filled if o.filled > 0 else Decimal(0)
        return {"symbol": o.symbol, "client_order_id": o.client_order_id, "order_id": o.order_id, "side": o.side,
                "position_side": o.position_side, "type": "MARKET", "qty": fmt(o.qty), "filled_qty": fmt(o.filled),
                "avg_price": fmt(avg), "status": o.status, "raw_status": o.raw_status,
                "close_position": o.close_position, "reduce_only": o.reduce_only, "update_time_ms": o.update_ms}

    @staticmethod
    def _algo_json(a: _Algo) -> dict[str, Any]:
        return {"symbol": a.symbol, "client_algo_id": a.client_algo_id, "algo_id": a.algo_id, "side": a.side,
                "position_side": a.position_side, "type": a.algo_type, "trigger_price": fmt(a.trigger_price),
                "working_type": a.working_type, "close_position": a.close_position, "qty": fmt(a.qty),
                "status": a.status, "actual_order_id": a.actual_order_id, "create_time_ms": a.create_ms,
                "update_time_ms": a.update_ms}

    @staticmethod
    def _trade_json(t: _Trade) -> dict[str, Any]:
        return {"trade_id": t.trade_id, "order_id": t.order_id, "symbol": t.symbol, "side": t.side,
                "position_side": t.position_side, "qty": fmt(t.qty), "price": fmt(t.price),
                "commission": fmt(t.commission), "commission_asset": ASSET, "realized_pnl": fmt(t.realized_pnl),
                "time_ms": t.time_ms, "maker": False}

    @staticmethod
    def _income_json(r: _Income) -> dict[str, Any]:
        return {"tran_id": r.tran_id, "type": r.type, "symbol": r.symbol, "amount": fmt(r.amount), "asset": ASSET,
                "time_ms": r.time_ms, "info": r.info}

    # ------------------------------------------------------------ 数据流事件

    def drain_events(self) -> list[dict[str, Any]]:
        """取走发件箱里的数据流帧（7.3 的格式，带 event_time_ms）。"""
        events, self._events = self._events, []
        return events

    def _emit_order(self, o: _Order, execution: str, trade: _Trade | None = None) -> dict[str, Any]:
        frame = {"type": "order_update", "event_time_ms": self._now(), "execution": execution,
                 "order": self._order_json(o), "fill": self._trade_json(trade) if trade is not None else None}
        self._events.append(frame)
        return frame

    def _account_frame(self, symbols: Iterable[str], reason: str) -> dict[str, Any]:
        positions = []
        for sym in dict.fromkeys(symbols):
            for side in POSITION_SIDES:
                pos = self._positions.get((sym, side)) or _Position(sym, side, Decimal(0), Decimal(0), self._now())
                positions.append(self._position_json(pos))
        return {"type": "account_update", "event_time_ms": self._now(), "reason": reason, "positions": positions,
                "balances": [{"asset": ASSET, "wallet_balance": fmt(self._wallet)}]}

    def _emit_algo(self, a: _Algo) -> None:
        if self.switches.behavior("push_algo_events"):
            self._events.append({"type": "algo_update", "event_time_ms": self._now(), "algo": self._algo_json(a)})

    def _set_algo_status(self, a: _Algo, status: str) -> None:
        a.status = status
        a.update_ms = self._now()
        if status != "NEW":
            self._open_algos.pop(a.algo_id, None)
        self._emit_algo(a)

    # ------------------------------------------------------------ 行情与价格覆盖

    def quote(self, symbol: str) -> dict[str, Any] | None:
        """``{"symbol", "last", "mark", "time_ms"}``（价格覆盖优先）；没有行情时为 None。"""
        px = self._price(symbol)
        if px is None:
            return None
        return {"symbol": symbol, "last": fmt(px[0]), "mark": fmt(px[1]), "time_ms": px[2]}

    def set_price_override(self, symbol: str, *, price: Any = None, pct: Any = None,
                           clear: bool = False) -> dict[str, Any] | None:
        """覆盖最新价与标记价：绝对价 ``price``、相对当前价的百分比 ``pct``，或 ``clear`` 取消。按价格步长取整。"""
        spec = self._instrument(symbol)
        if clear:
            self._overrides.pop(symbol, None)
            self._touch()
            return self.quote(symbol)
        if price is not None:
            new = self._positive(price, "price")
        elif pct is not None:
            try:
                p = to_decimal(pct)
            except ValueError:
                raise EngineError(-1102, "pct 必须是数值") from None
            cur = self._price(symbol)
            if cur is None:
                raise EngineError(-1102, f"{symbol} 没有当前价，不能按百分比覆盖")
            new = cur[0] * (1 + p / 100)
        else:
            raise EngineError(-1102, "需要 price、pct 或 clear 之一")
        new = round_step(new, spec.tick_size)
        if new <= 0:
            raise EngineError(-1102, "覆盖后的价格必须为正")
        self._overrides[symbol] = _Override(new, self._now())
        self._touch()
        return self.quote(symbol)

    def overrides(self) -> dict[str, dict[str, Any]]:
        return {s: {"price": fmt(o.price), "time_ms": o.time_ms} for s, o in self._overrides.items()}

    # ------------------------------------------------------------ 账户

    def _active_positions(self) -> list[_Position]:
        return sorted((p for p in self._positions.values() if p.qty != 0), key=lambda p: (p.symbol, p.side))

    def _totals(self) -> tuple[Decimal, Decimal]:
        """(浮动盈亏合计, 占用保证金合计)。"""
        upnl = Decimal(0)
        used = Decimal(0)
        for pos in self._active_positions():
            mark = self._mark_of(pos)
            upnl += self._upnl(pos, mark)
            used += pos.qty * mark / self._lev(pos.symbol)
        return upnl, used

    def _available(self) -> Decimal:
        upnl, used = self._totals()
        return self._wallet + upnl - used

    def account(self) -> dict[str, Any]:
        upnl, used = self._totals()
        equity = self._wallet + upnl
        return {"equity": fmt(equity), "wallet_balance": fmt(self._wallet), "available_balance": fmt(equity - used),
                "unrealized_pnl": fmt(upnl), "update_time_ms": self._account_ms}

    def positions(self) -> list[dict[str, Any]]:
        """只含数量非零的持仓，按合约名、LONG 在前排序。"""
        return [self._position_json(p) for p in self._active_positions()]

    def interesting_symbols(self) -> set[str]:
        """持仓、挂单、条件单涉及的合约（行情源要持续轮询这些）。"""
        out = {p.symbol for p in self._active_positions()}
        out |= {o.symbol for o in self._open_orders.values()}
        out |= {a.symbol for a in self._open_algos.values()}
        return out

    # ------------------------------------------------------------ 市价单

    def place_market_order(self, *, symbol: str, side: str, position_side: str, qty: Any,
                           client_order_id: str | None = None, partial: PartialFill | None = None,
                           source: str = "api") -> dict[str, Any]:
        """下市价单并立即按最新价成交（部分成交见 ``partial``）。返回订单（成交后的状态）。"""
        spec = self._instrument(symbol)
        side, position_side = self._sides(side, position_side)
        q = self._check_qty(spec, qty)
        cid = self._exchange_client_id() if client_order_id is None else self._check_client_id(client_order_id)
        prev_id = self._cid_index.get((symbol, cid))
        prev = self._orders.get(prev_id) if prev_id is not None else None
        if prev is not None and (prev.status in OPEN_ORDER_STATUSES
                                 or self.switches.behavior("duplicate_client_id_after_fill") == "reject"):
            raise EngineError(-4116, f"自定义编号重复：{cid}")
        px = self._price(symbol)
        if px is None:
            raise EngineError(-4131, f"替身交易所没有 {symbol} 的行情，市价单无法成交")
        last = px[0]
        opening = _is_opening(side, position_side)
        if opening:
            if spec.status != "TRADING":
                raise EngineError(-4140, f"{symbol} 不在交易状态（{spec.status}），不能开仓")
            notional = q * last
            if notional < spec.min_notional:
                raise EngineError(-4164, f"名义金额 {fmt(notional)} 低于最小名义金额 {fmt(spec.min_notional)}")
            required = notional / self._lev(symbol) + notional * FEE_RATE
            if required > self._available():
                raise EngineError(-2019, f"保证金不足：需要 {fmt(required)}，可用 {fmt(self._available())}")
        else:
            held = self._held(symbol, position_side)
            if held == 0:
                raise EngineError(-2022, f"{symbol} {position_side} 侧没有持仓可平")
            if q > held:
                if self.switches.behavior("close_qty_exceeds") == "reject":
                    raise EngineError(-2022, f"平仓数量 {fmt(q)} 超过持仓 {fmt(held)}")
                q = held
            if q * last < spec.min_notional and not self.switches.behavior("close_min_notional_exempt"):
                raise EngineError(-4164, f"名义金额 {fmt(q * last)} 低于最小名义金额 {fmt(spec.min_notional)}")
        now = self._now()
        order = _Order(order_id=self._new_id("order"), symbol=symbol, client_order_id=cid, side=side,
                       position_side=position_side, qty=q, filled=Decimal(0), cost=Decimal(0), status="NEW",
                       raw_status="NEW", close_position=False, reduce_only=False, create_ms=now, update_ms=now,
                       source=source)
        self._add_order(order)
        self._emit_order(order, "NEW")
        fill_qty = q
        if partial is not None:
            part = (q * to_decimal(partial.ratio) / spec.market_step).to_integral_value(rounding=ROUND_FLOOR)
            part *= spec.market_step
            if 0 < part < q:
                fill_qty = part
                order.pending_due_ms = now + int(partial.delay_ms)
                order.pending_mode = partial.remainder
        self._fill(order, fill_qty, last)
        self._touch()
        return self._order_json(order)

    def _add_order(self, order: _Order) -> None:
        self._orders[order.order_id] = order
        self._open_orders[order.order_id] = order
        self._cid_index[(order.symbol, order.client_order_id)] = order.order_id
        if len(self._orders) > HISTORY_KEPT + 1000:
            closed = [k for k in self._orders if k not in self._open_orders]
            for oid in closed[: len(self._orders) - HISTORY_KEPT]:
                old = self._orders.pop(oid)
                if self._cid_index.get((old.symbol, old.client_order_id)) == oid:
                    del self._cid_index[(old.symbol, old.client_order_id)]

    def _fill(self, order: _Order, qty: Decimal, price: Decimal) -> _Trade:
        now = self._now()
        pos = self._pos(order.symbol, order.position_side)
        opening = _is_opening(order.side, order.position_side)
        notional = qty * price
        commission = notional * FEE_RATE
        if opening:
            new_qty = pos.qty + qty
            pos.entry = (pos.entry * pos.qty + price * qty) / new_qty
            pos.qty = new_qty
            realized = Decimal(0)
        else:
            realized = (price - pos.entry) * qty if pos.side == "LONG" else (pos.entry - price) * qty
            pos.qty -= qty
            if pos.qty == 0:
                pos.entry = Decimal(0)
        pos.update_ms = now
        self._wallet += realized - commission
        self._account_ms = now
        trade = _Trade(trade_id=self._new_id("trade"), order_id=order.order_id, symbol=order.symbol, side=order.side,
                       position_side=order.position_side, qty=qty, price=price, commission=commission,
                       realized_pnl=realized, time_ms=now)
        self._trades.append(trade)
        if realized != 0:
            self._add_income("REALIZED_PNL", order.symbol, realized, trade.trade_id)
        if commission != 0:
            self._add_income("COMMISSION", order.symbol, -commission, trade.trade_id)
        if len(self._trades) > HISTORY_KEPT + 1000:
            del self._trades[: len(self._trades) - HISTORY_KEPT]
        order.filled += qty
        order.cost += notional
        order.update_ms = now
        if order.filled >= order.qty:
            order.status = order.raw_status = "FILLED"
            order.pending_due_ms = order.pending_mode = None
            self._open_orders.pop(order.order_id, None)
        else:
            order.status = order.raw_status = "PARTIALLY_FILLED"
        account = self._account_frame([order.symbol], "ORDER")
        if self.switches.behavior("fill_event_order") == "account_first":
            self._events.append(account)
            self._emit_order(order, "TRADE", trade)
        else:
            self._emit_order(order, "TRADE", trade)
            self._events.append(account)
        if not opening and pos.qty == 0 and self.switches.behavior("close_position_stop_autocancel_on_flat"):
            for a in list(self._open_algos.values()):
                if a.symbol == order.symbol and a.position_side == order.position_side and a.close_position:
                    self._set_algo_status(a, "CANCELED")
        return trade

    def _add_income(self, kind: str, symbol: str, amount: Decimal, info: str) -> _Income:
        row = _Income(tran_id=self._new_id("tran"), type=kind, symbol=symbol, amount=amount, time_ms=self._now(),
                      info=info)
        self._income.append(row)
        if len(self._income) > HISTORY_KEPT + 1000:
            del self._income[: len(self._income) - HISTORY_KEPT]
        return row

    def _finish_unfilled(self, order: _Order, status: str, raw_status: str, execution: str) -> None:
        order.status, order.raw_status = status, raw_status
        order.pending_due_ms = order.pending_mode = None
        order.update_ms = self._now()
        self._open_orders.pop(order.order_id, None)
        self._emit_order(order, execution)

    def _process_pending(self) -> bool:
        now = self._now()
        due = [o for o in self._open_orders.values() if o.pending_due_ms is not None and o.pending_due_ms <= now]
        for order in due:
            if order.pending_mode == "expire":
                self._finish_unfilled(order, "EXPIRED", "EXPIRED_IN_MATCH", "EXPIRED")
                continue
            px = self._price(order.symbol)
            rest = order.qty - order.filled
            if not _is_opening(order.side, order.position_side):
                rest = min(rest, self._held(order.symbol, order.position_side))
            if px is None or rest <= 0:
                self._finish_unfilled(order, "EXPIRED", "EXPIRED", "EXPIRED")
                continue
            self._fill(order, rest, px[0])
            if order.status != "FILLED":
                self._finish_unfilled(order, "EXPIRED", "EXPIRED", "EXPIRED")
        return bool(due)

    # ------------------------------------------------------------ 条件单

    def place_algo_order(self, *, symbol: str, side: str, position_side: str, algo_type: str, trigger_price: Any,
                         working_type: str = "CONTRACT_PRICE", close_position: bool = False, qty: Any = None,
                         client_algo_id: str | None = None, source: str = "api") -> dict[str, Any]:
        """挂条件单。返回受理时的快照（``stop_crossed_on_create=trigger_immediately`` 时随后立即触发）。"""
        spec = self._instrument(symbol)
        side, position_side = self._sides(side, position_side)
        if algo_type not in ALGO_TYPES:
            raise EngineError(-1102, f"条件单类型只能是 STOP_MARKET 或 TAKE_PROFIT_MARKET：{algo_type!r}")
        if working_type not in WORKING_TYPES:
            raise EngineError(-1102, f"触发价类型只能是 CONTRACT_PRICE 或 MARK_PRICE：{working_type!r}")
        if _is_opening(side, position_side):
            raise EngineError(-1102, f"替身只支持平仓方向的条件单：{position_side} 侧应为 {_closing_side(position_side)}")
        if type(close_position) is not bool:
            raise EngineError(-1102, "close_position 必须是 true 或 false")
        if close_position:
            if qty is not None:
                raise EngineError(-1102, "全平型条件单不得带数量")
            q = None
        else:
            if qty is None:
                raise EngineError(-1102, "数量型条件单必须带数量")
            q = self._check_qty(spec, qty)
        trigger = self._check_price(spec, trigger_price)
        cid = self._exchange_client_id() if client_algo_id is None else self._check_client_id(client_algo_id)
        if any(a.client_algo_id == cid for a in self._open_algos.values()):
            raise EngineError(-4116, f"条件单自定义编号重复：{cid}")
        if self.switches.behavior("conditional_limit") == "per_symbol_10":
            if sum(1 for a in self._open_algos.values() if a.symbol == symbol) >= 10:
                raise EngineError(-4045, f"{symbol} 的条件单已达上限 10 张")
        elif len(self._open_algos) >= 200:
            raise EngineError(-4045, "账户的条件单已达上限 200 张")
        if close_position and self.switches.behavior("dual_close_position_stop") == "reject_4130":
            if any(a.symbol == symbol and a.position_side == position_side and a.algo_type == algo_type
                   and a.close_position for a in self._open_algos.values()):
                raise EngineError(-4130, f"{symbol} {position_side} 侧已有全平型 {algo_type}")
        px = self._price(symbol)
        immediate = False
        if px is not None:
            ref = px[0] if working_type == "CONTRACT_PRICE" else px[1]
            if _hit(algo_type, side, trigger, ref):
                if self.switches.behavior("stop_crossed_on_create") == "reject_2021":
                    raise EngineError(-2021, f"触发价 {fmt(trigger)} 已被当前价 {fmt(ref)} 越过，会立即触发")
                immediate = True
        now = self._now()
        algo = _Algo(algo_id=self._new_id("algo"), symbol=symbol, client_algo_id=cid, side=side,
                     position_side=position_side, algo_type=algo_type, trigger_price=trigger,
                     working_type=working_type, close_position=close_position, qty=q, status="NEW",
                     actual_order_id=None, create_ms=now, update_ms=now, source=source)
        self._algos[algo.algo_id] = algo
        self._open_algos[algo.algo_id] = algo
        self._trim_algos()
        self._emit_algo(algo)
        receipt = self._algo_json(algo)
        if immediate:
            self._trigger(algo)
        self._touch()
        return receipt

    def _trim_algos(self) -> None:
        if len(self._algos) > HISTORY_KEPT + 1000:
            closed = [k for k in self._algos if k not in self._open_algos]
            for aid in closed[: len(self._algos) - HISTORY_KEPT]:
                del self._algos[aid]

    def _trigger(self, algo: _Algo) -> None:
        self._set_algo_status(algo, "TRIGGERING")
        held = self._held(algo.symbol, algo.position_side)
        px = self._price(algo.symbol)
        if held == 0 or px is None:
            self._set_algo_status(algo, "EXPIRED")  # 该侧已无仓
            return
        q = held if algo.close_position else algo.qty
        if q > held:
            if self.switches.behavior("qty_stop_after_shrink") == "reject":
                self._set_algo_status(algo, "REJECTED")
                return
            q = held
        linked = self.switches.behavior("triggered_order_link") == "available"
        now = self._now()
        order = _Order(order_id=self._new_id("order"), symbol=algo.symbol,
                       client_order_id=algo.client_algo_id if linked else self._exchange_client_id(),
                       side=algo.side, position_side=algo.position_side, qty=q, filled=Decimal(0), cost=Decimal(0),
                       status="NEW", raw_status="NEW", close_position=algo.close_position, reduce_only=True,
                       create_ms=now, update_ms=now, source="trigger")
        self._add_order(order)
        if linked:
            algo.actual_order_id = order.order_id
        self._set_algo_status(algo, "TRIGGERED")
        self._emit_order(order, "NEW")
        self._fill(order, q, px[0])
        self._set_algo_status(algo, "FINISHED")

    def check_triggers(self) -> list[str]:
        """处理到期的部分成交余量，再检查全部未结条件单；返回本次触发的条件单编号（含触发后过期或被拒的）。"""
        changed = self._process_pending()
        fired: list[str] = []
        for algo in list(self._open_algos.values()):
            if algo.status != "NEW":
                continue  # 前面触发的单自动撤掉了它
            px = self._price(algo.symbol)
            if px is None:
                continue
            ref = px[0] if algo.working_type == "CONTRACT_PRICE" else px[1]
            if _hit(algo.algo_type, algo.side, algo.trigger_price, ref):
                self._trigger(algo)
                fired.append(algo.algo_id)
        if changed or fired:
            self._touch()
        return fired

    # ------------------------------------------------------------ 撤单与查询

    def _find_order(self, symbol: str, order_id: Any, client_order_id: Any) -> _Order | None:
        if order_id is not None:
            o = self._orders.get(str(order_id))
        elif client_order_id is not None:
            oid = self._cid_index.get((symbol, client_order_id)) if isinstance(client_order_id, str) else None
            o = self._orders.get(oid) if oid is not None else None
        else:
            raise EngineError(-1102, "需要 order_id 或 client_order_id")
        return o if o is not None and o.symbol == symbol else None

    def _find_algo(self, symbol: str, algo_id: Any, client_algo_id: Any) -> _Algo | None:
        if algo_id is not None:
            a = self._algos.get(str(algo_id))
            return a if a is not None and a.symbol == symbol else None
        if client_algo_id is not None:
            for a in self._open_algos.values():  # 条件单编号只在未结条件单内唯一
                if a.symbol == symbol and a.client_algo_id == client_algo_id:
                    return a
            return None
        raise EngineError(-1102, "需要 algo_id 或 client_algo_id")

    def get_order(self, *, symbol: str, order_id: Any = None, client_order_id: Any = None) -> dict[str, Any]:
        o = self._find_order(symbol, order_id, client_order_id)
        if o is None:
            raise EngineError(-2013, "订单不存在")
        return self._order_json(o)

    def get_algo_order(self, *, symbol: str, algo_id: Any = None, client_algo_id: Any = None) -> dict[str, Any]:
        a = self._find_algo(symbol, algo_id, client_algo_id)
        if a is None:
            raise EngineError(-2013, "条件单不存在")
        return self._algo_json(a)

    def cancel_order(self, *, symbol: str, order_id: Any = None, client_order_id: Any = None) -> dict[str, Any]:
        o = self._find_order(symbol, order_id, client_order_id)
        if o is None or o.status not in OPEN_ORDER_STATUSES:
            raise EngineError(-2011, "订单不存在或已结束")
        self._finish_unfilled(o, "CANCELED", "CANCELED", "CANCELED")
        self._touch()
        return self._order_json(o)

    def cancel_algo_order(self, *, symbol: str, algo_id: Any = None, client_algo_id: Any = None) -> dict[str, Any]:
        a = self._find_algo(symbol, algo_id, client_algo_id)
        if a is None or a.status != "NEW":
            raise EngineError(-2011, "条件单不存在或已结束")
        self._set_algo_status(a, "CANCELED")
        self._touch()
        return self._algo_json(a)

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [self._order_json(o) for o in self._open_orders.values() if symbol is None or o.symbol == symbol]

    def open_algo_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [self._algo_json(a) for a in self._open_algos.values() if symbol is None or a.symbol == symbol]

    @staticmethod
    def _window(rows: list[Any], start_ms: int | None, end_ms: int | None, limit: int) -> list[Any]:
        picked = [r for r in rows if (start_ms is None or r.time_ms >= start_ms)
                  and (end_ms is None or r.time_ms <= end_ms)]
        return picked[:limit] if start_ms is not None else picked[-limit:]

    def trades(self, symbol: str | None = None, start_ms: int | None = None, end_ms: int | None = None,
               limit: int | None = 500) -> list[dict[str, Any]]:
        """成交记录（不带自定义编号，用 ``order_id`` 关联）；limit 缺省 500、最大 1000。"""
        n = _limit(limit, 500, 1000)
        rows = [t for t in self._trades if symbol is None or t.symbol == symbol]
        return [self._trade_json(t) for t in self._window(rows, start_ms, end_ms, n)]

    def income(self, income_type: str | None = None, symbol: str | None = None, start_ms: int | None = None,
               end_ms: int | None = None, limit: int | None = 100) -> list[dict[str, Any]]:
        """收益流水（FUNDING_FEE、REALIZED_PNL、COMMISSION）；limit 缺省 100、最大 1000。"""
        if income_type is not None and income_type not in INCOME_TYPES:
            raise EngineError(-1102, f"收益类型只能是 {' | '.join(INCOME_TYPES)}：{income_type!r}")
        n = _limit(limit, 100, 1000)
        rows = [r for r in self._income if (income_type is None or r.type == income_type)
                and (symbol is None or r.symbol == symbol)]
        return [self._income_json(r) for r in self._window(rows, start_ms, end_ms, n)]

    # ------------------------------------------------------------ 杠杆与保证金模式

    def leverage_info(self, symbol: str) -> dict[str, Any]:
        self._instrument(symbol)
        return {"symbol": symbol, "leverage": self._lev(symbol), "margin_mode": self._mode(symbol)}

    def set_leverage(self, symbol: str, leverage: Any) -> dict[str, Any]:
        self._instrument(symbol)
        if type(leverage) is not int or not 1 <= leverage <= MAX_LEVERAGE:
            raise EngineError(-4028, f"杠杆 {leverage!r} 不合法（1 到 {MAX_LEVERAGE} 的整数）")
        self._leverage[symbol] = leverage
        self._touch()
        return self.leverage_info(symbol)

    def set_margin_mode(self, symbol: str, margin_mode: Any) -> dict[str, Any]:
        self._instrument(symbol)
        if margin_mode not in MARGIN_MODES:
            raise EngineError(-1102, f"保证金模式只能是 cross 或 isolated：{margin_mode!r}")
        if self._mode(symbol) != margin_mode:
            if any(self._held(symbol, side) != 0 for side in POSITION_SIDES):
                raise EngineError(-4048, f"{symbol} 有持仓，不能改保证金模式")
            if any(o.symbol == symbol for o in self._open_orders.values()) or any(
                    a.symbol == symbol for a in self._open_algos.values()):
                raise EngineError(-4047, f"{symbol} 有挂单或条件单，不能改保证金模式")
            self._margin_mode[symbol] = margin_mode
            self._touch()
        return self.leverage_info(symbol)

    # ------------------------------------------------------------ 资金费

    def settle_funding(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """立即对持仓结算一次资金费：多仓付、空仓收（费率为正时）；金额 = 数量 × 标记价 × 费率。"""
        if symbol is not None:
            self._instrument(symbol)
        rows: list[_Income] = []
        for pos in self._active_positions():
            if symbol is not None and pos.symbol != symbol:
                continue
            rate = self.feed.funding_rate(pos.symbol)
            if rate is None:
                rate = FALLBACK_FUNDING_RATE
            amount = pos.qty * self._mark_of(pos) * rate
            if pos.side == "LONG":
                amount = -amount
            if amount == 0:
                continue
            self._wallet += amount
            rows.append(self._add_income("FUNDING_FEE", pos.symbol, amount, fmt(rate)))
        if rows:
            self._account_ms = self._now()
            self._events.append(self._account_frame([r.symbol for r in rows], "FUNDING_FEE"))
            self._touch()
        return [self._income_json(r) for r in rows]

    def maybe_settle_funding(self) -> list[dict[str, Any]]:
        """到了 00:00、08:00、16:00 UTC 的结算时刻就结算一次（同一时刻只结一次）。"""
        boundary = (self._now() // FUNDING_PERIOD_MS) * FUNDING_PERIOD_MS
        if boundary <= self._funding_ms:
            return []
        self._funding_ms = boundary
        self._touch()
        return self.settle_funding()

    # ------------------------------------------------------------ 重置与快照

    def reset(self, equity: Any) -> None:
        """清空账户重来（只给测试用）：持仓、订单、条件单、成交、流水、杠杆、价格覆盖全部清掉；编号继续往后数。"""
        self._reset_state(self._positive(equity, "equity"))
        self._events.clear()
        self._touch()

    def to_snapshot(self) -> dict[str, Any]:
        state = self._rng.getstate()
        return {
            "version": SNAPSHOT_VERSION,
            "wallet": str(self._wallet),
            "account_ms": self._account_ms,
            "funding_ms": self._funding_ms,
            "positions": [_dump(p) for p in self._positions.values()],
            "leverage": dict(self._leverage),
            "margin_mode": dict(self._margin_mode),
            "orders": [_dump(o) for o in self._orders.values()],
            "algos": [_dump(a) for a in self._algos.values()],
            "trades": [_dump(t) for t in self._trades],
            "income": [_dump(r) for r in self._income],
            "overrides": {s: _dump(o) for s, o in self._overrides.items()},
            "counters": dict(self._next),
            "rng": [state[0], list(state[1]), state[2]],
        }

    @classmethod
    def from_snapshot(cls, snap: Any, *, feed: Any, switches: Switches, clock: Callable[[], int],
                      seed: int | None = None) -> Engine:
        """从 ``to_snapshot()`` 的结果恢复；格式不对抛 ValueError（不会当成空账户）。"""
        try:
            if snap.get("version") != SNAPSHOT_VERSION:
                raise ValueError(f"快照版本 {snap.get('version')!r} 不认识")
            engine = cls(feed=feed, switches=switches, clock=clock, equity=Decimal(snap["wallet"]), seed=seed)
            engine._wallet = Decimal(snap["wallet"])
            engine._account_ms = int(snap["account_ms"])
            engine._funding_ms = int(snap["funding_ms"])
            for row in snap["positions"]:
                p = _load(_Position, row)
                engine._positions[(p.symbol, p.side)] = p
            engine._leverage = {str(k): int(v) for k, v in snap["leverage"].items()}
            engine._margin_mode = {str(k): str(v) for k, v in snap["margin_mode"].items()}
            for row in snap["orders"]:
                o = _load(_Order, row)
                engine._orders[o.order_id] = o
                engine._cid_index[(o.symbol, o.client_order_id)] = o.order_id
                if o.status in OPEN_ORDER_STATUSES:
                    engine._open_orders[o.order_id] = o
            for row in snap["algos"]:
                a = _load(_Algo, row)
                engine._algos[a.algo_id] = a
                if a.status == "NEW":
                    engine._open_algos[a.algo_id] = a
            engine._trades = [_load(_Trade, r) for r in snap["trades"]]
            engine._income = [_load(_Income, r) for r in snap["income"]]
            engine._overrides = {str(s): _load(_Override, r) for s, r in snap["overrides"].items()}
            engine._next = {k: int(snap["counters"][k]) for k in ("order", "algo", "trade", "tran")}
            version, internal, gauss = snap["rng"]
            engine._rng.setstate((version, tuple(internal), gauss))
        except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"替身状态快照不完整或格式不对：{exc!r}") from exc
        return engine
