#!/usr/bin/env bash
# WebUI 部署引擎：默认预览；执行须 --execute 与 --yes/--confirm。
set -euo pipefail
export PATH=/usr/bin:/bin
umask 077
unset VIRTUAL_ENV PYTHONHOME PYTHONPATH PYTHONSTARTUP
export DMR_ENV=standin
unset DMR_ALLOW_LIVE

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"
WEBUI_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
DMR_ROOT="$(cd "$WEBUI_ROOT/.." && pwd -P)"
KERNEL_ENV="$WEBUI_ROOT/.venv-kernel"
CONSOLE_ENV="$WEBUI_ROOT/.venv-console"
KERNEL_PY="$KERNEL_ENV/bin/python"
CONSOLE_PY="$CONSOLE_ENV/bin/python"
EVIDENCE_DIR="$(cd "$WEBUI_ROOT/../.." && pwd -P)/.dmr-webui-deploy"
MODE=preview
CONFIRMED=0
EXECUTE_REQUESTED=0
PREVIEW_REQUESTED=0
REBUILD=0
START=0
OPERATOR=""
TESTS=both
RESULT_READY=0
CURRENT_STEP=preflight
SYSTEM_PYTHON=""
STACK_STARTED=0

say() { printf '%s\n' "$*"; }
die() { local code="$1"; shift; printf '错误：%s\n' "$*" >&2; exit "$code"; }

usage() {
  cat <<'HELP'
WebUI 部署引擎（webui/deploy/deploy_webui.sh）
用法：deploy_webui.sh [--preview] [--execute --yes|--confirm]
                  [--rebuild] [--start] [--operator NAME]
                  [--tests kernel|console|both|none] [--evidence-dir DIR]
默认只预览；执行会严格安装 kernel-dev.lock/console-dev.lock，运行禁网测试，
并做纯净快照。--start 只启动替身环境回环进程，随后确认“已停止开仓”。
HELP
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --preview) PREVIEW_REQUESTED=1; MODE=preview; shift ;;
    --execute) EXECUTE_REQUESTED=1; MODE=execute; shift ;;
    --yes|--confirm) CONFIRMED=1; shift ;;
    --rebuild) REBUILD=1; shift ;;
    --start) START=1; shift ;;
    --operator) [ "$#" -ge 2 ] || die 2 "--operator 缺少用户名"; OPERATOR="$2"; shift 2 ;;
    --tests) [ "$#" -ge 2 ] || die 2 "--tests 缺少值"; TESTS="$2"; shift 2 ;;
    --evidence-dir) [ "$#" -ge 2 ] || die 2 "--evidence-dir 缺少目录"; EVIDENCE_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die 2 "不认识的参数：$1（使用 --help 查看用法）" ;;
  esac
done
case "$TESTS" in kernel|console|both|none) ;; *) die 2 "--tests 必须是 kernel、console、both 或 none" ;; esac
[ -z "$OPERATOR" ] || [[ "$OPERATOR" =~ ^[A-Za-z0-9_.-]{1,32}$ ]] || die 2 "操作员名格式无效"
[ "$PREVIEW_REQUESTED" = 0 ] || [ "$EXECUTE_REQUESTED" = 0 ] || die 2 "--preview 与 --execute 不能同时使用"
[ "$CONFIRMED" = 0 ] || [ "$EXECUTE_REQUESTED" = 1 ] || die 2 "--yes/--confirm 只能和 --execute 一起使用"
[ "$MODE" != execute ] || [ "$CONFIRMED" = 1 ] || die 2 "执行模式必须显式确认：加 --yes 或 --confirm"

