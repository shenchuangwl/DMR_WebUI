#!/usr/bin/env bash
# deploy/dev_stop.sh —— 开发用：按 Web 控制台 → 交易内核 → 替身交易所 的顺序停掉三个进程，清理进程号文件。
#
# 内核的停法：先写停用标记 run/kernel.nosupervise（守护看到它就不再拉起），再给内核 SIGTERM，最多等 30 秒，超时 SIGKILL；
# 然后等守护自己退出，全部停掉后清除标记。进程号文件指向的进程命令行对不上（进程号被别的程序复用）时不会去杀它。
# 没有东西在运行时也成功（幂等）。
#
# 环境变量：DMR_VAR_DIR 以及端口变量与 dev_start.sh 相同；
#   DMR_DEV_STOP_TIMEOUT_S=15              控制台、替身交易所收到 SIGTERM 后的最长等待，超时 SIGKILL
#   DMR_DEV_KERNEL_STOP_TIMEOUT_S=30       内核收到 SIGTERM 后的最长等待，超时 SIGKILL
#   DMR_DEV_SUPERVISE_EXIT_TIMEOUT_S=10    内核停掉后等守护自行退出的最长时间
#
# 退出码：0 全部停掉；1 有进程无法终止（保留其进程号文件和停用标记）；2 配置无效。
set -euo pipefail
export PATH=/usr/bin:/bin
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib.sh"

case "${1:-}" in
  "") ;;
  -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  *) die 2 "不认识的参数：$1（用法见 dev_stop.sh --help）" ;;
esac

load_config
load_timeouts
if [ ! -d "$RUN_DIR" ]; then
  say "没有运行记录（$RUN_DIR 不存在），无需停止"
  exit 0
fi
acquire_ops_lock

rc=0
stop_one console "Web 控制台" "$STOP_TIMEOUT_S" || rc=1
stop_kernel_group || rc=1
stop_one standin "替身交易所" "$STOP_TIMEOUT_S" || rc=1

if [ "$rc" = 0 ]; then say "已全部停止"; else warn "有进程没有停掉，见上面的提示"; fi
exit "$rc"
