#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DMR四象限量化策略回测脚本
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os
import sys
from datetime import datetime, timedelta
import backtrader as bt

# 添加项目根目录到路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 导入策略和配置
from strategy.DMRQuadrantStrategy import DMRQuadrantStrategy
from config.config import SYMBOL, POSITION_SIZE, TIMEFRAME_SHORT
from data.data_fetcher import DataFetcher

class DMRQuadrantBacktest:
    """DMR四象限量化策略回测类"""
    
    def __init__(self, data_path=None, symbol=SYMBOL, initial_capital=10000):
        self.data_path = data_path
        self.symbol = symbol
        self.initial_capital = initial_capital
        self.results = None
        
    def load_data(self):
        """加载回测数据"""
        if self.data_path and os.path.exists(self.data_path):
            print(f"从文件加载数据: {self.data_path}")
            df = pd.read_csv(self.data_path, index_col=0, parse_dates=True)
        else:
            print("从API获取数据")
            # 初始化数据获取器
            fetcher = DataFetcher()
            # 获取历史数据
            df = fetcher.get_historical_data(self.symbol, TIMEFRAME_1H, limit=1000)
            # 保存数据
            if df is not None and self.data_path:
                os.makedirs(os.path.dirname(self.data_path), exist_ok=True)
                df.to_csv(self.data_path)
                
        return df
    
    def run_backtest_custom(self):
        """运行自定义回测"""
        # 加载数据
        df = self.load_data()
        if df is None:
            print("无法获取数据，回测中止")
            return None
            
        # 初始化资金和持仓
        capital = self.initial_capital
        position = 0
        position_price = 0
        trades = []
        
        # 创建结果DataFrame
        results = df.copy()
        results['capital'] = capital
        results['position'] = 0
        results['trade_type'] = ''
        
        # 创建一个模拟的OrderExecutor
        class MockOrderExecutor:
            def open_long(self, symbol, amount, price=None):
                return {'id': 'mock_long', 'price': price, 'amount': amount}
                
            def open_short(self, symbol, amount, price=None):
                return {'id': 'mock_short', 'price': price, 'amount': amount}
                
            def close_position(self, symbol, position_side):
                return {'id': 'mock_close', 'position_side': position_side}
        
        # 初始化策略
        mock_executor = MockOrderExecutor()
        strategy = DMRQuadrantStrategy(df, mock_executor)
        
        # 计算DMR指标
        strategy.calculate_dmr()
        strategy.resample_data()
        strategy.generate_signals()
        
        # 获取信号
        signals_4h = strategy.df_4h[['signal_4h', 'dmr_avg12', 'market_state']]
        signals_1h = strategy.df_1h[['signal_1h', 'dmr_avg26']]
        
        # 合并信号到原始数据
        results = results.join(signals_4h, how='left')
        results = results.join(signals_1h, how='left')
        
        # 前向填充NaN值
        results = results.ffill()
        
        # 模拟交易
        for i in range(1, len(results)):
            # 获取当前行
            row = results.iloc[i]
            prev_row = results.iloc[i-1]
            
            # 检查4H信号
            if row['signal_4h'] == 1 and prev_row['signal_4h'] != 1:  # 4H开多信号
                # 如果有空仓先平掉
                if position < 0:
                    profit = position * (position_price - row['close'])
                    capital += profit
                    trades.append({
                        'time': row.name,
                        'type': 'close_short',
                        'price': row['close'],
                        'profit': profit
                    })
                    position = 0
                
                # 开多仓
                position = POSITION_SIZE / row['close']
                position_price = row['close']
                results.loc[results.index[i], 'position'] = position
                results.loc[results.index[i], 'trade_type'] = 'buy_4h'
                trades.append({
                    'time': row.name,
                    'type': 'buy_4h',
                    'price': row['close'],
                    'position': position
                })
                
            elif row['signal_4h'] == -1 and prev_row['signal_4h'] != -1:  # 4H开空信号
                # 如果有多仓先平掉
                if position > 0:
                    profit = position * (row['close'] - position_price)
                    capital += profit
                    trades.append({
                        'time': row.name,
                        'type': 'close_long',
                        'price': row['close'],
                        'profit': profit
                    })
                    position = 0
                
                # 开空仓
                position = -POSITION_SIZE / row['close']
                position_price = row['close']
                results.loc[results.index[i], 'position'] = position
                results.loc[results.index[i], 'trade_type'] = 'sell_4h'
                trades.append({
                    'time': row.name,
                    'type': 'sell_4h',
                    'price': row['close'],
                    'position': position
                })
                
            # 检查1H信号
            elif row['signal_1h'] == 1 and prev_row['signal_1h'] != 1:  # 1H开多信号
                # 如果有空仓先平掉
                if position < 0:
                    profit = position * (position_price - row['close'])
                    capital += profit
                    trades.append({
                        'time': row.name,
                        'type': 'close_short',
                        'price': row['close'],
                        'profit': profit
                    })
                    position = 0
                
                # 开多仓
                position = POSITION_SIZE / row['close']
                position_price = row['close']
                results.loc[results.index[i], 'position'] = position
                results.loc[results.index[i], 'trade_type'] = 'buy_1h'
                trades.append({
                    'time': row.name,
                    'type': 'buy_1h',
                    'price': row['close'],
                    'position': position
                })
                
            elif row['signal_1h'] == -1 and prev_row['signal_1h'] != -1:  # 1H开空信号
                # 如果有多仓先平掉
                if position > 0:
                    profit = position * (row['close'] - position_price)
                    capital += profit
                    trades.append({
                        'time': row.name,
                        'type': 'close_long',
                        'price': row['close'],
                        'profit': profit
                    })
                    position = 0
                
                # 开空仓
                position = -POSITION_SIZE / row['close']
                position_price = row['close']
                results.loc[results.index[i], 'position'] = position
                results.loc[results.index[i], 'trade_type'] = 'sell_1h'
                trades.append({
                    'time': row.name,
                    'type': 'sell_1h',
                    'price': row['close'],
                    'position': position
                })
            
            # 更新资金
            if position != 0:
                # 计算未实现盈亏
                if position > 0:  # 多仓
                    unrealized_pnl = position * (row['close'] - position_price)
                else:  # 空仓
                    unrealized_pnl = position * (position_price - row['close'])
                    
                results.loc[results.index[i], 'capital'] = capital + unrealized_pnl
            else:
                results.loc[results.index[i], 'capital'] = capital
        
        # 最后平仓
        if position != 0:
            last_price = results['close'].iloc[-1]
            if position > 0:  # 多仓
                profit = position * (last_price - position_price)
            else:  # 空仓
                profit = position * (position_price - last_price)
                
            capital += profit
            trades.append({
                'time': results.index[-1],
                'type': 'close_final',
                'price': last_price,
                'profit': profit
            })
            
        # 计算回测指标
        trades_df = pd.DataFrame(trades)
        if not trades_df.empty:
            profit_trades = trades_df[trades_df['profit'] > 0]
            loss_trades = trades_df[trades_df['profit'] < 0]
            
            win_rate = len(profit_trades) / len(trades_df) if len(trades_df) > 0 else 0
            
            # 计算最大回撤
            results['drawdown'] = results['capital'].cummax() - results['capital']
            max_drawdown = results['drawdown'].max()
            max_drawdown_pct = max_drawdown / results['capital'].cummax().max() * 100
            
            # 计算收益率
            total_return = (capital - self.initial_capital) / self.initial_capital * 100
            
            # 计算夏普比率
            daily_returns = results['capital'].pct_change().dropna()
            sharpe_ratio = daily_returns.mean() / daily_returns.std() * np.sqrt(252) if daily_returns.std() != 0 else 0
            
            backtest_metrics = {
                'initial_capital': self.initial_capital,
                'final_capital': capital,
                'total_return_pct': total_return,
                'win_rate': win_rate,
                'trade_count': len(trades_df),
                'profit_trades': len(profit_trades),
                'loss_trades': len(loss_trades),
                'max_drawdown': max_drawdown,
                'max_drawdown_pct': max_drawdown_pct,
                'sharpe_ratio': sharpe_ratio
            }
        else:
            backtest_metrics = {
                'initial_capital': self.initial_capital,
                'final_capital': capital,
                'total_return_pct': 0,
                'win_rate': 0,
                'trade_count': 0,
                'profit_trades': 0,
                'loss_trades': 0,
                'max_drawdown': 0,
                'max_drawdown_pct': 0,
                'sharpe_ratio': 0
            }
            
        self.results = {
            'metrics': backtest_metrics,
            'trades': trades_df,
            'equity_curve': results['capital'],
            'full_results': results
        }
        
        return self.results
    
    def run_backtest_bt(self):
        """使用backtrader运行回测"""
        # 这里可以实现基于backtrader的回测
        # 由于需要额外的数据适配器和策略类，这里仅作为示例
        print("Backtrader回测功能暂未实现")
        return None
    
    def plot_results(self):
        """绘制回测结果"""
        if self.results is None:
            print("没有回测结果可绘制")
            return
            
        results = self.results['full_results']
        trades = self.results['trades']
        metrics = self.results['metrics']
        
        # 创建图形
        fig, axes = plt.subplots(3, 1, figsize=(12, 16), gridspec_kw={'height_ratios': [3, 1, 1]})
        
        # 绘制价格和资金曲线
        ax1 = axes[0]
        ax1.set_title('DMR四象限量化策略回测结果')
        ax1.plot(results.index, results['close'], label='价格', color='blue', alpha=0.5)
        ax1_right = ax1.twinx()
        ax1_right.plot(results.index, results['capital'], label='资金曲线', color='green')
        
        # 标记交易点
        if not trades.empty:
            for _, trade in trades.iterrows():
                if trade['type'] == 'buy_4h' or trade['type'] == 'buy_1h':
                    ax1.scatter(trade['time'], results.loc[trade['time'], 'close'], 
                                color='green', marker='^', s=100)
                elif trade['type'] == 'sell_4h' or trade['type'] == 'sell_1h':
                    ax1.scatter(trade['time'], results.loc[trade['time'], 'close'], 
                                color='red', marker='v', s=100)
        
        # 添加图例
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax1_right.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper left')
        
        # 绘制DMR指标
        ax2 = axes[1]
        ax2.set_title('DMR指标')
        ax2.plot(results.index, results['dmr_avg12'], label='DMR12 (4H)', color='blue')
        ax2.plot(results.index, results['dmr_avg26'], label='DMR26 (1H)', color='red')
        ax2.axhline(y=0, color='black', linestyle='-', alpha=0.3)
        ax2.legend()
        
        # 绘制市场状态
        ax3 = axes[2]
        ax3.set_title('市场状态')
        # 将市场状态转换为数值
        state_map = {'T1': 1, 'T2': 2, 'R1': 3, 'R2': 4}
        results['state_num'] = results['market_state'].map(state_map)
        ax3.plot(results.index, results['state_num'], label='市场状态', color='purple')
        ax3.set_yticks([1, 2, 3, 4])
        ax3.set_yticklabels(['T1-双多', 'T2-双空', 'R1-高位震荡', 'R2-低位震荡'])
        ax3.legend()
        
        # 添加回测指标文本
        textstr = '\n'.join((
            f"初始资金: {metrics['initial_capital']} USDT",
            f"最终资金: {metrics['final_capital']:.2f} USDT",
            f"总收益率: {metrics['total_return_pct']:.2f}%",
            f"胜率: {metrics['win_rate']*100:.2f}%",
            f"交易次数: {metrics['trade_count']}",
            f"最大回撤: {metrics['max_drawdown_pct']:.2f}%",
            f"夏普比率: {metrics['sharpe_ratio']:.2f}"
        ))
        
        # 在图表右上角添加文本框
        props = dict(boxstyle='round', facecolor='wheat', alpha=0.5)
        ax1.text(0.02, 0.98, textstr, transform=ax1.transAxes, fontsize=10,
                verticalalignment='top', bbox=props)
        
        plt.tight_layout()
        plt.show()
        
        return fig

