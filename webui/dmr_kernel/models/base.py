"""三个交易模型的共同基类（4.5 层，只给 ``models.*`` 用）。

框架只要求每个模型回答两件事：启动自检里本模型特有的项（``start_problems``），以及报告 4.10 里四个统一按钮在本模型下的
含义（``button_meanings``）；``on_start``/``on_stop`` 是启停钩子，本波什么都不做，业务逻辑在 M3（模型 2）、M4（模型 1）。
这里的方法都是纯计算：不读库、不访问交易所、不产生意图。启动自检里各模型通用的项（对冲模式、对账、止损参数……）由上层的启动流程负责。

启动上下文 ``ctx`` 是只读字典，由上层在启动前组好：
- ``list_valid``：当日名单是否有效（有效但为空也算有效）；
- ``leverage``、``margin_mode``、``takeover_symbols``、``takeover_min_notional``、``takeover_max_notional``：配置现值。
  可以直接放进 ``ctx``（放了 ``None`` 表示明确没配置），没放的经 ``ctx["config_get"]``（``key -> value``，没有这项抛 ``KeyError``）去读，
  见 ``ctx_value``；``checked_config`` 在此之上再按配置定义校验取值。
"""
from __future__ import annotations

import abc
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from dmr_kernel.core import configdefs
from dmr_kernel.core.types import Model

__all__ = ["BUTTONS", "ModelNotStartable", "TradingModel", "checked_config", "ctx_value"]

# 报告 4.10：三个模型下位置、名称、流程一致的四个统一操作（键顺序固定）
BUTTONS = ("start", "stop", "partial_close", "close_all")


class ModelNotStartable(RuntimeError):
    """这个模型现在不允许启动（模型 3 本期只预留）。"""


def ctx_value(ctx: Mapping[str, Any], key: str) -> Any:
    """取启动自检需要的某项配置现值。

    上下文里直接给了就用它（哪怕是 ``None``：表示明确没配置，不再去问配置库）；没给就经 ``ctx["config_get"]`` 读。
    读不到（``KeyError``）或没有可调用的 ``config_get`` 都当作没配置，返回 ``None``；其它异常照常抛出，不掩盖配置库的故障。
    """
    if key in ctx:
        return ctx[key]
    getter = ctx.get("config_get")
    if not callable(getter):
        return None
    try:
        return getter(key)
    except KeyError:
        return None


def checked_config(ctx: Mapping[str, Any], key: str) -> tuple[Any, str | None]:
    """取一项配置现值并按 ``core.configdefs`` 的定义校验（与配置库同一套取值范围与转换）。

    返回 ``(校验后的值, None)``；没配置或不合法返回 ``(None, 中文原因)``，原因以配置项的名称开头。
    校验后的值可能是空列表（接管清单为空）：判断有没有问题看第二项，不要看第一项的真假。``key`` 不是已定义的配置项属于程序错误。
    """
    label = configdefs.DEFINITIONS[key].label
    value = ctx_value(ctx, key)
    if value is None:
        return None, f"{label}还没有配置"
    try:
        return configdefs.validate_value(key, value), None
    except configdefs.ConfigError as exc:
        return None, f"{label}无效：{exc}"
    except ArithmeticError:  # Decimal("NaN") 之类在比较大小时抛出
        return None, f"{label}无效：不是有效的数值"


class TradingModel(abc.ABC):
    """交易模型的抽象基类。子类设置 ``model`` 与 ``meanings``，并实现 ``start_problems``。"""

    model: ClassVar[Model]
    # 四个按钮在本模型下的含义（一句话，键为 BUTTONS）
    meanings: ClassVar[Mapping[str, str]] = MappingProxyType({})

    @abc.abstractmethod
    def start_problems(self, ctx: Mapping[str, Any]) -> list[str]:
        """F7 启动自检里本模型特有的项：返回问题列表（中文），空表示通过。不修改 ``ctx``。"""

    def button_meanings(self) -> dict[str, str]:
        """报告 4.10 的四个按钮在本模型下的含义。返回副本，键顺序固定为 ``BUTTONS``。"""
        missing = [key for key in BUTTONS if key not in self.meanings]
        if missing:
            raise ValueError(f"{type(self).__name__} 没有写明这些按钮的含义：{missing}")
        return {key: self.meanings[key] for key in BUTTONS}

    async def on_start(self, ctx: Mapping[str, Any]) -> None:
        """开仓闸打开之后的钩子。框架阶段什么都不做。"""

    async def on_stop(self, ctx: Mapping[str, Any]) -> None:
        """开仓闸关闭之后的钩子。框架阶段什么都不做。"""
