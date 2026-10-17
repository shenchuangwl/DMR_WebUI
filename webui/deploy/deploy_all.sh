#!/usr/bin/env bash
# DMR_WebUI 总部署入口：薄编排，不复制任一引擎内部逻辑。
set -euo pipefail
export PATH=/usr/bin:/bin
unset VIRTUAL_ENV PYTHONHOME PYTHONPATH PYTHONSTARTUP

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"
WEBUI_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
DMR_ROOT="$(cd "$WEBUI_ROOT/.." && pwd -P)"
WEBUI_ENGINE="$SCRIPT_DIR/deploy_webui.sh"
DMR_ENGINE="$DMR_ROOT/deploy""_and_run.sh"
MODE=preview
CONFIRMED=0
EXECUTE_REQUESTED=0
PREVIEW_REQUESTED=0
SCOPE=all
START=0
OPERATOR=""
REBUILD=0
TESTS=both
EVIDENCE_BASE="$(cd "$WEBUI_ROOT/../.." && pwd -P)/.dmr-webui-integrated-deploy"

die() { local code="$1"; shift; printf '错误：%s\n' "$*" >&2; exit "$code"; }
usage() {
  cat <<'HELP'
DMR_WebUI 一键部署入口（deploy_all.sh）
用法：deploy_all.sh [--preview] [--execute --yes|--confirm]
                    [--scope all|dmr|webui] [--start] [--operator NAME]
                    [--rebuild] [--tests kernel|console|both|none]
                    [--evidence-dir DIR]
preview 不调用没有 preview 参数的 DMR 根部署器；execute 顺序是 DMR → WebUI。
HELP
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --preview) PREVIEW_REQUESTED=1; MODE=preview; shift ;;
    --execute) EXECUTE_REQUESTED=1; MODE=execute; shift ;;
    --yes|--confirm) CONFIRMED=1; shift ;;
    --scope) [ "$#" -ge 2 ] || die 2 "--scope 缺少值"; SCOPE="$2"; shift 2 ;;
    --dmr-only) SCOPE=dmr; shift ;;
    --webui-only) SCOPE=webui; shift ;;
    --start) START=1; shift ;;
    --operator) [ "$#" -ge 2 ] || die 2 "--operator 缺少用户名"; OPERATOR="$2"; shift 2 ;;
    --rebuild) REBUILD=1; shift ;;
    --tests) [ "$#" -ge 2 ] || die 2 "--tests 缺少值"; TESTS="$2"; shift 2 ;;
    --evidence-dir) [ "$#" -ge 2 ] || die 2 "--evidence-dir 缺少目录"; EVIDENCE_BASE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die 2 "不认识的参数：$1（使用 --help 查看用法）" ;;
  esac
done
case "$SCOPE" in all|dmr|webui) ;; *) die 2 "--scope 必须是 all、dmr 或 webui" ;; esac
case "$TESTS" in kernel|console|both|none) ;; *) die 2 "--tests 值无效" ;; esac
[ -z "$OPERATOR" ] || [[ "$OPERATOR" =~ ^[A-Za-z0-9_.-]{1,32}$ ]] || die 2 "操作员名格式无效"
[ "$SCOPE" != dmr ] || [ -z "$OPERATOR" ] || die 2 "--operator 需要 WebUI scope"
[ "$PREVIEW_REQUESTED" = 0 ] || [ "$EXECUTE_REQUESTED" = 0 ] || die 2 "--preview 与 --execute 不能同时使用"
[ "$CONFIRMED" = 0 ] || [ "$EXECUTE_REQUESTED" = 1 ] || die 2 "--yes/--confirm 只能和 --execute 一起使用"
[ "$MODE" != execute ] || [ "$CONFIRMED" = 1 ] || die 2 "执行模式必须显式确认：加 --yes 或 --confirm"
if [ "$SCOPE" != dmr ]; then
  [ -f "$WEBUI_ENGINE" ] && [ ! -L "$WEBUI_ENGINE" ] && [ -x "$WEBUI_ENGINE" ] \
    || die 2 "WebUI 部署引擎不是可执行普通文件：$WEBUI_ENGINE"
fi
if [ "$SCOPE" != webui ]; then
  [ -f "$DMR_ENGINE" ] && [ ! -L "$DMR_ENGINE" ] && [ -x "$DMR_ENGINE" ] \
    || die 2 "DMR 部署脚本不是可执行普通文件：$DMR_ENGINE"
fi

