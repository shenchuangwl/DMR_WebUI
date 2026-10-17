#!/bin/bash

# DMR量化交易系统 - 统一启动脚本 (最新版本)
# 整合了环境检查、兼容性测试、自动修复和多种启动选项
# 支持NumPy 2.x和所有最新版本依赖包
# 版本: 3.1 (2026-09-15)
# v3.1: 关键包版本校验与 fix 命令改为读取 requirements.txt，与依赖清单自动保持同步（不再硬编码版本号）

set -e  # 遇到错误立即退出
PROJECT_PATH="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"
cd "$PROJECT_PATH"
export PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# 打印带颜色的消息
print_info() { echo -e "${BLUE}$1${NC}"; }
print_success() { echo -e "${GREEN}$1${NC}"; }
print_warning() { echo -e "${YELLOW}$1${NC}"; }
print_error() { echo -e "${RED}$1${NC}"; }

# requirements.txt 版本号（从文件头 "# 版本: x.y" 读取）
REQ_VER="$(sed -n 's/^# 版本: *\([0-9.]*\).*/\1/p' requirements.txt 2>/dev/null | head -1)"

# 显示帮助信息
show_help() {
    print_info "🚀 DMR量化交易系统 - 统一启动脚本 v3.1"
    print_info "=================================================="
    echo ""
    print_info "📋 使用方法:"
    echo "  $0 [选项] [策略]"
    echo ""
    print_info "🎯 策略选项:"
    echo "  main      启动主策略 (DMR四象限)"
    echo "  long      启动长周期策略"
    echo "  short     启动短周期策略"
    echo "  backtest  启动回测分析"
    echo ""
    print_info "🔧 系统选项:"
    echo "  test      运行完整兼容性测试"
    echo "  check     检查环境和依赖"
    echo "  fix       修复常见问题"
    echo "  install   安装/更新依赖包"
    echo "  version   显示版本信息"
    echo "  help      显示此帮助信息"
    echo ""
    print_info "💡 示例:"
    echo "  $0 main           # 启动主策略"
    echo "  $0 test           # 运行兼容性测试"
    echo "  $0 check          # 检查环境"
    echo "  $0 fix            # 修复问题"
    echo ""
}

# 显示版本信息
show_version() {
    print_info "🚀 DMR量化交易系统 - 统一启动脚本"
    print_info "=================================================="
    print_info "脚本版本: 3.1"
    print_info "更新日期: 2026-09-15"
    print_info "依赖清单: requirements.txt v${REQ_VER:-未知}"
    print_info "支持特性: NumPy 2.x, 自动修复, 兼容性测试"
    print_info "当前时间: $(date)"
    print_info "工作目录: $(pwd)"
    
    # 检查Python版本
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 --version 2>&1 | cut -d' ' -f2)
        print_info "Python版本: $PYTHON_VERSION"
    else
        print_error "Python3 未安装"
    fi
}

# 检查虚拟环境
check_venv() {
    if [ ! -d "venv" ]; then
        print_warning "❌ 虚拟环境不存在"
        print_info "🔧 正在创建新的虚拟环境..."
        python3 -m venv venv
        print_success "✅ 虚拟环境创建完成"

        # 激活并升级pip
        source venv/bin/activate
        print_info "保留venv自带pip，构建工具由requirements.txt锁定"

        # 安装依赖
        print_info "📥 安装项目依赖..."
        pip install -r requirements.txt
        print_success "✅ 依赖安装完成"
    else
        print_success "✅ 虚拟环境已存在"
    fi
}

# 激活虚拟环境
activate_venv() {
    print_info "🔄 激活虚拟环境..."
    source venv/bin/activate
    if [ "${VIRTUAL_ENV:-}" != "$PROJECT_PATH/venv" ] || [ -L venv ]; then
        print_error "❌ 虚拟环境路径不一致或为符号链接；拒绝修改/运行其他环境"
        return 1
    fi
}

