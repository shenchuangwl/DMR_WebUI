#!/usr/bin/env python3
"""
BIT专属策略模型切换工具
用于快速切换运行模型（原有模型/现有模型/混合模型）

文件路径: bit/config/switch_model.py
版本: v1.0.0
创建日期: 2025-10-30

使用方法:
    python3 switch_model.py 1  # 切换到模型1（完全自动）
    python3 switch_model.py 2  # 切换到模型2（手动开仓+自动管理）
    python3 switch_model.py 3  # 切换到模型3（混合模式）
    python3 switch_model.py status  # 查看当前模型状态
"""

import sys
import os
from pathlib import Path

# 添加项目路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config.feature_switches import (
    RunningModel,
    load_model_preset,
    print_switches_status,
    validate_switches,
    FEATURE_SWITCHES,
    POSITION_CLAIM_CONFIG
)


def print_banner():
    """打印横幅"""
    print("=" * 60)
    print("BIT专属策略模型切换工具 v1.0.0")
    print("=" * 60)


def print_model_info(model_num: int):
    """打印模型信息"""
    if model_num == 1:
        print("\n【模型1: 完全自动模式】")
        print("  - 自动开仓: ✅ 开启")
        print("  - 自动平仓: ✅ 开启")
        print("  - 接管外部持仓: ❌ 关闭")
        print("  - 适用场景: 完全量化交易，无需人工干预")
        print("  - 运行链条: 信号检测 → 自动开仓 → 追踪止损 → 自动平仓")
    elif model_num == 2:
        print("\n【模型2: 手动开仓+自动管理模式】")
        print("  - 自动开仓: ❌ 关闭")
        print("  - 自动平仓: ✅ 开启")
        print("  - 接管外部持仓: ✅ 开启")
        print("  - 适用场景: 手动开仓，策略负责止损和平仓")
        print("  - 运行链条: 手动开仓 → 检测接管 → 追踪止损 → 自动平仓")
    elif model_num == 3:
        print("\n【模型3: 混合模式】")
        print("  - 自动开仓: ✅ 开启")
        print("  - 自动平仓: ✅ 开启")
        print("  - 接管外部持仓: ✅ 开启")
        print("  - 优先级: 外部持仓优先")
        print("  - 适用场景: 灵活切换，支持手动和自动开仓")
        print("  - 运行链条: 检测外部持仓 → 无则检测信号 → 自动开仓 → 追踪止损")


def switch_to_model(model_num: int):
    """
    切换到指定模型
    
    Args:
        model_num: 模型编号 (1, 2, 3)
    """
    print_banner()
    
    if model_num == 1:
        print("\n🔄 正在切换到模型1: 完全自动模式...")
        load_model_preset(RunningModel.ORIGINAL)
        print_model_info(1)
        
    elif model_num == 2:
        print("\n🔄 正在切换到模型2: 手动开仓+自动管理模式...")
        load_model_preset(RunningModel.NEW)
        print_model_info(2)
        
    elif model_num == 3:
        print("\n🔄 正在切换到模型3: 混合模式...")
        load_model_preset(RunningModel.HYBRID)
        print_model_info(3)
        
    else:
        print(f"\n❌ 错误: 无效的模型编号 {model_num}")
        print_usage()
        return False
    
    # 验证配置
    print("\n🔍 验证配置一致性...")
    is_valid, errors = validate_switches()
    
    if not is_valid:
        print("⚠️ 配置验证发现以下问题:")
        for error in errors:
            print(f"  {error}")
    else:
        print("✅ 配置验证通过")
    
    # 打印当前状态
    print("\n📊 当前开关状态:")
    print(f"  运行模型: {FEATURE_SWITCHES['running_model']['mode']}")
    print(f"  自动开仓: {FEATURE_SWITCHES['order_execution']['auto_open']}")
    print(f"  自动平仓: {FEATURE_SWITCHES['order_execution']['auto_close']}")
    print(f"  接管外部持仓: {FEATURE_SWITCHES['state_sync']['claim_orphan_positions']}")
    print(f"  持仓接管模式: {POSITION_CLAIM_CONFIG['claim_mode']}")
    print(f"  持仓推断方式: {POSITION_CLAIM_CONFIG['infer_method']}")
    
    # 提示重启策略
    print("\n" + "=" * 60)
    print("⚠️  重要提示: 配置已更新，但需要重启策略才能生效！")
    print("=" * 60)
    print("\n重启命令:")
    print("  pkill -f bit_long_term_main")
    print("  pkill -f bit_short_term_main")
    _bit_root = Path(__file__).resolve().parents[1]
    print(f"  python3 {_bit_root / 'bit_long_term_main.py'}")
    print(f"  python3 {_bit_root / 'bit_short_term_main.py'}")
    
    return True


