import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

from config.short_term_config import SHORT_TERM_CONFIG

class ShortTermPositionManager:
    def __init__(self, exchange):
        self.exchange = exchange
        self.config = SHORT_TERM_CONFIG
        self.position_size = self.config['position_size']
        self.stop_loss_percentage = self.config.get('stop_loss_percentage', 0.01)
        self.take_profit_percentage = self.config.get('take_profit_percentage', 0.02)

    def calculate_position_size(self, current_price):
        """计算仓位大小"""
        return self.position_size / current_price

    def set_stop_loss(self, entry_price):
        """设置止损价格"""
        return entry_price * (1 - self.stop_loss_percentage)

    def set_take_profit(self, entry_price):
        """设置止盈价格"""
        return entry_price * (1 + self.take_profit_percentage)

    def get_current_position(self, side=None):
        """
        获取当前持仓.
        如果提供了side ('long' or 'short'), 则只返回该方向的持仓.
        否则, 返回第一个有效持仓.
        """
        try:
            symbol = self.config['symbol']
            positions = self.exchange.fetch_positions([symbol])
            
            # 过滤出有效持仓（合约数量不为0）
            active_positions = [pos for pos in positions if float(pos.get('contracts', 0)) != 0]
            
            if not active_positions:
                return None

            if side:
                # ccxt 将 positionSide 标准化为 'side'
                for p in active_positions:
                    if p.get('side') == side:
                        return p
                return None # 没有找到指定方向的持仓
            else:
                # 保持原有的行为，返回第一个有效仓位
                return active_positions[0]
                
        except Exception as e:
            print(f"获取短期持仓失败: {e}")
            return None

    def check_position_limits(self, new_position_size):
        """检查仓位限制"""
        max_position = self.config.get('max_position_size', self.position_size * 2)
        return abs(new_position_size) <= max_position