# 验证关键包版本 (期望版本读取自 requirements.txt)
verify_packages() {
    print_info "🔍 验证关键包版本 (基于requirements.txt v${REQ_VER:-未知})..."
    python -c "
import sys
print(f'Python版本: {sys.version}')
print('=' * 60)

# 基于requirements.txt的精确版本验证：期望版本全部直接读取 requirements.txt，避免脚本与依赖清单不同步
pinned_versions = {}
for req_line in open('requirements.txt', encoding='utf-8'):
    req_line = req_line.split('#', 1)[0].strip()
    if '==' in req_line:
        req_name, req_version = req_line.split('==', 1)
        pinned_versions[req_name.strip().lower()] = req_version.strip()
required_packages = pinned_versions
if not required_packages:
    sys.exit('requirements.txt has no pinned packages')

success_count = 0
version_match_count = 0
total_count = len(required_packages)

# 使用已安装分发包的元数据获取版本，避免导入名/版本属性差异导致的误报
from importlib.metadata import version as _pkg_version, PackageNotFoundError

for pkg_name, expected_version in required_packages.items():
    try:
        actual_version = _pkg_version(pkg_name)
        if actual_version == expected_version:
            print(f'✅ {pkg_name:15} {actual_version} (匹配)')
            version_match_count += 1
        else:
            print(f'⚠️ {pkg_name:15} {actual_version} (期望: {expected_version})')
        success_count += 1
    except PackageNotFoundError:
        print(f'❌ {pkg_name:15} 未安装')

print('=' * 60)
print(f'📊 包安装成功率: {success_count}/{total_count} ({success_count/total_count*100:.1f}%)')
print(f'🎯 版本匹配率: {version_match_count}/{success_count} ({version_match_count/success_count*100:.1f}%)' if success_count > 0 else '🎯 版本匹配率: 0%')

# NumPy 2.x 兼容性专项检查
try:
    import numpy as np
    expected_numpy = required_packages.get('numpy', '')
    if np.__version__ == expected_numpy:
        print('🎯 NumPy 2.x 兼容性: ✅ 完全匹配 requirements.txt')
    elif np.__version__.startswith('2.'):
        print(f'⚠️ NumPy 版本: {np.__version__} (期望: {expected_numpy})')
    else:
        print(f'❌ NumPy 版本: {np.__version__} (需要升级到{expected_numpy})')
except:
    print('❌ NumPy 兼容性: 检查失败')
    sys.exit(1)
sys.exit(0 if version_match_count == total_count else 1)
"
}

# 验证配置文件
verify_config() {
    print_info "🔍 验证配置文件..."
    python -c "
try:
    from config.config import validate_config, get_config_summary
    validate_config()
    config = get_config_summary()
    print('✅ 配置文件验证通过')
    print(f'交易对: {config.get(\"trade_symbol\", \"未知\")}')
    print(f'交易金额: {config.get(\"trade_amount\", \"未知\")}')
    print(f'配置版本: {config.get(\"config_version\", \"未知\")}')
except Exception as e:
    print(f'❌ 配置文件验证失败: {e}')
    print('💡 请检查 config/config.py 文件')
    exit(1)
"
}

# 检查环境
check_environment() {
    print_info "🔍 检查系统环境..."
    print_info "=================================================="
    
    show_version
    echo ""
    
    check_venv
    activate_venv
    verify_packages
    echo ""
    verify_config
    
    print_success "✅ 环境检查完成"
}

# 安装/更新依赖
install_dependencies() {
    print_info "📦 安装/更新依赖包..."
    print_info "=================================================="
    
    check_venv
    activate_venv
    
    print_info "保留当前pip，构建工具由requirements.txt锁定"
    
    print_info "📥 安装/更新依赖包..."
    python -m pip install -r requirements.txt
    python -m pip check
    
    print_success "✅ 依赖包安装/更新完成"
    
    # 验证安装
    verify_packages
}

