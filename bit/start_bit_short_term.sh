#!/bin/bash
# 任一步骤失败立即退出：此前 activate 或 pip 失败后脚本仍会继续，
# 用 PATH 上的任意 python 拉起真实策略（可能是错误的解释器/依赖）。
set -euo pipefail
###############################################################################
# BIT 短周期策略启动脚本
# 功能：激活虚拟环境并启动 BIT 短周期 DMR 量化策略
###############################################################################

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "========================================================================"
echo "BIT 短周期 DMR 量化策略启动脚本"
echo "========================================================================"
echo "项目根目录: $PROJECT_ROOT"
echo "脚本目录: $SCRIPT_DIR"
echo "========================================================================"

# 检查虚拟环境
VENV_PATH="$PROJECT_ROOT/venv"
if [ ! -d "$VENV_PATH" ]; then
    echo "❌ 错误: 未找到虚拟环境 $VENV_PATH"
    echo "请先创建虚拟环境: python3 -m venv venv"
    exit 1
fi

echo "✅ 找到虚拟环境: $VENV_PATH"

# 激活虚拟环境
echo "正在激活虚拟环境..."
source "$VENV_PATH/bin/activate"
PYTHON_BIN="$VENV_PATH/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "❌ 错误: 虚拟环境解释器不存在: $PYTHON_BIN"; exit 1
fi

# 检查 Python 版本
PYTHON_VERSION=$(python --version 2>&1)
echo "Python 版本: $PYTHON_VERSION"

# 检查必要的依赖
echo "========================================================================"
echo "检查依赖..."
echo "========================================================================"

# 交易启动路径不做联网安装：安装与启动必须分离，否则一次失败的
# pip 就可能让策略带着半套依赖连上实盘账户。
if ! "$PYTHON_BIN" -c "import ccxt" 2>/dev/null; then
    echo "❌ 错误: 依赖未安装（ccxt 不可导入）"
    echo "请先单独执行: $PYTHON_BIN -m pip install -r $PROJECT_ROOT/requirements.txt"
    exit 1
fi

# 进入项目根目录
cd "$PROJECT_ROOT" || exit 1

echo "========================================================================"
echo "启动 BIT 短周期策略..."
echo "========================================================================"

# 运行策略（使用 python 而不是 python3，因为在虚拟环境中）
"$PYTHON_BIN" bit/bit_short_term_main.py

# 捕获退出码
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "========================================================================"
    echo "❌ 策略异常退出，退出码: $EXIT_CODE"
    echo "========================================================================"
else
    echo "========================================================================"
    echo "✅ 策略正常退出"
    echo "========================================================================"
fi

exit $EXIT_CODE
