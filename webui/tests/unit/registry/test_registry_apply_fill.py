"""持仓登记：apply_fill 的每条规则（登记、加仓、混合仓、减仓、归零与出场原因、推定止损、幂等）。"""
from __future__ import annotations

import asyncio

import pytest

from dmr_kernel.core.events import (
    FillIngested,
    PositionChanged,
    PositionClosed,
    PositionRegistered,
    UnregisteredPositionFound,
)
from dmr_kernel.core.registry import PRESUMED_STOP_NOTE
from dmr_kernel.core.types import (
    ExitReason,
    ExitRuleset,
    IntentAction,
    IntentSource,
    IntentState,
    OperationType,
    OrderOrigin,
    Ownership,
    PositionStatus,
    ProtectionState,
)
from dmr_kernel.exchange import ids
from dmr_kernel.ledger import roundtrip
from registry_kit import (
    D,
    LONG,
    Rig,
    SHORT,
    SYM,
    T0,
    add_intent,
    add_operation,
    all_position_rows,
    audit_rows,
    fill_position_id,
    make_fill,
    trade_of,
)

EXTERNAL = OrderOrigin.EXTERNAL


# ================================================================ 没有记录时：登记或报告

async def test_system_open_fill_registers_strategy_position(rig, clock):
    await rig.up()
    iid = await add_intent(rig.ledger, model="m1", reason="信号开多", source=IntentSource.SIGNAL)
    fill = await make_fill(rig.ledger, qty="0.4", price="100", intent_id=iid, time_ms=T0 + 500)
    rec = await rig.registry.apply_fill(fill)
    assert rec is not None
    assert (rec.symbol, rec.side) == (SYM, LONG)
    assert rec.status is PositionStatus.MANAGED
    assert rec.ownership is Ownership.STRATEGY and rec.exit_ruleset is ExitRuleset.STRATEGY
    assert rec.protection_state is ProtectionState.NONE
    assert (rec.qty, rec.avg_price, rec.max_qty) == (D("0.4"), D("100"), D("0.4"))
    assert rec.open_time_ms == T0 + 500 and rec.anchor_ms == T0 + 500 and rec.open_time_estimated is False
    assert rec.generation == 1 and rec.version == 1
    assert rec.opened_by_intent_id == iid and rec.entry_reason == "信号开多"
    assert (rec.leverage, rec.margin_mode) == (3, "isolated")  # 取自配置
    assert rig.registry.get_active(SYM, LONG) == rec
    assert await fill_position_id(rig.ledger, fill.fill_id) == rec.id  # 给 fill 回填 position_id
    events = await rig.events()
    assert events == [PositionRegistered(position_id=rec.id, symbol=SYM, side=LONG, ownership="strategy",
                                         status="managed", at_ms=clock.now_ms())]


async def test_system_open_fill_for_short_registers_short_position(rig):
    await rig.up()
    iid = await add_intent(rig.ledger, side=SHORT)
    rec = await rig.apply(qty=2, price=50, position_side=SHORT, intent_id=iid)
    assert rec.side is SHORT and rec.qty == D(2)
    assert rig.registry.get_active(SYM, SHORT) == rec and rig.registry.get_active(SYM, LONG) is None


async def test_intent_params_carry_list_info_and_overrides_into_the_position(rig):
    await rig.up()
    iid = await add_intent(rig.ledger, params={"list_snapshot_id": 4, "list_direction": "LONG", "list_score": "0.9",
                                               "leverage": 5, "margin_mode": "cross"})
    rec = await rig.apply(qty=1, price=100, intent_id=iid)
    assert (rec.list_snapshot_id, rec.list_direction, rec.list_score) == (4, "LONG", D("0.9"))
    assert (rec.leverage, rec.margin_mode) == (5, "cross")


async def test_malformed_intent_params_never_block_registration(rig):
    await rig.up()
    iid = await add_intent(rig.ledger, params={"list_snapshot_id": "x", "list_score": "abc", "leverage": "y",
                                               "list_direction": 5, "margin_mode": None})
    rec = await rig.apply(qty=1, price=100, intent_id=iid)
    assert rec is not None and rec.status is PositionStatus.MANAGED
    assert (rec.list_snapshot_id, rec.list_score) == (None, None)
    assert (rec.leverage, rec.margin_mode) == (3, "isolated")  # 退回配置值


async def test_absurd_intent_params_never_block_registration(rig):
    await rig.up()
    iid = await add_intent(rig.ledger, params={"list_snapshot_id": 10 ** 40, "leverage": str(10 ** 40),
                                               "list_score": "NaN", "margin_mode": "weird"})
    rec = await rig.apply(qty=1, price=100, intent_id=iid)
    assert rec is not None and rec.status is PositionStatus.MANAGED
    assert (rec.list_snapshot_id, rec.list_score) == (None, None)
    assert (rec.leverage, rec.margin_mode) == (3, "isolated")  # 退回配置值