case "$EVIDENCE_DIR" in /*) ;; *) EVIDENCE_DIR="$PWD/$EVIDENCE_DIR" ;; esac
EVIDENCE_DIR="$(readlink -m "$EVIDENCE_DIR")"
case "$EVIDENCE_DIR/" in "$WEBUI_ROOT"/*|"$DMR_ROOT"/*) die 2 "证据目录必须在项目外：$EVIDENCE_DIR" ;; esac
if [ -e "$EVIDENCE_DIR" ]; then
  [ -d "$EVIDENCE_DIR" ] && [ ! -L "$EVIDENCE_DIR" ] || die 2 "证据路径不是普通目录：$EVIDENCE_DIR"
  [ "$(stat -c %a "$EVIDENCE_DIR")" = 700 ] || die 2 "已有证据目录必须是 0700：$EVIDENCE_DIR"
else
  mkdir -p -m 700 "$EVIDENCE_DIR"
fi
OPERATION_LOCK="$(readlink -m "$WEBUI_ROOT/../../.dmr-webui-operation.lock")"
if [ "$MODE" = execute ]; then
  [ ! -L "$OPERATION_LOCK" ] || die 3 "项目操作锁不能是符号链接：$OPERATION_LOCK"
  if [ -e "$OPERATION_LOCK" ]; then
    [ -f "$OPERATION_LOCK" ] || die 3 "项目操作锁不是普通文件：$OPERATION_LOCK"
  else
    : >> "$OPERATION_LOCK"
  fi
  chmod 600 "$OPERATION_LOCK"
  exec 9>>"$OPERATION_LOCK"
  flock -n 9 || die 3 "已有部署/清理操作在运行，拒绝并发执行"
fi
RESULT_FILE="$EVIDENCE_DIR/result.json"
RESULT_READY=1

write_result() {
  local final_status="$1" rc="$2"
  [ "$RESULT_READY" = 1 ] || return 0
  "$SYSTEM_PYTHON" -I -B - "$RESULT_FILE" "$final_status" "$rc" "$MODE" "$CURRENT_STEP" \
    "$WEBUI_ROOT" "$KERNEL_ENV" "$CONSOLE_ENV" "$TESTS" "$START" "$OPERATOR" "$REBUILD" <<'PY' || true
import json, os, sys
from datetime import datetime, timezone
out, status, rc, mode, step, root, kernel, console, tests, start, operator, rebuild = sys.argv[1:]
data = {"component":"webui", "version":1, "status":status, "returncode":int(rc),
        "mode":mode, "last_step":step, "webui_root":root,
        "environments":{"kernel":kernel, "console":console}, "tests":tests,
        "start_requested":start == "1", "operator_requested":bool(operator),
        "operator_name":operator or None, "rebuild_requested":rebuild == "1",
        "live_trading_enabled":False, "created_at_utc":datetime.now(timezone.utc).isoformat()}
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
os.chmod(tmp, 0o600); os.replace(tmp, out)
PY
}

on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ "$STACK_STARTED" = 1 ]; then
    # --start is owned by this invocation; do not leave a partially verified
    # stack running after a gate, operator, or purity failure.
    "$SCRIPT_DIR/dev_stop.sh" >/dev/null 2>&1 || true
    printf '部署失败后已尝试停止本次启动的 WebUI 栈。\n' >&2
  fi
  if [ "$rc" -eq 0 ]; then
    [ "$MODE" = preview ] && write_result preview 0 || write_result success 0
  else
    write_result failed "$rc"
  fi
  exit "$rc"
}
trap on_exit EXIT

SYSTEM_PYTHON="${PYTHON_BIN:-$(command -v python3.12 || true)}"
[ -n "$SYSTEM_PYTHON" ] || die 2 "找不到 Python 3.12（可用 PYTHON_BIN 指定绝对路径）"
SYSTEM_PYTHON="$(readlink -f "$SYSTEM_PYTHON")"
[ -x "$SYSTEM_PYTHON" ] || die 2 "Python 解释器不可执行：$SYSTEM_PYTHON"
"$SYSTEM_PYTHON" -I -B -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
  || die 2 "部署器要求 Python 3.12：$SYSTEM_PYTHON"
if [ -x /sbin/ldconfig ]; then
  seccomp_listing="$(/sbin/ldconfig -p 2>/dev/null || true)"
  grep -q 'libseccomp\.so\.2' <<<"$seccomp_listing" || die 2 "找不到 libseccomp.so.2"
else
  found_seccomp=0
  for f in /lib*/libseccomp.so.2 /usr/lib*/libseccomp.so.2; do [ -e "$f" ] && found_seccomp=1; done
  [ "$found_seccomp" = 1 ] || die 2 "找不到 libseccomp.so.2"
