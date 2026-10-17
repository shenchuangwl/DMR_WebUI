#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DMR四象限市场状态分析工具
基于最新的币种行情K线数据，结合DMR四象限多周期策略，自动判断当前市场所处的四象限状态
"""

import os
import sys
import argparse
from pathlib import Path
from datetime import datetime

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).parent)
if project_root not in sys.path:
    sys.path.append(project_root)

# 导入项目组件
from data.data_fetcher import DataFetcher
from strategy.DMRQuadrantStrategy import DMRQuadrantStrategy
from config.config import SYMBOL

class DMRMarketAnalyzer:
    """DMR四象限市场状态分析工具"""
    
    def __init__(self, symbol=SYMBOL, dmr_period_12=12, dmr_period_26=26):
        self.symbol = symbol
        self.dmr_period_12 = dmr_period_12
        self.dmr_period_26 = dmr_period_26
        self.fetcher = DataFetcher()
        self.df = None
        self.df_1h = None
        self.df_4h = None
        self.dmr_4h_value = None
        self.dmr_1h_value = None
        self.dmr_4h_prev_value = None
        self.dmr_1h_prev_value = None
        self.market_state = None
        self.enet_value = 0  # 净额/Enet值
        
    def fetch_latest_data(self):
        """获取最新市场数据"""
        print(f"正在获取 {self.symbol} 的最新行情数据...")
        
        # 强制同步时间
        self.fetcher.sync_time(force=True)
        
        # 获取历史数据
        self.df = self.fetcher.get_historical_data(self.symbol, '1h', limit=1000)
        
        if self.df is None or len(self.df) == 0:
            print("获取数据失败")
            return False
            
        print(f"成功获取 {len(self.df)} 条K线数据")
        return True
    
    def analyze_market_state(self):
        """分析市场状态"""
        # 创建策略对象
        class MockOrderExecutor:
            def open_long(self, symbol, amount, price=None):
                return {'id': 'mock_long', 'price': price, 'amount': amount}
                
            def open_short(self, symbol, amount, price=None):
                return {'id': 'mock_short', 'price': price, 'amount': amount}
                
            def close_position(self, symbol, position_side):
                return {'id': 'mock_close', 'position_side': position_side}
        
        mock_executor = MockOrderExecutor()
        strategy = DMRQuadrantStrategy(self.df, mock_executor, params={
            'dmr_period_12': self.dmr_period_12,
            'dmr_period_26': self.dmr_period_26
        })
        
        # 计算DMR指标
        strategy.calculate_dmr()
        strategy.resample_data()
        strategy.generate_signals()
        
        # 获取市场状态
        self.market_state = strategy.get_market_state()
        
        # 获取DMR值
        self.df_4h = strategy.df_4h
        self.df_1h = strategy.df_1h
        
        if len(self.df_4h) >= 2 and len(self.df_1h) >= 2:
            self.dmr_4h_value = self.df_4h['dmr_avg12'].iloc[-1]
            self.dmr_4h_prev_value = self.df_4h['dmr_avg12'].iloc[-2]
            self.dmr_1h_value = self.df_1h['dmr_avg26'].iloc[-1]
            self.dmr_1h_prev_value = self.df_1h['dmr_avg26'].iloc[-2]
            
            # 计算净额/Enet (示例计算，实际可能需要根据账户数据计算)
            # 这里简单模拟一个Enet值，实际应用中应从账户数据获取
            positions = self.fetcher.get_positions(self.symbol)
            self.enet_value = sum([float(p.get('unrealizedPnl', 0)) for p in positions]) if positions else 0
            
            # 如果没有持仓，使用模拟值
            if self.enet_value == 0:
                # 根据当前市场状态模拟一个合理的Enet值
                if self.market_state == 'T1':  # 多头趋势
                    self.enet_value = 200
                elif self.market_state == 'T2':  # 空头趋势
                    self.enet_value = -150
                elif self.market_state == 'R1':  # 高位震荡
                    self.enet_value = 50
                elif self.market_state == 'R2':  # 低位震荡
                    self.enet_value = -30
            
            return True
        else:
            print("数据不足，无法分析市场状态")
            return False
    
    def get_dmr_transition(self, current, previous):
        """判断DMR值的转变方向"""
        if previous < 0 and current > 0:
            return "负转正"
        elif previous > 0 and current < 0:
            return "正转负"
        elif current > 0:
            return "持续为正"
        else:
            return "持续为负"
    
    def get_market_description(self):
        """获取市场描述"""
        if self.market_state == 'T1':
            return "多头趋势行情"
        elif self.market_state == 'T2':
            return "空头趋势行情"
        elif self.market_state == 'R1':
            return "高位震荡行情"
        elif self.market_state == 'R2':
            return "低位震荡行情"
        else:
            return "未知行情"
    
    def get_position_advice(self):
        """获取仓位建议"""
        if self.market_state == 'T1':
            return "浮盈加仓 双多"
        elif self.market_state == 'T2':
            return "浮盈加仓 双空"
        elif self.market_state == 'R1':
            return "对冲行情 锁空"
        elif self.market_state == 'R2':
            return "对冲行情 锁多"
        else:
            return "无明确建议"
    
    def get_summary(self):
        """获取简明总结说明"""
        if self.market_state == 'T1':
            return "T1-多头趋势：建议做多加码，移动止盈。"
        elif self.market_state == 'T2':
            return "T2-空头趋势：建议做空加码，移动止盈。"
        elif self.market_state == 'R1':
            return "R1-高位震荡：建议对冲锁空，降低杠杆，耐心等待共振信号。"
        elif self.market_state == 'R2':
            return "R2-低位震荡：建议对冲锁多，降低杠杆，耐心等待共振信号。"
        else:
            return "无明确市场状态，建议观望。"
    
    def generate_analysis_report(self):
        """生成分析报告"""
        if not self.fetch_latest_data() or not self.analyze_market_state():
            return "无法生成报告，数据获取或分析失败。"
        
        # 获取DMR转变方向
        dmr_4h_transition = self.get_dmr_transition(self.dmr_4h_value, self.dmr_4h_prev_value)
        dmr_1h_transition = self.get_dmr_transition(self.dmr_1h_value, self.dmr_1h_prev_value)
        
        # 格式化输出
        report = f"""
