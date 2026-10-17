#!/usr/bin/env bash
# deploy/supervise.sh —— 内核守护：循环运行内核，内核退出后（包括被 kill -9）若没有停用标记就拉起。
#
# 用法：
#   supervise.sh                 运行 <工程根>/.venv-kernel/bin/python -m dmr_kernel
#   supervise.sh -- 命令 参数…    守护别的命令（测试用；命令用绝对路径，工作目录是工程根）
#
# 规则：
#  - 停用标记 $DMR_VAR_DIR/run/kernel.nosupervise 存在时不拉起、守护自己退出（退出码 0）。dev_stop.sh 和命令行应急入口（M2）
#    靠它阻止拉起；检查发生在每次启动之前和内核退出之后，所以退避等待期间写的标记也有效。守护不创建、不删除这个标记。
#  - 内核退出后隔 DMR_SUPERVISE_DELAY_S（缺省 2）秒再拉起。内核运行不足 DMR_SUPERVISE_FAST_S（缺省 10）秒就退出算一次
#    "快速失败"，连续快速失败时间隔翻倍，封顶 DMR_SUPERVISE_MAX_BACKOFF_S（缺省 60）秒；运行满这么久后退避清零。
#  - 记进程号：守护自己 -> run/supervise.pid，内核 -> run/kernel.pid（每次拉起都更新，退出时清理）。
#  - 收到 SIGTERM/SIGINT/SIGHUP：转发给内核，等 DMR_SUPERVISE_STOP_GRACE_S（缺省 30）秒，超时 SIGKILL，然后不再拉起，退出 0。
#  - 内核的标准输出和错误追加到 log/kernel.out（超过 10MiB 在下次拉起前换成 .1）；守护自己的事件打印到标准输出
#    （dev_start.sh 把它接到 log/supervise.log）。
#  - 同一时刻只允许一个守护（run/supervise.lock）。
set -euo pipefail
export PATH=/usr/bin:/bin
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib.sh"

case "${1:-}" in
  -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  --) shift ;;
  -*) die 2 "未知选项：$1（用法见 supervise.sh --help）" ;;
esac
if [ "$#" -gt 0 ]; then COMMAND=("$@"); else COMMAND=("$KERNEL_PY" -m dmr_kernel); fi

load_config
ensure_dirs
cd "$WEBUI_ROOT"

DELAY_MS="$(secs_to_ms "${DMR_SUPERVISE_DELAY_S:-2}")" || die 2 "DMR_SUPERVISE_DELAY_S 必须是非负数（秒）"
FAST_MS="$(secs_to_ms "${DMR_SUPERVISE_FAST_S:-10}")" || die 2 "DMR_SUPERVISE_FAST_S 必须是非负数（秒）"
MAX_MS="$(secs_to_ms "${DMR_SUPERVISE_MAX_BACKOFF_S:-60}")" || die 2 "DMR_SUPERVISE_MAX_BACKOFF_S 必须是非负数（秒）"
GRACE_MS="$(secs_to_ms "${DMR_SUPERVISE_STOP_GRACE_S:-30}")" || die 2 "DMR_SUPERVISE_STOP_GRACE_S 必须是非负数（秒）"
if [ "$MAX_MS" -lt "$DELAY_MS" ]; then MAX_MS="$DELAY_MS"; fi

KERNEL_PID_FILE="$RUN_DIR/kernel.pid"
SUPERVISE_PID_FILE="$RUN_DIR/supervise.pid"
KERNEL_OUT="$LOG_DIR/kernel.out"

slog() { printf '%s %s\n' "$(utc_now)" "$*"; }

# 只清理属于自己的进程号文件（文件里是别的进程号就不动）
remove_pid_if() {
  local file="$1" expected="$2" value=""
  if [ -f "$file" ]; then IFS= read -r value < "$file" || true; fi
  if [ -n "$expected" ] && [ "$value" = "$expected" ]; then rm -f "$file"; fi
  return 0
}

child=0
sleeper=0
stopping=0
term_at=0

on_signal() {
  stopping=1
  if [ "$sleeper" -gt 0 ]; then kill "$sleeper" 2>/dev/null || true; fi
  if [ "$child" -gt 0 ] && pid_running "$child"; then
    term_at="$(now_ms)"
    kill -TERM "$child" 2>/dev/null || true
  fi
}

cleanup() {
  if [ "$child" -gt 0 ]; then remove_pid_if "$KERNEL_PID_FILE" "$child"; fi
  remove_pid_if "$SUPERVISE_PID_FILE" "$$"
  return 0
}

trap on_signal TERM INT HUP
trap cleanup EXIT

if [ -e "$NOSUP_FILE" ]; then
  slog "停用标记 $NOSUP_FILE 存在，不启动内核，守护退出"
  exit 0
fi

exec 8>"$RUN_DIR/supervise.lock"
flock -n 8 || die 1 "已经有一个守护在运行（见 $SUPERVISE_PID_FILE）"
write_pid "$SUPERVISE_PID_FILE" "$$"
slog "守护启动（pid $$）：${COMMAND[*]}；重启间隔 ${DELAY_MS}ms，快速失败门限 ${FAST_MS}ms，退避上限 ${MAX_MS}ms"

fails=0
while :; do
  if [ "$stopping" = 1 ]; then break; fi
  if [ -e "$NOSUP_FILE" ]; then
    slog "停用标记存在，不再拉起，守护退出"
    break
  fi

  rotate_out_if_big "$KERNEL_OUT"
  started_ms="$(now_ms)"
  "${COMMAND[@]}" >>"$KERNEL_OUT" 2>&1 </dev/null 8>&- &
  child=$!
  write_pid "$KERNEL_PID_FILE" "$child"
  slog "已启动：pid $child"
  if [ "$stopping" = 1 ]; then
    term_at="$(now_ms)"
    kill -TERM "$child" 2>/dev/null || true
  fi

  status=0
  while :; do
    wait "$child" && status=0 || status=$?
    pid_running "$child" || break
    # wait 被信号打断而内核还在：正在停止时给它宽限期，超时强杀；否则继续等
    if [ "$stopping" = 1 ]; then
      deadline=$((term_at + GRACE_MS))
      while pid_running "$child" && [ "$(now_ms)" -lt "$deadline" ]; do sleep 0.1; done
      if pid_running "$child"; then
        slog "pid $child 在 ${GRACE_MS}ms 内没有退出，发送 SIGKILL"
        kill -KILL "$child" 2>/dev/null || true
      fi
    fi
  done
  ended_ms="$(now_ms)"
  runtime_ms=$((ended_ms - started_ms))
  remove_pid_if "$KERNEL_PID_FILE" "$child"
  slog "内核已退出：pid $child，状态 $status，运行了 ${runtime_ms}ms"
  child=0

  if [ "$stopping" = 1 ]; then break; fi
  if [ -e "$NOSUP_FILE" ]; then
    slog "停用标记存在，不再拉起，守护退出"
    break
  fi

  if [ "$runtime_ms" -lt "$FAST_MS" ]; then fails=$((fails + 1)); else fails=0; fi
  exponent=$((fails > 1 ? fails - 1 : 0))
  if [ "$exponent" -gt 20 ]; then exponent=20; fi
  delay_ms=$((DELAY_MS * (1 << exponent)))
  if [ "$delay_ms" -gt "$MAX_MS" ]; then delay_ms="$MAX_MS"; fi
  slog "${delay_ms}ms 后拉起（连续快速失败 $fails 次）"
  sleep "$(ms_to_secs "$delay_ms")" &
  sleeper=$!
  wait "$sleeper" || true
  sleeper=0
done

slog "守护退出"
exit 0
