#!/usr/bin/env bash
# WebUI 清理引擎：默认预览；执行须 --execute 与 --yes/--confirm。
set -euo pipefail
export PATH=/usr/bin:/bin
umask 077
unset VIRTUAL_ENV PYTHONHOME PYTHONPATH PYTHONSTARTUP

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"
WEBUI_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
DMR_ROOT="$(cd "$WEBUI_ROOT/.." && pwd -P)"
EVIDENCE_DIR="$(cd "$WEBUI_ROOT/../.." && pwd -P)/.dmr-webui-cleanup"
MODE=preview
CONFIRMED=0
EXECUTE_REQUESTED=0
PREVIEW_REQUESTED=0
RETENTION_DAYS=30
KEEP_BACKUPS=2

die() { local code="$1"; shift; printf '错误：%s\n' "$*" >&2; exit "$code"; }
usage() {
  cat <<'HELP'
WebUI 清理引擎（webui/deploy/cleanup_webui.sh）
用法：cleanup_webui.sh [--preview] [--execute --yes|--confirm]
                    [--retention-days N] [--keep-backups N] [--evidence-dir DIR]
默认只预览。只清理缓存、日志、过期 ledger-*.sqlite 备份和陈旧 PID；
账本/WAL/SHM、令牌、口令、锁、两个虚拟环境、锁文件永远保护。
执行只做同文件系统 rename 到项目外隔离目录，不直接删除。
HELP
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --preview) PREVIEW_REQUESTED=1; MODE=preview; shift ;;
    --execute) EXECUTE_REQUESTED=1; MODE=execute; shift ;;
    --yes|--confirm) CONFIRMED=1; shift ;;
    --retention-days) [ "$#" -ge 2 ] || die 2 "--retention-days 缺少值"; RETENTION_DAYS="$2"; shift 2 ;;
    --keep-backups) [ "$#" -ge 2 ] || die 2 "--keep-backups 缺少值"; KEEP_BACKUPS="$2"; shift 2 ;;
    --evidence-dir) [ "$#" -ge 2 ] || die 2 "--evidence-dir 缺少目录"; EVIDENCE_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die 2 "不认识的参数：$1（使用 --help 查看用法）" ;;
  esac
done
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || die 2 "--retention-days 必须是非负整数"
[[ "$KEEP_BACKUPS" =~ ^[1-9][0-9]*$ ]] || die 2 "--keep-backups 必须是正整数"
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

SYSTEM_PYTHON="${PYTHON_BIN:-$(command -v python3.12 || true)}"
[ -n "$SYSTEM_PYTHON" ] || die 2 "找不到 Python 3.12"
SYSTEM_PYTHON="$(readlink -f "$SYSTEM_PYTHON")"
[ -x "$SYSTEM_PYTHON" ] || die 2 "Python 解释器不可执行：$SYSTEM_PYTHON"
"$SYSTEM_PYTHON" -I -B -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
  || die 2 "清理器要求 Python 3.12：$SYSTEM_PYTHON"

exec "$SYSTEM_PYTHON" -I -B - "$WEBUI_ROOT" "$MODE" "$EVIDENCE_DIR" "$RETENTION_DAYS" "$KEEP_BACKUPS" <<'PY'
import hashlib
import fcntl
import json
import os
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
evidence = Path(sys.argv[3]).resolve()
retention_days = int(sys.argv[4])
keep_backups = int(sys.argv[5])
result_path = evidence / "result.json"
journal_path = evidence / "journal.jsonl"
evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(evidence, 0o700)
root_dev = root.stat().st_dev
# Keep the lock outside the project and independent of the caller's evidence
# directory, so deploy/cleanup invocations still serialize across evidence dirs.
lock_path = root.parent.parent / ".dmr-webui-operation.lock"
if lock_path.is_symlink():
    raise SystemExit("项目操作锁不能是符号链接，拒绝执行。")
lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
os.fchmod(lock_fd, 0o600)
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("清理器已有另一个实例在运行，拒绝并发执行。", file=sys.stderr)
    raise SystemExit(3)

def rel(path):
    return path.relative_to(root).as_posix()