async def test_missing_config_values_leave_leverage_unset(ledger, clock):
    rig = Rig(ledger, clock)
    rig.config.clear()  # 配置里没有杠杆与保证金模式
    await rig.up()
    iid = await add_intent(ledger)
    rec = await rig.apply(qty=1, price=100, intent_id=iid)
    assert rec is not None and (rec.leverage, rec.margin_mode) == (None, None)


@pytest.mark.parametrize("model, source, expected", [
    ("m1", IntentSource.SIGNAL, Ownership.STRATEGY),
    ("op", IntentSource.OPERATOR, Ownership.STRATEGY),
    ("op", IntentSource.TEST_ENTRY, Ownership.STRATEGY),
    ("m3", IntentSource.SIGNAL, Ownership.AI),
])
async def test_ownership_follows_the_model_of_the_intent(rig, model, source, expected):
    await rig.up()
    iid = await add_intent(rig.ledger, model=model, source=source)
    rec = await rig.apply(qty=1, price=100, intent_id=iid)
    assert rec.ownership is expected
    assert rec.exit_ruleset is ExitRuleset.STRATEGY  # 策略与 AI 都用策略规则


@pytest.mark.parametrize("state", [IntentState.COMPLETED, IntentState.EXPIRED, IntentState.CANCELLED,
                                   IntentState.REJECTED])
async def test_late_system_fill_registers_even_when_intent_is_terminal(rig, state):
    """带系统前缀的晚到成交即使意图已到终态，也登记为策略仓（它不是外部成交）。"""
    await rig.up()
    iid = await add_intent(rig.ledger, state=state)
    rec = await rig.apply(qty=1, price=100, intent_id=iid)
    assert rec is not None and rec.ownership is Ownership.STRATEGY and rec.status is PositionStatus.MANAGED
    assert [e for e in await rig.events(UnregisteredPositionFound)] == []


@pytest.mark.parametrize("model_code, intent_id, expected", [
    ("1", None, Ownership.STRATEGY),
    ("0", None, Ownership.STRATEGY),
    ("3", None, Ownership.AI),
    ("3", 424_242, Ownership.AI),  # 意图行读不到：退回编号里的模型代码
])
async def test_model_falls_back_to_client_id_when_intent_row_is_unreadable(rig, model_code, intent_id, expected):
    await rig.up()
    cid = ids.make_client_id("a1", model_code, 5, "o")
    rec = await rig.apply(qty=1, price=100, intent_id=intent_id, client_order_id=cid)
    assert rec is not None and rec.ownership is expected
    assert rec.opened_by_intent_id == intent_id


async def test_system_fill_without_purpose_is_recognised_by_the_open_leg(rig):
    await rig.up()
    iid = await add_intent(rig.ledger)
    rec = await rig.apply(qty=1, price=100, intent_id=iid, purpose=None)  # 编号分腿是 o
    assert rec is not None and rec.ownership is Ownership.STRATEGY


@pytest.mark.parametrize("origin", [OrderOrigin.EXTERNAL, OrderOrigin.UNKNOWN, OrderOrigin.LEGACY,
                                    OrderOrigin.CCXT_DEFAULT, OrderOrigin.LIQUIDATION, OrderOrigin.ADL,
                                    OrderOrigin.EMERGENCY])
@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_unattributed_open_direction_fill_is_reported_not_registered(rig, clock, origin, side):
    await rig.up()
    fill = await make_fill(rig.ledger, qty="1.5", price="200", position_side=side, origin=origin, purpose=None,
                           time_ms=T0 + 10)
    assert await rig.registry.apply_fill(fill) is None
    assert rig.registry.all_active() == [] and await all_position_rows(rig.ledger) == []
    assert await fill_position_id(rig.ledger, fill.fill_id) is None
    assert await rig.events() == [UnregisteredPositionFound(symbol=SYM, side=side, qty=D("1.5"),
                                                            entry_price=D("200"), trigger="external_fill",
                                                            at_ms=clock.now_ms())]


@pytest.mark.parametrize("origin, purpose", [(OrderOrigin.SYSTEM, "close"), (OrderOrigin.SYSTEM, "stop"),
                                             (EXTERNAL, None), (OrderOrigin.LIQUIDATION, None)])
async def test_reduce_direction_fill_without_record_is_only_booked(rig, origin, purpose):
    await rig.up()
    fill = await make_fill(rig.ledger, qty=1, price=100, opening=False, origin=origin, purpose=purpose)
    assert await rig.registry.apply_fill(fill) is None
    assert rig.registry.all_active() == [] and await all_position_rows(rig.ledger) == []
    assert await fill_position_id(rig.ledger, fill.fill_id) is None
    assert await rig.events() == []


async def test_new_generation_is_registered_after_the_previous_one_is_closed(rig):
    await rig.up()
    iid = await add_intent(rig.ledger)
    first = await rig.apply(qty=1, price=100, intent_id=iid)
    closed = await rig.apply(qty=1, price=101, opening=False, intent_id=await add_intent(
        rig.ledger, action=IntentAction.CLOSE, reason="manual"))
    assert closed.status is PositionStatus.CLOSED
    second = await rig.apply(qty=2, price=102, intent_id=iid)
    assert second.id != first.id and second.generation == 2 and second.qty == D(2)


