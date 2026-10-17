"""models.base：TradingModel 抽象基类与上下文取值帮手。"""
from __future__ import annotations

import inspect
from decimal import Decimal
from typing import Any, Mapping

import pytest

from dmr_kernel.core.types import Model
from dmr_kernel.models.base import BUTTONS, ModelNotStartable, TradingModel, checked_config, ctx_value


class Dummy(TradingModel):
    model = Model.M1
    meanings = {"start": "开", "stop": "关", "partial_close": "部分", "close_all": "全平"}

    def start_problems(self, ctx: Mapping[str, Any]) -> list[str]:
        return []


def test_buttons_are_the_four_unified_operations():
    assert BUTTONS == ("start", "stop", "partial_close", "close_all")


def test_trading_model_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        TradingModel()  # type: ignore[abstract]


def test_start_problems_is_the_only_abstract_method():
    assert TradingModel.__abstractmethods__ == frozenset({"start_problems"})


def test_subclass_without_start_problems_is_still_abstract():
    class Incomplete(TradingModel):
        model = Model.M2

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_hooks_are_coroutines_and_default_to_noops():
    model = Dummy()
    assert inspect.iscoroutinefunction(model.on_start) and inspect.iscoroutinefunction(model.on_stop)


async def test_default_on_start_and_on_stop_do_nothing():
    model = Dummy()
    assert await model.on_start({}) is None
    assert await model.on_stop({}) is None


def test_button_meanings_returns_a_copy_with_the_four_buttons():
    model = Dummy()
    meanings = model.button_meanings()
    assert tuple(meanings) == BUTTONS and meanings["start"] == "开"
    meanings["start"] = "被改了"
    assert model.button_meanings()["start"] == "开"  # 改返回值不会影响模型自己的文字


def test_button_meanings_keeps_only_the_four_buttons_in_the_fixed_order():
    class Shuffled(TradingModel):
        model = Model.M2
        meanings = {"close_all": "全", "extra": "多余", "start": "开", "partial_close": "部分", "stop": "关"}

        def start_problems(self, ctx: Mapping[str, Any]) -> list[str]:
            return []

    assert list(Shuffled().button_meanings().items()) == [("start", "开"), ("stop", "关"), ("partial_close", "部分"),
                                                         ("close_all", "全")]


def test_button_meanings_refuses_an_incomplete_table():
    class Partial(TradingModel):
        model = Model.M2
        meanings = {"start": "开", "close_all": "全"}

        def start_problems(self, ctx: Mapping[str, Any]) -> list[str]:
            return []

    with pytest.raises(ValueError, match="stop.*partial_close"):
        Partial().button_meanings()


def test_button_meanings_refuses_a_model_that_defines_none():
    class Bare(TradingModel):
        model = Model.M2

        def start_problems(self, ctx: Mapping[str, Any]) -> list[str]:
            return []

    with pytest.raises(ValueError, match="Bare"):
        Bare().button_meanings()


def test_model_not_startable_is_a_runtime_error():
    assert issubclass(ModelNotStartable, RuntimeError)


# ---------------------------------------------------------------- ctx_value：上下文里直接给值，或经 config_get 读


def test_ctx_value_prefers_the_value_in_the_context():
    ctx = {"leverage": 3, "config_get": {"leverage": 9}.__getitem__}
    assert ctx_value(ctx, "leverage") == 3


def test_ctx_value_falls_back_to_config_get():
    ctx = {"config_get": {"leverage": 9}.__getitem__}
    assert ctx_value(ctx, "leverage") == 9


def test_ctx_value_is_none_when_nothing_provides_it():
    assert ctx_value({}, "leverage") is None
    assert ctx_value({"config_get": {}.__getitem__}, "leverage") is None  # KeyError 当作没配置
    assert ctx_value({"config_get": "not callable"}, "leverage") is None


def test_ctx_value_does_not_hide_other_config_errors():
    def broken(key: str) -> Any:
        raise RuntimeError("配置读取失败")

    with pytest.raises(RuntimeError):
        ctx_value({"config_get": broken}, "leverage")


def test_explicit_none_in_the_context_means_not_configured_even_if_config_get_has_it():
    ctx = {"leverage": None, "config_get": {"leverage": 9}.__getitem__}
    assert ctx_value(ctx, "leverage") is None


# ---------------------------------------------------------------- checked_config：取值并按配置定义校验


def test_checked_config_returns_the_validated_value_and_no_problem():
    assert checked_config({"leverage": 5}, "leverage") == (5, None)
    assert checked_config({"margin_mode": "cross"}, "margin_mode") == ("cross", None)


def test_checked_config_converts_values_the_way_the_config_store_does():
    assert checked_config({"leverage": "5"}, "leverage") == (5, None)
    assert checked_config({"takeover_min_notional": "5"}, "takeover_min_notional") == (Decimal("5"), None)
    assert checked_config({"takeover_symbols": "btcusdt, ETHUSDT"}, "takeover_symbols") == (["BTCUSDT", "ETHUSDT"], None)


def test_checked_config_keeps_a_valid_empty_value_distinct_from_a_problem():
    assert checked_config({"takeover_symbols": []}, "takeover_symbols") == ([], None)


def test_checked_config_reads_through_config_get():
    ctx = {"config_get": {"leverage": 7}.__getitem__}
    assert checked_config(ctx, "leverage") == (7, None)


@pytest.mark.parametrize("ctx", [{}, {"leverage": None}, {"config_get": {}.__getitem__},
                                 {"leverage": None, "config_get": {"leverage": 9}.__getitem__}],
                         ids=["没有", "显式空", "配置库没有", "显式空盖过配置库"])
def test_checked_config_says_not_configured_with_the_label(ctx):
    assert checked_config(ctx, "leverage") == (None, "杠杆还没有配置")


@pytest.mark.parametrize("value,fragment", [(0, "不能小于"), (21, "不能大于 20"), ("abc", "整数"), (True, "整数")])
def test_checked_config_says_invalid_with_label_and_reason(value, fragment):
    checked, problem = checked_config({"leverage": value}, "leverage")
    assert checked is None and problem.startswith("杠杆无效：") and fragment in problem


def test_checked_config_treats_nan_as_invalid_instead_of_crashing():
    checked, problem = checked_config({"takeover_max_notional": Decimal("NaN")}, "takeover_max_notional")
    assert checked is None and "上限" in problem and "无效" in problem


def test_checked_config_does_not_modify_the_context():
    ctx = {"leverage": "5", "config_get": {}.__getitem__}
    snapshot = dict(ctx)
    checked_config(ctx, "leverage")
    assert ctx == snapshot


def test_checked_config_rejects_unknown_keys_as_a_program_error():
    with pytest.raises(KeyError):
        checked_config({}, "no_such_key")


def test_checked_config_does_not_hide_other_config_errors():
    def broken(key: str) -> Any:
        raise RuntimeError("配置读取失败")

    with pytest.raises(RuntimeError):
        checked_config({"config_get": broken}, "leverage")
