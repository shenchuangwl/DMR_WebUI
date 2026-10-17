"""回放行情源（计划 T0.5「行情源」）：用 DMR_WebUI/bit/data 下的真实 CSV（只读）测对齐、循环、插值、缩放。

约定（ReplayFeed 的文档同此）：
- 去掉 CSV 的末行（未收盘），其余 N 行按「正放到头再倒放」往返循环：虚拟序号 r = k mod 2N，r < N 取第 r 行；
  否则取第 2N−1−r 行并把开收互换（高低不变）。这样首尾相接处价格连续，长时间运行也不漂移。
- 启动时刻 S：最后一根已收盘的行（第 N−1 行）对齐到 floor(S, 周期) − 周期 那根。
- 当根 K 线内按时间在开、高、低、收之间线性插值：阳线 开→低→高→收，阴线 开→高→低→收，各占三分之一。
- 样例里的其它合约：按 crc32 选一条基础序列并错开相位，按参考价缩放，使启动时刻的价格等于 ref_price。
"""
from __future__ import annotations

import csv
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from dmr_standin.feed import FeedError, ReplayFeed, load_sample
from standin_kit import T0, FakeClock, spec

DMR = Path(__file__).resolve().parents[3]
DATA = DMR / "bit" / "data"
M15 = 900_000
H1 = 3_600_000
S = T0 + 5 * 60_000  # 02:05，15m 当根走了三分之一