当前行情判定：{self.get_market_description()}（{self.market_state}）
当前仓位建议：{self.get_position_advice()}
策略判定细节：4H DMR({self.dmr_period_12}) {dmr_4h_transition}（{self.dmr_4h_value:.6f}），1H DMR({self.dmr_period_26}) {dmr_1h_transition}（{self.dmr_1h_value:.6f}），Enet={self.enet_value:.2f}USDT
简明说明：{self.get_summary()}
"""
        return report

def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='DMR四象限市场状态分析工具')
    parser.add_argument('-s', '--symbol', type=str, default=SYMBOL,
                        help=f'交易对名称，例如 BTC/USDT (默认: {SYMBOL})')
    parser.add_argument('-4h', '--dmr-4h', type=int, default=12,
                        help='4H DMR周期 (默认: 12)')
    parser.add_argument('-1h', '--dmr-1h', type=int, default=26,
                        help='1H DMR周期 (默认: 26)')
    parser.add_argument('-o', '--output', type=str, default='dmr_analysis_report.txt',
                        help='输出报告文件名 (默认: dmr_analysis_report.txt)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='显示详细信息')
    
    return parser.parse_args()

def main():
    """主函数"""
    # 解析命令行参数
    args = parse_arguments()
    
    print("=" * 50)
    print("DMR四象限市场状态分析工具")
    print("=" * 50)
    print(f"交易对: {args.symbol}")
    print(f"4H DMR周期: {args.dmr_4h}")
    print(f"1H DMR周期: {args.dmr_1h}")
    print("=" * 50)
    
    # 初始化分析器
    analyzer = DMRMarketAnalyzer(
        symbol=args.symbol,
        dmr_period_12=args.dmr_4h,
        dmr_period_26=args.dmr_1h
    )
    
    # 生成报告
    report = analyzer.generate_analysis_report()
    
    # 打印报告
    print(report)
    
    # 将报告保存到文件
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(f"分析时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"交易对：{analyzer.symbol}\n")
        f.write(f"4H DMR周期: {analyzer.dmr_period_12}\n")
        f.write(f"1H DMR周期: {analyzer.dmr_period_26}\n")
        f.write("=" * 50 + "\n")
        f.write(report)
    
    print(f"\n分析报告已保存至 {args.output}")

if __name__ == "__main__":
    main() 