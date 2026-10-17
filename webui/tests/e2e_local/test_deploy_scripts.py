"""开发用启停脚本（deploy/dev_start.sh、dev_stop.sh、dev_status.sh、supervise.sh）的端到端测试。

真实进程，临时端口（绝不占用 18200/18210/18220）与临时 var 目录，回放行情（DMR_STANDIN_FEED=replay，不连 Binance），
内核以空壳运行（DMR_KERNEL_SHELL=1：这里只验证脚本本身，空壳更快、不依赖业务装配；脚本要把这个变量传下去）。
完整内核（T0.22 的装配）的端到端见 test_kernel_full.py。

整个模块共用一套已启动的部署（模块级夹具）；测试按文件顺序执行，改变状态的测试自己恢复原状，
最后一个测试停掉全部并检查清理结果。夹具收尾时无论如何都会把漏网的进程强杀。
"""
from __future__ import annotations

import os
import re
import signal
import time

import pytest
from deploy_kit import (CONSOLE_PY, KERNEL_PY, LOOPBACK_ADDRS, NAMES, SHELL, UTC_LINE, Stack, cmdline, http_get,
                        leftover_pids, listening_addresses, parent_pid, pid_alive, port_is_free, wait_until)

pytestmark = pytest.mark.e2e

EXPECTED_CMD = {"standin": "dmr_standin", "kernel": "dmr_kernel", "supervise": "supervise.sh", "console": "dmr_console"}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    deployment = Stack(tmp_path_factory.mktemp("deploy_stack"), **SHELL)
    try:
        deployment.start_result = deployment.run("dev_start.sh", timeout=180)
        yield deployment
    finally:
        deployment.shutdown()


def status_lines(output: str) -> dict[str, str]:
    """dev_start/dev_status 打印的状态行：每行以进程名开头（standin、kernel、supervise、console）。"""
    lines = {}
    for line in output.splitlines():
        first = line.split(maxsplit=1)[0] if line.strip() else ""
        if first in EXPECTED_CMD:
            lines[first] = line
    return lines


# ------------------------------------------------------------------ 启动

def test_start_succeeds_and_prints_the_three_addresses_with_their_status(stack):
    result = stack.start_result
    assert result.returncode == 0, result.stdout + result.stderr
    for name in NAMES:
        assert stack.url(name) in result.stdout, f"{name} 的地址没有打印"
    lines = status_lines(result.stdout)
    assert set(lines) == set(EXPECTED_CMD), result.stdout
    for name, line in lines.items():
        assert "alive=yes" in line, line
    for name in NAMES:
        assert "health=ok" in lines[name], lines[name]


def test_pid_files_logs_and_interpreters_are_where_the_conventions_say(stack):
    for name, needle in EXPECTED_CMD.items():
        pid = stack.pid(name)
        assert pid and pid_alive(pid), f"{name}.pid 缺失或进程不在"
        assert needle in cmdline(pid), (name, cmdline(pid))
    # 解释器一律是本工程的两个环境，绝对路径
    assert cmdline(stack.pid("standin")).split()[0] == str(KERNEL_PY)
    assert cmdline(stack.pid("kernel")).split()[0] == str(KERNEL_PY)
    assert cmdline(stack.pid("console")).split()[0] == str(CONSOLE_PY)
    # 内核由守护拉起，三个后台进程都脱离了启动它们的会话
    assert parent_pid(stack.pid("kernel")) == stack.pid("supervise")
    for name in ("standin", "supervise", "console"):
        assert os.getsid(stack.pid(name)) == stack.pid(name), f"{name} 没有用独立会话脱离终端"
    # 日志在 var/log/，时间是 UTC
    for name in ("standin.log", "kernel.log", "console.log"):
        text = stack.read_log(name)
        assert re.search(UTC_LINE, text, re.M), f"{name} 里没有 UTC 时间戳开头的日志行：{text[:200]!r}"
    assert stack.log_dir.is_dir() and stack.run_dir.is_dir()
    # 起了什么、用了哪些环境变量：内核空壳标志传了下去
    assert "kernel shell ready" in stack.read_log("kernel.log")


