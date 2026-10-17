# 短周期策略独立配置
# 从主配置文件同步核心参数 - 实现参数统一管理
import sys
import os

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# 直接导入config模块
import config.config as main_config

# 导入所需的配置项
API_KEY = main_config.API_KEY
API_SECRET = main_config.API_SECRET
TRADE_SYMBOL = main_config.TRADE_SYMBOL
TRADE_AMOUNT = main_config.TRADE_AMOUNT
DMR_SHORT_PERIOD = main_config.DMR_SHORT_PERIOD
TIMEFRAME_SHORT = main_config.TIMEFRAME_SHORT
TIMEFRAME_LONG = main_config.TIMEFRAME_LONG
STRATEGY_ID_SHORT = main_config.STRATEGY_ID_SHORT
get_current_correction_config = main_config.get_current_correction_config
get_correction_threshold = main_config.get_correction_threshold
get_sequence_by_timeframes = main_config.get_sequence_by_timeframes
register_config_change_callback = main_config.register_config_change_callback

# === 配置同步与热更新支持 ===
# 当核心配置发生变化（如时间周期组合、交易币种、交易金额）时，自动更新本地配置

def _update_from_core():
    """从核心配置同步关键字段并重建动态信号配置"""
    global SHORT_TERM_CONFIG
    SHORT_TERM_CONFIG.update({
        'symbol': main_config.TRADE_SYMBOL,
        'position_size': main_config.TRADE_AMOUNT,
        'dmr_period': main_config.DMR_SHORT_PERIOD,
        'timeframe': main_config.TIMEFRAME_SHORT,
    })
    SHORT_TERM_CONFIG['signal_config'] = _generate_dynamic_signal_config()


def _on_core_config_change(change_type, old_value, new_value):
    """核心配置变更回调：自动同步短周期配置"""
    if change_type in ('timeframe_combination', 'trade_symbol', 'trade_amount'):
        try:
            _update_from_core()
            print(f"[short_term_config] 已同步核心配置变更: {change_type} -> {new_value}")
        except Exception as e:
            print(f"[short_term_config] 同步核心配置失败: {e}")


def enable_auto_sync():
    """启用自动同步（注册核心配置回调）"""
    try:
        register_config_change_callback(_on_core_config_change)
    except Exception as e:
        print(f"[short_term_config] 注册自动同步失败: {e}")


def disable_auto_sync():
    """禁用自动同步（取消注册回调）"""
    try:
        unregister_config_change_callback(_on_core_config_change)
    except Exception:
        pass

# 默认启用自动同步
enable_auto_sync()

unregister_config_change_callback = main_config.unregister_config_change_callback

# 短周期策略专用配置
SHORT_TERM_CONFIG = {
    # === 策略基础信息 ===
    'strategy_name': 'ShortTerm_DMR26_Strategy',
    'strategy_version': '2.0.0',
    'strategy_type': 'short_term',
    'strategy_id': STRATEGY_ID_SHORT,  # 使用从config.py导入的策略标识

    # === 核心交易参数（自动同步自config.py）===
    'symbol': TRADE_SYMBOL,            # 自动同步交易币种
    'position_size': TRADE_AMOUNT,     # 自动同步交易金额
    'dmr_period': DMR_SHORT_PERIOD,    # 自动同步DMR周期
    'timeframe': TIMEFRAME_SHORT,      # 自动同步时间框架

    # === API配置（自动同步）===
    'api_key': API_KEY,
    'api_secret': API_SECRET,

    # === 策略专用配置 ===
    'data_config': {
        'data_limit': 1000,
        'sync_interval': 300000,
        'max_retries': 3,
        'recv_window': 10000,
        'rate_limit': True,
        'cache_enabled': True,
        'cache_ttl': 180,  # 短周期缓存时间更短
    },

    'order_config': {
        'open_order_type': 'limit',
        'close_order_type': 'market',
        'order_timeout': 30000,
        'slippage_tolerance': 0.001,
        'retry_attempts': 3,
        'retry_delay': 500,  # 短周期重试延迟更短
    },

    'risk_config': {
        'max_position_size': TRADE_AMOUNT * 5,  # 基于统一交易金额动态计算
        'stop_loss_pct': 0.02,
        'take_profit_pct': 0.05,
        'max_daily_trades': 25,
        'max_drawdown_pct': 0.10,
        'position_limit_check': True,
    },

    'log_config': {
        'log_file': 'short_term_strategy.log',
        'log_level': 'INFO',
        'log_format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        'max_file_size': 10485760,  # 10MB
        'backup_count': 5,
    },

    # === 策略特定参数 ===
    'signal_config': None,  # 将在下面动态生成

    'performance_config': {
        'benchmark_symbol': 'BTC/USDT',
        'performance_window': 7,   # 短周期性能评估窗口更短
        'report_interval': 21600,  # 报告间隔更频繁（6小时）
    }
}

