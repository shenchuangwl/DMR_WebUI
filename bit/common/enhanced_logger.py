import logging
import json
from datetime import datetime
from typing import Dict, Any, Optional

class EnhancedLogger:
    def __init__(self, strategy_name: str, log_file: str = None):
        self.strategy_name = strategy_name
        self.logger = self.setup_logger(log_file)
        
    def setup_logger(self, log_file: Optional[str] = None) -> logging.Logger:
        """设置增强日志记录器"""
        logger = logging.getLogger(f"Enhanced_{self.strategy_name}")
        logger.setLevel(logging.INFO)
        
        # 创建格式化器
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # 文件处理器
        if log_file:
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            
        return logger
        
    def log_signal_detection(self, signal: Dict[str, Any], market_data: Dict[str, Any]):
        """记录信号检测"""
        log_data = {
            'event_type': 'signal_detection',
            'timestamp': datetime.now().isoformat(),
            'strategy': self.strategy_name,
            'signal': signal,
            'market_data': {
                'price': market_data.get('price'),
                'dmr_value': market_data.get('dmr_value'),
                'volume': market_data.get('volume')
            }
        }
        
        self.logger.info(f"信号检测: {json.dumps(log_data, ensure_ascii=False)}")
        
    def log_position_status(self, position_before: Dict[str, Any], position_after: Dict[str, Any]):
        """记录持仓状态变化"""
        log_data = {
            'event_type': 'position_change',
            'timestamp': datetime.now().isoformat(),
            'strategy': self.strategy_name,
            'position_before': position_before,
            'position_after': position_after,
            'change_amount': position_after.get('amount', 0) - position_before.get('amount', 0)
        }
        
        self.logger.info(f"持仓变化: {json.dumps(log_data, ensure_ascii=False)}")
        
    def log_order_execution(self, order_request: Dict[str, Any], order_result: Dict[str, Any]):
        """记录订单执行"""
        log_data = {
            'event_type': 'order_execution',
            'timestamp': datetime.now().isoformat(),
            'strategy': self.strategy_name,
            'order_request': order_request,
            'order_result': {
                'id': order_result.get('id'),
                'status': order_result.get('status'),
                'filled': order_result.get('filled'),
                'average_price': order_result.get('average'),
                'fee': order_result.get('fee')
            }
        }
        
        self.logger.info(f"订单执行: {json.dumps(log_data, ensure_ascii=False)}")
        
    def log_complete_trade_cycle(self, signal: Dict[str, Any], position_before: Dict[str, Any], 
                                order_result: Dict[str, Any], position_after: Dict[str, Any], 
                                risk_metrics: Dict[str, Any]):
        """记录完整交易周期"""
        log_data = {
            'event_type': 'complete_trade_cycle',
            'timestamp': datetime.now().isoformat(),
            'strategy': self.strategy_name,
            'signal': signal,
            'position_before': position_before,
            'order_result': order_result,
            'position_after': position_after,
            'risk_metrics': risk_metrics,
            'profit_loss': self.calculate_pnl(position_before, position_after, order_result)
        }
        
        self.logger.info(f"完整交易周期: {json.dumps(log_data, ensure_ascii=False)}")
        
    def log_risk_alert(self, alert_type: str, description: str, severity: str, data: Dict[str, Any] = None):
        """记录风险告警"""
        alert_data = {
            'event_type': 'risk_alert',
            'timestamp': datetime.now().isoformat(),
            'strategy': self.strategy_name,
            'alert_type': alert_type,
            'description': description,
            'severity': severity,
            'data': data or {}
        }
        
        if severity == 'critical':
            self.logger.critical(f"严重风险告警: {json.dumps(alert_data, ensure_ascii=False)}")
        elif severity == 'high':
            self.logger.warning(f"高风险告警: {json.dumps(alert_data, ensure_ascii=False)}")
        else:
            self.logger.info(f"风险告警: {json.dumps(alert_data, ensure_ascii=False)}")
            
    def calculate_pnl(self, position_before: Dict[str, Any], position_after: Dict[str, Any], 
                     order_result: Dict[str, Any]) -> Dict[str, Any]:
        """计算盈亏"""
        # 简化的盈亏计算
        return {
            'realized_pnl': 0,  # 实际应该根据具体交易计算
            'unrealized_pnl': 0,
            'total_pnl': 0
        }