# ================================================================ 已有记录：加仓

async def test_partial_fills_of_one_open_order_accumulate_into_one_position(rig, clock):
    await rig.up()
    iid = await add_intent(rig.ledger)
    first = await rig.apply(qty="0.4", price="100", intent_id=iid)
    rig.clear_events()
    second_fill = await make_fill(rig.ledger, qty="0.6", price="101", intent_id=iid)
    second = await rig.registry.apply_fill(second_fill)
    assert second.id == first.id and second.version == first.version + 1
    assert (second.qty, second.avg_price, second.max_qty) == (D("1.0"), D("100.6"), D("1.0"))
    assert second.is_mixed is False and second.exit_ruleset is ExitRuleset.STRATEGY
    assert second.protection_state is ProtectionState.NONE and second.open_time_ms == first.open_time_ms
    assert await fill_position_id(rig.ledger, second_fill.fill_id) == first.id
    assert await rig.events() == [PositionChanged(position_id=first.id, symbol=SYM, side=LONG, cause="fill",
                                                  version=second.version, at_ms=clock.now_ms())]


async def test_system_add_weights_the_average_and_does_not_mix(rig):
    await rig.up()
    rec = await rig.open_strategy(qty=1, price=100)
    after = await rig.apply(qty=3, price=104, intent_id=await add_intent(rig.ledger))
    assert after.qty == D(4) and after.avg_price == D(103) and after.max_qty == D(4)
    assert after.is_mixed is False and after.exit_ruleset is ExitRuleset.STRATEGY
    assert after.version == rec.version + 1


@pytest.mark.parametrize("origin", [OrderOrigin.EXTERNAL, OrderOrigin.LEGACY, OrderOrigin.UNKNOWN,
                                    OrderOrigin.EMERGENCY])
async def test_non_system_add_makes_strategy_position_mixed(rig, clock, origin):
    await rig.up()
    rec = await rig.open_strategy(qty=1, price=100)
    rig.clear_events()
    mixed = await rig.apply(qty=1, price=110, origin=origin, purpose=None)
    assert mixed.is_mixed is True and mixed.exit_ruleset is ExitRuleset.STRATEGY_NO_REVERSE
    assert mixed.ownership is Ownership.STRATEGY and mixed.status is PositionStatus.MANAGED
    assert (mixed.qty, mixed.avg_price, mixed.max_qty) == (D(2), D(105), D(2))
    assert mixed.version == rec.version + 1
    assert await rig.events() == [PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="外部调整",
                                                  version=mixed.version, at_ms=clock.now_ms())]
    audit = [r for r in await audit_rows(rig.ledger) if r.action == "position.mixed"]
    assert len(audit) == 1
    assert audit[0].before["is_mixed"] is False and audit[0].after["is_mixed"] is True
    assert audit[0].after["exit_ruleset"] == "strategy_no_reverse"

    again = await rig.apply(qty=1, price=120, origin=origin, purpose=None)  # 再加一次：仍是混合仓，只记一次转换
    assert again.is_mixed is True and again.qty == D(3) and again.avg_price == D("110")
    assert len([r for r in await audit_rows(rig.ledger) if r.action == "position.mixed"]) == 1


async def test_non_system_add_also_mixes_an_ai_position(rig):
    await rig.up()
    await rig.open_strategy(ownership=Ownership.AI, model="m3")
    mixed = await rig.apply(qty=1, price=100, origin=EXTERNAL, purpose=None)
    assert mixed.ownership is Ownership.AI and mixed.is_mixed is True
    assert mixed.exit_ruleset is ExitRuleset.STRATEGY_NO_REVERSE


@pytest.mark.parametrize("managed", [False, True])
async def test_external_add_to_takeover_position_keeps_it_a_takeover_position(rig, clock, managed):
    await rig.up()
    rec = await (rig.managed_takeover() if managed else rig.takeover())
    rig.clear_events()
    added = await rig.apply(qty=1, price=110, origin=EXTERNAL, purpose=None)
    assert added.ownership is Ownership.TAKEOVER and added.exit_ruleset is ExitRuleset.TAKEOVER
    assert added.is_mixed is False and added.status is rec.status
    assert (added.qty, added.avg_price, added.max_qty) == (D(2), D(105), D(2))
    assert await rig.events() == [PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="外部调整",
                                                  version=added.version, at_ms=clock.now_ms())]


async def test_add_to_pending_position_publishes_qty_change_for_model_two(rig, clock):
    await rig.up()
    rec = await rig.pending(qty=1, price=100)
    rig.clear_events()
    added = await rig.apply(qty=1, price=120, origin=EXTERNAL, purpose=None)
    assert added.status is PositionStatus.PENDING and added.ownership is Ownership.NONE
    assert (added.qty, added.avg_price) == (D(2), D(110))
    assert await rig.events() == [PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="qty_change",
                                                  version=added.version, at_ms=clock.now_ms())]