def _generate_dynamic_signal_config():
    """
    动态生成短周期策略的信号配置
    从统一纠错配置系统获取当前时间周期对应的纠错参数
    """
    # 获取当前纠错配置
    correction_config = get_current_correction_config()

    return {
        'signal_threshold': 1e-6,
        'confirmation_periods': 1,
        'signal_timeout': 1800,  # 短周期信号超时时间更短（30分钟）
        'position_correction_enabled': True,  # 启用持仓纠错功能
        'correction_threshold': correction_config['correction_threshold'],  # 从统一配置获取
        'correction_min_interval': correction_config['correction_min_interval_short'],  # 短周期间隔
        'sequence_id': correction_config['sequence_id'],  # 序列ID
        'timeframe_combination': correction_config['timeframe_combination'],  # 时间周期组合
        'is_matched_sequence': correction_config['is_matched_sequence'],  # 是否匹配序列
        'description': correction_config.get('description', '短周期策略纠错配置')
    }

# 动态生成signal_config
SHORT_TERM_CONFIG['signal_config'] = _generate_dynamic_signal_config()

# === 配置验证函数 ===
def validate_short_term_config():
    """验证短周期策略配置"""
    config = SHORT_TERM_CONFIG

    # 验证核心参数同步
    assert config['symbol'] == TRADE_SYMBOL, f"交易币种参数同步失败: {config['symbol']} != {TRADE_SYMBOL}"
    assert config['position_size'] == TRADE_AMOUNT, f"交易金额参数同步失败: {config['position_size']} != {TRADE_AMOUNT}"
    assert config['dmr_period'] == DMR_SHORT_PERIOD, f"DMR周期参数同步失败: {config['dmr_period']} != {DMR_SHORT_PERIOD}"
    assert config['timeframe'] == TIMEFRAME_SHORT, f"时间框架参数同步失败: {config['timeframe']} != {TIMEFRAME_SHORT}"

    print("短周期策略配置验证通过")
    return True

def get_short_term_config():
    """获取短周期策略配置（支持热更新与自动同步）"""
    # 不再reload核心配置模块，避免覆盖动态变更；直接从已加载模块读取
    SHORT_TERM_CONFIG.update({
        'symbol': main_config.TRADE_SYMBOL,
        'position_size': main_config.TRADE_AMOUNT,
        'dmr_period': main_config.DMR_SHORT_PERIOD,
        'timeframe': main_config.TIMEFRAME_SHORT,
    })
    # 重建动态信号配置以反映当前纠错参数
    SHORT_TERM_CONFIG['signal_config'] = _generate_dynamic_signal_config()
    return SHORT_TERM_CONFIG

# 执行验证
if __name__ == "__main__":
    validate_short_term_config()
    print("短周期策略配置摘要:", {
        'symbol': SHORT_TERM_CONFIG['symbol'],
        'position_size': SHORT_TERM_CONFIG['position_size'],
        'dmr_period': SHORT_TERM_CONFIG['dmr_period'],
        'timeframe': SHORT_TERM_CONFIG['timeframe']
    })

    print("\n=== 短周期策略纠错配置 ===")
    signal_config = SHORT_TERM_CONFIG['signal_config']
    print(f"纠错阈值: {signal_config['correction_threshold']}")
    print(f"纠错间隔: {signal_config['correction_min_interval']}秒")
    print(f"序列ID: {signal_config['sequence_id']}")
    print(f"时间周期组合: {signal_config['timeframe_combination']}")
    print(f"是否匹配序列: {signal_config['is_matched_sequence']}")
    print(f"配置描述: {signal_config['description']}")
