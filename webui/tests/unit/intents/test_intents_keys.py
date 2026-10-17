"""幂等键（纯函数）：同样的输入给出同样的键；任何一个组成部分不同，键就不同；不同种类的键互不相等。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.intents import key_op, key_open, key_protect, key_signal_close
from dmr_kernel.core.types import Model, PositionSide

BAR = 1_760_000_100_000
STEP = 900_000  # 15 分钟


def test_key_open_is_deterministic_and_has_a_fixed_format():
    key = key_open("m1", "BTCUSDT", PositionSide.LONG, BAR)
    assert key == key_open("m1", "BTCUSDT", PositionSide.LONG, BAR)
    # 键存进库里，格式变了就等于换了一套幂等键：这里把格式钉死
    assert key == f"open:m1:BTCUSDT:LONG:{BAR}"


def test_key_open_every_component_matters():
    base = key_open("m1", "BTCUSDT", PositionSide.LONG, BAR)
    variants = {
        key_open("m2", "BTCUSDT", PositionSide.LONG, BAR),
        key_open("m1", "ETHUSDT", PositionSide.LONG, BAR),
        key_open("m1", "BTCUSDT", PositionSide.SHORT, BAR),
        key_open("m1", "BTCUSDT", PositionSide.LONG, BAR + STEP),
    }
    assert base not in variants
    assert len(variants) == 4


def test_key_open_accepts_enums_and_plain_strings_alike():
    assert key_open(Model.M1, "BTCUSDT", PositionSide.LONG, BAR) == key_open("m1", "BTCUSDT", "LONG", BAR)


def test_key_open_reverse_marker_carries_the_generation_of_the_closed_position():
    plain = key_open("m1", "BTCUSDT", PositionSide.SHORT, BAR)
    rev3 = key_open("m1", "BTCUSDT", PositionSide.SHORT, BAR, reverse_of_generation=3)
    rev4 = key_open("m1", "BTCUSDT", PositionSide.SHORT, BAR, reverse_of_generation=4)
    rev0 = key_open("m1", "BTCUSDT", PositionSide.SHORT, BAR, reverse_of_generation=0)
    assert len({plain, rev3, rev4, rev0}) == 4  # 代际 0 也要和「不是反手」区分开


def test_key_open_injected_sequence_allows_several_injections_on_one_bar():
    plain = key_open("m1", "BTCUSDT", PositionSide.LONG, BAR)
    inj1 = key_open("m1", "BTCUSDT", PositionSide.LONG, BAR, injected_seq=1)
    inj2 = key_open("m1", "BTCUSDT", PositionSide.LONG, BAR, injected_seq=2)
    both = key_open("m1", "BTCUSDT", PositionSide.LONG, BAR, reverse_of_generation=1, injected_seq=1)
    rev = key_open("m1", "BTCUSDT", PositionSide.LONG, BAR, reverse_of_generation=1)
    assert len({plain, inj1, inj2, both, rev}) == 5


def test_key_signal_close_components():
    key = key_signal_close("BTCUSDT", PositionSide.LONG, 2, BAR)
    assert key == key_signal_close("BTCUSDT", PositionSide.LONG, 2, BAR)
    assert key == f"sigclose:BTCUSDT:LONG:g2:{BAR}"
    variants = {
        key_signal_close("ETHUSDT", PositionSide.LONG, 2, BAR),
        key_signal_close("BTCUSDT", PositionSide.SHORT, 2, BAR),
        key_signal_close("BTCUSDT", PositionSide.LONG, 3, BAR),  # 同一合约方向的下一代仓不会撞上上一代的信号平仓
        key_signal_close("BTCUSDT", PositionSide.LONG, 2, BAR + STEP),
    }
    assert key not in variants and len(variants) == 4


def test_key_op_components_and_optional_parts():
    only = key_op("op-1")
    with_symbol = key_op("op-1", "BTCUSDT")
    with_side = key_op("op-1", "BTCUSDT", PositionSide.LONG)
    with_leg = key_op("op-1", "BTCUSDT", PositionSide.LONG, "close")
    assert only == "op:op-1"
    assert len({only, with_symbol, with_side, with_leg}) == 4
    assert key_op("op-2", "BTCUSDT", PositionSide.LONG) != with_side
    # 只给方向不给合约，和只给合约不给方向不能撞
    assert key_op("op-1", None, PositionSide.LONG) != key_op("op-1", "LONG")


def test_key_op_is_stable_for_the_same_operation():
    assert key_op("op-1", "BTCUSDT", "LONG") == key_op("op-1", "BTCUSDT", PositionSide.LONG)


def test_key_protect_components():
    key = key_protect(7, 2, "initial", 1)
    assert key == key_protect(7, 2, "initial", 1)
    assert key == "protect:7:g2:initial:1"
    variants = {key_protect(8, 2, "initial", 1), key_protect(7, 3, "initial", 1),
                key_protect(7, 2, "replace", 1), key_protect(7, 2, "initial", 2)}
    assert key not in variants and len(variants) == 4


def test_keys_of_different_families_never_collide():
    keys = {
        key_open("m1", "BTCUSDT", PositionSide.LONG, BAR),
        key_signal_close("BTCUSDT", PositionSide.LONG, 1, BAR),
        key_op("op-1", "BTCUSDT", PositionSide.LONG),
        key_protect(1, 1, "initial", 1),
    }
    assert len(keys) == 4


def test_separator_inside_a_component_cannot_forge_another_key():
    # 操作号由客户端生成，可能带冒号；转义后不能和「多一段」的键相等
    assert key_op("a:b") != key_op("a", "b")
    assert key_op("a:b", "X") != key_op("a", "b:X")
    assert key_open("m:1", "A", "LONG", BAR) != key_open("m", "1:A", "LONG", BAR)


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_components_are_rejected(bad):
    with pytest.raises(ValueError):
        key_open(bad, "BTCUSDT", PositionSide.LONG, BAR)
    with pytest.raises(ValueError):
        key_open("m1", bad, PositionSide.LONG, BAR)
    with pytest.raises(ValueError):
        key_op(bad)
    with pytest.raises(ValueError):
        key_protect(1, 1, bad, 1)


def test_negative_numbers_are_rejected():
    with pytest.raises(ValueError):
        key_open("m1", "BTCUSDT", PositionSide.LONG, -1)
    with pytest.raises(ValueError):
        key_open("m1", "BTCUSDT", PositionSide.LONG, BAR, reverse_of_generation=-1)
    with pytest.raises(ValueError):
        key_open("m1", "BTCUSDT", PositionSide.LONG, BAR, injected_seq=-1)
    with pytest.raises(ValueError):
        key_signal_close("BTCUSDT", PositionSide.LONG, -1, BAR)
    with pytest.raises(ValueError):
        key_protect(1, -1, "initial", 1)


def test_side_must_be_a_position_side():
    with pytest.raises(ValueError):
        key_open("m1", "BTCUSDT", "BOTH", BAR)
    with pytest.raises(ValueError):
        key_signal_close("BTCUSDT", "BUY", 1, BAR)
    with pytest.raises(ValueError):
        key_op("op-1", "BTCUSDT", "BUY")
