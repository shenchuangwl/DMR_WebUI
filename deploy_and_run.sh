#!/bin/bash

# DMR量化交易系统 - 服务器部署和运行脚本 (增强版)
# Version V16_A1 v4.0 - 纯净部署与离线验证；绝不自动启动交易
# 支持智能环境检测、配置验证、错误恢复、虚拟环境迁移自愈等高级功能
# 原始部署时间: 2026-10-14 06:59:33（迁移到新目录后的首次部署，该次由 v3.9 执行）
# 部署路径: 脚本所在目录（运行时自动解析）
# 更新内容: v4.0 新增纯净部署自检：部署前后对 venv/ 与 .git/ 之外的项目条目各做一次内存快照并比对，
#           在最终状态检查前列出变化、在最终状态检查中给出“无新增、消失或改动”的结论，使“不生成额外文件”每次运行都可验证；
#           项目整体迁移到新目录后从空环境部署通过（部署路径运行时解析，迁移无需改动脚本）。
#           v3.9 新增 libseccomp 前置预检（禁网组件缺失时在下载依赖前即停）；
#           离线回归脚本纳入核心文件完整性检查，缺失时给出明确原因而非笼统的验证失败；
#           记录并输出本次环境处置方式（新建/重建/自愈/复用），使终端摘要可直接作为验收证据。
#           v3.8 严格选择 Python 3.12；隔离宿主 PYTHONPATH；在线精确安装并复跑复用。
#           真实配置只检查存在；合成配置导入、25项依赖导入及业务回归；测试阶段操作系统禁网。
#           不修改其他脚本权限、不写项目日志、不自动启动交易；保留原章节及函数入口。

set -e  # 遇到错误立即退出

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
print_header() { echo -e "${PURPLE}$1${NC}"; }

# 项目信息 (更新为最新版本)
PROJECT_NAME="DMR量化交易系统"
PROJECT_VERSION="Version V16_A1 v4.0"
PROJECT_PATH="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"  # 支持文件与目录符号链接
CURRENT_TIME=$(date '+%Y-%m-%d %H:%M:%S')
SCRIPT_VERSION="4.0"
CONFIG_VERSION="5.0.0"
DEPLOYMENT_DATE="2026-10-14 06:59:33"  # 新目录首次部署时间 (该次由 v3.9 执行)；仅作记录，脚本未引用
PYTHON_VERSION="3.12.3"
REQUIREMENTS_VERSION="$(sed -n 's/^# 版本: *\([0-9.]*\).*/\1/p' "$PROJECT_PATH/requirements.txt" 2>/dev/null | head -1)"
REQUIREMENTS_DATE="$(sed -n 's/^# 版本: *[0-9.]* *(\(.*\)).*/\1/p' "$PROJECT_PATH/requirements.txt" 2>/dev/null | head -1)"

# 纯净部署：不生成 __pycache__ 字节码缓存，关闭 pip 版本检查提示
export PYTHONDONTWRITEBYTECODE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

# 部署结果统计 (由验证步骤动态填充，最终汇总不再硬编码)
REQ_TOTAL=0
REQ_MATCH=0
PIP_CHECK_RESULT="未执行"
MAIN_MOD_OK=0
MAIN_MOD_TOTAL=0
BIT_MOD_OK=0
BIT_MOD_TOTAL=0
SYSTEM_TEST_OK=0
NETWORK_TEST_OK=2  # 2=未测试，不能报告为正常
DEPLOY_MODE="未处理"  # 虚拟环境本次处置方式：新建/强制纯净重建/迁移自愈重建/复用健康环境
PURITY_BEFORE=""       # 部署前 venv/ 与 .git/ 之外的项目条目快照 (仅存内存，不落盘)
PURITY_TOTAL=0
PURITY_CHANGES=-1      # -1=自检未完成，0=无变化，>0=发生变化的条目数

# 解析命令行参数：支持全自动非交互部署 (-y/--yes/--auto/--non-interactive)，--rebuild 强制纯净重建虚拟环境
NON_INTERACTIVE=0
FORCE_REBUILD=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes|--auto|--non-interactive) NON_INTERACTIVE=1 ;;
        --rebuild) FORCE_REBUILD=1 ;;
        -h|--help) echo "用法: bash $0 [--yes] [--rebuild]；可设置 PYTHON_BIN 指定 Python 3.12；仅部署，不启动交易"; exit 0 ;;
        *) print_error "未知参数: $arg"; exit 2 ;;
    esac
