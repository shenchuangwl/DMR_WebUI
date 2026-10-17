"""账本备份恢复演练（由 deploy/backup_drill.sh 在内核环境里运行）。

取备份目录里最新的 ``ledger-*.sqlite``（内核在线备份的产物），用 ``restore_backup`` 恢复到临时目录，然后：
``PRAGMA integrity_check``、``PRAGMA foreign_key_check``、每张表的行数、与备份文件逐字节比对（SHA-256）。
通过与否都打印结果并删除临时目录（``--keep`` 除外）。绝不打开正在使用的账本。

退出码：0 通过；1 恢复或检查失败；2 用法错误、没有备份、或指定的文件是正在使用的账本。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

WEBUI_ROOT = Path(__file__).resolve().parents[1]
if str(WEBUI_ROOT) not in sys.path:
    sys.path.insert(0, str(WEBUI_ROOT))

from dmr_kernel.ledger import connect  # noqa: E402
from dmr_kernel.ledger.backup import BACKUP_PREFIX, BACKUP_SUFFIX, restore_backup  # noqa: E402
from dmr_kernel.ledger.errors import LedgerError  # noqa: E402

STAMP_LEN = len("20251009T085320.000Z")
STALE_AFTER_S = 3 * 3600  # 内核每小时在线备份一次；最新的备份超过 3 小时就提醒（不判失败）


def candidates(backup_dir: Path) -> list[Path]:
    """备份目录里的账本备份文件（不含写了一半的 .partial 与别的后缀）。"""
    if not backup_dir.is_dir():
        return []
    return [path for path in backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}")
            if path.is_file() and not path.name.startswith(".")]


def latest(backup_dir: Path) -> Path | None:
    """最新的备份：文件名里的 UTC 时间戳（字典序即时间序）优先，其次修改时间。"""
    found = candidates(backup_dir)
    if not found:
        return None
    return max(found, key=lambda path: (path.name[len(BACKUP_PREFIX):len(BACKUP_PREFIX) + STAMP_LEN],
                                        path.stat().st_mtime_ns, path.name))


def stamp_of(path: Path) -> datetime | None:
    text = path.name[len(BACKUP_PREFIX):len(BACKUP_PREFIX) + STAMP_LEN]
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def inspect_ledger(path: Path) -> dict:
    """只读打开一个离线的账本文件（不建 -wal、-shm），返回检查结果。"""
    with closing(connect.connect_raw(path, immutable=True)) as conn:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {name: conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(name)}").fetchone()[0] for name in tables}
        version = None
        if "alembic_version" in tables:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            version = row[0] if row else None
    return {"integrity": integrity, "foreign_key_violations": len(violations), "counts": counts, "version": version}


def human_size(size: int) -> str:
    return f"{size / 1024:.1f} KiB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MiB"


def human_age(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} 秒"
    if seconds < 7200:
        return f"{seconds / 60:.0f} 分钟"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def parse_args(argv: Sequence[str] | None, env: Mapping[str, str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="backup_drill.sh", description="账本备份恢复演练：恢复最新的在线备份并做一致性检查")
    parser.add_argument("--backup-dir", type=Path, default=Path(env.get("DMR_BACKUP_DIR") or WEBUI_ROOT / "backup"),
                        help="备份目录（缺省 $DMR_BACKUP_DIR，再缺省 <工程根>/backup）")
    parser.add_argument("--file", type=Path, default=None, help="指定备份文件，而不是取最新的")
    parser.add_argument("--keep", action="store_true", help="演练后保留恢复出来的库和临时目录")
    return parser.parse_args(argv)


def live_ledger_path(env: Mapping[str, str]) -> Path:
    var_dir = Path(env.get("DMR_VAR_DIR") or WEBUI_ROOT / "var")
    return Path(env.get("DMR_DB_PATH") or var_dir / "kernel" / "ledger.sqlite")


def is_same_file(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve() or (first.exists() and second.exists() and first.samefile(second))
    except OSError:
        return False


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    args = parse_args(argv, env)
    live = live_ledger_path(env)

    if args.file is not None:
        backup = args.file
        if is_same_file(backup, live):
            print(f"错误：{backup} 是正在使用的账本，演练只用备份文件（不碰正在使用的账本）", file=sys.stderr)
            return 2
        if not backup.is_file():
            print(f"错误：指定的备份文件不存在：{backup}", file=sys.stderr)
            return 2
    else:
        backup = latest(args.backup_dir)
        if backup is None:
            print(f"错误：在 {args.backup_dir} 里没有找到账本备份（ledger-*{BACKUP_SUFFIX}）。"
                  "内核每小时在线备份一次；还没有备份时没有可演练的东西", file=sys.stderr)
            return 2

    print("账本备份恢复演练")
    print(f"  备份目录：{backup.parent}")
    stamp = stamp_of(backup)
    age_text = ""
    if stamp is not None:
        age_s = (datetime.now(timezone.utc) - stamp).total_seconds()
        age_text = f"，生成于 {stamp:%Y-%m-%dT%H:%M:%SZ}，距今 {human_age(age_s)}"
    print(f"  选用备份：{backup.name}（{human_size(backup.stat().st_size)}{age_text}）")
    if stamp is not None and (datetime.now(timezone.utc) - stamp).total_seconds() > STALE_AFTER_S:
        print(f"  提醒：最新的备份已超过 {STALE_AFTER_S // 3600} 小时，请确认内核的定时在线备份在运行")

    workdir = Path(tempfile.mkdtemp(prefix="dmr-backup-drill-"))
    target = workdir / "restored.sqlite"
    print(f"  恢复位置：{target}（{'已选择保留' if args.keep else '演练结束后删除'}）")
    try:
        restore_backup(backup, target)  # 复制并做 integrity_check；失败时不留下文件
        restored = inspect_ledger(target)
        original = inspect_ledger(backup)
        copy_digest, source_digest = sha256(target), sha256(backup)
    except (LedgerError, OSError, FileExistsError, FileNotFoundError) as exc:
        print(f"  结论：失败 —— {backup.name} 无法恢复：{exc}")
        print(f"错误：恢复演练失败：{backup.name}：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 演练要给出明确的失败结论，而不是一串栈
        print(f"  结论：失败 —— {backup.name} 检查时出错：{type(exc).__name__}: {exc}")
        print(f"错误：恢复演练失败：{backup.name}：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.keep:
            print(f"  已保留：{workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    problems: list[str] = []
    integrity_ok = restored["integrity"] == ["ok"]
    print(f"  完整性检查（PRAGMA integrity_check）：{'ok' if integrity_ok else '; '.join(restored['integrity'][:5])}")
    if not integrity_ok:
        problems.append("integrity_check 不是 ok")
    violations = restored["foreign_key_violations"]
    print(f"  外键检查（PRAGMA foreign_key_check）：{'无问题' if not violations else f'{violations} 处违反'}")
    if violations:
        problems.append(f"外键检查发现 {violations} 处违反")
    if restored["version"]:
        print(f"  结构版本：{restored['version']}")
    counts = restored["counts"]
    print(f"  各表行数（共 {len(counts)} 张）：")
    for name, count in counts.items():
        print(f"    {name}: {count}")
    if counts != original["counts"]:
        problems.append("恢复出的库与备份文件的各表行数不一致")
    if copy_digest != source_digest:
        problems.append("恢复出的文件与备份文件内容不一致")
    print(f"  与备份文件比对：{'内容一致' if copy_digest == source_digest else '内容不一致'}（SHA-256 前 12 位 {source_digest[:12]}）")

    if problems:
        print(f"  结论：失败 —— {'；'.join(problems)}")
        print(f"错误：恢复演练失败：{backup.name}：{'；'.join(problems)}", file=sys.stderr)
        return 1
    print("  结论：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
