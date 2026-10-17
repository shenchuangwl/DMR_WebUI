"""开发脚本的边界与保护：配置校验、停用标记、停止顺序与强杀、过期进程号文件。

这些测试不启动真实的三个服务：校验类用例在任何进程启动之前就应当被拒绝；停止类用例用假进程
（命令行里带 dmr_standin / dmr_kernel / dmr_console 的 Python 小脚本）代替服务，检查 dev_stop.sh 的行为。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from deploy_kit import DEPLOY, SHELL, WEBUI_ROOT, Stack, cmdline, leftover_pids, pid_alive, wait_until

pytestmark = pytest.mark.e2e


@pytest.fixture
def stack(tmp_path):
    deployment = Stack(tmp_path, **SHELL)
    try:
        yield deployment
    finally:
        deployment.shutdown()


# ------------------------------------------------------------------ 配置校验：任何进程启动之前就拒绝

@pytest.mark.parametrize("var, value, mention", [
    ("DMR_STANDIN_PORT", "abc", "DMR_STANDIN_PORT"),
    ("DMR_KERNEL_PORT", "0", "DMR_KERNEL_PORT"),
    ("DMR_CONSOLE_PORT", "70000", "DMR_CONSOLE_PORT"),
    ("DMR_CONSOLE_PORT", "-5", "DMR_CONSOLE_PORT"),
    ("DMR_STANDIN_FEED", "live", "DMR_STANDIN_FEED"),
    ("DMR_ENV", "mainnet_live", "DMR_ENV"),
    ("DMR_ENV", "testnet", "DMR_ENV"),
    ("DMR_KERNEL_HOST", "0.0.0.0", "DMR_KERNEL_HOST"),
    ("DMR_CONSOLE_HOST", "192.168.1.5", "DMR_CONSOLE_HOST"),
])
def test_invalid_configuration_is_refused_before_anything_starts(stack, var, value, mention):
    result = stack.run("dev_start.sh", env={var: value})
    assert result.returncode == 2, result.stdout + result.stderr
    assert mention in result.stderr, result.stderr
    assert not list(stack.run_dir.glob("*.pid")) if stack.run_dir.exists() else True
    assert not stack.read_log("standin.log")


def test_two_services_on_the_same_port_are_refused(stack):
    same = str(stack.ports["standin"])
    result = stack.run("dev_start.sh", env={"DMR_KERNEL_PORT": same})
    assert result.returncode == 2 and "端口" in result.stderr, result.stdout + result.stderr
    assert not list(stack.run_dir.glob("*.pid")) if stack.run_dir.exists() else True


def test_a_port_that_is_already_taken_is_reported_without_starting_anything_else(stack):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", stack.ports["standin"]))
        blocker.listen(1)
        result = stack.run("dev_start.sh")
    assert result.returncode == 1, result.stdout + result.stderr
    assert str(stack.ports["standin"]) in result.stderr and "占用" in result.stderr
    assert stack.pid("kernel") is None and stack.pid("console") is None, "替身起不来时不应继续启动内核与控制台"


def test_the_nosupervise_marker_blocks_a_fresh_start(stack):
    stack.run_dir.mkdir(parents=True)
    marker = stack.run_dir / "kernel.nosupervise"
    marker.write_text("emergency\n")
    result = stack.run("dev_start.sh")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "kernel.nosupervise" in result.stderr
    assert not list(stack.run_dir.glob("*.pid")), "拒绝启动时不应已经起了任何进程"
    assert marker.exists(), "启动脚本不应擅自删掉别人写的停用标记"


def test_a_kernel_that_crashes_at_startup_fails_dev_start_quickly_and_leaves_no_supervisor_behind(stack):
    started = time.monotonic()
    result = stack.run("dev_start.sh", env={"DMR_INSTANCE_ID": "TOOLONG", "DMR_DEV_START_TIMEOUT_S": "30"})
    elapsed = time.monotonic() - started
    assert result.returncode == 1, result.stdout + result.stderr
    assert elapsed < 20, f"内核一启动就退出：应当马上失败，而不是等满 30 秒（实际 {elapsed:.0f} 秒；守护会一直重试）"
    assert "交易内核" in result.stderr and "DMR_INSTANCE_ID" in result.stderr, "要把内核日志末尾（崩溃原因）打印出来：" + result.stderr
    assert stack.pid("console") is None, "内核没起来就不该再启动控制台"
    assert stack.pid("kernel") is None and stack.pid("supervise") is None, "失败后不能留下崩溃循环里的守护"
    assert not (stack.run_dir / "kernel.nosupervise").exists(), "清理时写的停用标记要随之清除，否则下次启动被拒绝"
    survivors = {cmdline(pid).split()[1] if len(cmdline(pid).split()) > 1 else cmdline(pid) for pid in leftover_pids(stack.var)}
    assert not any("supervise.sh" in text or "dmr_kernel" in text for text in survivors), survivors
    again = stack.run("dev_start.sh")  # 配置改对之后可以直接再启动：替身交易所还在，只补启动内核与控制台
    assert again.returncode == 0, again.stdout + again.stderr
    assert "已在运行" in again.stdout and stack.pid("kernel") and pid_alive(stack.pid("kernel"))


def test_status_with_nothing_running_fails_and_says_so(stack):
    result = stack.run("dev_status.sh")
    assert result.returncode == 1
    assert result.stdout.count("alive=no") >= 3 and "未运行" in result.stdout


# ------------------------------------------------------------------ 缺省值与命令行

def loaded_config(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """在干净的环境里 source lib.sh 并运行 load_config（只读环境变量，不创建目录、不启动、不绑端口），返回导出的配置。"""
    names = ("DMR_STANDIN_PORT", "DMR_KERNEL_PORT", "DMR_CONSOLE_PORT", "DMR_STANDIN_FEED", "DMR_VAR_DIR", "DMR_KERNEL_HOST",
             "DMR_CONSOLE_HOST", "DMR_STANDIN_URL", "DMR_STANDIN_TOKEN_FILE", "DMR_ENV")
    script = f'. "{DEPLOY}/lib.sh"; load_config; ' + "; ".join(f'printf "%s=%s\\n" {name} "${name}"' for name in names)
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **(extra_env or {})}
    result = subprocess.run(["/bin/bash", "-c", script], env=env, cwd="/", capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def test_defaults_are_the_formal_ports_the_project_var_dir_and_the_auto_feed():
    config = loaded_config()
    assert config == {
        "DMR_STANDIN_PORT": "18220", "DMR_KERNEL_PORT": "18200", "DMR_CONSOLE_PORT": "18210",
        "DMR_STANDIN_FEED": "auto", "DMR_VAR_DIR": str(WEBUI_ROOT / "var"), "DMR_KERNEL_HOST": "127.0.0.1",
        "DMR_CONSOLE_HOST": "127.0.0.1", "DMR_STANDIN_URL": "http://127.0.0.1:18220",
        "DMR_STANDIN_TOKEN_FILE": str(WEBUI_ROOT / "var" / "standin" / "kernel.token"), "DMR_ENV": "standin"}


def test_environment_overrides_are_normalised_and_handed_to_the_children():
    config = loaded_config({"DMR_STANDIN_PORT": "08080", "DMR_KERNEL_PORT": "9001", "DMR_CONSOLE_PORT": "9002",
                            "DMR_STANDIN_FEED": "replay", "DMR_VAR_DIR": "rel/var//"})
    assert config["DMR_STANDIN_PORT"] == "8080", "前导零按十进制，不当八进制"
    assert (config["DMR_KERNEL_PORT"], config["DMR_CONSOLE_PORT"], config["DMR_STANDIN_FEED"]) == ("9001", "9002", "replay")
    assert config["DMR_VAR_DIR"] == str(WEBUI_ROOT / "rel" / "var"), "相对路径按工程根解析，去掉结尾的斜杠"
    assert config["DMR_STANDIN_URL"] == "http://127.0.0.1:8080", "内核连的替身地址跟着替身端口走"
    assert config["DMR_STANDIN_TOKEN_FILE"] == str(WEBUI_ROOT / "rel" / "var" / "standin" / "kernel.token")


def test_live_switch_is_cleared_and_hosts_are_forced_to_loopback():
    script = (f'. "{DEPLOY}/lib.sh"; load_config; printf "[%s][%s][%s]" "${{DMR_ALLOW_LIVE-unset}}" "$DMR_KERNEL_HOST" '
              '"$DMR_CONSOLE_HOST"')
    result = subprocess.run(["/bin/bash", "-c", script], env={"PATH": "/usr/bin:/bin", "DMR_ALLOW_LIVE": "1",
                                                              "DMR_KERNEL_HOST": "localhost"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "[unset][127.0.0.1][127.0.0.1]", "DMR_ALLOW_LIVE 要被清除；回环主机统一成 127.0.0.1"


ENTRY_SCRIPTS = ["dev_start.sh", "dev_stop.sh", "dev_status.sh", "supervise.sh", "backup_drill.sh"]


@pytest.mark.parametrize("script", ENTRY_SCRIPTS)
def test_help_prints_the_usage_and_creates_nothing(stack, script):
    result = stack.run(script, "--help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"deploy/{script}" in result.stdout and "set -euo" not in result.stdout, result.stdout
    assert not stack.var.exists(), "--help 不应创建 var 目录"


@pytest.mark.parametrize("script", ENTRY_SCRIPTS)
def test_unknown_arguments_are_a_usage_error_and_start_nothing(stack, script):
    result = stack.run(script, "--bogus-option")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--bogus-option" in result.stderr or "bogus" in result.stderr, result.stderr
    assert not stack.run_dir.exists() or not list(stack.run_dir.glob("*.pid"))
    assert not stack.pid_file("kernel").exists()


# ------------------------------------------------------------------ 停止：用假进程检查顺序、停用标记、强杀

FAKE_SERVICE = textwrap.dedent("""
    import os, signal, sys, time
    name, report, marker, mode = sys.argv[1:5]
    ballast = bytearray(int(sys.argv[5]) * 1024 * 1024) if len(sys.argv) > 5 else b""
    for offset in range(0, len(ballast), 4096):
        ballast[offset] = 1  # 真正占用内存：退出时释放它需要一段时间

    def on_term(signum, frame):
        with open(report, "a") as out:
            out.write(f"{name} {time.time_ns()} marker={os.path.exists(marker)}\\n")
        if mode == "cooperative":
            os._exit(0)

    signal.signal(signal.SIGTERM, on_term)
    open(f"{report}.{name}.ready", "w").close()
    while True:
        time.sleep(0.05)
