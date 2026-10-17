"""core.types：共享枚举与数据类型的约定。"""
from __future__ import annotations

from decimal import Decimal

from dmr_kernel.core import types as t


def test_position_side_helpers():
    assert t.PositionSide.LONG.open_side() is t.OrderSide.BUY
    assert t.PositionSide.LONG.close_side() is t.OrderSide.SELL
    assert t.PositionSide.SHORT.open_side() is t.OrderSide.SELL
    assert t.PositionSide.SHORT.close_side() is t.OrderSide.BUY
    assert t.PositionSide.LONG.opposite() is t.PositionSide.SHORT


def test_intent_states_are_the_ten_documented_states():
    assert [s.value for s in t.IntentState] == [
        "CREATED", "VALIDATED", "RESERVED", "DISPATCH_READY", "ACTIVE",
        "RECONCILING", "COMPLETED", "REJECTED", "EXPIRED", "CANCELLED",
    ]
    assert t.IntentState.terminal() == {
        t.IntentState.COMPLETED, t.IntentState.REJECTED,
        t.IntentState.EXPIRED, t.IntentState.CANCELLED,
    }


def test_order_states_are_the_eight_documented_states():
    assert [s.value for s in t.OrderState] == [
        "PENDING_SEND", "SENT_UNKNOWN", "ACKNOWLEDGED", "PARTIALLY_FILLED",
        "FILLED", "CANCEL_PENDING", "CANCELLED", "REJECTED",
    ]
    assert t.OrderState.terminal() == {
        t.OrderState.FILLED, t.OrderState.CANCELLED, t.OrderState.REJECTED,
    }


def test_registered_position_statuses_exclude_pending_and_ignored():
    reg = t.PositionStatus.registered()
    assert reg == {t.PositionStatus.TAKING_OVER, t.PositionStatus.MANAGED, t.PositionStatus.CLOSING}
    assert t.PositionStatus.PENDING not in reg and t.PositionStatus.IGNORED not in reg


def test_suppression_reasons_cover_the_nine_documented_reasons():
    assert len(list(t.Suppression)) == 9
    assert {o.value for o in t.OpLock} == {"switch_preview", "switching", "bulk_closing"}


def _inst(**kw):
    base = dict(symbol="BTCUSDT", base="BTC", quote="USDT", contract_type="PERPETUAL",
                status="TRADING", tick_size=Decimal("0.1"), step_size=Decimal("0.001"),
                min_qty=Decimal("0.001"), min_notional=Decimal("100"))
    base.update(kw)
    return t.Instrument(**base)


def test_instrument_tradable_requires_perpetual_usdt_trading():
    assert _inst().tradable
    assert not _inst(status="SETTLING").tradable
    assert not _inst(contract_type="CURRENT_QUARTER").tradable
    assert not _inst(quote="USDC").tradable


def test_algo_order_open_uses_raw_status():
    def algo(raw):
        return t.AlgoOrderInfo(
            symbol="BTCUSDT", client_algo_id="DKA-1-1-s", algo_id="9", side=t.OrderSide.SELL,
            position_side=t.PositionSide.LONG, algo_type=t.AlgoType.STOP_MARKET,
            trigger_price=Decimal("1"), working_type=t.WorkingType.CONTRACT_PRICE,
            close_position=True, qty=None, raw_status=raw, actual_order_id=None,
            create_time_ms=0, update_time_ms=0)
    assert algo("NEW").is_open and algo("TRIGGERING").is_open
    for raw in ("TRIGGERED", "FINISHED", "CANCELED", "EXPIRED", "REJECTED"):
        assert not algo(raw).is_open


def test_read_results_are_explicit_known_or_unknown():
    k = t.Known([1, 2])
    u = t.Unknown("timeout")
    assert t.is_known(k) and not t.is_known(u)
    assert k.value == [1, 2]
    # 「未知」绝不能被当成「空」：Unknown 没有 value 属性
    assert not hasattr(u, "value")


def test_risk_decision_is_a_shared_frozen_type():
    d = t.RiskDecision(False, "已达持仓上限", "account")
    assert (d.allowed, d.reason, d.level) == (False, "已达持仓上限", "account")
    assert t.RiskDecision(True) == t.RiskDecision(True, "", "")
