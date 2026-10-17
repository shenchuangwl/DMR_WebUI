"""meta 表的仓储函数：账本身份（``ledger_nonce``、``created_ms``）与少量键值。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录或基本类型。

- ``ledger_nonce``：建库时生成的 2 位小写 36 进制随机串，是订单编号里的实例部分；``created_ms`` 是建库时间。
  两者只由 ``ensure_identity`` 写入一次，之后 ``put``/``put_json`` 一律拒绝改写。
- ``put_json``/``get_json``：值按账本统一的 JSON 写法存（Decimal 存成字符串，取回来也是字符串）。
"""
from __future__ import annotations

import re
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dmr_kernel.ledger.coltypes import json_dumps, json_loads
from dmr_kernel.ledger.errors import LedgerError
from dmr_kernel.ledger.models import Meta
from dmr_kernel.ledger.records import MetaRecord

NONCE_KEY = "ledger_nonce"
CREATED_KEY = "created_ms"
IDENTITY_KEYS = frozenset({NONCE_KEY, CREATED_KEY})
NONCE_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
NONCE_LENGTH = 2
_NONCE_RE = re.compile(f"[0-9a-z]{{{NONCE_LENGTH}}}")


def _check_writable(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise TypeError(f"meta 的键必须是非空字符串：{key!r}")
    if key in IDENTITY_KEYS:
        raise ValueError(f"{key} 是账本身份，建库时写入一次，之后不能改")


def get(session: Session, key: str) -> str | None:
    row = session.get(Meta, key)
    return None if row is None else row.value


def put(session: Session, key: str, value: str) -> None:
    """写入或覆盖一个键（值是字符串）。身份键拒绝。"""
    _check_writable(key)
    if not isinstance(value, str):
        raise TypeError(f"meta 的值必须是字符串（结构化的值用 put_json）：{type(value).__name__}")
    row = session.get(Meta, key)
    if row is None:
        session.add(Meta(key=key, value=value))
    else:
        row.value = value
    session.flush()


def items(session: Session) -> list[MetaRecord]:
    return [MetaRecord.from_row(row) for row in session.scalars(select(Meta).order_by(Meta.key))]


def get_json(session: Session, key: str, default: Any = None) -> Any:
    value = get(session, key)
    return default if value is None else json_loads(value)


def put_json(session: Session, key: str, value: Any) -> None:
    _check_writable(key)
    put(session, key, json_dumps(value))


def nonce(session: Session) -> str:
    value = get(session, NONCE_KEY)
    if value is None:
        raise LedgerError("账本还没有身份（meta 里没有 ledger_nonce）")
    return value


def ensure_identity(session: Session, *, now_ms: int) -> str:
    """没有身份就生成（随机串与建库时间），已有就原样保留。返回 ``ledger_nonce``。"""
    row = session.get(Meta, NONCE_KEY)
    if row is None:
        value = "".join(secrets.choice(NONCE_ALPHABET) for _ in range(NONCE_LENGTH))
        session.add(Meta(key=NONCE_KEY, value=value))
    else:
        value = row.value
        if not _NONCE_RE.fullmatch(value):
            raise LedgerError(f"账本身份损坏：ledger_nonce = {value!r}")
    if session.get(Meta, CREATED_KEY) is None:
        session.add(Meta(key=CREATED_KEY, value=str(int(now_ms))))
    session.flush()
    return value
