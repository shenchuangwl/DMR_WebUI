import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# 设置中文字体支持
plt.rcParams['font.sans-serif'] = ['SimHei', 'Arial Unicode MS', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

class DMRIndicator:
    """
    日中价比率(Daily Mid-price Ratio)指标
    
    该指标计算基于日内高低价中点的比率移动平均值，
    用于识别价格趋势的变化点，特别是穿越零轴的信号。
    """
    
    def __init__(self, data=None):
        """
        初始化DMR指标
        
        Parameters:
        data (DataFrame): 包含OHLC数据的DataFrame，列名应为['open', 'high', 'low', 'close']
        """
        self.data = data
        self.results = None
    
    def calculate(self, data=None):
        """
        计算DMR指标
        
        Parameters:
        data (DataFrame): OHLC数据，如果为None则使用初始化时的数据
        
        Returns:
        DataFrame: 包含DMR指标各项数值的DataFrame
        """
        if data is not None:
            self.data = data
        
        if self.data is None:
            raise ValueError("需要提供OHLC数据")
        
        # 确保数据列名正确
        required_columns = ['high', 'low']
        if not all(col in self.data.columns for col in required_columns):
            raise ValueError(f"数据必须包含以下列: {required_columns}")
        
        df = self.data.copy()
        
        # 1. 计算当日中价 (日内高低价的中点)
        df['dmr_midprice'] = (df['high'] + df['low']) / 2
        
        # 2. 计算当日中价比率 (当前中价/前一日中价)
        df['dmr_ratio'] = df['dmr_midprice'] / df['dmr_midprice'].shift(1)
        
        # 3. 计算前6日、12日、26日中价比率移动平均值-1（双时间框架升级版）
        df['dmr_avg6'] = df['dmr_ratio'].rolling(window=6).mean() - 1    # 长期趋势判断（主要指标）
        df['dmr_avg12'] = df['dmr_ratio'].rolling(window=12).mean() - 1  # 短期信号执行
        df['dmr_avg26'] = df['dmr_ratio'].rolling(window=26).mean() - 1  # 保留原有指标
        
        # 添加零轴参考线
        df['zero_line'] = 0
        
        self.results = df
        return df
    
    def detect_crossovers(self):
        """
        检测穿越零轴的信号
        
        Returns:
        dict: 包含各周期穿越信号的字典
        """
        if self.results is None:
            raise ValueError("请先调用calculate()方法计算指标")
        
        df = self.results
        crossovers = {}
        
        # 检测DMR6穿越零轴
        dmr6_cross_up = (df['dmr_avg6'].shift(1) <= 0) & (df['dmr_avg6'] > 0)
        dmr6_cross_down = (df['dmr_avg6'].shift(1) >= 0) & (df['dmr_avg6'] < 0)
        crossovers['dmr6_cross_up'] = dmr6_cross_up
        crossovers['dmr6_cross_down'] = dmr6_cross_down
        
        # 检测DMR12穿越零轴
        dmr12_cross_up = (df['dmr_avg12'].shift(1) <= 0) & (df['dmr_avg12'] > 0)
        dmr12_cross_down = (df['dmr_avg12'].shift(1) >= 0) & (df['dmr_avg12'] < 0)
        crossovers['dmr12_cross_up'] = dmr12_cross_up
        crossovers['dmr12_cross_down'] = dmr12_cross_down
        
        # 检测DMR26穿越零轴
        dmr26_cross_up = (df['dmr_avg26'].shift(1) <= 0) & (df['dmr_avg26'] > 0)
        dmr26_cross_down = (df['dmr_avg26'].shift(1) >= 0) & (df['dmr_avg26'] < 0)
        crossovers['dmr26_cross_up'] = dmr26_cross_up
        crossovers['dmr26_cross_down'] = dmr26_cross_down
        
        return crossovers
    
    def detect_zero_axis_signals(self):
        """
        检测基于零轴的正负值信号
        
        Returns:
        dict: 包含各周期正负值信号的字典
        """
        if self.results is None:
            raise ValueError("请先调用calculate()方法计算指标")
        
        df = self.results
        signals = {}
        
        # 检测DMR6基于零轴的正负值信号
        # DMR6数值大于零轴且在零轴之上，为正值
        dmr6_positive = df['dmr_avg6'] > 0
        # DMR6数值小于零轴且在零轴之下，为负值
        dmr6_negative = df['dmr_avg6'] < 0
        signals['dmr6_positive'] = dmr6_positive
        signals['dmr6_negative'] = dmr6_negative
        
        # 检测DMR12基于零轴的正负值信号
        # DMR12数值大于零轴且在零轴之上，为正值
        dmr12_positive = df['dmr_avg12'] > 0
        # DMR12数值小于零轴且在零轴之下，为负值
        dmr12_negative = df['dmr_avg12'] < 0
        signals['dmr12_positive'] = dmr12_positive
        signals['dmr12_negative'] = dmr12_negative
        
        # 检测DMR26基于零轴的正负值信号
        # DMR26数值大于零轴且在零轴之上，为正值
        dmr26_positive = df['dmr_avg26'] > 0
        # DMR26数值小于零轴且在零轴之下，为负值
        dmr26_negative = df['dmr_avg26'] < 0
        signals['dmr26_positive'] = dmr26_positive
        signals['dmr26_negative'] = dmr26_negative
        
        return signals
    
    def get_signals(self):
        """
        获取交易信号
        
        Returns:
        DataFrame: 包含信号的DataFrame
        """
        crossovers = self.detect_crossovers()
        zero_axis_signals = self.detect_zero_axis_signals()
        df = self.results.copy()
        
        # 添加穿越信号列
        for signal_name, signal_series in crossovers.items():
            df[signal_name] = signal_series
        
        # 添加正负值信号列
        for signal_name, signal_series in zero_axis_signals.items():
            df[signal_name] = signal_series
        
        return df
    
    def plot(self, figsize=(12, 8), start_date=None, end_date=None, show_all_signals=False):
        """
        绘制DMR指标图表
        
        Parameters:
        figsize (tuple): 图表大小
        start_date (str): 开始日期
        end_date (str): 结束日期
        show_all_signals (bool): 是否显示所有周期的穿越信号
        """
        if self.results is None:
            raise ValueError("请先调用calculate()方法计算指标")
        
        df = self.results.copy()
        
        # 日期过滤
        if start_date:
            df = df[df.index >= start_date]
        if end_date:
            df = df[df.index <= end_date]
        
        # 获取穿越信号
        crossovers = self.detect_crossovers()
        
        plt.figure(figsize=figsize)
        
        # 绘制零轴（虚线样式，模拟原始指标）
        plt.axhline(y=0, color='grey', linestyle='--', linewidth=1, alpha=0.8, label='DMR 零轴')
        
        # 优化的绘图方法：使用向量化操作提高性能
        # DMR6 - 蓝色（正值）/ 红色（负值）
        dmr6_pos = df['dmr_avg6'].where(df['dmr_avg6'] > 0)
        dmr6_neg = df['dmr_avg6'].where(df['dmr_avg6'] <= 0)
        plt.plot(df.index, dmr6_pos, color='blue', linewidth=2, alpha=0.8, label='DMR Average 6')
        plt.plot(df.index, dmr6_neg, color='red', linewidth=2, alpha=0.8)
        
        # DMR12 - 橙色（正值）/ 红色（负值）
        dmr12_pos = df['dmr_avg12'].where(df['dmr_avg12'] > 0)
        dmr12_neg = df['dmr_avg12'].where(df['dmr_avg12'] <= 0)
        plt.plot(df.index, dmr12_pos, color='orange', linewidth=2, alpha=0.7, label='DMR Average 12')
        plt.plot(df.index, dmr12_neg, color='red', linewidth=2, alpha=0.7)
        
        # DMR26 - 紫色（正值）/ 红色（负值）
        dmr26_pos = df['dmr_avg26'].where(df['dmr_avg26'] > 0)
        dmr26_neg = df['dmr_avg26'].where(df['dmr_avg26'] <= 0)
        plt.plot(df.index, dmr26_pos, color='purple', linewidth=2, alpha=0.6, label='DMR Average 26')
        plt.plot(df.index, dmr26_neg, color='red', linewidth=2, alpha=0.6)
        
        # 标记穿越点
        if show_all_signals:
            # 显示所有周期的穿越信号
            periods = [('dmr6', 6, 'DMR6'), ('dmr12', 12, 'DMR12'), ('dmr26', 26, 'DMR26')]
            colors = ['green', 'blue', 'purple']
            
            for i, (period, window, label) in enumerate(periods):
                cross_up = df[crossovers[f'{period}_cross_up']]
                cross_down = df[crossovers[f'{period}_cross_down']]
                
                if not cross_up.empty:
                    plt.scatter(cross_up.index, cross_up[f'dmr_avg{window}'], 
                               color=colors[i], marker='^', s=80, alpha=0.9, 
                               label=f'{label}上穿零轴')
                if not cross_down.empty:
                    plt.scatter(cross_down.index, cross_down[f'dmr_avg{window}'], 
                               color='red', marker='v', s=80, alpha=0.9, 
                               label=f'{label}下穿零轴')
        else:
            # 只显示DMR6的穿越信号（主要信号）
            cross_up_6 = df[crossovers['dmr6_cross_up']]
            cross_down_6 = df[crossovers['dmr6_cross_down']]
            
            if not cross_up_6.empty:
                plt.scatter(cross_up_6.index, cross_up_6['dmr_avg6'], 
                           color='green', marker='^', s=100, alpha=0.9, label='DMR6上穿零轴')
            if not cross_down_6.empty:
                plt.scatter(cross_down_6.index, cross_down_6['dmr_avg6'], 
                           color='red', marker='v', s=100, alpha=0.9, label='DMR6下穿零轴')
        
        plt.title('DMR (Daily Mid-price Ratio) 指标', fontsize=16, fontweight='bold', pad=20)
        plt.xlabel('日期', fontsize=12)
        plt.ylabel('DMR值', fontsize=12)
        plt.legend(loc='best', fontsize=10)
        plt.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)
        plt.tight_layout()
        
        # 添加数值范围信息
        y_min, y_max = plt.ylim()
        plt.text(0.02, 0.98, f'数值范围: {y_min:.4f} ~ {y_max:.4f}', 
                transform=plt.gca().transAxes, fontsize=9, 
                verticalalignment='top', bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
        
        plt.show()
    
    def print_recent_signals(self, days=10):
        """
        打印最近的交易信号
        
        Parameters:
        days (int): 显示最近多少天的信号
        """
        if self.results is None:
            raise ValueError("请先调用calculate()方法计算指标")
        
        crossovers = self.detect_crossovers()
        recent_data = self.results.tail(days)
        
        print(f"\n=== 最近{days}天的DMR穿越信号 ===")
        
        signal_names = {
            'dmr6_cross_up': 'DMR6上穿零轴',
            'dmr6_cross_down': 'DMR6下穿零轴',
            'dmr12_cross_up': 'DMR12上穿零轴',
            'dmr12_cross_down': 'DMR12下穿零轴',
            'dmr26_cross_up': 'DMR26上穿零轴',
            'dmr26_cross_down': 'DMR26下穿零轴'
        }
        
        for signal_key, signal_name in signal_names.items():
            signal_dates = recent_data[crossovers[signal_key].tail(days)].index
            if len(signal_dates) > 0:
                for date in signal_dates:
                    print(f"{date.strftime('%Y-%m-%d')}: {signal_name}")
        
        print("\n=== 最新DMR值 ===")
        latest = recent_data.iloc[-1]
        print(f"DMR6: {latest['dmr_avg6']:.6f}")
        print(f"DMR12: {latest['dmr_avg12']:.6f}")
        print(f"DMR26: {latest['dmr_avg26']:.6f}")

    def get_latest_values(self):
        """
        获取最新的DMR值
        
        Returns:
        dict: 包含最新DMR值的字典
        """
        if self.results is None:
            raise ValueError("请先调用calculate()方法计算指标")
        
        latest = self.results.iloc[-1]
        return {
            'dmr6': latest['dmr_avg6'],
            'dmr12': latest['dmr_avg12'],
            'dmr26': latest['dmr_avg26'],
            'date': latest.name
        }
    
    def get_signal_summary(self, days=30):
        """
        获取信号统计摘要
        
        Parameters:
        days (int): 统计天数
        
        Returns:
        dict: 信号统计信息
        """
        if self.results is None:
            raise ValueError("请先调用calculate()方法计算指标")
        
        crossovers = self.detect_crossovers()
        zero_axis_signals = self.detect_zero_axis_signals()
        recent_data = self.results.tail(days)
        
        summary = {}
        periods = ['dmr6', 'dmr12', 'dmr26']
        
        for period in periods:
            # 穿越信号统计
            up_signals = recent_data[crossovers[f'{period}_cross_up']].shape[0]
            down_signals = recent_data[crossovers[f'{period}_cross_down']].shape[0]
            total_cross_signals = up_signals + down_signals
            
            # 正负值信号统计
            positive_signals = recent_data[zero_axis_signals[f'{period}_positive']].shape[0]
            negative_signals = recent_data[zero_axis_signals[f'{period}_negative']].shape[0]
            
            summary[period] = {
                '穿越信号': {
                    '上穿信号': up_signals,
                    '下穿信号': down_signals,
                    '总穿越信号数': total_cross_signals,
                    '穿越信号频率': f'{total_cross_signals/days*100:.1f}%'
                },
                '正负值信号': {
                    '正值信号天数': positive_signals,
                    '负值信号天数': negative_signals,
                    '正值占比': f'{positive_signals/days*100:.1f}%',
                    '负值占比': f'{negative_signals/days*100:.1f}%'
                }
            }
        
        return summary
    
    def export_data(self, filepath, include_signals=True):
        """
        导出DMR数据到CSV文件
        
        Parameters:
        filepath (str): 文件路径
        include_signals (bool): 是否包含信号数据
        """
        if self.results is None:
            raise ValueError("请先调用calculate()方法计算指标")
        
        export_df = self.results[['dmr_midprice', 'dmr_ratio', 'dmr_avg6', 'dmr_avg12', 'dmr_avg26']].copy()
        
        if include_signals:
            # 添加穿越信号
            crossovers = self.detect_crossovers()
            for signal_name, signal_series in crossovers.items():
                export_df[signal_name] = signal_series
            
            # 添加正负值信号
            zero_axis_signals = self.detect_zero_axis_signals()
            for signal_name, signal_series in zero_axis_signals.items():
                export_df[signal_name] = signal_series
        
        export_df.to_csv(filepath, encoding='utf-8-sig')
        print(f"数据已导出到: {filepath}")
    
    def validate_data(self):
        """
        验证输入数据的质量
        
        Returns:
        dict: 数据质量报告
        """
        if self.data is None:
            return {"错误": "未提供数据"}
        
        report = {
            "数据行数": len(self.data),
            "缺失值": self.data[['high', 'low']].isnull().sum().to_dict(),
            "数据范围": {
                "最高价范围": f"{self.data['high'].min():.2f} - {self.data['high'].max():.2f}",
                "最低价范围": f"{self.data['low'].min():.2f} - {self.data['low'].max():.2f}"
            },
            "异常值检测": {
                "高低价倒挂": (self.data['high'] < self.data['low']).sum(),
                "零值或负值": ((self.data['high'] <= 0) | (self.data['low'] <= 0)).sum()
            }
        }
        
        return report
    
    def analyze_zero_axis_signals(self, days=10):
        """
        分析最近的零轴正负值信号
        
        Parameters:
        days (int): 分析最近多少天的信号
        """
        if self.results is None:
            raise ValueError("请先调用calculate()方法计算指标")
        
        zero_axis_signals = self.detect_zero_axis_signals()
        recent_data = self.results.tail(days)
        
        print(f"\n=== 最近{days}天的DMR零轴正负值信号分析 ===")
        
        periods = [('dmr6', 'DMR6'), ('dmr12', 'DMR12'), ('dmr26', 'DMR26')]
        
        for period_key, period_name in periods:
            print(f"\n--- {period_name} 正负值信号 ---")
            
            # 获取最近的正负值信号
            positive_days = recent_data[zero_axis_signals[f'{period_key}_positive']]
            negative_days = recent_data[zero_axis_signals[f'{period_key}_negative']]
            
            print(f"正值天数: {len(positive_days)} 天 ({len(positive_days)/days*100:.1f}%)")
            print(f"负值天数: {len(negative_days)} 天 ({len(negative_days)/days*100:.1f}%)")
            
            # 显示当前状态
            latest_value = recent_data.iloc[-1][f'dmr_avg{period_key[3:]}']
            current_status = "正值区间" if latest_value > 0 else "负值区间" if latest_value < 0 else "零轴位置"
            print(f"当前状态: {current_status} (数值: {latest_value:.6f})")
            
            # 连续正负值天数统计
            if len(recent_data) > 0:
                consecutive_positive = 0
                consecutive_negative = 0
                
                # 从最新数据向前统计连续天数
                for i in range(len(recent_data)-1, -1, -1):
                    current_val = recent_data.iloc[i][f'dmr_avg{period_key[3:]}']
                    if current_val > 0:
                        if consecutive_negative == 0:  # 还在正值区间
                            consecutive_positive += 1
                        else:  # 已经进入负值区间，停止统计
                            break
                    elif current_val < 0:
                        if consecutive_positive == 0:  # 还在负值区间
                            consecutive_negative += 1
                        else:  # 已经进入正值区间，停止统计
                            break
                    else:  # 等于0的情况
                        break
                
                if consecutive_positive > 0:
                    print(f"连续正值天数: {consecutive_positive} 天")
                elif consecutive_negative > 0:
                    print(f"连续负值天数: {consecutive_negative} 天")
    
    def get_current_signal_status(self):
        """
        获取当前所有DMR指标的信号状态
        
        Returns:
        dict: 当前信号状态
        """
        if self.results is None:
            raise ValueError("请先调用calculate()方法计算指标")
        
        latest = self.results.iloc[-1]
        zero_axis_signals = self.detect_zero_axis_signals()
        
        status = {
            'date': latest.name,
            'dmr6': {
                'value': latest['dmr_avg6'],
                'status': '正值' if latest['dmr_avg6'] > 0 else '负值' if latest['dmr_avg6'] < 0 else '零轴',
                'is_positive': zero_axis_signals['dmr6_positive'].iloc[-1],
                'is_negative': zero_axis_signals['dmr6_negative'].iloc[-1]
            },
            'dmr12': {
                'value': latest['dmr_avg12'],
                'status': '正值' if latest['dmr_avg12'] > 0 else '负值' if latest['dmr_avg12'] < 0 else '零轴',
                'is_positive': zero_axis_signals['dmr12_positive'].iloc[-1],
                'is_negative': zero_axis_signals['dmr12_negative'].iloc[-1]
            },
            'dmr26': {
                'value': latest['dmr_avg26'],
                'status': '正值' if latest['dmr_avg26'] > 0 else '负值' if latest['dmr_avg26'] < 0 else '零轴',
                'is_positive': zero_axis_signals['dmr26_positive'].iloc[-1],
                'is_negative': zero_axis_signals['dmr26_negative'].iloc[-1]
            }
        }
        
        return status


# 使用示例
def example_usage():
    """
    DMR指标使用示例
    """
    # 创建示例数据（实际使用时替换为真实的OHLC数据）
    dates = pd.date_range('2024-01-01', periods=100, freq='D')
    np.random.seed(42)
    
    # 生成模拟的OHLC数据
    base_price = 100
    returns = np.cumsum(np.random.randn(100) * 0.02)
    prices = base_price * np.exp(returns)
    
    sample_data = pd.DataFrame({
        'open': prices * (1 + np.random.randn(100) * 0.005),
        'high': prices * (1 + np.abs(np.random.randn(100)) * 0.01),
        'low': prices * (1 - np.abs(np.random.randn(100)) * 0.01),
        'close': prices * (1 + np.random.randn(100) * 0.005)
    }, index=dates)
    
    # 创建DMR指标实例
    dmr = DMRIndicator(sample_data)
    
    # 计算指标
    results = dmr.calculate()
    
    # 显示结果
    print("DMR指标计算完成!")
    print("\n最新数据:")
    print(results[['dmr_avg6', 'dmr_avg12', 'dmr_avg26']].tail())
    
    # 打印最近信号
    dmr.print_recent_signals()
    
    # 绘制图表
    dmr.plot()
    
    # 测试新增功能
    print("\n=== 最新DMR值 ===")
    latest_values = dmr.get_latest_values()
    for key, value in latest_values.items():
        if key != 'date':
            print(f"{key.upper()}: {value:.6f}")
        else:
            print(f"日期: {value}")
    
    print("\n=== 信号统计摘要 ===")
    summary = dmr.get_signal_summary(30)
    for period, stats in summary.items():
        print(f"{period.upper()}:")
        for category, category_stats in stats.items():
            print(f"  {category}:")
            for stat_name, stat_value in category_stats.items():
                print(f"    {stat_name}: {stat_value}")
    
    # 测试零轴正负值信号分析
    dmr.analyze_zero_axis_signals(10)
    
    print("\n=== 当前信号状态 ===")
    current_status = dmr.get_current_signal_status()
    print(f"日期: {current_status['date']}")
    for period in ['dmr6', 'dmr12', 'dmr26']:
        period_status = current_status[period]
        print(f"{period.upper()}: {period_status['value']:.6f} ({period_status['status']})")
    
    print("\n=== 数据质量报告 ===")
    quality_report = dmr.validate_data()
    for key, value in quality_report.items():
        print(f"{key}: {value}")
    
    return dmr


if __name__ == "__main__":
    # 运行示例
    dmr_indicator = example_usage()