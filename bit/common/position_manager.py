import logging
import time
from datetime import datetime
from typing import Dict, Any, Optional

class UnifiedPositionManager:
    def __init__(self):
        self.strategies = {
            'long_term': {
                'fund_pool': 20.0,  # 基础资金池20U
                'positions': {},
                'max_add_times': 1,  # 更新：最大加仓1次
                'current_add_times': 0,
                'base_position_size': 20.0,
                'current_position': {'side': None, 'amount': 0, 'entry_price': 0}
            },
            'short_term': {
                'fund_pool': 20.0,  # 基础资金池20U
                'positions': {},
                'max_add_times': 1,  # 更新：最大加仓1次
                'current_add_times': 0,
                'base_position_size': 20.0,
                'current_position': {'side': None, 'amount': 0, 'entry_price': 0}
            }
        }
        self.logger = logging.getLogger(self.__class__.__name__)
        
    def validate_position_rules(self, strategy_name: str, action: str, amount: float, current_price: float = None) -> bool:
        """验证加仓/持仓规则"""
        strategy = self.strategies[strategy_name]
        
        # 检查加仓次数限制（最大1次）
        if action == 'add_position':
            if strategy['current_add_times'] >= strategy['max_add_times']:
                raise ValueError(f"{strategy_name}策略已达到最大加仓次数限制(1次)")
            
            # 检查是否为浮盈加仓
            if not self.is_profitable_position(strategy_name, current_price):
                raise ValueError(f"{strategy_name}策略只允许浮盈时加仓")
            
        # 检查对冲状态下的等额要求
        if self.is_hedging_state():
            self.validate_hedge_balance(strategy_name, action, amount)
            
        return True
        
    def is_profitable_position(self, strategy_name: str, current_price: float) -> bool:
        """检查当前持仓是否盈利"""
        strategy = self.strategies[strategy_name]
        position = strategy['current_position']
        
        if position['side'] is None or position['amount'] == 0:
            return False
            
        if position['side'] == 'LONG':
            return current_price > position['entry_price']
        elif position['side'] == 'SHORT':
            return current_price < position['entry_price']
            
        return False
        
    def is_hedging_state(self) -> bool:
        """检查是否处于对冲状态"""
        long_position = self.strategies['long_term']['current_position']
        short_position = self.strategies['short_term']['current_position']
        
        return (long_position['amount'] > 0 and short_position['amount'] > 0)
        
    def validate_hedge_balance(self, strategy_name: str, action: str, amount: float):
        """验证对冲状态下的多空等额要求"""
        long_amount = self.strategies['long_term']['current_position']['amount']
        short_amount = self.strategies['short_term']['current_position']['amount']
        
        # 计算操作后的持仓
        if strategy_name == 'long_term':
            new_long_amount = long_amount + amount if action in ['open_long', 'add_position'] else long_amount - amount
            new_short_amount = short_amount
        else:
            new_long_amount = long_amount
            new_short_amount = short_amount + amount if action in ['open_short', 'add_position'] else short_amount - amount
            
        # 检查等额要求（允许5%误差）
        if abs(new_long_amount - new_short_amount) > max(new_long_amount, new_short_amount) * 0.05:
            raise ValueError(f"对冲状态下多空不等额: 多仓{new_long_amount:.2f}U vs 空仓{new_short_amount:.2f}U")
            
    def update_position(self, strategy_name: str, side: str, amount: float, price: float, action: str):
        """更新持仓信息"""
        strategy = self.strategies[strategy_name]
        position = strategy['current_position']
        
        if action == 'open':
            position['side'] = side
            position['amount'] = amount
            position['entry_price'] = price
            strategy['current_add_times'] = 0
        elif action == 'add':
            if position['side'] == side:
                # 加权平均价格
                total_value = position['amount'] * position['entry_price'] + amount * price
                position['amount'] += amount
                position['entry_price'] = total_value / position['amount']
                strategy['current_add_times'] += 1
        elif action == 'close':
            position['side'] = None
            position['amount'] = 0
            position['entry_price'] = 0
            strategy['current_add_times'] = 0
            
        self.logger.info(f"{strategy_name}策略持仓更新: {action} {side} {amount}@{price}, 当前持仓: {position}")
        
    def get_strategy_position(self, strategy_name: str) -> Dict[str, Any]:
        """获取策略持仓信息"""
        return self.strategies[strategy_name]['current_position'].copy()
        
    def get_total_positions(self, strategy_name: str) -> float:
        """获取策略总持仓金额"""
        position = self.strategies[strategy_name]['current_position']
        return position['amount'] if position['side'] else 0
        
    def calculate_add_position_size(self, strategy_name: str) -> float:
        """计算加仓大小 - 20U+20U=40U"""
        strategy = self.strategies[strategy_name]
        base_size = strategy['base_position_size']
        
        # 第一次加仓：20U + 20U = 40U
        if strategy['current_add_times'] == 0:
            return base_size
        else:
            raise ValueError(f"{strategy_name}策略已达到最大加仓次数")
            
    def detect_position_anomaly(self) -> list:
        """检测持仓异常"""
        anomalies = []
        
        # 检查对冲不平衡
        if self.is_hedging_state():
            long_amount = self.get_total_positions('long_term')
            short_amount = self.get_total_positions('short_term')
            imbalance_ratio = abs(long_amount - short_amount) / max(long_amount, short_amount, 1)
            
            if imbalance_ratio > 0.1:  # 超过10%不平衡
                anomalies.append(f"对冲不平衡: 多仓{long_amount:.2f}U vs 空仓{short_amount:.2f}U")
                
        # 检查加仓溢出
        for strategy_name, strategy in self.strategies.items():
            if strategy['current_add_times'] > strategy['max_add_times']:
                anomalies.append(f"{strategy_name}策略加仓溢出: {strategy['current_add_times']}/{strategy['max_add_times']}")
                
        return anomalies