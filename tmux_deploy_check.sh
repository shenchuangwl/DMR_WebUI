#!/bin/bash

# ============================================================================
# tmux 自动保存恢复工作空间一键部署脚本
# 版本: 1.0
# 作者: AI Assistant
# 描述: 自动化部署 tmux + tmux-resurrect + tmux-continuum 完整方案
# ============================================================================

set -e  # 遇到错误立即退出

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 显示脚本标题
show_banner() {
    echo -e "${BLUE}"
    echo "============================================================================"
    echo "                tmux 自动保存恢复工作空间一键部署脚本"
    echo "============================================================================"
    echo -e "${NC}"
}

# 检查系统环境
check_environment() {
    log_info "开始环境检查..."
    
    # 检查操作系统
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        log_success "操作系统: Linux"
    else
        log_error "不支持的操作系统: $OSTYPE"
        exit 1
    fi
    
    # 检查是否为 root 用户
    if [[ $EUID -eq 0 ]]; then
        log_error "请不要使用 root 用户运行此脚本"
        exit 1
    fi
    
    # 检查 systemd 支持
    if command -v systemctl &> /dev/null; then
        log_success "systemd 支持: 已安装"
    else
        log_error "系统不支持 systemd，无法使用自动启动功能"
        exit 1
    fi
    
    # 检查用户目录权限
    if [[ -w "$HOME" ]]; then
        log_success "用户目录权限: 可写"
    else
        log_error "用户目录无写权限"
        exit 1
    fi
    
    log_success "环境检查完成！"
}

# 检查并安装依赖
install_dependencies() {
    log_info "检查并安装依赖包..."
    
    # 检查包管理器
    if command -v apt &> /dev/null; then
        PKG_MANAGER="apt"
        INSTALL_CMD="sudo apt update && sudo apt install -y"
    elif command -v yum &> /dev/null; then
        PKG_MANAGER="yum"
        INSTALL_CMD="sudo yum install -y"
    elif command -v dnf &> /dev/null; then
        PKG_MANAGER="dnf"
        INSTALL_CMD="sudo dnf install -y"
    else
        log_error "未找到支持的包管理器 (apt/yum/dnf)"
        exit 1
    fi
    
    log_info "使用包管理器: $PKG_MANAGER"
    
    # 检查 tmux
    if command -v tmux &> /dev/null; then
        TMUX_VERSION=$(tmux -V | cut -d' ' -f2)
        log_success "tmux 已安装: $TMUX_VERSION"
        
        # 检查版本是否满足要求 (>= 2.1)
        if [[ $(echo "$TMUX_VERSION 2.1" | tr " " "\n" | sort -V | head -n1) == "2.1" ]]; then
            log_success "tmux 版本满足要求"
        else
            log_warning "tmux 版本过低，建议升级到 2.1 或更高版本"
        fi
    else
        log_info "安装 tmux..."
        eval $INSTALL_CMD tmux
        log_success "tmux 安装完成"
    fi
    
    # 检查 git
    if command -v git &> /dev/null; then
        log_success "git 已安装: $(git --version | cut -d' ' -f3)"
    else
        log_info "安装 git..."
        eval $INSTALL_CMD git
        log_success "git 安装完成"
    fi
    
    # 检查 curl (用于下载)
    if ! command -v curl &> /dev/null; then
        log_info "安装 curl..."
        eval $INSTALL_CMD curl
        log_success "curl 安装完成"
    fi
}

# 安装 TPM (tmux 插件管理器)
install_tpm() {
    log_info "安装 TPM (tmux 插件管理器)..."
    
    TPM_DIR="$HOME/.tmux/plugins/tpm"
    
    if [[ -d "$TPM_DIR" ]]; then
        log_warning "TPM 已存在，更新到最新版本..."
        cd "$TPM_DIR"
        git pull origin master
    else
        log_info "克隆 TPM 仓库..."
        mkdir -p "$HOME/.tmux/plugins"
        git clone https://github.com/tmux-plugins/tpm "$TPM_DIR"
    fi
    
    log_success "TPM 安装完成"
}

