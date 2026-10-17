import time
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

from bit.strategy.bit_short_term.data_fetcher import ShortTermDataFetcher
from bit.config.bit_short_term_config import SHORT_TERM_CONFIG
from bit.common.time_utils import TimeInfo, TimeSource, TimePriorityManager

class ShortTermOrderExecutor:
    def __init__(self, exchange, position_manager=None):
        # 初始化交易所对象
        self.exchange = exchange
        self.position_manager = position_manager  # 保持对 position_manager 的引用，可能其他地方需要
        # 创建数据获取器用于时间同步
        self.data_fetcher = ShortTermDataFetcher()
        # 获取短期配置
        self.config = SHORT_TERM_CONFIG
        # 获取交易对规则
        self.market_info = {}
        try:
            # 确保时间同步
            self.data_fetcher.sync_time(force=True)
            markets = self.exchange.load_markets()
            symbol = self.config['symbol']
            if symbol in markets:
                self.market_info[symbol] = markets[symbol]
                print(f"已加载{symbol}交易规则: 最小下单金额={markets[symbol].get('limits', {}).get('cost', {}).get('min', 'unknown')}")
        except Exception as e:
            print(f"加载市场信息失败: {e}")

    def place_market_order(self, side, amount, position_side=None):
        """下市价单，返回(order, time_info)"""
        try:
            self.data_fetcher.sync_time()
            symbol = self.config['symbol']
            
            # 构建订单参数
            params = {}
            if position_side:
                params['positionSide'] = position_side
            
            # 添加策略标识到clientOrderId
            strategy_id = self.config.get('strategy_id', 'DMR26_SHORT')
            timestamp = int(time.time() * 1000)
            client_order_id = f"{strategy_id}_{side}_{position_side}_{timestamp}"
            params['clientOrderId'] = client_order_id[:36]  # 确保不超过交易所限制
            
            # 修正：移除多余的None参数
            order = self.exchange.create_market_order(symbol, side, amount, None, params)
            print(f"短期策略市价单已下达: {side} {amount} {symbol} (positionSide: {position_side}, clientOrderId: {client_order_id[:36]})")
            
            # 从订单响应中提取实际成交时间
            time_info = self._extract_trade_time(order)
            if time_info:
                print(f"  >> 成交时间: {time_info.iso_format} (来源: {time_info.source.value})")
            
            return order, time_info
        except Exception as e:
            # 捕获并处理 "ReduceOnly Order is rejected" 错误
            if '"code":-2022' in str(e) or "ReduceOnly Order is rejected" in str(e):
                # 这是一个预期的错误：当策略状态与交易所状态不同步时（例如，尝试平掉一个不存在的仓位）
                print(f"警告：平仓指令被交易所拒绝，这通常是因为仓位已经不存在。这是一个常见的状态同步问题，策略将继续执行。")
                print(f"  > 原始错误: {e}")
            else:
                # 其他未知错误，需要记录下来
                print(f"短期策略下单失败: {e}")
            return None, None

    def _extract_trade_time(self, order):
        """
        从币安订单响应中提取实际成交时间
        
        Args:
            order: 币安订单响应对象
            
        Returns:
            TimeInfo对象，包含实际成交时间及来源标记
        """
        if not order:
            return None
        
        # 使用TimePriorityManager从订单中提取时间
        time_info = TimePriorityManager.create_from_binance_order(order)
        
        if time_info:
            return time_info
        else:
            # 如果无法从订单中提取时间，使用当前系统时间
            print(f"  >> 警告: 无法从订单中提取成交时间，使用系统时间")
            return TimePriorityManager.create_system_time("订单无时间信息，使用系统时间")
    
    def open_long(self, price, size):
        """开多仓，返回(order, time_info)"""
        print(f"短期策略准备开多仓: 价格={price}, 数量={size}")
        order, time_info = self.place_market_order('buy', size, 'LONG')
        if order:
            print(f"短期策略开多仓成功: {order.get('id')}")
        else:
            print(f"短期策略开多仓失败")
        return order, time_info

    def open_short(self, price, size):
        """开空仓，返回(order, time_info)"""
        print(f"短期策略准备开空仓: 价格={price}, 数量={size}")
        order, time_info = self.place_market_order('sell', size, 'SHORT')
        if order:
            print(f"短期策略开空仓成功: {order.get('id')}")
        else:
            print(f"短期策略开空仓失败")
        return order, time_info

    def close_long(self, amount, strategy_position):
        """平多仓 - 基于策略自身持仓记录，返回(order, time_info)"""
        try:
            # 验证策略自身的持仓记录，而不是查询全局持仓
            if strategy_position and strategy_position.get('side') == 'long' and amount > 0:
                print(f"根据策略持仓记录，确认平多仓，数量: {amount}")
                return self.place_market_order('sell', amount, 'LONG')
            
            print("警告：短周期策略尝试平多仓，但策略持仓记录不匹配或数量无效。")
            print(f"策略持仓: {strategy_position}, 计划平仓数量: {amount}")
            return None, None
        except Exception as e:
            print(f"平多仓失败: {e}")
            return None, None

    def close_short(self, amount, strategy_position):
        """平空仓 - 基于策略自身持仓记录，返回(order, time_info)"""
        try:
            # 验证策略自身的持仓记录，而不是查询全局持仓
            if strategy_position and strategy_position.get('side') == 'short' and amount > 0:
                print(f"根据策略持仓记录，确认平空仓，数量: {amount}")
                return self.place_market_order('buy', amount, 'SHORT')
                
            print("警告：短周期策略尝试平空仓，但策略持仓记录不匹配或数量无效。")
            print(f"策略持仓: {strategy_position}, 计划平仓数量: {amount}")
            return None, None
        except Exception as e:
            print(f"平空仓失败: {e}")
            return None, None

    def get_position_size(self):
        """获取当前持仓大小"""
        try:
            if self.position_manager:
                current_position = self.position_manager.get_current_position()
                if current_position:
                    return float(current_position['contracts'])
            return 0
        except Exception as e:
            print(f"获取持仓失败: {e}")
            return 0
