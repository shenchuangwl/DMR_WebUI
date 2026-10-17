"""内核心跳文件（第 2 层 alerts）：内核每隔 ``heartbeat_s`` 秒原子地写一次，看护方（另一个进程或脚本）读它，
超过 ``heartbeat_alert_after_s`` 没更新就告警。

- ``beat``：临时文件（同目录、0600）写完整再 ``os.replace`` 改名，读的人要么看到旧的、要么看到新的，不会看到写了一半的。
  写失败（磁盘满、目录没权限）抛 ``OSError``，旧心跳保持完好、不留临时文件；怎么处理由调用方（调度循环）决定。
- ``read`` / ``stale`` 是静态方法：看护方不持有 Heartbeat 实例，只给路径。文件缺失或损坏都当作「没有心跳」。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from dmr_kernel.core.clock import Clock

log = logging.getLogger(__name__)

FILE_MODE = 0o600
DIR_MODE = 0o700


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class Heartbeat:
    def __init__(self, path: Path, clock: Clock, *, pid: int):
        if not _is_int(pid) or pid <= 0:
            raise ValueError(f"pid 必须是正整数：{pid!r}")
        self._path = Path(path)
        self._clock = clock
        self._pid = pid

    @property
    def path(self) -> Path:
        return self._path

    def beat(self) -> None:
        """原子写 ``{"pid", "at_ms"}``（临时文件 + rename，0600）。"""
        payload = json.dumps({"pid": self._pid, "at_ms": self._clock.now_ms()}, separators=(",", ":"))
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), FILE_MODE)
                handle.write(payload)
                handle.flush()
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def read(path: Path) -> dict | None:
        """读心跳：``{"pid": int, "at_ms": int}``；文件缺失、读不了或内容不对都返回 None。"""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        pid, at_ms = data.get("pid"), data.get("at_ms")
        if not (_is_int(pid) and pid > 0 and _is_int(at_ms) and at_ms >= 0):
            return None
        return {"pid": pid, "at_ms": at_ms}

    @staticmethod
    def stale(path: Path, now_ms: int, alert_after_s: int) -> tuple[bool, float | None]:
        """(是否超时, 距上次心跳的秒数)。距今超过 ``alert_after_s`` 才算超时（刚好等于不算）；
        没有可用的心跳时是 (True, None)；时钟回拨使心跳「来自未来」时距今按 0 计。"""
        data = Heartbeat.read(path)
        if data is None:
            return True, None
        age_s = max((now_ms - data["at_ms"]) / 1000, 0.0)
        return age_s > alert_after_s, age_s
