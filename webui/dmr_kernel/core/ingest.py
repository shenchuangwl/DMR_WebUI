"""成交与收益流水入账的编排（第 3 层；计划报告 4.3「core.ingest」、F1 第 7 步、F3 第 2 步、T0.11）。

- **两条路径，一笔成交只入账一次**：数据流的成交回报（``StreamOrderReceived``）实时入账；每 60 秒按合约水位补拉
  成交（``backfill_trades``）兜底。两条路径都走 ``ingest_trade``，按成交号去重在账本的写事务里最终裁决
  （并发到达也只有一个写入者成功）。
- **归类**：先按订单表认领为系统成交（自定义编号 → 交易所订单号 → 条件单触发后订单号），带上意图号与订单用途；
  认领不到再按编号前缀归类（``ids.parse_client_id``；系统前缀但实例号不是本实例的算外部）；REST 成交没有编号时先沿用
  同一张订单已入账成交里的编号，没有就用交易所订单号回查一次订单，回查不到来源记 UNKNOWN。
- **订单的合并**：写 fill 行与合并对应订单在同一个事务里（``repo_ingest.record_trade`` → ``repo_orders.merge_view``），
  订单状态的单调合并只有那一处，订单服务（``core.orders``）与本模块共用。没有成交的订单事件（NEW/CANCELED/EXPIRED/
  REJECTED……）直接合并到系统订单。
- **事件**：先发布 ``FillIngested``，再在订单状态变化时发布 ``OrderStateChanged``。条件单事件原样转发成
  ``AlgoOrderObserved(source="stream")``，同时合并进系统条件单（状态、条件单号、触发后订单号）。
- 订单与条件单的数据流事件由**同一个订阅**按到达顺序逐个处理：条件单事件带来的「触发后订单号」要先于触发后订单的
  成交处理，才认领得到。
- **补拉的失败要报出来**：``backfill_trades`` 返回 ``BackfillResult``——本身就是新入账的笔数（``int`` 的子类，``== 2``、``+=``
  等现有用法照旧），另带 ``failed``（``frozenset[str]``）：这一次没能补拉完整的合约。读成交返回未知、补拉这个合约时出错（账本忙等）、
  翻页达到上限，都算；读失败不是「没有新成交」。读到了而没有新成交的合约、被丢弃的无效成交（永久性的坏数据）不算。
  ``failed`` 只属于这一次调用，没有需要清除的状态；``core.reconcile`` 据此在这些合约上不下结论。
- 只用适配层的读方法；不碰登记簿（那是 ``core.registry`` 的事，它订阅 ``FillIngested``）；不导入 ``core.orders``、
  ``core.intents``（同层，靠事件和账本里的仓储函数协作）。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Callable, Iterable

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import (
    AlgoOrderObserved,
    EventBus,
    FillIngested,
    IncomeIngested,
    OrderStateChanged,
    StreamAlgoReceived,
    StreamOrderReceived,
    Subscription,
)
from dmr_kernel.core.types import (
    FillSource,
    Known,
    OrderInfo,
    OrderOrigin,
    OrderState,
    StreamAlgo,
    StreamOrder,
    TradeInfo,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.adapter import ExchangeAdapter
from dmr_kernel.ledger import repo_ingest, repo_orders
from dmr_kernel.ledger.records import OrderRecord
from dmr_kernel.ledger.writer import Ledger

log = logging.getLogger(__name__)

PATHS = ("stream", "rest")
# 成交来源（FillSource）：系统、应急、强平、自动减仓、旧程序；其余（外部、编号漏传、没有编号）都算外部
_SOURCE_BY_ORIGIN = {
    OrderOrigin.SYSTEM: FillSource.SYSTEM,
    OrderOrigin.EMERGENCY: FillSource.EMERGENCY,
    OrderOrigin.LIQUIDATION: FillSource.LIQUIDATION,
    OrderOrigin.ADL: FillSource.ADL,
    OrderOrigin.LEGACY: FillSource.LEGACY,
}


def _validate_trade(trade: TradeInfo, path: str) -> None:
    if path not in PATHS:
        raise ValueError(f"成交来源路径只能是 {'、'.join(PATHS)}：{path!r}")
    for name in ("trade_id", "exchange_order_id", "symbol"):
        value = getattr(trade, name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"成交的 {name} 不能为空：{value!r}")
    for name in ("qty", "price"):
        value = getattr(trade, name)
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"成交 {trade.trade_id} 的 {name} 必须是大于 0 的 Decimal：{value!r}")
    for name in ("commission", "realized_pnl"):
        value = getattr(trade, name)
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"成交 {trade.trade_id} 的 {name} 必须是有限的 Decimal：{value!r}")


class BackfillResult(int):
    """``Ingest.backfill_trades`` 的返回值：本身就是这一次新入账的笔数（``int`` 的子类），``failed`` 是这一次没能补拉完整的
    合约（读成交返回未知、补拉出错、翻页达到上限）。见模块说明。"""

    failed: frozenset[str]

    def __new__(cls, count: int = 0, failed: Iterable[str] = ()) -> "BackfillResult":
        result = super().__new__(cls, count)
        result.failed = frozenset(failed)
        return result


def _trade_order(trade: TradeInfo) -> tuple[int, int, str]:
    """补拉时按成交时间排；同一毫秒的按成交号的数值排（字符串排序会把 "10" 排在 "9" 前面）。"""
    return trade.time_ms, int(trade.trade_id) if trade.trade_id.isdigit() else 0, trade.trade_id


class Ingest:
    # 补拉的参数。类属性：测试里可以改成小值
    TRADE_OVERLAP_MS = 60_000  # 成交：从水位往前重叠 60 秒（交易所偶尔晚给）
    INCOME_OVERLAP_MS = 600_000  # 收益流水：每小时拉一次，重叠 10 分钟
    INITIAL_LOOKBACK_MS = 24 * 3_600_000  # 第一次补拉（没有水位）往前看多久
    PAGE_LIMIT = 500  # 一次最多取多少笔成交
    INCOME_PAGE_LIMIT = 1000  # 一次最多取多少条收益流水
    MAX_PAGES = 20  # 一次补拉最多翻几页

    def __init__(self, ledger: Ledger, bus: EventBus, clock: Clock, adapter: ExchangeAdapter, *, instance: str,
                 config_get: Callable[[str], Any]) -> None:
        # 实例号 = 设置里的实例号 + 账本随机串；用来分辨「系统前缀但不是本实例」的订单编号
        ids.make_client_id(instance, "0", 0, "o")
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._adapter = adapter
        self._instance = instance
        self._config_get = config_get  # 入账本身没有可调参数；保留给后续（限频、补拉间隔等）使用
        self._subscription: Subscription | None = None

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """订阅账户数据流的订单事件与条件单事件（同一个订阅，按到达顺序处理）。重复调用无害。"""
        if self._subscription is None:
            self._subscription = self._bus.subscribe((StreamOrderReceived, StreamAlgoReceived), self._on_stream,
                                                     name="ingest.stream")

    async def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None

    # ------------------------------------------------------------ 数据流事件

    async def _on_stream(self, event: StreamOrderReceived | StreamAlgoReceived) -> None:
        if isinstance(event, StreamOrderReceived):
            await self._on_order(event.payload)
        else:
            await self._on_algo(event.payload)

    async def _on_order(self, payload: StreamOrder) -> None:
        """有成交的走 ``ingest_trade``；没有成交的（NEW/CANCELED/EXPIRED/REJECTED/AMENDMENT）合并到系统订单。
        成交是重复的（重放、补拉先到）时，订单视图也要合并：``raw_status`` 等要跟上。"""
        view = payload.order
        if payload.fill is not None:
            if await self.ingest_trade(payload.fill, client_order_id=view.client_order_id, path="stream",
                                       view=view) is not None:
                return
        now = self._clock.now_ms()
        outcome = await self._ledger.write(repo_ingest.merge_order_event, view, now_ms=now)
        self._publish_order_change(outcome, now)

    async def _on_algo(self, payload: StreamAlgo) -> None:
        """转发成 ``AlgoOrderObserved``；同时合并进系统条件单（条件单状态，带触发后订单号的就关联上）。"""
        algo = payload.algo
        now = self._clock.now_ms()
        try:
            outcome = await self._ledger.write(repo_ingest.merge_algo_event, algo, now_ms=now)
            self._publish_order_change(outcome, now)
        finally:  # 账本写失败也要让别的模块知道看到了这张条件单
            self._bus.publish(AlgoOrderObserved(algo=algo, source="stream", at_ms=now))

    # ------------------------------------------------------------ 成交入账

    async def ingest_trade(self, trade: TradeInfo, *, client_order_id: str | None, path: str,
                           view: OrderInfo | None = None) -> FillIngested | None:
        """一笔成交入账。已入账（成交号已有）返回 None；否则返回已发布的 ``FillIngested``。

        1. 按成交号去重；2. 归类（见模块说明）；3. 同一事务写 fill 行并合并对应订单；4. 发布事件。
        ``view`` 是数据流成交回报里的订单视图（可选）：认领到系统订单时一并合并。
        """
        _validate_trade(trade, path)
        now = self._clock.now_ms()
        duplicate, claimed, sibling = await self._ledger.read(
            repo_ingest.lookup, symbol=trade.symbol, trade_id=trade.trade_id, client_order_id=client_order_id,
            exchange_order_id=trade.exchange_order_id)
        if duplicate:
            return None
        resolved_id = client_order_id
        if claimed is None and resolved_id is None:
            resolved_id = sibling
            if resolved_id is None:
                fetched = await self._fetch_order(trade)
                if fetched is not None:
                    resolved_id = fetched.client_order_id
                    view = view if view is not None else fetched
            if resolved_id is not None:  # 回查到的编号可能就是订单表里的系统订单（它的订单号还没记下来）
                claimed = await self._ledger.read(repo_orders.get, resolved_id)
        origin, source = self._classify(claimed, resolved_id)
        if origin is OrderOrigin.CCXT_DEFAULT:  # 系统漏传自定义编号时 ccxt 自动生成的：必须当作异常（报告 4.6）
            log.error("成交 %s 的订单编号是 ccxt 自动生成的（%s）：系统下单漏传了自定义编号", trade.trade_id, resolved_id)

        outcome = await self._ledger.write(repo_ingest.record_trade, trade=trade, client_order_id=resolved_id,
                                           origin=origin, source=source, path=path, now_ms=now, view=view)
        if outcome is None:  # 读到写之间，别的路径已经入账了
            return None
        fill, before, after = outcome
        on_dead_order = (before is not None and after is not None
                         and before.state in (OrderState.REJECTED, OrderState.CANCELLED)
                         and after.filled_qty > before.filled_qty)
        event = FillIngested(fill_id=fill.id, trade=trade, client_order_id=fill.client_order_id, origin=fill.origin,
                             intent_id=fill.intent_id, order_purpose=None if before is None else before.purpose.value,
                             path=path, at_ms=now, on_dead_order=on_dead_order)
        self._bus.publish(event)
        self._publish_order_change(None if before is None or after is None else (before, after), now)
        return event

    def _classify(self, claimed: OrderRecord | None, client_order_id: str | None) -> tuple[OrderOrigin, FillSource]:
        if claimed is not None:
            return OrderOrigin.SYSTEM, FillSource.SYSTEM
        parsed = ids.parse_client_id(client_order_id)
        origin = parsed.origin
        if origin is OrderOrigin.SYSTEM and parsed.instance != self._instance:
            origin = OrderOrigin.EXTERNAL  # 别的实例（比如换库之前）下的单：不是本账本的订单，当外部处理
        return origin, _SOURCE_BY_ORIGIN.get(origin, FillSource.EXTERNAL)

    async def _fetch_order(self, trade: TradeInfo) -> OrderInfo | None:
        """REST 成交没有编号：用交易所订单号回查一次订单。查不到（未知或明确不存在）返回 None，来源记 UNKNOWN。"""
        result = await self._adapter.fetch_order(trade.symbol, client_order_id=None,
                                                 exchange_order_id=trade.exchange_order_id)
        if isinstance(result, Known) and result.value is not None:
            return result.value
        return None

    # ------------------------------------------------------------ 补拉

    async def backfill_trades(self, symbols: Iterable[str]) -> BackfillResult:
        """每 60 秒：按合约水位（上次见到的成交时间 − 60 秒重叠）拉成交，逐笔 ``ingest_trade(path="rest")``。
        返回 ``BackfillResult``：新入账的笔数（``int``），``failed`` 是没能补拉完整的合约（读成交返回未知、补拉出错、翻页达到
        上限——读失败不是「没有新成交」）。某个合约读失败（未知）或入账失败不影响别的合约，水位也不推进，下一轮再补。"""
        counter = [0]
        failed: set[str] = set()
        for symbol in dict.fromkeys(symbols):
            try:
                complete = await self._backfill_symbol(symbol, counter)
            except Exception:
                log.exception("补拉成交失败：%s", symbol)
                complete = False
            if not complete:
                failed.add(symbol)
        return BackfillResult(counter[0], failed)

    async def _backfill_symbol(self, symbol: str, counter: list[int]) -> bool:
        """补拉一个合约。补完了返回 True；读成交返回未知、或翻页达到上限还没补完返回 False（出错则抛出）。"""
        mark = await self._ledger.read(repo_ingest.trade_watermark, symbol)
        start = self._clock.now_ms() - self.INITIAL_LOOKBACK_MS if mark is None else mark - self.TRADE_OVERLAP_MS
        start = max(0, start)
        for _ in range(self.MAX_PAGES):
            result = await self._adapter.fetch_trades(symbol, start, None, limit=self.PAGE_LIMIT)
            if not isinstance(result, Known):
                log.warning("补拉成交读取失败（未知）：%s", symbol)  # 「未知」不是「没有成交」：水位不动
                return False
            trades = list(result.value)
            newest = max((t.time_ms for t in trades), default=None)
            known = (await self._ledger.read(repo_ingest.known_trade_ids, symbol, [t.trade_id for t in trades])
                     if trades else set())
            for trade in sorted((t for t in trades if t.trade_id not in known), key=_trade_order):
                try:
                    event = await self.ingest_trade(trade, client_order_id=None, path="rest")
                except ValueError:
                    log.error("丢弃无效成交 %s：%r", trade.trade_id, trade, exc_info=True)
                    continue
                if event is not None:
                    counter[0] += 1
            # 一页全部处理完（失败会抛出，不会走到这里）才推进水位
            if newest is not None and (mark is None or newest > mark):
                await self._ledger.write(repo_ingest.set_trade_watermark, symbol, newest)
                mark = newest
            if newest is None or len(trades) < self.PAGE_LIMIT:
                return True
            # 满页：可能还有。下一页从本页最晚的成交时间开始（同一毫秒的成交可能被页尾截断，所以重叠一笔）；
            # 整页都在同一毫秒时没有进展，只能越过去
            start = newest if newest > start else start + 1
        log.warning("补拉成交翻页达到上限（%s 页）：%s", self.MAX_PAGES, symbol)
        return False  # 没补完：水位已前进到取到的最晚一笔，下一轮接着补

    async def backfill_income(self) -> int:
        """每小时：拉收益流水（所有类型），按 ``income_id`` 去重写 income 表，逐条发布 ``IncomeIngested``。
        返回新入账的条数。读取失败（未知）返回 0，水位不动。"""
        mark = await self._ledger.read(repo_ingest.get_watermark, repo_ingest.INCOME_WM_KEY)
        start = self._clock.now_ms() - self.INITIAL_LOOKBACK_MS if mark is None else mark - self.INCOME_OVERLAP_MS
        start = max(0, start)
        total = 0
        for _ in range(self.MAX_PAGES):
            result = await self._adapter.fetch_income(start, None, None, limit=self.INCOME_PAGE_LIMIT)
            if not isinstance(result, Known):
                log.warning("补拉收益流水读取失败（未知）")
                return total
            items = sorted(result.value, key=lambda item: (item.time_ms, item.income_id))
            now = self._clock.now_ms()
            fresh = await self._ledger.write(repo_ingest.record_incomes, items, now_ms=now)
            for row in fresh:
                self._bus.publish(IncomeIngested(income_id=row.income_id, income_type=row.income_type,
                                                 symbol=row.symbol, amount=row.amount, at_ms=now))
            total += len(fresh)
            newest = items[-1].time_ms if items else None
            if newest is not None and (mark is None or newest > mark):
                await self._ledger.write(repo_ingest.set_watermark, repo_ingest.INCOME_WM_KEY, newest)
                mark = newest
            if newest is None or len(items) < self.INCOME_PAGE_LIMIT:
                return total
            start = newest if newest > start else start + 1
        log.warning("补拉收益流水翻页达到上限（%s 页）", self.MAX_PAGES)
        return total

    # ------------------------------------------------------------ 内部

    def _publish_order_change(self, outcome: tuple[OrderRecord, OrderRecord] | None, at_ms: int) -> None:
        """订单状态变了才发布 ``OrderStateChanged``。"""
        if outcome is None:
            return
        before, after = outcome
        if after.state is not before.state:
            self._bus.publish(OrderStateChanged(client_order_id=after.client_order_id, state=after.state.value,
                                                intent_id=after.intent_id, symbol=after.symbol,
                                                position_side=after.position_side, at_ms=at_ms))