async def test_add_to_ignored_position_updates_quantity_but_stays_ignored(rig, clock):
    await rig.up()
    rec = await rig.pending(qty=1, price=100)
    ignored = await rig.registry.ignore(rec.id, expected_version=None)
    rig.clear_events()
    added = await rig.apply(qty=1, price=100, origin=EXTERNAL, purpose=None)
    assert added.status is PositionStatus.IGNORED and added.qty == D(2) and added.ownership is Ownership.NONE
    assert added.version == ignored.version + 1
    assert await rig.events() == [PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="外部调整",
                                                  version=added.version, at_ms=clock.now_ms())]


async def test_max_qty_remembers_the_peak(rig):
    await rig.up()
    await rig.open_strategy(qty=1, price=100)
    iid = await add_intent(rig.ledger)
    await rig.apply(qty=2, price=100, intent_id=iid)  # 3
    down = await rig.apply(qty=2, price=100, opening=False, intent_id=iid, purpose="partial_close")  # 1
    assert down.qty == D(1) and down.max_qty == D(3)
    up = await rig.apply(qty=1, price=100, intent_id=iid)  # 2
    assert up.qty == D(2) and up.max_qty == D(3)


# ================================================================ 已有记录：减仓与归零

async def test_reduce_fill_lowers_quantity_and_keeps_the_average(rig, clock):
    await rig.up()
    rec = await rig.open_strategy(qty=3, price=100)
    iid = await add_intent(rig.ledger, action=IntentAction.PARTIAL_CLOSE, reason="manual")
    rig.clear_events()
    fill = await make_fill(rig.ledger, qty=1, price=90, opening=False, intent_id=iid, purpose="partial_close")
    after = await rig.registry.apply_fill(fill)
    assert after.id == rec.id and after.status is PositionStatus.MANAGED
    assert (after.qty, after.avg_price, after.max_qty) == (D(2), D(100), D(3))
    assert after.version == rec.version + 1
    assert await fill_position_id(rig.ledger, fill.fill_id) == rec.id
    assert await rig.events() == [PositionChanged(position_id=rec.id, symbol=SYM, side=LONG, cause="fill",
                                                  version=after.version, at_ms=clock.now_ms())]


async def test_external_reduce_cause_depends_on_the_record(rig, clock):
    await rig.up()
    managed = await rig.open_strategy(qty=3)
    pending = await rig.pending(symbol="ETHUSDT", qty=3)
    ignored = await rig.registry.ignore((await rig.pending(symbol="XRPUSDT", qty=3)).id, expected_version=None)
    rig.clear_events()
    for symbol in (SYM, "ETHUSDT", "XRPUSDT"):
        await rig.apply(symbol=symbol, qty=1, price=100, opening=False, origin=EXTERNAL, purpose=None)
    events = await rig.events(PositionChanged)
    assert {e.position_id: e.cause for e in events} == {
        managed.id: "外部调整", pending.id: "qty_change", ignored.id: "外部调整"}
    assert rig.registry.by_id(pending.id).qty == D(2)


@pytest.mark.parametrize("status_case", ["managed", "closing"])
async def test_reduce_to_zero_closes_the_position_and_writes_the_trade(rig, clock, status_case):
    await rig.up()
    rec = await rig.open_strategy(qty=2, price=100)
    if status_case == "closing":
        await rig.registry.set_closing(rec.id, True, cause="close_one")
    iid = await add_intent(rig.ledger, action=IntentAction.CLOSE, reason="signal")
    rig.clear_events()
    fill = await make_fill(rig.ledger, qty=2, price=110, opening=False, intent_id=iid, time_ms=T0 + 7_000,
                           realized_pnl=20)
    closed = await rig.registry.apply_fill(fill)
    assert closed.id == rec.id and closed.status is PositionStatus.CLOSED
    assert closed.qty == D(0) and closed.exit_reason is ExitReason.SIGNAL and closed.closed_ms == T0 + 7_000
    assert rig.registry.by_id(rec.id) is None and rig.registry.get_active(SYM, LONG) is None
    assert await fill_position_id(rig.ledger, fill.fill_id) == rec.id
    trade = await trade_of(rig.ledger, rec.id)
    assert trade is not None and trade.exit_reason is ExitReason.SIGNAL and trade.gross_pnl == D(20)
    assert await rig.events() == [PositionClosed(position_id=rec.id, symbol=SYM, side=LONG, exit_reason="signal",
                                                 at_ms=clock.now_ms())]


async def test_two_step_close_changes_then_closes(rig):
    await rig.up()
    rec = await rig.open_strategy(qty=3, price=100)
    iid = await add_intent(rig.ledger, action=IntentAction.CLOSE, reason="manual")
    part = await rig.apply(qty=1, price=100, opening=False, intent_id=iid)
    assert part.status is PositionStatus.MANAGED and part.qty == D(2)
    done = await rig.apply(qty=2, price=100, opening=False, intent_id=iid)
    assert done.status is PositionStatus.CLOSED and done.exit_reason is ExitReason.MANUAL
    assert done.version == rec.version + 2


