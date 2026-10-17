"""配置读写（第 2 层）：把 ``core.configdefs`` 的定义落到账本的 ``config_item`` 表。

- ``load``：读账本；缺的键写入本环境的初始值（「待你定」的项存空值）；账本里已有但定义里没有的键忽略；
  已有的值不合法（被人手改坏）就拒绝加载，说明是哪一项——宁可不启动，也不带着错误的风控参数运行。
- ``get`` / ``snapshot``：同步取缓存（给其它模块当 ``config_get`` 用）；取到的都是副本。
- ``set``：校验取值 → 需要部署级开关的项在开关没开时拒绝 → 用「改后的整份配置」做跨字段校验 → 同一事务写
  ``config_item`` 与 ``audit_log`` → 缓存更新 → 发布 ``ConfigChanged``。校验失败抛 ``configdefs.ConfigError``（中文原因），
  什么都不改。改动串行执行，所以两个各自合法、合起来冲突的修改同时到达时，后到的会按新状态重新校验。
- ``describe``：给界面和接口用的每项说明（含当前值、默认值、范围、生效方式的中文说明）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from dmr_kernel.core import configdefs
from dmr_kernel.core.clock import Clock
from dmr_kernel.core.configdefs import APPLY_MODES, ConfigError
from dmr_kernel.core.events import ConfigChanged, EventBus
from dmr_kernel.core.types import Environment
from dmr_kernel.ledger import repo_config
from dmr_kernel.ledger.writer import Ledger

log = logging.getLogger(__name__)

__all__ = ["ConfigChange", "ConfigError", "ConfigStore"]


@dataclass(frozen=True)
class ConfigChange:
    key: str
    old: Any
    new: Any
    applies: str  # APPLY_MODES 里的中文说明
    affects_existing: bool


def _copy(value: Any) -> Any:
    """列表（接管清单）给出副本；其余值（数、字符串、布尔、Decimal）本身不可变。"""
    return list(value) if isinstance(value, list) else value


class ConfigStore:
    def __init__(self, ledger: Ledger, bus: EventBus, clock: Clock, environment: Environment, *,
                 deploy_switch: bool):
        self._ledger = ledger
        self._bus = bus
        self._clock = clock
        self._environment = environment
        self._deploy_switch = bool(deploy_switch)
        self._values: dict[str, Any] = {}
        self._loaded = False
        self._lock = asyncio.Lock()  # 改动串行：跨字段校验必须基于最新状态

    # ------------------------------------------------------------ 读

    async def load(self) -> None:
        """读账本，补上缺的键，解码成带类型的值放进缓存。"""
        async with self._lock:
            initial = configdefs.initial_values(self._environment)
            added = await self._ledger.write(repo_config.init_missing, initial, at_ms=self._clock.now_ms())
            if added:
                log.info("配置项首次写入初始值：%d 项（环境 %s）", len(added), self._environment.value)
            values: dict[str, Any] = {}
            for record in await self._ledger.read(repo_config.load_all):
                defn = configdefs.DEFINITIONS.get(record.key)
                if defn is None:
                    log.warning("账本里有未定义的配置项 %s，已忽略（行原样保留）", record.key)
                    continue
                values[record.key] = self._decode(defn, record.value)
            self._values = values
            self._loaded = True

    @staticmethod
    def _decode(defn: configdefs.ConfigDef, stored: Any) -> Any:
        if stored is None:
            if defn.default is None:  # 「待你定」的项：还没定
                return None
            raise ConfigError(f"账本里的配置项 {defn.key}（{defn.label}）为空，但它必须有值；请修正账本后再启动")
        try:
            return configdefs.validate_value(defn.key, stored)
        except ConfigError as exc:
            raise ConfigError(f"账本里的配置项 {defn.key}（{defn.label}）取值不合法：{exc}；请修正账本后再启动") from exc

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("配置尚未加载：先调用 load()")

    def get(self, key: str) -> Any:
        """同步取当前值。未知键抛 KeyError；「待你定」且还没定的项是 None。"""
        if key not in configdefs.DEFINITIONS:
            raise KeyError(key)
        self._require_loaded()
        return _copy(self._values[key])

    def snapshot(self) -> dict[str, Any]:
        self._require_loaded()
        return {key: _copy(value) for key, value in self._values.items()}

    def missing_for_live(self) -> list[str]:
        self._require_loaded()
        return configdefs.missing_for_live(self._values)

    def describe(self) -> list[dict]:
        """每项：key group label value default min max choices unit apply(中文) affects_existing
        required_for_live requires_deploy_switch note。default 是本环境的初始值。"""
        self._require_loaded()
        return [
            {
                "key": key,
                "group": defn.group,
                "label": defn.label,
                "value": _copy(self._values[key]),
                "default": _copy(defn.initial(self._environment)),
                "min": defn.min,
                "max": defn.max,
                "choices": list(defn.choices),
                "unit": defn.unit,
                "apply": APPLY_MODES[defn.apply],
                "affects_existing": defn.affects_existing,
                "required_for_live": defn.required_for_live,
                "requires_deploy_switch": defn.requires_deploy_switch,
                "note": defn.note,
            }
            for key, defn in configdefs.DEFINITIONS.items()
        ]

    # ------------------------------------------------------------ 写

    async def set(self, key: str, value: Any, *, operator: str) -> ConfigChange:
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator 不能为空：配置改动必须留下是谁改的")
        self._require_loaded()
        async with self._lock:
            new = configdefs.validate_value(key, value)  # 未知键、类型、范围、可选项
            defn = configdefs.DEFINITIONS[key]
            if defn.requires_deploy_switch and not self._deploy_switch:
                raise ConfigError(f"{defn.label} 需要部署级开关（环境变量 DMR_MODEL3_DEPLOY_SWITCH=1）才能修改，当前未开启")
            old = self._values[key]
            change = ConfigChange(key=key, old=_copy(old), new=_copy(new), applies=APPLY_MODES[defn.apply],
                                  affects_existing=defn.affects_existing)
            if new == old:
                return change  # 没有变化：不写账本、不留审计、不发事件
            problems = configdefs.cross_field_problems({**self._values, key: new})
            if problems:
                raise ConfigError(f"{defn.label} 的新值与其它配置冲突：{'；'.join(problems)}")
            # 提交一旦开始就做完（即使调用方被取消）：账本、缓存、事件三者不能只做一半
            await asyncio.shield(self._commit(key, new, operator, old))
            return change

    async def _commit(self, key: str, new: Any, operator: str, old: Any) -> None:
        now = self._clock.now_ms()
        await self._ledger.write(repo_config.set_item, key, new, operator=operator, at_ms=now)
        self._values[key] = new
        log.info("配置项 %s 已由 %s 修改：%r -> %r", key, operator, old, new)
        self._bus.publish(ConfigChanged(key=key, at_ms=now))
