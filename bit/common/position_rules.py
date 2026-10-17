import logging
from typing import Dict, Any

class PositionRulesEngine:
    def __init__(self):
        from config.config import TRADE_AMOUNT
        self.base_position_size = TRADE_AMOUNT  # 20u
        self.max_total_position = TRADE_AMOUNT * 2  # 40u (两个策略各20u)
        self.logger = logging.getLogger(self.__class__.__name__)
        
    def validate_total_position_limit(self, current_total: float, new_position: float) -> bool:
        """验证总仓位限制 - 基于两个独立策略的总和"""
        if current_total + new_position > self.max_total_position:
            raise ValueError(
                f"总仓位超限: 当前{current_total:.2f}u + 新增{new_position:.2f}u > "
                f"最大{self.max_total_position}u (两个独立策略各{self.base_position_size}u)"
            )
        
        self.logger.info(
            f"总仓位验证通过: 当前{current_total:.2f}u + 新增{new_position:.2f}u <= "
            f"最大{self.max_total_position}u"
        )
        return True
        
    def validate_add_position(self, current_position: Dict[str, Any], current_price: float, add_times: int) -> bool:
        """验证加仓规则"""
        # 检查加仓次数
        if add_times >= self.max_add_times:
            raise ValueError(f"已达到最大加仓次数限制({self.max_add_times}次)")
            
        # 检查是否有持仓
        if current_position['amount'] == 0:
            raise ValueError("无持仓时不能加仓")
            
        # 检查是否浮盈
        profit_ratio = self.calculate_profit_ratio(current_position, current_price)
        if profit_ratio <= 0:
            raise ValueError(f"当前亏损{profit_ratio:.2%}，只允许浮盈时加仓")
            
        self.logger.info(f"加仓验证通过: 当前盈利{profit_ratio:.2%}，加仓次数{add_times}/{self.max_add_times}")
        return True
        
    def calculate_profit_ratio(self, position: Dict[str, Any], current_price: float) -> float:
        """计算盈利比例"""
        if position['amount'] == 0 or position['entry_price'] == 0:
            return 0
            
        if position['side'] == 'LONG':
            return (current_price - position['entry_price']) / position['entry_price']
        elif position['side'] == 'SHORT':
            return (position['entry_price'] - current_price) / position['entry_price']
            
        return 0
        
    def calculate_add_position_size(self, current_position: Dict[str, Any]) -> float:
        """计算加仓大小 - 20U+20U=40U模式"""
        if current_position['amount'] == 0:
            raise ValueError("无持仓时不能计算加仓大小")
            
        # 固定加仓20U，使总仓位变为40U
        return self.base_position_size
        
    def validate_hedge_balance(self, long_amount: float, short_amount: float) -> bool:
        """验证对冲平衡"""
        if long_amount == 0 or short_amount == 0:
            return True  # 非对冲状态
            
        imbalance_ratio = abs(long_amount - short_amount) / max(long_amount, short_amount)
        if imbalance_ratio > self.hedge_tolerance:
            raise ValueError(f"对冲不平衡超过容忍度: 多仓{long_amount:.2f}U vs 空仓{short_amount:.2f}U")
            
        return True