# 修复常见问题 (按 requirements.txt 精确锁定版本重建环境)
fix_issues() {
    print_info "🔧 修复常见问题 (基于requirements.txt v${REQ_VER:-未知})..."
    print_info "=================================================="

    # 1. 重建虚拟环境（旧环境备份为 venv_backup_*，便于回滚）
    print_info "1. 重建虚拟环境..."
    if [ -d "venv" ]; then
        print_warning "备份现有虚拟环境..."
        mv venv venv_backup_$(date +%Y%m%d_%H%M%S)
    fi

    python3 -m venv venv
    source venv/bin/activate

    # 2. 升级pip（setuptools/wheel 由 requirements.txt 精确锁定）
    print_info "2. 保留venv自带pip..."

    # 3. 按 requirements.txt 精确版本安装全部依赖
    print_info "3. 按 requirements.txt 精确版本安装全部依赖..."
    pip install -r requirements.txt

    # 4. 验证修复结果
    print_info "4. 验证修复结果..."
    verify_packages

    # 5. 生成环境快照
    print_info "5. 生成环境快照..."
    pip freeze > requirements_current_$(date +%Y%m%d_%H%M%S).txt

    print_success "✅ 问题修复完成 - 环境已完全匹配requirements.txt v${REQ_VER:-未知}"
}

# 启动策略
start_strategy() {
    local strategy=$1
    
    # 先检查环境
    check_venv
    activate_venv
    
    # 快速验证关键包
    python -c "
try:
    import numpy, pandas, ccxt
    from config.config import validate_config
    validate_config()
    print('✅ 环境验证通过')
except Exception as e:
    print(f'❌ 环境验证失败: {e}')
    exit(1)
"
    
    if [ $? -ne 0 ]; then
        print_error "环境验证失败，请先运行: $0 fix"
        exit 1
    fi
    
    case $strategy in
        "main")
            print_success "🚀 启动主策略 (DMR四象限)..."
            python main.py
            ;;
        "long")
            # 改指 first_long_term_main.py：strategy/long_term 是旧版引擎，
            # 完全没有集成移动止损/移动止盈，开仓后没有任何自动退出机制。
            print_success "🚀 启动长周期策略 (FIRST 引擎，含移动止损/止盈)..."
            python first_long_term_main.py
            ;;
        "short")
            # 同上：strategy/short_term 旧引擎无任何止损保护。
            print_success "🚀 启动短周期策略 (FIRST 引擎，含移动止损/阶梯止盈/移动止盈)..."
            python first_short_term_main.py
            ;;
        "legacy-long"|"legacy-short")
            print_error "旧版引擎 (strategy/long_term、strategy/short_term) 没有任何止损保护，已停止通过本脚本启动。"
            print_error "如确需运行，请自行评估风险后直接执行 long_term_main.py / short_term_main.py。"
            exit 1
            ;;
        "backtest")
            print_success "🚀 启动回测分析..."
            python backtest_dmr_quadrant.py
            ;;
        *)
            print_error "未知策略: $strategy"
            show_help
            exit 1
            ;;
    esac
}

