"""完整内核（T0.22 装配）的端到端：真实的三个进程，经 deploy/dev_start.sh 启动，临时端口（绝不占用 18200/18210/18220）、
临时 var 目录、回放行情（DMR_STANDIN_FEED=replay）；内核不再是空壳。

整个模块共用一套已启动的部署（模块级夹具），测试按文件顺序执行，最后一个测试停掉全部并检查清理结果；夹具收尾时无论如何
都会把漏网的进程强杀。涵盖：dev_status 全部健康、内核 /api/v1/health、命令行建操作员（口令只在 0600 文件里）后经控制接口登录并读
/state（env=standin、gate=stopped、恢复已完成）、定时作业在跑、kill -9 内核后守护拉起并再次健康、SIGTERM 后 30 秒内按顺序关停。
还有三个不需要整套部署的：非替身环境拒绝启动（退出码 2）、裸内核收到 SIGTERM 干净退出、端口被占用时内核按顺序关停并以退出码 1 退出。

注意：内核启动要经过重启恢复（core.recovery）。恢复没有完成或有缺陷时，这里的用例会因内核起不来而失败，请先看 var/log/kernel.log。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest
from deploy_kit import (KERNEL_PY, LOOPBACK_ADDRS, NAMES, WEBUI_ROOT, Stack, free_ports, http_get, http_json, leftover_pids,
                        listening_addresses, parent_pid, pid_alive, port_is_free, script_env, wait_until)

pytestmark = pytest.mark.e2e

OPERATOR = "alice"
SHUTDOWN_PHASES = ("stop_accepting", "drain_intents", "close_stream", "close_exchange", "checkpoint_close_db")
SHARED: dict[str, str] = {}  # 模块内共享：创建操作员时得到的口令（只存在测试进程的内存和临时目录里）


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    deployment = Stack(tmp_path_factory.mktemp("kernel_full"))
    try:
        deployment.start_result = deployment.run("dev_start.sh", timeout=240)
        yield deployment
    finally:
        deployment.shutdown()


def api(stack: Stack) -> str:
    return stack.url("kernel") + "/api/v1"


def codes_of(state: dict) -> list[str]:
    return [item["code"] for item in state["model"]["suppressions"]]


def read_state(stack: Stack, token: str) -> dict | None:
    status, body = http_json("GET", api(stack) + "/state", token=token)
    return body if status == 200 and isinstance(body, dict) else None


def recovered(stack: Stack, token: str) -> bool:
    state = read_state(stack, token)
    return state is not None and "recovering" not in codes_of(state)


def login(stack: Stack) -> str:
    status, body = http_json("POST", api(stack) + "/auth/login", {"username": OPERATOR, "password": SHARED["password"]})
    assert status == 200, body
    return body["token"]


def local_token(stack: Stack) -> str:
    return (stack.var / "kernel" / "local.token").read_text(encoding="utf-8").strip()


def heartbeat(stack: Stack) -> dict | None:
    try:
        return json.loads((stack.var / "kernel" / "heartbeat.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lock_is_free(path: Path) -> bool:
    """内核单实例锁（var/kernel.lock）现在能不能拿到：拿得到说明没有内核在持有它。"""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(fd)  # 关闭文件描述符同时释放刚拿到的锁
    return True


def run_module(stack_env: dict[str, str], *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(KERNEL_PY), *args], env=stack_env, cwd=WEBUI_ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


# ------------------------------------------------------------------ 启动

def test_start_brings_up_the_full_kernel_with_everything_healthy(stack):
    result = stack.start_result
    assert result.returncode == 0, result.stdout + result.stderr
    assert (stack.run_dir / "kernel.mode").read_text().strip() == "full"
    status = stack.run("dev_status.sh")
    assert status.returncode == 0, status.stdout + status.stderr
    for line in status.stdout.splitlines():
        first = line.split(maxsplit=1)[0] if line.strip() else ""
        if first in ("standin", "kernel", "supervise", "console"):
            assert "alive=yes" in line and (first == "supervise" or "health=ok" in line), line
    for name in NAMES:
        assert stack.url(name) in result.stdout


def test_the_kernel_answers_health_without_a_token_and_listens_on_loopback_only(stack):
    status, body = http_get(api(stack) + "/health")
    assert status == 200, body
    assert http_get(api(stack) + "/state")[0] == 401  # 其它接口都要令牌
    addresses = listening_addresses(stack.ports["kernel"])
    assert addresses and addresses <= LOOPBACK_ADDRS, addresses
    kernel = stack.pid("kernel")
    assert (stack.var / "kernel.lock").read_text().strip() == str(kernel)
    log = stack.read_log("kernel.log")
    assert "内核已启动" in log and "重启恢复完成" in log, log[-2000:]
    assert local_token(stack) and (stack.var / "kernel" / "local.token").stat().st_mode & 0o777 == 0o600


def test_the_kernel_writes_its_heartbeat_from_the_start(stack):
    beat = heartbeat(stack)
    assert beat is not None and beat["pid"] == stack.pid("kernel")
    assert abs(beat["at_ms"] - time.time() * 1000) < 60_000
    assert (stack.var / "kernel" / "heartbeat.json").stat().st_mode & 0o777 == 0o600


def test_a_second_kernel_on_the_same_var_refuses_to_start(stack):
    first = stack.pid("kernel")
    started = time.monotonic()
    result = run_module(stack.env, "-m", "dmr_kernel")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "另一个内核正在运行" in result.stderr
    assert time.monotonic() - started < 30
    assert stack.pid("kernel") == first and pid_alive(first), "后来的那个不能影响正在运行的内核"


# ------------------------------------------------------------------ 操作员、登录、/state

def test_create_operator_then_login_and_read_the_state(stack):
    created = run_module(stack.env, "-m", "dmr_kernel.cli", "create-operator", "--username", OPERATOR)
    assert created.returncode == 0, created.stdout + created.stderr
    password_file = stack.var / "operators" / f"{OPERATOR}.password"
    assert str(password_file) in created.stdout
    assert password_file.stat().st_mode & 0o777 == 0o600
    password = password_file.read_text(encoding="utf-8").strip()
    assert password and password not in created.stdout + created.stderr, "口令只在 0600 文件里，终端不显示"
    SHARED["password"] = password

    token = login(stack)
    wait_until(lambda: recovered(stack, token), 60, interval=0.5, what="恢复完成（/state 里没有「恢复中」抑制）")
    state = read_state(stack, token)
    assert state["env"] == "standin" and state["standin"] is True
    assert state["model"]["gate"] == "stopped"
    assert state["me"]["operator"] == OPERATOR
    assert "recovering" not in codes_of(state)
    assert password not in stack.read_log("kernel.log"), "口令不能进内核日志"
    cli = run_module(stack.env, "-m", "dmr_kernel.cli", "status")  # 命令行的状态也能读（本机令牌）
    assert cli.returncode == 0, cli.stdout + cli.stderr


def test_the_scheduled_jobs_are_running_and_feed_the_state(stack):
    token = login(stack)

    def jobs_fed() -> bool:
        state = read_state(stack, token)
        return (state is not None and state["reconcile"]["positions"].get("status") == "ok"
                and state["stream"].get("state") == "connected")

    wait_until(jobs_fed, 45, interval=1.0, what="持仓对账跑过且数据流已连上")
    state = read_state(stack, token)
    assert state["position_mode"] == {"known": True, "dual_side": True}
    assert state["restrictions"] == {"known": True, "restricted": False}
    assert state["account"]["known"] is True
    assert state["heartbeat_ms"] is not None and abs(state["heartbeat_ms"] - state["time_ms"]) < 60_000


# ------------------------------------------------------------------ 守护

def test_kill_nine_on_the_kernel_is_restarted_by_the_supervisor_and_comes_back_healthy(stack):
    old_kernel, supervisor = stack.pid("kernel"), stack.pid("supervise")
    os.kill(old_kernel, signal.SIGKILL)
    started = time.monotonic()
    wait_until(lambda: stack.pid("kernel") not in (None, old_kernel) and pid_alive(stack.pid("kernel")), 15,
               what="守护拉起新的内核进程")
    new_kernel = stack.pid("kernel")
    assert not pid_alive(old_kernel)
    assert stack.pid("supervise") == supervisor and parent_pid(new_kernel) == supervisor
    wait_until(lambda: http_get(api(stack) + "/health")[0] == 200, 60, interval=0.5, what="新内核的 /health")
    wait_until(lambda: stack.run("dev_status.sh").returncode == 0, 60, interval=1.0, what="dev_status 全部健康")
    assert time.monotonic() - started < 90
    token = login(stack)  # 账本还在：同一个操作员、同一个口令
    wait_until(lambda: recovered(stack, token), 60, interval=0.5, what="新内核的恢复完成")
    state = read_state(stack, token)
    assert state["model"]["gate"] == "stopped" and state["env"] == "standin"
    wait_until(lambda: (heartbeat(stack) or {}).get("pid") == new_kernel, 30, what="新内核写了自己的心跳")
    assert (stack.var / "kernel.lock").read_text().strip() == str(new_kernel)
    assert "重启恢复完成" in stack.read_log("kernel.log")


# ------------------------------------------------------------------ 停止（必须是最后一个）

def test_stop_shuts_the_kernel_down_in_order_within_thirty_seconds_and_leaves_nothing_behind(stack):
    kernel = stack.pid("kernel")
    started = time.monotonic()
    result = stack.run("dev_stop.sh", timeout=120)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stdout + result.stderr
    assert not pid_alive(kernel)
    log = stack.read_log("kernel.log")
    # 被 kill -9 的那个内核没有机会记关停阶段，所以整份日志里只有最后这个内核的五个阶段，按报告 4.4 的顺序
    assert re.findall(r"shutdown phase (\w+)", log) == list(SHUTDOWN_PHASES), log[-3000:]
    assert "shutdown complete" in log
    assert elapsed < 30, f"dev_stop 用了 {elapsed:.0f} 秒；内核应在 SIGTERM 后 30 秒内收尾"
    assert "发送 SIGKILL" not in result.stdout, "内核应当自己收尾退出，而不是被强杀：" + result.stdout
    assert leftover_pids(stack.var) == []
    assert all(port_is_free(port) for port in stack.ports.values())
    assert lock_is_free(stack.var / "kernel.lock")
    assert heartbeat(stack) is not None  # 关停不删心跳文件


# ------------------------------------------------------------------ 不需要整套部署的

def test_other_environments_are_refused_with_exit_code_two(tmp_path):
    ports = free_ports(3)
    env = script_env(tmp_path / "var", dict(zip(NAMES, ports)), DMR_ENV="testnet")
    result = run_module(env, "-m", "dmr_kernel", timeout=60)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Demo Trading" in result.stderr
    assert not (tmp_path / "var" / "kernel.lock").exists() and not (tmp_path / "var" / "kernel").exists()


def start_background(args: list[str], env: dict[str, str], out: Path) -> subprocess.Popen:
    handle = open(out, "ab")
    try:
        return subprocess.Popen(args, env=env, cwd=WEBUI_ROOT, stdin=subprocess.DEVNULL, stdout=handle,
                                stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        handle.close()


def start_bare_standin(deployment: Stack, tmp_path: Path) -> subprocess.Popen:
    """只起替身交易所（内核另起）：内核装配时要读它给的访问令牌文件。"""
    deployment.log_dir.mkdir(parents=True)
    standin = start_background(
        [str(KERNEL_PY), "-m", "dmr_standin", "--host", "127.0.0.1", "--port", str(deployment.ports["standin"]),
         "--state-dir", str(deployment.var / "standin"), "--log-dir", str(deployment.log_dir), "--feed", "replay"],
        deployment.env, tmp_path / "standin.out")
    wait_until(lambda: http_get(deployment.url("standin") + "/api/public/time")[0] == 200
               and (deployment.var / "standin" / "kernel.token").exists(), 60, what="替身交易所起来")
    return standin


def stop_all(deployment: Stack, *processes: subprocess.Popen | None) -> None:
    for process in processes:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    deployment.shutdown()


def test_sigterm_to_a_bare_kernel_runs_the_shutdown_phases_and_exits_zero_within_thirty_seconds(tmp_path):
    deployment = Stack(tmp_path)
    standin = kernel = None
    try:
        standin = start_bare_standin(deployment, tmp_path)
        kernel = start_background([str(KERNEL_PY), "-m", "dmr_kernel"], deployment.env, tmp_path / "kernel.out")
        wait_until(lambda: http_get(api(deployment) + "/health")[0] == 200, 90, interval=0.5, what="内核 /health")
        token = local_token(deployment)
        wait_until(lambda: recovered(deployment, token), 60, interval=0.5, what="恢复完成")

        started = time.monotonic()
        kernel.send_signal(signal.SIGTERM)
        code = kernel.wait(timeout=30)
        elapsed = time.monotonic() - started
        assert code == 0, (tmp_path / "kernel.out").read_text(errors="replace")[-3000:]
        assert elapsed < 30
        log = deployment.read_log("kernel.log")
        assert re.findall(r"shutdown phase (\w+)", log) == list(SHUTDOWN_PHASES), log[-3000:]
        assert lock_is_free(deployment.var / "kernel.lock")
        assert port_is_free(deployment.ports["kernel"])
    finally:
        stop_all(deployment, kernel, standin)


def test_a_kernel_whose_port_is_taken_says_so_shuts_down_in_order_and_exits_with_code_one(tmp_path):
    """uvicorn 绑不上端口时调 sys.exit(1)；内核必须接住它（不能让它穿过事件循环把进程直接带走），按顺序关停并释放锁。"""
    deployment = Stack(tmp_path)
    standin = kernel = None
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        standin = start_bare_standin(deployment, tmp_path)
        squatter.bind(("127.0.0.1", deployment.ports["kernel"]))
        squatter.listen()
        kernel = start_background([str(KERNEL_PY), "-m", "dmr_kernel"], deployment.env, tmp_path / "kernel.out")
        started = time.monotonic()
        code = kernel.wait(timeout=60)
        output = (tmp_path / "kernel.out").read_text(errors="replace")
        assert code == 1, output[-3000:]
        assert time.monotonic() - started < 40
        assert "控制接口没有起来" in output and str(deployment.ports["kernel"]) in output, output[-3000:]
        log = deployment.read_log("kernel.log")
        assert re.findall(r"shutdown phase (\w+)", log) == list(SHUTDOWN_PHASES), log[-3000:]
        assert "重启恢复完成" not in log, "控制接口没起来，不往下做恢复"
        assert lock_is_free(deployment.var / "kernel.lock")
    finally:
        squatter.close()
        stop_all(deployment, kernel, standin)
