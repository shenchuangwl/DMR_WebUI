#!/usr/bin/env bash
# deploy/backup_drill.sh —— 备份恢复演练：取备份目录里最新的账本在线备份，用 dmr_kernel.ledger.backup.restore_backup 恢复到
# 临时目录，做一致性检查（PRAGMA integrity_check、外键检查、每张表的行数、与备份文件逐字节比对），打印结果，删除临时目录。
# 不碰正在使用的账本：既不打开它，也不在它旁边建任何文件。
#
# 用法：backup_drill.sh [--backup-dir 目录] [--file 备份文件] [--keep]
#   --backup-dir  备份目录（缺省 $DMR_BACKUP_DIR，再缺省 <工程根>/backup）
#   --file        指定某个备份文件而不是取最新的（不能是正在使用的账本）
#   --keep        演练后保留恢复出来的库和临时目录（排查用）
# 环境变量：DMR_BACKUP_DIR、DMR_DB_PATH（正在使用的账本路径，缺省 <var>/kernel/ledger.sqlite，只用来拒绝把它当成备份）、
#           DMR_VAR_DIR、TMPDIR（临时目录所在位置）。
#
# 退出码：0 通过；1 恢复或检查失败；2 用法错误、没有备份、或指定的文件是正在使用的账本。
set -euo pipefail
export PATH=/usr/bin:/bin
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib.sh"

case "${1:-}" in
  -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
esac

load_var_dir
[ -x "$KERNEL_PY" ] || die 2 "找不到解释器 $KERNEL_PY（本工程的虚拟环境没有建好？）"
exec "$KERNEL_PY" -B "$DEPLOY_DIR/backup_drill.py" "$@"
