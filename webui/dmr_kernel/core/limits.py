"""合约数上限与订单频率（第 3 层）：开仓前的名额预占、同一合约的订单数限制（报告 4.6 的上限口径）。

纯内存、同步、只在事件循环线程里用；时间一律由调用方显式给出（快照时间 ``at_ms``、判定时间 ``now_ms``），
构造时传入的时钟只为与其它模块的接口一致，这里不读它。

- 占用 = 最近一次持仓快照与挂单快照里「有持仓或任何挂单」的（合约，方向）数，对冲两侧各算一个；条件单（止损等）只要还开着也算。
  只看快照，不问归属：外部持仓、待确认仓也占交易所的合约数。
- 预占：开仓意图在发单前向 ``try_reserve`` 要名额，意图到终态时 ``release``。「已占用 + 已预占 + 1 > 上限」就拒绝，
  所以同一批多个意图不会一起越过上限。成交之后持仓快照里出现了这个仓、预占还没释放的这段时间，同一个仓被数了两次：偏保守，不会多放。
- 快照过期按未知处理：持仓快照超过 2 × ``reconcile_positions_s`` 秒、挂单快照超过 2 × ``reconcile_orders_s`` 秒没有更新
  （或者还没收到过），不放行新的预占（已经拿到名额的意图重试时仍然是放行的）。
- 上限：``max_symbols``，仅轮询降级时取 ``max_symbols_degraded``；两者都不超过硬上限 35，降级只会收紧。
  每次判定都读一次配置，改配置下一次判定就生效。
- 订单频率：同一合约在最近 10 分钟（左开右闭 ``(now − 10 分钟, now]``）内的订单数必须 < ``max_orders_per_symbol_10m``。

``RiskDecision`` 是共享类型（``core.types``），与 ``core.riskgate`` 返回的是同一个类，上层可以统一判断；本模块里保留这个名字，
``from dmr_kernel.core.limits import RiskDecision`` 照常可用。
"""
from __future__ import annotations

import logging
from bisect import bisect_right, insort
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.types import AlgoOrderInfo, OrderInfo, PositionInfo, PositionSide, RiskDecision

log = logging.getLogger(__name__)

__all__ = ["HARD_MAX_SYMBOLS", "ORDER_WINDOW_MS", "Limits", "Occupancy", "RiskDecision"]

HARD_MAX_SYMBOLS = 35  # 考核口径的硬上限；配置里写得再大也不超过
ORDER_WINDOW_MS = 600_000  # 订单频率的统计窗口：10 分钟
_WORKING_ORDER_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})

Key = tuple[str, str]  # (交易所原始合约代码, "LONG" | "SHORT")


@dataclass(frozen=True)
class Occupancy:
    """合约数占用的一个视图。keys 只列快照里的（合约，方向），预占的名额不在里面。"""

    used: int
    reserved: int
    cap: int
    stale: bool
    keys: frozenset[Key]


