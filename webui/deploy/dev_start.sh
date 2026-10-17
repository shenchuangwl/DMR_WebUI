#!/usr/bin/env bash
# deploy/dev_start.sh —— 开发用：后台依次启动 替身交易所 → 交易内核（经 supervise.sh 守护）→ Web 控制台，打印三个地址与状态。
#
#   替身交易所  .venv-kernel/bin/python -m dmr_standin --feed $DMR_STANDIN_FEED      127.0.0.1:$DMR_STANDIN_PORT（18220）
#   交易内核    .venv-kernel/bin/python -m dmr_kernel（由 deploy/supervise.sh 守护）  127.0.0.1:$DMR_KERNEL_PORT（18200）
#   Web 控制台  .venv-console/bin/python -m dmr_console                              127.0.0.1:$DMR_CONSOLE_PORT（18210）
#
# 进程号文件在 $DMR_VAR_DIR/run/，日志在 $DMR_VAR_DIR/log/（各进程自己按大小轮转 10MB×5，时间 UTC；*.out 是原始标准输出）。
# 已经在运行的不重复启动（进程号文件指向的进程还在、且命令行对得上才算）。
#
# 环境变量（都可省略）：
#   DMR_STANDIN_PORT=18220 DMR_KERNEL_PORT=18200 DMR_CONSOLE_PORT=18210   端口，1–65535 且互不相同；只绑 127.0.0.1
#   DMR_VAR_DIR=<工程根>/var    运行期目录（相对路径按工程根解析）
#   DMR_STANDIN_FEED=auto       替身交易所的行情源：auto（先试 Binance 公共接口，不可用则回放）| binance | replay
#   DMR_KERNEL_SHELL=1          内核以空壳运行（T0.22 业务装配完成前的自检用；空壳没有控制接口）
#   DMR_DEV_START_TIMEOUT_S=60  每个进程起来并通过健康检查的最长等待
#   其余 DMR_* 变量原样传给被启动的进程。DMR_ENV 只能是 standin；DMR_ALLOW_LIVE 会被清除。
#
# 退出码：0 全部健康；1 某个进程没起来（已打印原因和日志末尾）；2 配置无效（什么都没启动）；
#         3 停用标记 run/kernel.nosupervise 存在（应急入口写的，确认后删除它再启动）。
set -euo pipefail
export PATH=/usr/bin:/bin
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib.sh"

case "${1:-}" in
  "") ;;
  -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  *) die 2 "不认识的参数：$1（用法见 dev_start.sh --help）" ;;
esac

load_config
load_timeouts
require_interpreters
ensure_dirs
acquire_ops_lock

if [ -e "$NOSUP_FILE" ] && ! process_running supervise; then
  die 3 "停用标记 $NOSUP_FILE 存在（命令行应急入口或上一次没停完整的 dev_stop.sh 留下的），内核不会被拉起。确认可以启动后删除该文件再运行本脚本"
fi

# 启动失败：打印原因和日志末尾，退出 1。参数：显示名 日志文件…
start_failed() {
  local label="$1" file
  shift
  printf '错误：%s 没有启动成功（进程退出了，或 %s 秒内健康检查一直没通过）\n' "$label" "$START_TIMEOUT_S" >&2
  for file in "$@"; do
    if [ -s "$file" ]; then
      printf -- '--- %s 的最后 15 行 ---\n' "$file" >&2
      tail -n 15 "$file" >&2 || true
    fi
  done
  exit 1
}

start_standin() {
  if process_running standin; then
    say "替身交易所已在运行（pid $(read_pid standin)），不重复启动"
    return 0
  fi
  rm -f "$RUN_DIR/standin.pid"
  if port_in_use "$DMR_STANDIN_PORT"; then die 1 "端口 $DMR_STANDIN_PORT 已被占用，替身交易所没有启动"; fi
  say "启动替身交易所（行情源 $DMR_STANDIN_FEED）……"
  spawn_daemon standin "$LOG_DIR/standin.out" "$KERNEL_PY" -m dmr_standin --host 127.0.0.1 --port "$DMR_STANDIN_PORT" \
    --state-dir "$DMR_VAR_DIR/standin" --log-dir "$LOG_DIR" --feed "$DMR_STANDIN_FEED"
  wait_ready standin health_standin "$START_TIMEOUT_S" || start_failed "替身交易所" "$LOG_DIR/standin.out" "$LOG_DIR/standin.log"
}

start_kernel() {
  if process_running supervise; then
    say "内核守护已在运行（pid $(read_pid supervise)），不重复启动"
  elif process_running kernel; then
    warn "内核（pid $(read_pid kernel)）在运行，但没有守护进程；先运行 deploy/dev_stop.sh 再启动，内核才会重新带上守护"
    return 0
  else
    rm -f "$RUN_DIR/kernel.pid" "$RUN_DIR/supervise.pid"
    if [ "${DMR_KERNEL_SHELL:-}" = 1 ]; then
      printf 'shell\n' > "$RUN_DIR/kernel.mode"
    else
      if port_in_use "$DMR_KERNEL_PORT"; then die 1 "端口 $DMR_KERNEL_PORT 已被占用，交易内核没有启动"; fi
      printf 'full\n' > "$RUN_DIR/kernel.mode"
    fi
    say "启动交易内核（经 supervise.sh 守护）……"
    spawn_daemon supervise "$LOG_DIR/supervise.log" "$DEPLOY_DIR/supervise.sh"
  fi
  if ! wait_ready kernel health_kernel "$START_TIMEOUT_S" supervise; then
    stop_kernel_group > /dev/null 2>&1 || true  # 内核起不来时不要留下一个在崩溃循环里的守护
    start_failed "交易内核" "$LOG_DIR/kernel.out" "$LOG_DIR/kernel.log" "$LOG_DIR/supervise.log"
  fi
}

start_console() {
  if process_running console; then
    say "Web 控制台已在运行（pid $(read_pid console)），不重复启动"
    return 0
  fi
  rm -f "$RUN_DIR/console.pid"
  if port_in_use "$DMR_CONSOLE_PORT"; then die 1 "端口 $DMR_CONSOLE_PORT 已被占用，Web 控制台没有启动"; fi
  say "启动 Web 控制台……"
  spawn_daemon console "$LOG_DIR/console.out" "$CONSOLE_PY" -m dmr_console
  wait_ready console health_console "$START_TIMEOUT_S" || start_failed "Web 控制台" "$LOG_DIR/console.out" "$LOG_DIR/console.log"
}

start_standin
start_kernel
start_console

say ""
say "DMR 开发环境（替身交易所，非真实交易）  var: $DMR_VAR_DIR"
print_status
say "日志：$LOG_DIR/    进程号：$RUN_DIR/    状态：deploy/dev_status.sh    停止：deploy/dev_stop.sh"
[ "$STATUS_OK" = 1 ]
