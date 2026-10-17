import logging


class TradingLogger:
    def __init__(self, name='DMRStrategy'):
        self.logger = logging.getLogger(name)
        self.setup_handlers()
        
    def setup_handlers(self):
        """设置日志处理器"""
        # 文件处理器 - 详细日志
        file_handler = logging.FileHandler('dmr_strategy_detailed.log')
        file_handler.setLevel(logging.DEBUG)
        
        # 文件处理器 - 交易日志
        trade_handler = logging.FileHandler('dmr_trades.log')
        trade_handler.setLevel(logging.INFO)
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # 格式化器
        detailed_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
        )
        trade_formatter = logging.Formatter(
            '%(asctime)s - TRADE - %(message)s'
        )
        
        file_handler.setFormatter(detailed_formatter)
        trade_handler.setFormatter(trade_formatter)
        console_handler.setFormatter(detailed_formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(trade_handler)
        self.logger.addHandler(console_handler)
    
    def log_signal(self, quadrant, timeframe, signal_type, dmr_value, comment):
        """记录交易信号"""
        self.logger.info(f"SIGNAL | {quadrant} | {timeframe} | {signal_type} | DMR:{dmr_value:.6f} | {comment}")
    
    def log_trade(self, action, symbol, amount, price, order_id, status):
        """记录交易执行"""
        self.logger.info(f"TRADE | {action} | {symbol} | {amount} | {price} | {order_id} | {status}")
    
    def log_error_with_context(self, error, context):
        """记录带上下文的错误"""
        self.logger.error(f"ERROR | {context} | {str(error)}")