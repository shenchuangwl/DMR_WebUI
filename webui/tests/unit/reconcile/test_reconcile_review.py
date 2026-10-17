"""独立复核（T0.17）补的用例：每条先复现问题（修复前失败），再固定修复后的行为。

1. 撤单意图落了盘却没派发（提交后进程被杀；协调者重启后不会自己去派发没有订单行的意图）：对账要用同一个幂等键
   再交给协调者，否则孤儿与旧止损永远撤不掉，该键也一直「在途」。
2. 登记仓的当前止损不在交易所时，待清理的旧止损可能是它唯一的止损：先留着，等当前止损回来再撤；
   待清理列表里若出现已移交给用户的止损（登记异常），仓位非零时也不撤。
3. 准备撤单的间隙里登记簿认领了这张单（成了当前止损），或并发的一轮持仓对账读到这一侧有仓了：不撤。
4. 查「系统成交还没入账」读不出来是未知，不是「没有」。
5. 数量差异由系统自己已成交、成交明细还没入账的订单解释（成交接口也还查不到）：先不判，不是「对账不一致」。
6. 本机时钟偏了、签名请求被拒时只有服务器时间读得到：时钟偏差照样要比对（这正是最需要它的时候）。
7. 补拉成交与复核读数之间仓位又变了（这一轮不下结论，整轮 unknown），或这个键上刚有系统意图开始在途（同在途一样先不判）：
   登记簿可能还没跟上，都不能判成「对账不一致」。
8. 进程内崩溃点（CrashSignal，模拟进程被杀）绝不被吞掉，锁照常释放（覆盖补充，修复前也通过）。

第二次复核（接续）补的：

9. 孤儿的第 5 条「明确为零」要真能说明这张单现在没有在保护仓位：零读数早于这张单的建立（新鲜窗口 2×持仓周期
   可以长于条件单周期）说明不了什么；登记簿认为这一侧还有仓时（复核读数可能正看到它），零读数也不作数。
   两种情形都先不撤、不告警，等登记簿跟上、等建立之后的快照。
10. 补拉成交前后两次读数之间仓位在变（首读为零、复核却是别的数量；首读有仓、复核为零）：这一轮对这个键不下结论，
    整轮不是 ok（core.guard 据 ok 结束「数据流切换中」）。
11. 条件单快照的时间是查询发出的时间（保护单管理拿它与最近一次挂/换止损的时间比，早于挂单的快照不作数）。
12. 外部保护的登记仓：这份快照里这一侧看不到用户的止损类条件单时（用户刚撤了它，登记还没跟上），旧的系统止损
    可能是它唯一的止损，同 2 一样先留着（见第 2 节的两个外部保护用例）。
"""
from __future__ import annotations

from typing import Any

import pytest

from dmr_kernel.core.crashpoints import CrashSignal
from dmr_kernel.core.events import OrdersSnapshotTaken, UnregisteredPositionFound
from dmr_kernel.core.types import (
    AlgoType,
    IntentAction,
    IntentSource,
    IntentState,
    Known,
    OrderInfo,
    OrderPurpose,
    ProtectionState,
    Suppression,
    WriteAccepted,
)
from dmr_kernel.ledger import repo_reconcile
from reconcile_kit import BUY, D, LONG, SELL, SHORT, SYM, algo, pos, trade, web_view

PERIOD_MS = 60_000  # reconcile_orders_s 的默认值

_STEPS = {
    IntentState.CREATED: (),
    IntentState.DISPATCH_READY: (IntentState.VALIDATED, IntentState.DISPATCH_READY),
    IntentState.RECONCILING: (IntentState.VALIDATED, IntentState.DISPATCH_READY, IntentState.RECONCILING),
}


async def earlier_attempt(env, key: str, stop, state: IntentState):
    """上一个进程交给协调者的撤单意图：落了盘，停在 ``state``。"""
    rec = await env.new_intent(action=IntentAction.STOP_CANCEL, model="op", source=IntentSource.RECONCILE,
                               symbol=stop.symbol, side=stop.position_side, state=IntentState.CREATED, key=key,
                               params={"client_algo_id": stop.client_algo_id, "algo_id": stop.algo_id})
    for step in _STEPS[state]:
        rec = await env.intents.transition(rec.id, step, reason="测试")
    return rec


