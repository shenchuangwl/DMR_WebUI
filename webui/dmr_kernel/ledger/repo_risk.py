"""风控的仓储函数（第 1 层，账本）：risk_state，以及模型级判定要用的当日汇总。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回基本类型或记录。

- ``risk_state`` 是 meta 表的一个键，值是 JSON（Decimal 存成字符串）。内容由 ``core.riskgate`` 定义和解释，这里只管读写；
  读到的不是合法 JSON 时报 ValueError（风控状态损坏不能悄悄当成「没有」）。
- ``save_state_with_audit``：人工复位这类操作要让「状态落盘」与「审计留痕」在同一个事务里，要么都有要么都没有。
- ``model_day_stats``：模型 1 的模型级判定的输入。只看策略仓（归属为 strategy）：接管仓、模型 3 的仓、待确认仓与外部仓
  都不计入。当日净盈亏 = 平仓时间落在 [day_start_ms, day_end_ms) 的策略仓交易的净盈亏之和（已平交易，不含浮动盈亏）；
  当日开仓次数 = 开仓时间落在该区间的策略仓记录数（含还没平的；开仓时间未知时用记录的创建时间）。
  净盈亏的求和在 Python 里做（Decimal 列存成文本，不能在 SQL 里求和）。
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dmr_kernel.core.types import Ownership
from dmr_kernel.ledger import repo_audit, repo_meta
from dmr_kernel.ledger.coltypes import json_loads
from dmr_kernel.ledger.models import Position, Trade
from dmr_kernel.ledger.records import AuditLogRecord

RISK_STATE_KEY = "risk_state"


def load_state(session: Session) -> Any:
    """meta 里的 risk_state（解析后的 JSON）；还没有就返回 None。"""
    text = repo_meta.get(session, RISK_STATE_KEY)
    if text is None:
        return None
    try:
        return json_loads(text)
    except ValueError as exc:
        raise ValueError(f"meta 里的 {RISK_STATE_KEY} 不是合法的 JSON：{exc}") from exc


def save_state(session: Session, state: Mapping[str, Any]) -> None:
    """覆盖写 risk_state。"""
    if not isinstance(state, Mapping):
        raise TypeError(f"{RISK_STATE_KEY} 必须是字典：{type(state).__name__}")
    repo_meta.put_json(session, RISK_STATE_KEY, dict(state))


def save_state_with_audit(session: Session, state: Mapping[str, Any], *, at_ms: int, operator: str, action: str,
                          target: str = "risk", before: Any = None, after: Any = None,
                          result: str = "ok") -> AuditLogRecord:
    """同一个事务里写 risk_state 与一条审计记录；任何一步失败，整个事务回滚。"""
    save_state(session, state)
    return repo_audit.append(session, at_ms=at_ms, operator=operator, action=action, target=target, before=before,
                             after=after, result=result)


def model_day_stats(session: Session, *, day_start_ms: int, day_end_ms: int) -> dict[str, Any]:
    """模型级判定的输入（见模块说明）：``strategy_net_pnl``（Decimal）、``strategy_closed_trades``、``strategy_opens``。"""
    for name, value in (("day_start_ms", day_start_ms), ("day_end_ms", day_end_ms)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} 必须是整数毫秒：{value!r}")
    if day_end_ms <= day_start_ms:
        raise ValueError(f"区间必须非空：[{day_start_ms}, {day_end_ms})")
    pnls = list(session.scalars(
        select(Trade.net_pnl).where(Trade.ownership == Ownership.STRATEGY, Trade.close_ms >= day_start_ms,
                                    Trade.close_ms < day_end_ms)))
    opened_ms = func.coalesce(Position.open_time_ms, Position.created_ms)
    opens = session.scalar(
        select(func.count()).select_from(Position).where(Position.ownership == Ownership.STRATEGY,
                                                         opened_ms >= day_start_ms, opened_ms < day_end_ms))
    return {"strategy_net_pnl": sum(pnls, Decimal(0)), "strategy_closed_trades": len(pnls),
            "strategy_opens": int(opens or 0)}
