import pandas as pd
import numpy as np

class SignalGenerator:
    def __init__(self):
        # 初始化仓位状态
        self.position_open = False

    def generate_signal(self, historical_data):
        """
        生成交易信号
        :param historical_data: 历史数据（在此示例中未使用）
        :return: 1表示买入信号，0表示持有信号
        """
        if not self.position_open:
            self.position_open = True
            return 1  # 买入信号
        return 0  # 持有信号