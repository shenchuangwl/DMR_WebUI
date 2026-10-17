"""模型 1 的少量持久状态（第 1 层；存在 ``meta`` 表里，不另建表）。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用。

- 出场判定进度（F11 第 2 步）：按（合约, 方向, 持仓代际）记「上次已判定的那根短周期 K 线的开盘时间」``judged_ms`` 和
  「待补判」的 K 线开盘时间列表 ``pending``（长周期取不到时记下的反向过零）。重启后接着判；``judged_ms`` 只前进不后退。
  持仓平掉后用 ``drop_exit_state`` 清掉。
- 出过平仓信号的那根（按合约）：``mark_exit_bar``/``last_exit_bar``。同一根 K 线（重复投递、重启后补跑）不再按自然信号开反向仓——
  反手只能走信号平仓那条路（混合仓、接管仓、闸已关、超时都只平不反）。
- 信号注入序号（替身环境的 ``/dev/inject_signal``）：只增不减，写进幂等键，使同一根 K 线可以注入多次。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy.orm import Session

from dmr_kernel.core.types import PositionSide
from dmr_kernel.ledger import repo_meta
from dmr_kernel.ledger.models import Meta

__all__ = ["ExitState", "drop_exit_state", "get_exit_state", "last_exit_bar", "mark_exit_bar", "next_inject_seq",
           "save_exit_state"]

INJECT_SEQ_KEY = "m1.inject_seq"
MAX_PENDING = 50


@dataclass(frozen=True)
class ExitState:
    judged_ms: int | None = None
    pending: tuple[int, ...] = ()


def _key(symbol: str, side: PositionSide | str, generation: int) -> str:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError(f"持仓代际必须是非负整数：{generation!r}")
    return f"m1.exit:{symbol}:{PositionSide(side).value}:{generation}"


def get_exit_state(session: Session, symbol: str, side: PositionSide | str, generation: int) -> ExitState:
    raw: Any = repo_meta.get_json(session, _key(symbol, side, generation))
    if not isinstance(raw, dict):
        return ExitState()
    judged = raw.get("judged_ms")
    pending = raw.get("pending") or ()
    return ExitState(judged if isinstance(judged, int) else None, tuple(int(x) for x in pending))


def save_exit_state(session: Session, symbol: str, side: PositionSide | str, generation: int, *,
                    judged_ms: int | None, pending: Iterable[int] = ()) -> ExitState:
    """写进度。``judged_ms`` 比库里已有的小就保留已有的（只前进）；``pending`` 整个替换，最多留最新的 ``MAX_PENDING`` 个。"""
    key = _key(symbol, side, generation)
    old = get_exit_state(session, symbol, side, generation)
    candidates = [x for x in (old.judged_ms, judged_ms) if x is not None]
    state = ExitState(max(candidates) if candidates else None, tuple(sorted(set(int(x) for x in pending)))[-MAX_PENDING:])
    repo_meta.put_json(session, key, {"judged_ms": state.judged_ms, "pending": list(state.pending)})
    return state


def drop_exit_state(session: Session, symbol: str, side: PositionSide | str, generation: int) -> None:
    row = session.get(Meta, _key(symbol, side, generation))
    if row is not None:
        session.delete(row)
        session.flush()


def _exit_bar_key(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol:
        raise ValueError(f"合约名必须是非空字符串：{symbol!r}")
    return f"m1.exit_bar:{symbol}"


def mark_exit_bar(session: Session, symbol: str, bar_open_ms: int) -> int:
    """记下该合约最近一次出平仓信号的那一轮（短周期 K 线开盘时间）；只前进不后退。返回记下的值。"""
    if isinstance(bar_open_ms, bool) or not isinstance(bar_open_ms, int) or bar_open_ms < 0:
        raise ValueError(f"K 线开盘时间必须是非负整数：{bar_open_ms!r}")
    old = last_exit_bar(session, symbol)
    value = bar_open_ms if old is None else max(old, bar_open_ms)
    repo_meta.put_json(session, _exit_bar_key(symbol), value)
    return value


def last_exit_bar(session: Session, symbol: str) -> int | None:
    raw: Any = repo_meta.get_json(session, _exit_bar_key(symbol))
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def next_inject_seq(session: Session) -> int:
    current = repo_meta.get_json(session, INJECT_SEQ_KEY, 0)
    value = (current if isinstance(current, int) else 0) + 1
    repo_meta.put_json(session, INJECT_SEQ_KEY, value)
    return value
