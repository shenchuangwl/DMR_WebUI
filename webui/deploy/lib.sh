# shellcheck shell=bash
# deploy/lib.sh —— 开发脚本的公共部分，只被 source，不直接运行（dev_start.sh、dev_stop.sh、dev_status.sh、
# supervise.sh、backup_drill.sh 都从这里取配置和帮手函数）。
#
# 约定：
#  - PATH 固定为 /usr/bin:/bin；解释器只用本工程两个环境下的绝对路径（KERNEL_PY、CONSOLE_PY），
#    不用 shell 里激活的 DMR_WebUI/venv，不用裸的 python。
#  - 只绑回环；端口、var 目录、行情源由环境变量覆盖（见 deploy/README.md）。
#  - 进程号文件在 $DMR_VAR_DIR/run/，日志在 $DMR_VAR_DIR/log/。
#  - 不读取任何密钥文件，不 source 任何 .env。

export PATH=/usr/bin:/bin
unset VIRTUAL_ENV PYTHONHOME PYTHONPATH PYTHONSTARTUP
# NiceGUI 在环境里看到 PYTEST_CURRENT_TEST 就改走"屏幕测试"模式（要求 NICEGUI_SCREEN_TEST_PORT，没有就在 ui.run 里崩溃）。
# 这些脚本会从 pytest 进程里运行（e2e 测试），起来的是真服务，不是测试夹具，所以把测试模式的标记清掉。
unset PYTEST_CURRENT_TEST NICEGUI_SCREEN_TEST_PORT NICEGUI_USER_SIMULATION
export PYTHONUNBUFFERED=1

DEPLOY_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"
WEBUI_ROOT="$(dirname "$DEPLOY_DIR")"
KERNEL_PY="$WEBUI_ROOT/.venv-kernel/bin/python"
CONSOLE_PY="$WEBUI_ROOT/.venv-console/bin/python"

# ------------------------------------------------------------------ 输出

say() { printf '%s\n' "$*"; }
warn() { printf '警告：%s\n' "$*" >&2; }
die() { local code="$1"; shift; printf '错误：%s\n' "$*" >&2; exit "$code"; }

# ------------------------------------------------------------------ 时间

now_ms() {
  if [ -n "${EPOCHREALTIME:-}" ]; then
    local micros="${EPOCHREALTIME/[.,]/}"
    printf '%s\n' "$((10#$micros / 1000))"
  else
    date +%s%3N
  fi
}

# 非负十进制秒数 → 整数毫秒；格式不对返回 1
secs_to_ms() {
  [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]] || return 1
  awk -v s="$1" 'BEGIN { printf "%d\n", s * 1000 + 0.5 }'
}

# 整数毫秒 → 给 sleep 用的秒数写法（如 2300 → 2.300）
ms_to_secs() { printf '%d.%03d\n' "$(($1 / 1000))" "$(($1 % 1000))"; }

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ------------------------------------------------------------------ 配置

# 校验并规范化名为 $1 的端口变量（1–65535 的整数）
check_port() {
  local name="$1" value="${!1}"
  if ! [[ "$value" =~ ^[0-9]{1,5}$ ]] || [ "$((10#$value))" -lt 1 ] || [ "$((10#$value))" -gt 65535 ]; then
    die 2 "$name 必须是 1–65535 的整数，收到 '$value'"
  fi
  printf -v "$name" '%s' "$((10#$value))"
}

# 名为 $1 的主机变量若设置了，只允许回环（127.0.0.1 或 localhost）
check_loopback_host() {
  local name="$1" value="${!1:-}"
  case "$value" in
    ""|127.0.0.1|localhost) ;;
    *) die 2 "$name 只允许回环地址 127.0.0.1（开发脚本的健康检查也只连回环），收到 '$value'" ;;
  esac
}