def show_status():
    """显示当前模型状态"""
    print_banner()
    
    current_mode = FEATURE_SWITCHES['running_model']['mode']
    
    print(f"\n📊 当前运行模型: {current_mode}")
    
    # 判断是哪个模型
    if current_mode == RunningModel.ORIGINAL.value:
        print_model_info(1)
    elif current_mode == RunningModel.NEW.value:
        print_model_info(2)
    elif current_mode == RunningModel.HYBRID.value:
        print_model_info(3)
    else:
        print(f"\n⚠️ 未知的运行模型: {current_mode}")
    
    # 详细配置信息
    print("\n📋 详细配置信息:")
    print("=" * 60)
    print("【功能开关】")
    print(f"  信号检测: {FEATURE_SWITCHES['signal_detection']['enabled']}")
    print(f"    - DMR穿越: {FEATURE_SWITCHES['signal_detection']['dmr_crossover']}")
    print(f"    - 仓位纠偏: {FEATURE_SWITCHES['signal_detection']['position_correction']}")
    print(f"    - 双时间框架过滤: {FEATURE_SWITCHES['signal_detection']['dual_timeframe_filter']}")
    
    print(f"\n  订单执行: {FEATURE_SWITCHES['order_execution']['enabled']}")
    print(f"    - 自动开仓: {FEATURE_SWITCHES['order_execution']['auto_open']}")
    print(f"    - 自动平仓: {FEATURE_SWITCHES['order_execution']['auto_close']}")
    print(f"    - 失败重试: {FEATURE_SWITCHES['order_execution']['retry_on_failure']}")
    
    print(f"\n  追踪止损: {FEATURE_SWITCHES['risk_management']['trailing_stop']['enabled']}")
    print(f"    - 6-K线规则: {FEATURE_SWITCHES['risk_management']['trailing_stop']['k_line_rule']}")
    print(f"    - ATR动态止损: {FEATURE_SWITCHES['risk_management']['trailing_stop']['atr_dynamic']}")
    
    print(f"\n  风险限制: {FEATURE_SWITCHES['risk_management']['risk_limits']['enabled']}")
    print(f"    - 日损失限制: {FEATURE_SWITCHES['risk_management']['risk_limits']['daily_loss_limit']}")
    print(f"    - 最大回撤限制: {FEATURE_SWITCHES['risk_management']['risk_limits']['max_drawdown_limit']}")
    
    print("\n【持仓接管配置】")
    print(f"  接管模式: {POSITION_CLAIM_CONFIG['claim_mode']}")
    print(f"  推断方式: {POSITION_CLAIM_CONFIG['infer_method']}")
    print(f"  最小持仓量: {POSITION_CLAIM_CONFIG['min_position_amount']}")
    print(f"  检测间隔: {POSITION_CLAIM_CONFIG['detection_interval']}秒")
    
    if POSITION_CLAIM_CONFIG['claim_mode'] == 'HYBRID':
        print(f"  混合模式优先级: {POSITION_CLAIM_CONFIG['hybrid_priority']}")
        print(f"  自动开仓延迟: {POSITION_CLAIM_CONFIG['auto_open_delay']}秒")
    
    print("=" * 60)


def print_usage():
    """打印使用说明"""
    print("\n使用方法:")
    print("  python3 switch_model.py 1        # 切换到模型1（完全自动）")
    print("  python3 switch_model.py 2        # 切换到模型2（手动开仓+自动管理）")
    print("  python3 switch_model.py 3        # 切换到模型3（混合模式）")
    print("  python3 switch_model.py status   # 查看当前模型状态")
    print("  python3 switch_model.py help     # 显示帮助信息")


def print_help():
    """打印详细帮助信息"""
    print_banner()
    print("\n📖 BIT专属策略模型切换工具帮助文档")
    print("=" * 60)
    
    print("\n【三种运行模型说明】")
    print("\n模型1: 完全自动模式 (ORIGINAL)")
    print("  特点: 策略完全自主控制，从信号检测到开平仓全自动")
    print("  流程: 信号检测 → 自动开仓 → 本地记录 → 追踪止损 → 自动平仓")
    print("  适用: 量化交易者，希望策略完全自动运行")
    
    print("\n模型2: 手动开仓+自动管理模式 (NEW)")
    print("  特点: 用户手动开仓，策略接管后负责止损和平仓")
    print("  流程: 手动开仓 → 检测接管 → 获取持仓信息 → 追踪止损 → 自动平仓")
    print("  适用: 主观交易者，希望结合盘感和自动化管理")
    
    print("\n模型3: 混合模式 (HYBRID)")
    print("  特点: 同时支持自动开仓和手动开仓，优先检测外部持仓")
    print("  流程: 检测外部持仓 → 无则检测信号 → 自动开仓 → 追踪止损")
    print("  适用: 专业交易者，需要灵活应对不同市场环境")
    
    print("\n【命令说明】")
    print_usage()
    
    print("\n【配置文件】")
    print(f"  功能开关: {project_root}/config/feature_switches.py")
    print(f"  主配置: {project_root}/config/bit_config.py")
    
    print("\n【注意事项】")
    print("  1. 切换模型后需要重启策略才能生效")
    print("  2. 模型2和模型3需要确保订单历史可查询")
    print("  3. 建议先在测试环境验证后再用于实盘")
    print("  4. 任何模型下都建议保持风险限制和追踪止损开启")
    
    print("\n【文档路径】")
    print("  运行模型说明: 见 README.md「运行模型」一节")
    
    print("=" * 60)


def main():
    """主函数"""
    if len(sys.argv) < 2:
        print_banner()
        print("\n❌ 错误: 缺少参数")
        print_usage()
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    if command == 'status':
        show_status()
    elif command == 'help' or command == '--help' or command == '-h':
        print_help()
    elif command in ['1', '2', '3']:
        model_num = int(command)
        success = switch_to_model(model_num)
        sys.exit(0 if success else 1)
    else:
        print_banner()
        print(f"\n❌ 错误: 未知的命令 '{command}'")
        print_usage()
        sys.exit(1)


if __name__ == '__main__':
    main()
