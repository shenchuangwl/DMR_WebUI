import json
import os
import sys

"""
配置管理器 - 实现参数同步、验证和热更新
"""
import importlib
import json
from datetime import datetime
from typing import Dict, Any, Optional

class ConfigManager:
    """配置管理器 - 统一管理所有配置参数"""
    
    def __init__(self):
        self.config_modules = {
            'main': 'config.config',
            'long_term': 'config.long_term_config',
            'short_term': 'config.short_term_config'
        }
        self.last_reload_time = {}
        
    def reload_config(self, module_name: str = 'all') -> bool:
        """重新加载配置模块"""
        try:
            if module_name == 'all':
                for name, module_path in self.config_modules.items():
                    module = importlib.import_module(module_path)
                    importlib.reload(module)
                    self.last_reload_time[name] = datetime.now()
                    print(f"配置模块 {name} 重新加载成功")
            else:
                if module_name in self.config_modules:
                    module_path = self.config_modules[module_name]
                    module = importlib.import_module(module_path)
                    importlib.reload(module)
                    self.last_reload_time[module_name] = datetime.now()
                    print(f"配置模块 {module_name} 重新加载成功")
                else:
                    raise ValueError(f"未知的配置模块: {module_name}")
            return True
        except Exception as e:
            print(f"配置重新加载失败: {e}")
            return False
    
    def validate_all_configs(self) -> Dict[str, bool]:
        """验证所有配置的一致性"""
        results = {}
        
        try:
            # 验证主配置
            from config.config import validate_config
            results['main'] = validate_config()
        except Exception as e:
            print(f"主配置验证失败: {e}")
            results['main'] = False
        
        try:
            # 验证长周期配置
            from config.long_term_config import validate_long_term_config
            results['long_term'] = validate_long_term_config()
        except Exception as e:
            print(f"长周期配置验证失败: {e}")
            results['long_term'] = False
        
        try:
            # 验证短周期配置
            from config.short_term_config import validate_short_term_config
            results['short_term'] = validate_short_term_config()
        except Exception as e:
            print(f"短周期配置验证失败: {e}")
            results['short_term'] = False
        
        return results
    
    def get_config_summary(self) -> Dict[str, Any]:
        """获取配置摘要"""
        try:
            from config.config import get_config_summary
            from config.long_term_config import LONG_TERM_CONFIG
            from config.short_term_config import SHORT_TERM_CONFIG
            
            return {
                'main': get_config_summary(),
                'long_term': {
                    'symbol': LONG_TERM_CONFIG['symbol'],
                    'position_size': LONG_TERM_CONFIG['position_size'],
                    'dmr_period': LONG_TERM_CONFIG['dmr_period'],
                    'timeframe': LONG_TERM_CONFIG['timeframe']
                },
                'short_term': {
                    'symbol': SHORT_TERM_CONFIG['symbol'],
                    'position_size': SHORT_TERM_CONFIG['position_size'],
                    'dmr_period': SHORT_TERM_CONFIG['dmr_period'],
                    'timeframe': SHORT_TERM_CONFIG['timeframe']
                },
                'last_reload': self.last_reload_time
            }
        except Exception as e:
            print(f"获取配置摘要失败: {e}")
            return {}
    
    def check_parameter_sync(self) -> Dict[str, bool]:
        """检查参数同步状态"""
        try:
            from config.config import TRADE_SYMBOL, TRADE_AMOUNT, DMR_LONG_PERIOD, DMR_SHORT_PERIOD
            from config.long_term_config import LONG_TERM_CONFIG
            from config.short_term_config import SHORT_TERM_CONFIG
            
            sync_status = {
                'symbol_sync': (
                    LONG_TERM_CONFIG['symbol'] == TRADE_SYMBOL and 
                    SHORT_TERM_CONFIG['symbol'] == TRADE_SYMBOL
                ),
                'amount_sync': (
                    LONG_TERM_CONFIG['position_size'] == TRADE_AMOUNT and 
                    SHORT_TERM_CONFIG['position_size'] == TRADE_AMOUNT
                ),
                'dmr_period_sync': (
                    LONG_TERM_CONFIG['dmr_period'] == DMR_LONG_PERIOD and 
                    SHORT_TERM_CONFIG['dmr_period'] == DMR_SHORT_PERIOD
                )
            }
            
            sync_status['all_synced'] = all(sync_status.values())
            return sync_status
            
        except Exception as e:
            print(f"检查参数同步状态失败: {e}")
            return {'all_synced': False}
    
    def export_config_report(self, filename: Optional[str] = None) -> str:
        """导出配置报告"""
        if filename is None:
            filename = f"config_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        report = {
            'timestamp': datetime.now().isoformat(),
            'config_summary': self.get_config_summary(),
            'validation_results': self.validate_all_configs(),
            'sync_status': self.check_parameter_sync()
        }
        
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            print(f"配置报告已导出到: {filename}")
            return filename
        except Exception as e:
            print(f"导出配置报告失败: {e}")
            return ""

# 全局配置管理器实例
config_manager = ConfigManager()

# 便捷函数
def reload_all_configs():
    """重新加载所有配置"""
    return config_manager.reload_config('all')

def validate_configs():
    """验证所有配置"""
    return config_manager.validate_all_configs()

def check_sync_status():
    """检查同步状态"""
    return config_manager.check_parameter_sync()

if __name__ == "__main__":
    # 测试配置管理器
    print("=== 配置管理器测试 ===")
    
    # 验证配置
    validation_results = validate_configs()
    print(f"配置验证结果: {validation_results}")
    
    # 检查同步状态
    sync_status = check_sync_status()
    print(f"参数同步状态: {sync_status}")
    
    # 导出配置报告
    report_file = config_manager.export_config_report()
    print(f"配置报告: {report_file}")