async def open_diffs(env):
    return await env.ledger.read(repo_reconcile.list_diffs, open_only=True)


# ================================================================ 1. 没派发的撤单意图要再交给协调者


@pytest.mark.parametrize("state", [IntentState.CREATED, IntentState.DISPATCH_READY])
async def test_an_orphan_cancel_left_undispatched_is_handed_to_the_coordinator_again(make_env, clock, state):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")  # 孤儿：意图已完成、登记簿不引用、建立已 120 秒
    stuck = await earlier_attempt(env, "orphan:9001", stop, state)
    clock.advance(PERIOD_MS + 1)  # 停在未派发的状态已超过一个条件单对账周期
    assert await env.reconciler.reconcile_positions() == "ok"  # 新鲜的快照：这一侧明确为零

    assert await env.reconciler.reconcile_orders() == "ok"

    [spec] = env.coordinator.submitted
    assert spec.idem_key == "orphan:9001"  # 同一个幂等键：协调者据此重新派发，不会多出第二个意图
    assert (spec.action, spec.symbol, spec.side, spec.position_id) == (IntentAction.STOP_CANCEL, SYM, LONG, None)
    assert spec.params == {"client_algo_id": stop.client_algo_id, "algo_id": "9001"}
    assert [i.id for i in await env.intents.non_terminal()] == [stuck.id]


async def test_a_cancel_attempt_that_is_only_queued_or_already_sent_is_not_handed_over_again(make_env, clock):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    await earlier_attempt(env, "orphan:9001", stop, IntentState.CREATED)  # 刚交出去：协调者还在排队派发
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_orders()
    assert env.coordinator.submitted == []

    other = await env.system_stop(symbol="ETHUSDT", algo_id="9002")
    await earlier_attempt(env, "orphan:9002", other, IntentState.RECONCILING)  # 请求已发出、结果未知：协调者自己回查
    clock.advance(10 * PERIOD_MS)
    await env.reconciler.reconcile_positions()
    await env.reconciler.reconcile_orders()
    assert [s.idem_key for s in env.coordinator.submitted] == ["orphan:9001"]  # 只有停在未派发状态的那个


async def test_an_undispatched_orphan_cancel_is_not_resumed_once_the_position_is_no_longer_zero(make_env, clock):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    await earlier_attempt(env, "orphan:9001", stop, IntentState.CREATED)
    clock.advance(PERIOD_MS + 1)
    env.positions = [pos(SYM, LONG, "1")]  # 这一侧又有仓了：五条不再同时满足
    await env.reconciler.reconcile_positions()

    await env.reconciler.reconcile_orders()

    assert env.coordinator.submitted == []
    assert [a.kind for a in env.alerts("orphan_stop_kept")] == ["orphan_stop_kept"]  # 自己的撤单尝试不算「别的引用」


async def test_a_stale_cancel_left_undispatched_is_handed_to_the_coordinator_again(make_env, clock):
    env = await make_env().boot()
    current = await env.system_stop(algo_id="9002", trigger="97")
    old = await env.system_stop(algo_id="9001", trigger="95")
    rec = await env.add_position(stop=(current.client_algo_id, "9002", "97"))
    await env.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id="9001")
    await earlier_attempt(env, f"stale:{rec.id}:9001", old, IntentState.CREATED)
    clock.advance(PERIOD_MS + 1)

    await env.reconciler.reconcile_orders()

    [spec] = env.coordinator.submitted
    assert (spec.idem_key, spec.position_id) == (f"stale:{rec.id}:9001", rec.id)


# ================================================================ 2. 当前止损不在交易所时，旧止损先留着