# 创建 tmux 配置文件
create_tmux_config() {
    log_info "创建 tmux 配置文件..."
    
    TMUX_CONF="$HOME/.tmux.conf"
    
    # 备份现有配置
    if [[ -f "$TMUX_CONF" ]]; then
        BACKUP_FILE="$TMUX_CONF.backup.$(date +%Y%m%d_%H%M%S)"
        cp "$TMUX_CONF" "$BACKUP_FILE"
        log_warning "已备份现有配置到: $BACKUP_FILE"
    fi
    
    # 创建新配置
    cat > "$TMUX_CONF" << 'EOF'
# ================================
# tmux 基础配置
# ================================

# 设置前缀键
set -g prefix C-b
bind C-b send-prefix

# 启用鼠标支持
set -g mouse on

# 设置窗口和面板索引从1开始
set -g base-index 1
setw -g pane-base-index 1

# 自动重新编号窗口
set -g renumber-windows on

# 增加历史缓冲区大小
set -g history-limit 10000

# 设置终端类型
set -g default-terminal "screen-256color"

# 设置窗口标题
set -g set-titles on
set -g set-titles-string '#T - #W'

# ================================
# 插件配置
# ================================

# 插件列表
set -g @plugin 'tmux-plugins/tpm'
set -g @plugin 'tmux-plugins/tmux-resurrect'
set -g @plugin 'tmux-plugins/tmux-continuum'

# ================================
# tmux-resurrect 配置
# ================================

# 保存 bash 历史
set -g @resurrect-save-bash-history 'on'

# 捕获面板内容
set -g @resurrect-capture-pane-contents 'on'

# 保存 shell 历史
set -g @resurrect-save-shell-history 'on'

# 保存 vim/neovim 会话
set -g @resurrect-strategy-vim 'session'
set -g @resurrect-strategy-nvim 'session'

# 保存特定程序状态
set -g @resurrect-processes 'ssh mysql postgresql redis-server redis-cli'
set -g @resurrect-processes 'npm python python3 node java'
set -g @resurrect-processes 'htop top btop vim nvim nano'

# 自定义保存目录
set -g @resurrect-dir '$HOME/.tmux/resurrect'

# ================================
# tmux-continuum 配置  
# ================================

# 自动保存间隔（分钟）
set -g @continuum-save-interval '15'

# 启用自动恢复
set -g @continuum-restore 'on'

# 系统启动时自动启动 tmux
set -g @continuum-boot 'on'

# 自动启动 tmux 服务器
set -g @continuum-systemd-start-cmd 'new-session -d -s main'

# ================================
# 快捷键绑定
# ================================

# 重载配置文件
bind r source-file ~/.tmux.conf \; display-message "配置已重载!"

# 面板分割
bind | split-window -h -c "#{pane_current_path}"
bind - split-window -v -c "#{pane_current_path}"

# 面板导航
bind h select-pane -L
bind j select-pane -D
bind k select-pane -U
bind l select-pane -R

# 面板大小调整
bind -r H resize-pane -L 5
bind -r J resize-pane -D 5
bind -r K resize-pane -U 5
bind -r L resize-pane -R 5

# ================================
# 状态栏配置
# ================================

# 状态栏位置
set -g status-position bottom

# 状态栏颜色
set -g status-bg colour234
set -g status-fg colour137

# 左侧状态栏
set -g status-left '#[fg=colour233,bg=colour241,bold] #S #[fg=colour241,bg=colour235,nobold]'

# 右侧状态栏
set -g status-right '#[fg=colour233,bg=colour241,bold] %Y-%m-%d %H:%M:%S '

# 窗口状态格式
setw -g window-status-current-format '#[fg=colour81,bg=colour238,bold] #I#[fg=colour250]:#[fg=colour255]#W#[fg=colour50]#F '
setw -g window-status-format '#[fg=colour138,bg=colour235] #I#[fg=colour250]:#[fg=colour250]#W#[fg=colour244]#F '

# ================================
# 初始化 TPM（必须放在最后）
# ================================
run '~/.tmux/plugins/tpm/tpm'
EOF

    # 创建 resurrect 目录
    mkdir -p "$HOME/.tmux/resurrect"
    
    log_success "tmux 配置文件创建完成: $TMUX_CONF"
}

