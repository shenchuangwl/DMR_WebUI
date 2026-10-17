"""账本的在线备份与恢复演练（第 1 层）。

- ``online_backup``：SQLite 在线备份接口（``Connection.backup``，一步复制完）。源库用只读连接，复制在一个读事务里
  完成，得到的是某一次提交之后的完整快照；WAL 模式下不挡写。备份先写到同目录的临时文件，转成回滚日志模式
  （自包含的单个文件，不需要 -wal）并通过 ``integrity_check`` 后，才以最终文件名出现；同一毫秒的备份不互相覆盖。
  文件名 ``ledger-<UTC 时间戳>[-<标签>][-<序号>].sqlite``，时间取注入的时钟；目录 0700、文件 0600。
- ``restore_backup``：演练用。复制备份到新位置并做 ``integrity_check``；目标已存在（含残留的 -wal、-shm、-journal）
  时拒绝，校验不通过时不留下任何文件。
"""
from __future__ import annotations

import os
import secrets
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from dmr_kernel.ledger import connect
from dmr_kernel.ledger.errors import LedgerError

__all__ = ["BACKUP_PREFIX", "BACKUP_SUFFIX", "backup_name", "online_backup", "restore_backup", "utc_stamp",
           "verify_backup"]

BACKUP_PREFIX = "ledger-"
BACKUP_SUFFIX = ".sqlite"
_COPY_CHUNK = 1 << 20


def utc_stamp(ms: int) -> str:
    """UTC 毫秒 → ``20251009T085320.000Z``（可以直接放进文件名，按字典序就是时间顺序）。"""
    seconds, millis = divmod(int(ms), 1000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y%m%dT%H%M%S") + f".{millis:03d}Z"


def backup_name(now_ms: int, label: str | None = None, seq: int = 0) -> str:
    parts = [BACKUP_PREFIX + utc_stamp(now_ms)]
    if label:
        parts.append(label)
    if seq:
        parts.append(str(seq))
    return "-".join(parts) + BACKUP_SUFFIX


def _remove_quietly(path: Path) -> None:
    for candidate in (path, *connect.side_files(path)):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _new_private_temp(directory: Path, stem: str) -> Path:
    while True:
        candidate = directory / f".{stem}.{secrets.token_hex(4)}.partial"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, connect.FILE_MODE)
        except FileExistsError:
            continue
        try:
            os.fchmod(fd, connect.FILE_MODE)
        finally:
            os.close(fd)
        return candidate


def _place_without_overwrite(tmp: Path, target: Path) -> None:
    """把临时文件放到目标名上；目标已存在时报 FileExistsError，绝不覆盖。"""
    try:
        os.link(tmp, target)  # 原子且不覆盖
    except FileExistsError:
        raise
    except OSError:  # 文件系统不支持硬链接
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"目标已存在：{target}") from None
        os.rename(tmp, target)
        return
    tmp.unlink()


def verify_backup(path: Path) -> None:
    """校验一个离线的账本文件：能打开、``integrity_check`` = ok、有账本身份。不改动文件，也不建 -wal、-shm。"""
    try:
        with closing(connect.connect_raw(path, immutable=True)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)]:
                raise LedgerError(f"账本文件校验不通过：{path}：{[row[0] for row in result[:5]]}")
            has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
            nonce = conn.execute("SELECT value FROM meta WHERE key = 'ledger_nonce'").fetchone() if has_meta else None
    except sqlite3.DatabaseError as exc:
        raise LedgerError(f"账本文件损坏或不是 SQLite 库：{path}：{exc}") from exc
    if not nonce:
        raise LedgerError(f"不是账本文件（没有 ledger_nonce）：{path}")


def online_backup(source: Path, dest_dir: Path, *, now_ms: int, label: str | None = None) -> Path:
    """把 ``source`` 在线备份到 ``dest_dir``，返回备份文件路径（见模块说明）。"""
    source, dest_dir = Path(source), Path(dest_dir)
    connect.ensure_private_dir(dest_dir)
    tmp = _new_private_temp(dest_dir, backup_name(now_ms, label))
    try:
        with closing(connect.connect_raw(source, readonly=True)) as src, \
                closing(sqlite3.connect(tmp, isolation_level=None)) as dst:
            src.backup(dst)  # pages=-1：一步复制完，源库上是一个读事务
            # 复制过来的文件头沿用源库的 WAL 标记；在同一连接上切回回滚日志模式，备份成为自包含的单个文件
            mode = dst.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                raise LedgerError(f"备份无法切换到回滚日志模式（{mode}）")
            result = dst.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)]:
                raise LedgerError(f"备份校验不通过：{[row[0] for row in result[:5]]}")
        _fsync_file(tmp)
        seq = 0
        while True:
            target = dest_dir / backup_name(now_ms, label, seq)
            try:
                _place_without_overwrite(tmp, target)
            except FileExistsError:
                seq += 1
                continue
            break
        connect.fsync_dir(dest_dir)
        return target
    finally:
        _remove_quietly(tmp)


def restore_backup(backup_file: Path, target: Path) -> None:
    """演练用：把备份复制到 ``target`` 并做 ``integrity_check``。不覆盖已有的库；失败时不留下文件。"""
    backup_file, target = Path(backup_file), Path(target)
    if not backup_file.is_file():
        raise FileNotFoundError(f"备份文件不存在：{backup_file}")
    for existing in (target, *connect.side_files(target)):
        if existing.exists() or existing.is_symlink():
            raise FileExistsError(f"恢复目标已存在：{existing}（不覆盖已有的库，残留的日志文件也会套到恢复出来的库上）")
    connect.ensure_private_dir(target.parent)
    tmp = _new_private_temp(target.parent, target.name)
    try:
        with open(backup_file, "rb") as src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out, _COPY_CHUNK)
            out.flush()
            os.fsync(out.fileno())
        verify_backup(tmp)
        _place_without_overwrite(tmp, target)
        connect.fsync_dir(target.parent)
    finally:
        _remove_quietly(tmp)
