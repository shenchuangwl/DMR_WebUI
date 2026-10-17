"""模型 3（AI，本期只预留结构、入口、配置）：启动一律拒绝，不产生任何意图，配置的当前值与可选项可查看。"""
from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from dmr_kernel.core import configdefs
from dmr_kernel.core.types import Model
from dmr_kernel.models.base import BUTTONS, ModelNotStartable, TradingModel
from dmr_kernel.models.m3_reserved import REFUSAL, ReservedModel

SOURCE = Path(__file__).resolve().parents[3] / "dmr_kernel" / "models" / "m3_reserved.py"

# 就算别的模型要求的东西全都齐了，模型 3 也不能启动
PERFECT_CTX = {"list_valid": True, "leverage": 2, "margin_mode": "isolated", "takeover_symbols": ["BTCUSDT"],
               "takeover_min_notional": Decimal("5"), "takeover_max_notional": Decimal("200")}


def test_it_is_model_three_and_a_trading_model():
    assert ReservedModel.model is Model.M3 and isinstance(ReservedModel(), TradingModel)


# ---------------------------------------------------------------- 启动一律拒绝


@pytest.mark.parametrize("ctx", [{}, PERFECT_CTX, {"list_valid": True}, {"anything": object()}],
                         ids=["空", "条件全齐", "只有名单", "无关内容"])
def test_start_problems_is_constant_whatever_the_context(ctx):
    assert ReservedModel().start_problems(ctx) == ["模型 3 本期只预留结构、入口、配置；启动请求一律拒绝"]


def test_refusal_text_is_exposed_for_the_kernel_to_reuse():
    assert REFUSAL == "模型 3 本期只预留结构、入口、配置；启动请求一律拒绝"


def test_each_call_returns_a_fresh_list():
    first = ReservedModel().start_problems({})
    first.append("被改了")
    assert ReservedModel().start_problems({}) == [REFUSAL]


async def test_on_start_refuses_even_if_called_directly():
    with pytest.raises(ModelNotStartable, match="模型 3 本期只预留"):
        await ReservedModel().on_start(PERFECT_CTX)


async def test_on_stop_is_harmless():
    assert await ReservedModel().on_stop({}) is None


def test_button_meanings_follow_report_4_10():
    meanings = ReservedModel().button_meanings()
    assert tuple(meanings) == BUTTONS
    assert "本期不可用" in meanings["start"] and "只预留" in meanings["start"]  # 注明原因
    assert meanings["stop"] == "可点，本期没有可停的东西"
    assert meanings["partial_close"] == "任何一个托管持仓"
    assert meanings["close_all"] == "默认范围为空，可选全部托管持仓"


# ---------------------------------------------------------------- 配置的当前值与可选项


KEYS = ["m3_ai_tier", "m3_exec_tier", "m3_form", "m3_master_switch", "m3_max_notional_usdt", "m3_max_symbols"]


def defaults(key: str):
    return configdefs.DEFINITIONS[key].default


def test_describe_config_covers_tiers_form_master_switch_and_limits():
    described = ReservedModel().describe_config(defaults)
    assert list(described) == KEYS


def test_describe_config_defaults_are_all_off():
    described = ReservedModel().describe_config(defaults)
    assert described["m3_ai_tier"]["value"] == "off"
    assert described["m3_exec_tier"]["value"] == "off"
    assert described["m3_master_switch"]["value"] is False
    assert described["m3_max_notional_usdt"]["value"] == Decimal("0")
    assert described["m3_max_symbols"]["value"] == 0
    assert described["m3_form"]["value"] == "strategy_exec_ai_veto"


def test_describe_config_lists_the_choices_from_the_definitions():
    described = ReservedModel().describe_config(defaults)
    assert described["m3_ai_tier"]["choices"] == ["off", "shadow", "advise", "veto"]  # AI 档
    assert described["m3_exec_tier"]["choices"] == ["off", "shadow", "paper", "testnet", "live_manual", "live_auto"]
    assert described["m3_form"]["choices"] == ["strategy_exec_ai_veto", "ai_proposal_sealed", "ai_autonomous"]
    assert described["m3_master_switch"]["choices"] == [False, True]
    assert described["m3_max_notional_usdt"]["choices"] == [] and described["m3_max_symbols"]["choices"] == []


def test_describe_config_carries_labels_and_the_deploy_switch_requirement():
    described = ReservedModel().describe_config(defaults)
    for key, item in described.items():
        definition = configdefs.DEFINITIONS[key]
        assert item["label"] == definition.label
        assert item["default"] == definition.default
        assert item["requires_deploy_switch"] is True  # 改任何一档都需要部署级开关，设置页改不了
        assert item["unit"] == definition.unit


def test_describe_config_reads_the_current_values_through_config_get():
    current = {"m3_ai_tier": "shadow", "m3_exec_tier": "paper", "m3_master_switch": True,
               "m3_max_notional_usdt": Decimal("100"), "m3_max_symbols": 3, "m3_form": "ai_proposal_sealed"}
    asked: list[str] = []

    def config_get(key: str):
        asked.append(key)
        return current[key]

    described = ReservedModel().describe_config(config_get)
    assert {key: item["value"] for key, item in described.items()} == current
    assert sorted(asked) == sorted(KEYS)  # 只读这六项


def test_describe_config_falls_back_to_the_default_for_keys_the_store_lacks():
    described = ReservedModel().describe_config({"m3_ai_tier": "veto"}.__getitem__)
    assert described["m3_ai_tier"]["value"] == "veto"
    assert described["m3_exec_tier"]["value"] == "off" and described["m3_max_symbols"]["value"] == 0


def test_describe_config_does_not_start_anything():
    # 描述配置不会改变“不能启动”这件事，即便配置里已经是最激进的档位
    model = ReservedModel()
    model.describe_config(lambda key: {"m3_exec_tier": "live_auto", "m3_ai_tier": "veto", "m3_master_switch": True,
                                       "m3_max_notional_usdt": Decimal("1000"), "m3_max_symbols": 30,
                                       "m3_form": "ai_autonomous"}[key])
    assert model.start_problems(PERFECT_CTX) == [REFUSAL]


# ---------------------------------------------------------------- 不产生任何意图


def imported_modules(path: Path) -> list[str]:
    """源码里 import 语句涉及的模块名（from X import Y 记 X；``import X`` 记 X）。"""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append("." * node.level + (node.module or ""))
    return found


def test_module_never_touches_intents_orders_or_the_coordinator():
    for name in imported_modules(SOURCE):
        parts = name.split(".")
        for banned in ("intents", "orders", "coordinator", "registry", "exchange", "ledger", "ingest", "riskgate",
                       "limits", "protection", "reconcile"):
            assert banned not in parts, name


def test_module_only_depends_on_base_types_and_config_definitions():
    kernel = {name for name in imported_modules(SOURCE) if name.startswith(".") or name.startswith("dmr_kernel")}
    assert kernel <= {"dmr_kernel.core.configdefs", "dmr_kernel.core.types", "dmr_kernel.models.base"}, kernel


def test_module_has_no_method_that_could_create_an_intent():
    public = {name for name in dir(ReservedModel) if not name.startswith("_")}
    assert public == {"model", "meanings", "start_problems", "button_meanings", "on_start", "on_stop",
                      "describe_config"}
