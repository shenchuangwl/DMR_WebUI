"""三个模型放在一起看：编号、按钮键、文字、启动自检的返回形态是一致的。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.types import Model
from dmr_kernel.models.base import BUTTONS, TradingModel
from dmr_kernel.models.m1_strategy import StrategyModel
from dmr_kernel.models.m2_takeover import TakeoverModel
from dmr_kernel.models.m3_reserved import ReservedModel

ALL = [StrategyModel, TakeoverModel, ReservedModel]


def has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def test_each_model_class_has_its_own_model_code():
    assert [cls.model for cls in ALL] == [Model.M1, Model.M2, Model.M3]


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_every_model_defines_the_same_four_buttons_in_chinese(cls):
    meanings = cls().button_meanings()
    assert tuple(meanings) == BUTTONS
    assert all(isinstance(text, str) and has_cjk(text) for text in meanings.values())


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_start_problems_returns_a_list_of_strings_and_leaves_the_context_alone(cls):
    ctx = {"list_valid": False, "leverage": None}
    problems = cls().start_problems(ctx)
    assert isinstance(problems, list) and all(isinstance(p, str) for p in problems)
    assert ctx == {"list_valid": False, "leverage": None}


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_models_are_trading_models_with_async_hooks(cls):
    import inspect

    model = cls()
    assert isinstance(model, TradingModel)
    assert inspect.iscoroutinefunction(model.on_start) and inspect.iscoroutinefunction(model.on_stop)


def test_only_model_three_refuses_with_an_empty_requirement_set():
    # 模型 1、2 的问题来自缺失的条件；模型 3 的问题与条件无关
    assert StrategyModel().start_problems({"list_valid": True, "leverage": 2, "margin_mode": "cross"}) == []
    assert ReservedModel().start_problems({"list_valid": True, "leverage": 2, "margin_mode": "cross"}) != []