async def test_oversized_reduce_is_clamped_to_zero_and_closes(rig):
    await rig.up()
    rec = await rig.open_strategy(qty=1, price=100)
    closed = await rig.apply(qty=2, price=100, opening=False, origin=EXTERNAL, purpose=None)
    assert closed.id == rec.id and closed.status is PositionStatus.CLOSED and closed.qty == D(0)
    assert closed.exit_reason is ExitReason.EXTERNAL


def _case(origin, purpose, expected, *, intent=None, operation=None, client_leg=None, id_):
    return pytest.param(origin, purpose, intent, operation, client_leg, expected, id=id_)


SYSTEM = OrderOrigin.SYSTEM
CLOSE_INTENT = dict(action=IntentAction.CLOSE)

EXIT_REASON_CASES = [
    # 系统止损单 → STOP（订单用途优先于意图）
    _case(SYSTEM, "stop", ExitReason.STOP, id_="system-stop-order"),
    _case(SYSTEM, "stop", ExitReason.STOP, intent=dict(reason="signal"), id_="stop-purpose-beats-intent"),
    _case(SYSTEM, None, ExitReason.STOP, client_leg="s", id_="stop-leg-when-purpose-missing"),
    # 系统平仓意图 → 按意图原因
    _case(SYSTEM, "close", ExitReason.SIGNAL, intent=dict(reason="signal"), id_="intent-signal"),
    _case(SYSTEM, "close", ExitReason.REVERSE, intent=dict(reason="reverse"), id_="intent-reverse"),
    _case(SYSTEM, "close", ExitReason.MANUAL, intent=dict(reason="manual"), id_="intent-manual"),
    _case(SYSTEM, "close", ExitReason.CLOSE_ALL, intent=dict(reason="close_all"), id_="intent-close-all"),
    _case(SYSTEM, "close", ExitReason.PROTECTIVE, intent=dict(reason="protective"), id_="intent-protective"),
    _case(SYSTEM, "close", ExitReason.PROTECTIVE, intent=dict(reason="PROTECTIVE: 止损挂不上"),
          id_="intent-reason-prefix-and-case"),
    _case(SYSTEM, "partial_close", ExitReason.MANUAL,
          intent=dict(action=IntentAction.PARTIAL_CLOSE, reason="manual"), id_="partial-close-intent-to-zero"),
    _case(SYSTEM, "dust_close", ExitReason.CLOSE_ALL, intent=dict(reason="close_all"), id_="dust-close-intent"),
    # 意图原因读不出来时按来源推断
    _case(SYSTEM, "close", ExitReason.PROTECTIVE, intent=dict(source=IntentSource.PROTECTION, reason=""),
          id_="source-protection"),
    _case(SYSTEM, "close", ExitReason.SIGNAL, intent=dict(source=IntentSource.SIGNAL, reason=""), id_="source-signal"),
    _case(SYSTEM, "close", ExitReason.SIGNAL, intent=dict(source=IntentSource.INJECTED, reason=""),
          id_="source-injected"),
    _case(SYSTEM, "close", ExitReason.MANUAL, intent=dict(source=IntentSource.OPERATOR, reason=""),
          id_="source-operator"),
    _case(SYSTEM, "close", ExitReason.MANUAL, intent=dict(source=IntentSource.CLI, reason=""), id_="source-cli"),
    _case(SYSTEM, "close", ExitReason.MANUAL, intent=dict(source=IntentSource.OPERATOR, reason="随便写点什么"),
          id_="source-operator-free-text"),
    _case(SYSTEM, "close", ExitReason.SIGNAL, intent=dict(source=IntentSource.SIGNAL, reason="reversed_by_hand"),
          id_="reason-token-must-be-a-whole-word"),
    _case(SYSTEM, "close", ExitReason.CLOSE_ALL,
          intent=dict(source=IntentSource.OPERATOR, reason="", op_id="op-ca"), operation=("op-ca", OperationType.CLOSE_ALL),
          id_="operation-close-all"),
    _case(SYSTEM, "close", ExitReason.CLOSE_ALL,
          intent=dict(source=IntentSource.OPERATOR, reason="", op_id="op-sc"),
          operation=("op-sc", OperationType.STOP_AND_CLOSE_ALL), id_="operation-stop-and-close-all"),
    _case(SYSTEM, "close", ExitReason.MANUAL,
          intent=dict(source=IntentSource.OPERATOR, reason="", op_id="op-c1"),
          operation=("op-c1", OperationType.CLOSE_ONE), id_="operation-close-one"),
    _case(SYSTEM, "close", ExitReason.UNKNOWN, intent=dict(source=IntentSource.SYSTEM, reason=""),
          id_="source-system-unknown"),
    _case(SYSTEM, "close", ExitReason.UNKNOWN, id_="system-close-without-intent"),
    # 强平、自动减仓、应急 → 对应原因（即使带了意图编号）
    _case(OrderOrigin.LIQUIDATION, None, ExitReason.LIQUIDATION, id_="liquidation"),
    _case(OrderOrigin.ADL, None, ExitReason.ADL, id_="adl"),
    _case(OrderOrigin.EMERGENCY, None, ExitReason.EMERGENCY, id_="emergency"),
    _case(OrderOrigin.EMERGENCY, None, ExitReason.EMERGENCY, intent=dict(reason="signal"), id_="emergency-beats-intent"),
    # 其它 → EXTERNAL
    _case(OrderOrigin.EXTERNAL, None, ExitReason.EXTERNAL, id_="external"),
    _case(OrderOrigin.UNKNOWN, None, ExitReason.EXTERNAL, id_="unknown-origin"),
    _case(OrderOrigin.LEGACY, None, ExitReason.EXTERNAL, id_="legacy"),
    _case(OrderOrigin.CCXT_DEFAULT, None, ExitReason.EXTERNAL, id_="ccxt-default"),
]


