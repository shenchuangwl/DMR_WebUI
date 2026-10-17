import time
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

from strategy.long_term.data_fetcher import LongTermDataFetcher
from config.long_term_config import LONG_TERM_CONFIG

class LongTermOrderExecutor:
    def __init__(self, exchange, position_manager=None):
        # 初始化交易所对象
        self.exchange = exchange
        self.position_manager = position_manager  # 保持对 position_manager 的引用，可能其他地方需要
        # 创建数据获取器用于时间同步
        self.data_fetcher = LongTermDataFetcher()
        # 获取长期配置
        self.config = LONG_TERM_CONFIG
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
        """下市价单"""
        try:
            self.data_fetcher.sync_time()
            symbol = self.config['symbol']
            
            # 构建订单参数
            params = {}
            if position_side:
                params['positionSide'] = position_side
            
            # 使用正确的策略ID
            strategy_id = self.config.get('strategy_id', 'DMR12_LONG')
            timestamp = int(time.time() * 1000)
            client_order_id = f"{strategy_id}_{side}_{position_side}_{timestamp}"
            params['clientOrderId'] = client_order_id[:36]  # 确保不超过交易所限制
            
            # 修正：移除多余的None参数
            order = self.exchange.create_market_order(symbol, side, amount, None, params)
            print(f"长期策略市价单已下达: {side} {amount} {symbol} (positionSide: {position_side}, clientOrderId: {client_order_id[:36]})")
            return order
        except Exception as e:
            # 捕获并处理 "ReduceOnly Order is rejected" 错误
            if '"code":-2022' in str(e) or "ReduceOnly Order is rejected" in str(e):
                # 这是一个预期的错误：当策略状态与交易所状态不同步时（例如，尝试平掉一个不存在的仓位）
                print(f"警告：平仓指令被交易所拒绝，这通常是因为仓位已经不存在。这是一个常见的状态同步问题，策略将继续执行。")
                print(f"  > 原始错误: {e}")
            else:
                # 其他未知错误，需要记录下来
                print(f"长期策略下单失败: {e}")
            return None

    def open_long(self, price, size):
        """开多仓"""
        return self.place_market_order('buy', size, 'LONG')

    def open_short(self, price, size):
        """开空仓"""
        return self.place_market_order('sell', size, 'SHORT')

    def close_long(self, amount, strategy_position):
        """平多仓 - 基于策略自身持仓记录"""
        try:
            # 验证策略自身的持仓记录，而不是查询全局持仓
            if strategy_position and strategy_position.get('side') == 'long' and amount > 0:
                print(f"根据策略持仓记录，确认平多仓，数量: {amount}")
                return self.place_market_order('sell', amount, 'LONG')
            
            print("警告：长周期策略尝试平多仓，但策略持仓记录不匹配或数量无效。")
            print(f"策略持仓: {strategy_position}, 计划平仓数量: {amount}")
            return None
        except Exception as e:
            print(f"平多仓失败: {e}")
            return None
    
    def close_short(self, amount, strategy_position):
        """平空仓 - 基于策略自身持仓记录"""
        try:
            # 验证策略自身的持仓记录，而不是查询全局持仓
            if strategy_position and strategy_position.get('side') == 'short' and amount > 0:
                print(f"根据策略持仓记录，确认平空仓，数量: {amount}")
                return self.place_market_order('buy', amount, 'SHORT')

            print("警告：长周期策略尝试平空仓，但策略持仓记录不匹配或数量无效。")
            print(f"策略持仓: {strategy_position}, 计划平仓数量: {amount}")
            return None
        except Exception as e:
            print(f"平空仓失败: {e}")
            return None

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