@pytest.mark.parametrize("status", ["managed", "closing", "taking_over"])
async def test_a_stale_stop_is_kept_while_the_current_stop_is_missing_from_the_exchange(make_env, status):
    env = await make_env().boot()
    current = await env.system_stop(algo_id="9002", trigger="97", add_to_exchange=False)  # 当前止损被人手动撤了
    old = await env.system_stop(algo_id="9001", trigger="95")
    rec = await env.add_position(status=status, stop=(current.client_algo_id, "9002", "97"))
    await env.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id="9001")

    assert await env.reconciler.reconcile_orders() == "ok"

    assert env.coordinator.submitted == []  # 旧止损这时是这个仓唯一的止损：撤了就裸奔
    assert (await env.registry.fetch(rec.id)).stale_stops == [
        {"client_algo_id": old.client_algo_id, "algo_id": "9001"}]

    env.algos.append(current)  # 保护单管理把当前止损补回来了：下一轮照常撤旧单
    await env.reconciler.reconcile_orders()
    [spec] = env.coordinator.submitted
    assert spec.params["algo_id"] == "9001"


async def test_a_stale_stop_is_kept_on_a_registered_position_with_no_current_stop_recorded(make_env):
    env = await make_env().boot()
    old = await env.system_stop(algo_id="9001")
    rec = await env.add_position()  # 登记了还没挂上当前止损（或在重建中）
    await env.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id="9001")

    await env.reconciler.reconcile_orders()

    assert env.coordinator.submitted == []


async def test_a_stale_stop_is_still_cleaned_when_the_position_relies_on_external_protection(make_env):
    env = await make_env().boot()
    old = await env.system_stop(algo_id="9001")
    env.algos.append(algo(SYM, LONG, cid="web_user_stop", algo_id="8001", trigger="94"))  # 用户自己的止损保护着
    rec = await env.add_position()
    await env.registry.set_protection(rec.id, state=ProtectionState.EXTERNAL)
    await env.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id="9001")

    await env.reconciler.reconcile_orders()

    [spec] = env.coordinator.submitted
    assert spec.idem_key == f"stale:{rec.id}:9001"


async def test_a_stale_stop_is_kept_while_no_user_stop_of_an_externally_protected_position_is_visible(make_env):
    env = await make_env().boot()
    old = await env.system_stop(algo_id="9001")
    rec = await env.add_position()
    await env.registry.set_protection(rec.id, state=ProtectionState.EXTERNAL)  # 登记说靠用户的止损保护……
    await env.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id="9001")
    env.algos.append(algo(SYM, LONG, cid="web_user_tp", algo_id="8002", trigger="120",
                          algo_type=AlgoType.TAKE_PROFIT_MARKET))  # ……可这份快照里只有用户的止盈，止损已被撤掉
    env.algos.append(algo(SYM, SHORT, cid="web_other_side", algo_id="8003", trigger="105"))  # 另一侧的不算

    assert await env.reconciler.reconcile_orders() == "ok"
    assert env.coordinator.submitted == []  # 旧的系统止损这时是它唯一的止损：等保护单管理处理（认领或补挂）
    assert (await env.registry.fetch(rec.id)).stale_stops == [
        {"client_algo_id": old.client_algo_id, "algo_id": "9001"}]

    env.algos.append(algo(SYM, LONG, cid="web_user_stop", algo_id="8001", trigger="94"))  # 用户的止损又在了：照常清理
    assert await env.reconciler.reconcile_orders() == "ok"
    assert [s.idem_key for s in env.coordinator.submitted] == [f"stale:{rec.id}:9001"]


async def test_a_stale_entry_that_is_the_handed_over_stop_is_never_cancelled_while_the_position_is_active(make_env):
    # 已移交给用户的止损（F13）在仓位非零时系统不动它：哪怕登记里它也出现在待清理列表（登记异常），也不按旧单撤
    env = await make_env().boot()
    handed = await env.system_stop(algo_id="9001")
    rec = await env.add_position(status="ignored")
    await env.registry.set_handed_over(rec.id, {"client_algo_id": handed.client_algo_id, "algo_id": "9001"})
    await env.registry.add_stale_stop(rec.id, client_algo_id=handed.client_algo_id, algo_id="9001")

    await env.reconciler.reconcile_orders()

    assert env.coordinator.submitted == []
    assert (await env.registry.fetch(rec.id)).stale_stops != []