def _check_ms(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数毫秒：{value!r}")
    return value


def _key(symbol: str, side: PositionSide | str) -> Key:
    """规整成纯字符串的键：枚举成员换成它的值，避免键的类型随来源不同。"""
    return str(symbol), str(PositionSide(side).value)


class Limits:
    def __init__(self, clock: Clock, *, config_get: Callable[[str], Any]) -> None:
        self._clock = clock
        self._config_get = config_get
        self._position_keys: frozenset[Key] = frozenset()
        self._positions_ms: int | None = None
        self._order_keys: frozenset[Key] = frozenset()
        self._orders_ms: int | None = None
        self._reserved: dict[int, Key] = {}
        self._order_times: dict[str, list[int]] = {}  # 合约 -> 升序的下单时间

    # ------------------------------------------------------------ 快照

    def update_positions(self, positions: Iterable[PositionInfo], at_ms: int) -> None:
        """用持仓快照整个替换上一份（数量为 0 的不算有仓）。比已收到的更旧的快照丢弃：两轮对账重叠时先发起的后返回。"""
        _check_ms(at_ms, "at_ms")
        keys = frozenset(_key(p.symbol, p.side) for p in positions if p.qty != 0)  # 先物化：可能是一次性迭代器
        if self._positions_ms is not None and at_ms < self._positions_ms:
            return
        self._position_keys, self._positions_ms = keys, at_ms

    def update_orders(self, open_orders: Iterable[OrderInfo], algo_orders: Iterable[AlgoOrderInfo], at_ms: int) -> None:
        """用挂单快照与条件单快照整个替换上一份（只数还开着的）。更旧的快照丢弃。"""
        _check_ms(at_ms, "at_ms")
        keys = {_key(o.symbol, o.position_side) for o in open_orders if o.status in _WORKING_ORDER_STATUSES}
        keys.update(_key(a.symbol, a.position_side) for a in algo_orders if a.is_open)
        if self._orders_ms is not None and at_ms < self._orders_ms:
            return
        self._order_keys, self._orders_ms = frozenset(keys), at_ms

    # ------------------------------------------------------------ 占用与上限

    def occupancy(self, *, degraded: bool, now_ms: int) -> Occupancy:
        _check_ms(now_ms, "now_ms")
        keys = self._position_keys | self._order_keys
        return Occupancy(used=len(keys), reserved=len(self._reserved), cap=self._cap(degraded),
                         stale=self._stale(now_ms), keys=keys)

    def try_reserve(self, intent_id: int, symbol: str, side: PositionSide, *, degraded: bool,
                    now_ms: int) -> RiskDecision:
        """给开仓意图要一个名额。已经拿到名额的意图重试时直接放行（不占第二个）；被拒绝的不占名额。"""
        _check_ms(now_ms, "now_ms")
        key = _key(symbol, side)
        if intent_id in self._reserved:
            return RiskDecision(True)
        seen = self.occupancy(degraded=degraded, now_ms=now_ms)
        if seen.stale:
            reason = ("持仓或挂单快照过期，快照过期按未知处理，暂不新增风险"
                      f"（持仓快照{self._age_text(self._positions_ms, now_ms)}，挂单快照{self._age_text(self._orders_ms, now_ms)}）")
            log.info("预占被拒绝（意图 %s，%s %s）：%s", intent_id, key[0], key[1], reason)
            return RiskDecision(False, reason, "stale")
        if seen.used + seen.reserved + 1 > seen.cap:
            reason = f"已达持仓上限（已占用 {seen.used}，已预占 {seen.reserved}，上限 {seen.cap}）"
            log.info("预占被拒绝（意图 %s，%s %s）：%s", intent_id, key[0], key[1], reason)
            return RiskDecision(False, reason, "")
        self._reserved[intent_id] = key
        return RiskDecision(True)

    def release(self, intent_id: int) -> None:
        """意图到终态时释放它的名额；没有预占或重复释放都无妨。"""
        self._reserved.pop(intent_id, None)

    # ------------------------------------------------------------ 订单频率

    def note_order(self, symbol: str, at_ms: int) -> None:
        """记一笔订单（时间可以乱序到达）。顺手丢掉早于该时间 10 分钟的旧记录。"""
        _check_ms(at_ms, "at_ms")
        times = self._order_times.setdefault(str(symbol), [])
        insort(times, at_ms)
        cutoff = bisect_right(times, at_ms - ORDER_WINDOW_MS)
        if cutoff:
            del times[:cutoff]

    def order_rate_ok(self, symbol: str, now_ms: int) -> bool:
        """同一合约 ``(now − 10 分钟, now]`` 内的订单数是否 < ``max_orders_per_symbol_10m``。"""
        _check_ms(now_ms, "now_ms")
        times = self._order_times.get(str(symbol), [])
        in_window = len(times) - bisect_right(times, now_ms - ORDER_WINDOW_MS)
        return in_window < int(self._config_get("max_orders_per_symbol_10m"))

    # ------------------------------------------------------------ 内部

    def _cap(self, degraded: bool) -> int:
        cap = min(int(self._config_get("max_symbols")), HARD_MAX_SYMBOLS)
        if degraded:
            cap = min(cap, int(self._config_get("max_symbols_degraded")))  # 降级只会收紧
        return cap

    def _stale(self, now_ms: int) -> bool:
        """持仓快照与挂单快照任一没收到过、或超过各自对账间隔的 2 倍没更新，就是过期。"""
        for taken_ms, key in ((self._positions_ms, "reconcile_positions_s"), (self._orders_ms, "reconcile_orders_s")):
            if taken_ms is None or now_ms - taken_ms > 2000 * self._config_get(key):
                return True
        return False

    @staticmethod
    def _age_text(taken_ms: int | None, now_ms: int) -> str:
        return "未收到" if taken_ms is None else f"已 {max(now_ms - taken_ms, 0) / 1000:.1f} 秒"
