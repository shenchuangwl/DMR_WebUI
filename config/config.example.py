# ============================================================
# DMR 量化交易系统 - 主配置示例
#
# 用法：
#   cp config/config.example.py config/config.py
#   然后按自己的交易对、仓位与风控参数修改。
#
# ⚠️  config/config.py 已被 .gitignore 忽略（config/*.py 默认拒绝策略），
#     请勿把填好的版本提交到版本库。
# ⚠️  API 凭证一律通过环境变量注入，切勿写死在本文件里：
#       export BINANCE_API_KEY=...
#       export BINANCE_API_SECRET=...
#     或复制 .env.example 为 .env（导入 config 包时会自动加载）。
#
# 本文件包含公开代码实际引用的**全部**配置符号，复制后即可启动。
# 下列数值均为保守的中性默认值，不代表任何实盘参数。
# ============================================================
import os

# ---------- Binance API 凭证（必填，缺失时 validate_config 会拦截）----------
API_KEY = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '')

# ---------- 全局核心参数（唯一数据源）----------
TRADE_SYMBOL = 'BTC/USDT'
TRADE_AMOUNT = 10          # 单位 USDT

# 向后兼容别名
SYMBOL = TRADE_SYMBOL
POSITION_SIZE = TRADE_AMOUNT

# ---------- 策略标识（用于 clientOrderId 前缀与持仓归属判定）----------
STRATEGY_ID_LONG = 'DMR_LONG'
STRATEGY_ID_SHORT = 'DMR_SHORT'

# ---------- 时间周期 ----------
TIMEFRAME_LONG = '4h'
TIMEFRAME_SHORT = '1h'
TIMEFRAME_4H = '4h'
TIMEFRAME_1H = '1h'

# 周期 -> 分钟数映射（代码用它换算 K 线间隔）
TIMEFRAME_MINUTES = {
    '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
    '1h': 60, '2h': 120, '4h': 240, '6h': 360, '8h': 480,
    '12h': 720, '1d': 1440,
}

# ---------- DMR / 均线参数 ----------
DMR_LONG_PERIOD = 12
DMR_SHORT_PERIOD = 26
STRATEGY_LONG_PERIOD = DMR_LONG_PERIOD
STRATEGY_SHORT_PERIOD = DMR_SHORT_PERIOD
MA_LONG_PERIOD = 26
MA_SHORT_PERIOD = 12

# ---------- 日志 ----------
LOG_LEVEL = 'INFO'
LOG_FILE = 'trading_log.txt'

# ============================================================
# 账户设置写入开关（⚠️ 高风险，默认关闭）
# 开启后程序会在开仓前改写账户级杠杆与保证金模式，
# 影响同一账户下的其它持仓与风控设置。
# ============================================================
APPLY_ACCOUNT_SETTINGS = os.environ.get('DMR_APPLY_ACCOUNT_SETTINGS', '0') == '1'
APPLY_LEVERAGE = int(os.environ.get('DMR_LEVERAGE', '10'))
APPLY_MARGIN_TYPE = os.environ.get('DMR_MARGIN_TYPE', 'CROSSED')

# ---------- 数据获取 ----------
DATA_FETCHER_CONFIG = {
    'data_limit': 500,        # 单次拉取 K 线根数
    'max_retries': 3,
    'rate_limit': True,       # 交给 ccxt 限速，避免被币安封 IP
    'recv_window': 5000,      # 毫秒；过大会放宽签名时间窗，不建议调高
    'sync_interval': 300,     # 秒；与交易所对时的间隔
}

# ---------- 下单执行 ----------
ORDER_EXECUTOR_CONFIG = {
    'default_type': 'future',
    'adjust_for_time_diff': True,   # 必须开启，币安签名对本地时钟偏差敏感
}

# ---------- 系统循环 ----------
SYSTEM_CONFIG = {
    'main_loop_sleep': 60,          # 秒
    'scheduler_sleep': 1,
    'force_sync_on_startup': True,
}

