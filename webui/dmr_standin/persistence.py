"""替身交易所的持久化（docs/设计约定.md 3、7.1）：两把令牌、状态快照、操作留痕。

全部放在 ``state_dir``（缺省 ``var/standin``，目录 0700，文件 0600）：

- ``kernel.token``：给内核的适配层（相当于交易 API 密钥）；``manual.token``：给控制台的「模拟手动下单」与开发控制。
  首次启动生成，之后沿用；空文件或太短的令牌报错（不会静默换掉，免得已经读过令牌的进程失联）。
- ``state.json``：状态快照（``{"version": 1, "saved_ms", "engine", "behaviors", "subscribed"}``）。原子写：先写同目录的
  临时文件并 fsync，再 ``os.replace``。损坏或格式不对时拒绝启动（ValueError），文件原样保留，不会当成空账户。
- ``ops.log``：开发操作留痕，一行一条 JSON（追加写）；启动时读回最近的若干条。
"""
from __future__ import annotations

import json
import os
import secrets
from collections import deque
from pathlib import Path
from typing import Any

TOKEN_FILES = ("kernel.token", "manual.token")
MIN_TOKEN_LEN = 32
STATE_VERSION = 1


def ensure_dir(path: Path | str) -> Path:
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_new(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def ensure_tokens(state_dir: Path | str) -> tuple[str, str]:
    """返回 (kernel 令牌, manual 令牌)；没有就生成（0600），有就沿用。"""
    directory = ensure_dir(state_dir)
    tokens: list[str] = []
    for name in TOKEN_FILES:
        path = directory / name
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if not token:
                raise ValueError(f"令牌文件是空的：{path}（不会自动覆盖；确认没有进程在用它之后删掉再启动即可重新生成）")
            if len(token) < MIN_TOKEN_LEN or any(c.isspace() for c in token):
                raise ValueError(f"令牌文件内容不合规（至少 {MIN_TOKEN_LEN} 个字符、不含空白）：{path}")
            os.chmod(path, 0o600)
        else:
            token = secrets.token_urlsafe(32)
            _write_new(path, token + "\n")
        tokens.append(token)
    if tokens[0] == tokens[1]:
        raise ValueError(f"{directory} 里的两把令牌相同，请删掉后重新生成")
    return tokens[0], tokens[1]


class StateStore:
    def __init__(self, state_dir: Path | str):
        self.dir = Path(state_dir)
        self.path = self.dir / "state.json"

    def load(self) -> dict[str, Any] | None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError as exc:
            raise ValueError(f"替身状态快照损坏（不是 UTF-8）：{self.path}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"替身状态快照损坏：{self.path}（{exc}）；请检查后移走或删除，替身不会把它当成空账户") from exc
        if not (isinstance(data, dict) and data.get("version") == STATE_VERSION
                and isinstance(data.get("engine"), dict)):
            raise ValueError(f"替身状态快照格式不对：{self.path}")
        return data

    def save(self, data: dict[str, Any]) -> None:
        ensure_dir(self.dir)
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        tmp = self.dir / f".state.json.{os.getpid()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, text.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        dfd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


class OpsLog:
    """开发操作留痕：内存里保留最近 ``keep`` 条；``path`` 为空时不写文件（--no-persist）。"""

    def __init__(self, path: Path | str | None, keep: int = 1000):
        self.path = Path(path) if path is not None else None
        self._recent: deque[dict[str, Any]] = deque(maxlen=keep)
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-keep:]:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    self._recent.append(entry)

    def append(self, entry: dict[str, Any]) -> None:
        self._recent.append(entry)
        if self.path is None:
            return
        ensure_dir(self.path.parent)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            os.close(fd)

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        entries = list(self._recent)
        return entries[-limit:] if limit > 0 else []