done

# 显示部署信息
show_deployment_info() {
    print_header "🚀 $PROJECT_NAME - 服务器部署脚本 (增强版)"
    print_header "=================================================================="
    print_info "脚本版本: $SCRIPT_VERSION"
    print_info "项目版本: $PROJECT_VERSION"
    print_info "配置验证范围: 仅文件存在，真实配置不读取"
    print_info "部署时间: $CURRENT_TIME"
    print_info "部署路径: $PROJECT_PATH"
    print_info "解释器基线: Python 3.12（将在环境检查时严格选择）"
    print_info "操作系统: $(uname -s) $(uname -r)"
    echo ""
}

# 检查系统环境
check_system_environment() {
    print_info "🔍 检查系统环境（严格 Python 3.12 基线）..."
    if [ -n "${PYTHON_BIN:-}" ]; then
        candidates=("$PYTHON_BIN")
    else
        candidates=(/usr/bin/python3.12 /usr/bin/python3 python3.12 python3)
    fi
    selected=""
    for candidate in "${candidates[@]}"; do
        if "$candidate" -I -B -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' >/dev/null 2>&1; then
            selected=$(command -v "$candidate")
            break
        fi
    done
    if [ -z "$selected" ]; then
        print_error "❌ 未找到符合基线的 Python 3.12；显式 PYTHON_BIN 不兼容时不会回退"
        return 1
    fi
    PYTHON_BIN="$selected"
    PYTHON_VERSION=$("$PYTHON_BIN" -I -B -c 'import platform; print(platform.python_version())')
    print_success "✅ 选定解释器: $PYTHON_BIN；版本: $PYTHON_VERSION"
    cd "$PROJECT_PATH"
}

# 离线验证前置组件预检
# (禁网验证依赖 libseccomp.so.2；组件缺失时验证阶段必然失败。
#  v3.9 将该判定提前到依赖安装之前，避免先完成耗时下载再在最后一步失败。)
check_offline_prerequisites() {
    print_info "🔍 预检离线验证前置组件（libseccomp）..."
    if "$PYTHON_BIN" -I -B -c 'import ctypes; ctypes.CDLL("libseccomp.so.2")' >/dev/null 2>&1; then
        print_success "✅ libseccomp.so.2 可加载；测试阶段操作系统级禁网可用"
    else
        print_error "❌ 未找到可加载的 libseccomp.so.2；禁网验证无法启用，安全停止"
        print_error "   处理方式：安装该组件后重跑，例如 sudo apt-get install -y libseccomp2"
        exit 1
    fi
}

# 纯净部署自检 (v4.0)
# 快照 venv/ 与 .git/ 之外的全部项目条目：只取路径、类型、大小、修改时间，不读取任何文件内容；
# snapshot 模式输出快照，compare 模式从标准输入读入部署前快照并与当前状态比对。
# 检测边界：大小与修改时间都不变的就地改写、权限或属主变化、部署期间创建后又删除的临时条目、
#           venv/ 与 .git/ 内的变化，以及自检之后最终状态检查阶段的动作，均不在检测范围内；目录只记类型。
#           部署中途失败退出时不做比对。需要逐字节结论时以外部哈希清单为准。
PURITY_PY=$(cat <<'PYEOF'
import json, os, stat, sys
mode, root = sys.argv[1], sys.argv[2]
now = {}
for base, dirs, files in os.walk(root):
    if base == root:
        dirs[:] = [d for d in dirs if d not in ('venv', '.git')]
    for name in dirs + files:
        path = os.path.join(base, name)
        try:
            s = os.lstat(path)
        except FileNotFoundError:
            continue
        kind = 'd' if stat.S_ISDIR(s.st_mode) else 'l' if stat.S_ISLNK(s.st_mode) else 'f'
        now[os.path.relpath(path, root)] = [kind] if kind == 'd' else [kind, s.st_size, s.st_mtime_ns]
if mode == 'snapshot':
    json.dump(now, sys.stdout)
else:
    before = json.load(sys.stdin)
    changed = sorted(set(before) ^ set(now)) + sorted(p for p in now if p in before and before[p] != now[p])
    for p in changed[:20]:
        shown = p.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace').replace('\n', '\\n').replace('\r', '\\r')
        print('   ' + ('新增 ' if p not in before else '消失 ' if p not in now else '改动 ') + shown)
    print('__RESULT__ %d %d' % (len(now), len(changed)))
PYEOF
)