""")


def start_fake(stack: Stack, name: str, *, mode: str = "cooperative", ballast_mb: int = 0) -> subprocess.Popen:
    """起一个假服务：命令行里带 dmr_<name>（dev_stop 据此确认进程号文件没有指错进程），收到 SIGTERM 时记录时间与停用标记在不在。
    ballast_mb > 0 时先占住这么多内存（真的写过每一页）。"""
    script = stack.root / f"fake_dmr_{name}.py"
    script.write_text(FAKE_SERVICE)
    report = stack.root / "terminations.txt"
    marker = stack.run_dir / "kernel.nosupervise"
    stack.run_dir.mkdir(parents=True, exist_ok=True)
    extra = [str(ballast_mb)] if ballast_mb else []
    proc = subprocess.Popen([sys.executable, str(script), name, str(report), str(marker), mode, *extra],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_until(lambda: Path(f"{report}.{name}.ready").exists(), 20, what=f"假 {name} 就绪")
    stack.pid_file(name).write_text(f"{proc.pid}\n")
    return proc


def test_stop_order_and_the_marker_is_written_before_the_kernel_gets_sigterm(stack):
    procs = {name: start_fake(stack, name) for name in ("standin", "kernel", "console")}
    try:
        result = stack.run("dev_stop.sh")
        assert result.returncode == 0, result.stdout + result.stderr
        records = [line.split() for line in (stack.root / "terminations.txt").read_text().splitlines()]
        stamps = {name: int(stamp) for name, stamp, _ in records}
        assert stamps["console"] < stamps["kernel"] < stamps["standin"], "应按 控制台 → 内核 → 替身 的顺序停"
        marker_seen = {name: flag for name, _, flag in records}
        assert marker_seen["kernel"] == "marker=True", "先写停用标记，再给内核发 SIGTERM"
        for proc in procs.values():
            assert proc.wait(timeout=10) == 0
        assert not (stack.run_dir / "kernel.nosupervise").exists()
        assert not list(stack.run_dir.glob("*.pid"))
    finally:
        for proc in procs.values():
            proc.kill()
            proc.wait()


def test_a_kernel_that_ignores_sigterm_is_killed_after_the_timeout(stack):
    kernel = start_fake(stack, "kernel", mode="stubborn")
    try:
        started = time.monotonic()
        result = stack.run("dev_stop.sh", env={"DMR_DEV_KERNEL_STOP_TIMEOUT_S": "1"})
        elapsed = time.monotonic() - started
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SIGKILL" in result.stdout, result.stdout
        assert 1 <= elapsed < 15, f"应在约 1 秒超时后强杀，实际 {elapsed:.1f} 秒"
        assert kernel.wait(timeout=5) == -9, "内核应被 SIGKILL 结束"
        assert not stack.pid_file("kernel").exists()
    finally:
        kernel.kill()
        kernel.wait()


def test_dev_stop_returns_only_after_a_memory_heavy_process_has_fully_exited(stack):
    """进程退出时先释放内存：这段时间里 /proc/<pid>/cmdline 是空的，进程却还没变成僵尸，套接字也还没关（实测 512MiB 约 30 毫秒，
    真服务在 swap 紧张的机器上更久）。等进程退出的循环若把"读不到命令行"当成"已经走了"，dev_stop 就会提前返回，
    紧接着的 dev_start 会撞上还没释放的端口。"""
    standin = start_fake(stack, "standin", ballast_mb=512)
    try:
        result = stack.run("dev_stop.sh")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not pid_alive(standin.pid), "dev_stop 返回时进程应已彻底退出（不是还在释放内存）"
        assert not stack.pid_file("standin").exists()
    finally:
        standin.kill()
        standin.wait()


def test_stale_pid_files_are_cleaned_without_killing_unrelated_processes(stack):
    stack.run_dir.mkdir(parents=True)
    innocent = subprocess.Popen(["sleep", "60"])  # 进程号被别的程序复用的情形：活着，但不是替身交易所
    try:
        stack.pid_file("standin").write_text(f"{innocent.pid}\n")
        stack.pid_file("kernel").write_text("999999\n")  # 不存在的进程
        stack.pid_file("console").write_text("not-a-pid\n")
        result = stack.run("dev_stop.sh")
        assert result.returncode == 0, result.stdout + result.stderr
        assert pid_alive(innocent.pid), "进程号文件指向的不是替身交易所，dev_stop 不能杀它"
        assert not list(stack.run_dir.glob("*.pid")), "过期的进程号文件应被清理"
    finally:
        innocent.kill()
        innocent.wait()


def test_dev_scripts_can_be_run_from_any_directory_and_through_a_symlink(stack, tmp_path):
    link = tmp_path / "linked_status.sh"
    link.symlink_to(Path(__file__).resolve().parents[2] / "deploy" / "dev_status.sh")
    result = subprocess.run([str(link)], env=stack.env, cwd="/", capture_output=True, text=True)
    assert result.returncode == 1 and "standin" in result.stdout, result.stdout + result.stderr