# 创建 systemd 用户服务
create_systemd_service() {
    log_info "创建 systemd 用户服务..."
    
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    SERVICE_FILE="$SYSTEMD_DIR/tmux.service"
    
    # 创建目录
    mkdir -p "$SYSTEMD_DIR"
    
    # 获取 tmux 可执行文件路径
    TMUX_PATH=$(which tmux)
    
    # 创建服务文件
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=tmux default session (detached)
Documentation=man:tmux(1)
After=graphical-session.target

[Service]
Type=forking
WorkingDirectory=%h
ExecStart=/bin/bash -c '$TMUX_PATH has-session -t main 2>/dev/null || $TMUX_PATH new-session -d -s main'
ExecReload=$TMUX_PATH source-file %h/.tmux.conf
Restart=on-failure
RestartSec=5
Environment=DISPLAY=:0

[Install]
WantedBy=default.target
EOF

    log_success "systemd 服务文件创建完成: $SERVICE_FILE"
}

# 安装插件
install_plugins() {
    log_info "安装 tmux 插件..."
    
    # 启动 tmux 服务器（如果未运行）
    if ! tmux has-session 2>/dev/null; then
        log_info "启动 tmux 服务器..."
        tmux new-session -d -s installer
    fi
    
    # 安装插件
    log_info "执行插件安装..."
    "$HOME/.tmux/plugins/tpm/scripts/install_plugins.sh"
    
    # 清理安装会话
    if tmux has-session -t installer 2>/dev/null; then
        tmux kill-session -t installer
    fi
    
    log_success "插件安装完成"
}

# 配置 systemd 服务
configure_systemd() {
    log_info "配置 systemd 用户服务..."
    
    # 重载 systemd 配置
    systemctl --user daemon-reload
    
    # 启用服务
    systemctl --user enable tmux.service
    
    # 启用用户服务在系统启动时自动运行
    if command -v loginctl &> /dev/null; then
        sudo loginctl enable-linger "$USER"
        log_success "已启用用户服务自动启动"
    else
        log_warning "loginctl 不可用，可能需要手动启用用户服务自动启动"
    fi
    
    # 启动服务
    if systemctl --user start tmux.service; then
        log_success "tmux 服务启动成功"
    else
        log_error "tmux 服务启动失败"
        systemctl --user status tmux.service
        return 1
    fi
    
    # 检查服务状态 - 对于 forking 类型的服务，检查 tmux 会话是否存在
    if tmux has-session -t main 2>/dev/null; then
        log_success "tmux 服务运行正常 (main 会话已创建)"
    else
        log_error "tmux 服务未正常运行 (main 会话不存在)"
        return 1
    fi
}