project_snapshot() {
    "$PYTHON_BIN" -I -B -c "$PURITY_PY" snapshot "$PROJECT_PATH"
}

# 比对部署前后快照；只提示、不改变部署判定 (策略运行中的日志与状态写入同样会被列出)
check_deployment_purity() {
    local output
    if [ -z "$PURITY_BEFORE" ] || ! output=$(printf '%s' "$PURITY_BEFORE" | "$PYTHON_BIN" -I -B -c "$PURITY_PY" compare "$PROJECT_PATH"); then
        PURITY_CHANGES=-1
        return 0
    fi
    read -r _ PURITY_TOTAL PURITY_CHANGES <<< "$(printf '%s\n' "$output" | grep '^__RESULT__' | tail -n 1)"
    if [ "$PURITY_CHANGES" != "0" ]; then
        print_warning "⚠️ 部署期间 venv/ 与 .git/ 之外有 ${PURITY_CHANGES} 个项目条目发生变化 (最多列出20条；有其他程序在写入时属正常)："
        printf '%s\n' "$output" | grep -v '^__RESULT__' || true
    fi
}

show_deployment_info
check_system_environment
check_offline_prerequisites
PURITY_BEFORE="$(project_snapshot)" || PURITY_BEFORE=""

# 检查项目文件完整性
check_project_files() {
    print_info "🔍 检查项目文件完整性..."

    # 核心文件检查
    required_files=(
        "requirements.txt"
        "main.py"
        "start_trading.sh"
        "config/config.py"
        "data/data_fetcher.py"
        "strategy/DMRQuadrantStrategy.py"
        "execution/order_executor.py"
        "utils/logger.py"
        "bit/bit_long_term_main.py"
        "bit/bit_short_term_main.py"
        "bit/start_bit_long_term.sh"
        "bit/start_bit_short_term.sh"
        "tests/offline/test_business_regressions.py"
    )

    missing_files=()
    for file in "${required_files[@]}"; do
        if [ -f "$file" ]; then
            print_success "✅ $file"
        else
            print_error "❌ 缺少文件: $file"
            missing_files+=("$file")
        fi
    done

    if [ ${#missing_files[@]} -gt 0 ]; then
        print_error "❌ 发现 ${#missing_files[@]} 个缺失文件，请检查项目完整性"
        exit 1
    fi

    print_info "真实配置仅检查存在；不读取内容，不验证密钥或配置版本"
}

check_project_files

# 设置脚本执行权限
setup_script_permissions() {
    print_info "🔧 保留所有脚本现有权限，不修改其他文件"
}

# 检测虚拟环境是否可用且属于当前项目路径
# (从其他路径拷贝/迁移而来的venv: activate 中的 VIRTUAL_ENV 与 pip 等入口脚本的 shebang 仍指向旧路径，必须重建)
venv_is_healthy() {
    [ -f venv/bin/activate ] && [ -x venv/bin/python3 ] && [ -f venv/bin/pip ] || return 1
    venv/bin/python3 -I -B - "$PROJECT_PATH/venv" <<'PYEOF' >/dev/null 2>&1
import pathlib, sys, re
p = pathlib.Path(sys.argv[1])
assert sys.version_info[:2] == (3, 12)
assert pathlib.Path(sys.prefix) == p
text = (p / 'bin/activate').read_text()
assert any(v.strip('"\'') == str(p) for v in re.findall(r'^\s*(?:export\s+)?VIRTUAL_ENV=(.+)$', text, re.M))
assert (p / 'bin/pip').read_text().splitlines()[0] in ('#!' + str(p / 'bin/python3'), '#!' + str(p / 'bin/python3.12'), '#!' + str(p / 'bin/python'))
import pip
PYEOF
}

# 创建虚拟环境；不执行未锁定的远程get-pip脚本
create_venv() {
    if ! "$PYTHON_BIN" -I -B - <<'PYEOF'
import subprocess, sys
p = subprocess.run([sys.executable, '-m', 'venv', 'venv'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
if p.returncode:
    print('❌ 虚拟环境创建失败；请检查匹配的 venv/ensurepip 系统组件')
sys.exit(p.returncode)
PYEOF
    then
        return 1
    fi
}

# 智能虚拟环境管理
manage_virtual_environment() {
    print_info "📦 管理虚拟环境..."
    if [ -L venv ]; then
        print_error "❌ 拒绝修改符号链接venv，请先人工确认目标环境"
        return 1
    fi

    if [ "$FORCE_REBUILD" = "1" ] && [ -d "venv" ]; then
        print_warning "⚠️ 已指定纯净重建，删除旧虚拟环境..."
        rm -rf venv
        DEPLOY_MODE="强制纯净重建"
    fi

    if [ ! -d "venv" ]; then
        print_warning "⚠️ 虚拟环境不存在，正在创建..."
        create_venv || return 1
        [ "$DEPLOY_MODE" = "强制纯净重建" ] || DEPLOY_MODE="从空环境新建"
        print_success "✅ 虚拟环境创建完成"
    elif venv_is_healthy; then
        DEPLOY_MODE="复用健康环境"
        print_success "✅ 虚拟环境已存在且路径一致: $PROJECT_PATH/venv"
    else
        # 检查虚拟环境是否损坏或由其他路径迁移而来
        print_warning "⚠️ 虚拟环境损坏或由其他路径迁移而来 (activate/pip 仍指向旧路径)，正在纯净重建..."
        rm -rf venv
        create_venv || return 1
        DEPLOY_MODE="迁移自愈重建"
        print_success "✅ 虚拟环境重新创建完成"
    fi

    if ! venv_is_healthy; then
        print_error "❌ 虚拟环境不可用，请检查 python3-venv 是否已安装"
        exit 1
    fi
}

# 安装和验证依赖包
install_dependencies() {
    print_info "📥 按精确清单在线安装依赖；安装输出仅保留内存，不生成日志或缓存"
    local output rc=0
    output=$(venv/bin/python3 -I -B - <<'PYEOF'
import importlib.metadata as md
import os, re, subprocess, sys
required = {}
for line in open('requirements.txt', encoding='utf-8'):
    line = line.split('#', 1)[0].strip()
    if not line: continue
    match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)', line)
    if not match: print('❌ 依赖清单包含非精确版本条目'); sys.exit(1)
    name, value = match.groups()
    if name in required: print('❌ 依赖清单存在重复条目'); sys.exit(1)
    required[name] = value
if not required: print('❌ 依赖清单为空'); sys.exit(1)
def matches():
    count = 0
    for name, expected in required.items():
        try: count += md.version(name) == expected
        except md.PackageNotFoundError: pass
    return count
env = dict(os.environ, PIP_CONFIG_FILE=os.devnull, PIP_NO_CACHE_DIR='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
env.pop('PIP_LOG', None)
def pip(*args):
    return subprocess.run([sys.executable, '-I', '-B', '-m', 'pip', *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
if matches() != len(required):
    result = pip('install', '--no-cache-dir', '--no-compile', '-r', 'requirements.txt')
    if result.returncode:
        text = (result.stdout + result.stderr).decode(errors='replace').lower()
        kind = '依赖解析或构建失败'
        if 'no matching distribution' in text or 'could not find a version' in text: kind = '包源未提供指定版本或当前平台发行包'
        elif 'connection' in text or 'timed out' in text: kind = '包源网络连接失败'
        print('❌ 安装失败：' + kind + '；原始输出已隐去以保护包源认证信息')
        sys.exit(1)
    print('✅ 在线安装完成')
else:
    print('✅ 全部精确版本已安装，复用健康环境，不重复下载安装')
count = matches()
print(f'✅ 依赖安装元数据：{count}/{len(required)} 精确匹配')
check = pip('check')
if check.returncode: print('❌ 依赖冲突检查失败（详情已脱敏）'); sys.exit(1)
print('✅ 依赖冲突检查：无冲突')
print(f'__RESULT__ {len(required)} {count}')
sys.exit(0 if count == len(required) else 1)
PYEOF
    ) || rc=$?
    printf '%s\n' "$output" | grep -v '^__RESULT__' || true
    [ "$rc" -eq 0 ] || return "$rc"
    read -r _ REQ_TOTAL REQ_MATCH <<< "$(printf '%s\n' "$output" | grep '^__RESULT__')"
    PIP_CHECK_RESULT="无冲突"
}

setup_script_permissions
manage_virtual_environment
install_dependencies

# 配置验证
validate_configuration() {
    print_info "🔧 真实配置仅验证文件存在；禁止读取、执行或输出配置内容"
    [ -f config/config.py ] || return 1
    print_warning "⚠️ 后续业务导入使用明确合成配置，不代表真实配置或账户验证通过"
}

# 核心模块严格导入测试 (主系统 + BIT专属策略，任一失败即返回非0)
verify_project_modules() {
    print_info "🔍 离线依赖功能、合成配置业务导入与现有业务回归验证..."
    local output rc=0
    output=$(venv/bin/python3 -I -B - <<'PYEOF'
import contextlib, ctypes, errno, importlib, io, os, pathlib, socket, sys, tempfile, types, unittest
root = pathlib.Path.cwd().resolve()
sys.path.insert(0, str(root))
# 测试阶段操作系统级禁网；不影响前面的包源安装。
try:
    sec = ctypes.CDLL('libseccomp.so.2')
    sec.seccomp_init.argtypes = [ctypes.c_uint32]; sec.seccomp_init.restype = ctypes.c_void_p
    sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]; sec.seccomp_syscall_resolve_name.restype = ctypes.c_int
    sec.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    sec.seccomp_load.argtypes = [ctypes.c_void_p]; sec.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = sec.seccomp_init(0x7fff0000)
    if not ctx: raise RuntimeError()
    try:
        for name in ('connect', 'sendto', 'sendmsg', 'sendmmsg'):
            num = sec.seccomp_syscall_resolve_name(name.encode())
            if num >= 0 and sec.seccomp_rule_add(ctx, 0x50000 | errno.EPERM, num, 0): raise RuntimeError()
        if sec.seccomp_load(ctx): raise RuntimeError()
    finally: sec.seccomp_release(ctx)
    with socket.socket() as probe:
        try: probe.connect(('127.0.0.1', 9))
        except PermissionError: pass
        else: raise RuntimeError()
except Exception:
    print('❌ 无法启用或确认操作系统级测试禁网；安全停止')
    sys.exit(1)
print('✅ 测试阶段禁网：操作系统过滤器及回环拒绝探针通过')
# 临时目录只容纳自动清除的绘图库缓存与回归状态，不容纳报告或日志。
with tempfile.TemporaryDirectory(prefix='dmr-offline-') as folder:
    safe = pathlib.Path(folder).resolve()
    os.environ['MPLCONFIGDIR'] = str(safe / 'matplotlib')
    os.environ['XDG_CACHE_HOME'] = str(safe / 'cache')
    os.environ['MPLBACKEND'] = 'Agg'
    tempfile.tempdir = str(safe)
    def path_of(p, fd=None):
        if isinstance(p, int): return pathlib.Path(os.readlink(f'/proc/self/fd/{p}')).resolve()
        p = pathlib.Path(os.fsdecode(p))
        if not p.is_absolute() and fd is not None and fd >= 0:
            p = pathlib.Path(os.readlink(f'/proc/self/fd/{fd}')) / p
        return p.resolve()
    def write_allowed(p, fd=None):
        p = path_of(p, fd)
        if not (p == safe or safe in p.parents) or p.suffix in ('.log', '.report'):
            raise PermissionError('离线验证禁止写入')
    def audit(event, args):
        if event == 'open':
            p, mode, flags = args
            resolved = path_of(p)
            if resolved == root / '.env' or root / 'config' in resolved.parents or root / 'bit/config' in resolved.parents:
                raise PermissionError('禁止读取真实配置')
            if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                write_allowed(p)
        elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.chown', 'os.utime', 'os.truncate'):
            fd = args[-1] if event in ('os.mkdir','os.remove','os.rmdir') else None
            write_allowed(args[0], fd)
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            raise PermissionError('离线验证禁止重命名或链接')
        elif event in ('subprocess.Popen','os.system','os.fork','os.posix_spawn','socket.connect','socket.getaddrinfo','socket.sendto','socket.sendmsg'):
            raise PermissionError('离线验证禁止进程或网络操作')
    sys.addaudithook(audit)
    captured = io.StringIO()
    main = ['data.data_fetcher', 'strategy.DMRQuadrantStrategy', 'execution.order_executor', 'utils.logger', 'strategy.multi_strategy']
    bit = [f'bit.strategy.{term}.{mod}' for term in ('bit_long_term','bit_short_term') for mod in ('data_fetcher','strategy_engine','order_executor','position_manager','risk_manager')] + ['bit.utils.logger']
    third = ['numpy','pandas','numexpr','ccxt','binance','backtrader','matplotlib.pyplot','aiohttp','aiodns','websockets','requests','cryptography','Crypto','PIL','orjson','uvloop','coincurve','schedule','dateparser','tzlocal','packaging','cffi','pycares','aiohttp_fast_zlib','zlib_ng.zlib_ng']
    stage = '第三方导入'
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            for name in third: importlib.import_module(name)
            import numpy as np, pandas as pd, numexpr as ne
            stage = '数值与数据框计算'
            a = np.arange(10, dtype=float)
            np.testing.assert_allclose(ne.evaluate('a * a + 1'), a*a+1)
            frame = pd.DataFrame({'x': [1.,2.,3.,4.]})
            assert frame.x.rolling(2).mean().iloc[-1] == 3.5
            child = frame.copy(deep=False); child.iloc[0,0] = 99
            assert frame.iloc[0,0] == 1
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(); ax.plot(a); fig.savefig(io.BytesIO(), format='png'); plt.close(fig)
            stage = '合成配置建立'
            def synthetic(name, values=None, package=False):
                mod = types.ModuleType(name)
                mod.__dict__.update(values or {})
                mod.__dict__['SYNTHETIC_DEPLOYMENT_FIXTURE'] = True
                if package: mod.__path__ = []
                sys.modules[name] = mod
                if '.' in name:
                    parent, attr = name.rsplit('.',1)
                    if parent in sys.modules: setattr(sys.modules[parent], attr, mod)
                return mod
            synthetic('config', package=True)
            synthetic('config.config', dict(API_KEY='offline-synthetic', API_SECRET='offline-synthetic', SYMBOL='X/USDT', POSITION_SIZE=1, TIMEFRAME_LONG='1h', MA_LONG_PERIOD=20, MA_SHORT_PERIOD=5, DATA_FETCHER_CONFIG={}, ORDER_EXECUTOR_CONFIG={}, LOG_LEVEL='INFO', LOG_FILE=str(root/'forbidden-offline.log'), APPLY_ACCOUNT_SETTINGS=False, APPLY_LEVERAGE=10, APPLY_MARGIN_TYPE='CROSSED'))
            synthetic('bit.config', package=True)
            synthetic('bit.config.config', dict(LOG_LEVEL='INFO', LOG_FILE=str(root/'forbidden-bit.log')))
            synthetic('bit.config.bit_config', dict(DMR_LONG_PERIOD=20, DMR_SHORT_PERIOD=5))
            synthetic('bit.config.feature_switches', dict(FEATURE_SWITCHES={}, POSITION_CLAIM_CONFIG={}))
            for term in ('long','short'):
                synthetic(f'bit.config.bit_{term}_term_config', {f'{term.upper()}_TERM_CONFIG':{}, f'get_{term}_term_config':lambda: {}, 'TRAILING_STOP_CONFIG':{}})
            for name in main + bit:
                stage = name
                importlib.import_module(name)
            stage = '现有离线业务回归'
            import runpy
            ns = runpy.run_path(str(root/'tests/offline/test_business_regressions.py'), run_name='deployment_offline_tests')
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(ns['BusinessTests'])
            result = unittest.TextTestRunner(stream=captured, verbosity=0).run(suite)
            if not result.wasSuccessful():
                print('回归失败', file=captured)
                failed = ', '.join(t.id().split('.')[-1] for t, _ in result.failures + result.errors)
                raise AssertionError(failed)
        print(f'✅ 第三方实际导入：{len(third)}/{len(third)}；向量、滚动均值、写时复制与内存绘图通过')
        print(f'✅ 合成配置下真实业务模块导入：主系统 {len(main)}/{len(main)}；BIT {len(bit)}/{len(bit)}；配置替身不计入真实模块数')
        print(f'✅ 现有离线业务回归：{result.testsRun} 项通过；不代表全部业务或实盘正确')
        print(f'__RESULT__ {len(main)} {len(main)} {len(bit)} {len(bit)}')
    except Exception as exc:
        print(f'❌ 离线验证失败：{stage}；异常类别：{type(exc).__name__}；详情仅存内存，不输出秘密')
        sys.exit(1)
PYEOF
    ) || rc=$?
    printf '%s\n' "$output" | grep -v '^__RESULT__' || true
    [ "$rc" -eq 0 ] || return "$rc"
    read -r _ MAIN_MOD_OK MAIN_MOD_TOTAL BIT_MOD_OK BIT_MOD_TOTAL <<< "$(printf '%s\n' "$output" | grep '^__RESULT__')"
}

# 启动脚本兼容性测试入口（只输出跳过原因；实际离线测试由上一步执行）
run_start_trading_test() {
    NETWORK_TEST_OK=2
    print_info "兼容测试入口保留：不执行会读取真实配置或联网的 start_trading.sh test；由上一步离线回归替代"
}

# 运行系统测试
run_system_tests() {
    print_info "🧪 运行安全离线系统测试..."
    if verify_project_modules && run_start_trading_test; then
        return 0
    fi
    print_error "❌ 离线测试失败；保留已安装环境，不因业务失败重建依赖"
    return 1
}

validate_configuration
if run_system_tests; then
    SYSTEM_TEST_OK=1
else
    print_error "❌ 系统测试未通过，停止部署，不进入启动菜单"
    exit 1
fi

# 纯净部署策略：不生成任何临时报告或日志文件
# (v3.4 起移除 deployment_report_*.txt 的生成，部署摘要仅在终端输出；
#  v3.6 起修复流程不再调用 fix，且不写入 __pycache__；
#  v3.9 仍不落盘：环境处置方式与验收计数只在终端输出，由人工回填持久化文档；
#  v4.0 起由纯净部署自检在每次运行结束时核对 venv/ 与 .git/ 之外没有新增、消失或改动的条目；
#  已有的 DEPLOYMENT_SUCCESS_REPORT.md / Requirements_DEPLOYMENT_GUIDE.md 作为持久化文档)

# 显示部署完成信息
show_deployment_summary() {
    print_success "🎉 依赖部署与有限离线验证完成"
    print_info "项目版本: $PROJECT_VERSION；解释器: $PYTHON_BIN ($PYTHON_VERSION)"
    print_info "本次环境处置: $DEPLOY_MODE"
    print_info "真实配置、密钥、账户及交易网络均未验证"
    print_info "重跑部署: bash deploy_and_run.sh --yes；显式重建: bash deploy_and_run.sh --yes --rebuild"
    print_warning "手动启动交易前必须另行完成真实配置审核、测试网验证及业务安全审核"
}

show_deployment_summary

# 智能启动选择
interactive_startup() {
    print_info "部署模式不启动交易、不连接账户；已移除交互实盘试运行路径"
}

# 最终状态检查
final_status_check() {
    print_info "🔍 最终状态检查..."

    local status_ok=true

    # 检查虚拟环境 (存在、可执行且路径与当前项目一致)
    if venv_is_healthy; then
        print_success "✅ 虚拟环境: 正常 (路径一致)"
    else
        print_error "❌ 虚拟环境: 异常"
        status_ok=false
    fi

    # 检查依赖版本
    if [ "$REQ_TOTAL" -gt 0 ] && [ "$REQ_MATCH" = "$REQ_TOTAL" ]; then
        print_success "✅ 依赖包: ${REQ_MATCH}/${REQ_TOTAL} 精确匹配 (pip check: $PIP_CHECK_RESULT)"
    else
        print_error "❌ 依赖包: ${REQ_MATCH}/${REQ_TOTAL} 匹配"
        status_ok=false
    fi

    # 检查核心文件
    if [ -f "main.py" ] && [ -f "config/config.py" ]; then
        print_success "✅ 核心文件: 完整"
    else
        print_error "❌ 核心文件: 缺失"
        status_ok=false
    fi

    # 检查启动脚本
    if [ -x "start_trading.sh" ]; then
        print_success "✅ 启动脚本: 可执行"
    else
        print_error "❌ 启动脚本: 权限问题"
        status_ok=false
    fi

    # 检查公共网络 (仅警告)
    if [ "$NETWORK_TEST_OK" = "2" ]; then
        print_warning "⚠️ 公共网络: 未测试 (离线验证不代表实盘就绪)"
    elif [ "$NETWORK_TEST_OK" = "1" ]; then
        print_success "✅ 公共网络: 正常"
    else
        print_warning "⚠️ 公共网络: 连接测试失败 (不影响部署判定)"
    fi

    # 检查系统测试
    if [ "$SYSTEM_TEST_OK" = "1" ]; then
        print_success "✅ 系统测试: 通过"
    else
        print_error "❌ 系统测试: 未通过"
        status_ok=false
    fi

    # 纯净部署自检 (仅提示，不改变部署判定)
    if [ "$PURITY_CHANGES" = "0" ]; then
        print_success "✅ 纯净部署: venv/ 与 .git/ 之外 ${PURITY_TOTAL} 个项目条目无新增、消失或改动"
    elif [ "$PURITY_CHANGES" = "-1" ]; then
        print_warning "⚠️ 纯净部署: 自检未完成"
    else
        print_warning "⚠️ 纯净部署: venv/ 与 .git/ 之外有 ${PURITY_CHANGES} 个项目条目发生变化 (见上方清单)"
    fi

    if $status_ok; then
        print_success "🎯 部署检查通过；实盘账户及交易行为未验证"
        return 0
    else
        print_warning "⚠️ 系统状态: 需要检查"
        return 1
    fi
}

interactive_startup
check_deployment_purity

echo ""
if final_status_check; then
    print_success "🎉 DMR量化交易系统 $PROJECT_VERSION 部署成功完成！"
    print_info "✅ 部署路径: $PROJECT_PATH"
    print_info "✅ 部署时间: $CURRENT_TIME"
    print_info "✅ 环境处置: $DEPLOY_MODE"
    print_info "✅ Python版本: $(venv/bin/python3 --version | cut -d' ' -f2)"
    print_info "✅ Pip版本: $(venv/bin/python3 -I -B -m pip --version | cut -d' ' -f2)"
    print_info "✅ 依赖包安装: ${REQ_MATCH}/${REQ_TOTAL} 个依赖包100%成功安装 (pip check: $PIP_CHECK_RESULT)"
    print_info "✅ 模块导入测试: 主系统 ${MAIN_MOD_OK}/${MAIN_MOD_TOTAL} | BIT专属策略 ${BIT_MOD_OK}/${BIT_MOD_TOTAL}"
    print_info "✅ 版本匹配率: 100% (完全匹配requirements.txt v$REQUIREMENTS_VERSION)"
    print_info "📊 Requirements版本: v$REQUIREMENTS_VERSION ($REQUIREMENTS_DATE)"
    print_info "🚀 核心包: $(grep -E '^(numpy|pandas|ccxt|aiohttp)==' requirements.txt | sed -E 's/[[:space:]].*//; s/==/ /' | paste -sd'|' | sed 's/|/ | /g')"
    print_info "🎯 依赖/模块部署检查通过；实盘使用前仍需业务回归和人工安全审核"
    print_info ""
    print_info "真实配置未读取；账户与实盘未测试；不会自动启动交易"
else
    print_warning "⚠️ 部署完成但发现一些问题，建议运行: ./deploy_and_run.sh --yes --rebuild"
    print_info "如需帮助，请查看 Requirements_DEPLOYMENT_GUIDE.md 故障排除章节"
    exit 1
fi