@pytest.mark.parametrize("origin, purpose, intent, operation, client_leg, expected", EXIT_REASON_CASES)
async def test_exit_reason_when_the_position_reaches_zero(rig, origin, purpose, intent, operation, client_leg,
                                                          expected):
    await rig.up()
    rec = await rig.open_strategy(qty=1, price=100)
    intent_id = None
    if operation is not None:
        await add_operation(rig.ledger, *operation)
    if intent is not None:
        spec = dict(CLOSE_INTENT)
        spec.update(intent)
        intent_id = await add_intent(rig.ledger, **spec)
    kwargs = {}
    if client_leg is not None:
        kwargs["client_order_id"] = ids.make_client_id("a1", "1", 3, client_leg)
    closed = await rig.apply(qty=1, price=100, opening=False, origin=origin, purpose=purpose, intent_id=intent_id,
                             **kwargs)
    assert closed.id == rec.id and closed.status is PositionStatus.CLOSED
    assert closed.exit_reason is expected
    assert (await trade_of(rig.ledger, rec.id)).exit_reason is expected
    assert closed.stop_note == ""  # 只有推定止损才写说明


# ================================================================ 推定为系统止损触发

async def _protected(rig, *, side=LONG, qty=2, stop="95", state=ProtectionState.SYSTEM):
    rec = await rig.open_strategy(side=side, qty=qty, price=100)
    await rig.registry.set_protection(rec.id, state=state, stop_price=None if stop is None else D(stop),
                                      stop_client_id="DKa1-1-1-s", stop_algo_id="55")
    return rec


async def test_unlinked_trigger_fill_is_presumed_to_be_the_system_stop(rig, clock):
    await rig.up()
    rec = await _protected(rig, qty=2, stop="95")
    rig.clear_events()
    fill = await make_fill(rig.ledger, qty=2, price=94, opening=False, origin=EXTERNAL, purpose=None,
                           time_ms=T0 + 3_000)
    closed = await rig.registry.apply_fill(fill)
    assert closed.status is PositionStatus.CLOSED and closed.exit_reason is ExitReason.STOP
    assert closed.stop_note == PRESUMED_STOP_NOTE == "推定为系统止损触发，待条件单对账复核"
    assert closed.closed_ms == T0 + 3_000
    assert (closed.stop_client_id, closed.stop_algo_id) == ("DKa1-1-1-s", "55")  # 留给条件单对账复核
    assert (await trade_of(rig.ledger, rec.id)).exit_reason is ExitReason.STOP
    assert await rig.events() == [PositionClosed(position_id=rec.id, symbol=SYM, side=LONG, exit_reason="stop",
                                                 at_ms=clock.now_ms())]
    audit = [r for r in await audit_rows(rig.ledger) if r.action == "position.close"]
    assert audit[-1].after["exit_reason"] == "stop" and audit[-1].after["cause"] == PRESUMED_STOP_NOTE


@pytest.mark.parametrize("price, presumed", [
    ("94", True),
    ("95", True),
    ("95.475", True),    # 止损价 × 1.005：边界含在内
    ("95.476", False),
    ("100", False),
])
async def test_presumed_stop_price_window_for_long(rig, price, presumed):
    await rig.up()
    await _protected(rig, side=LONG, qty=1, stop="95")
    closed = await rig.apply(qty=1, price=price, opening=False, origin=EXTERNAL, purpose=None)
    assert closed.exit_reason is (ExitReason.STOP if presumed else ExitReason.EXTERNAL)
    assert (closed.stop_note == PRESUMED_STOP_NOTE) is presumed


@pytest.mark.parametrize("price, presumed", [
    ("106", True),
    ("105", True),
    ("104.475", True),   # 止损价 × 0.995：边界含在内
    ("104.474", False),
    ("100", False),
])
async def test_presumed_stop_price_window_for_short(rig, price, presumed):
    await rig.up()
    await _protected(rig, side=SHORT, qty=1, stop="105")
    closed = await rig.apply(qty=1, price=price, opening=False, origin=EXTERNAL, purpose=None, position_side=SHORT)
    assert closed.exit_reason is (ExitReason.STOP if presumed else ExitReason.EXTERNAL)
    assert (closed.stop_note == PRESUMED_STOP_NOTE) is presumed


