"""风控判定 dmr_kernel.algo.riskeval 的单元测试（报告 4.6「风控闸的口径」，与旧代码不同，不做数值对拍）。

与原实现 common/risk_state.py 第 134–189 行（resolve_limits、evaluate）的差异，逐条写在各测试的说明里。
触发后的锁存与复位（日亏损 UTC 日切自动复位、回撤人工复位）由风控闸 core.riskgate 负责，算法只给出判定。
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from dmr_kernel.algo.riskeval import AccountRisk, ModelRisk, evaluate_account, evaluate_model, gate
from dmr_kernel.core.types import Model

D = Decimal


def _account(equity="1000", day_start="1000", peak="1000", limit="50", dd="10") -> AccountRisk:
    as_dec = (lambda v: None if v is None else D(v))
    return evaluate_account(as_dec(equity), as_dec(day_start), as_dec(peak), D(limit), D(dd))


# ---------------------------------------------------------------- 账户级：日亏损


def test_daily_loss_is_measured_from_the_utc_day_start_equity():
    """差异：原实现的日亏损是策略回调累计的已实现日盈亏，上限是「当前权益 × 百分比」；新实现是
    「UTC 日初权益 − 当前权益」（含浮动盈亏，覆盖账户里全部持仓），上限是 USDT 金额；达到上限即触发（≥，与原实现相同）。"""
    hit = _account(equity="950")
    assert hit.daily_loss == D("50") and hit.daily_loss_tripped and hit.tripped
    assert "日亏损" in hit.reason and "50" in hit.reason
    near = _account(equity="950.01")
    assert near.daily_loss == D("49.99") and not near.daily_loss_tripped and not near.tripped
    assert near.reason == ""


def test_a_daily_gain_never_trips():
    res = _account(equity="1100", peak="1100")
    assert res.daily_loss == D("-100") and not res.tripped


# ---------------------------------------------------------------- 账户级：权益峰值回撤


def test_drawdown_is_measured_from_peak_equity():
    """差异：原实现的回撤是「账本建立以来累计已实现亏损」与「峰值 × 百分比」比较；新实现是权益峰值回撤百分比
    （峰值 − 当前）/ 峰值 × 100，达到上限即触发。"""
    hit = _account(equity="900", day_start="900")
    assert hit.drawdown_pct == D("10") and hit.drawdown_tripped and not hit.daily_loss_tripped and hit.tripped
    assert "回撤" in hit.reason
    near = _account(equity="900.01", day_start="900.01")
    assert not near.drawdown_tripped and not near.tripped


def test_equity_above_the_recorded_peak_is_no_drawdown():
    res = _account(equity="1200", day_start="1200", peak="1000")
    assert res.drawdown_pct == D("0") and not res.tripped


def test_both_account_limits_can_trip_together():
    res = _account(equity="800")
    assert res.daily_loss_tripped and res.drawdown_tripped
    assert "日亏损" in res.reason and "回撤" in res.reason


@pytest.mark.parametrize(("equity", "day_start", "peak"), [
    (None, "1000", "1000"), ("1000", None, "1000"), ("1000", "1000", None), ("0", "1000", "1000"),
    ("1000", "-1", "1000"),
])
def test_unknown_equity_blocks_new_risk(equity, day_start, peak):
    """与原实现相同的原则：权益取不到时不能评估百分比限额，不新增风险，而不是悄悄放行。"""
    res = _account(equity=equity, day_start=day_start, peak=peak)
    assert not res.known and res.tripped and "权益" in res.reason
    assert res.daily_loss is None and res.drawdown_pct is None


@pytest.mark.parametrize(("limit", "dd"), [("0", "10"), ("-5", "10"), ("50", "0"), ("50", "100.1"), ("NaN", "10")])
def test_invalid_account_limits_are_program_errors(limit, dd):
    with pytest.raises(ValueError):
        _account(limit=limit, dd=dd)


# ---------------------------------------------------------------- 模型级


def test_model_daily_loss_uses_strategy_net_pnl():
    """模型级的日亏损：账本里归属为策略开仓的逐笔净盈亏之和（接管仓默认不计入），由调用方汇总后传入。"""
    hit = evaluate_model(D("-50"), 3, D("50"), 50)
    assert hit.loss_tripped and hit.tripped and "日亏损" in hit.reason
    assert not evaluate_model(D("-49.99"), 3, D("50"), 50).tripped
    assert not evaluate_model(D("120"), 3, D("50"), 50).tripped


def test_model_daily_trade_limit_counts_opens():
    """差异：原实现的日交易次数在每笔平仓时计数（RiskLedger.record_trade），而且与亏损限额混在同一个判定里；
    新实现按模型 1 当日的开仓次数计，单独成为模型级条件，只停模型 1 的新开仓。"""
    hit = evaluate_model(D("0"), 50, D("50"), 50)
    assert hit.trades_tripped and not hit.loss_tripped and hit.tripped and "交易次数" in hit.reason
    assert not evaluate_model(D("0"), 49, D("50"), 50).tripped


@pytest.mark.parametrize("args", [(D("0"), -1, D("50"), 50), (D("0"), 1, D("0"), 50), (D("0"), 1, D("50"), 0),
                                  (D("NaN"), 1, D("50"), 50)])
def test_invalid_model_inputs_are_program_errors(args):
    with pytest.raises(ValueError):
        evaluate_model(*args)


# ---------------------------------------------------------------- 风控闸判定


CLEAR_ACCOUNT = _account()
TRIPPED_ACCOUNT = _account(equity="900", day_start="1000")
CLEAR_MODEL = evaluate_model(D("0"), 0, D("50"), 50)
TRIPPED_MODEL = evaluate_model(D("-60"), 1, D("50"), 50)


@pytest.mark.parametrize("model", [Model.M1, Model.M2, Model.M3, None])
def test_gate_never_blocks_risk_reducing_actions(model):
    """差异：只拦新增风险（模型 1 开仓、反手的开仓步、将来的 AI 开仓）；平仓、止损类意图一律放行，
    即使账户级与模型级都已触发（旧的策略层风控会连信号平仓一起拦住；risk_state.evaluate 对非开仓同样放行）。"""
    assert gate(False, TRIPPED_ACCOUNT, TRIPPED_MODEL, model) == (True, "")


@pytest.mark.parametrize("model", [Model.M1, Model.M2, Model.M3, None])
def test_account_level_trip_blocks_every_model(model):
    allowed, reason = gate(True, TRIPPED_ACCOUNT, CLEAR_MODEL, model)
    assert not allowed and reason == TRIPPED_ACCOUNT.reason and reason


def test_model_level_trip_only_blocks_model_1():
    """差异：账户级与模型级分开；模型级触发不是抑制原因，只是模型 1 的开仓条件，不影响模型 2 与操作员入口。"""
    allowed, reason = gate(True, CLEAR_ACCOUNT, TRIPPED_MODEL, Model.M1)
    assert not allowed and reason == TRIPPED_MODEL.reason
    assert gate(True, CLEAR_ACCOUNT, TRIPPED_MODEL, Model.M2) == (True, "")
    assert gate(True, CLEAR_ACCOUNT, TRIPPED_MODEL, None) == (True, "")


def test_gate_allows_new_risk_within_limits():
    assert gate(True, CLEAR_ACCOUNT, CLEAR_MODEL, Model.M1) == (True, "")
    assert gate(True, CLEAR_ACCOUNT, None, Model.M2) == (True, "")


def test_model_1_needs_a_model_level_evaluation():
    with pytest.raises(ValueError):
        gate(True, CLEAR_ACCOUNT, None, Model.M1)


def test_results_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        CLEAR_ACCOUNT.known = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        CLEAR_MODEL.loss_tripped = True  # type: ignore[misc]
    assert isinstance(CLEAR_MODEL, ModelRisk)
