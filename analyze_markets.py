#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DMR四象限多市场分析工具
批量分析多个交易对的市场状态
"""

import os
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime
import pandas as pd

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).parent)
if project_root not in sys.path:
    sys.path.append(project_root)

# 导入项目组件
from dmr_analysis import DMRMarketAnalyzer

# 默认分析的交易对列表
DEFAULT_SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "ADA/USDT",
    "AVAX/USDT",
    "MATIC/USDT",
    "LINK/USDT"
]

def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='DMR四象限多市场分析工具')
    parser.add_argument('-s', '--symbols', type=str, nargs='+', default=DEFAULT_SYMBOLS,
                        help='要分析的交易对列表 (默认: 前10大加密货币)')
    parser.add_argument('-4h', '--dmr-4h', type=int, default=12,
                        help='4H DMR周期 (默认: 12)')
    parser.add_argument('-1h', '--dmr-1h', type=int, default=26,
                        help='1H DMR周期 (默认: 26)')
    parser.add_argument('-o', '--output', type=str, default='market_analysis_summary.csv',
                        help='输出汇总文件名 (默认: market_analysis_summary.csv)')
    parser.add_argument('-d', '--output-dir', type=str, default='analysis_reports',
                        help='输出报告目录 (默认: analysis_reports)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='显示详细信息')
    
    return parser.parse_args()

def analyze_market(symbol, dmr_4h, dmr_1h, output_dir):
    """分析单个市场状态"""
    print(f"\n正在分析 {symbol} 市场状态...")
    
    # 初始化分析器
    analyzer = DMRMarketAnalyzer(
        symbol=symbol,
        dmr_period_12=dmr_4h,
        dmr_period_26=dmr_1h
    )
    
    # 生成报告
    report = analyzer.generate_analysis_report()
    
    # 如果分析失败，返回None
    if report.startswith("无法生成报告"):
        print(f"分析 {symbol} 失败")
        return None
    
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    # 将报告保存到文件
    base_currency = symbol.split('/')[0]
    output_file = f"{output_dir}/{base_currency}_analysis.txt"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(f"分析时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"交易对：{symbol}\n")
        f.write(f"4H DMR周期: {dmr_4h}\n")
        f.write(f"1H DMR周期: {dmr_1h}\n")
        f.write("=" * 50 + "\n")
        f.write(report)
    
    print(f"分析报告已保存至 {output_file}")
    
    # 返回分析结果摘要
    return {
        'symbol': symbol,
        'market_state': analyzer.market_state,
        'market_description': analyzer.get_market_description(),
        'position_advice': analyzer.get_position_advice(),
        'dmr_4h_value': analyzer.dmr_4h_value,
        'dmr_1h_value': analyzer.dmr_1h_value,
        'dmr_4h_transition': analyzer.get_dmr_transition(analyzer.dmr_4h_value, analyzer.dmr_4h_prev_value),
        'dmr_1h_transition': analyzer.get_dmr_transition(analyzer.dmr_1h_value, analyzer.dmr_1h_prev_value),
        'enet_value': analyzer.enet_value
    }

def main():
    """主函数"""
    # 解析命令行参数
    args = parse_arguments()
    
    print("=" * 50)
    print("DMR四象限多市场分析工具")
    print("=" * 50)
    print(f"分析交易对: {', '.join(args.symbols)}")
    print(f"4H DMR周期: {args.dmr_4h}")
    print(f"1H DMR周期: {args.dmr_1h}")
    print("=" * 50)
    
    # 分析结果列表
    results = []
    
    # 分析每个交易对
    for symbol in args.symbols:
        try:
            result = analyze_market(symbol, args.dmr_4h, args.dmr_1h, args.output_dir)
            if result:
                results.append(result)
                # 休眠1秒，避免API请求过于频繁
                time.sleep(1)
        except Exception as e:
            print(f"分析 {symbol} 时发生错误: {e}")
    
    # 如果有分析结果，生成汇总报告
    if results:
        # 转换为DataFrame
        df = pd.DataFrame(results)
        
        # 保存为CSV
        df.to_csv(args.output, index=False)
        print(f"\n汇总分析报告已保存至 {args.output}")
        
        # 打印市场状态分布统计
        market_state_counts = df['market_state'].value_counts()
        print("\n市场状态分布统计:")
        for state, count in market_state_counts.items():
            state_desc = {
                'T1': '多头趋势',
                'T2': '空头趋势',
                'R1': '高位震荡',
                'R2': '低位震荡'
            }.get(state, '未知')
            print(f"{state} ({state_desc}): {count} 个交易对")
    else:
        print("没有成功分析任何交易对")

if __name__ == "__main__":
    main() 