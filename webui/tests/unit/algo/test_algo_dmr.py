"""DMR 指标 dmr_kernel.algo.dmr.dmr 的单元测试：手算对照、无效值、输入校验。与金标的对拍在 tests/parity。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from dmr_kernel.algo.dmr import dmr

HIGH = [10.0, 10.4, 10.2, 10.8, 11.0, 10.6, 10.9, 11.3]
LOW = [9.6, 10.0, 9.8, 10.2, 10.4, 10.0, 10.5, 10.7]


def _by_hand(high: list[float], low: list[float], n: int) -> list[float]:
    mid = [(h + lo) / 2 for h, lo in zip(high, low)]
    ratio = [1.0] + [mid[i] / mid[i - 1] for i in range(1, len(mid))]
    return [math.nan if i < n - 1 else sum(ratio[i - n + 1:i + 1]) / n - 1 for i in range(len(mid))]


@pytest.mark.parametrize("n", [1, 2, 3, 6])
def test_dmr_is_the_n_bar_mean_of_midprice_ratios_minus_one(n):
    out = dmr(pd.Series(HIGH), pd.Series(LOW), n)
    np.testing.assert_allclose(out.to_numpy(), _by_hand(HIGH, LOW, n), rtol=0, atol=1e-15)


def test_first_ratio_is_taken_as_one():
    out = dmr(pd.Series(HIGH), pd.Series(LOW), 3)
    mid = [(h + lo) / 2 for h, lo in zip(HIGH, LOW)]
    assert out.iloc[2] == pytest.approx((1.0 + mid[1] / mid[0] + mid[2] / mid[1]) / 3 - 1, abs=1e-15)


def test_first_n_minus_1_values_are_invalid():
    """5.4 第 12 项（有意差异）：原实现把前 n−1 个值补 0，新实现标为无效（NaN），不参与过零判断。"""
    out = dmr(pd.Series(HIGH), pd.Series(LOW), 6)
    assert out.iloc[:5].isna().all()
    assert not out.iloc[5:].isna().any()
    assert dmr(pd.Series(HIGH), pd.Series(LOW), 1).iloc[0] == 0.0  # n = 1 时没有无效前缀


def test_a_bar_without_price_invalidates_the_values_that_depend_on_it():
    """有意差异：原实现对比率整列 fillna(1.0)、对 DMR 整列 ffill()，缺价的那根被当成「价格没变」，DMR 沿用旧值；
    新实现把依赖缺价那根的值都标为无效（NaN），之后的窗口不再含它时恢复有效。"""
    high = list(HIGH) + [11.2, 11.5, 11.4]
    low = list(LOW) + [10.8, 11.0, 11.1]
    high[4] = math.nan
    n = 3
    out = dmr(pd.Series(high), pd.Series(low), n)
    # 第 4 根缺价：第 4、5 根的比率无效；窗口含它们的第 4 … 5+n−1 根全部无效
    assert out.iloc[2:4].notna().all()
    assert out.iloc[4:5 + n].isna().all()
    assert out.iloc[5 + n:].notna().all()


def test_keeps_the_input_index_and_does_not_modify_inputs():
    idx = pd.Index([1_789_000_200_000 + 900_000 * i for i in range(len(HIGH))], name="open_time_ms")
    high, low = pd.Series(HIGH, index=idx), pd.Series(LOW, index=idx)
    before = (high.copy(), low.copy())
    out = dmr(high, low, 3)
    assert out.index.equals(idx)
    assert out.name == "dmr_3"
    pd.testing.assert_series_equal(high, before[0])
    pd.testing.assert_series_equal(low, before[1])


@pytest.mark.parametrize("n", [0, -1, 1.5, True])
def test_rejects_invalid_period(n):
    with pytest.raises(ValueError):
        dmr(pd.Series(HIGH), pd.Series(LOW), n)


def test_rejects_misaligned_high_and_low():
    with pytest.raises(ValueError):
        dmr(pd.Series(HIGH), pd.Series(LOW[:-1]), 3)
    with pytest.raises(ValueError):
        dmr(pd.Series(HIGH), pd.Series(LOW, index=range(1, len(LOW) + 1)), 3)
    with pytest.raises(TypeError):
        dmr(HIGH, LOW, 3)