# ================================================================ 3. 准备撤单的间隙里被认领了


class _HookedLedger:
    """对账看到的账本：每次 ``read`` 之前先跑一下钩子（模拟别的模块在这个间隙里改了登记簿）。"""

    def __init__(self, inner: Any, hook) -> None:
        self._inner, self._hook = inner, hook

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def read(self, fn, *args: Any, **kwargs: Any) -> Any:
        await self._hook(fn)
        return await self._inner.read(fn, *args, **kwargs)


async def test_an_orphan_claimed_by_a_position_while_the_cancel_is_prepared_is_not_cancelled(make_env, monkeypatch):
    env = await make_env().boot()
    stop = await env.system_stop(algo_id="9001")
    await env.reconciler.reconcile_positions()
    claimed: list[Any] = []

    async def hook(fn) -> None:
        if fn is repo_reconcile.cancel_key_state and not claimed:  # 已经判定是孤儿、正要提交撤单
            claimed.append(await env.add_position(stop=(stop.client_algo_id, "9001", "95")))

    monkeypatch.setattr(env.reconciler, "_ledger", _HookedLedger(env.ledger, hook))
    await env.reconciler.reconcile_orders()

    assert claimed and env.coordinator.submitted == []


async def test_an_orphan_whose_position_appears_while_the_cancel_is_prepared_is_not_cancelled(make_env, monkeypatch):
    env = await make_env().boot()
    await env.system_stop(algo_id="9001")
    await env.reconciler.reconcile_positions()  # 新鲜的快照：这一侧为零
    fired: list[Any] = []

    async def hook(fn) -> None:
        if fn is repo_reconcile.cancel_key_state and not fired:  # 正要提交撤单时，并发的一轮持仓对账读到这一侧有仓了
            fired.append(True)
            env.positions = [pos(SYM, LONG, "1")]
            assert await env.reconciler.reconcile_positions() == "ok"

    monkeypatch.setattr(env.reconciler, "_ledger", _HookedLedger(env.ledger, hook))
    await env.reconciler.reconcile_orders()

    assert fired and env.coordinator.submitted == []


async def test_a_stale_stop_that_became_the_current_stop_while_the_cancel_is_prepared_is_not_cancelled(
        make_env, monkeypatch):
    env = await make_env().boot()
    current = await env.system_stop(algo_id="9002", trigger="97")
    old = await env.system_stop(algo_id="9001", trigger="95")
    rec = await env.add_position(stop=(current.client_algo_id, "9002", "97"))
    await env.registry.add_stale_stop(rec.id, client_algo_id=old.client_algo_id, algo_id="9001")
    adopted: list[Any] = []

    async def hook(fn) -> None:
        if fn is repo_reconcile.cancel_key_state and not adopted:  # 保护单管理把旧单认领成了当前止损
            adopted.append(await env.registry.set_protection(rec.id, state=ProtectionState.SYSTEM,
                                                             stop_client_id=old.client_algo_id, stop_algo_id="9001"))

    monkeypatch.setattr(env.reconciler, "_ledger", _HookedLedger(env.ledger, hook))
    await env.reconciler.reconcile_orders()

    assert adopted and env.coordinator.submitted == []


# ================================================================ 4. 查不清「系统成交是否已入账」是未知