def under(path, parent=root):
    try:
        path.resolve().relative_to(parent)
        return True
    except ValueError:
        return False

def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def metadata(path, include_hash=True):
    st = path.lstat()
    item = {"type": stat.filemode(st.st_mode), "mode": stat.S_IMODE(st.st_mode),
            "size": st.st_size, "mtime_ns": st.st_mtime_ns, "inode": st.st_ino,
            "device": st.st_dev}
    if stat.S_ISLNK(st.st_mode):
        item["target"] = os.readlink(path)
    elif stat.S_ISREG(st.st_mode) and include_hash:
        item["sha256"] = file_hash(path)
    return item

def sensitive_relpath(value):
    """Return true for files whose contents must never enter evidence."""
    lower = value.lower()
    name = Path(value).name.lower()
    return (name == ".env" or name.startswith(".env.") or
            name.endswith((".token", ".password", ".secret")) or
            "ledger.sqlite" in name)

def protected_snapshot():
    patterns = [
        ".venv-kernel", ".venv-console", "requirements/*.lock", "requirements/*.in",
        "var/kernel/ledger.sqlite", "var/kernel/ledger.sqlite-wal", "var/kernel/ledger.sqlite-shm",
        "var/kernel/*.token", "var/kernel.lock", "var/kernel/heartbeat.json",
        "var/standin/*.token", "var/standin/state.json", "var/standin/ops.log",
        "var/operators/*.password", "var/console/session.secret",
        "var/run/*.lock", "var/run/kernel.nosupervise",
    ]
    paths = []
    for pattern in patterns:
        found = list(root.glob(pattern))
        if not found and "*" not in pattern:
            found = [root / pattern]
        paths.extend(found)
    result = {}
    for path in sorted(set(paths)):
        key = rel(path)
        if not path.exists() and not path.is_symlink():
            result[key] = {"missing": True}
            continue
        if path.is_dir() and not path.is_symlink():
            tree = {}
            for current, dirs, files in os.walk(path, topdown=True, followlinks=False):
                dirs.sort(); files.sort()
                current_path = Path(current)
                for name in dirs + files:
                    child = current_path / name
                    try:
                        child_key = rel(child)
                        tree[child_key] = metadata(
                            child,
                            include_hash=(not path.name.startswith(".venv-") and
                                          not sensitive_relpath(child_key)),
                        )
                    except FileNotFoundError:
                        tree[rel(child)] = {"raced": True}
            result[key] = {"directory": True, "tree": tree, **metadata(path, include_hash=False)}
        else:
            result[key] = metadata(path, include_hash=not sensitive_relpath(key))
    return result

def proc_cmd(pid):
    try:
        value = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        return value, None
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return "", type(exc).__name__
    except UnicodeError:
        return "", "UnicodeError"

def pid_from_file(path):
    try:
        value = path.read_text(encoding="ascii").strip()
        return int(value) if value.isdigit() and int(value) > 0 else None
    except (OSError, ValueError):
        return None

