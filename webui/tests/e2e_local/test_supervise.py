"""deploy/supervise.sh：内核被杀后自动拉起；停用标记阻止拉起；连续快速失败时退避；信号转发。

用真实的内核空壳（DMR_KERNEL_SHELL=1）验证"kill -9 后拉起""停用标记"；退避与信号转发用小脚本代替内核
（supervise.sh 的 ``--`` 之后可以给要守护的命令），时间参数用环境变量缩短。
"""
from __future__ import annotations

import os
import signal
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from deploy_kit import DEPLOY, SHELL, Stack, cmdline, leftover_pids, parent_pid, pid_alive, wait_until

pytestmark = pytest.mark.e2e


@pytest.fixture
def stack(tmp_path):
    deployment = Stack(tmp_path, **SHELL)
    try:
        yield deployment
    finally:
        deployment.shutdown()


def start_supervisor(stack: Stack, *command: str, env: dict[str, str] | None = None) -> subprocess.Popen:
    stack.root.mkdir(parents=True, exist_ok=True)
    merged = {**stack.env, **(env or {})}
    args = [str(DEPLOY / "supervise.sh")] + (["--", *command] if command else [])
    out = open(stack.root / "supervisor.out", "ab")
    try:
        return subprocess.Popen(args, env=merged, cwd=stack.root, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        out.close()


def kernel_up(stack: Stack, not_pid: int | None = None) -> bool:
    pid = stack.pid("kernel")
    return bool(pid) and pid != not_pid and pid_alive(pid)


def write_script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return path


def read_stamps(path: Path) -> list[float]:
    return [int(line) / 1e9 for line in path.read_text().split()]


def gaps(stamps: list[float]) -> list[float]:
    return [later - earlier for earlier, later in zip(stamps, stamps[1:])]


# ------------------------------------------------------------------ 拉起

def test_supervisor_records_its_own_pid_and_the_kernels_and_restarts_the_kernel_after_kill_nine(stack):
    supervisor = start_supervisor(stack)
    wait_until(lambda: kernel_up(stack), 20, what="内核空壳启动")
    assert stack.pid("supervise") == supervisor.pid
    first = stack.pid("kernel")
    assert "dmr_kernel" in cmdline(first) and parent_pid(first) == supervisor.pid

    os.kill(first, signal.SIGKILL)
    started = time.monotonic()
    wait_until(lambda: kernel_up(stack, not_pid=first), 10, what="守护在 10 秒内拉起内核")
    elapsed = time.monotonic() - started
    assert elapsed >= 1.8, f"默认应隔 2 秒再拉起，实际 {elapsed:.2f} 秒（会让快速崩溃的内核空转）"
    assert pid_alive(supervisor.pid) and stack.pid("supervise") == supervisor.pid


def test_a_marker_written_before_the_kernel_dies_prevents_the_restart_even_after_kill_nine(stack):
    supervisor = start_supervisor(stack)
    wait_until(lambda: kernel_up(stack), 20, what="内核空壳启动")
    kernel = stack.pid("kernel")
    marker = stack.run_dir / "kernel.nosupervise"
    marker.write_text("emergency\n")
    os.kill(kernel, signal.SIGKILL)
    assert supervisor.wait(timeout=10) == 0, (stack.root / "supervisor.out").read_text()
    assert not pid_alive(kernel) and leftover_pids(stack.var) == [], "标记存在时不能再拉起内核"
    assert not stack.pid_file("kernel").exists() and not stack.pid_file("supervise").exists()
    assert marker.exists(), "守护不删停用标记（谁写谁删）"


def test_a_marker_written_during_the_restart_delay_is_still_honoured(stack):
    supervisor = start_supervisor(stack, env={"DMR_SUPERVISE_DELAY_S": "3"})
    wait_until(lambda: kernel_up(stack), 20, what="内核空壳启动")
    os.kill(stack.pid("kernel"), signal.SIGKILL)
    (stack.run_dir / "kernel.nosupervise").write_text("emergency\n")  # 内核刚死、守护正在等 3 秒
    assert supervisor.wait(timeout=10) == 0
    assert leftover_pids(stack.var) == [], "等待期间写的标记也要生效"
    assert not stack.pid_file("kernel").exists()


def test_the_supervisor_does_not_start_the_kernel_at_all_while_the_marker_exists(stack):
    stack.run_dir.mkdir(parents=True)
    (stack.run_dir / "kernel.nosupervise").write_text("emergency\n")
    supervisor = start_supervisor(stack)
    assert supervisor.wait(timeout=10) == 0
    assert not stack.pid_file("kernel").exists() and not stack.pid_file("supervise").exists()
    assert leftover_pids(stack.var) == []
    assert "kernel.nosupervise" in (stack.root / "supervisor.out").read_text()


# ------------------------------------------------------------------ 退避

def counting_child(stack: Stack, *, slow_run: int | None = None, slow_s: float = 0.0) -> Path:
    """每次被拉起就在 starts.txt 里记一个纳秒时间戳然后失败退出；第 slow_run 次（从 0 数）先睡 slow_s 秒。"""
    starts = stack.root / "starts.txt"
    count = stack.root / "count.txt"
    slow = f'[ "$n" = "{slow_run}" ] && sleep {slow_s}' if slow_run is not None else ":"
    return write_script(stack.root / "failing_child.sh", f"""
        n=$(wc -l < "{count}" 2>/dev/null || echo 0)
        echo x >> "{count}"
        date +%s%N >> "{starts}"
        {slow}
        exit 1
    """)


def stop_supervisor(supervisor: subprocess.Popen) -> None:
    supervisor.send_signal(signal.SIGTERM)
    assert supervisor.wait(timeout=10) == 0


def test_consecutive_fast_failures_back_off_exponentially_up_to_the_cap(stack):
    child = counting_child(stack)
    supervisor = start_supervisor(stack, str(child), env={
        "DMR_SUPERVISE_DELAY_S": "0.3", "DMR_SUPERVISE_FAST_S": "30", "DMR_SUPERVISE_MAX_BACKOFF_S": "1.2"})
    starts = stack.root / "starts.txt"
    try:
        wait_until(lambda: starts.exists() and len(read_stamps(starts)) >= 6, 30, what="至少拉起 6 次")
    finally:
        stop_supervisor(supervisor)
    observed = gaps(read_stamps(starts))[:5]
    for gap, want in zip(observed, [0.3, 0.6, 1.2, 1.2, 1.2]):  # 每次翻倍，封顶 1.2 秒
        assert want - 0.05 <= gap <= want + 0.8, f"间隔 {observed}，期望 0.3, 0.6, 1.2, 1.2, 1.2"


def test_a_run_that_lasts_long_enough_resets_the_backoff(stack):
    child = counting_child(stack, slow_run=2, slow_s=0.9)
    supervisor = start_supervisor(stack, str(child), env={
        "DMR_SUPERVISE_DELAY_S": "0.3", "DMR_SUPERVISE_FAST_S": "0.6", "DMR_SUPERVISE_MAX_BACKOFF_S": "5"})
    starts = stack.root / "starts.txt"
    try:
        wait_until(lambda: starts.exists() and len(read_stamps(starts)) >= 5, 30, what="至少拉起 5 次")
    finally:
        stop_supervisor(supervisor)
    g1, g2, g3, g4 = gaps(read_stamps(starts))[:4]
    assert 0.25 <= g1 <= 1.0 and 0.55 <= g2 <= 1.4, "前两次快速失败：0.3 秒、0.6 秒"
    assert 1.15 <= g3 <= 1.9, f"第三次运行了 0.9 秒（超过快速失败门限）：退避清零，只等 0.3 秒；实际 {g3:.2f}（含运行 0.9）"
    assert 0.25 <= g4 <= 0.9, f"清零后再失败一次：只等 0.3 秒；实际 {g4:.2f}（没清零会是 2 秒以上）"


# ------------------------------------------------------------------ 环境

def test_supervised_children_never_inherit_pytest_or_nicegui_test_mode_markers(stack):
    """NiceGUI 在环境里看到 PYTEST_CURRENT_TEST 就改走"屏幕测试"模式并要求 NICEGUI_SCREEN_TEST_PORT，没有就在 ui.run 里崩溃。
    开发脚本要能从 pytest 进程里运行（e2e 就是这样），所以脚本自己把这些标记清掉，不能指望调用方（dev_start.sh 起控制台走同一个 lib.sh）。"""
    seen = stack.root / "seen_env.txt"
    marker = stack.run_dir / "kernel.nosupervise"
    child = write_script(stack.root / "env_child.sh", f"""
        {{ echo "pytest=${{PYTEST_CURRENT_TEST-unset}}"
           echo "screen_port=${{NICEGUI_SCREEN_TEST_PORT-unset}}"
           echo "user_sim=${{NICEGUI_USER_SIMULATION-unset}}"
           echo "keep=${{DMR_VAR_DIR-unset}}"; }} > "{seen}"
        echo done > "{marker}"
        exit 0
    """)
    supervisor = start_supervisor(stack, str(child), env={
        "PYTEST_CURRENT_TEST": "tests/e2e_local/test_x.py::test_y (call)", "NICEGUI_SCREEN_TEST_PORT": "4321",
        "NICEGUI_USER_SIMULATION": "1"})
    assert supervisor.wait(timeout=15) == 0, (stack.root / "supervisor.out").read_text()
    assert seen.read_text().splitlines() == ["pytest=unset", "screen_port=unset", "user_sim=unset", f"keep={stack.var}"]


# ------------------------------------------------------------------ 信号

def test_sigterm_to_the_supervisor_stops_the_child_and_nothing_is_restarted(stack):
    supervisor = start_supervisor(stack, "sleep", "300")
    wait_until(lambda: kernel_up(stack), 10, what="被守护的命令启动")
    child = stack.pid("kernel")
    supervisor.send_signal(signal.SIGTERM)
    assert supervisor.wait(timeout=10) == 0
    assert not pid_alive(child)
    assert not stack.pid_file("kernel").exists() and not stack.pid_file("supervise").exists()
    assert leftover_pids(stack.var) == []


def test_a_child_that_ignores_sigterm_is_killed_after_the_grace_period(stack):
    stubborn = write_script(stack.root / "stubborn_child.sh", """
        trap '' TERM
        while :; do sleep 0.1; done
    """)
    supervisor = start_supervisor(stack, str(stubborn), env={"DMR_SUPERVISE_STOP_GRACE_S": "1"})
    wait_until(lambda: kernel_up(stack), 10, what="被守护的命令启动")
    child = stack.pid("kernel")
    started = time.monotonic()
    supervisor.send_signal(signal.SIGTERM)
    assert supervisor.wait(timeout=15) == 0
    assert 0.8 <= time.monotonic() - started < 10
    wait_until(lambda: not pid_alive(child), 5, what="顽固的子进程被强杀")
    assert not stack.pid_file("kernel").exists() and not stack.pid_file("supervise").exists()