fi
[ -f "$WEBUI_ROOT/tests/guard/no_network_ip.py" ] || die 2 "缺少禁网测试组件"

LOCKS=("$WEBUI_ROOT/requirements/kernel.lock" "$WEBUI_ROOT/requirements/kernel-dev.lock"
       "$WEBUI_ROOT/requirements/console.lock" "$WEBUI_ROOT/requirements/console-dev.lock")
CURRENT_STEP=lock_validation
"$SYSTEM_PYTHON" -I -B - "${LOCKS[@]}" <<'PY'
import re, sys
from pathlib import Path
for raw in sys.argv[1:]:
    path = Path(raw)
    if not path.is_file() or path.is_symlink(): raise SystemExit(f"锁文件缺失或为符号链接: {path}")
    current = None; hashed = False; count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==[^ ]+", line)
        if match:
            if current is not None and not hashed: raise SystemExit(f"锁条目无 hash: {path}: {current}")
            current, hashed, count = match.group(1), False, count + 1
        if current is not None and re.search(r"--hash=sha256:[0-9a-f]{64}", line): hashed = True
    if current is not None and not hashed: raise SystemExit(f"锁条目无 hash: {path}: {current}")
    if count == 0: raise SystemExit(f"锁文件没有条目: {path}")
    print(f"锁文件有效: {path} ({count} 个条目)")
PY

venv_healthy() {
  local env="$1" py="$2"
  [ -d "$env" ] && [ ! -L "$env" ] && [ -f "$env/pyvenv.cfg" ] && [ -x "$py" ] || return 1
  "$py" -I -B - "$env" <<'PY'
import sys
from pathlib import Path
env = Path(sys.argv[1]).resolve()
raise SystemExit(0 if sys.version_info[:2] == (3, 12) and Path(sys.prefix).resolve() == env else 1)
PY
}

runtime_active() {
  local kind pid file cmd entry cwd marker root_text
  for kind in standin kernel console supervise; do
    file="$WEBUI_ROOT/var/run/$kind.pid"; [ -r "$file" ] || continue
    IFS= read -r pid < "$file" || true; [[ "$pid" =~ ^[0-9]+$ ]] || continue
    [ -r "/proc/$pid/cmdline" ] || continue
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$kind:$cmd" in
      standin:*dmr_standin*|kernel:*dmr_kernel*|console:*dmr_console*|supervise:*supervise.sh*)
        printf '%s(pid %s)' "$kind" "$pid"; return 0 ;;
    esac
  done
  if command -v ps >/dev/null 2>&1; then
    while read -r pid cmd; do
      [[ "$pid" =~ ^[0-9]+$ ]] || continue
      case "$cmd" in
        *"$WEBUI_ROOT"*dmr_standin*|*"$WEBUI_ROOT"*dmr_kernel*|*"$WEBUI_ROOT"*dmr_console*|*"$WEBUI_ROOT"*supervise.sh*)
          printf 'ps-scan(pid %s)' "$pid"; return 0 ;;
      esac
    done < <(ps -eo pid=,args= 2>/dev/null || true)
  fi
  # PID files are evidence, not the only source of truth.  A stale/missing
  # pid file must not allow a live WebUI process through the deployment gate.
  root_text="$WEBUI_ROOT"
  for entry in /proc/[0-9]*; do
    [ -r "$entry/cmdline" ] || continue
    pid="${entry##*/}"
    [ "$pid" = "$$" ] && continue
    cmd="$(tr '\0' ' ' < "$entry/cmdline" 2>/dev/null || true)"
    case "$cmd" in
      *dmr_standin*|*dmr_kernel*|*dmr_console*|*supervise.sh*) ;;
      *) continue ;;
    esac
    cwd="$(readlink -f "$entry/cwd" 2>/dev/null || true)"
    case "$cmd $cwd" in
      *"$root_text"*) printf 'proc-scan(pid %s)' "$pid"; return 0 ;;
    esac
  done
  return 1
}