case "$EVIDENCE_BASE" in /*) ;; *) EVIDENCE_BASE="$PWD/$EVIDENCE_BASE" ;; esac
EVIDENCE_BASE="$(readlink -m "$EVIDENCE_BASE")"
case "$EVIDENCE_BASE/" in "$WEBUI_ROOT"/*|"$DMR_ROOT"/*) die 2 "证据目录必须在项目外：$EVIDENCE_BASE" ;; esac
if [ -e "$EVIDENCE_BASE" ]; then
  [ -d "$EVIDENCE_BASE" ] && [ ! -L "$EVIDENCE_BASE" ] || die 2 "证据路径不是普通目录：$EVIDENCE_BASE"
  [ "$(stat -c %a "$EVIDENCE_BASE")" = 700 ] || die 2 "已有证据目录必须是 0700：$EVIDENCE_BASE"
else
  mkdir -p -m 700 "$EVIDENCE_BASE"
fi
RUN_DIR="$(mktemp -d "$EVIDENCE_BASE/run-XXXXXX")"; chmod 700 "$RUN_DIR"
SUMMARY="$RUN_DIR/result.json"
DMR_STATUS=not-run
WEBUI_STATUS=not-run
CURRENT_STEP=preflight
STEP_RC=0

SYSTEM_PYTHON="${PYTHON_BIN:-$(command -v python3.12 || true)}"
[ -n "$SYSTEM_PYTHON" ] || die 2 "找不到 Python 3.12"
SYSTEM_PYTHON="$(readlink -f "$SYSTEM_PYTHON")"
[ -x "$SYSTEM_PYTHON" ] || die 2 "Python 解释器不可执行：$SYSTEM_PYTHON"
"$SYSTEM_PYTHON" -I -B -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
  || die 2 "聚合入口要求 Python 3.12：$SYSTEM_PYTHON"

write_summary() {
  local final_status="$1" rc="$2"
  "$SYSTEM_PYTHON" -I -B - "$SUMMARY" "$final_status" "$rc" "$MODE" "$SCOPE" "$RUN_DIR" \
    "$DMR_STATUS" "$WEBUI_STATUS" "$CURRENT_STEP" "$START" "$REBUILD" "$TESTS" "$OPERATOR" <<'PY' || true
import json, os, sys
from datetime import datetime, timezone
out, status, rc, mode, scope, run_dir, dmr, webui, step, start, rebuild, tests, operator = sys.argv[1:]
data = {"component":"integrated", "version":1, "status":status, "returncode":int(rc),
        "mode":mode, "scope":scope, "run_dir":run_dir,
        "sequence":["dmr","webui"] if scope == "all" else [scope],
        "dmr_status":dmr, "webui_status":webui, "last_step":step,
        "start_requested":start == "1", "rebuild_requested":rebuild == "1",
        "tests":tests, "operator_requested":bool(operator),
        "operator_name":operator or None, "live_trading_enabled":False,
        "created_at_utc":datetime.now(timezone.utc).isoformat()}
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
os.chmod(tmp, 0o600); os.replace(tmp, out)
PY
}
on_exit() {
  local rc=$?
  if [ "$rc" -eq 0 ]; then [ "$MODE" = preview ] && write_summary preview 0 || write_summary success 0
  else write_summary failed "$rc"
  fi
  exit "$rc"
}
trap on_exit EXIT

run_logged() {
  local label="$1"; shift
  "$@" 2>&1 | tee "$RUN_DIR/$label.log"
  return "${PIPESTATUS[0]}"
}

webui_active() {
  local kind pid file cmd entry cwd root_text
  for kind in standin kernel console supervise; do
    file="$WEBUI_ROOT/var/run/$kind.pid"
    [ -r "$file" ] || continue
    IFS= read -r pid < "$file" || true
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
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

if [ "$MODE" = execute ] && [ "$SCOPE" != dmr ]; then
  CURRENT_STEP=runtime_gate
  if active="$(webui_active 2>/dev/null)"; then
    die 3 "检测到 WebUI 进程仍在运行（$active）；一键部署不会先改动 DMR，请先运行 dev_stop.sh"
  fi
fi

if [ "$MODE" = preview ]; then
  printf '[预览] 总部署顺序：'
  if [ "$SCOPE" = webui ]; then
    printf 'WebUI'
  elif [ "$SCOPE" = dmr ]; then
    printf 'DMR'
  else
    printf 'DMR → WebUI'
  fi
  printf '\n'
  if [ "$SCOPE" != webui ]; then
    [ -f "$DMR_ENGINE" ] || die 2 "缺少 DMR 部署脚本"
    printf '[预览] 不调用 DMR 根部署器（它没有 preview）；execute 时将调用：%s --yes%s\n' "$DMR_ENGINE" "$([ "$REBUILD" = 1 ] && printf ' --rebuild' || true)"
  fi
  if [ "$SCOPE" != dmr ]; then
    CURRENT_STEP=webui_preview
    args=(--preview --tests "$TESTS" --evidence-dir "$RUN_DIR/webui")
    [ "$START" = 1 ] && args+=(--start)
    [ -z "$OPERATOR" ] || args+=(--operator "$OPERATOR")
    run_logged webui-preview "$WEBUI_ENGINE" "${args[@]}"
    WEBUI_STATUS=preview
  fi
  printf '证据目录：%s\n' "$RUN_DIR"
  exit 0
fi

if [ "$SCOPE" != webui ]; then
  CURRENT_STEP=dmr_deploy
  dmr_args=(--yes)
  [ "$REBUILD" = 1 ] && dmr_args+=(--rebuild)
  if run_logged dmr-deploy "$DMR_ENGINE" "${dmr_args[@]}"; then
    DMR_STATUS=success
  else
    STEP_RC=$?
    DMR_STATUS=failed
    die 3 "DMR 部署失败；按顺序停止，未调用 WebUI 部署"
  fi
fi
if [ "$SCOPE" != dmr ]; then
  CURRENT_STEP=webui_deploy
  webui_args=(--execute --yes --tests "$TESTS" --evidence-dir "$RUN_DIR/webui")
  [ "$START" = 1 ] && webui_args+=(--start)
  [ "$REBUILD" = 1 ] && webui_args+=(--rebuild)
  [ -z "$OPERATOR" ] || webui_args+=(--operator "$OPERATOR")
  if run_logged webui-deploy "$WEBUI_ENGINE" "${webui_args[@]}"; then
    WEBUI_STATUS=success
  else
    STEP_RC=$?
    WEBUI_STATUS=failed
    die 3 "WebUI 部署失败；不会继续执行后续步骤"
  fi
fi
printf '一键部署完成。证据目录：%s\n' "$RUN_DIR"
exit 0

