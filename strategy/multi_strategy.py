from config.config import SYMBOL, POSITION_SIZE
import logging

class MultiStrategy:
    def __init__(self, order_executor):
        self.strategies = []
        self.order_executor = order_executor
        self.logger = logging.getLogger('TradingBot')

    def add_strategy(self, strategy):
        self.strategies.append(strategy)

    def execute_strategies(self):
        for strategy in self.strategies:
            try:
                results = strategy.run_strategy()
                self.logger.info("Strategy execution completed")
            except Exception as e:
                self.logger.error(f"Error executing strategy: {e}")