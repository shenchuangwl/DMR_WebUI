"""内核代码 → 中文显示。代码取值来自内核的枚举（docs/设计约定.md 第 9 节、报告 4.6）；这里不导入内核，
只按报文里的字符串对表。表里没有的新代码原样显示（宁可显示英文代码，也不要把它吞掉或写错）。
"""
from __future__ import annotations

from typing import Any, Mapping

TITLE = "DMR 控制台"

MODEL_LABELS = {
    "m1": "模型 1 · 完全自动化量化交易",
    "m2": "模型 2 · 接管手动订单",
    "m3": "模型 3 · AI（预留）",
}

GATE_LABELS = {"stopped": "已停止开仓", "running": "运行中"}

ENV_LABELS = {
    "standin": "替身（standin）",
    "testnet": "测试网",
    "mainnet_shadow": "主网只读影子",
    "mainnet_live": "主网实盘",
}

SUPPRESSION_LABELS = {
    "stream_switching": "数据流切换中",
    "reconcile_unknown": "对账未知",
    "reconcile_mismatch": "对账不一致",
    "exchange_restricted": "交易所限制",
    "risk_tripped": "风控触发（账户级）",
    "unprotected": "存在无保护持仓",
    "recovering": "恢复中",
    "mutex_anomaly": "互斥异常",
    "clock_skew": "时钟偏差",
}

OP_LOCK_LABELS = {"switch_preview": "切换预览中", "switching": "切换中", "bulk_closing": "批量平仓中"}

RUN_MODE_LABELS = {"normal": "正常", "polling_degraded": "仅轮询降级"}

STREAM_STATE_LABELS = {
    "connected": "已连接",
    "connecting": "连接中",
    "reconnecting": "重连中",
    "disconnected": "已断开",
    "stale": "数据停滞",
    "switching": "切换中",
}
STREAM_OK_STATES = frozenset({"connected"})
STREAM_BAD_STATES = frozenset({"disconnected", "stale"})

RECONCILE_STATUS_LABELS = {"ok": "一致", "mismatch": "不一致", "unknown": "未知", "never": "未对账", "running": "进行中"}
RECONCILE_OK_STATUSES = frozenset({"ok"})
RECONCILE_BAD_STATUSES = frozenset({"mismatch"})

SIDE_LABELS = {"LONG": "多 LONG", "SHORT": "空 SHORT"}

POSITION_STATUS_LABELS = {
    "pending": "待确认",
    "taking_over": "接管中",
    "managed": "托管中",
    "closing": "平仓中",
    "closed": "已平",
    "ignored": "外部（已忽略）",
}
REGISTERED_STATUSES = frozenset({"taking_over", "managed", "closing"})

OWNERSHIP_LABELS = {"strategy": "策略", "takeover": "接管", "ai": "AI", "none": "无归属"}

PROTECTION_LABELS = {"system": "系统保护", "external": "外部保护", "rebuilding": "重建中", "none": "无保护"}

EXIT_RULESET_LABELS = {
    "strategy": "策略规则",
    "strategy_no_reverse": "策略规则（不反手）",
    "takeover": "接管规则",
}

PENDING_REASON_LABELS = {
    "out_of_scope": "不在接管清单",
    "notional_out_of_range": "名义金额不在上下限之间",
    "preexisting": "启动模型 2 时已有",
    "legacy": "旧程序遗留",
    "unknown_origin": "来源不明",
    "origin_to_verify": "来源待核对",
    "model_not_active": "模型 2 未生效或未运行",
}

EXIT_REASON_LABELS = {
    "stop": "止损",
    "signal": "反向信号",
    "reverse": "反手",
    "manual": "手动平仓",
    "close_all": "一键全平",
    "partial": "部分平仓",
    "protective": "保护性平仓",
    "external": "外部平仓",
    "liquidation": "强平",
    "adl": "自动减仓",
    "settlement": "交易所结算",
    "emergency": "应急入口",
    "unknown": "未知",
}

TRADE_MODEL_LABELS = {"m1": "模型 1", "m2": "模型 2", "m3": "模型 3", "op": "手动"}

M3_AI_TIER_LABELS = {"off": "关闭", "shadow": "影子记录", "advise": "建议", "veto": "否决"}
M3_EXEC_TIER_LABELS = {"off": "关闭", "shadow": "影子", "paper": "模拟盘", "testnet": "测试网", "live_manual": "实盘手动",
                       "live_auto": "实盘自动"}

MARGIN_MODE_LABELS = {"cross": "全仓", "isolated": "逐仓"}

ALERT_LEVEL_LABELS = {"info": "提示", "warning": "警告", "critical": "严重"}

OPERATION_LABELS = {
    "start": "启动",
    "stop": "停止",
    "partial_close": "部分平仓",
    "close_all": "一键全部平仓",
    "switch": "切换模型",
    "switch_confirm": "确认切换",
    "switch_cancel": "取消切换",
    "close_one": "平掉此仓",
    "abort": "中止批量操作",
    "takeover": "接管",
    "ignore": "忽略",
    "restore": "恢复托管",
    "cancel_takeover": "取消托管",
    "hand_stop_to_system": "交给系统管理止损",
}


def label_of(table: Mapping[str, str], code: Any) -> str:
    """表里有就返回中文，没有就原样显示代码（非字符串转成文字；None 返回空串）。"""
    if isinstance(code, str) and code in table:
        return table[code]
    return "" if code is None else str(code)
