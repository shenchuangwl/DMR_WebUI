"""candle 表的仓储函数：已收盘的 K 线（行情模块的缓存落盘，重启后预热用）。

签名一律 ``fn(session, ...)``，经 ``Ledger.write``/``Ledger.read`` 调用；返回记录或基本类型。

- 表里只该有已收盘的 K 线；是否收盘由 ``marketdata`` 按服务器时间判断，这里不判断。
- 主键是（合约、周期、开盘时间）：同一根再写一次就是覆盖（交易所偶尔修正已收盘的 K 线）。
- ``upsert_bars``：一个事务写多个合约的一批 K 线；``keep`` 让每个**写到了的**合约只留最新的 ``keep`` 根
  （先写后修剪，补写的旧 K 线若落在最新 ``keep`` 根之外，被修剪掉的是它）。批次里任何一根被表拒绝，整个批次回滚。
- ``load_recent``：每个合约取最新的 ``limit`` 根，升序；没有数据的合约给空列表。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from dmr_kernel.core.types import Candle
from dmr_kernel.ledger import models
from dmr_kernel.ledger.records import CandleRecord

MAX_LIMIT = 5000
_VALUE_COLUMNS = ("open", "high", "low", "close", "volume")


def _check_name(what: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{what} 必须是字符串：{value!r}")
    if not value:
        raise ValueError(f"{what} 不能为空")


def _check_positive_int(what: str, value: object, *, upper: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} 必须是整数：{value!r}")
    if value < 1 or (upper is not None and value > upper):
        bound = "不小于 1" if upper is None else f"在 1 到 {upper} 之间"
        raise ValueError(f"{what} 必须{bound}：{value}")


def upsert_bars(session: Session, timeframe: str, bars_by_symbol: Mapping[str, Sequence[Candle]], *,
                keep: int | None = None) -> int:
    """写入（或覆盖）各合约的一批已收盘 K 线，返回写入的总根数。"""
    _check_name("timeframe", timeframe)
    if keep is not None:
        _check_positive_int("keep", keep)
    for symbol in bars_by_symbol:
        _check_name("symbol", symbol)
    written = 0
    for symbol, bars in bars_by_symbol.items():
        if not bars:
            continue
        rows = [{"symbol": symbol, "timeframe": timeframe, "open_time_ms": bar.open_time_ms, "open": bar.open,
                 "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume} for bar in bars]
        stmt = insert(models.Candle)
        stmt = stmt.on_conflict_do_update(
            index_elements=[models.Candle.symbol, models.Candle.timeframe, models.Candle.open_time_ms],
            set_={name: stmt.excluded[name] for name in _VALUE_COLUMNS})
        session.execute(stmt, rows)
        written += len(rows)
        if keep is not None:
            _trim(session, symbol, timeframe, keep)
    return written


def _trim(session: Session, symbol: str, timeframe: str, keep: int) -> None:
    """只留这个合约这个周期最新的 ``keep`` 根。"""
    oldest_kept = session.scalar(
        select(models.Candle.open_time_ms)
        .where(models.Candle.symbol == symbol, models.Candle.timeframe == timeframe)
        .order_by(models.Candle.open_time_ms.desc()).offset(keep - 1).limit(1))
    if oldest_kept is not None:
        session.execute(delete(models.Candle).where(
            models.Candle.symbol == symbol, models.Candle.timeframe == timeframe,
            models.Candle.open_time_ms < oldest_kept))


def load_recent(session: Session, timeframe: str, symbols: Sequence[str], limit: int) -> dict[str, list[CandleRecord]]:
    """每个合约最新的 ``limit`` 根 K 线（升序）；表里没有的合约对应空列表。"""
    _check_name("timeframe", timeframe)
    _check_positive_int("limit", limit, upper=MAX_LIMIT)
    if isinstance(symbols, str):
        raise TypeError(f"symbols 必须是合约列表，不是单个字符串：{symbols!r}")
    loaded: dict[str, list[CandleRecord]] = {}
    for symbol in symbols:
        _check_name("symbol", symbol)
        newest_first = session.scalars(
            select(models.Candle)
            .where(models.Candle.symbol == symbol, models.Candle.timeframe == timeframe)
            .order_by(models.Candle.open_time_ms.desc()).limit(limit))
        loaded[symbol] = [CandleRecord.from_row(row) for row in reversed(list(newest_first))]
    return loaded
