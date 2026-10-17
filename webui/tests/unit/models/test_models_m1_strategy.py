"""模型 1（完全自动化量化交易）的框架：启动自检里本模型特有的项，以及四个按钮的含义。业务逻辑在 M4。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import Model
from dmr_kernel.models.base import BUTTONS, TradingModel
from dmr_kernel.models.m1_strategy import StrategyModel

VALID = {"list_valid": True, "leverage": 2, "margin_mode": "isolated"}


def problems(**overrides) -> list[str]:
    ctx = {**VALID, **overrides}
    return StrategyModel().start_problems({k: v for k, v in ctx.items() if v is not ...})


def test_it_is_model_one_and_a_trading_model():
    assert StrategyModel.model is Model.M1 and isinstance(StrategyModel(), TradingModel)


def test_no_problems_when_the_list_is_valid_and_leverage_and_margin_mode_are_set():
    assert problems() == []


def test_a_valid_but_empty_list_passes():
    # F7：名单有效但为空也算通过
    assert StrategyModel().start_problems({**VALID, "list_member_count": 0}) == []


@pytest.mark.parametrize("value", [False, None, 0])
def test_invalid_list_blocks_start(value):
    found = problems(list_valid=value)
    assert len(found) == 1 and "名单" in found[0]


def test_missing_list_status_blocks_start():
    found = problems(list_valid=...)  # 上下文里没有这个键
    assert len(found) == 1 and "名单" in found[0]


@pytest.mark.parametrize("value", [None, ..., 0, 21, "abc", True])
def test_unconfigured_or_invalid_leverage_blocks_start(value):
    found = problems(leverage=value)
    assert len(found) == 1 and "杠杆" in found[0]


@pytest.mark.parametrize("value", [None, ..., "", "weird", 1])
def test_unconfigured_or_invalid_margin_mode_blocks_start(value):
    found = problems(margin_mode=value)
    assert len(found) == 1 and "保证金模式" in found[0]


@pytest.mark.parametrize("leverage,margin_mode", [(1, "cross"), (20, "isolated"), ("5", "cross")])
def test_every_legal_leverage_and_margin_mode_passes(leverage, margin_mode):
    assert problems(leverage=leverage, margin_mode=margin_mode) == []


def test_problems_accumulate_in_a_stable_order():
    found = StrategyModel().start_problems({})
    assert len(found) == 3
    assert "名单" in found[0] and "杠杆" in found[1] and "保证金模式" in found[2]


def test_leverage_and_margin_mode_can_come_from_config_get():
    config = {"leverage": 2, "margin_mode": "cross"}
    assert StrategyModel().start_problems({"list_valid": True, "config_get": config.__getitem__}) == []
    found = StrategyModel().start_problems({"list_valid": True, "config_get": {}.__getitem__})
    assert len(found) == 2


def test_problems_are_plain_chinese_strings():
    for text in StrategyModel().start_problems({}):
        assert isinstance(text, str) and any("一" <= ch <= "鿿" for ch in text)


def test_start_problems_does_not_modify_the_context():
    ctx = {"list_valid": False}
    StrategyModel().start_problems(ctx)
    assert ctx == {"list_valid": False}


def test_button_meanings_follow_report_4_10():
    meanings = StrategyModel().button_meanings()
    assert tuple(meanings) == BUTTONS
    assert meanings == {
        "start": "打开开仓闸，按信号自动开仓",
        "stop": "关上开仓闸，撤掉未成交的开仓单",
        "partial_close": "任何一个托管持仓",
        "close_all": "默认平策略仓",
    }


async def test_framework_hooks_do_nothing_yet():
    model = StrategyModel()
    assert await model.on_start({}) is None and await model.on_stop({}) is None
