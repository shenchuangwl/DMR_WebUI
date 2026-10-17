# ============================================================
# BIT 专属策略配置示例
#
# 用法： cp bit/config/bit_config.example.py bit/config/bit_config.py
#
# ⚠️  bit/config/bit_config.py 已被 .gitignore 忽略，请勿提交填好的版本。
# ⚠️  凭证只通过环境变量注入（或 .env）。
# ============================================================
import os

API_KEY = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '')

TRADE_SYMBOL = 'BTC/USDT'
TRADE_AMOUNT = 10

STRATEGY_ID_LONG = 'BIT_LONG'
STRATEGY_ID_SHORT = 'BIT_SHORT'

TIMEFRAME_LONG = '4h'
TIMEFRAME_SHORT = '15m'
TIMEFRAME_MINUTES = {
    '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
    '1h': 60, '2h': 120, '4h': 240, '6h': 360, '8h': 480,
    '12h': 720, '1d': 1440,
}

DMR_LONG_PERIOD = 12
DMR_SHORT_PERIOD = 26

TRAILING_STOP_CONFIG = {
    'enabled': True,
    'atr_period': 14,
    'data_dir': 'bit/data/bit_positions',
}

LADDER_TAKE_PROFIT_CONFIG = {
    'enabled': False,
    'data_dir': 'bit/data/bit_positions',
}

TRAILING_TAKE_PROFIT_CONFIG = {
    'enabled': True,
    'atr_period': 14,
    'data_dir': 'bit/data/bit_positions',
}

_config_change_callbacks = []


def register_config_change_callback(callback):
    if callback not in _config_change_callbacks:
        _config_change_callbacks.append(callback)


def unregister_config_change_callback(callback):
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
    return None
