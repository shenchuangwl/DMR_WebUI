"""账本的列类型（第 1 层）：Decimal、JSON、枚举一律存 TEXT（docs/设计约定.md 第 6、8 节）。

- ``DecimalText``：Decimal ↔ TEXT。存 ``str(Decimal)``，取回 ``Decimal(文本)``，全程不经 float，连写法都原样保留。
  只接受 Decimal 与 int；float、bool、str 以及 NaN、无穷都是程序错误。列按 TEXT 比较：不要在 SQL 里对这类列
  做大小比较、数值排序或求和，取出来以后在 Python 里算。
- ``JSONText``：Python 对象 ↔ JSON 文本（中文原样，不转义）。JSON 里的 Decimal 存成字符串（与接口约定一致），
  NaN 与无穷拒绝。None 存 SQL NULL。ORM 不跟踪对列表、字典的原地修改：改 JSON 列要整体重新赋值。
- ``EnumText(枚举类)``：存枚举的值（core.types 的取值），取回枚举成员。写入未知取值、或别的枚举的成员，都报错。
"""
from __future__ import annotations

import json
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy.engine import Dialect
from sqlalchemy.types import Text, TypeDecorator


def json_dumps(value: Any) -> str:
    """账本统一的 JSON 写法：紧凑、中文原样、Decimal 转字符串、拒绝 NaN 与无穷。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=_json_default)


def json_loads(text: str) -> Any:
    return json.loads(text)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError(f"JSON 里不能存非有限的 Decimal：{obj!r}")
        return str(obj)
    raise TypeError(f"无法存成 JSON：{type(obj).__name__}")


def to_decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"Decimal 列只接受 Decimal 或 int，收到 {type(value).__name__}：{value!r}")
    if isinstance(value, int):
        value = Decimal(value)
    if not value.is_finite():
        raise ValueError(f"Decimal 列不接受非有限值：{value!r}")
    return str(value)


class DecimalText(TypeDecorator):
    impl = Text
    cache_ok = True

    @property
    def python_type(self) -> type:
        return Decimal

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        return to_decimal_text(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError(f"Decimal 列里出现了浮点数 {value!r}：只能存文本")
        return Decimal(value)


class JSONText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        return None if value is None else json_dumps(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        return None if value is None else json_loads(value)


class EnumText(TypeDecorator):
    impl = Text
    cache_ok = True

    def __init__(self, enum_type: type[Enum]):
        super().__init__()
        self.enum_type = enum_type

    @property
    def python_type(self) -> type:
        return self.enum_type

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, Enum) and not isinstance(value, self.enum_type):
            raise TypeError(f"{self.enum_type.__name__} 列不接受 {type(value).__name__}.{value.name}")
        return self.enum_type(value).value

    def process_result_value(self, value: Any, dialect: Dialect) -> Enum | None:
        return None if value is None else self.enum_type(value)

    def __repr__(self) -> str:
        return f"EnumText({self.enum_type.__name__})"
