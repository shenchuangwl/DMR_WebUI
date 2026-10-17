"""账本的异常类型（第 1 层）。

上层模块不导入 sqlalchemy（docs/设计约定.md 第 5 节），所以需要捕获的数据库异常从这里取：
- ``IntegrityError``：唯一约束、部分唯一索引、外键、审计日志只追加触发器被违反。它就是 SQLAlchemy 的
  ``sqlalchemy.exc.IntegrityError``，``Ledger.write`` 原样抛给调用方，这里只是转出口。
"""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError

__all__ = ["IntegrityError", "LedgerClosed", "LedgerError"]


class LedgerError(RuntimeError):
    """账本的程序错误或无法继续的状态（迁移失败、写线程异常、备份校验失败等）。"""


class LedgerClosed(LedgerError):
    """账本已关闭，不再接受读写。"""
