#!/usr/bin/env bash
# DMR_WebUI 总清理入口：WebUI 停止 → WebUI 清理 → DMR 根清理。
set -euo pipefail
export PATH=/usr/bin:/bin
unset VIRTUAL_ENV PYTHONHOME PYTHONPATH PYTHONSTARTUP

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"
WEBUI_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
DMR_ROOT="$(cd "$WEBUI_ROOT/.." && pwd -P)"
WEBUI_ENGINE="$SCRIPT_DIR/cleanup_webui.sh"
WEBUI_STOP="$SCRIPT_DIR/dev_stop.sh"
DMR_ENGINE="$DMR_ROOT/cleanup""_deployment_files.sh"
MODE=preview
CONFIRMED=0
EXECUTE_REQUESTED=0
PREVIEW_REQUESTED=0
SCOPE=all
RETENTION_DAYS=30
KEEP_BACKUPS=2
EVIDENCE_BASE="$(cd "$WEBUI_ROOT/../.." && pwd -P)/.dmr-webui-integrated-cleanup"

die() { local code="$1"; shift; printf '错误：%s\n' "$*" >&2; exit "$code"; }
usage() {
  cat <<'HELP'
DMR_WebUI 一键清理入口（cleanup_all.sh）
用法：cleanup_all.sh [--preview] [--execute --yes|--confirm]
                    [--scope all|dmr|webui] [--retention-days N]
                    [--keep-backups N] [--evidence-dir DIR]
preview 不停止服务、不移动文件；execute 顺序是停止 WebUI → WebUI 清理 → DMR 清理。
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
    --retention-days) [ "$#" -ge 2 ] || die 2 "--retention-days 缺少值"; RETENTION_DAYS="$2"; shift 2 ;;
    --keep-backups) [ "$#" -ge 2 ] || die 2 "--keep-backups 缺少值"; KEEP_BACKUPS="$2"; shift 2 ;;
    --evidence-dir) [ "$#" -ge 2 ] || die 2 "--evidence-dir 缺少目录"; EVIDENCE_BASE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die 2 "不认识的参数：$1（使用 --help 查看用法）" ;;
  esac
done
case "$SCOPE" in all|dmr|webui) ;; *) die 2 "--scope 必须是 all、dmr 或 webui" ;; esac
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || die 2 "--retention-days 必须是非负整数"
[[ "$KEEP_BACKUPS" =~ ^[1-9][0-9]*$ ]] || die 2 "--keep-backups 必须是正整数"
[ "$PREVIEW_REQUESTED" = 0 ] || [ "$EXECUTE_REQUESTED" = 0 ] || die 2 "--preview 与 --execute 不能同时使用"
[ "$CONFIRMED" = 0 ] || [ "$EXECUTE_REQUESTED" = 1 ] || die 2 "--yes/--confirm 只能和 --execute 一起使用"
[ "$MODE" != execute ] || [ "$CONFIRMED" = 1 ] || die 2 "执行模式必须显式确认：加 --yes 或 --confirm"
if [ "$SCOPE" != dmr ]; then
  [ -f "$WEBUI_ENGINE" ] && [ ! -L "$WEBUI_ENGINE" ] && [ -x "$WEBUI_ENGINE" ] \
    || die 2 "WebUI 清理引擎不是可执行普通文件：$WEBUI_ENGINE"
fi
if [ "$SCOPE" != webui ]; then
  [ -f "$DMR_ENGINE" ] && [ ! -L "$DMR_ENGINE" ] && [ -x "$DMR_ENGINE" ] \
    || die 2 "DMR 清理脚本不是可执行普通文件：$DMR_ENGINE"
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
WEBUI_STATUS=not-run
DMR_STATUS=not-run
CURRENT_STEP=preflight

