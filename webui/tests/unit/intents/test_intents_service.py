"""IntentService：创建与幂等、过期、查询、重开、晚到成交。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.events import IntentStateChanged
from dmr_kernel.core.intents import IntentService, InvalidTransition
from dmr_kernel.core.types import (
    AlertLevel,
    IntentAction,
    IntentSource,
    IntentState,
    OrderOrigin,
    PositionSide,
)
from dmr_kernel.ledger import repo_audit
from dmr_kernel.ledger.records import IntentRecord

S = IntentState


# ---------------------------------------------------------------- 创建

async def test_create_persists_a_created_intent_with_all_fields(service, spec, clock):
    rec, created = await service.create(spec(
        "open:m1:BTCUSDT:LONG:1", qty=Decimal("0.250"), ratio=Decimal("0.5"), params={"notional": Decimal("12.5"), "n": 3},
        reason="DMR12 上穿且 DMR6>0", ttl_s=60, operator="alice", op_id="op-9", position_id=11, position_generation=2,
        parent_intent_id=None, ai_eval_ref="eval-1", model="m3", source=IntentSource.INJECTED,
        action=IntentAction.OPEN, symbol="ETHUSDT", side=PositionSide.SHORT))
    assert created is True
    assert isinstance(rec, IntentRecord)
    assert rec.id >= 1
    assert rec.idem_key == "open:m1:BTCUSDT:LONG:1"
    assert (rec.action, rec.model, rec.source) == (IntentAction.OPEN, "m3", IntentSource.INJECTED)
    assert (rec.symbol, rec.side) == ("ETHUSDT", PositionSide.SHORT)
    assert (rec.qty, rec.ratio) == (Decimal("0.250"), Decimal("0.5"))
    assert rec.params == {"notional": "12.5", "n": 3}  # JSON 里的 Decimal 按接口约定存成字符串
    assert rec.reason == "DMR12 上穿且 DMR6>0"
    assert rec.state is S.CREATED and rec.state_reason == ""
    assert rec.created_ms == rec.updated_ms == clock.now_ms()
    assert rec.expires_ms == clock.now_ms() + 60_000
    assert (rec.operator, rec.op_id) == ("alice", "op-9")
    assert (rec.position_id, rec.position_generation, rec.parent_intent_id) == (11, 2, None)
    assert rec.ai_eval_ref == "eval-1"
    assert rec.reserved_slot is False
    assert await service.get(rec.id) == rec


async def test_create_without_ttl_never_expires(service, spec, clock):
    rec, _ = await service.create(spec("no-ttl"))
    assert rec.expires_ms is None
    clock.advance(10 * 24 * 3600 * 1000)
    assert IntentService.is_expired(rec, clock.now_ms()) is False
    assert await service.expire_due(clock.now_ms()) == []


async def test_create_assigns_increasing_ids(service, spec):
    a, _ = await service.create(spec("a"))
    b, _ = await service.create(spec("b"))
    assert b.id > a.id


async def test_duplicate_idempotency_key_is_rejected_without_a_second_row(service, spec, bus, collector):
    await bus.start()
    first, created1 = await service.create(spec("same-key", qty=Decimal("1")))
    # 第二次内容不同也一样：键相同就是同一个意图
    second, created2 = await service.create(spec("same-key", qty=Decimal("999"), symbol="ETHUSDT"))
    assert (created1, created2) == (True, False)
    assert second == first
    assert second.qty == Decimal("1")
    assert len(await service.non_terminal()) == 1
    await bus.drain()
    assert len(collector.of(IntentStateChanged)) == 1  # 重复创建不发事件


async def test_duplicate_key_still_returns_the_existing_intent_after_it_ended(service, spec):
    first, _ = await service.create(spec("ended-key"))
    ended = await service.transition(first.id, S.REJECTED, reason="没通过")
    again, created = await service.create(spec("ended-key"))
    assert created is False
    assert again == ended and again.state is S.REJECTED  # 同一根 K 线内重启不会产生第二个意图
    assert await service.non_terminal() == []


async def test_get_and_get_by_key(service, spec):
    rec, _ = await service.create(spec("lookup"))
    assert await service.get(rec.id) == rec
    assert await service.get_by_key("lookup") == rec
    assert await service.get(987654) is None
    assert await service.get_by_key("nope") is None


async def test_create_publishes_a_created_event(service, spec, bus, collector, clock):
    await bus.start()
    rec, _ = await service.create(spec("evt", symbol="SOLUSDT", side=PositionSide.SHORT, action=IntentAction.CLOSE))
    await bus.drain()
    [event] = collector.of(IntentStateChanged)
    assert (event.intent_id, event.state, event.action, event.symbol, event.side, event.at_ms) == (
        rec.id, "CREATED", "close", "SOLUSDT", PositionSide.SHORT, clock.now_ms())


@pytest.mark.parametrize("overrides", [
    {"idem_key": ""}, {"idem_key": "  "}, {"symbol": ""}, {"model": ""}, {"ttl_s": 0}, {"ttl_s": -5},
    {"qty": Decimal("-1")}, {"ratio": Decimal("-0.1")},
])
async def test_create_rejects_malformed_specs(service, spec, overrides):
    with pytest.raises(ValueError):
        await service.create(spec(**{"idem_key": "bad", **overrides}))
    assert await service.non_terminal() == []


# ---------------------------------------------------------------- 过期

def test_is_expired_boundaries():
    def rec(**kw):
        base = dict(id=1, idem_key="k", action=IntentAction.OPEN, model="m1", source=IntentSource.SIGNAL,
                    symbol="BTCUSDT", side=PositionSide.LONG, qty=None, ratio=None, params={}, reason="",
                    state=S.CREATED, state_reason="", created_ms=1_000, expires_ms=61_000, updated_ms=1_000,
                    operator=None, op_id=None, position_id=None, position_generation=None, parent_intent_id=None,
                    ai_eval_ref=None, reserved_slot=False)
        base.update(kw)
        return IntentRecord(**base)

    assert IntentService.is_expired(rec(), 60_999) is False
    assert IntentService.is_expired(rec(), 61_000) is True  # 有效期满的这一毫秒起就算过期
    assert IntentService.is_expired(rec(), 61_001) is True
    assert IntentService.is_expired(rec(expires_ms=None), 10**15) is False
    # 还没发出订单的状态才有「过期」可言；已经在执行或已结束的不算
    for state in (S.CREATED, S.VALIDATED, S.RESERVED, S.DISPATCH_READY):
        assert IntentService.is_expired(rec(state=state), 70_000) is True, state
    for state in (S.ACTIVE, S.RECONCILING, S.COMPLETED, S.REJECTED, S.EXPIRED, S.CANCELLED):
        assert IntentService.is_expired(rec(state=state), 70_000) is False, state


async def test_expire_due_expires_only_unsent_intents_that_are_past_their_deadline(service, spec, in_state, clock):
    created = await in_state(S.CREATED, "x-created", ttl_s=30)
    validated = await in_state(S.VALIDATED, "x-validated", ttl_s=30)
    reserved = await in_state(S.RESERVED, "x-reserved", ttl_s=30)
    later = await in_state(S.CREATED, "x-later", ttl_s=300)
    forever = await in_state(S.CREATED, "x-forever")
    ready = await in_state(S.DISPATCH_READY, "x-ready", ttl_s=30)
    active = await in_state(S.ACTIVE, "x-active", ttl_s=30)
    done = await in_state(S.COMPLETED, "x-done", ttl_s=30)
    clock.advance(31_000)

    expired = await service.expire_due(clock.now_ms())

    assert sorted(r.id for r in expired) == sorted([created.id, validated.id, reserved.id])
    assert all(r.state is S.EXPIRED and r.state_reason for r in expired)
    assert all(r.updated_ms == clock.now_ms() for r in expired)
    assert next(r for r in expired if r.id == reserved.id).reserved_slot is False  # 过期释放预占的名额
    for untouched in (later, forever, ready, active, done):
        assert (await service.get(untouched.id)).state is untouched.state
    assert await service.expire_due(clock.now_ms()) == []  # 再跑一次什么也不做


async def test_expire_due_uses_the_given_time_not_the_clock(service, in_state, clock):
    rec = await in_state(S.CREATED, "given-time", ttl_s=10)
    assert await service.expire_due(clock.now_ms() + 9_999) == []
    [expired] = await service.expire_due(clock.now_ms() + 10_000)
    assert expired.id == rec.id and expired.state is S.EXPIRED


async def test_expire_due_publishes_an_event_per_expired_intent(service, in_state, bus, collector, clock):
    await bus.start()
    a = await in_state(S.CREATED, "ea", ttl_s=5)
    b = await in_state(S.VALIDATED, "eb", ttl_s=5)
    await bus.drain()
    collector.events.clear()
    clock.advance(6_000)
    await service.expire_due(clock.now_ms())
    await bus.drain()
    assert sorted((e.intent_id, e.state) for e in collector.of(IntentStateChanged)) == sorted(
        [(a.id, "EXPIRED"), (b.id, "EXPIRED")])


async def test_an_expired_intent_can_no_longer_be_executed(service, in_state, clock):
    """过期意图不会被执行：expire_due 之后它是终态，不能再走向派发。"""
    rec = await in_state(S.VALIDATED, "never-run", ttl_s=5)
    clock.advance(5_000)
    assert IntentService.is_expired(rec, clock.now_ms())  # 协调者在派发前先问这一句
    await service.expire_due(clock.now_ms())
    for target in (S.RESERVED, S.DISPATCH_READY, S.ACTIVE):
        with pytest.raises(InvalidTransition):
            await service.transition(rec.id, target)


# ---------------------------------------------------------------- 查询

async def test_non_terminal_filters(service, in_state):
    a = await in_state(S.CREATED, "f1", symbol="BTCUSDT", side=PositionSide.LONG, action=IntentAction.OPEN, model="m1")
    b = await in_state(S.ACTIVE, "f2", symbol="BTCUSDT", side=PositionSide.SHORT, action=IntentAction.CLOSE, model="op")
    c = await in_state(S.RECONCILING, "f3", symbol="ETHUSDT", side=PositionSide.LONG, action=IntentAction.OPEN, model="m2")
    await in_state(S.COMPLETED, "f4", symbol="BTCUSDT", side=PositionSide.LONG, action=IntentAction.OPEN, model="m1")
    await in_state(S.REJECTED, "f5", symbol="ETHUSDT", side=PositionSide.SHORT, action=IntentAction.OPEN, model="m1")

    assert [r.id for r in await service.non_terminal()] == [a.id, b.id, c.id]
    assert [r.id for r in await service.non_terminal(symbol="BTCUSDT")] == [a.id, b.id]
    assert [r.id for r in await service.non_terminal(side=PositionSide.LONG)] == [a.id, c.id]
    assert [r.id for r in await service.non_terminal(action=IntentAction.CLOSE)] == [b.id]
    assert [r.id for r in await service.non_terminal(model="m2")] == [c.id]
    assert [r.id for r in await service.non_terminal(symbol="BTCUSDT", side=PositionSide.LONG,
                                                      action=IntentAction.OPEN, model="m1")] == [a.id]
    assert await service.non_terminal(symbol="NOPEUSDT") == []


async def test_non_terminal_follows_state_changes(service, in_state):
    rec = await in_state(S.ACTIVE, "follow")
    assert [r.id for r in await service.non_terminal()] == [rec.id]
    await service.transition(rec.id, S.COMPLETED)
    assert await service.non_terminal() == []


# ---------------------------------------------------------------- 重开

@pytest.mark.parametrize("terminal", [S.COMPLETED, S.REJECTED, S.EXPIRED, S.CANCELLED])
async def test_reopen_for_late_fill_brings_any_terminal_state_back_to_active(service, in_state, ledger, bus, collector,
                                                                             terminal):
    await bus.start()
    rec = await in_state(terminal, f"reopen:{terminal.value}")
    await bus.drain()
    collector.events.clear()

    reopened = await service.reopen_for_late_fill(rec.id, reason="晚到成交 t99")

    assert reopened.state is S.ACTIVE and reopened.state_reason == "晚到成交 t99"
    assert reopened.reserved_slot is False
    assert (await service.get(rec.id)).state is S.ACTIVE
    await bus.drain()
    assert [(e.intent_id, e.state) for e in collector.of(IntentStateChanged)] == [(rec.id, "ACTIVE")]
    audit = await ledger.read(repo_audit.recent, action="intent.reopen_late_fill")
    assert len(audit) == 1
    assert audit[0].target == f"intent:{rec.id}"
    assert audit[0].before["state"] == terminal.value and audit[0].after["state"] == "ACTIVE"
    assert "晚到成交 t99" in audit[0].after["reason"]


@pytest.mark.parametrize("live", [S.CREATED, S.VALIDATED, S.RESERVED, S.DISPATCH_READY, S.ACTIVE, S.RECONCILING])
async def test_reopen_of_a_non_terminal_intent_is_refused(service, in_state, ledger, live):
    rec = await in_state(live, f"noreopen:{live.value}")
    with pytest.raises(InvalidTransition):
        await service.reopen_for_late_fill(rec.id, reason="不该成功")
    assert await service.get(rec.id) == rec
    assert await ledger.read(repo_audit.recent, action="intent.reopen_late_fill") == []


async def test_reopen_of_unknown_intent_is_a_key_error(service):
    with pytest.raises(KeyError):
        await service.reopen_for_late_fill(31337, reason="x")


async def test_a_reopened_intent_follows_the_normal_table_again(service, in_state):
    rec = await in_state(S.REJECTED, "again")
    await service.reopen_for_late_fill(rec.id, reason="晚到")
    assert (await service.transition(rec.id, S.COMPLETED)).state is S.COMPLETED
    with pytest.raises(InvalidTransition):
        await service.transition(rec.id, S.ACTIVE)  # 又成终态后，再回 ACTIVE 仍只能走 reopen


# ---------------------------------------------------------------- 晚到成交（订阅 FillIngested）

# 「晚到成交」的口径（实施记录 R17）：
# - 意图已放弃（REJECTED/EXPIRED/CANCELLED）后又收到它自己订单的成交：一律是晚到成交，重开并告警（F3 第 2 步末条）。
# - 意图已完成（COMPLETED）：它自己订单的成交随后入账是常态（REST 应答里就是 FILLED），只有成交落在一张我们判为
#   拒绝或撤销、且带来新成交量的订单上（入账侧标 on_dead_order）才算晚到。
# - 保护性条件单（止损、碎仓条件单）本来就会在挂单意图结束之后才触发成交：永不重开。

@pytest.mark.parametrize("terminal", [S.REJECTED, S.EXPIRED, S.CANCELLED])
async def test_late_system_fill_reopens_a_given_up_intent_and_raises_an_alert(service, in_state, bus, collector,
                                                                              notifier, fill_event, terminal):
    await bus.start()
    await service.start()
    rec = await in_state(terminal, f"late:{terminal.value}", symbol="ETHUSDT", side=PositionSide.SHORT)

    bus.publish(fill_event(intent_id=rec.id, trade_id="late-1", symbol="ETHUSDT", position_side=PositionSide.SHORT))
    await bus.drain()

    assert (await service.get(rec.id)).state is S.ACTIVE
    [alert] = notifier.alerts
    assert alert["level"] is AlertLevel.CRITICAL
    assert alert["kind"] == "intent_late_fill"
    assert str(rec.id) in alert["message"] and terminal.value in alert["message"] and "late-1" in alert["message"]
    assert alert["dedupe_key"]
    assert (rec.id, "ACTIVE") in [(e.intent_id, e.state) for e in collector.of(IntentStateChanged)]
    await service.stop()


async def test_a_completed_intents_own_expected_fill_is_not_late(service, in_state, bus, notifier, fill_event):
    await bus.start()
    await service.start()
    rec = await in_state(S.COMPLETED, "done-normally")
    bus.publish(fill_event(intent_id=rec.id, trade_id="normal-1", on_dead_order=False))
    await bus.drain()
    assert (await service.get(rec.id)).state is S.COMPLETED
    assert notifier.alerts == []
    await service.stop()


async def test_a_completed_intent_reopens_when_the_fill_lands_on_an_order_judged_dead(service, in_state, bus,
                                                                                     notifier, fill_event):
    await bus.start()
    await service.start()
    rec = await in_state(S.COMPLETED, "resent-and-done")
    bus.publish(fill_event(intent_id=rec.id, trade_id="ghost-1", on_dead_order=True))
    await bus.drain()
    assert (await service.get(rec.id)).state is S.ACTIVE
    [alert] = notifier.alerts
    assert alert["kind"] == "intent_late_fill" and "ghost-1" in alert["message"]
    await service.stop()


@pytest.mark.parametrize("purpose", ["stop", "dust_close"])
@pytest.mark.parametrize("terminal", [S.COMPLETED, S.REJECTED, S.EXPIRED, S.CANCELLED])
async def test_protective_conditional_fills_never_reopen_their_placing_intent(service, in_state, bus, notifier,
                                                                              fill_event, purpose, terminal):
    await bus.start()
    await service.start()
    rec = await in_state(terminal, f"protect:{purpose}:{terminal.value}")
    bus.publish(fill_event(intent_id=rec.id, trade_id=f"trig-{purpose}", order_purpose=purpose,
                           client_order_id="DKa1b2-0-1-s", on_dead_order=True))
    await bus.drain()
    assert (await service.get(rec.id)).state is terminal
    assert notifier.alerts == []
    await service.stop()


async def test_second_fill_of_the_same_late_order_does_not_alert_again(service, in_state, bus, notifier, fill_event):
    await bus.start()
    await service.start()
    rec = await in_state(S.EXPIRED, "late-twice")
    bus.publish(fill_event(intent_id=rec.id, trade_id="a"))
    bus.publish(fill_event(intent_id=rec.id, trade_id="b"))
    await bus.drain()
    assert (await service.get(rec.id)).state is S.ACTIVE
    assert len(notifier.alerts) == 1  # 意图已经重开，后面的成交不再算「晚到」


@pytest.mark.parametrize("live", [S.CREATED, S.VALIDATED, S.RESERVED, S.DISPATCH_READY, S.ACTIVE, S.RECONCILING])
async def test_fill_on_a_live_intent_changes_nothing(service, in_state, bus, notifier, fill_event, live):
    await bus.start()
    await service.start()
    rec = await in_state(live, f"live:{live.value}")
    bus.publish(fill_event(intent_id=rec.id))
    await bus.drain()
    assert await service.get(rec.id) == rec
    assert notifier.alerts == []


@pytest.mark.parametrize("origin", [OrderOrigin.EXTERNAL, OrderOrigin.EMERGENCY, OrderOrigin.LIQUIDATION,
                                    OrderOrigin.ADL, OrderOrigin.LEGACY, OrderOrigin.UNKNOWN,
                                    OrderOrigin.CCXT_DEFAULT])
async def test_non_system_fills_never_reopen_an_intent(service, in_state, bus, notifier, fill_event, origin):
    await bus.start()
    await service.start()
    rec = await in_state(S.COMPLETED, f"origin:{origin.value}")
    bus.publish(fill_event(intent_id=rec.id, origin=origin))
    await bus.drain()
    assert (await service.get(rec.id)).state is S.COMPLETED
    assert notifier.alerts == []


async def test_system_fill_without_a_known_intent_is_ignored(service, bus, notifier, fill_event):
    await bus.start()
    await service.start()
    bus.publish(fill_event(intent_id=None))
    bus.publish(fill_event(intent_id=777_777))  # 账本里没有这个意图（比如换库前的旧单）
    await bus.drain()
    assert notifier.alerts == []


async def test_late_fill_without_a_notifier_still_reopens(ledger, bus, clock, fill_event, spec):
    quiet = IntentService(ledger, bus, clock)  # 不配通知器
    await bus.start()
    await quiet.start()
    rec, _ = await quiet.create(spec("quiet"))
    rec = await quiet.transition(rec.id, S.REJECTED, reason="x")
    bus.publish(fill_event(intent_id=rec.id))
    await bus.drain()
    assert (await quiet.get(rec.id)).state is S.ACTIVE


async def test_stopped_service_no_longer_listens(service, in_state, bus, notifier, fill_event):
    await bus.start()
    await service.start()
    await service.stop()
    rec = await in_state(S.REJECTED, "stopped")
    bus.publish(fill_event(intent_id=rec.id))
    await bus.drain()
    assert (await service.get(rec.id)).state is S.REJECTED
    assert notifier.alerts == []


async def test_start_twice_does_not_double_handle(service, in_state, bus, notifier, fill_event):
    await bus.start()
    await service.start()
    await service.start()
    rec = await in_state(S.CANCELLED, "twice")
    bus.publish(fill_event(intent_id=rec.id))
    await bus.drain()
    assert len(notifier.alerts) == 1