def main():
    """主函数"""
    # 设置数据路径
    data_dir = 'data'
    os.makedirs(data_dir, exist_ok=True)
    symbol = SYMBOL.replace('/', '_')
    data_path = f'{data_dir}/{symbol}_data.csv'
    
    # 初始化回测器
    backtest = DMRQuadrantBacktest(data_path=data_path, initial_capital=10000)
    
    # 运行回测
    results = backtest.run_backtest_custom()
    
    if results:
        # 打印回测结果
        metrics = results['metrics']
        print("\n==== DMR四象限量化策略回测结果 ====")
        print(f"初始资金: {metrics['initial_capital']} USDT")
        print(f"最终资金: {metrics['final_capital']:.2f} USDT")
        print(f"总收益率: {metrics['total_return_pct']:.2f}%")
        print(f"胜率: {metrics['win_rate']*100:.2f}%")
        print(f"交易次数: {metrics['trade_count']}")
        print(f"盈利交易: {metrics['profit_trades']}")
        print(f"亏损交易: {metrics['loss_trades']}")
        print(f"最大回撤: {metrics['max_drawdown']:.2f} USDT ({metrics['max_drawdown_pct']:.2f}%)")
        print(f"夏普比率: {metrics['sharpe_ratio']:.2f}")
        print("================================\n")
        
        # 绘制回测结果
        backtest.plot_results()
    else:
        print("回测失败，无结果")

if __name__ == "__main__":
    main()