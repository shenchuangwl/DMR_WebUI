"""exchange.ids：系统订单编号的生成与来源识别（计划报告 4.6「订单编号规则」）。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import OrderOrigin
from dmr_kernel.exchange import ids


def test_system_id_round_trip_and_constraints():
    cid = ids.make_client_id("A7", "1", 123456, "o")
    assert len(cid) <= 36 and ids.SAFE_CHARS.fullmatch(cid)
    p = ids.parse_client_id(cid)
    assert p.origin is OrderOrigin.SYSTEM
    assert (p.instance, p.model_code, p.intent_id, p.leg) == ("A7", "1", 123456, "o")


def test_system_id_never_contains_symbol_names():
    cid = ids.make_client_id("A7", "2", 1, "s")
    assert "USDT" not in cid


@pytest.mark.parametrize("bad", [("", "1", 1, "o"), ("ABCDE", "1", 1, "o"), ("A", "9", 1, "o"),
                                 ("A", "1", -1, "o"), ("A", "1", 1, "O"), ("A", "1", 1, "o123")])
def test_invalid_components_are_rejected(bad):
    with pytest.raises(ValueError):
        ids.make_client_id(*bad)


def test_largest_intent_id_still_fits():
    cid = ids.make_client_id("A7zz", "0", 36 ** 10 - 1, "c99")
    assert len(cid) <= 36


@pytest.mark.parametrize("cid,origin", [
    ("DKE-lx2k3m-1", OrderOrigin.EMERGENCY),
    ("autoclose-1791949893", OrderOrigin.LIQUIDATION),
    ("adl_autoclose-1791949893", OrderOrigin.ADL),
    ("x-cvBPrNm9abcdef0123456789", OrderOrigin.CCXT_DEFAULT),
    ("DMR12_SHORT_sell_SHORT_1726640000000", OrderOrigin.LEGACY),
    ("DMR6_LONG_buy_LONG_1726640000000", OrderOrigin.LEGACY),
    ("DMR26_SHORT_buy_SHORT_172664000000", OrderOrigin.LEGACY),       # 截断到 36 字符
    ("FIRST_SHORT_sell_SHORT_1726640000", OrderOrigin.LEGACY),
    ("BIT_LONG_buy_LONG_1726640000000", OrderOrigin.LEGACY),
    ("DMR_SHORT_sell_SHORT_1726640000000", OrderOrigin.LEGACY),
    ("web_k2j3h4g5f6d7s8a9", OrderOrigin.EXTERNAL),
    ("ios_123", OrderOrigin.EXTERNAL),
    ("1234567890", OrderOrigin.EXTERNAL),
])
def test_origin_classification(cid, origin):
    assert ids.parse_client_id(cid).origin is origin


def test_missing_client_id_is_unknown_origin():
    assert ids.parse_client_id(None).origin is OrderOrigin.UNKNOWN
    assert ids.parse_client_id("").origin is OrderOrigin.UNKNOWN


def test_system_id_check_can_require_this_instance():
    cid = ids.make_client_id("A7", "1", 5, "o")
    assert ids.is_system_id(cid)
    assert ids.is_system_id(cid, instance="A7")
    assert not ids.is_system_id(cid, instance="B1")
    assert not ids.is_system_id("web_abc")


def test_emergency_id_is_recognized_and_short():
    cid = ids.make_emergency_id(1791949893522, 3)
    assert len(cid) <= 36
    assert ids.parse_client_id(cid).origin is OrderOrigin.EMERGENCY


def test_leg_helpers():
    assert ids.resend_leg("c", 1) == "c1"
    assert ids.resend_leg("c1", 2) == "c2"
    with pytest.raises(ValueError):
        ids.resend_leg("c", 100)