def process_check():
    active = []
    pid_reused = []
    scan_errors = []
    scan_error_index = {}
    def record_scan_error(source, error, pid=None):
        key = (source, error)
        index = scan_error_index.get(key)
        if index is None:
            item = {"source": source, "error": error, "count": 1}
            if pid is not None:
                item["sample_pid"] = pid
            scan_error_index[key] = len(scan_errors)
            scan_errors.append(item)
        else:
            scan_errors[index]["count"] += 1
    pid_dir = root / "var" / "run"
    expected = {"standin": "dmr_standin", "kernel": "dmr_kernel", "console": "dmr_console", "supervise": "supervise.sh"}
    for name, marker in expected.items():
        pid = pid_from_file(pid_dir / f"{name}.pid")
        if pid is None:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            record_scan_error("pid-file", "permission", pid)
            continue
        except OSError as exc:
            record_scan_error("pid-file", type(exc).__name__, pid)
            continue
        cmd, cmd_error = proc_cmd(pid)
        if cmd is None:
            continue
        if cmd_error:
            record_scan_error("pid-file/cmdline", cmd_error, pid)
        elif marker in cmd:
            active.append({"pid": pid, "kind": name, "reference": "pid-file"})
        else:
            pid_reused.append({"pid": pid, "kind": name, "reference": "pid-file"})
    ancestors = {os.getpid()}
    current = os.getppid()
    while current > 1:
        ancestors.add(current)
        try:
            current = int(Path(f"/proc/{current}/stat").read_text().split()[3])
        except (OSError, ValueError, IndexError):
            break
    root_text = str(root)
    # Inspect every visible process.  We do not rely on a command-name
    # shortlist because an arbitrary Python process can hold a WebUI path.
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError as exc:
        proc_entries = []
        record_scan_error("/proc", type(exc).__name__)
    for entry in proc_entries:
        if not entry.name.isdigit() or int(entry.name) in ancestors:
            continue
        pid = int(entry.name)
        cmd, cmd_error = proc_cmd(pid)
        if cmd is None:
            continue
        if cmd_error:
            record_scan_error("proc/cmdline", cmd_error, pid)
            continue
        if not cmd:
            # Kernel threads legitimately have an empty cmdline and no user
            # file descriptors.  Do not let them make cleanup impossible;
            # an unreadable status file remains fail-closed.
            try:
                is_kthread = any(
                    line.partition(":")[0] == "Kthread" and
                    line.partition(":")[2].strip() == "1"
                    for line in Path(f"/proc/{pid}/status").read_text(errors="replace").splitlines()
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                record_scan_error("proc/status", type(exc).__name__, pid)
                continue
            if is_kthread:
                continue
        paths = []
        for name in ("cwd", "exe"):
            try:
                # readlink is intentional: unlike realpath(strict=False), it
                # does not turn a permission error into a plausible path.
                paths.append(os.readlink(entry / name))
            except FileNotFoundError:
                pass
            except OSError as exc:
                record_scan_error(f"proc/{name}", type(exc).__name__, pid)
        try:
            for fd in (entry / "fd").iterdir():
                try:
                    paths.append(os.readlink(fd))
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    record_scan_error("proc/fd", type(exc).__name__, pid)
        except FileNotFoundError:
            pass
        except OSError as exc:
            record_scan_error("proc/fd", type(exc).__name__, pid)
        try:
            maps = (entry / "maps").read_text(errors="replace")
            paths.extend(line.split()[-1] for line in maps.splitlines() if line.split() and "/" in line.split()[-1])
        except FileNotFoundError:
            pass
        except OSError as exc:
            record_scan_error("proc/maps", type(exc).__name__, pid)
        if root_text in cmd or any(p == root_text or p.startswith(root_text + os.sep) for p in paths):
            if not any(item["pid"] == pid for item in active):
                active.append({"pid": pid, "kind": "proc-scan", "reference": "proc-path"})
    return {"active": active, "pid_reused": pid_reused, "scan_errors": scan_errors,
            "blocked": bool(active or pid_reused or scan_errors)}

def add_candidate(items, path, kind):
    if not under(path) or path.is_symlink():
        return
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if st.st_dev != root_dev or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        return
    items.append({"path": rel(path), "kind": kind, "size": st.st_size,
                  "mtime_ns": st.st_mtime_ns, "mode": stat.S_IMODE(st.st_mode),
                  "inode": st.st_ino, "device": st.st_dev})

def inventory():
    items = []
    skip = {".git", ".venv-kernel", ".venv-console", "backup", "var", "graphify-out"}
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for name in sorted(dirs):
            child = current_path / name
            if name in skip:
                continue
            if name in {"__pycache__", ".pytest_cache", ".nicegui"}:
                add_candidate(items, child, "cache")
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
    log_dir = root / "var" / "log"
    if log_dir.is_dir():
        for path in sorted(log_dir.iterdir()):
            if path.is_file() and not path.is_symlink() and (path.name.endswith((".log", ".out")) or ".log." in path.name or ".out." in path.name):
                add_candidate(items, path, "log")
    run_dir = root / "var" / "run"
    if run_dir.is_dir():
        for path in sorted(run_dir.glob("*.pid")):
            pid = pid_from_file(path)
            alive = False
            if pid:
                try:
                    os.kill(pid, 0); alive = True
                except OSError:
                    pass
            if not alive:
                add_candidate(items, path, "stale-pid")
    cutoff = time.time() - retention_days * 86400
    backup_dir = root / "backup"
    backups = []
    if backup_dir.is_dir():
        for path in sorted(backup_dir.glob("ledger-*.sqlite")):
            if path.is_file() and not path.is_symlink():
                backups.append(path)
    backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    kept = []
    for index, path in enumerate(backups):
        if "pre-upgrade" in path.name or index < keep_backups or path.stat().st_mtime >= cutoff:
            kept.append(rel(path))
    for path in backups:
        if rel(path) not in kept:
            add_candidate(items, path, "expired-backup")
    unique = {item["path"]: item for item in items}
    return sorted(unique.values(), key=lambda item: item["path"]), {
        "cutoff_utc": datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
        "keep_backups": keep_backups, "retention_days": retention_days, "kept_backups": kept,
    }

def append_journal(entry):
    if journal_path.is_symlink():
        raise RuntimeError("journal.jsonl 不能是符号链接")
    with journal_path.open("a", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush(); os.fsync(fh.fileno())

process = process_check()
candidates, retention = inventory()
before = protected_snapshot()
write_json(evidence / "protected-before.json", before)
write_json(evidence / "plan.json", {"candidates": candidates, "retention": retention, "process_check": process})
moved = []
status = "preview"
error = None
archive = None
if mode == "execute":
    if process["blocked"]:
        status, error = "blocked", "WebUI 进程仍在运行或进程扫描不可读，拒绝清理"
    elif evidence.stat().st_dev != root_dev:
        status, error = "failed", "证据/隔离目录必须与项目同一文件系统"
    else:
        archive = Path(tempfile.mkdtemp(prefix="quarantine-", dir=str(evidence)))
        os.chmod(archive, 0o700)
        for item in candidates:
            source = root / item["path"]
            try:
                current = source.lstat()
                if (stat.S_IMODE(current.st_mode), current.st_mtime_ns, current.st_size,
                    current.st_ino, current.st_dev) != (item["mode"], item["mtime_ns"],
                                                        item["size"], item["inode"], item["device"]):
                    raise RuntimeError("候选项在执行前发生变化")
                if source.is_symlink() or current.st_dev != root_dev:
                    raise RuntimeError("候选项是符号链接或跨文件系统")
                destination = archive / item["path"]
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                append_journal({"action":"plan", "source":str(source), "destination":str(destination), "item":item})
                os.rename(source, destination)
                append_journal({"action":"moved", "source":str(source), "destination":str(destination)})
                moved.append({"source":item["path"], "destination":str(destination), "kind":item["kind"]})
            except Exception as exc:
                error, status = str(exc), "incomplete"
                break
        if error is None:
            status = "success"
after = protected_snapshot()
write_json(evidence / "protected-after.json", after)
protected_ok = before == after
if not protected_ok:
    status, error = "failed", error or "保护对象前后状态不一致"
remaining, _ = inventory()
if mode == "preview":
    print(f"[预览] WebUI 清理候选 {len(candidates)} 项；未移动文件。")
    if process["blocked"]:
        print("检测到运行中进程或不可读的进程扫描状态；执行时会拒绝，需先停止 WebUI 并确认扫描可读。")
if archive:
    print("可恢复隔离目录: " + str(archive))
result = {
    "component":"webui", "version":1, "status":status, "mode":mode,
    "webui_root":str(root), "evidence_dir":str(evidence), "process_check":process,
    "candidate_count":len(candidates), "candidates":candidates, "moved":moved,
    "remaining_candidates":remaining, "protected_ok":protected_ok, "retention":retention,
    "archive":str(archive) if archive else None, "filesystem_blocks_freed":False,
    "error":error, "created_at_utc":datetime.now(timezone.utc).isoformat(),
}
write_json(result_path, result)
print("证据目录: " + str(evidence))
if mode == "execute" and status != "success":
    raise SystemExit(3)
PY

