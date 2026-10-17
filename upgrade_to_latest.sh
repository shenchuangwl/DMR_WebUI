#!/bin/bash

# DMR量化交易系统 - 依赖升级到最新兼容版本脚本
# Version 5.0 (2026-09-15)
# 逻辑: 仅安装审核后的 requirements.txt 精确版本，不移除约束、不查询网络最新。
# 无参数仅显示帮助；--apply 才修改venv，快照保存在项目外。
# 说明: 不读取项目配置、不启动策略；业务回归必须另行隔离验证。
# v5.0: setuptools/wheel 不再预先单独升级，与其他包一起单次解析
#       (v4.0→v5.0 升级时 ccxt 4.5.67 锁定 setuptools==82.0.1，预先升级 setuptools 会产生临时依赖冲突)

set -e  # 遇到错误立即退出

# 颜色
GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${BLUE}$1${NC}"; }
ok()    { echo -e "${GREEN}$1${NC}"; }
warn()  { echo -e "${YELLOW}$1${NC}"; }
err()   { echo -e "${RED}$1${NC}"; }

PROJECT_PATH="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"
cd "$PROJECT_PATH"
case "${1:---help}" in
    -h|--help) echo "Usage: $0 --apply  (install reviewed pins; external backup; no live test)"; exit 0 ;;
    --apply) [ "$#" -eq 1 ] || exit 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
esac
export PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
BACKUP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/dmr-dependency-backup.XXXXXXXX")
case "$(readlink -f "$BACKUP_DIR")/" in "$PROJECT_PATH/"*) echo 'Backup must be outside project' >&2; exit 1 ;; esac
TS=$(date '+%Y-%m-%d %H:%M:%S')

echo "🚀 DMR量化交易系统 - 安装审核后的锁定版本 (不追逐最新)"
echo "=================================================="
info "当前时间: $TS"
info "工作目录: $PROJECT_PATH"
echo ""

# 1. 检查/创建虚拟环境
if [ ! -f "venv/bin/activate" ]; then
    warn "⚠️ 未发现虚拟环境，正在创建 venv ..."
    python3 -m venv venv
fi
source venv/bin/activate
if [ "${VIRTUAL_ENV:-}" != "$PROJECT_PATH/venv" ] || [ -L venv ]; then
    err "虚拟环境路径不一致或为符号链接；拒绝修改其他环境"
    exit 1
fi
ok "✅ 虚拟环境已激活: $(which python3)"

# 2. 备份当前 requirements.txt (升级前基线，便于回滚)
if [ -f "requirements.txt" ]; then
    backup="$BACKUP_DIR/requirements.txt"
    cp requirements.txt "$backup"
    ok "✅ 已备份当前依赖清单: $backup"
fi

# 3. 保留pip；setuptools/wheel与其他依赖一起解析
echo ""
info "保留当前pip；保存升级前包快照到项目外 ..."
python3 -m pip freeze > "$BACKUP_DIR/before.txt"

# 4. 保留版本约束，单次解析安装
echo ""
info "📥 按 requirements.txt 审核后的精确版本安装 ..."
python3 -m pip install -r requirements.txt

# 5. 依赖冲突检测
echo ""
info "🔍 依赖冲突检测 (pip check) ..."
if pip check; then
    ok "✅ 无依赖冲突"
else
    err "❌ 检测到依赖冲突；安装已中止。升级前快照: $BACKUP_DIR/before.txt (不是venv完整备份)"
    exit 1
fi

# 6. 关键包版本验证
echo ""
info "🔍 关键包版本验证 ..."
python3 -c "
from importlib.metadata import version, PackageNotFoundError
keys = ['numpy','pandas','numexpr','ccxt','python-binance','aiohttp','cryptography','matplotlib','requests','websockets']
for k in keys:
    try:
        print(f'✅ {k:16} {version(k)}')
    except PackageNotFoundError:
        print(f'❌ {k:16} 未安装')
        raise SystemExit(1)
"

# 7. 全量锁定版本验证；不导入真实配置或项目模块
echo ""
info "🔍 全量锁定版本验证 ..."
bash -c 'source ./start_trading.sh; activate_venv; verify_packages'

# 8. 生成升级后的完整版本清单(供审核比对，不覆盖 requirements.txt)
echo ""
info "📝 保存安装后版本清单到项目外 ..."
python3 -m pip freeze > "$BACKUP_DIR/after.txt"
ok "✅ 已生成 $BACKUP_DIR/after.txt"

echo ""
ok "🎉 升级完成！"
echo "=================================================="
ok "✅ 已安装审核后的锁定版本 (pip check 无冲突)"
ok "✅ 依赖版本校验通过；未验证业务模块、账户或交易行为"
echo ""
info "📋 下一步操作："
info "  在不含真实配置且断网的隔离副本中执行 deploy_and_run.sh --yes。"
info "  ccxt余额和数值类型业务回归通过前禁止将此结果视为实盘就绪。"
echo ""
info "📁 相关文件："
info "  - 升级前清单及前后包快照: $BACKUP_DIR"
info "  - 不提供事务性venv回滚；变更前应停机并另做完整venv备份。"