def test_status_script_reports_every_process_healthy(stack):
    result = stack.run("dev_status.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = status_lines(result.stdout)
    assert set(lines) == set(EXPECTED_CMD)
    for name in EXPECTED_CMD:
        assert "alive=yes" in lines[name], lines[name]
        assert f"pid={stack.pid(name)}" in lines[name]
    for name in NAMES:
        assert "health=ok" in lines[name], lines[name]
    # 健康检查背后的事实：替身交易所的公共时间接口、控制台首页（登录页）确实在回应
    status, body = http_get(stack.url("standin") + "/api/public/time")
    assert status == 200 and "server_time_ms" in body
    assert http_get(stack.url("console") + "/")[0] == 200
    # 空壳内核没有控制接口，只持有单实例锁
    assert listening_addresses(stack.ports["kernel"]) == set()
    assert (stack.var / "kernel.lock").read_text().strip() == str(stack.pid("kernel"))


def test_services_listen_on_loopback_only(stack):
    for name in ("standin", "console"):
        addresses = listening_addresses(stack.ports[name])
        assert addresses, f"{name} 没有在端口 {stack.ports[name]} 上监听"
        assert addresses <= LOOPBACK_ADDRS, f"{name} 监听了非回环地址：{addresses}"


def test_starting_again_does_not_start_duplicates(stack):
    before_pids = {name: stack.pid(name) for name in EXPECTED_CMD}
    before_all = set(leftover_pids(stack.var))
    result = stack.run("dev_start.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("已在运行") >= 3, result.stdout
    assert {name: stack.pid(name) for name in EXPECTED_CMD} == before_pids
    assert set(leftover_pids(stack.var)) == before_all, "重复启动留下了额外的进程"


# ------------------------------------------------------------------ 守护

def test_kill_nine_on_the_kernel_is_restarted_by_the_supervisor_within_ten_seconds(stack):
    old_kernel, supervisor = stack.pid("kernel"), stack.pid("supervise")
    os.kill(old_kernel, signal.SIGKILL)
    started = time.monotonic()
    wait_until(lambda: stack.pid("kernel") not in (None, old_kernel) and pid_alive(stack.pid("kernel")), 10,
               what="守护拉起新的内核进程")
    assert time.monotonic() - started < 10
    new_kernel = stack.pid("kernel")
    assert not pid_alive(old_kernel)
    assert stack.pid("supervise") == supervisor and pid_alive(supervisor), "守护自己不应该变"
    assert "dmr_kernel" in cmdline(new_kernel) and parent_pid(new_kernel) == supervisor
    wait_until(lambda: stack.run("dev_status.sh").returncode == 0, 20, interval=0.5, what="dev_status 重新报告健康")
    log = stack.read_log("supervise.log")
    assert str(old_kernel) in log and str(new_kernel) in log, log


def test_restarting_the_console_does_not_touch_the_kernel_or_the_standin(stack):
    others = {name: stack.pid(name) for name in ("standin", "kernel", "supervise")}
    old_console = stack.pid("console")
    os.kill(old_console, signal.SIGTERM)
    wait_until(lambda: not pid_alive(old_console), 20, what="控制台退出")
    assert all(pid_alive(pid) for pid in others.values()), "停控制台不应影响内核与替身交易所"
    assert (stack.var / "kernel.lock").read_text().strip() == str(others["kernel"])

    result = stack.run("dev_start.sh")  # 进程号文件已过期：只补启动控制台
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("已在运行") >= 2, result.stdout
    new_console = stack.pid("console")
    assert new_console and new_console != old_console and pid_alive(new_console)
    assert {name: stack.pid(name) for name in others} == others
    assert http_get(stack.url("console") + "/")[0] == 200


# ------------------------------------------------------------------ 停止（必须是最后一个）

def test_stop_takes_console_then_kernel_then_standin_down_and_cleans_up(stack):
    pids = {name: stack.pid(name) for name in EXPECTED_CMD}
    assert all(pid_alive(pid) for pid in pids.values())

    result = stack.run("dev_stop.sh", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    order = [result.stdout.index(text) for text in ("停止 Web 控制台", "停止 交易内核", "停止 替身交易所")]
    assert order == sorted(order), f"停止顺序应为 控制台 → 内核 → 替身：\n{result.stdout}"
    for name, pid in pids.items():
        assert not pid_alive(pid), f"{name}（{pid}）在 dev_stop 返回后仍在运行"
    assert leftover_pids(stack.var) == [], "还有带本部署环境变量的进程"
    assert not list(stack.run_dir.glob("*.pid")), "进程号文件没有清理"
    assert not (stack.run_dir / "kernel.nosupervise").exists(), "停止完成后应清除停用标记，下次启动才能正常拉起"
    assert all(port_is_free(port) for port in stack.ports.values())

    status = stack.run("dev_status.sh")
    assert status.returncode == 1 and "alive=no" in status.stdout
    assert stack.run("dev_stop.sh").returncode == 0, "没有东西在运行时 dev_stop 也应成功（幂等）"
