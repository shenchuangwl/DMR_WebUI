import time
from datetime import datetime

from execution.order_executor import OrderExecutor


class EnhancedOrderExecutor(OrderExecutor):
    def __init__(self, exchange):
        super().__init__(exchange)
        self.order_history = []  # 订单历史
        self.retry_queue = []    # 重试队列
        self.max_retries = 3     # 最大重试次数
        
    def execute_order_with_retry(self, order_type, symbol, amount, price=None, max_retries=3):
        """带重试机制的订单执行"""
        for attempt in range(max_retries):
            try:
                # 生成唯一订单ID
                order_id = f"{order_type}_{symbol}_{int(time.time())}_{attempt}"
                
                # 执行订单
                if order_type == 'limit_buy':
                    result = self.place_limit_order('buy', amount, price)
                elif order_type == 'limit_sell':
                    result = self.place_limit_order('sell', amount, price)
                elif order_type == 'market_buy':
                    result = self.place_market_order('buy', amount)
                elif order_type == 'market_sell':
                    result = self.place_market_order('sell', amount)
                
                if result:
                    # 记录成功订单
                    self.order_history.append({
                        'id': order_id,
                        'type': order_type,
                        'symbol': symbol,
                        'amount': amount,
                        'price': price,
                        'timestamp': datetime.now(),
                        'status': 'success',
                        'exchange_order_id': result.get('id')
                    })
                    return result
                    
            except Exception as e:
                self.logger.error(f"订单执行失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # 指数退避
                else:
                    # 记录失败订单
                    self.order_history.append({
                        'id': order_id,
                        'type': order_type,
                        'symbol': symbol,
                        'amount': amount,
                        'price': price,
                        'timestamp': datetime.now(),
                        'status': 'failed',
                        'error': str(e)
                    })
        
        return None
    
    def validate_order_parameters(self, symbol, amount, price=None):
        """验证订单参数"""
        # 检查最小订单金额
        if symbol in self.market_info:
            min_cost = self.market_info[symbol].get('limits', {}).get('cost', {}).get('min', 0)
            if amount < min_cost:
                raise ValueError(f"订单金额 {amount} 小于最小限制 {min_cost}")
        
        # 检查价格精度
        if price is not None:
            price_precision = self.market_info[symbol].get('precision', {}).get('price', 8)
            if len(str(price).split('.')[-1]) > price_precision:
                raise ValueError(f"价格精度超出限制: {price}")
        
        return True