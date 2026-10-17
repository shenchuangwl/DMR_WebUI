#!/usr/bin/env bash
# deploy/dev_status.sh —— 开发用：打印每个进程的进程号、是否存活、健康检查结果。
#
#   standin    进程号 / 存活 / 健康：GET /api/public/time 返回 200
#   kernel     进程号 / 存活 / 健康：GET /api/v1/health 返回 200（空壳内核没有控制接口：以单实例锁里写的是它的进程号判断）
#   supervise  内核守护的进程号 / 存活
#   console    进程号 / 存活 / 健康：首页（未登录会跳到登录页）最终返回 200
#
# 每行形如：  standin    pid=1234    alive=yes health=ok   http://127.0.0.1:18220    替身交易所
# 退出码：0 全部存活且健康；1 有进程不在或不健康；2 配置无效。只读：不创建目录、不改动任何文件。
set -euo pipefail
export PATH=/usr/bin:/bin
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib.sh"

case "${1:-}" in
  "") ;;
  -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  *) die 2 "不认识的参数：$1（用法见 dev_status.sh --help）" ;;
esac

load_config
say "DMR 开发环境状态  var: $DMR_VAR_DIR"
print_status
[ "$STATUS_OK" = 1 ]