@pytest.mark.parametrize("state, stop", [
    (ProtectionState.EXTERNAL, "95"),
    (ProtectionState.NONE, "95"),
    (ProtectionState.REBUILDING, "95"),
    (ProtectionState.SYSTEM, None),   # 系统保护但没有止损价
])
async def test_no_presumption_without_system_protection_and_a_stop_price(rig, state, stop):
    await rig.up()
    await _protected(rig, qty=1, state=state, stop=stop)
    closed = await rig.apply(qty=1, price=90, opening=False, origin=EXTERNAL, purpose=None)
    assert closed.exit_reason is ExitReason.EXTERNAL and closed.stop_note == ""


async def test_presumption_needs_the_fill_to_equal_the_whole_position(rig):
    await rig.up()
    await _protected(rig, qty=2, stop="95")
    part = await rig.apply(qty=1, price=94, opening=False, origin=EXTERNAL, purpose=None)
    assert part.status is PositionStatus.MANAGED and part.qty == D(1)  # 只是减仓：不推定
    assert part.exit_reason is None and part.stop_note == ""
    last = await rig.apply(qty=1, price=94, opening=False, origin=EXTERNAL, purpose=None)  # 这一笔等于剩余持仓
    assert last.exit_reason is ExitReason.STOP and last.stop_note == PRESUMED_STOP_NOTE


async def test_oversized_fill_is_not_presumed_to_be_the_stop(rig):
    await rig.up()
    await _protected(rig, qty=2, stop="95")
    closed = await rig.apply(qty=3, price=94, opening=False, origin=EXTERNAL, purpose=None)
    assert closed.status is PositionStatus.CLOSED and closed.exit_reason is ExitReason.EXTERNAL


@pytest.mark.parametrize("origin", [OrderOrigin.EXTERNAL, OrderOrigin.UNKNOWN, OrderOrigin.LEGACY,
                                    OrderOrigin.CCXT_DEFAULT])
async def test_every_unattributed_origin_can_be_presumed(rig, origin):
    await rig.up()
    await _protected(rig, qty=1, stop="95")
    closed = await rig.apply(qty=1, price=94, opening=False, origin=origin, purpose=None)
    assert closed.exit_reason is ExitReason.STOP


@pytest.mark.parametrize("origin, expected", [(OrderOrigin.LIQUIDATION, ExitReason.LIQUIDATION),
                                              (OrderOrigin.ADL, ExitReason.ADL),
                                              (OrderOrigin.EMERGENCY, ExitReason.EMERGENCY)])
async def test_identified_origins_beat_the_presumption(rig, origin, expected):
    await rig.up()
    await _protected(rig, qty=1, stop="95")
    closed = await rig.apply(qty=1, price=90, opening=False, origin=origin, purpose=None)
    assert closed.exit_reason is expected and closed.stop_note == ""


async def test_system_close_fill_is_never_presumed_to_be_the_stop(rig):
    await rig.up()
    await _protected(rig, qty=1, stop="95")
    iid = await add_intent(rig.ledger, action=IntentAction.CLOSE, reason="manual")
    closed = await rig.apply(qty=1, price=90, opening=False, intent_id=iid)  # 系统平仓单，价格虽然越过止损价
    assert closed.exit_reason is ExitReason.MANUAL and closed.stop_note == ""


# ================================================================ 幂等、并发、事件总线

async def test_applying_the_same_fill_twice_has_no_second_effect(rig):
    await rig.up()
    rec = await rig.open_strategy(qty=1, price=100)
    fill = await make_fill(rig.ledger, qty=1, price=110, intent_id=await add_intent(rig.ledger))
    first = await rig.registry.apply_fill(fill)
    rig.clear_events()
    again = await rig.registry.apply_fill(fill)
    assert again == first and again.qty == D(2) and again.version == rec.version + 1
    assert await rig.events() == []

    close = await make_fill(rig.ledger, qty=2, price=120, opening=False, intent_id=await add_intent(
        rig.ledger, action=IntentAction.CLOSE, reason="signal"))
    closed = await rig.registry.apply_fill(close)
    rig.clear_events()
    again_closed = await rig.registry.apply_fill(close)
    assert again_closed == closed and closed.status is PositionStatus.CLOSED
    assert await rig.events() == []
    assert len(await all_position_rows(rig.ledger)) == 1


async def test_fill_that_is_not_in_the_ledger_is_rejected(rig):
    await rig.up()
    fill = await make_fill(rig.ledger, qty=1, price=100, intent_id=await add_intent(rig.ledger))
    bogus = FillIngested(fill_id=999_999, trade=fill.trade, client_order_id=fill.client_order_id, origin=fill.origin,
                         intent_id=fill.intent_id, order_purpose=fill.order_purpose, path="stream", at_ms=T0)
    with pytest.raises(LookupError):
        await rig.registry.apply_fill(bogus)
    assert rig.registry.all_active() == []


async def test_concurrent_fills_never_lose_quantity(rig):
    other = rig.another()
    await rig.up()
    await other.up()
    await rig.open_strategy(qty=1, price=100)
    await other.registry.load()
    iid = await add_intent(rig.ledger)
    fills = [await make_fill(rig.ledger, qty=1, price=100, intent_id=iid) for _ in range(6)]
    await asyncio.gather(*(
        (rig if i % 2 == 0 else other).registry.apply_fill(f) for i, f in enumerate(fills)))
    rows = await all_position_rows(rig.ledger)
    assert len(rows) == 1 and rows[0].qty == D(7) and rows[0].max_qty == D(7) and rows[0].version == 7


