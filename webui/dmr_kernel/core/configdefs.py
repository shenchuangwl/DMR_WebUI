"""运行期配置项的定义（第 0a 层，纯数据与纯校验）。

来源：计划报告 4.12 的配置表；替身环境的本轮取值见任务说明第 4 节（接实盘前由用户另定）；
由测试网实测决定的设计分支（6.1）各给一个开关，默认取保守的一种（docs/实施记录.md）。

值保存在账本的 config_item 表；读取与修改由 core.config（第 2 层）与 ops.config_ops（第 6 层）完成。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from dmr_kernel.core.types import Environment


class ConfigError(ValueError):
    pass


APPLY_MODES = {
    "immediate": "立即生效",
    "next_signal": "下一个信号起生效（不影响已有持仓）",
    "next_stop": "下一次挂止损或移动止损起生效",
    "next_takeover": "只影响之后新出现的仓，已托管的不变",
    "next_start": "下次启动模型时生效",
    "restart": "内核重启后生效",
}

_UNSET = object()
# 交易所原始合约代码：大写字母、数字，以及主网的中文合约名（附录 C：5 个中文合约名）
_SYMBOL_RE = re.compile(r"^[0-9A-Z㐀-鿿豈-﫿]{2,40}$")
TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h")


@dataclass(frozen=True)
class ConfigDef:
    key: str
    group: str
    label: str
    kind: str  # int | decimal | bool | choice | symbols
    default: Any
    standin_default: Any = _UNSET
    min: Any = None
    max: Any = None
    choices: tuple = ()
    unit: str = ""
    apply: str = "immediate"
    affects_existing: bool = False
    required_for_live: bool = False
    requires_deploy_switch: bool = False
    note: str = ""

    def initial(self, env: Environment) -> Any:
        if env is Environment.STANDIN and self.standin_default is not _UNSET:
            return self.standin_default
        return self.default


D = Decimal


def _defs() -> list[ConfigDef]:
    return [
        # ---------------------------------------------------------------- 资金
        ConfigDef("trade_notional_usdt", "资金", "单笔名义金额", "decimal", None, D("10"), D("1"), D("10000"),
                  unit="USDT", apply="next_signal", required_for_live=True, note="待你定（DMR 现值 10）"),
        ConfigDef("leverage", "资金", "杠杆", "int", None, 2, 1, 20, unit="倍", apply="next_signal",
                  required_for_live=True, note="模型 1 开仓前检查；接管仓所在合约不改动"),
        ConfigDef("margin_mode", "资金", "保证金模式", "choice", None, "isolated", choices=("isolated", "cross"),
                  apply="next_signal", required_for_live=True),
        ConfigDef("max_symbols", "资金", "持仓合约数上限", "int", 30, min=1, max=35, unit="个",
                  note="硬上限 35；按全账户「有持仓或任何挂单」的合约数计，对冲两侧各算一个"),
        ConfigDef("max_symbols_degraded", "资金", "仅轮询降级时的上限", "int", 10, min=1, max=35, unit="个"),
        ConfigDef("max_orders_per_symbol_10m", "资金", "同一合约 10 分钟内的订单数上限", "int", 25, min=1, max=200,
                  unit="笔", note="上限 30 时考核门槛的一半"),
        # ---------------------------------------------------------------- 风控
        ConfigDef("daily_loss_limit", "风控", "日亏损上限（账户级，UTC 日初权益为基准）", "decimal", None, D("50"),
                  D("0.01"), D("1000000"), unit="USDT", required_for_live=True),
        ConfigDef("max_drawdown", "风控", "权益峰值回撤上限", "decimal", None, D("10"), D("0.1"), D("100"),
                  unit="%", required_for_live=True, note="触发后须人工复位"),
        ConfigDef("daily_trade_limit", "风控", "日交易次数上限（模型级，只停模型 1 新开仓）", "int", None, 50, 1, 10000,
                  unit="次", required_for_live=True),
        # ---------------------------------------------------------------- 止损
        ConfigDef("init_stop_min_pct", "止损", "初始止损距离下限 A", "decimal", None, D("1"), D("0.3"), D("50"),
                  unit="%", apply="next_stop", required_for_live=True,
                  note="必须 0 < A < B；A 不小于 0.3%；B 小于按配置杠杆估算的强平距离"),
        ConfigDef("init_stop_max_pct", "止损", "初始止损距离上限 B", "decimal", None, D("5"), D("0.3"), D("99"),
                  unit="%", apply="next_stop", required_for_live=True),
        ConfigDef("stop_trigger_price_type", "止损", "止损触发价类型", "choice", "CONTRACT_PRICE",
                  choices=("CONTRACT_PRICE", "MARK_PRICE"), apply="next_stop", affects_existing=True,
                  note="CONTRACT_PRICE=最新价（现状）；MARK_PRICE=标记价"),
        ConfigDef("protect_target_s", "止损", "挂止损的目标时限", "int", 5, min=1, max=60, unit="秒"),
        ConfigDef("protect_retry_window_s", "止损", "挂止损的重试窗口", "int", 30, min=5, max=300, unit="秒"),
        ConfigDef("protect_retries", "止损", "挂止损的重试次数", "int", 3, min=0, max=20, unit="次"),
        ConfigDef("stop_gap_target_s", "止损", "更新止损时无保护间隙的目标", "int", 3, min=1, max=30, unit="秒"),
        ConfigDef("stop_gap_max_s", "止损", "更新止损时无保护间隙的上限", "int", 30, min=3, max=120, unit="秒"),
        # ---------------------------------------------------------------- 策略
        ConfigDef("timeframe_short", "策略", "短周期（入场）", "choice", "15m", choices=TIMEFRAMES, apply="restart"),
        ConfigDef("timeframe_long", "策略", "长周期（定方向）", "choice", "1h", choices=TIMEFRAMES, apply="restart"),
        ConfigDef("correction_signal", "策略", "纠错信号", "bool", False, note="首期只留配置位，不实现；只能为关"),
        ConfigDef("cooldown_bars", "策略", "平仓后该合约冷却的短周期 K 线根数", "int", 4, min=0, max=96, unit="根"),
        ConfigDef("intent_ttl_s", "策略", "开仓意图有效期", "int", 60, min=5, max=600, unit="秒"),
        ConfigDef("order_unknown_grace_s", "策略", "判定开仓单「不存在」前在签名时间窗之外再等的时间", "int", 15,
                  min=0, max=120, unit="秒"),
        # ---------------------------------------------------------------- 名单
        ConfigDef("list_poll_s", "名单", "名单拉取间隔", "int", 60, min=10, max=600, unit="秒", apply="restart"),
        ConfigDef("list_member_ttl_h", "名单", "成员有效时长（0 表示当日有效）", "int", 0, min=0, max=48, unit="小时"),
        ConfigDef("require_direction_match", "名单", "名单方向与信号方向一致才开", "bool", True, apply="next_signal"),
        ConfigDef("list_max_new_per_poll", "名单", "单次新增币种数上限", "int", 40, min=1, max=500, unit="个",
                  note="T3.1 用历史快照标定"),
        ConfigDef("list_invalid_alert_min", "名单", "名单连续无效多久告警", "int", 30, min=1, max=1440, unit="分钟"),
        # ---------------------------------------------------------------- 接管
        ConfigDef("takeover_symbols", "接管", "接管清单", "symbols", [], [], apply="next_takeover",
                  note="首次启动模型 2 前必须设置；修改只影响之后新出现的仓"),
        ConfigDef("takeover_min_notional", "接管", "自动接管的名义金额下限", "decimal", None, D("5"), D("0"),
                  D("1000000"), unit="USDT", apply="next_takeover", required_for_live=True),
        ConfigDef("takeover_max_notional", "接管", "自动接管的名义金额上限", "decimal", None, D("200"), D("0"),
                  D("1000000"), unit="USDT", apply="next_takeover", required_for_live=True),
        ConfigDef("takeover_signal_exit", "接管", "接管仓是否启用反向信号平仓", "bool", False, apply="next_takeover"),
        ConfigDef("pending_remind_min", "接管", "待确认仓多久没处理就重复推送", "int", 30, min=1, max=1440, unit="分钟"),
        # ---------------------------------------------------------------- 行情
        ConfigDef("kline_delay_s", "行情", "收盘后多久开始取数", "int", 3, min=0, max=60, unit="秒"),
        ConfigDef("kline_ready_deadline_s", "行情", "收盘后多久内必须就绪", "int", 20, min=5, max=120, unit="秒"),
        ConfigDef("kline_missing_alert_periods", "行情", "连续几个周期取不到 K 线告警", "int", 2, min=1, max=20,
                  unit="个"),
        # ---------------------------------------------------------------- 同步
        ConfigDef("stream_silence_s", "同步", "数据流判活阈值", "int", 90, min=10, max=600, unit="秒"),
        ConfigDef("stream_recover_stable_s", "同步", "数据流恢复后的稳定时间", "int", 60, min=5, max=600, unit="秒"),
        ConfigDef("listenkey_stale_min", "同步", "listenKey 多久没有续期成功就判定数据流中断", "int", 50, min=5,
                  max=59, unit="分钟"),
        ConfigDef("reconcile_mismatch_stop_s", "同步", "对账不一致多久没解决就自动停止", "int", 300, min=30,
                  max=3600, unit="秒"),
        ConfigDef("reconcile_positions_s", "同步", "持仓对账间隔", "int", 10, min=3, max=60, unit="秒",
                  apply="restart"),
        ConfigDef("reconcile_orders_s", "同步", "挂单与条件单对账间隔", "int", 60, min=10, max=600, unit="秒",
                  apply="restart"),
        ConfigDef("clock_skew_max_ms", "同步", "与交易所服务器时间的最大偏差", "int", 1000, min=100, max=10000,
                  unit="毫秒"),
        ConfigDef("weight_soft_limit", "同步", "已用权重超过多少时暂停非保护类读请求", "int", 1500, min=100,
                  max=2400),
        # ---------------------------------------------------------------- 操作
        ConfigDef("partial_close_pause_s", "操作", "部分平仓在途时自动动作的暂停上限", "int", 30, min=5, max=300,
                  unit="秒"),
        ConfigDef("close_one_timeout_s", "操作", "单仓平仓时限", "int", 15, min=5, max=120, unit="秒"),
        ConfigDef("close_all_timeout_s", "操作", "一键全平整体时限", "int", 120, min=30, max=600, unit="秒"),
        ConfigDef("close_all_wait_open_s", "操作", "一键全平前等待在途开仓意图的时限", "int", 10, min=1, max=60,
                  unit="秒"),
        ConfigDef("shutdown_drain_s", "操作", "关停时等待在途意图的时限", "int", 10, min=1, max=60, unit="秒",
                  apply="restart"),
        ConfigDef("switch_settle_s", "操作", "切换停稳时限", "int", 10, min=1, max=60, unit="秒"),
        ConfigDef("switch_preview_ttl_s", "操作", "切换预览有效期", "int", 60, min=10, max=600, unit="秒"),
        # ---------------------------------------------------------------- 会话
        ConfigDef("reauth_window_s", "会话", "口令重新校验的有效窗口", "int", 300, min=30, max=3600, unit="秒"),
        ConfigDef("seat_lease_s", "会话", "操作席位租约", "int", 60, min=15, max=600, unit="秒"),
        ConfigDef("session_idle_min", "会话", "会话闲置过期", "int", 30, min=1, max=1440, unit="分钟"),
        # ---------------------------------------------------------------- 告警
        ConfigDef("heartbeat_s", "告警", "心跳间隔", "int", 30, min=5, max=300, unit="秒", apply="restart"),
        ConfigDef("heartbeat_alert_after_s", "告警", "心跳停止多久告警", "int", 60, min=10, max=600, unit="秒"),
        # ---------------------------------------------------------------- 由测试网实测决定的分支（6.1）
        ConfigDef("ex_stop_update_mode", "实测分支", "移动止损的更新方式（实测第 5 项）", "choice", "gap",
                  choices=("gap", "make_before_break"),
                  note="gap=先撤后挂、受控间隙（不假设可并存两张全平型止损，保守）；make_before_break=先建后撤"),
        ConfigDef("ex_resend_client_id", "实测分支", "超时重发用的订单编号（实测第 13 项）", "choice", "new",
                  choices=("new", "same"), note="new=换新编号，以「持仓数量未变」为唯一防线（保守）"),
        ConfigDef("ex_qty_stop_protect_rule", "实测分支", "数量型止损算外部保护的条件（实测第 3 项）", "choice",
                  "equal_only", choices=("equal_only", "ge"),
                  note="equal_only=数量等于持仓才算（保守）；ge=数量不小于持仓即算"),
        ConfigDef("ex_close_min_notional_exempt", "实测分支", "对冲平仓单是否豁免最小名义金额（实测第 2 项）",
                  "bool", False, note="否=碎仓改用全平型条件单平掉（保守）"),
        ConfigDef("ex_conditional_limit", "实测分支", "条件单数量上限的口径（实测第 4 项）", "choice",
                  "per_symbol_10", choices=("per_symbol_10", "account_200"), note="只影响告警阈值"),
        ConfigDef("ex_stream_enabled", "实测分支", "使用账户数据流（实测第 8 项）", "bool", True, apply="restart",
                  note="否=默认按仅轮询运行，上限取降级值"),
        # ---------------------------------------------------------------- 模型 3 预留（默认全关；改档需部署级开关）
        ConfigDef("m3_form", "模型3", "模型 3 的形态", "choice", "strategy_exec_ai_veto",
                  choices=("strategy_exec_ai_veto", "ai_proposal_sealed", "ai_autonomous"),
                  apply="restart", requires_deploy_switch=True, note="本轮按「策略执行、AI 只做否决」预留"),
        ConfigDef("m3_ai_tier", "模型3", "AI 档", "choice", "off", choices=("off", "shadow", "advise", "veto"),
                  apply="restart", requires_deploy_switch=True),
        ConfigDef("m3_exec_tier", "模型3", "执行档", "choice", "off",
                  choices=("off", "shadow", "paper", "testnet", "live_manual", "live_auto"),
                  apply="restart", requires_deploy_switch=True),
        ConfigDef("m3_master_switch", "模型3", "模型 3 总闸", "bool", False, apply="restart",
                  requires_deploy_switch=True),
        ConfigDef("m3_max_notional_usdt", "模型3", "模型 3 资金上限", "decimal", D("0"), min=D("0"),
                  max=D("1000000"), unit="USDT", apply="restart", requires_deploy_switch=True),
        ConfigDef("m3_max_symbols", "模型3", "模型 3 持仓上限", "int", 0, min=0, max=35, unit="个", apply="restart",
                  requires_deploy_switch=True),
    ]


DEFINITIONS: dict[str, ConfigDef] = {d.key: d for d in _defs()}


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ConfigError("需要数值")
    try:
        out = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"无法解析为数值：{value!r}") from exc
    if not out.is_finite():
        raise ConfigError(f"无法解析为数值（不能是 NaN 或无穷大）：{value!r}")
    return out


def validate_value(key: str, value: Any) -> Any:
    defn = DEFINITIONS.get(key)
    if defn is None:
        raise ConfigError(f"未知的配置项：{key}")
    if value is None:
        raise ConfigError(f"{defn.label} 不能为空")
    kind = defn.kind
    if kind == "int":
        if isinstance(value, bool):
            raise ConfigError(f"{defn.label} 需要整数")
        try:
            out: Any = int(str(value).strip()) if not isinstance(value, int) else value
        except ValueError as exc:
            raise ConfigError(f"{defn.label} 需要整数") from exc
    elif kind == "decimal":
        out = _to_decimal(value)
    elif kind == "bool":
        if isinstance(value, bool):
            out = value
        elif str(value).lower() in ("true", "1", "yes", "on"):
            out = True
        elif str(value).lower() in ("false", "0", "no", "off"):
            out = False
        else:
            raise ConfigError(f"{defn.label} 需要是或否")
        if key == "correction_signal" and out:
            raise ConfigError("纠错信号首期不实现，只能为关")
        return out
    elif kind == "choice":
        out = str(value).strip()
        if out not in defn.choices:
            raise ConfigError(f"{defn.label} 只能取 {', '.join(defn.choices)}")
        return out
    elif kind == "symbols":
        if isinstance(value, str):
            value = [v for v in re.split(r"[,\s]+", value) if v]
        if not isinstance(value, Iterable):
            raise ConfigError(f"{defn.label} 需要合约代码列表")
        seen: list[str] = []
        for item in value:
            code = str(item).strip().upper()
            if not _SYMBOL_RE.fullmatch(code):
                raise ConfigError(f"合约代码格式不对（用交易所原始代码，如 BTCUSDT）：{item!r}")
            if code not in seen:
                seen.append(code)
        return seen
    else:  # pragma: no cover - 定义表写错时才会走到
        raise ConfigError(f"未知的类型 {kind}")
    if defn.min is not None and out < defn.min:
        raise ConfigError(f"{defn.label} 不能小于 {defn.min}{defn.unit}")
    if defn.max is not None and out > defn.max:
        raise ConfigError(f"{defn.label} 不能大于 {defn.max}{defn.unit}")
    return out


def initial_values(env: Environment) -> dict[str, Any]:
    return {key: (list(d.initial(env)) if isinstance(d.initial(env), list) else d.initial(env))
            for key, d in DEFINITIONS.items()}


def estimate_liquidation_distance_pct(leverage: int, mmr_pct: Decimal = Decimal("0.5")) -> Decimal:
    """按配置杠杆粗估逐仓强平距离（%）：100 / 杠杆 − 维持保证金率。只用于校验 B 的上限。"""
    return Decimal(100) / Decimal(leverage) - mmr_pct


def check_stop_band(a: Decimal | None, b: Decimal | None, leverage: int | None) -> list[str]:
    problems: list[str] = []
    if a is None or b is None:
        return ["初始止损的 A、B 必须都配置"]
    a, b = _to_decimal(a), _to_decimal(b)
    if not a > 0:
        problems.append("初始止损下限 A 必须大于 0")
    if a < Decimal("0.3"):
        problems.append("初始止损下限 A 不应小于 0.3%（建议下限）")
    if not a < b:
        problems.append("初始止损必须满足 A < B")
    if leverage:
        liq = estimate_liquidation_distance_pct(int(leverage))
        if b >= liq:
            problems.append(f"初始止损上限 B 必须小于按 {leverage} 倍杠杆估算的强平距离（约 {liq}%）")
    return problems


def cross_field_problems(values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    a, b = values.get("init_stop_min_pct"), values.get("init_stop_max_pct")
    if a is not None and b is not None:
        problems += check_stop_band(a, b, values.get("leverage"))
    lo, hi = values.get("takeover_min_notional"), values.get("takeover_max_notional")
    if lo is not None and hi is not None and not _to_decimal(lo) < _to_decimal(hi):
        problems.append("自动接管的名义金额必须下限 < 上限")
    ms, md = values.get("max_symbols"), values.get("max_symbols_degraded")
    if ms is not None and md is not None and md > ms:
        problems.append("仅轮询降级时的上限不能大于持仓合约数上限")
    ts, tl = values.get("timeframe_short"), values.get("timeframe_long")
    if ts and tl and TIMEFRAMES.index(ts) >= TIMEFRAMES.index(tl):
        problems.append("短周期必须小于长周期")
    return problems


def missing_for_live(values: dict[str, Any]) -> list[str]:
    return [k for k, d in DEFINITIONS.items() if d.required_for_live and values.get(k) is None]
