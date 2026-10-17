# 风险控制配置
RISK_CONFIG = {
    # 持仓规则
    'position_rules': {
        'base_position_size': 20.0,        # 基础仓位20U
        'max_add_times': 1,                # 最大加仓1次 (20U+20U=40U)
        'add_profit_threshold': 0.01,      # 加仓盈利阈值1%
        'hedge_balance_tolerance': 0.05,   # 对冲平衡容忍度5%
        'max_position_ratio': 0.5          # 最大持仓比例50%
    },
    
    # 风险限制
    'risk_limits': {
        'max_position_imbalance': 0.1,     # 最大持仓不平衡10%
        'emergency_stop_loss': 0.05,       # 紧急止损5%
        'max_daily_trades': 50,            # 每日最大交易次数
        'max_daily_loss': 0.1,             # 最大日损失10%
        'min_order_interval': 30           # 最小订单间隔30秒
    },
    
    # 监控设置
    'monitoring': {
        'position_sync_interval': 30,      # 持仓同步间隔30秒
        'anomaly_check_interval': 60,      # 异常检查间隔60秒
        'auto_repair_enabled': True,       # 启用自动修复
        'emergency_stop_enabled': True,    # 启用紧急停止
        'alert_webhook_url': None          # 告警webhook地址
    },
    
    # 策略隔离
    'strategy_isolation': {
        'independent_fund_pools': True,    # 独立资金池
        'cross_strategy_hedge_check': True, # 跨策略对冲检查
        'position_sync_required': True     # 要求持仓同步
    }
}

# 测试场景配置
TEST_SCENARIOS = {
    'basic_trading': {
        'description': '基础交易场景测试',
        'test_cases': [
            'single_strategy_open_close',
            'add_position_on_profit',
            'hedge_balance_validation'
        ]
    },
    
    'stress_testing': {
        'description': '压力测试场景',
        'test_cases': [
            'concurrent_trading',
            'rapid_signal_changes',
            'network_interruption',
            'exchange_api_errors'
        ]
    },
    
    'anomaly_handling': {
        'description': '异常处理测试',
        'test_cases': [
            'position_imbalance_recovery',
            'add_overflow_prevention',
            'emergency_stop_trigger'
        ]
    }
}