async def test_registry_applies_fills_published_on_the_bus(rig):
    await rig.up()
    await rig.registry.start()
    await rig.registry.start()  # 重复启动无害
    iid = await add_intent(rig.ledger)
    fill = await make_fill(rig.ledger, qty=1, price=100, intent_id=iid)
    rig.bus.publish(fill)
    await rig.events()
    rec = rig.registry.get_active(SYM, LONG)
    assert rec is not None and rec.qty == D(1) and await fill_position_id(rig.ledger, fill.fill_id) == rec.id

    await rig.registry.stop()
    later = await make_fill(rig.ledger, qty=1, price=100, intent_id=iid)
    rig.bus.publish(later)
    await rig.events()
    assert rig.registry.get_active(SYM, LONG).qty == D(1)  # 停止后不再自动应用
    assert await fill_position_id(rig.ledger, later.fill_id) is None


async def test_a_failing_fill_does_not_stop_the_subscription(rig):
    await rig.up()
    await rig.registry.start()
    good = await make_fill(rig.ledger, qty=1, price=100, intent_id=await add_intent(rig.ledger))
    bogus = FillIngested(fill_id=999_999, trade=good.trade, client_order_id=None, origin=EXTERNAL,
                         intent_id=None, order_purpose=None, path="rest", at_ms=T0)
    rig.bus.publish(bogus)
    rig.bus.publish(good)
    await rig.events()
    assert rig.registry.get_active(SYM, LONG) is not None


async def test_start_loads_the_cache_when_it_was_not_loaded(rig):
    await rig.up()
    rec = await rig.open_strategy()
    fresh = rig.another()
    await fresh.registry.start()  # 没有先 load：start 会补上
    assert fresh.registry.get_active(SYM, LONG) == rec
    await fresh.registry.stop()


# ================================================================ 补充：用途不符、整体回滚、配置缺省

@pytest.mark.parametrize("purpose", ["close", "partial_close", "stop", "dust_close"])
async def test_system_open_direction_fill_with_a_non_open_purpose_is_reported_not_registered(rig, clock, purpose):
    """只有「系统编号且订单用途为开仓」才登记；编号是系统的、方向是开仓、用途却不是开仓，自相矛盾，按未登记处理。"""
    await rig.up()
    iid = await add_intent(rig.ledger)
    fill = await make_fill(rig.ledger, qty=1, price=100, intent_id=iid, purpose=purpose, opening=True)
    assert await rig.registry.apply_fill(fill) is None
    assert rig.registry.all_active() == [] and await all_position_rows(rig.ledger) == []
    assert await fill_position_id(rig.ledger, fill.fill_id) is None
    assert await rig.events() == [UnregisteredPositionFound(symbol=SYM, side=LONG, qty=D(1), entry_price=D(100),
                                                            trigger="external_fill", at_ms=clock.now_ms())]


async def test_closing_fill_rolls_everything_back_when_the_trade_cannot_be_built(rig, monkeypatch):
    """归零是一个事务：改持仓、回填成交、写审计、生成逐笔交易，任何一步失败都整体回滚，重试可以成功。"""
    await rig.up()
    rec = await rig.open_strategy(qty=1, price=100)
    iid = await add_intent(rig.ledger, action=IntentAction.CLOSE, reason="signal")
    fill = await make_fill(rig.ledger, qty=1, price=110, opening=False, intent_id=iid)
    rig.clear_events()

    def broken(session, position_id):
        raise RuntimeError("生成逐笔交易失败")

    monkeypatch.setattr(roundtrip, "build_trade", broken)
    with pytest.raises(RuntimeError):
        await rig.registry.apply_fill(fill)
    assert rig.registry.get_active(SYM, LONG) == rec and await rig.registry.fetch(rec.id) == rec
    assert await fill_position_id(rig.ledger, fill.fill_id) is None
    assert [r for r in await audit_rows(rig.ledger) if r.action == "position.close"] == []
    assert await rig.events() == []

    monkeypatch.undo()
    closed = await rig.registry.apply_fill(fill)
    assert closed.status is PositionStatus.CLOSED and closed.exit_reason is ExitReason.SIGNAL
    assert (await trade_of(rig.ledger, rec.id)) is not None


@pytest.mark.parametrize("config", [
    {"leverage": None, "margin_mode": None},          # 「待你定」且还没定
    {"leverage": "x", "margin_mode": "weird"},
    {"leverage": 0, "margin_mode": ""},
    {"leverage": True, "margin_mode": 3},
])
async def test_unusable_config_values_leave_leverage_and_margin_mode_unset(ledger, clock, config):
    rig = Rig(ledger, clock)
    rig.config.clear()
    rig.config.update(config)
    await rig.up()
    rec = await rig.apply(qty=1, price=100, intent_id=await add_intent(ledger))
    assert rec is not None and (rec.leverage, rec.margin_mode) == (None, None)
