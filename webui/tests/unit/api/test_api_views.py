"""视图函数（api.views）：账本记录、交易所数据 -> 报文里的 JSON 结构。纯函数，不碰账本与网络。

约定（docs/plans/M1-控制接口只读部分.md）：金额数量一律字符串（不用科学计数法），时间是 UTC 毫秒整数，可能缺的给 null。
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from enum import StrEnum

import pytest

from api_kit import T0, algo_info, order_info, order_record, position_info, position_record
from dmr_kernel.api import views
from dmr_kernel.core.events import OrdersSnapshotTaken
from dmr_kernel.core.types import (
    AlgoType,
    OrderPurpose,
    OrderState,
    Ownership,
    PendingReason,
    PositionSide,
    PositionStatus,
    ProtectionState,
)

D = Decimal

POSITION_KEYS = (
    "id", "symbol", "side", "generation", "status", "ownership", "is_mixed", "qty", "avg_price", "mark_price",
    "unrealized_pnl", "liquidation_price", "protection_state", "stop_price", "stop_note", "exit_ruleset",
    "signal_exit", "open_time_ms", "open_time_estimated", "exchange_qty", "consistent", "has_unconfirmed_orders",
    "closing", "pending_reason", "pending_flags", "origin_note", "leverage", "margin_mode", "has_stop_on_exchange",
    "version")
ORDER_KEYS = (
    "client_order_id", "intent_id", "purpose", "symbol", "side", "position_side", "kind", "order_type", "qty",
    "filled_qty", "avg_price", "trigger_price", "state", "raw_status", "sent_ms", "updated_ms")


class Colour(StrEnum):
    RED = "red"


# ---------------------------------------------------------------- 数字与 JSON 化

@pytest.mark.parametrize("value, text", [
    (D("0.50"), "0.50"), (D("60000"), "60000"), (D("1E+3"), "1000"), (D("6E+4"), "60000"),
    (D("-0.00000001"), "-0.00000001"), (D("0"), "0"), (D("123456789012345678.123456789"), "123456789012345678.123456789"),
])
def test_plain_never_uses_scientific_notation(value, text):
    assert views.plain(value) == text


def test_plain_keeps_none_and_stringifies_non_finite_numbers():
    assert views.plain(None) is None
    assert views.plain(D("NaN")) == "NaN"


def test_jsonable_turns_decimals_into_strings_and_enums_into_values():
    data = {"qty": D("0.5"), "side": PositionSide.LONG, "nested": [{"p": D("1E+2")}], "tuple": (D("1"), Colour.RED)}
    assert views.jsonable(data) == {"qty": "0.5", "side": "LONG", "nested": [{"p": "100"}], "tuple": ["1", "red"]}


def test_jsonable_yields_plain_strings_for_string_enums():
    out = views.jsonable({"side": PositionSide.LONG})
    assert type(out["side"]) is str


def test_jsonable_keeps_json_native_values_exactly():
    data = {"a": None, "b": True, "c": 3, "d": 1.5, "e": "x", "f": False, "g": 0}
    out = views.jsonable(data)
    assert out == data and out["b"] is True and out["f"] is False and out["g"] == 0 and out["g"] is not False


def test_jsonable_handles_sets_and_non_string_keys_deterministically():
    out = views.jsonable({1: frozenset({"b", "a"}), "s": {3, 1, 2}})
    assert out == {"1": ["a", "b"], "s": [1, 2, 3]}


def test_jsonable_replaces_non_finite_floats_with_null_so_the_output_is_valid_json():
    out = views.jsonable({"x": math.nan, "y": math.inf})
    assert out == {"x": None, "y": None}
    json.dumps(out, allow_nan=False)


@pytest.mark.parametrize("bad", [object(), b"bytes", complex(1, 2)])
def test_jsonable_refuses_types_it_does_not_know_instead_of_guessing(bad):
    with pytest.raises(TypeError):
        views.jsonable({"x": bad})


# ---------------------------------------------------------------- 持仓

def test_position_view_has_exactly_the_agreed_fields_in_order():
    view = views.position_view(position_record(), quote=None, unconfirmed=False, stop_on_exchange=None)
    assert tuple(view) == POSITION_KEYS


def test_position_view_values_for_a_managed_strategy_position():
    quote = position_info(mark=D("61000"), pnl=D("500"))
    view = views.position_view(position_record(), quote=quote, unconfirmed=False, stop_on_exchange=True)
    assert view["id"] == 1 and view["symbol"] == "BTCUSDT" and view["side"] == "LONG" and view["generation"] == 1
    assert view["status"] == "managed" and view["ownership"] == "strategy" and view["is_mixed"] is False
    assert (view["qty"], view["avg_price"]) == ("0.5", "60000")
    assert (view["mark_price"], view["unrealized_pnl"], view["liquidation_price"]) == ("61000", "500", "30000")
    assert (view["protection_state"], view["stop_price"], view["stop_note"]) == ("system", "58000", "")
    assert (view["exit_ruleset"], view["signal_exit"]) == ("strategy", False)
    assert (view["open_time_ms"], view["open_time_estimated"]) == (T0, False)
    assert (view["exchange_qty"], view["consistent"]) == ("0.5", True)
    assert (view["has_unconfirmed_orders"], view["closing"], view["has_stop_on_exchange"]) == (False, False, True)
    assert (view["pending_reason"], view["pending_flags"], view["origin_note"]) == (None, [], "")
    assert (view["leverage"], view["margin_mode"], view["version"]) == (2, "isolated", 3)


def test_mark_price_and_pnl_are_null_without_a_snapshot_quote():
    view = views.position_view(position_record(), quote=None, unconfirmed=False, stop_on_exchange=None)
    assert view["mark_price"] is None and view["unrealized_pnl"] is None
    assert view["liquidation_price"] == "30000"  # 强平价取自登记，不依赖快照


def test_a_quote_without_mark_or_pnl_gives_null_not_zero():
    quote = position_info(mark=None, pnl=None)
    view = views.position_view(position_record(), quote=quote, unconfirmed=False, stop_on_exchange=None)
    assert view["mark_price"] is None and view["unrealized_pnl"] is None


def test_closing_flag_follows_the_status():
    closing = views.position_view(position_record(status=PositionStatus.CLOSING), quote=None, unconfirmed=False,
                                  stop_on_exchange=None)
    assert closing["closing"] is True and closing["status"] == "closing"
    taking = views.position_view(position_record(status=PositionStatus.TAKING_OVER), quote=None, unconfirmed=False,
                                 stop_on_exchange=None)
    assert taking["closing"] is False


def test_pending_position_carries_reason_flags_and_note():
    rec = position_record(status=PositionStatus.PENDING, ownership=Ownership.NONE, exit_ruleset=None,
                          protection_state=ProtectionState.NONE, stop_price=None, pending_reason=PendingReason.OUT_OF_SCOPE,
                          pending_flags=["manual", "origin_to_verify"], origin_note="来源不明", exchange_qty=None,
                          consistent=None, open_time_ms=None, open_time_estimated=True, leverage=None, margin_mode=None,
                          liquidation_price=None)
    view = views.position_view(rec, quote=None, unconfirmed=True, stop_on_exchange=False)
    assert (view["status"], view["ownership"], view["exit_ruleset"]) == ("pending", "none", None)
    assert (view["pending_reason"], view["pending_flags"], view["origin_note"]) == (
        "out_of_scope", ["manual", "origin_to_verify"], "来源不明")
    assert (view["stop_price"], view["exchange_qty"], view["consistent"], view["open_time_ms"]) == (None,) * 4
    assert (view["leverage"], view["margin_mode"], view["liquidation_price"]) == (None, None, None)
    assert view["has_unconfirmed_orders"] is True and view["has_stop_on_exchange"] is False
    assert view["open_time_estimated"] is True and view["protection_state"] == "none"


def test_position_view_copies_the_flag_list():
    rec = position_record(pending_flags=["a"])
    view = views.position_view(rec, quote=None, unconfirmed=False, stop_on_exchange=None)
    view["pending_flags"].append("b")
    assert rec.pending_flags == ["a"]


def test_position_view_is_json_serialisable():
    quote = position_info()
    view = views.position_view(position_record(), quote=quote, unconfirmed=False, stop_on_exchange=True)
    json.dumps(views.jsonable(view), allow_nan=False)


# ---------------------------------------------------------------- 持仓旁边的判定

def test_unconfirmed_orders_are_those_whose_fate_is_not_settled():
    records = [
        order_record(client_order_id="a", state=OrderState.PENDING_SEND, symbol="BTCUSDT"),
        order_record(client_order_id="b", state=OrderState.SENT_UNKNOWN, symbol="ETHUSDT",
                     position_side=PositionSide.SHORT),
        order_record(client_order_id="c", state=OrderState.CANCEL_PENDING, symbol="XRPUSDT"),
        order_record(client_order_id="d", state=OrderState.ACKNOWLEDGED, symbol="SOLUSDT"),
        order_record(client_order_id="e", state=OrderState.PARTIALLY_FILLED, symbol="ADAUSDT"),
    ]
    keys = views.unconfirmed_keys(records)
    assert keys == {("BTCUSDT", PositionSide.LONG), ("ETHUSDT", PositionSide.SHORT), ("XRPUSDT", PositionSide.LONG)}
    assert views.unconfirmed_keys([]) == set()


def snapshot(*algos, orders=()):
    return OrdersSnapshotTaken(open_orders=tuple(orders), algo_orders=tuple(algos), at_ms=T0)


def test_stop_on_exchange_is_unknown_without_an_orders_snapshot():
    assert views.stop_on_exchange(None, "BTCUSDT", PositionSide.LONG) is None


def test_stop_on_exchange_needs_an_open_stop_kind_algo_on_the_same_symbol_and_side():
    stop = algo_info("BTCUSDT", position_side=PositionSide.LONG, algo_type=AlgoType.STOP_MARKET, raw="NEW")
    assert views.stop_on_exchange(snapshot(stop), "BTCUSDT", PositionSide.LONG) is True
    assert views.stop_on_exchange(snapshot(stop), "BTCUSDT", PositionSide.SHORT) is False
    assert views.stop_on_exchange(snapshot(stop), "ETHUSDT", PositionSide.LONG) is False
    assert views.stop_on_exchange(snapshot(), "BTCUSDT", PositionSide.LONG) is False  # 有快照而没有止损


def test_stop_on_exchange_ignores_finished_and_non_stop_algos():
    done = algo_info("BTCUSDT", raw="FINISHED")
    take_profit = algo_info("BTCUSDT", cid="DKA-1-1-t", aid="2002", algo_type=AlgoType.TAKE_PROFIT_MARKET)
    assert views.stop_on_exchange(snapshot(done, take_profit), "BTCUSDT", PositionSide.LONG) is False
    triggering = algo_info("BTCUSDT", aid="2003", raw="TRIGGERING")
    assert views.stop_on_exchange(snapshot(done, triggering), "BTCUSDT", PositionSide.LONG) is True


# ---------------------------------------------------------------- 订单

def test_order_view_has_exactly_the_agreed_fields_in_order():
    assert tuple(views.order_view(order_record())) == ORDER_KEYS


def test_order_view_values():
    rec = order_record(client_order_id="DKA-1-5-s", intent_id=5, purpose=OrderPurpose.STOP, kind="algo",
                       order_type="STOP_MARKET", qty=None, trigger_price=D("58000"), filled_qty=D("0"),
                       state=OrderState.PARTIALLY_FILLED, raw_status="PARTIALLY_FILLED", avg_price=D("57990.5"),
                       sent_ms=T0 + 1, updated_ms=T0 + 9)
    view = views.order_view(rec)
    assert view == {
        "client_order_id": "DKA-1-5-s", "intent_id": 5, "purpose": "stop", "symbol": "BTCUSDT", "side": "BUY",
        "position_side": "LONG", "kind": "algo", "order_type": "STOP_MARKET", "qty": None, "filled_qty": "0",
        "avg_price": "57990.5", "trigger_price": "58000", "state": "PARTIALLY_FILLED",
        "raw_status": "PARTIALLY_FILLED", "sent_ms": T0 + 1, "updated_ms": T0 + 9}


def test_exchange_order_view_keeps_what_the_exchange_reported():
    info = order_info("ETHUSDT", cid=None, oid="777", position_side=PositionSide.SHORT, qty=D("2"), filled=D("0.5"),
                      avg=D("3000.25"), status="PARTIALLY_FILLED", at=T0 + 3)
    assert views.exchange_order_view(info) == {
        "client_order_id": None, "exchange_order_id": "777", "symbol": "ETHUSDT", "side": "BUY",
        "position_side": "SHORT", "order_type": "LIMIT", "qty": "2", "filled_qty": "0.5", "avg_price": "3000.25",
        "status": "PARTIALLY_FILLED", "raw_status": "PARTIALLY_FILLED", "close_position": False,
        "reduce_only": False, "updated_ms": T0 + 3}


def test_exchange_algo_view_keeps_what_the_exchange_reported():
    info = algo_info("BTCUSDT", cid="DKA-1-1-s", aid="2001", trigger=D("59000"), at=T0 + 4)
    assert views.exchange_algo_view(info) == {
        "client_algo_id": "DKA-1-1-s", "algo_id": "2001", "symbol": "BTCUSDT", "side": "SELL",
        "position_side": "LONG", "algo_type": "STOP_MARKET", "trigger_price": "59000",
        "working_type": "MARK_PRICE", "close_position": True, "qty": None, "raw_status": "NEW",
        "actual_order_id": None, "created_ms": T0 + 4, "updated_ms": T0 + 4}
