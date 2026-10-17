"""会话签名密钥：给浏览器 Cookie 签名、区分不同浏览器会话；首次启动随机生成，之后沿用。

文件 ``var/console/session.secret``，权限 0600，只有运行控制台的系统用户读得到。内核令牌不在这里：
令牌只存在控制台进程内存里（见 ``session.py``），重启控制台后需要重新登录，但内核不受影响。
"""
from __future__ import annotations

import errno
import logging
import os
import re
import secrets
import stat
from pathlib import Path

log = logging.getLogger(__name__)

SECRET_MIN_CHARS = 32
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]+")
_ATTEMPTS = 3


class SecretError(RuntimeError):
    pass


def _generate() -> str:
    return secrets.token_urlsafe(48)


def _write_new(path: Path, text: str, *, replace: bool) -> None:
    """用 O_EXCL 写 0600 的新文件；replace=True 时先写同目录临时文件再原子替换。"""
    target = path.with_name(path.name + ".tmp") if replace else path
    if replace:
        target.unlink(missing_ok=True)  # 上次中断留下的临时文件不应该永远挡住替换
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if replace:
            target.unlink(missing_ok=True)
        raise
    if replace:
        os.replace(target, path)


def _read_existing(path: Path) -> str | None:
    """返回现有的有效密钥；文件不存在返回 None；内容不合格返回 ""（调用方重新生成）。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SecretError(f"{path} 是符号链接，拒绝使用") from exc
        raise SecretError(f"无法打开 {path}：{exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretError(f"{path} 不是普通文件")
        if info.st_uid != os.getuid():
            raise SecretError(f"{path} 不属于当前系统用户，拒绝使用")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, 0o600)
            log.warning("会话签名密钥文件权限过宽，已改为 0600：%s", path)
        raw = os.read(fd, 4096)
    finally:
        os.close(fd)
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""
    if len(text) >= SECRET_MIN_CHARS and _SECRET_RE.fullmatch(text):
        return text
    return ""


def load_or_create_secret(path: Path) -> str:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise SecretError(f"无法创建目录 {path.parent}：{exc.strerror}") from exc
    for _ in range(_ATTEMPTS):
        existing = _read_existing(path)
        if existing:
            return existing
        fresh = _generate()
        try:
            _write_new(path, fresh, replace=existing == "")
        except FileExistsError:
            continue  # 另一个进程刚创建了它：重新读取
        except OSError as exc:
            raise SecretError(f"无法写入 {path}：{exc.strerror}") from exc
        log.info("已生成会话签名密钥：%s", path)
        return fresh
    raise SecretError(f"反复竞争失败，无法取得 {path}")
