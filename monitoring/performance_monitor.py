import pandas as pd
import numpy as np

class PerformanceMonitor:
    def __init__(self):
        self.trades = []  # 存储所有交易记录

    def add_trade(self, entry_time, exit_time, entry_price, exit_price, side, amount):
        """
        添加一笔交易记录
        :param entry_time: 入场时间
        :param exit_time: 出场时间
        :param entry_price: 入场价格
        :param exit_price: 出场价格
        :param side: 交易方向（做多/做空）
        :param amount: 交易数量
        """
        trade = {
            'entry_time': entry_time,
            'exit_time': exit_time,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'side': side,
            'amount': amount
        }
        self.trades.append(trade)

    def calculate_returns(self):
        """
        计算每笔交易的收益率
        :return: 包含每笔交易收益率的Series
        """
        df = pd.DataFrame(self.trades)
        df['return'] = np.where(df['side'] == 'long',
                                (df['exit_price'] - df['entry_price']) / df['entry_price'],
                                (df['entry_price'] - df['exit_price']) / df['entry_price'])
        return df['return']

    def calculate_sharpe_ratio(self, risk_free_rate=0.02):
        """
        计算夏普比率
        :param risk_free_rate: 无风险利率
        :return: 夏普比率
        """
        returns = self.calculate_returns()
        sharpe_ratio = (returns.mean() - risk_free_rate) / returns.std()
        return sharpe_ratio

    def calculate_max_drawdown(self):
        """
        计算最大回撤
        :return: 最大回撤比例
        """
        returns = self.calculate_returns()
        cumulative_returns = (1 + returns).cumprod()
        peak = cumulative_returns.expanding(min_periods=1).max()
        drawdown = (cumulative_returns / peak) - 1
        return drawdown.min()