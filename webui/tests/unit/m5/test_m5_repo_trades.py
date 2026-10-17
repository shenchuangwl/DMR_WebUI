"""逐笔交易的查询（repo_trades）：筛选、时间窗（按平仓时间）、排序、分页与总数。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dmr_kernel.core.types import ExitReason, Ownership, PositionSide
from dmr_kernel.ledger import repo_trades
from dmr_kernel.ledger.models import Trade

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT


def _add(session, **kw):
    session.add(Trade(**kw))


async def _put(ledger, n, *, symbol="BTCUSDT", side=LONG, ownership=Ownership.STRATEGY, model="m1", close_ms=0):
    await ledger.write(_add, position_id=n, symbol=symbol, side=side, ownership=ownership, model=model, is_mixed=False,
                       open_ms=close_ms - 10, close_ms=close_ms, open_time_estimated=False, max_qty=D(1), open_avg=D(100),
                       close_avg=D(101), open_notional=D(100), gross_pnl=D(1), fees=D(0), funding=D(0), net_pnl=D(1),
                       entry_reason="", exit_reason=ExitReason.SIGNAL)


async def _fill(ledger):
    await _put(ledger, 1, close_ms=1000)
    await _put(ledger, 2, symbol="ETHUSDT", side=SHORT, close_ms=2000)
    await _put(ledger, 3, ownership=Ownership.TAKEOVER, model="m2", close_ms=3000)
    await _put(ledger, 4, model="m3", ownership=Ownership.AI, close_ms=4000)
    return ledger


async def _ids(ledger, **kw):
    rows, total = await ledger.read(repo_trades.query, **kw)
    return [r.position_id for r in rows], total


async def test_default_is_newest_first_and_total_ignores_limit(ledger):
    filled = await _fill(ledger)
    assert await _ids(filled) == ([4, 3, 2, 1], 4)
    assert await _ids(filled, limit=2) == ([4, 3], 4)
    assert await _ids(filled, order="asc", limit=2) == ([1, 2], 4)


async def test_filters(ledger):
    filled = await _fill(ledger)
    assert await _ids(filled, symbol="ETHUSDT") == ([2], 1)
    assert await _ids(filled, side=SHORT) == ([2], 1)
    assert await _ids(filled, ownership=Ownership.TAKEOVER) == ([3], 1)
    assert await _ids(filled, model="m3") == ([4], 1)
    assert await _ids(filled, since_ms=2000, until_ms=3000) == ([3, 2], 2)  # 含两端，按平仓时间
    assert await _ids(filled, symbol="BTCUSDT", model="m1") == ([1], 1)


async def test_bad_arguments(ledger):
    filled = await _fill(ledger)
    with pytest.raises(ValueError):
        await _ids(filled, order="sideways")
    with pytest.raises(ValueError):
        await _ids(filled, limit=0)