# 运行完整兼容性测试
run_full_test() {
    print_info "🧪 运行完整兼容性测试..."
    print_info "=================================================="

    check_venv
    activate_venv

    # 系统信息
    print_info "📊 系统信息:"
    echo "Python版本: $(python --version)"
    echo "Pip版本: $(pip --version)"
    echo ""

    # 核心依赖包检查 (期望版本读取自 requirements.txt)
    verify_packages
    echo ""

    # 项目模块测试
    print_info "🔍 项目模块导入测试:"
    print_info "----------------------------------------"
    python -c "
import sys
import traceback

# DMR量化交易系统核心模块
modules = [
    ('配置模块', 'config.config'),
    ('数据获取', 'data.data_fetcher'),
    ('DMR策略', 'strategy.DMRQuadrantStrategy'),
    ('订单执行', 'execution.order_executor'),
    ('日志工具', 'utils.logger'),
    ('多策略', 'strategy.multi_strategy')
]

success_count = 0
total_count = len(modules)

print('DMR量化交易系统模块验证:')
for name, module in modules:
    try:
        __import__(module)
        print(f'✅ {name:10} ({module})')
        success_count += 1
    except Exception as e:
        print(f'❌ {name:10} ({module}): {str(e)[:50]}...')

print(f'\\n📊 模块导入成功率: {success_count}/{total_count} ({success_count/total_count*100:.1f}%)')
if success_count != total_count:
    sys.exit(1)

# 验证requirements.txt中关键包的功能
print('\\n关键包功能验证:')
try:
    import numpy as np
    import pandas as pd
    import ccxt
    from config.config import validate_config

    # 验证NumPy 2.x功能
    arr = np.array([1, 2, 3, 4, 5])
    print(f'✅ NumPy 2.x数组操作: {arr.dtype}')

    # 验证Pandas功能
    df = pd.DataFrame({'test': [1, 2, 3]})
    print(f'✅ Pandas数据处理: {df.shape}')

    # 验证CCXT交易所连接
    exchange = ccxt.binance()
    print(f'✅ CCXT交易所: {exchange.id}')

    # 验证配置
    validate_config()
    print('✅ 配置验证: 通过')

except Exception as e:
    print(f'❌ 关键包功能验证失败: {e}')
    sys.exit(1)
"

    # 配置验证
    echo ""
    verify_config

    # 数据处理测试
    echo ""
    print_info "🔍 数据处理功能测试:"
    print_info "----------------------------------------"
    python -c "
import numpy as np
import pandas as pd

try:
    # 创建测试数据
    dates = pd.date_range('2024-01-01', periods=100, freq='1h')
    data = {
        'timestamp': dates,
        'open': np.random.uniform(100, 200, 100),
        'high': np.random.uniform(150, 250, 100),
        'low': np.random.uniform(50, 150, 100),
        'close': np.random.uniform(100, 200, 100),
        'volume': np.random.uniform(1000, 10000, 100)
    }

    df = pd.DataFrame(data)
    print(f'✅ 测试数据创建: {df.shape}')

    # 测试DMR计算
    df['dmr'] = (df['close'] - df['close'].rolling(12).mean()) / df['close'].rolling(12).std()
    print(f'✅ DMR计算: 最新值 {df[\"dmr\"].iloc[-1]:.6f}')

    # 测试重采样
    df_resampled = df.set_index('timestamp').resample('4h').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    })
    print(f'✅ 数据重采样: {df_resampled.shape}')

    print('🎯 数据处理测试通过')

except Exception as e:
    print(f'❌ 数据处理测试失败: {e}')
    exit(1)
"

    # 网络连接测试
    if [ "${DMR_SKIP_NETWORK_TEST:-0}" = "1" ]; then
        print_warning "公共网络: 未测试 (DMR_SKIP_NETWORK_TEST=1)"
        return 0
    fi
    echo ""
    print_info "🔍 网络连接测试:"
    print_info "----------------------------------------"
    python -c "
import requests
import ccxt

try:
    # 测试网络连接
    response = requests.get('https://api.binance.com/api/v3/ping', timeout=5)
    if response.status_code == 200:
        print('✅ Binance API连接正常')
    else:
        print(f'⚠️ Binance API响应异常: {response.status_code}')
        raise RuntimeError('Binance ping returned a non-200 status')

    # 测试CCXT
    exchange = ccxt.binance()
    markets = exchange.load_markets()
    print(f'✅ CCXT加载市场数据: {len(markets)} 个交易对')

except Exception as e:
    print(f'❌ 网络连接测试失败: {e}')
    raise SystemExit(1)
"

    echo ""
    print_success "🎉 完整兼容性测试完成！"
}

# 主程序逻辑
main() {
    case "${1:-help}" in
        "help"|"-h"|"--help")
            show_help
            ;;
        "version"|"-v"|"--version")
            show_version
            ;;
        "check")
            check_environment
            ;;
        "install")
            install_dependencies
            ;;
        "fix")
            fix_issues
            ;;
        "test")
            run_full_test
            ;;
        "main"|"long"|"short"|"backtest")
            start_strategy "$1"
            ;;
        *)
            print_warning "未知选项: $1"
            echo ""
            show_help
            exit 1
            ;;
    esac
}

# 脚本入口
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
