"""
配置同步自动化测试
"""
import unittest
import sys
import os

# 添加项目根目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class TestConfigSync(unittest.TestCase):
    """配置同步测试类"""
    
    def setUp(self):
        """测试前准备"""
        # 重新加载配置以确保最新状态
        from utils.config_manager import reload_all_configs
        reload_all_configs()
    
    def test_parameter_import(self):
        """测试参数导入"""
        # 测试主配置参数
        from config.config import TRADE_SYMBOL, TRADE_AMOUNT, DMR_LONG_PERIOD, DMR_SHORT_PERIOD
        
        self.assertIsNotNone(TRADE_SYMBOL)
        self.assertIsNotNone(TRADE_AMOUNT)
        self.assertIsNotNone(DMR_LONG_PERIOD)
        self.assertIsNotNone(DMR_SHORT_PERIOD)
        
        print(f"主配置参数导入成功: {TRADE_SYMBOL}, {TRADE_AMOUNT}, {DMR_LONG_PERIOD}, {DMR_SHORT_PERIOD}")
    
    def test_long_term_config_sync(self):
        """测试长周期配置同步"""
        from config.config import TRADE_SYMBOL, TRADE_AMOUNT, DMR_LONG_PERIOD
        from config.long_term_config import LONG_TERM_CONFIG
        
        # 验证参数同步
        self.assertEqual(LONG_TERM_CONFIG['symbol'], TRADE_SYMBOL)
        self.assertEqual(LONG_TERM_CONFIG['position_size'], TRADE_AMOUNT)
        self.assertEqual(LONG_TERM_CONFIG['dmr_period'], DMR_LONG_PERIOD)
        
        print("长周期配置同步测试通过")
    
    def test_short_term_config_sync(self):
        """测试短周期配置同步"""
        from config.config import TRADE_SYMBOL, TRADE_AMOUNT, DMR_SHORT_PERIOD
        from config.short_term_config import SHORT_TERM_CONFIG
        
        # 验证参数同步
        self.assertEqual(SHORT_TERM_CONFIG['symbol'], TRADE_SYMBOL)
        self.assertEqual(SHORT_TERM_CONFIG['position_size'], TRADE_AMOUNT)
        self.assertEqual(SHORT_TERM_CONFIG['dmr_period'], DMR_SHORT_PERIOD)
        
        print("短周期配置同步测试通过")
    
    def test_config_validation(self):
        """测试配置验证"""
        from utils.config_manager import validate_configs
        
        results = validate_configs()
        
        # 所有配置都应该验证通过
        for config_name, result in results.items():
            self.assertTrue(result, f"{config_name} 配置验证失败")
        
        print("配置验证测试通过")
    
    def test_parameter_consistency(self):
        """测试参数一致性"""
        from utils.config_manager import check_sync_status
        
        sync_status = check_sync_status()
        
        # 所有参数都应该同步
        self.assertTrue(sync_status['all_synced'], f"参数同步失败: {sync_status}")
        
        print("参数一致性测试通过")
    
    def test_hot_reload(self):
        """测试热更新功能"""
        from utils.config_manager import config_manager
        
        # 测试重新加载
        result = config_manager.reload_config('all')
        self.assertTrue(result, "配置热更新失败")
        
        # 验证重新加载后的一致性
        sync_status = config_manager.check_parameter_sync()
        self.assertTrue(sync_status['all_synced'], "热更新后参数不一致")
        
        print("热更新测试通过")

if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)