snapshot() {
  local out="$1"
  "$SYSTEM_PYTHON" -I -B - "$WEBUI_ROOT" "$out" <<'PY'
import hashlib, json, os, stat, sys
from pathlib import Path
root, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
excluded = {".git", ".venv-kernel", ".venv-console", "var", "backup", "__pycache__", ".pytest_cache", ".nicegui"}
items = {}
def sensitive_name(name):
    lower = name.lower()
    return (lower == ".env" or lower.startswith(".env.") or
            lower.endswith((".token", ".password", ".secret")))
for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
    current_path = Path(current)
    dirs[:] = sorted(d for d in dirs if d not in excluded and not (current_path / d).is_symlink())
    for name in sorted(files):
        path = current_path / name
        rel = path.relative_to(root).as_posix()
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            items[rel] = {"type":"symlink", "mode":stat.S_IMODE(st.st_mode), "target":os.readlink(path)}
            continue
        if not stat.S_ISREG(st.st_mode):
            items[rel] = {"type":"special", "mode":stat.S_IMODE(st.st_mode)}
            continue
        if sensitive_name(name):
            # Never read credential-like files merely to produce deployment
            # evidence.  Metadata still detects ordinary replacement/changes.
            items[rel] = {"type":"file", "sensitive":True,
                          "mode":stat.S_IMODE(st.st_mode), "size":st.st_size,
                          "mtime_ns":st.st_mtime_ns}
            continue
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        items[rel] = {"type":"file", "mode":stat.S_IMODE(st.st_mode), "size":st.st_size,
                      "mtime_ns":st.st_mtime_ns, "sha256":digest.hexdigest()}
data = {"root":str(root), "excluded":sorted(excluded), "files":items}
tmp = out.with_suffix(out.suffix + ".tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, out)
PY
}

compare_snapshots() {
  "$SYSTEM_PYTHON" -I -B - "$1" "$2" <<'PY'
import json
import sys
before = json.load(open(sys.argv[1], encoding="utf-8"))["files"]
after = json.load(open(sys.argv[2], encoding="utf-8"))["files"]
changed = sorted(set(before) ^ set(after))
changed += sorted(k for k in set(before) & set(after) if before[k] != after[k])
if changed:
    print("纯净快照变化（运行期排除项之外）:", file=sys.stderr)
    for item in changed[:80]:
        print("  " + item, file=sys.stderr)
    raise SystemExit(1)
print("纯净快照一致（运行期目录已排除）")
PY
}

if [ "$MODE" = preview ]; then
  say "[预览] WebUI 部署计划（不创建环境、不安装、不启动）："
  say "  WebUI：$WEBUI_ROOT"
  say "  Python：$SYSTEM_PYTHON（3.12；libseccomp.so.2 已检查）"
  say "  锁：kernel-dev.lock、console-dev.lock（runtime lock 同时校验）"
  say "  环境：$KERNEL_ENV；$CONSOLE_ENV"
  say "  测试：$TESTS；启动：$([ "$START" = 1 ] && printf '是' || printf '否')；重建：$([ "$REBUILD" = 1 ] && printf '是' || printf '否')"
  [ -z "$OPERATOR" ] || say "  操作员：$OPERATOR（只在显式执行且内核启动后创建）"
  say "  证据：$RESULT_FILE"
  exit 0
fi

CURRENT_STEP=runtime_gate
if active="$(runtime_active 2>/dev/null)"; then
  die 3 "检测到 WebUI 进程仍在运行（$active）；请先运行 dev_stop.sh"
fi

CURRENT_STEP=purity_before
SNAP_BEFORE="$EVIDENCE_DIR/purity-before.json"
SNAP_AFTER="$EVIDENCE_DIR/purity-after.json"
snapshot "$SNAP_BEFORE"

ensure_env() {
  local env="$1" py="$2" lock="$3" label="$4" backup
  CURRENT_STEP="environment_$label"
  if [ -e "$env" ] && [ ! -d "$env" ]; then
    die 3 "$label 环境不是目录：$env"
  fi
  [ ! -L "$env" ] || die 3 "$label 环境是符号链接，拒绝跟随：$env"
  if [ -e "$env" ] && ! venv_healthy "$env" "$py"; then
    [ "$REBUILD" = 1 ] || die 3 "$label 环境不健康；如确需重建请显式加 --rebuild"
    backup_dir="$(mktemp -d "$EVIDENCE_DIR/$(basename "$env").pre-rebuild.XXXXXX")"
    backup="$backup_dir/$(basename "$env")"
    mv -- "$env" "$backup"
    say "$label 旧环境已可恢复地移到：$backup"
  fi
  [ -e "$env" ] || "$SYSTEM_PYTHON" -I -B -m venv "$env"
  venv_healthy "$env" "$py" || die 3 "$label 环境创建后健康检查失败"
  CURRENT_STEP="lock_install_$label"
  PIP_CONFIG_FILE=/dev/null PIP_NO_CACHE_DIR=1 "$py" -I -B -m "p""ip" install --disable-pip-version-check --no-cache-dir \
    --require-hashes --no-deps --dry-run -r "$lock"
  PIP_CONFIG_FILE=/dev/null PIP_NO_CACHE_DIR=1 "$py" -I -B -m "p""ip" install --disable-pip-version-check --no-cache-dir \
    --require-hashes --no-deps --no-compile -r "$lock"
  "$py" -I -B -m "p""ip" check
}

ensure_env "$KERNEL_ENV" "$KERNEL_PY" "$WEBUI_ROOT/requirements/kernel-dev.lock" kernel
ensure_env "$CONSOLE_ENV" "$CONSOLE_PY" "$WEBUI_ROOT/requirements/console-dev.lock" console

run_suite() {
  local suite="$1" cache
  [ "$TESTS" = none ] && return 0
  [ "$TESTS" = both ] || [ "$TESTS" = "$suite" ] || return 0
  CURRENT_STEP="tests_$suite"
  cache="$(mktemp -d "${TMPDIR:-/tmp}/dmr-webui-$suite.XXXXXX")"
  chmod 700 "$cache"
  MPLCONFIGDIR="$cache" XDG_CACHE_HOME="$cache" PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -p no:cacheprovider" "$WEBUI_ROOT/run_tests.sh" "$suite"
  rmdir -- "$cache" 2>/dev/null || true
}
run_suite kernel
run_suite console

if [ "$START" = 1 ]; then
  CURRENT_STEP=start_stack
  unset DMR_KERNEL_SHELL
  STACK_STARTED=1
  "$SCRIPT_DIR/dev_start.sh"
  CURRENT_STEP=verify_gate
  "$KERNEL_PY" -I -B -m dmr_kernel.cli stop >/dev/null 2>&1 || die 3 "无法确认停止开仓"
  status_file="$EVIDENCE_DIR/kernel-status.txt"
  "$KERNEL_PY" -I -B -m dmr_kernel.cli status > "$status_file" 2>&1 || die 3 "内核状态查询失败"
  chmod 600 "$status_file"
  grep -Fq '开仓闸：已停止开仓' "$status_file" || die 3 "内核启动后未确认“已停止开仓”"
  say "内核已启动且开仓闸确认：已停止开仓"
fi

if [ -n "$OPERATOR" ]; then
  [ "$START" = 1 ] || die 2 "--operator 需要同时使用 --start"
  CURRENT_STEP=create_operator
  operator_file="$WEBUI_ROOT/var/operators/$OPERATOR.password"
  if [ -e "$operator_file" ]; then
    [ -f "$operator_file" ] && [ ! -L "$operator_file" ] || die 3 "操作员口令路径不是普通文件"
    [ "$(stat -c %a "$operator_file")" = 600 ] || die 3 "操作员口令文件权限不是 0600"
    say "操作员已存在，未覆盖口令文件：$operator_file"
  else
    "$KERNEL_PY" -I -B -m dmr_kernel.cli create-operator --username "$OPERATOR" >/dev/null 2>&1 \
      || die 3 "操作员创建失败；未回显口令，请检查内核日志和本机 token"
    [ -f "$operator_file" ] && [ "$(stat -c %a "$operator_file")" = 600 ] || die 3 "口令文件权限校验失败"
    say "已创建操作员；口令仅写入 0600 文件：$operator_file"
  fi
fi

CURRENT_STEP=purity_after
snapshot "$SNAP_AFTER"
compare_snapshots "$SNAP_BEFORE" "$SNAP_AFTER"
say "WebUI 部署引擎完成。证据：$EVIDENCE_DIR"
exit 0

