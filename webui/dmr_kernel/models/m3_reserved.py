"""模型 3（AI）的预留（5 层）：本期只预留结构、入口、配置。

启动一律拒绝，不产生任何意图，不碰意图、订单、协调者、登记簿、账本和交易所；
只提供配置当前值与可选项的查看（``describe_config``），供总览页和设置页展示。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from dmr_kernel.core.configdefs import DEFINITIONS, ConfigDef
from dmr_kernel.core.types import Model
from dmr_kernel.models.base import ModelNotStartable, TradingModel

__all__ = ["REFUSAL", "ReservedModel"]

REFUSAL = "模型 3 本期只预留结构、入口、配置；启动请求一律拒绝"

# 报告 4.12 的模型 3 配置：AI 档、执行档、形态、总闸、资金上限、持仓上限
CONFIG_KEYS = ("m3_ai_tier", "m3_exec_tier", "m3_form", "m3_master_switch", "m3_max_notional_usdt", "m3_max_symbols")


def _choices(definition: ConfigDef) -> list[Any]:
    if definition.kind == "choice":
        return list(definition.choices)
    if definition.kind == "bool":
        return [False, True]
    return []


class ReservedModel(TradingModel):
    model = Model.M3
    meanings = {
        "start": "本期不可用：模型 3 本期只预留，不能启动",
        "stop": "可点，本期没有可停的东西",
        "partial_close": "任何一个托管持仓",
        "close_all": "默认范围为空，可选全部托管持仓",
    }

    def start_problems(self, ctx: Mapping[str, Any]) -> list[str]:
        """恒为拒绝：不看上下文，别的条件全齐也不行。每次返回新列表。"""
        return [REFUSAL]

    async def on_start(self, ctx: Mapping[str, Any]) -> None:
        raise ModelNotStartable(REFUSAL)

    def describe_config(self, config_get: Callable[[str], Any]) -> dict[str, dict[str, Any]]:
        """模型 3 的六项配置：当前值、默认值、可选项（没有固定可选项的为空列表）、单位、改动是否需要部署级开关。

        只读这六项；配置库里没有的项（``KeyError``）按默认值显示。描述配置不会让模型 3 变得可以启动。
        """
        described: dict[str, dict[str, Any]] = {}
        for key in CONFIG_KEYS:
            definition = DEFINITIONS[key]
            try:
                value = config_get(key)
            except KeyError:
                value = definition.default
            described[key] = {
                "label": definition.label,
                "value": value,
                "default": definition.default,
                "choices": _choices(definition),
                "unit": definition.unit,
                "requires_deploy_switch": definition.requires_deploy_switch,
            }
        return described
