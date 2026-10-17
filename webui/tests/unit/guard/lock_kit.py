"""旧入口互斥测试的工具：模拟 DMR_WebUI/data/runtime_state 的临时目录，以及像旧入口那样在另一个进程里持锁。

绝不碰真实的 DMR_WebUI/data/runtime_state：这里所有路径都建在 pytest 的 tmp_path 下。
"""
from __future__ import annotations

import os
import select
import subprocess
import sys
from pathlib import Path

MARKER = "LEGACY_DISABLED"
NAMES = ("bit_short_term", "bit_long_term", "first_short_term", "first_long_term",
         "legacy_short_term", "legacy_long_term", "main")

# 与旧入口的单实例锁同一套做法（common/atomic_state.py 的 single_instance）：
# open('a+') → flock(LOCK_EX | LOCK_NB) → 清空并写进程号；一直持锁，直到标准输入被关闭。
_HOLDER_CODE = r"""
import fcntl, os, sys
handle = open(sys.argv[1], "a+")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
handle.seek(0)
handle.truncate()
if sys.argv[2] == "pid":
    handle.write(str(os.getpid()))
handle.flush()
sys.stdout.write("locked\n")
sys.stdout.flush()
sys.stdin.read()
"""


def lock_path(runtime_dir: Path, name: str) -> Path:
    return runtime_dir / f"instance_{name}.lock"


def make_runtime_dir(root: Path, *, marker: bool = True) -> Path:
    """在 root 下建一个模拟的 DMR_WebUI/data/runtime_state（0700），可选放一个停用标记（0600）。"""
    runtime = root / "DMR_WebUI" / "data" / "runtime_state"
    runtime.mkdir(parents=True)
    os.chmod(runtime, 0o700)
    if marker:
        put_marker(runtime)
    return runtime


def put_marker(runtime: Path) -> Path:
    path = runtime / MARKER
    path.write_text("disabled for tests\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class LockHolder:
    """另一个进程持有一把旧入口锁。``pid`` 是它的进程号；``stop()`` 让它退出（锁随之释放）。"""

    def __init__(self, path: Path, *, write_pid: bool = True) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", _HOLDER_CODE, str(path), "pid" if write_pid else "nopid"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], 15)
        line = self.proc.stdout.readline() if ready else ""
        if line.strip() != "locked":
            self.kill()
            raise AssertionError(f"持锁进程没有就绪：{line!r}")

    @property
    def pid(self) -> int:
        return self.proc.pid

    def stop(self) -> None:
        if self.proc.poll() is None:
            assert self.proc.stdin is not None
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.kill()
        self._close_pipes()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._close_pipes()

    def _close_pipes(self) -> None:
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
