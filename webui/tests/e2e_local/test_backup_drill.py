"""deploy/backup_drill.sh：取 backup/ 下最新的账本备份，恢复到临时目录并做一致性检查；不碰正在用的账本。

备份由真实账本经 ``online_backup`` 生成（与内核定时备份用的是同一个函数）；脚本在内核环境里运行。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from deploy_kit import DEPLOY, script_env
from tests.support.ledger import START_MS, open_ledger

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.types import AlertLevel
from dmr_kernel.ledger import repo_alerts
from dmr_kernel.ledger.backup import online_backup

pytestmark = pytest.mark.e2e

HOUR_MS = 3_600_000


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.var = tmp_path / "var"
        self.backup_dir = tmp_path / "backup"
        self.scratch = tmp_path / "scratch"  # 脚本的 TMPDIR：演练后必须是空的
        self.scratch.mkdir()
        self.clock = ManualClock(START_MS)
        self.live = open_ledger(self.var, self.clock)  # 正在用的账本：var/kernel/ledger.sqlite
        self.live_path = self.live.path

    def add_alerts(self, count: int) -> None:
        for index in range(count):
            self.live.write_sync(repo_alerts.insert, at_ms=START_MS + index, level=AlertLevel.INFO, kind="drill",
                                 message=f"row {index}")

    def backup(self, *, hours_after_start: int = 0, label: str | None = None) -> Path:
        return online_backup(self.live_path, self.backup_dir, now_ms=START_MS + hours_after_start * HOUR_MS, label=label)

    def run(self, *args: str, env: dict[str, str | None] | None = None) -> subprocess.CompletedProcess[str]:
        merged = script_env(self.var, DMR_BACKUP_DIR=str(self.backup_dir), TMPDIR=str(self.scratch), **(env or {}))
        return subprocess.run([str(DEPLOY / "backup_drill.sh"), *args], env=merged, cwd=self.tmp, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)

    def live_fingerprint(self) -> list[tuple[str, int, int]]:
        """正在用的账本及其旁边文件的 (名字, 大小, 修改时间)；演练前后必须一样。"""
        found = []
        for path in sorted(self.live_path.parent.iterdir()):
            stat = path.stat()
            found.append((path.name, stat.st_size, stat.st_mtime_ns))
        return found


@pytest.fixture
def drill(tmp_path):
    fixture = Fixture(tmp_path)
    try:
        yield fixture
    finally:
        fixture.live.close_sync()


def table_counts(output: str) -> dict[str, int]:
    return {name: int(count) for name, count in re.findall(r"^\s+(\w+)\s*[:：]\s*(\d+)\s*$", output, re.M)}


def test_the_newest_backup_is_restored_and_checked_and_the_live_ledger_is_not_touched(drill):
    drill.add_alerts(2)
    drill.backup(hours_after_start=0)  # 较旧：2 行
    drill.add_alerts(3)
    newest = drill.backup(hours_after_start=5)  # 较新：5 行
    drill.add_alerts(4)  # 备份之后又写了 4 行：只存在于正在用的账本里
    drill.live.close_sync()  # 检查点后关库：账本旁边不再有 -wal；演练前后一模一样
    before = drill.live_fingerprint()

    result = drill.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert newest.name in result.stdout, "应选最新的备份"
    assert re.search(r"integrity_check.*\bok\b", result.stdout), result.stdout
    counts = table_counts(result.stdout)
    assert counts["alert"] == 5, f"恢复出来的是最新备份的内容，不是旧备份也不是正在用的账本：{counts}"
    assert {"meta", "audit_log", "position", "intent", "orders", "fill"} <= set(counts), "每张表都要列出行数"
    assert "通过" in result.stdout
    assert drill.live_fingerprint() == before, "演练不能碰正在用的账本（含不能在它旁边建 -wal、-shm）"
    assert list(drill.scratch.iterdir()) == [], "临时恢复目录演练后应删除"
    restored = re.search(r"(\S+restored\.sqlite)", result.stdout)
    assert restored and not Path(restored.group(1)).exists()


def test_no_backup_at_all_is_an_error_with_a_clear_message(drill):
    result = drill.run()
    assert result.returncode == 2, result.stdout + result.stderr
    assert "没有找到账本备份" in result.stderr
    assert list(drill.scratch.iterdir()) == []


def test_a_corrupt_newest_backup_fails_the_drill_and_leaves_nothing_behind(drill):
    drill.add_alerts(2)
    drill.backup(hours_after_start=0)
    bad = drill.backup(hours_after_start=3)
    data = bytearray(bad.read_bytes())
    for offset in range(4096, min(len(data), 4096 * 6), 7):  # 在内容页里写坏一批字节，文件头保持完好
        data[offset] ^= 0xFF
    bad.write_bytes(bytes(data))
    result = drill.run()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "失败" in result.stdout + result.stderr and bad.name in result.stdout + result.stderr
    assert list(drill.scratch.iterdir()) == [], "校验失败时也不能留下恢复出来的文件"


def test_a_file_that_is_not_a_ledger_fails_the_drill(drill):
    drill.backup_dir.mkdir(parents=True)
    (drill.backup_dir / "ledger-20990101T000000.000Z.sqlite").write_bytes(b"not a database" * 100)
    result = drill.run()
    assert result.returncode == 1, result.stdout + result.stderr
    assert list(drill.scratch.iterdir()) == []


def test_an_explicit_file_can_be_chosen_but_never_the_live_ledger(drill):
    drill.add_alerts(1)
    older = drill.backup(hours_after_start=0)
    drill.add_alerts(1)
    drill.backup(hours_after_start=2)
    chosen = drill.run("--file", str(older))
    assert chosen.returncode == 0 and table_counts(chosen.stdout)["alert"] == 1, chosen.stdout + chosen.stderr
    drill.live.close_sync()
    refused = drill.run("--file", str(drill.live_path))
    assert refused.returncode == 2 and "正在使用" in refused.stderr, refused.stdout + refused.stderr


def test_the_drill_ignores_partial_files_and_unrelated_files_in_the_backup_dir(drill):
    drill.add_alerts(2)
    good = drill.backup(hours_after_start=1)
    (drill.backup_dir / ".ledger-20990101T000000.000Z.sqlite.ab12cd34.partial").write_bytes(b"partial")
    (drill.backup_dir / "notes.txt").write_text("not a backup")
    (drill.backup_dir / "ledger-20990101T000000.000Z.sqlite.bak").write_text("other suffix")
    result = drill.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert good.name in result.stdout


def test_the_script_never_uses_a_bare_interpreter_and_runs_from_any_directory(drill):
    drill.add_alerts(1)
    drill.backup()
    env = {key: value for key, value in os.environ.items() if not key.startswith("DMR_")}
    env.update(script_env(drill.var, DMR_BACKUP_DIR=str(drill.backup_dir), TMPDIR=str(drill.scratch)))
    env["PATH"] = "/nonexistent"  # 没有任何 python 可找：只能用脚本里写死的绝对路径
    result = subprocess.run(["/usr/bin/env", "-i", *(f"{k}={v}" for k, v in env.items()), "/bin/bash",
                             str(DEPLOY / "backup_drill.sh")], cwd="/", capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
