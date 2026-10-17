"""DMR 指标对拍：dmr_kernel.algo.dmr.dmr 对四份金标 CSV，以及对被哈希的原实现 calculate_dmr。

金标：bit/data/bit_short_term/*_short_term_data.csv（dmr_12）与 bit/data/bit_long_term/*_long_term_data.csv（dmr_6）。
对拍前去掉末行（获取时未收盘的那根，报告 5.3）；从第 n 行（0 起第 n−1 行）起逐项误差 ≤ 1e-12。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import dmr_originals as orig
from dmr_kernel.algo.dmr import dmr
from dmr_kernel.algo.signal import long_trend

GOLD = orig.gold_files()
GOLD_IDS = [rel.rsplit("/", 1)[-1] for rel, _, _ in GOLD]
TOL = 1e-12


def test_gold_csvs_for_both_timeframes_are_present():
    periods = [n for _, n, _ in GOLD]
    assert periods.count(12) >= 2 and periods.count(6) >= 2, GOLD


@pytest.mark.parametrize(("rel", "n", "col"), GOLD, ids=GOLD_IDS)
def test_dmr_matches_gold_from_row_n(rel, n, col):
    bars = orig.gold_closed(rel)
    assert len(bars) == len(orig.gold_all_rows(rel)) - 1
    new = dmr(bars["high"], bars["low"], n)
    gold = bars[col].to_numpy()
    np.testing.assert_allclose(new.to_numpy()[n - 1:], gold[n - 1:], rtol=0, atol=TOL)
    assert not new.iloc[n - 1:].isna().any()


@pytest.mark.parametrize(("rel", "n", "col"), GOLD, ids=GOLD_IDS)
def test_first_n_minus_1_values_are_invalid_instead_of_zero(rel, n, col):
    """5.4 第 12 项（有意差异）：原实现把 DMR 序列的前 n−1 个值补 0（第 140 行 ffill().fillna(0)），金标里是 0.0；
    新实现标为无效（NaN），不参与过零判断——否则开头补的 0 与第一个有效值之间会被当成一次过零。"""
    bars = orig.gold_closed(rel)
    assert (bars[col].iloc[: n - 1] == 0.0).all()
    new = dmr(bars["high"], bars["low"], n)
    assert new.iloc[: n - 1].isna().all()


@pytest.mark.parametrize(("rel", "n", "col"), GOLD, ids=GOLD_IDS)
def test_hashed_original_reproduces_the_gold_csv(rel, n, col):
    """金标确由被哈希的那段原实现生成（含末行）：原实现在隔离命名空间里执行，桩 self 不读日志文件。"""
    full = orig.gold_all_rows(rel)
    out = orig.calculate_dmr(n, long_term=(n == 6))(full[["open", "high", "low", "close", "volume"]])
    np.testing.assert_allclose(out[f"dmr_{n}"].to_numpy(), full[col].to_numpy(), rtol=0, atol=TOL)


@pytest.mark.parametrize("n", [1, 2, 6, 12, 26])
def test_dmr_matches_original_on_synthetic_bars(n):
    rng = np.random.default_rng(20261014 + n)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    df = pd.DataFrame({"open": close, "high": close * (1 + rng.uniform(0, 0.01, 400)),
                       "low": close * (1 - rng.uniform(0, 0.01, 400)), "close": close, "volume": 1.0})
    old = orig.calculate_dmr(n)(df)[f"dmr_{n}"].to_numpy()
    new = dmr(df["high"], df["low"], n).to_numpy()
    np.testing.assert_allclose(new[n - 1:], old[n - 1:], rtol=0, atol=TOL)
    assert np.isnan(new[: n - 1]).all() and (old[: n - 1] == 0.0).all()


@pytest.mark.parametrize(("rel", "n", "col"), GOLD, ids=GOLD_IDS)
def test_current_value_comes_from_the_last_closed_bar(rel, n, col):
    """5.4 第 1 项（有意差异）：原实现在含未收盘那根的序列上取「当前值」——短周期 detect_signal 第 266 行、
    长周期 get_long_term_trend 第 134 行都取 iloc[-1]，即金标的末行（获取时未收盘）。
    新实现只用已收盘 K 线：当前值是最后一根已收盘的 K 线（金标的倒数第二行）。"""
    full = orig.gold_all_rows(rel)
    closed = orig.gold_closed(rel)
    new = dmr(closed["high"], closed["low"], n)
    assert new.iloc[-1] == pytest.approx(full[col].iloc[-2], abs=TOL)
    if n == 12:
        _, current_old = orig.current_and_previous()(full, n, None)
        assert current_old == full[col].iloc[-1]
    else:
        trend_old = orig.long_trend()(full)
        assert trend_old == orig.long_trend()(full.iloc[-1:])  # 只看末行（未收盘那根）
        assert long_trend(new.iloc[-1]) == orig.long_trend()(full.iloc[-2:-1])