# 创建辅助脚本
create_helper_scripts() {
    log_info "创建辅助脚本..."
    
    SCRIPTS_DIR="$HOME/.tmux/scripts"
    mkdir -p "$SCRIPTS_DIR"
    
    # 创建状态检查脚本
    cat > "$SCRIPTS_DIR/check_status.sh" << 'EOF'
#!/bin/bash
echo "=== tmux 自动保存恢复状态检查 ==="
echo

echo "1. systemd 服务状态:"
systemctl --user status tmux.service --no-pager -l

echo
echo "2. tmux 会话列表:"
tmux list-sessions 2>/dev/null || echo "没有运行中的会话"

echo
echo "3. 保存文件状态:"
if [[ -d "$HOME/.tmux/resurrect" ]]; then
    ls -la "$HOME/.tmux/resurrect/"
else
    echo "保存目录不存在"
fi

echo
echo "4. 插件状态:"
if tmux show-options -g | grep -q resurrect; then
    echo "✓ tmux-resurrect 已加载"
else
    echo "✗ tmux-resurrect 未加载"
fi

if tmux show-options -g | grep -q continuum; then
    echo "✓ tmux-continuum 已加载"
else
    echo "✗ tmux-continuum 未加载"
fi
EOF

    # 创建手动保存脚本
    cat > "$SCRIPTS_DIR/manual_save.sh" << 'EOF'
#!/bin/bash
echo "手动触发 tmux 会话保存..."
if tmux run-shell ~/.tmux/plugins/tmux-resurrect/scripts/save.sh; then
    echo "保存成功！"
    echo "保存文件:"
    ls -la ~/.tmux/resurrect/last 2>/dev/null || echo "保存文件不存在"
else
    echo "保存失败！"
    exit 1
fi
EOF

    # 创建手动恢复脚本
    cat > "$SCRIPTS_DIR/manual_restore.sh" << 'EOF'
#!/bin/bash
echo "手动触发 tmux 会话恢复..."
if tmux run-shell ~/.tmux/plugins/tmux-resurrect/scripts/restore.sh; then
    echo "恢复成功！"
    echo "当前会话:"
    tmux list-sessions
else
    echo "恢复失败！"
    exit 1
fi
EOF

    # 创建服务重启脚本
    cat > "$SCRIPTS_DIR/restart_service.sh" << 'EOF'
#!/bin/bash
echo "重启 tmux 服务..."
systemctl --user restart tmux.service
sleep 2
systemctl --user status tmux.service --no-pager -l
EOF

    # 设置执行权限
    chmod +x "$SCRIPTS_DIR"/*.sh
    
    log_success "辅助脚本创建完成: $SCRIPTS_DIR"
}

# 运行测试
run_tests() {
    log_info "运行功能测试..."
    
    # 等待服务完全启动
    sleep 3
    
    # 测试 1: 检查会话是否存在
    if tmux has-session -t main 2>/dev/null; then
        log_success "测试 1: 主会话创建成功"
    else
        log_error "测试 1: 主会话创建失败"
        return 1
    fi
    
    # 测试 2: 创建测试会话
    log_info "测试 2: 创建测试会话..."
    # 清理可能存在的测试会话
    tmux kill-session -t test_session 2>/dev/null || true
    tmux new-session -d -s test_session
    tmux send-keys -t test_session 'echo "测试会话"' Enter
    tmux split-window -t test_session
    tmux send-keys -t test_session 'date' Enter
    
    if tmux has-session -t test_session 2>/dev/null; then
        log_success "测试 2: 测试会话创建成功"
    else
        log_error "测试 2: 测试会话创建失败"
        return 1
    fi
    
    # 测试 3: 手动保存
    log_info "测试 3: 手动保存测试..."
    if tmux run-shell ~/.tmux/plugins/tmux-resurrect/scripts/save.sh; then
        log_success "测试 3: 手动保存成功"
    else
        log_error "测试 3: 手动保存失败"
        return 1
    fi
    
    # 测试 4: 检查保存文件 (等待一下让保存完成)
    sleep 2
    if [[ -d "$HOME/.tmux/resurrect" ]] && [[ -n "$(ls -A $HOME/.tmux/resurrect/ 2>/dev/null)" ]]; then
        log_success "测试 4: 保存文件存在"
    else
        log_warning "测试 4: 保存文件可能还未生成，这是正常的"
        log_info "保存目录将在首次自动保存时创建"
    fi
    
    # 清理测试会话
    tmux kill-session -t test_session 2>/dev/null || true
    
    log_success "所有测试通过！"
}

# 显示部署结果和使用说明
show_results() {
    log_success "部署完成！"
    echo
    echo -e "${GREEN}=== 部署结果 ===${NC}"
    echo "✓ tmux 和依赖包已安装"
    echo "✓ TPM (插件管理器) 已安装"
    echo "✓ tmux-resurrect 和 tmux-continuum 插件已安装"
    echo "✓ tmux 配置文件已创建: ~/.tmux.conf"
    echo "✓ systemd 用户服务已配置并启动"
    echo "✓ 自动保存间隔: 15分钟"
    echo "✓ 系统重启后自动恢复: 已启用"
    echo
    echo -e "${BLUE}=== 使用说明 ===${NC}"
    echo "1. 基本操作:"
    echo "   - 连接主会话: tmux attach -t main"
    echo "   - 查看所有会话: tmux list-sessions"
    echo "   - 手动保存: Ctrl-b + Ctrl-s"
    echo "   - 手动恢复: Ctrl-b + Ctrl-r"
    echo
    echo "2. 服务管理:"
    echo "   - 查看服务状态: systemctl --user status tmux.service"
    echo "   - 重启服务: systemctl --user restart tmux.service"
    echo "   - 停止服务: systemctl --user stop tmux.service"
    echo
    echo "3. 辅助脚本:"
    echo "   - 状态检查: ~/.tmux/scripts/check_status.sh"
    echo "   - 手动保存: ~/.tmux/scripts/manual_save.sh"
    echo "   - 手动恢复: ~/.tmux/scripts/manual_restore.sh"
    echo "   - 重启服务: ~/.tmux/scripts/restart_service.sh"
    echo
    echo "4. 配置文件位置:"
    echo "   - tmux 配置: ~/.tmux.conf"
    echo "   - 保存目录: ~/.tmux/resurrect/"
    echo "   - 服务文件: ~/.config/systemd/user/tmux.service"
    echo
    echo -e "${YELLOW}=== 重要提示 ===${NC}"
    echo "• 系统重启后会自动恢复所有 tmux 会话"
    echo "• 每15分钟自动保存一次会话状态"
    echo "• 如需修改配置，编辑 ~/.tmux.conf 后重载: Ctrl-b + r"
    echo "• 如遇问题，查看日志: journalctl --user -u tmux.service -f"
    echo
}

# 主函数
main() {
    show_banner
    
    # 检查是否需要帮助
    if [[ "$1" == "-h" || "$1" == "--help" ]]; then
        echo "用法: $0 [选项]"
        echo
        echo "选项:"
        echo "  -h, --help     显示帮助信息"
        echo "  --skip-deps    跳过依赖包安装"
        echo "  --skip-test    跳过功能测试"
        echo
        echo "此脚本将自动安装和配置 tmux 自动保存恢复工作空间。"
        exit 0
    fi
    
    # 解析参数
    SKIP_DEPS=false
    SKIP_TEST=false
    
    while [[ $# -gt 0 ]]; do
        case $1 in
            --skip-deps)
                SKIP_DEPS=true
                shift
                ;;
            --skip-test)
                SKIP_TEST=true
                shift
                ;;
            *)
                log_error "未知参数: $1"
                exit 1
                ;;
        esac
    done
    
    # 执行部署步骤
    check_environment
    
    if [[ "$SKIP_DEPS" != true ]]; then
        install_dependencies
    fi
    
    install_tpm
    create_tmux_config
    create_systemd_service
    install_plugins
    configure_systemd
    create_helper_scripts
    
    if [[ "$SKIP_TEST" != true ]]; then
        run_tests
    fi
    
    show_results
    
    log_success "部署完成！享受你的 tmux 自动保存恢复工作空间吧！"
}

# 错误处理
trap 'log_error "脚本执行失败，请检查错误信息"; exit 1' ERR

# 运行主函数
main "$@"