# ---------- DMR 策略主配置 ----------
DMR_STRATEGY_CONFIG = {
    'symbol': TRADE_SYMBOL,
    'trade_amount': TRADE_AMOUNT,
    'long_period': DMR_LONG_PERIOD,
    'short_period': DMR_SHORT_PERIOD,
    'timeframe_long': TIMEFRAME_LONG,
    'timeframe_short': TIMEFRAME_SHORT,
}

# ---------- 风控 ----------
# 单位说明：*_pct 是账户权益的比例；阈值由 common/risk_state.py 强制执行。
RISK_MANAGEMENT_CONFIG = {
    'max_daily_loss_pct': 0.05,
    'max_drawdown_pct': 0.10,
    'max_daily_trades': 25,
}

MONITORING_CONFIG = {
    'enabled': False,
    'alert_webhook_url': None,
}

# ---------- 纠错序列（示例：留空表示不启用）----------
CORRECTION_SEQUENCES_CONFIG = {}
QUADRANT_CONFIG = {}
SUPPORTED_TIMEFRAMES = list(TIMEFRAME_MINUTES.keys())
TIMEFRAME_COMBINATIONS = {
    'default': {'long': TIMEFRAME_LONG, 'short': TIMEFRAME_SHORT},
}

_config_change_callbacks = []


def register_config_change_callback(callback):
    """注册配置变更回调。"""
    if callback not in _config_change_callbacks:
        _config_change_callbacks.append(callback)


def unregister_config_change_callback(callback):
    """注销配置变更回调。"""
    if callback in _config_change_callbacks:
        _config_change_callbacks.remove(callback)


def get_current_correction_config():
    """返回当前纠错配置。

    键名是公开代码的硬契约：config/first_short_term_config.py 等模块
    直接按下列键取值，缺任何一个都会在 import 期抛 KeyError。
    """
    return {
        'correction_threshold': 0.0,          # 0 表示不触发纠错
        'correction_min_interval_long': 3600,  # 秒
        'correction_min_interval_short': 3600,
        'is_matched_sequence': False,
        'sequence_id': 'none',
        'timeframe_combination': {'long': TIMEFRAME_LONG, 'short': TIMEFRAME_SHORT},
    }


def get_correction_threshold():
    return get_current_correction_config()['correction_threshold']


def get_sequence_by_timeframes(long_timeframe=None, short_timeframe=None):
    """按时间周期组合查找纠错序列。示例配置返回 None。"""
    return None


def get_config_summary():
    """返回配置摘要（不包含任何凭证）。"""
    return {
        'symbol': TRADE_SYMBOL,
        'trade_amount': TRADE_AMOUNT,
        'timeframe_long': TIMEFRAME_LONG,
        'timeframe_short': TIMEFRAME_SHORT,
        'dmr_long_period': DMR_LONG_PERIOD,
        'dmr_short_period': DMR_SHORT_PERIOD,
        'credentials_present': bool(API_KEY and API_SECRET),
    }


def validate_credentials():
    """凭证缺失时抛错，避免带着空签名去请求交易所并触发风控。"""
    if not API_KEY or not API_SECRET:
        raise ValueError(
            '缺少 Binance API 凭证：请设置环境变量 BINANCE_API_KEY / BINANCE_API_SECRET，'
            '或复制 .env.example 为 .env 后填写。'
        )
    return True


def validate_config(require_credentials=True):
    """启动期配置校验。

    ``require_credentials`` 默认为 True：此前默认是 False，且实盘入口
    根本不调用本函数，导致 SECURITY.md 宣称的"启动早期拦截"从未生效。
    """
    if TRADE_AMOUNT <= 0:
        raise ValueError('TRADE_AMOUNT 必须为正数')
    if not TRADE_SYMBOL or '/' not in TRADE_SYMBOL:
        raise ValueError("TRADE_SYMBOL 格式应为 'BASE/QUOTE'，例如 'BTC/USDT'")
    for name, value in (('TIMEFRAME_LONG', TIMEFRAME_LONG), ('TIMEFRAME_SHORT', TIMEFRAME_SHORT)):
        if value not in TIMEFRAME_MINUTES:
            raise ValueError('%s=%r 不在 TIMEFRAME_MINUTES 中' % (name, value))
    if require_credentials:
        validate_credentials()
    return True