# var 目录：缺省 webui/var，相对路径按 webui 根目录解析；导出 RUN_DIR、LOG_DIR、NOSUP_FILE
load_var_dir() {
  DMR_VAR_DIR="${DMR_VAR_DIR:-$WEBUI_ROOT/var}"
  case "$DMR_VAR_DIR" in /*) ;; *) DMR_VAR_DIR="$WEBUI_ROOT/$DMR_VAR_DIR" ;; esac
  while [ "${#DMR_VAR_DIR}" -gt 1 ] && [ "${DMR_VAR_DIR%/}" != "$DMR_VAR_DIR" ]; do DMR_VAR_DIR="${DMR_VAR_DIR%/}"; done
  RUN_DIR="$DMR_VAR_DIR/run"
  LOG_DIR="$DMR_VAR_DIR/log"
  NOSUP_FILE="$RUN_DIR/kernel.nosupervise"
  export DMR_VAR_DIR
}

# 读环境变量、校验、导出给三个进程用的那几个；任何一项无效就以退出码 2 结束。不创建任何目录、不启动任何东西。
load_config() {
  DMR_ENV="${DMR_ENV:-standin}"
  [ "$DMR_ENV" = standin ] || die 2 "开发脚本只用于替身环境（DMR_ENV=standin），当前 DMR_ENV=$DMR_ENV"
  unset DMR_ALLOW_LIVE
  DMR_STANDIN_PORT="${DMR_STANDIN_PORT:-18220}"
  DMR_KERNEL_PORT="${DMR_KERNEL_PORT:-18200}"
  DMR_CONSOLE_PORT="${DMR_CONSOLE_PORT:-18210}"
  check_port DMR_STANDIN_PORT
  check_port DMR_KERNEL_PORT
  check_port DMR_CONSOLE_PORT
  if [ "$DMR_STANDIN_PORT" = "$DMR_KERNEL_PORT" ] || [ "$DMR_STANDIN_PORT" = "$DMR_CONSOLE_PORT" ] \
      || [ "$DMR_KERNEL_PORT" = "$DMR_CONSOLE_PORT" ]; then
    die 2 "三个端口必须互不相同：DMR_STANDIN_PORT=$DMR_STANDIN_PORT DMR_KERNEL_PORT=$DMR_KERNEL_PORT DMR_CONSOLE_PORT=$DMR_CONSOLE_PORT"
  fi
  check_loopback_host DMR_KERNEL_HOST
  check_loopback_host DMR_CONSOLE_HOST
  DMR_STANDIN_FEED="${DMR_STANDIN_FEED:-auto}"
  case "$DMR_STANDIN_FEED" in
    auto|binance|replay) ;;
    *) die 2 "DMR_STANDIN_FEED 只能是 auto、binance 或 replay，收到 '$DMR_STANDIN_FEED'" ;;
  esac
  load_var_dir
  export DMR_ENV DMR_STANDIN_PORT DMR_KERNEL_PORT DMR_CONSOLE_PORT DMR_STANDIN_FEED
  export DMR_KERNEL_HOST=127.0.0.1 DMR_CONSOLE_HOST=127.0.0.1
  export DMR_STANDIN_URL="http://127.0.0.1:$DMR_STANDIN_PORT"
  export DMR_STANDIN_TOKEN_FILE="$DMR_VAR_DIR/standin/kernel.token"
}

# 启停脚本的等待时限（秒）；任何一项不是非负数就以退出码 2 结束
#   DMR_DEV_START_TIMEOUT_S（60）每个进程起来并通过健康检查的最长等待（替身交易所的 auto 行情最多先试 20 秒 Binance）
#   DMR_DEV_STOP_TIMEOUT_S（15）控制台、替身交易所收到 SIGTERM 后的最长等待，超时 SIGKILL
#   DMR_DEV_KERNEL_STOP_TIMEOUT_S（30）内核收到 SIGTERM 后的最长等待，超时 SIGKILL
#   DMR_DEV_SUPERVISE_EXIT_TIMEOUT_S（10）内核停掉后等守护自己退出的最长时间，超时先 SIGTERM 再 SIGKILL
load_timeouts() {
  START_TIMEOUT_S="${DMR_DEV_START_TIMEOUT_S:-60}"
  STOP_TIMEOUT_S="${DMR_DEV_STOP_TIMEOUT_S:-15}"
  KERNEL_STOP_TIMEOUT_S="${DMR_DEV_KERNEL_STOP_TIMEOUT_S:-30}"
  SUPERVISE_EXIT_TIMEOUT_S="${DMR_DEV_SUPERVISE_EXIT_TIMEOUT_S:-10}"
  secs_to_ms "$START_TIMEOUT_S" > /dev/null || die 2 "DMR_DEV_START_TIMEOUT_S 必须是非负数（秒），收到 '$START_TIMEOUT_S'"
  secs_to_ms "$STOP_TIMEOUT_S" > /dev/null || die 2 "DMR_DEV_STOP_TIMEOUT_S 必须是非负数（秒），收到 '$STOP_TIMEOUT_S'"
  secs_to_ms "$KERNEL_STOP_TIMEOUT_S" > /dev/null \
    || die 2 "DMR_DEV_KERNEL_STOP_TIMEOUT_S 必须是非负数（秒），收到 '$KERNEL_STOP_TIMEOUT_S'"
  secs_to_ms "$SUPERVISE_EXIT_TIMEOUT_S" > /dev/null \
    || die 2 "DMR_DEV_SUPERVISE_EXIT_TIMEOUT_S 必须是非负数（秒），收到 '$SUPERVISE_EXIT_TIMEOUT_S'"
}

require_interpreters() {
  local py
  for py in "$KERNEL_PY" "$CONSOLE_PY"; do
    [ -x "$py" ] || die 2 "找不到解释器 $py（本工程的虚拟环境没有建好？）"
  done
}

ensure_dirs() {
  [ -d "$DMR_VAR_DIR" ] || mkdir -p -m 700 "$DMR_VAR_DIR"
  mkdir -p "$RUN_DIR" "$LOG_DIR"
  chmod 700 "$RUN_DIR" "$LOG_DIR"
}

# 同一时刻只允许一个 dev_start/dev_stop 在改动进程；锁在脚本退出时自动释放。
# 起后台进程时必须关掉这个文件描述符（spawn_daemon 里的 9>&-），否则常驻进程会一直占着锁。
acquire_ops_lock() {
  exec 9>"$RUN_DIR/dev.lock"
  flock -w 120 9 || die 1 "另一个 dev_start.sh/dev_stop.sh 正在运行（等了 120 秒），请稍后再试"
}

# ------------------------------------------------------------------ 进程号文件与进程

pid_file() { printf '%s\n' "$RUN_DIR/$1.pid"; }

# 进程号文件里的正整数；文件不存在或内容不对时什么也不打印
read_pid() {
  local file="$RUN_DIR/$1.pid" value=""
  if [ -f "$file" ]; then
    IFS= read -r value < "$file" || true
    if [[ "$value" =~ ^[0-9]+$ ]] && [ "$((10#$value))" -gt 0 ]; then
      printf '%s\n' "$((10#$value))"
    fi
  fi
  return 0
}

# 写进程号文件（先写临时文件再改名，读的人不会读到半截）
write_pid() {
  local file="$1" tmp
  tmp="$(mktemp "$file.XXXXXX")"
  printf '%s\n' "$2" > "$tmp"
  mv -f "$tmp" "$file"
}

# 命令行里用来确认"进程号文件指的确实是它"的特征串（进程号被别的程序复用时不会误杀）
proc_pattern() {
  case "$1" in
    standin) printf 'dmr_standin\n' ;;
    kernel) printf 'dmr_kernel\n' ;;
    console) printf 'dmr_console\n' ;;
    supervise) printf 'supervise.sh\n' ;;
    *) return 1 ;;
  esac
}

# 进程还在运行（僵尸不算：它已经退出，只是还没被回收）
pid_running() {
  local pid="${1:-}" state=""
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if [ -r "/proc/$pid/status" ]; then
    state="$(awk '/^State:/ { print $2; exit }' "/proc/$pid/status" 2>/dev/null || true)"
    [ "$state" != "Z" ] || return 1
  fi
  return 0
}

# 进程还在运行，且命令行里带特征串 $2
proc_alive() {
  local pid="${1:-}" pattern="$2" cmd=""
  pid_running "$pid" || return 1
  if [ -r "/proc/$pid/cmdline" ]; then
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    [[ "$cmd" == *"$pattern"* ]] || return 1
  fi
  return 0
}

process_running() { proc_alive "$(read_pid "$1")" "$(proc_pattern "$1")"; }

# 进程还没有彻底消失：存在、不是僵尸，并且命令行含特征串，或者读不到命令行。
# 读不到命令行 = 进程正在退出：内存先被释放（/proc/<pid>/cmdline 变空，实测 512MiB 约 30 毫秒、swap 紧张时更久），
# 此时它还不是僵尸，监听的套接字也还没关。把这段时间当成"已经走了"，dev_stop 就会提前返回，
# 紧接着的 dev_start 会撞上还没释放的端口。（命令行读得到、但不含特征串，才是进程号被别的程序复用。）
proc_lingering() {
  local pid="${1:-}" pattern="$2" cmd=""
  pid_running "$pid" || return 1
  if [ -r "/proc/$pid/cmdline" ]; then
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    [ -z "$cmd" ] || [[ "$cmd" == *"$pattern"* ]] || return 1
  fi
  return 0
}

# 等进程彻底退出：pid 特征串 超时秒数；超时返回 1
wait_gone() {
  local pid="$1" pattern="$2" deadline
  deadline=$(( $(now_ms) + $(secs_to_ms "$3") ))
  while proc_lingering "$pid" "$pattern"; do
    [ "$(now_ms)" -lt "$deadline" ] || return 1
    sleep 0.1
  done
  return 0
}

# 原始标准输出文件超过 10MiB 就换成 .1（只留一份）；真正的轮转日志是各进程自己写的 *.log
rotate_out_if_big() {
  local file="$1" size=0
  [ -f "$file" ] || return 0
  size="$(stat -c %s "$file" 2>/dev/null || echo 0)"
  if [ "$size" -gt $((10 * 1024 * 1024)) ]; then mv -f "$file" "$file.1"; fi
  return 0
}

# 后台起一个脱离终端的进程：spawn_daemon 名字 输出文件 命令...
# 新会话（setsid）；标准输入接 /dev/null，标准输出和错误追加到输出文件；进程号由包装壳写进 run/<名字>.pid 后 exec 成目标命令
spawn_daemon() {
  local name="$1" out="$2"
  shift 2
  rotate_out_if_big "$out"
  setsid bash -c 'cd "$1" || exit 1; printf "%s\n" "$$" > "$2"; shift 2; exec "$@"' \
    dmr-spawn "$WEBUI_ROOT" "$RUN_DIR/$name.pid" "$@" >>"$out" 2>&1 </dev/null 9>&- &
}

# ------------------------------------------------------------------ 健康检查（只连回环）

# 打印 HTTP 状态码（连不上是 000）：http_code URL [curl 额外参数...]
http_code() {
  local url="$1"
  shift
  curl --noproxy '*' -sS -o /dev/null -m 3 -w '%{http_code}' "$@" "$url" 2>/dev/null || true
}

# 端口上有人在监听
port_in_use() { [ -n "$(ss -H -ltn "sport = :$1" 2>/dev/null || true)" ]; }

kernel_mode() {
  local mode="" file="$RUN_DIR/kernel.mode"
  if [ -f "$file" ]; then IFS= read -r mode < "$file" || true; fi
  if [ -z "$mode" ]; then
    if [ "${DMR_KERNEL_SHELL:-}" = 1 ]; then mode=shell; else mode=full; fi
  fi
  printf '%s\n' "$mode"
}

health_standin() { [ "$(http_code "http://127.0.0.1:$DMR_STANDIN_PORT/api/public/time")" = 200 ]; }

# 空壳内核没有控制接口（T0.22 的业务装配之前）：以"进程在、单实例锁里写的是它的进程号"代替 HTTP 检查
health_kernel() {
  local pid holder=""
  pid="$(read_pid kernel)"
  [ -n "$pid" ] || return 1
  if [ "$(kernel_mode)" = shell ]; then
    if [ -r "$DMR_VAR_DIR/kernel.lock" ]; then IFS= read -r holder < "$DMR_VAR_DIR/kernel.lock" || true; fi
    [ "$holder" = "$pid" ]
  else
    [ "$(http_code "http://127.0.0.1:$DMR_KERNEL_PORT/api/v1/health")" = 200 ]
  fi
}

# 控制台首页（未登录会跳转到登录页）：跟随跳转，最终要是 200
health_console() { [ "$(http_code "http://127.0.0.1:$DMR_CONSOLE_PORT/" -L --max-redirs 5)" = 200 ]; }

# 等 NAME 对应的进程起来并通过健康检查：wait_ready NAME 检查函数 超时秒数 [守护名]
# 立即返回 1（不等满超时）的情形：看到过的进程已经不在了；进程号文件被清掉或换成了别的进程号（守护在重新拉起它：
# 说明它起来之后退出过一次，启动没有成功，再等下去只是看守护重试崩溃循环）；给了守护名而守护在 5 秒后仍不在。
# 只看进程在不在，不比对命令行：进程号文件是刚刚才写的（dev_start 起进程前已删掉旧的），而内核的进程号文件是守护
# fork 之后马上写的，那一刻子进程可能还没 exec，命令行还是守护的；身份由随后的健康检查确认。
wait_ready() {
  local name="$1" check="$2" guard="${4:-}" pid seen="" started deadline
  started="$(now_ms)"
  deadline=$(( started + $(secs_to_ms "$3") ))
  while :; do
    pid="$(read_pid "$name")"
    if [ -n "$pid" ]; then
      if [ -n "$seen" ] && [ "$pid" != "$seen" ]; then return 1; fi
      seen="$pid"
      pid_running "$pid" || return 1
      if "$check"; then return 0; fi
    elif [ -n "$seen" ]; then
      return 1
    elif [ -n "$guard" ] && [ $(( $(now_ms) - started )) -gt 5000 ] && ! process_running "$guard"; then
      return 1
    fi
    [ "$(now_ms)" -lt "$deadline" ] || return 1
    sleep 0.25
  done
}

# ------------------------------------------------------------------ 状态表（dev_status.sh 和 dev_start.sh 共用）

STATUS_OK=1

report_line() { printf '%-10s pid=%-7s alive=%-3s health=%-4s %-24s %s\n' "$1" "${2:--}" "$3" "$4" "$5" "$6"; }

report_service() {
  local name="$1" label="$2" url="$3" check="$4" pid alive=no health=- note
  note="$label"
  pid="$(read_pid "$name")"
  if proc_alive "$pid" "$(proc_pattern "$name")"; then
    alive=yes
    if "$check"; then
      health=ok
    else
      health=fail
      STATUS_OK=0
      note="$label（进程在，健康检查失败）"
    fi
  else
    STATUS_OK=0
    if [ -n "$pid" ]; then note="$label：未运行（进程号文件里的 $pid 已过期）"; else note="$label：未运行"; fi
  fi
  report_line "$name" "$pid" "$alive" "$health" "$url" "$note"
}

report_kernel() {
  local pid alive=no health=- note="交易内核"
  pid="$(read_pid kernel)"
  if [ "$(kernel_mode)" = shell ]; then note="交易内核（空壳，无控制接口）"; fi
  if proc_alive "$pid" "$(proc_pattern kernel)"; then
    alive=yes
    if health_kernel; then health=ok; else health=fail; STATUS_OK=0; note="$note：进程在，健康检查失败"; fi
  else
    STATUS_OK=0
    if process_running supervise; then note="交易内核：未运行，守护正在拉起"; else note="交易内核：未运行"; fi
  fi
  report_line kernel "$pid" "$alive" "$health" "http://127.0.0.1:$DMR_KERNEL_PORT" "$note"
}

report_supervisor() {
  local pid alive=no note="内核守护（supervise.sh）"
  pid="$(read_pid supervise)"
  if process_running supervise; then alive=yes; else STATUS_OK=0; note="内核守护：未运行"; fi
  report_line supervise "$pid" "$alive" - - "$note"
}

print_status() {
  STATUS_OK=1
  report_service standin "替身交易所" "http://127.0.0.1:$DMR_STANDIN_PORT" health_standin
  report_kernel
  report_supervisor
  report_service console "Web 控制台" "http://127.0.0.1:$DMR_CONSOLE_PORT" health_console
}

# ------------------------------------------------------------------ 停止

# 停一个进程：stop_one NAME 显示名 等待秒数。先 SIGTERM，超时再 SIGKILL；进程号文件指错进程时不动它；最后清理进程号文件。
stop_one() {
  local name="$1" label="$2" timeout_s="$3" pid pattern
  pattern="$(proc_pattern "$name")"
  pid="$(read_pid "$name")"
  if [ -z "$pid" ]; then
    say "$label 没有在运行"
  elif ! proc_alive "$pid" "$pattern"; then
    say "$label 没有在运行（进程号文件里的 $pid 已过期，已清理）"
  else
    say "停止 $label（pid $pid）……"
    kill -TERM "$pid" 2>/dev/null || true
    if ! wait_gone "$pid" "$pattern" "$timeout_s"; then
      say "$label 在 ${timeout_s} 秒内没有退出，发送 SIGKILL"
      kill -KILL "$pid" 2>/dev/null || true
      if ! wait_gone "$pid" "$pattern" 10; then
        warn "$label（pid $pid）无法终止，保留进程号文件"
        return 1
      fi
    fi
    say "$label 已停止"
  fi
  rm -f "$RUN_DIR/$name.pid"
  return 0
}

# 停内核及其守护：先写停用标记（守护看到它就不再拉起），再给内核 SIGTERM，等 KERNEL_STOP_TIMEOUT_S（缺省 30）秒，
# 超时 SIGKILL；然后等守护自己退出（它看到内核没了且有标记）；全部停掉后清除停用标记。需要先 load_timeouts。
stop_kernel_group() {
  local rc=0 sup kernel_timeout="$KERNEL_STOP_TIMEOUT_S" exit_timeout="$SUPERVISE_EXIT_TIMEOUT_S"
  if ! process_running kernel && ! process_running supervise; then
    stop_one kernel "交易内核" 1 || rc=1
    rm -f "$RUN_DIR/supervise.pid" "$RUN_DIR/kernel.mode"
    return "$rc"
  fi
  (umask 077 && printf 'dev_stop %s\n' "$(utc_now)" > "$NOSUP_FILE")
  say "已写停用标记 $NOSUP_FILE（守护不再拉起内核）"
  stop_one kernel "交易内核" "$kernel_timeout" || rc=1
  sup="$(read_pid supervise)"
  if proc_alive "$sup" "$(proc_pattern supervise)"; then
    if ! wait_gone "$sup" "$(proc_pattern supervise)" "$exit_timeout"; then
      say "内核守护（pid $sup）没有自行退出，发送 SIGTERM"
      kill -TERM "$sup" 2>/dev/null || true
      if ! wait_gone "$sup" "$(proc_pattern supervise)" 35; then
        say "内核守护仍在运行，发送 SIGKILL"
        kill -KILL "$sup" 2>/dev/null || true
        wait_gone "$sup" "$(proc_pattern supervise)" 10 || { warn "内核守护（pid $sup）无法终止"; rc=1; }
      fi
    fi
  fi
  if [ "$rc" = 0 ]; then
    rm -f "$RUN_DIR/supervise.pid" "$RUN_DIR/kernel.mode" "$NOSUP_FILE"
  else
    warn "内核或守护没有完全停掉，保留停用标记 $NOSUP_FILE 和进程号文件"
  fi
  return "$rc"
}