def _rows(path: Path) -> list[tuple[Decimal, ...]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = [tuple(Decimal(r[k]) for k in ("open", "high", "low", "close", "volume")) for r in csv.DictReader(fh)]
    return rows[:-1]  # 末行未收盘


CYS15 = _rows(DATA / "bit_short_term" / "CYS_USDT_short_term_data.csv")
CYS1H = _rows(DATA / "bit_long_term" / "CYS_USDT_long_term_data.csv")


def _virtual(rows, k: int):
    n = len(rows)
    r = k % (2 * n)
    if r < n:
        return rows[r]
    o, h, l, c, v = rows[2 * n - 1 - r]
    return (c, h, l, o, v)


def _feed(clock=None, extra=()):
    clock = clock or FakeClock(S)
    insts = [spec("CYSUSDT", "0.0001", "1", "1", "5"), spec("MAVIAUSDT", "0.00001", "1", "1", "5"), *extra]
    return ReplayFeed(clock=clock, data_dir=DATA, instruments=insts), clock


def _ohlc(bar):
    return tuple(Decimal(x) for x in bar[1:5])


def test_base_symbols_come_from_csv_file_names():
    feed, _ = _feed()
    assert feed.base_symbols == ["CYSUSDT", "MAVIAUSDT"]
    assert feed.source == "replay" and feed.status()["source"] == "replay"
    assert feed.funding_rate("CYSUSDT") == Decimal("0.0001")


async def test_last_closed_bar_aligned_to_latest_closed_boundary():
    feed, _ = _feed()
    bars = await feed.klines("CYSUSDT", "15m", 3)
    opens = [b[0] for b in bars]
    assert opens == [T0 - 2 * M15, T0 - M15, T0]
    assert bars[-1][6] == T0 + M15 - 1  # close_time = open + 周期 − 1
    n = len(CYS15)
    assert _ohlc(bars[1]) == CYS15[n - 1][:4]  # 最后一根已收盘 = CSV 最后一行已收盘的
    assert _ohlc(bars[0]) == CYS15[n - 2][:4]
    hourly = await feed.klines("CYSUSDT", "1h", 2)
    assert hourly[0][0] == T0 - H1 and _ohlc(hourly[0]) == CYS1H[len(CYS1H) - 1][:4]  # 1h 用长周期 CSV


async def test_current_bar_is_interpolated_partial_bar():
    feed, clock = _feed()
    n = len(CYS15)
    o, h, l, c, _ = _virtual(CYS15, n)  # 当根 = 倒放的第 N−1 行
    first, second = (l, h) if c >= o else (h, l)
    bar = (await feed.klines("CYSUSDT", "15m", 1))[0]
    assert Decimal(bar[1]) == o
    assert Decimal(bar[4]) == first  # 三分之一处正好到第一个极值
    assert Decimal(bar[2]) == max(o, first) and Decimal(bar[3]) == min(o, first)
    assert feed.quote("CYSUSDT").last == first == feed.quote("CYSUSDT").mark
    clock.set(T0)  # 当根起点 = 开盘价
    assert feed.quote("CYSUSDT").last == o
    clock.set(T0 + M15 // 6)  # 起点与第一个极值的中点
    mid = ((o + first) / 2).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    assert feed.quote("CYSUSDT").last == mid
    clock.set(T0 + 2 * M15 // 3)
    assert feed.quote("CYSUSDT").last == second
    clock.set(T0 + M15 - 1)
    assert abs(feed.quote("CYSUSDT").last - c) <= Decimal("0.0001")


async def test_ping_pong_cycle_is_continuous_at_both_turns():
    feed, clock = _feed()
    n = len(CYS15)
    clock.set(T0 + 2 * M15)
    bars = await feed.klines("CYSUSDT", "15m", 4)
    assert [_ohlc(b) for b in bars[:3]] == [_virtual(CYS15, k)[:4] for k in (n - 1, n, n + 1)]
    assert Decimal(bars[1][1]) == Decimal(bars[0][4])  # 正放到头接倒放：开 = 上一根收
    # 倒放到头（第 0 行）再正放：T0 那根是 k = N，所以 T0 + (N+1)·15m 是 k = 2N+1，前两根已收盘的是 2N−1、2N
    clock.set(T0 + (n + 1) * M15)
    bars = await feed.klines("CYSUSDT", "15m", 3)
    ks = (2 * n - 1, 2 * n)
    assert [_ohlc(b) for b in bars[:2]] == [_virtual(CYS15, k)[:4] for k in ks]
    assert Decimal(bars[1][1]) == Decimal(bars[0][4])
    assert _ohlc(bars[1]) == CYS15[0][:4]


async def test_history_is_ascending_and_contiguous():
    feed, _ = _feed()
    bars = await feed.klines("MAVIAUSDT", "15m", 99)
    assert len(bars) == 99
    assert all(b2[0] - b1[0] == M15 for b1, b2 in zip(bars, bars[1:]))
    assert all(isinstance(x, str) for b in bars for x in b[1:6])


async def test_derived_symbol_scaled_to_ref_price_and_follows_base_shape():
    btc = spec("BTCUSDT", "0.1", "0.001", "0.001", "100", ref_price="50000")
    feed, clock = _feed(extra=[btc])
    assert feed.quote("BTCUSDT").last == Decimal("50000")  # 启动时刻 = 参考价
    info = feed.describe("BTCUSDT")
    assert info["base"] in ("CYSUSDT", "MAVIAUSDT")
    base_rows = CYS15 if info["base"] == "CYSUSDT" else _rows(DATA / "bit_short_term" / "MAVIA_USDT_short_term_data.csv")
    scale, phase = Decimal(info["scale"]["15m"]), info["phase_bars"]["15m"]
    n = len(base_rows)
    bars = await feed.klines("BTCUSDT", "15m", 5)
    for i, bar in enumerate(bars[:-1]):  # 已收盘的 4 根
        k = n - 1 - (3 - i) + phase
        expected = tuple((x * scale).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) for x in _virtual(base_rows, k)[:4])
        assert _ohlc(bar) == expected
    assert "ETHUSDT" not in feed.symbols()  # 不在合约清单里的不生成


async def test_aggregated_and_sub_intervals():
    feed, clock = _feed()
    clock.set(T0 + 2 * M15)  # 对齐 30m 边界
    b15 = await feed.klines("CYSUSDT", "15m", 3)
    b30 = await feed.klines("CYSUSDT", "30m", 2)
    first, second = b15[0], b15[1]
    agg = b30[0]
    assert agg[0] == first[0] and agg[6] == second[6]
    assert Decimal(agg[1]) == Decimal(first[1]) and Decimal(agg[4]) == Decimal(second[4])
    assert Decimal(agg[2]) == max(Decimal(first[2]), Decimal(second[2]))
    assert Decimal(agg[3]) == min(Decimal(first[3]), Decimal(second[3]))
    b5 = await feed.klines("CYSUSDT", "5m", 4)  # 上一根 15m 的三根 5m + 当根
    parts = b5[:3]
    assert parts[0][0] == first[0] + M15 and Decimal(parts[0][1]) == Decimal(second[1])
    assert Decimal(parts[-1][4]) == Decimal(second[4])
    assert max(Decimal(p[2]) for p in parts) == Decimal(second[2])
    assert min(Decimal(p[3]) for p in parts) == Decimal(second[3])
    with pytest.raises(FeedError) as e:
        await feed.klines("CYSUSDT", "7m", 2)
    assert e.value.code == -1120


async def test_unknown_symbol_is_feed_error():
    feed, _ = _feed()
    with pytest.raises(FeedError) as e:
        await feed.klines("NOPEUSDT", "15m", 2)
    assert e.value.code == -1121
    assert feed.quote("NOPEUSDT") is None


def test_default_sample_is_loaded_with_ref_prices():
    sample = load_sample()
    symbols = {i.symbol for i in sample}
    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "1000PEPEUSDT"} <= symbols
    assert all(i.ref_price is not None and i.ref_price > 0 for i in sample)
    assert all(i.tick_size > 0 and i.step_size > 0 for i in sample)
    feed = ReplayFeed(clock=FakeClock(S), data_dir=DATA)
    assert {i.symbol for i in feed.instruments()} == symbols
    btc = feed.instrument("BTCUSDT")
    assert feed.quote("BTCUSDT").last == btc.ref_price.quantize(btc.tick_size, rounding=ROUND_HALF_UP)