SYSTEM_PYTHON="${PYTHON_BIN:-$(command -v python3.12 || true)}"
[ -n "$SYSTEM_PYTHON" ] || die 2 "找不到 Python 3.12"
SYSTEM_PYTHON="$(readlink -f "$SYSTEM_PYTHON")"
[ -x "$SYSTEM_PYTHON" ] || die 2 "Python 解释器不可执行：$SYSTEM_PYTHON"
"$SYSTEM_PYTHON" -I -B -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
  || die 2 "聚合入口要求 Python 3.12：$SYSTEM_PYTHON"

write_summary() {
  local final_status="$1" rc="$2"
  "$SYSTEM_PYTHON" -I -B - "$SUMMARY" "$final_status" "$rc" "$MODE" "$SCOPE" "$RUN_DIR" \
    "$WEBUI_STATUS" "$DMR_STATUS" "$CURRENT_STEP" "$RETENTION_DAYS" "$KEEP_BACKUPS" <<'PY' || true
import json, os, sys
from datetime import datetime, timezone
out, status, rc, mode, scope, run_dir, webui, dmr, step, days, keep = sys.argv[1:]
data = {"component":"integrated-cleanup", "version":1, "status":status, "returncode":int(rc),
        "mode":mode, "scope":scope, "run_dir":run_dir,
        "sequence":["stop-webui","cleanup-webui","cleanup-dmr"] if scope == "all" else
                   (["stop-webui","cleanup-webui"] if scope == "webui" else ["cleanup-dmr"]),
        "webui_status":webui, "dmr_status":dmr, "last_step":step,
        "retention_days":int(days), "keep_backups":int(keep),
        "filesystem_blocks_freed":False,
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

if [ "$MODE" = preview ]; then
  printf '[预览] 总清理顺序：'
  if [ "$SCOPE" = webui ]; then
    printf 'WebUI（仅计划）'
  elif [ "$SCOPE" = dmr ]; then
    printf 'DMR（仅计划）'
  else
    printf '停止 WebUI → WebUI → DMR'
  fi
  printf '\n'
  if [ "$SCOPE" != dmr ]; then
    CURRENT_STEP=webui_preview
    run_logged webui-preview "$WEBUI_ENGINE" --preview --retention-days "$RETENTION_DAYS" --keep-backups "$KEEP_BACKUPS" --evidence-dir "$RUN_DIR/webui"
    WEBUI_STATUS=preview
  fi
  if [ "$SCOPE" != webui ]; then
    CURRENT_STEP=dmr_preview
    # 根清理器的 check 是只读预览；不调用 all，也不要求停止当前 WebUI。
    run_logged dmr-preview "$DMR_ENGINE" check --evidence-dir "$RUN_DIR/dmr"
    DMR_STATUS=preview
  fi
  printf '证据目录：%s\n' "$RUN_DIR"
  exit 0
fi

if [ "$SCOPE" != dmr ]; then
  CURRENT_STEP=stop_webui
  if run_logged stop-webui "$WEBUI_STOP"; then
    :
  else
    die 3 "WebUI 停止失败；未进入清理步骤"
  fi
  CURRENT_STEP=cleanup_webui
  if run_logged webui-cleanup "$WEBUI_ENGINE" --execute --yes --retention-days "$RETENTION_DAYS" \
      --keep-backups "$KEEP_BACKUPS" --evidence-dir "$RUN_DIR/webui"; then
    WEBUI_STATUS=success
  else
    WEBUI_STATUS=failed
    die 3 "WebUI 清理失败；不会继续执行 DMR 清理"
  fi
fi
if [ "$SCOPE" != webui ]; then
  CURRENT_STEP=cleanup_dmr
  if run_logged dmr-cleanup "$DMR_ENGINE" all --yes --evidence-dir "$RUN_DIR/dmr"; then
    DMR_STATUS=success
  else
    DMR_STATUS=failed
    die 3 "DMR 清理失败；保留各自证据目录"
  fi
fi
printf '一键清理完成。证据目录：%s\n' "$RUN_DIR"
exit 0

