import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from config.config import POSITION_SIZE, SYMBOL, TIMEFRAME_4H, TIMEFRAME_1H

class DMRCrossoverStrategy:
    def __init__(self, df, order_executor, commission=0.001):
        """
        初始化策略
        df: DataFrame，包含 'high', 'low', 'close' 的OHLCV数据
        order_executor: 交易执行器
        commission: 交易手续费率
        """
        self.df = df
        self.order_executor = order_executor
        self.commission = commission
        self.positions = pd.DataFrame(index=df.index)
        
    def calculate_dmr(self):
        """计算DMR指标"""
        # 1. 计算中间价
        self.df['dmr_midprice'] = (self.df['high'] + self.df['low']) / 2
        
        # 2. 计算中间价比率
        self.df['dmr_ratio'] = self.df['dmr_midprice'] / self.df['dmr_midprice'].shift(1)
        
        # 3-5. 计算不同周期的移动平均
        self.df['dmr_avg6'] = self.df['dmr_ratio'].rolling(window=6).mean() - 1
        self.df['dmr_avg12'] = self.df['dmr_ratio'].rolling(window=12).mean() - 1
        self.df['dmr_avg26'] = self.df['dmr_ratio'].rolling(window=26).mean() - 1
        
    def resample_data(self):
        """重采样数据到4H和1H时间周期"""
        # 4H 数据重采样
        self.df_4h = self.df.resample(TIMEFRAME_4H).agg({
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        # 1H 数据重采样
        self.df_1h = self.df.resample(TIMEFRAME_1H).agg({
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
    def generate_signals(self):
        """生成交易信号"""
        # 4H 策略信号
        self.df_4h['signal_4h'] = 0
        # 当4H的12日均线数值由负值转为正值时开多
        self.df_4h.loc[(self.df_4h['dmr_avg12'].shift(1) < 0) & (self.df_4h['dmr_avg12'] > 0), 'signal_4h'] = 1
        # 当4H的12日均线数值由正值转为负值时开空
        self.df_4h.loc[(self.df_4h['dmr_avg12'].shift(1) > 0) & (self.df_4h['dmr_avg12'] < 0), 'signal_4h'] = -1
        
        # 1H 策略信号
        self.df_1h['signal_1h'] = 0
        # 当1H的26日均线数值由负值转为正值时开多
        self.df_1h.loc[(self.df_1h['dmr_avg26'].shift(1) < 0) & (self.df_1h['dmr_avg26'] > 0), 'signal_1h'] = 1
        # 当1H的26日均线数值由正值转为负值时开空
        self.df_1h.loc[(self.df_1h['dmr_avg26'].shift(1) > 0) & (self.df_1h['dmr_avg26'] < 0), 'signal_1h'] = -1
        
    def execute_trades(self):
        """执行交易策略"""
        # 获取当前时间
        current_time = datetime.now()
        
        # 检查当前K线是否已收盘
        is_hourly_close = current_time.minute < 3  # 允许3分钟的误差
        is_4h_close = is_hourly_close and current_time.hour % 4 == 0
        
        # 分别检查1H和4H策略的信号
        has_1h_signal = False
        has_4h_signal = False
        
        # 检查4H策略信号
        if len(self.df_4h) > 0:
            latest_4h_signal = self.df_4h['signal_4h'].iloc[-1]  # 获取4H最新信号
            print(f"4H策略检查 - 当前信号: {latest_4h_signal}")
            
            # 如果有有效信号且是4H收盘时间，执行4H策略
            if latest_4h_signal != 0 and is_4h_close:
                has_4h_signal = True
                if latest_4h_signal == 1:  # 4H开多信号
                    # 如果有空仓先平掉
                    self.order_executor.close_position(SYMBOL, 'SHORT')
                    # 开多仓
                    self.order_executor.open_long(SYMBOL, POSITION_SIZE)
                    print(f"4H策略: 开多仓 {POSITION_SIZE} USDT")
                    
                # 在execute_trades方法中修改开仓操作
                
                # 例如，对于4H策略开空信号：
                if latest_4h_signal == -1:  # 4H开空信号
                    # 如果有多仓先平掉
                    self.order_executor.close_position(SYMBOL, 'LONG')
                    # 获取当前价格
                    current_price = self.df['close'].iloc[-1]
                    # 开空仓，传递当前价格
                    self.order_executor.open_short(SYMBOL, POSITION_SIZE, price=current_price)
                    print(f"4H策略: 开空仓 {POSITION_SIZE} USDT，价格={current_price}")
                
                # 对1H策略也做相同修改
        
        # 检查1H策略信号
        if len(self.df_1h) > 0:
            latest_1h_signal = self.df_1h['signal_1h'].iloc[-1]  # 获取1H最新信号
            print(f"1H策略检查 - 当前信号: {latest_1h_signal}")
            
            # 如果有有效信号且是1H收盘时间，执行1H策略
            if latest_1h_signal != 0 and is_hourly_close:
                has_1h_signal = True
                if latest_1h_signal == 1:  # 1H开多信号
                    # 如果有空仓先平掉
                    self.order_executor.close_position(SYMBOL, 'SHORT')
                    # 开多仓
                    self.order_executor.open_long(SYMBOL, POSITION_SIZE)
                    print(f"1H策略: 开多仓 {POSITION_SIZE} USDT")
                    
                elif latest_1h_signal == -1:  # 1H开空信号
                    # 如果有多仓先平掉
                    self.order_executor.close_position(SYMBOL, 'LONG')
                    # 开空仓
                    self.order_executor.open_short(SYMBOL, POSITION_SIZE)
                    print(f"1H策略: 开空仓 {POSITION_SIZE} USDT")
        
        # 如果没有任何有效信号
        if not has_1h_signal and not has_4h_signal:
            print("当前没有有效的交易信号或不是K线收盘时间，不执行交易")

    def run_strategy(self):
        """运行完整策略"""
        self.calculate_dmr()
        self.resample_data()
        self.generate_signals()
        self.execute_trades()
        return self.df

if __name__ == "__main__":
    pass