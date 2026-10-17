"""风控判定（报告 4.6「风控闸的口径」）：账户级（日亏损、权益峰值回撤）、模型级（模型 1 的日亏损与日交易次数）、
风控闸的放行判定。只给出判定；触发后的锁存与复位（日亏损 UTC 日切自动复位、回撤人工复位）由 core.riskgate 负责。

来源（只读引用 DMR 源码；sha256 是该行段原文 UTF-8 的哈希，与 tests/parity/source_hashes.json 一致）：

- common/risk_state.py:134-141  resolve_limits（限额与默认值合并；不誊写：限额来自配置 core.configdefs）
  sha256 a6cf804e58146e070df041e27b6583449f5b7588aa58cd9052328adc79e82074
- common/risk_state.py:144-189  evaluate（是否放行）
  sha256 3bfc4c4992a6aa5dc3ab45c898322797b44ceabc8248b7a169b4ba7fd49f429f

口径已按报告 4.6 改写，不做数值对拍；与原实现的差异逐条写在 tests/unit/algo/test_algo_riskeval.py 的测试说明里。
沿用原实现的原则：不新增风险的动作（平仓、止损类）一律放行；权益取不到时不能评估限额，不新增风险；达到上限即触发（≥）。

口径差异：

- 日亏损 = UTC 日初权益 − 当前权益（含浮动盈亏，覆盖账户里全部持仓），上限是 USDT 金额；
  原实现是策略回调累计的已实现日盈亏，上限是「当前权益 × 百分比」。
- 回撤 = (权益峰值 − 当前权益) / 峰值 × 100（百分数；当前权益高于峰值时为 0）；
  原实现是账本建立以来累计已实现亏损与「峰值 × 百分比」比较。
- 账户级与模型级分开：账户级触发拦住所有模型的新增风险；模型级（模型 1 当日策略仓的净盈亏、当日开仓次数，
  由调用方从账本汇总后传入）只拦模型 1 的新开仓。原实现的日交易次数在每笔平仓时计数，且与亏损限额混在同一个判定里。
- 限额非法（不为正、NaN、回撤上限超过 100%）是程序错误，报 ValueError。

版权与许可：本文件誊写自 DMR（DMR_binance_Version_V16_A1）的源码，保留其版权声明。
    Copyright 2026 shenchuangwl
    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    许可证全文见 DMR_WebUI/LICENSE，声明见 DMR_WebUI/NOTICE。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from dmr_kernel.algo.rounding import EXACT, finite_decimal, is_int, positive_decimal, to_decimal
from dmr_kernel.core.types import Model

_HUNDRED = Decimal(100)
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class AccountRisk:
    """账户级判定。known=False 表示权益取不到，此时不许新增风险。"""

    known: bool
    daily_loss: Decimal | None  # UTC 日初权益 − 当前权益（USDT；负数是当日盈利）
    drawdown_pct: Decimal | None  # 权益峰值回撤（百分数）
    daily_loss_tripped: bool
    drawdown_tripped: bool
    reason: str = ""  # 不许新增风险的原因；未触发时为空

    @property
    def tripped(self) -> bool:
        """是否不许新增风险：任一账户级限额触发，或权益取不到。"""
        return not self.known or self.daily_loss_tripped or self.drawdown_tripped


@dataclass(frozen=True)
class ModelRisk:
    """模型级判定（只约束模型 1 的新开仓）。"""

    loss_tripped: bool
    trades_tripped: bool
    reason: str = ""

    @property
    def tripped(self) -> bool:
        return self.loss_tripped or self.trades_tripped


def _s(value: Decimal) -> str:
    return format(value, "f")


def _equity(value: Decimal | int | float | None) -> Decimal | None:
    """权益读数：None、NaN、无穷、不为正都是「取不到」。"""
    if value is None:
        return None
    out = to_decimal(value)
    return out if out.is_finite() and out > 0 else None


def _count(value: int, what: str, minimum: int) -> int:
    if not is_int(value) or value < minimum:
        raise ValueError(f"{what}必须是不小于 {minimum} 的整数：{value!r}")
    return int(value)


def evaluate_account(equity: Decimal | None, day_start_equity: Decimal | None, peak_equity: Decimal | None,
                     daily_loss_limit: Decimal, max_drawdown_pct: Decimal) -> AccountRisk:
    """账户级：日亏损（UTC 日初权益为基准，上限是 USDT 金额）与权益峰值回撤（上限是百分数）。"""
    limit = positive_decimal(daily_loss_limit, "日亏损上限")
    max_dd = positive_decimal(max_drawdown_pct, "回撤上限")
    if max_dd > _HUNDRED:
        raise ValueError(f"回撤上限是百分数，不能超过 100：{max_drawdown_pct!r}")
    cur, start, peak = _equity(equity), _equity(day_start_equity), _equity(peak_equity)
    if cur is None or start is None or peak is None:
        missing = "、".join(name for name, v in (("当前权益", cur), ("UTC 日初权益", start), ("权益峰值", peak))
                           if v is None)
        return AccountRisk(known=False, daily_loss=None, drawdown_pct=None, daily_loss_tripped=False,
                           drawdown_tripped=False, reason=f"权益取不到（{missing}），不能评估账户级限额，不新增风险")
    with localcontext(EXACT):
        daily_loss = start - cur
        drawdown = max((peak - cur) / peak * _HUNDRED, Decimal(0))
        shown_dd = drawdown.quantize(_CENT)
    loss_hit = daily_loss >= limit
    dd_hit = drawdown >= max_dd
    reasons = []
    if loss_hit:
        reasons.append(f"日亏损 {_s(daily_loss)} USDT 达到上限 {_s(limit)} USDT"
                       f"（UTC 日初权益 {_s(start)}，当前权益 {_s(cur)}）")
    if dd_hit:
        reasons.append(f"权益峰值回撤 {_s(shown_dd)}% 达到上限 {_s(max_dd)}%（峰值 {_s(peak)}，当前权益 {_s(cur)}）")
    return AccountRisk(known=True, daily_loss=daily_loss, drawdown_pct=drawdown, daily_loss_tripped=loss_hit,
                       drawdown_tripped=dd_hit, reason="；".join(reasons))


def evaluate_model(strategy_net_pnl_today: Decimal, strategy_opens_today: int, daily_loss_limit: Decimal,
                   daily_trade_limit: int) -> ModelRisk:
    """模型级（模型 1）：当日（UTC）策略仓已平交易的净盈亏 ≤ −日亏损上限，或当日开仓次数 ≥ 日交易次数上限。"""
    pnl = finite_decimal(strategy_net_pnl_today, "当日策略净盈亏")
    opens = _count(strategy_opens_today, "当日开仓次数", 0)
    limit = positive_decimal(daily_loss_limit, "日亏损上限")
    trade_limit = _count(daily_trade_limit, "日交易次数上限", 1)
    loss_hit = pnl <= limit.copy_negate()
    trades_hit = opens >= trade_limit
    reasons = []
    if loss_hit:
        reasons.append(f"模型 1 当日策略仓净亏损 {_s(pnl.copy_negate())} USDT 达到日亏损上限 {_s(limit)} USDT")
    if trades_hit:
        reasons.append(f"模型 1 当日开仓 {opens} 次达到日交易次数上限 {trade_limit} 次")
    return ModelRisk(loss_tripped=loss_hit, trades_tripped=trades_hit, reason="；".join(reasons))


def gate(adds_risk: bool, account: AccountRisk, model_level: ModelRisk | None,
         model: Model | str | None) -> tuple[bool, str]:
    """风控闸：不新增风险的动作一律放行；账户级触发拦住所有模型；模型级触发只拦模型 1。返回 (是否放行, 原因)。

    model 为 None 表示操作员与系统动作。模型 1 新增风险时必须给出模型级判定（model_level）。
    """
    if not isinstance(adds_risk, bool):
        raise TypeError(f"adds_risk 必须是布尔值：{adds_risk!r}")
    if not adds_risk:
        return True, ""
    if not isinstance(account, AccountRisk):
        raise TypeError(f"需要账户级判定 AccountRisk，得到 {type(account).__name__}")
    if account.tripped:
        return False, account.reason
    if model is not None and Model(model) is Model.M1:
        if model_level is None:
            raise ValueError("模型 1 新增风险前必须给出模型级判定")
        if not isinstance(model_level, ModelRisk):
            raise TypeError(f"需要模型级判定 ModelRisk，得到 {type(model_level).__name__}")
        if model_level.tripped:
            return False, model_level.reason
    return True, ""
