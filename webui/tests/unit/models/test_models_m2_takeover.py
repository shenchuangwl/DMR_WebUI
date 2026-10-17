"""模型 2（接管手动订单）的框架：启动自检里本模型特有的项，以及四个按钮的含义。业务逻辑在 M3。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.types import Model
from dmr_kernel.models.base import BUTTONS, TradingModel
from dmr_kernel.models.m2_takeover import TakeoverModel

VALID = {"takeover_symbols": ["BTCUSDT", "ETHUSDT"], "takeover_min_notional": Decimal("5"),
         "takeover_max_notional": Decimal("200")}


def problems(**overrides) -> list[str]:
    ctx = {**VALID, **overrides}
    return TakeoverModel().start_problems({k: v for k, v in ctx.items() if v is not ...})


def test_it_is_model_two_and_a_trading_model():
    assert TakeoverModel.model is Model.M2 and isinstance(TakeoverModel(), TradingModel)


def test_no_problems_when_the_list_and_notional_bounds_are_configured():
    assert problems() == []


@pytest.mark.parametrize("value", [[], (), None, ...])
def test_empty_or_missing_takeover_list_blocks_start(value):
    found = problems(takeover_symbols=value)
    assert len(found) == 1 and "接管清单" in found[0]


def test_malformed_symbol_codes_block_start():
    found = problems(takeover_symbols=["btc usdt!"])
    assert len(found) == 1 and "接管清单" in found[0]


@pytest.mark.parametrize("value", [None, ...])
def test_missing_lower_bound_blocks_start(value):
    found = problems(takeover_min_notional=value)
    assert len(found) == 1 and "下限" in found[0]


@pytest.mark.parametrize("value", [None, ...])
def test_missing_upper_bound_blocks_start(value):
    found = problems(takeover_max_notional=value)
    assert len(found) == 1 and "上限" in found[0]


def test_lower_bound_must_be_below_upper_bound():
    found = problems(takeover_min_notional=Decimal("200"), takeover_max_notional=Decimal("200"))
    assert len(found) == 1 and "下限" in found[0] and "上限" in found[0]
    assert problems(takeover_min_notional=Decimal("300")) != []


def test_garbage_bounds_block_start():
    assert any("下限" in p for p in problems(takeover_min_notional="abc"))
    assert any("上限" in p for p in problems(takeover_max_notional=Decimal("-1")))


def test_string_numbers_are_accepted_like_the_config_store_does():
    assert problems(takeover_min_notional="5", takeover_max_notional="200") == []


def test_problems_accumulate_in_a_stable_order():
    found = TakeoverModel().start_problems({})
    assert len(found) == 3
    assert "接管清单" in found[0] and "下限" in found[1] and "上限" in found[2]


def test_values_can_come_from_config_get():
    config = dict(VALID)
    assert TakeoverModel().start_problems({"config_get": config.__getitem__}) == []
    assert len(TakeoverModel().start_problems({"config_get": {}.__getitem__})) == 3


def test_problems_are_plain_chinese_strings():
    for text in TakeoverModel().start_problems({}):
        assert isinstance(text, str) and any("一" <= ch <= "鿿" for ch in text)


def test_button_meanings_follow_report_4_10():
    meanings = TakeoverModel().button_meanings()
    assert tuple(meanings) == BUTTONS
    assert meanings == {
        "start": "开始自动接管清单内的手动仓",
        "stop": "不再接管新仓",
        "partial_close": "任何一个托管持仓",
        "close_all": "默认平接管仓",
    }


async def test_framework_hooks_do_nothing_yet():
    model = TakeoverModel()
    assert await model.on_start({}) is None and await model.on_stop({}) is None
