"""命令行应急入口 ``emergency close`` 的端到端（T1.7）：真实的三个进程，经 deploy/dev_start.sh 启动，临时端口（绝不占用
18200/18210/18220）、临时 var 目录、回放行情（DMR_STANDIN_FEED=replay）。

流程：造仓（替身的 /dev/manual/order；再用 kernel 令牌放一张系统前缀的止损和一张用户自己的条件单）-> 先写停用标记、再 kill -9 内核
（守护看到标记不会拉起）-> ``emergency close --yes`` -> 替身上持仓归零、系统条件单已撤、用户条件单还在、内核没有被拉起、停用标记仍在、
var/log/emergency.log 有记录 -> 操作者删标记、重新启动内核 -> 恢复后账本按成交补记（来源「应急入口」）-> dev_stop 全停、无残留进程。
整个模块共用一套部署，测试按文件顺序执行；夹具收尾无论如何都会强杀漏网的进程。
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from deploy_kit import (KERNEL_PY, WEBUI_ROOT, Stack, http_json, leftover_pids, pid_alive, port_is_free, wait_until)

pytestmark = pytest.mark.e2e

SYMBOL = "BTCUSDT"
SYSTEM_STOP_ID = "DKA1-0-1-s"  # 系统前缀的止损（DK + 实例号 + 模型代码 + 意图号 + 分腿）
SHARED: dict = {}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    deployment = Stack(tmp_path_factory.mktemp("cli_emergency"))
    try:
        deployment.start_result = deployment.run("dev_start.sh", timeout=240)
        yield deployment
    finally:
        deployment.shutdown()


def standin(stack: Stack, method: str, path: str, body=None, *, token_file: str = "manual.token", params: str = ""):
    token = (stack.var / "standin" / token_file).read_text(encoding="utf-8").strip()
    import urllib.request

    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(stack.url("standin") + path + params, data=data, method=method,
                                     headers={"X-Standin-Key": token, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "null")


def dev_state(stack: Stack) -> dict:
    status, body = standin(stack, "GET", "/dev/state")
    assert status == 200, body
    return body


def open_positions(stack: Stack) -> list[tuple[str, str, str]]:
    return sorted((p["symbol"], p["position_side"], str(p["qty"])) for p in dev_state(stack)["positions"]
                  if float(p["qty"]) != 0)


def algo_ids(stack: Stack) -> list[str | None]:
    return [a.get("client_algo_id") for a in dev_state(stack)["open_algo_orders"]]


def emergency(stack: Stack, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(KERNEL_PY), "-m", "dmr_kernel.cli", "emergency", "close", *args], env=stack.env,
                          cwd=WEBUI_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def kernel_state(stack: Stack) -> dict | None:
    try:
        token = (stack.var / "kernel" / "local.token").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    status, body = http_json("GET", stack.url("kernel") + "/api/v1/state", token=token, timeout=3)
    return body if status == 200 and isinstance(body, dict) else None


def fills(stack: Stack) -> list[tuple]:
    path = stack.var / "kernel" / "ledger.sqlite"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return conn.execute("SELECT symbol, side, position_side, qty, source, client_order_id FROM fill").fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------------ 造仓


def test_stack_is_up_and_the_kernel_is_healthy(stack):
    assert stack.start_result.returncode == 0, stack.start_result.stdout + stack.start_result.stderr
    wait_until(lambda: kernel_state(stack) is not None, 60, what="内核控制接口可用")
    SHARED["kernel_pid"] = stack.pid("kernel")
    assert pid_alive(SHARED["kernel_pid"])


def test_build_positions_and_conditional_orders_on_the_standin(stack):
    for position_side, side in (("LONG", "BUY"), ("SHORT", "SELL")):
        status, body = standin(stack, "POST", "/dev/manual/order",
                               {"symbol": SYMBOL, "side": side, "position_side": position_side, "qty": "0.01"})
        assert status == 200, body
    assert len(open_positions(stack)) == 2
    price = float(dev_state(stack)["prices"][SYMBOL]["mark"])
    trigger = f"{price * 0.8:.1f}"
    # 系统前缀的止损（kernel 令牌，等于内核自己下的）；用户自己的条件单（manual 令牌，编号是网页编号）
    status, body = standin(stack, "POST", "/api/private/algo_order",
                           {"symbol": SYMBOL, "side": "SELL", "position_side": "LONG", "type": "STOP_MARKET",
                            "trigger_price": trigger, "working_type": "MARK_PRICE", "close_position": True,
                            "client_algo_id": SYSTEM_STOP_ID}, token_file="kernel.token")
    assert status == 200, body
    status, body = standin(stack, "POST", "/dev/manual/algo_order",  # 空头方向上用户自己的止损（同侧只能有一张全平型）
                           {"symbol": SYMBOL, "side": "BUY", "position_side": "SHORT", "type": "STOP_MARKET",
                            "trigger_price": f"{price * 1.2:.1f}", "working_type": "CONTRACT_PRICE",
                            "close_position": True})
    assert status == 200, body
    assert SYSTEM_STOP_ID in algo_ids(stack) and len(algo_ids(stack)) == 2


# ------------------------------------------------------------------ 内核死掉、守护被挡住、应急平仓


def test_kill_the_kernel_with_the_supervisor_blocked(stack):
    marker = stack.run_dir / "kernel.nosupervise"
    marker.write_text("e2e: block supervise before kill -9\n")  # 先挡住守护，再杀内核
    os.kill(SHARED["kernel_pid"], signal.SIGKILL)
    wait_until(lambda: not pid_alive(SHARED["kernel_pid"]), 10, what="内核被杀")
    time.sleep(3)  # 守护的重拉间隔内，确认没有新内核
    assert kernel_state(stack) is None
    assert not pid_alive(stack.pid("kernel"))


def test_emergency_close_flattens_the_standin_and_cancels_only_system_stops(stack):
    result = emergency(stack, "--yes")
    text = result.stdout + result.stderr
    assert result.returncode == 0, text
    assert open_positions(stack) == []
    ids = algo_ids(stack)
    assert SYSTEM_STOP_ID not in ids and len(ids) == 1  # 系统止损撤了，用户自己的条件单还在
    assert "停用标记仍在，确认后删除 var/run/kernel.nosupervise 再启动内核" in result.stdout
    assert (stack.run_dir / "kernel.nosupervise").exists()
    log = (stack.var / "log" / "emergency.log").read_text(encoding="utf-8")
    assert SYMBOL in log and "DKE-" in log and SYSTEM_STOP_ID in log


def test_the_kernel_was_not_brought_back_by_the_supervisor(stack):
    time.sleep(2)
    assert kernel_state(stack) is None
    assert port_is_free(stack.ports["kernel"])
    assert not [p for p in leftover_pids(stack.var) if "dmr_kernel" in Path(f"/proc/{p}/cmdline").read_text().replace("\0", " ")]


# ------------------------------------------------------------------ 恢复：删标记、启动内核、账本补记


def test_after_removing_the_marker_the_kernel_starts_and_backfills_the_emergency_fills(stack):
    (stack.run_dir / "kernel.nosupervise").unlink()
    started = stack.run("dev_start.sh", timeout=240)
    assert started.returncode == 0, started.stdout + started.stderr
    wait_until(lambda: kernel_state(stack) is not None, 60, what="内核控制接口可用")

    def recovered() -> bool:
        state = kernel_state(stack)
        return state is not None and "recovering" not in [s["code"] for s in state["model"]["suppressions"]]

    wait_until(recovered, 90, what="重启恢复完成")
    wait_until(lambda: any(row[4] == "emergency" for row in fills(stack)), 60, what="账本补记应急入口的成交")
    emergency_rows = [row for row in fills(stack) if row[4] == "emergency"]
    assert len(emergency_rows) == 2
    assert sorted((r[1], r[2]) for r in emergency_rows) == [("BUY", "SHORT"), ("SELL", "LONG")]
    assert all(str(r[5]).startswith("DKE-") for r in emergency_rows)


# ------------------------------------------------------------------ 收尾


def test_dev_stop_leaves_no_process_behind(stack):
    result = stack.run("dev_stop.sh", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    wait_until(lambda: not leftover_pids(stack.var), 15, what="没有残留进程")
    for name in ("standin", "kernel", "console"):
        assert port_is_free(stack.ports[name])
