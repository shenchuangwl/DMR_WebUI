"""订单状态机、先落盘与超时原语（第 3 层；计划报告 4.6、F1 第 9 步、F4 第 8 步、F5 第 7 步）。

- 订单 8 态（PENDING_SEND → SENT_UNKNOWN/ACKNOWLEDGED → PARTIALLY_FILLED → FILLED/CANCELLED/REJECTED，另有 CANCEL_PENDING），
  另存交易所原始状态。**先落盘后下单**：``create_pending`` 写完账本才返回，协调者之后才发请求。
- 订单状态的「单调合并」只写在 ``repo_orders.merge_view`` 一处；本服务（写结果、撤单结果、回查结果）与 ``core.ingest``
  （数据流订单事件、成交）都调用它，规则不会两处不一致。
- 本服务**只用适配层的读方法**，从不发写请求、从不自己重发：开仓单超时不重发、平仓单超时的重发与部分平仓超时的
  处理由协调者决定，这里只提供原语（超时结果 → SENT_UNKNOWN、按自定义编号回查、判定「不存在」、标记拒绝）。
- 「未知」不是「不存在」：回查得到 ``Unknown`` 只更新回查时间，不动「连续不存在」的计数；只有交易所明确答复
  「不存在」（``Known(None)``）才计数，而且计数本身也不改订单状态。
- 状态变化经账本提交之后才发布 ``OrderStateChanged``；状态没变（只是成交量增加、回查时间更新）不发布。
- 订单之间、订单与意图之间靠事件和账本协作，本模块不导入 ``core.intents``、``core.ingest``。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Mapping

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import EventBus, OrderStateChanged
from dmr_kernel.core.types import (
    AlgoOrderInfo,
    CancelOutcome,
    CancelResult,
    Known,
    OrderInfo,
    OrderPurpose,
    OrderSide,
    OrderState,
    PositionSide,
    RejectKind,
    WorkingType,
    WriteAccepted,
    WriteRejected,
    WriteResult,
    WriteUnknown,
)
from dmr_kernel.exchange import ids
from dmr_kernel.exchange.adapter import SIGN_WINDOW_MS, ExchangeAdapter
from dmr_kernel.ledger import repo_orders
from dmr_kernel.ledger.records import IntentRecord, OrderRecord
from dmr_kernel.ledger.writer import Ledger

log = logging.getLogger(__name__)

S = OrderState

KINDS = ("market", "algo")
# 落盘后超过这么久还是 PENDING_SEND（没有任何回音）的订单，也要按编号回查（F1 第 9 步「在途」）
PENDING_REQUERY_AFTER_MS = 5_000
# 判定开仓单「不存在」需要的连续「明确不存在」次数（F1 第 9 步）
ABSENT_STREAK = 3
# 只有还没得到交易所确认的订单，才谈得上「根本没到交易所」
_UNCONFIRMED = frozenset({S.PENDING_SEND, S.SENT_UNKNOWN})

_MODEL_CODES: Mapping[str, str] = {"m1": "1", "m2": "2", "m3": "3"}


class OrderNotFound(LookupError):
    """账本里没有这个自定义编号的订单。"""


class DuplicateOrder(Exception):
    """这个意图的这个分腿已经建过订单（编号相同）：不写第二行，已有的在 ``existing`` 里。"""

    def __init__(self, existing: OrderRecord) -> None:
        super().__init__(f"订单 {existing.client_order_id} 已存在（状态 {existing.state.value}），不重复创建")
        self.existing = existing


def model_code(model: str) -> str:
    """订单编号里的模型代码：m1→"1"，m2→"2"，m3→"3"；其它（op、protection、cli……）一律 "0"。"""
    return _MODEL_CODES.get(str(model), "0")


def _check_decimal(name: str, value: object, *, positive: bool = True) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"订单的 {name} 必须是有限的 Decimal：{value!r}")
    if positive and value <= 0:
        raise ValueError(f"订单的 {name} 必须大于 0：{value!r}")
    return value


def _validate_new(*, kind: str, order_type: str, qty: Decimal | None, trigger_price: Decimal | None,
                  working_type: WorkingType | None, close_position: bool) -> None:
    if kind not in KINDS:
        raise ValueError(f"订单 kind 只能是 {'、'.join(KINDS)}：{kind!r}")
    if not isinstance(order_type, str) or not order_type.strip():
        raise ValueError("订单的 order_type 不能为空")
    if kind == "market":
        if close_position:
            raise ValueError("只有条件单可以是全平型（close_position）")
        if trigger_price is not None or working_type is not None:
            raise ValueError("市价单没有触发价与触发价类型")
        _check_decimal("qty", qty)
        return
    # 条件单：触发价与触发价类型必填；全平型不得带数量，数量型必须带
    _check_decimal("trigger_price", trigger_price)
    if working_type is None:
        raise ValueError("条件单必须指定触发价类型（working_type）")
    if close_position:
        if qty is not None:
            raise ValueError("全平型条件单不得带数量")
    else:
        _check_decimal("qty", qty)


class OrderService:
    def __init__(self, ledger: Ledger, bus: EventBus, clock: Clock, adapter: ExchangeAdapter, *,
                 instance: str) -> None:
        # 实例号 = 设置里的实例号 + 账本随机串；订单编号的实例部分只允许 1–4 位字母数字，这里提前拒绝
        ids.make_client_id(instance, "0", 0, "o")
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._adapter = adapter
        self._instance = instance

    # ------------------------------------------------------------ 先落盘

    async def create_pending(self, *, intent: IntentRecord, leg: str, purpose: OrderPurpose, kind: str,
                             order_type: str, side: OrderSide, position_side: PositionSide, qty: Decimal | None,
                             trigger_price: Decimal | None = None, working_type: WorkingType | None = None,
                             close_position: bool = False, resend_of: str | None = None) -> OrderRecord:
        """新建 PENDING_SEND 的订单并落盘（返回时已提交）。协调者拿到返回值才可以向交易所发请求。

        编号 = 实例 + 模型代码 + 意图号 + 分腿；同一意图的同一分腿只能建一次（``DuplicateOrder``），
        重发要换新的分腿（``ids.resend_leg``）并用 ``resend_of`` 指向原单。
        """
        _validate_new(kind=kind, order_type=order_type, qty=qty, trigger_price=trigger_price,
                      working_type=working_type, close_position=close_position)
        client_order_id = ids.make_client_id(self._instance, model_code(intent.model), intent.id, leg)
        now = self._clock.now_ms()
        record, created = await self._ledger.write(
            repo_orders.create_pending, client_order_id=client_order_id, intent_id=intent.id, leg=leg,
            purpose=purpose, symbol=intent.symbol, side=side, position_side=position_side, kind=kind,
            order_type=order_type, qty=qty, trigger_price=trigger_price, working_type=working_type,
            close_position=close_position, resend_of=resend_of, now_ms=now)
        if not created:
            raise DuplicateOrder(record)
        self._publish(record, now)
        return record

    async def record_sent(self, client_order_id: str) -> OrderRecord:
        """记下首次发出请求的时间（只记一次，状态不变：回执之前仍是 PENDING_SEND）。"""
        now = self._clock.now_ms()
        outcome = await self._ledger.write(repo_orders.record_sent, client_order_id, now_ms=now)
        return self._finish(client_order_id, outcome, now)

    # ------------------------------------------------------------ 写结果与撤单结果

    async def apply_write_result(self, client_order_id: str, result: WriteResult) -> OrderRecord:
        """下单（或挂条件单）的结果。受理 → 按交易所状态合并；拒绝 → REJECTED（记错误码与原因）；未知 → SENT_UNKNOWN。

        数据流可能比写结果先到：已经前进的订单不会被晚到的「未知」或「拒绝」拉回去（有成交的订单更不会变成 REJECTED）。
        ``-4116`` 编号重复说明先前那次请求可能已经到了交易所，不是拒绝：按未知处理，等回查。
        """
        now = self._clock.now_ms()
        if isinstance(result, WriteAccepted):
            view = result.order if result.order is not None else result.algo
            outcome = await self._ledger.write(repo_orders.merge_view, client_order_id, view, now_ms=now,
                                               target=S.ACKNOWLEDGED)
        elif isinstance(result, WriteRejected):
            if result.kind is RejectKind.DUPLICATE_CLIENT_ID:
                outcome = await self._ledger.write(repo_orders.mark_unknown, client_order_id, now_ms=now,
                                                   error_code=result.code, error_msg=result.message)
            else:
                outcome = await self._ledger.write(repo_orders.reject, client_order_id, now_ms=now,
                                                   error_code=result.code, error_msg=result.message,
                                                   from_states=_UNCONFIRMED, audit=False)
        elif isinstance(result, WriteUnknown):
            outcome = await self._ledger.write(repo_orders.mark_unknown, client_order_id, now_ms=now,
                                               error_code=result.code, error_msg=result.reason)
        else:
            raise TypeError(f"不认识的写结果：{type(result).__name__}")
        return self._finish(client_order_id, outcome, now)

    async def apply_cancel_result(self, client_order_id: str, result: CancelResult) -> OrderRecord:
        """撤单的结果。已撤 → CANCELLED（已成交部分照记）；「不存在」不能直接当成已撤，回查后再说；未知 → CANCEL_PENDING。"""
        now = self._clock.now_ms()
        if result.outcome is CancelOutcome.CANCELLED:
            view = result.order if result.order is not None else result.algo
            outcome = await self._ledger.write(repo_orders.merge_view, client_order_id, view, now_ms=now,
                                               target=S.CANCELLED)
        elif result.outcome is CancelOutcome.NOT_FOUND:
            return await self.requery(client_order_id)
        else:
            outcome = await self._ledger.write(repo_orders.mark_cancel_unknown, client_order_id, now_ms=now,
                                               error_code=result.code, error_msg=result.message)
        return self._finish(client_order_id, outcome, now)

    # ------------------------------------------------------------ 交易所视图与回查

    async def apply_exchange_view(self, client_order_id: str, view: OrderInfo | AlgoOrderInfo | None, *,
                                  source: str) -> OrderRecord:
        """把交易所给的订单视图单调地并进订单。``None`` 是交易所明确答复「不存在」：连续不存在次数 +1，
        不改订单状态。``source`` 是 ``stream``（数据流，不算回查）、``query``/``reconcile``（主动读取，记回查时间）。"""
        now = self._clock.now_ms()
        if view is None:
            outcome = await self._ledger.write(repo_orders.note_absent, client_order_id, now_ms=now)
        else:
            outcome = await self._ledger.write(repo_orders.merge_view, client_order_id, view, now_ms=now,
                                               queried=source != "stream")
        return self._finish(client_order_id, outcome, now)

    async def requery(self, client_order_id: str) -> OrderRecord:
        """按自定义编号回查（只用适配层的读方法）。明确不存在 → 计数；查到 → 合并；``Unknown`` 只更新回查时间。"""
        order = await self.get(client_order_id)
        if order is None:
            raise OrderNotFound(client_order_id)
        result = await self._fetch(order)
        if isinstance(result, Known):
            return await self.apply_exchange_view(client_order_id, result.value, source="query")
        now = self._clock.now_ms()
        outcome = await self._ledger.write(repo_orders.note_query_unknown, client_order_id, now_ms=now)
        return self._finish(client_order_id, outcome, now)

    async def requery_unknown(self) -> list[OrderRecord]:
        """回查全部命运未定的订单：SENT_UNKNOWN、CANCEL_PENDING，以及落盘（发出）后超过 5 秒仍是 PENDING_SEND 的。
        返回回查之后的记录；到了终态的订单下次不再回查。"""
        now = self._clock.now_ms()
        candidates = await self._ledger.read(repo_orders.requery_candidates, now_ms=now,
                                             pending_after_ms=PENDING_REQUERY_AFTER_MS)
        results: list[OrderRecord] = []
        for order in candidates:
            try:
                results.append(await self.requery(order.client_order_id))
            except Exception:  # 一张订单回查失败不能挡住其它订单；下一轮会再查
                log.exception("订单回查失败：%s", order.client_order_id)
        return results

    def judge_absent(self, order: OrderRecord, *, now_ms: int, grace_s: int, position_unchanged: bool) -> bool:
        """F1 第 9 步：请求发出已超过签名时间窗加宽限、连续 3 次查到不存在、该侧持仓没有变化，三条同时满足才 True。

        没有记录发出时间的订单（落盘后一直没有标记）从建立时间算。只有还没得到交易所确认的订单才谈得上「不存在」。
        """
        if order.state not in _UNCONFIRMED:
            return False
        since = order.sent_ms if order.sent_ms is not None else order.created_ms
        waited_long_enough = now_ms - since > SIGN_WINDOW_MS + grace_s * 1000
        return waited_long_enough and order.absent_count >= ABSENT_STREAK and position_unchanged

    async def mark_rejected(self, client_order_id: str, *, reason: str) -> OrderRecord:
        """把没有得到交易所确认、也没有任何成交的订单置为 REJECTED（同一事务写审计记录）。
        已到终态的、有成交的、已被交易所确认过的订单不改。"""
        now = self._clock.now_ms()
        outcome = await self._ledger.write(repo_orders.reject, client_order_id, now_ms=now, error_code=None,
                                           error_msg=reason, from_states=_UNCONFIRMED, audit=True)
        return self._finish(client_order_id, outcome, now)

    # ------------------------------------------------------------ 查询

    async def get(self, client_order_id: str) -> OrderRecord | None:
        return await self._ledger.read(repo_orders.get, client_order_id)

    async def by_exchange_order_id(self, exchange_order_id: str) -> OrderRecord | None:
        return await self._ledger.read(repo_orders.by_exchange_order_id, exchange_order_id)

    async def by_algo_id(self, algo_id: str) -> OrderRecord | None:
        return await self._ledger.read(repo_orders.by_algo_id, algo_id)

    async def by_actual_order_id(self, actual_order_id: str) -> OrderRecord | None:
        return await self._ledger.read(repo_orders.by_actual_order_id, actual_order_id)

    async def for_intent(self, intent_id: int) -> list[OrderRecord]:
        return await self._ledger.read(repo_orders.for_intent, intent_id)

    async def non_terminal(self, *, symbol: str | None = None, side: PositionSide | OrderSide | None = None,
                           purpose: OrderPurpose | None = None) -> list[OrderRecord]:
        """未到终态的订单（按建立顺序）。``side`` 给持仓方向就按持仓方向筛，给买卖方向就按买卖方向筛。"""
        return await self._ledger.read(repo_orders.non_terminal, symbol=symbol, side=side, purpose=purpose)

    async def link_actual_order(self, algo_id: str, actual_order_id: str) -> None:
        """条件单触发后生成的订单号（实测第 12 项「可关联」时）。已经关联过的不改；不是我们的条件单也无害。"""
        await self._ledger.write(repo_orders.link_actual_order, algo_id, actual_order_id,
                                 now_ms=self._clock.now_ms())

    # ------------------------------------------------------------ 内部

    async def _fetch(self, order: OrderRecord):
        if order.kind == "algo":
            # 已结束的条件单只能按 algo_id 查：知道就用它，不知道才用自定义编号
            if order.algo_id:
                return await self._adapter.fetch_algo_order(order.symbol, client_algo_id=None, algo_id=order.algo_id)
            return await self._adapter.fetch_algo_order(order.symbol, client_algo_id=order.client_order_id,
                                                        algo_id=None)
        return await self._adapter.fetch_order(order.symbol, client_order_id=order.client_order_id,
                                               exchange_order_id=None)

    def _finish(self, client_order_id: str, outcome: tuple[OrderRecord, OrderRecord] | None,
                now_ms: int) -> OrderRecord:
        """账本已提交：没有这张订单就报错；状态变了才发布事件。返回改后的记录。"""
        if outcome is None:
            raise OrderNotFound(client_order_id)
        before, after = outcome
        if after.state is not before.state:
            self._publish(after, now_ms)
        return after

    def _publish(self, order: OrderRecord, at_ms: int) -> None:
        self._bus.publish(OrderStateChanged(client_order_id=order.client_order_id, state=order.state.value,
                                            intent_id=order.intent_id, symbol=order.symbol,
                                            position_side=order.position_side, at_ms=at_ms))
