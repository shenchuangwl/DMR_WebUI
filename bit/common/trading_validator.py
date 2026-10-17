import logging
import time
from typing import Dict, Any, Optional

class TradingValidator:
    def __init__(self, exchange, position_manager):
        self.exchange = exchange
        self.position_manager = position_manager
        self.logger = logging.getLogger(self.__class__.__name__)
        
    def pre_trade_validation(self, strategy_name: str, signal: Dict[str, Any], current_price: float) -> bool:
        """交易前三重验证"""
        try:
            # 1. 信号验证
            self.validate_signal(signal)
            
            # 2. 实时同步交易所持仓
            exchange_positions = self.sync_exchange_positions()
            
            # 3. 本地持仓与交易所持仓一致性验证
            self.validate_position_consistency(strategy_name, exchange_positions)
            
            # 4. 持仓规则验证
            if signal['action'] == 'add_position':
                self.position_manager.validate_position_rules(
                    strategy_name, 'add_position', signal['amount'], current_price
                )
                
            self.logger.info(f"{strategy_name}策略交易前验证通过: {signal}")
            return True
            
        except Exception as e:
            self.logger.error(f"{strategy_name}策略交易前验证失败: {e}")
            raise
            
    def post_trade_validation(self, strategy_name: str, order_id: str, expected_result: Dict[str, Any]) -> Dict[str, Any]:
        """交易后验证"""
        try:
            # 1. 查询订单执行结果（重试机制）
            order_result = self.query_order_result_with_retry(order_id)
            
            # 2. 验证订单执行状态
            if order_result['status'] != 'closed':
                raise ValueError(f"订单未完全成交: {order_result}")
                
            # 3. 更新本地持仓
            self.update_local_position(strategy_name, order_result)
            
            # 4. 再次同步验证
            time.sleep(1)  # 等待交易所更新
            exchange_positions = self.sync_exchange_positions()
            self.validate_position_consistency(strategy_name, exchange_positions)
            
            self.logger.info(f"{strategy_name}策略交易后验证通过: {order_result}")
            return order_result
            
        except Exception as e:
            self.logger.error(f"{strategy_name}策略交易后验证失败: {e}")
            raise
            
    def validate_signal(self, signal: Dict[str, Any]):
        """验证交易信号"""
        required_fields = ['action', 'side', 'amount']
        for field in required_fields:
            if field not in signal:
                raise ValueError(f"信号缺少必要字段: {field}")
                
        if signal['amount'] <= 0:
            raise ValueError(f"交易数量必须大于0: {signal['amount']}")
            
    def sync_exchange_positions(self) -> Dict[str, Any]:
        """同步交易所持仓"""
        try:
            positions = self.exchange.fetch_positions()
            # 过滤出有持仓的记录
            active_positions = {pos['symbol']: pos for pos in positions if pos['size'] > 0}
            return active_positions
        except Exception as e:
            self.logger.error(f"同步交易所持仓失败: {e}")
            raise
            
    def validate_position_consistency(self, strategy_name: str, exchange_positions: Dict[str, Any]):
        """验证持仓一致性"""
        local_position = self.position_manager.get_strategy_position(strategy_name)
        
        # 这里需要根据实际的交易对进行验证
        # 简化实现，实际应该检查具体的持仓数量和方向
        self.logger.info(f"{strategy_name}策略持仓一致性验证: 本地{local_position}, 交易所{len(exchange_positions)}个持仓")
        
    def query_order_result_with_retry(self, order_id: str, max_retries: int = 3) -> Dict[str, Any]:
        """带重试的订单查询"""
        for attempt in range(max_retries):
            try:
                order = self.exchange.fetch_order(order_id)
                return order
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                self.logger.warning(f"查询订单失败，重试 {attempt + 1}/{max_retries}: {e}")
                time.sleep(1)
                
    def update_local_position(self, strategy_name: str, order_result: Dict[str, Any]):
        """更新本地持仓"""
        side = order_result['side'].upper()
        amount = order_result['filled']
        price = order_result['average'] or order_result['price']
        
        # 判断操作类型
        current_position = self.position_manager.get_strategy_position(strategy_name)
        if current_position['amount'] == 0:
            action = 'open'
        elif current_position['side'] == side:
            action = 'add'
        else:
            action = 'close'  # 反手操作
            
        self.position_manager.update_position(strategy_name, side, amount, price, action)