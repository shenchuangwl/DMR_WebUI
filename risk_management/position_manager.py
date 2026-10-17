from config.testnet_config import (
    POSITION_SIZE,
    STOP_LOSS_PERCENTAGE,
    TAKE_PROFIT_PERCENTAGE,
)

class PositionManager:
    def __init__(self):
        pass

    def calculate_position_size(self, current_price):
        """
        计算仓位大小
        :param current_price: 当前价格
        :return: 仓位大小（数量）
        """
        return POSITION_SIZE / current_price

    def set_stop_loss(self, entry_price):
        """
        设置止损价格
        :param entry_price: 入场价格
        :return: 止损价格
        """
        return entry_price * (1 - STOP_LOSS_PERCENTAGE)

    def set_take_profit(self, entry_price):
        """
        设置止盈价格
        :param entry_price: 入场价格
        :return: 止盈价格
        """
        return entry_price * (1 + TAKE_PROFIT_PERCENTAGE)