async def test_a_failed_check_for_unbooked_system_fills_is_unknown_not_none(make_env, monkeypatch):
    env = await make_env().boot()
    env.positions = [pos(SYM, LONG, "0.5")]

    def broken(session, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("账本读失败（测试注入）")

    monkeypatch.setattr(repo_reconcile, "system_fills_pending", broken)
    await env.clear_events()
    assert await env.reconciler.reconcile_positions() == "unknown"  # 有键没判清：不算完整的一轮
    await env.settle()
    assert env.events_of(UnregisteredPositionFound) == []  # 不能因此把它当成外部仓

    monkeypatch.undo()
    assert await env.reconciler.reconcile_positions() == "ok"
    await env.settle()
    assert len(env.events_of(UnregisteredPositionFound)) == 1


# ================================================================ 5. 系统成交还没入账解释的数量差异


async def filled_partial_close(env, *, qty: str, exchange_order_id: str = "7002"):
    """系统的部分平仓单：REST 应答已是 FILLED、意图已完成，成交明细还没入账。返回（订单行，待入账的成交）。"""
    intent = await env.new_intent(action=IntentAction.PARTIAL_CLOSE, model="op", source=IntentSource.OPERATOR,
                                  state=IntentState.COMPLETED)
    order = await env.orders.create_pending(intent=intent, leg="p", purpose=OrderPurpose.PARTIAL_CLOSE, kind="market",
                                            order_type="MARKET", side=SELL, position_side=LONG, qty=D(qty))
    view = OrderInfo(symbol=SYM, client_order_id=order.client_order_id, exchange_order_id=exchange_order_id,
                     side=SELL, position_side=LONG, order_type="MARKET", qty=D(qty), filled_qty=D(qty),
                     avg_price=D("100"), status="FILLED", raw_status="FILLED", update_time_ms=env.clock.now_ms())
    order = await env.orders.apply_write_result(order.client_order_id, WriteAccepted(order=view))
    fill = trade("sys-p1", side=SELL, position_side=LONG, qty=qty, order_id=exchange_order_id,
                 time_ms=env.clock.now_ms())
    return order, fill


async def test_a_difference_explained_by_unbooked_system_fills_is_not_a_mismatch(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    _, fill = await filled_partial_close(env, qty="0.4")
    env.positions = [pos(SYM, LONG, "0.6")]  # 交易所已经少了 0.4；成交接口暂时还查不到这笔成交

    assert await env.reconciler.reconcile_positions() == "ok"  # 和「在途」一样先不判

    assert Suppression.RECONCILE_MISMATCH not in env.suppressions()
    assert env.alerts("reconcile_mismatch") == [] and await open_diffs(env) == []

    env.trades = [fill]  # 成交接口查得到了：下一轮补拉入账，登记簿跟上
    assert await env.reconciler.reconcile_positions() == "ok"
    await env.settle()
    assert (await env.registry.fetch(rec.id)).qty == D("0.6")
    assert env.suppressions() == set()


async def test_without_unbooked_system_fills_the_same_difference_is_still_a_mismatch(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "0.6")]

    assert await env.reconciler.reconcile_positions() == "mismatch"
    assert Suppression.RECONCILE_MISMATCH in env.suppressions()


# ================================================================ 6. 签名请求失败时照样比对时钟偏差


async def test_the_clock_skew_is_checked_even_when_the_signed_reads_fail(make_env):
    env = await make_env().boot()
    env.skew_ms = 5000
    env.fail("fetch_positions", "fetch_account")  # 本机时钟偏了，签名请求被交易所拒绝：只有服务器时间读得到

    assert await env.reconciler.reconcile_positions() == "unknown"

    assert Suppression.CLOCK_SKEW in env.suppressions()
    assert [a.kind for a in env.alerts("clock_skew")] == ["clock_skew"]
    assert env.reconciler.status()["unknown_streak"] == 1  # 这一轮仍然是未知

    env.skew_ms = 0  # 校准了；持仓仍读不到
    assert await env.reconciler.reconcile_positions() == "unknown"
    assert Suppression.CLOCK_SKEW not in env.suppressions()


# ================================================================ 7. 两次读数之间仓位变了、或键刚开始在途


async def test_a_position_that_moves_between_the_two_reads_is_not_judged_this_round(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    added = trade("t1", side=BUY, position_side=LONG, qty="0.5")
    env.trades = [added]
    env.order_views[added.exchange_order_id] = web_view(added)
    env.positions = [pos(SYM, LONG, "1.5")]

    def trades_then_move(symbol, start_ms, end_ms, limit):
        env.positions = [pos(SYM, LONG, "2")]  # 补拉的时候用户又加了 0.5，这笔成交接口还查不到
        return [t for t in env.trades if t.symbol == symbol]

    env.adapter.set_read("fetch_trades", trades_then_move)
    assert await env.reconciler.reconcile_positions() == "unknown"  # 这个键没核对清楚：不算完整的一轮
    await env.settle()

    assert Suppression.RECONCILE_MISMATCH not in env.suppressions() and env.alerts("reconcile_mismatch") == []
    assert (await env.registry.fetch(rec.id)).qty == D("1.5")  # 第一笔已由补拉解释，第二笔下一轮再说

    second = trade("t2", side=BUY, position_side=LONG, qty="0.5")
    env.trades.append(second)
    env.order_views[second.exchange_order_id] = web_view(second, cid="web_def")
    env.adapter.set_read("fetch_trades", lambda symbol, start_ms, end_ms, limit: [
        t for t in env.trades if t.symbol == symbol])
    assert await env.reconciler.reconcile_positions() == "ok"  # 读数稳定了：照常补拉、复核，登记簿跟上
    await env.settle()
    assert (await env.registry.fetch(rec.id)).qty == D("2") and env.suppressions() == set()


async def test_a_key_that_becomes_busy_during_the_check_is_not_judged(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")
    env.positions = [pos(SYM, LONG, "1.5")]

    def trades_and_busy(symbol, start_ms, end_ms, limit):
        env.coordinator.busy.add((SYM, LONG))  # 补拉的时候系统在这个键上开始了一个意图
        return []

    env.adapter.set_read("fetch_trades", trades_and_busy)
    assert await env.reconciler.reconcile_positions() == "ok"
    assert Suppression.RECONCILE_MISMATCH not in env.suppressions()

    env.coordinator.busy.clear()  # 意图结束了；差异还在、也解释不了：照判
    env.adapter.set_read("fetch_trades", lambda **kwargs: [])
    assert await env.reconciler.reconcile_positions() == "mismatch"


# ================================================================ 8. 崩溃点不被吞掉


async def test_a_crash_signal_from_the_exchange_read_is_never_swallowed(make_env, monkeypatch):
    env = await make_env().boot()

    async def killed():
        raise CrashSignal("test_kill")

    monkeypatch.setattr(env.adapter, "fetch_positions", killed)
    with pytest.raises(CrashSignal):  # 读失败才是「未知」；进程被杀不是
        await env.reconciler.reconcile_positions()
    assert env.reconciler.status()["unknown_streak"] == 0

    monkeypatch.undo()
    assert await env.reconciler.reconcile_positions() == "ok"  # 锁已释放


async def test_a_crash_signal_from_the_coordinator_is_never_swallowed(make_env):
    env = await make_env().boot()
    await env.system_stop(algo_id="9001")
    await env.reconciler.reconcile_positions()
    env.coordinator.submit_error = CrashSignal("test_kill")

    with pytest.raises(CrashSignal):
        await env.reconciler.reconcile_orders()

    env.coordinator.submit_error = None
    assert await env.reconciler.reconcile_orders() == "ok"  # 锁已释放
    assert [s.idem_key for s in env.coordinator.submitted] == ["orphan:9001"]


# ================================================================ 9. 孤儿的「明确为零」要真能说明这张单没有在保护仓位


def reads_in_turn(env, *answers) -> None:
    """持仓读数依次给出 ``answers``，之后一直是最后一个。"""
    queue = list(answers)
    env.adapter.set_read("fetch_positions", lambda: queue.pop(0) if len(queue) > 1 else queue[0])


async def test_a_zero_reading_taken_before_the_stop_existed_does_not_make_it_an_orphan(make_env):
    env = await make_env(reconcile_positions_s=30, reconcile_orders_s=10).boot()  # 新鲜窗口 60 秒，长于条件单周期 10 秒
    assert await env.reconciler.reconcile_positions() == "ok"  # 这一侧为零：那时还没有这张单
    env.clock.advance(1_000)
    await env.system_stop(algo_id="9001", age_s=0)  # 快照之后才挂上的系统止损，登记簿里没有引用
    env.clock.advance(15_000)  # 建立已超过一个条件单周期，那张快照也仍算新鲜

    assert await env.reconciler.reconcile_orders() == "ok"
    assert env.coordinator.submitted == [] and env.alerts("orphan_stop_kept") == []  # 等建立之后的快照，不告警

    assert await env.reconciler.reconcile_positions() == "ok"  # 建立之后的快照仍明确为零：照撤
    assert await env.reconciler.reconcile_orders() == "ok"
    assert [s.idem_key for s in env.coordinator.submitted] == ["orphan:9001"]


async def test_a_zero_reading_on_a_side_the_registry_still_holds_does_not_make_a_stop_an_orphan(make_env):
    env = await make_env().boot()
    await env.add_position(qty="1")  # 登记簿认为这一侧有仓，却没有记着这张止损（例如丢了它的编号）
    await env.system_stop(algo_id="9001")  # 交易所上这一侧的系统止损：可能正是这个仓唯一的止损
    reads_in_turn(env, Known([]), Known([pos(SYM, LONG, "1")]))  # 首读这一侧为零（读数抖动），复核读数又看到了仓
    assert await env.reconciler.reconcile_positions() == "ok"  # 复核读数与登记一致：这一轮是完整的

    assert await env.reconciler.reconcile_orders() == "ok"
    assert env.coordinator.submitted == []


async def test_a_stop_left_on_a_side_whose_record_is_finally_closed_is_cancelled_as_an_orphan(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    await env.system_stop(algo_id="9001")
    env.positions = []
    for _ in range(2):  # 登记簿还认为这一侧有仓：零读数先不作数
        assert await env.reconciler.reconcile_positions() == "ok"
        assert await env.reconciler.reconcile_orders() == "ok"
    assert env.coordinator.submitted == [] and env.alerts("orphan_stop_kept") == []

    assert await env.reconciler.reconcile_positions() == "ok"  # 第 3 次明确的零：判已平
    assert env.registry.get_active(SYM, LONG) is None and (await env.registry.fetch(rec.id)).status.value == "closed"
    assert await env.reconciler.reconcile_orders() == "ok"
    assert [s.idem_key for s in env.coordinator.submitted] == ["orphan:9001"]


# ================================================================ 10. 两次读数之间仓位在变：这一轮不是 ok


async def test_a_side_that_reads_zero_and_then_another_quantity_is_not_a_complete_round(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    reads_in_turn(env, Known([]), Known([pos(SYM, LONG, "2")]))  # 首读为零，复核却是另一个数量：仓位正在变

    assert await env.reconciler.reconcile_positions() == "unknown"
    assert Suppression.RECONCILE_MISMATCH not in env.suppressions()
    assert env.registry.get_active(SYM, LONG).id == rec.id


async def test_a_side_that_reads_another_quantity_and_then_zero_is_not_a_complete_round(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    reads_in_turn(env, Known([pos(SYM, LONG, "1.5")]), Known([]))  # 首读数量对不上，复核又为零：仓位正在变

    assert await env.reconciler.reconcile_positions() == "unknown"
    assert Suppression.RECONCILE_MISMATCH not in env.suppressions()
    assert env.registry.get_active(SYM, LONG).id == rec.id


async def test_a_side_that_reads_zero_once_and_then_matches_the_registry_is_a_complete_round(make_env):
    env = await make_env().boot()
    rec = await env.add_position(qty="1")
    reads_in_turn(env, Known([]), Known([pos(SYM, LONG, "1")]))  # 首读抖了一下，复核与登记一致：核对清楚了

    assert await env.reconciler.reconcile_positions() == "ok"
    assert env.registry.get_active(SYM, LONG).id == rec.id


# ================================================================ 11. 条件单快照的时间是查询发出的时间


async def test_the_orders_snapshot_carries_the_time_the_queries_were_sent(make_env):
    env = await make_env().boot()
    sent = env.clock.now_ms()

    def slow_algos(symbol=None):
        env.clock.advance(3_000)  # 交易所 3 秒后才答复：这期间挂上的止损可能不在这份快照里
        return list(env.algos)

    env.adapter.set_read("fetch_open_algo_orders", slow_algos)
    assert await env.reconciler.reconcile_orders() == "ok"
    await env.settle()

    [snapshot] = env.events_of(OrdersSnapshotTaken)
    assert snapshot.at_ms == sent
