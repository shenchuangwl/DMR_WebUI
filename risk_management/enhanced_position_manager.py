from datetime import datetime


class EnhancedPositionManager:
    def __init__(self):
        self.positions = {}  # 持仓跟踪
        self.max_position_size = 100  # 最大持仓金额
        self.max_daily_trades = 50    # 每日最大交易次数
        self.daily_trade_count = 0
        self.last_reset_date = datetime.now().date()
        
    def validate_new_position(self, symbol, side, amount):
        """验证新仓位是否符合风控要求"""
        # 重置每日交易计数
        current_date = datetime.now().date()
        if current_date != self.last_reset_date:
            self.daily_trade_count = 0
            self.last_reset_date = current_date
        
        # 检查每日交易次数限制
        if self.daily_trade_count >= self.max_daily_trades:
            raise ValueError(f"已达到每日最大交易次数限制: {self.max_daily_trades}")
        
        # 检查持仓金额限制
        current_exposure = self.get_total_exposure(symbol)
        if current_exposure + amount > self.max_position_size:
            raise ValueError(f"新仓位将超出最大持仓限制: {self.max_position_size}")
        
        # 检查冲突持仓
        if self.has_conflicting_position(symbol, side):
            raise ValueError(f"存在冲突持仓，无法开设 {side} 仓位")
        
        return True
    
    def update_position(self, symbol, side, amount, price, action):
        """更新持仓记录"""
        if symbol not in self.positions:
            self.positions[symbol] = {'long': 0, 'short': 0, 'avg_price': 0}
        
        if action == 'open':
            self.positions[symbol][side] += amount
            self.daily_trade_count += 1
        elif action == 'close':
            self.positions[symbol][side] = max(0, self.positions[symbol][side] - amount)
            self.daily_trade_count += 1
        
        # 记录到日志
        self.log_position_change(symbol, side, amount, price, action)
    
    def get_position_summary(self):
        """获取持仓摘要"""
        summary = {
            'total_positions': len(self.positions),
            'daily_trades': self.daily_trade_count,
            'positions': self.positions.copy()
        }
        return summary