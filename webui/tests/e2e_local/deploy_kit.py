"""deploy/ 脚本端到端测试的公共设施：空闲端口、临时 var 目录、脚本运行、进程清理。

约束（docs/设计约定.md 第 1 节 8、9 条）：只用回环和临时端口，绝不占用正式端口 18200/18210/18220；行情一律回放
（``DMR_STANDIN_FEED=replay``，不连 Binance）。内核缺省是完整内核（T0.22 的装配）；只验证脚本本身的用例可以传
``**SHELL`` 让内核以空壳运行（``DMR_KERNEL_SHELL=1``，更快、不依赖业务装配）。
测试结束时用 ``Stack.shutdown`` 停掉一切：先 ``dev_stop.sh``，再按环境变量里的 ``DMR_VAR_DIR`` 扫 ``/proc``，
把漏网的进程强杀，保证不留常驻进程。
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

WEBUI_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = WEBUI_ROOT / "deploy"
KERNEL_PY = WEBUI_ROOT / ".venv-kernel" / "bin" / "python"
CONSOLE_PY = WEBUI_ROOT / ".venv-console" / "bin" / "python"
FORMAL_PORTS = frozenset({18200, 18210, 18220})  # 验收演示期间一直在运行，测试绝不占用
NAMES = ("standin", "kernel", "console")
LOOPBACK_ADDRS = frozenset({"0100007F", "00000000000000000000000001000000"})  # /proc/net/tcp、tcp6 里的 127.0.0.1 与 ::1
UTC_LINE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z "  # 日志行以 UTC 时间戳开头
SHELL = {"DMR_KERNEL_SHELL": "1"}  # Stack(tmp_path, **SHELL)：内核以空壳运行（只取单实例锁、等信号，没有控制接口）


def free_ports(count: int = 3) -> list[int]:
    """同时占住 count 个回环端口再一起放掉（保证彼此不同）；绝不返回正式端口。"""
    for _ in range(100):
        socks = [socket.socket(socket.AF_INET, socket.SOCK_STREAM) for _ in range(count)]
        try:
            for sock in socks:
                sock.bind(("127.0.0.1", 0))
            ports = [sock.getsockname()[1] for sock in socks]
        finally:
            for sock in socks:
                sock.close()
        if not FORMAL_PORTS & set(ports):
            return ports
    raise RuntimeError("找不到空闲的回环端口")


def script_env(var: Path, ports: Mapping[str, int] | None = None, **extra: str | None) -> dict[str, str]:
    """脚本运行用的环境：去掉外面的 DMR_* 与虚拟环境变量，再设临时 var、回放行情（内核缺省完整运行，要空壳传 ``**SHELL``）。
    extra 里值为 None 表示删除。"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("DMR_") and key not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "NICEGUI_STORAGE_PATH")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["DMR_VAR_DIR"] = str(var)
    env["DMR_BACKUP_DIR"] = str(var.parent / "backup")
    env["DMR_STANDIN_FEED"] = "replay"
    if ports:
        env["DMR_STANDIN_PORT"] = str(ports["standin"])
        env["DMR_KERNEL_PORT"] = str(ports["kernel"])
        env["DMR_CONSOLE_PORT"] = str(ports["console"])
        # 脚本自己也会导出这两项；直接起内核时没有它们就会连到正式的 18220，所以这里一律指向临时的替身
        env["DMR_STANDIN_URL"] = f"http://127.0.0.1:{ports['standin']}"
        env["DMR_STANDIN_TOKEN_FILE"] = str(var / "standin" / "kernel.token")
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


# ------------------------------------------------------------------ 进程帮手

def pid_alive(pid: int | None) -> bool:
    """进程还在运行（僵尸不算：它已经退出，只是还没被回收）。"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def cmdline(pid: int) -> str:
    return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()


def parent_pid(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])


def read_pid_file(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() and int(text) > 0 else None


def leftover_pids(var: Path) -> list[int]:
    """环境里带 ``DMR_VAR_DIR=<var>`` 的存活进程（脚本启动的东西都继承了它）。"""
    needle = f"DMR_VAR_DIR={var}".encode()
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if needle in environ and pid_alive(int(entry.name)):
            found.append(int(entry.name))
    return found


def wait_until(predicate: Callable[[], object], timeout: float, *, interval: float = 0.1, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"{timeout:g} 秒内没有等到：{what}")
        time.sleep(interval)


def listening_addresses(port: int) -> set[str]:
    """正在监听 port 的本地地址（/proc/net/tcp 与 tcp6 里的十六进制写法）。"""
    found: set[str] = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = Path(f"/proc/net/{name}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            address, _, hex_port = fields[1].partition(":")
            if fields[3] == "0A" and int(hex_port, 16) == port:  # 0A = LISTEN
                found.add(address)
    return found


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走任何代理


def http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with _OPENER.open(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:  # 连不上：当作没有回应
        return 0, str(exc)


def http_json(method: str, url: str, body: object = None, *, token: str | None = None,
              timeout: float = 5.0) -> tuple[int, object]:
    """带 JSON 的请求（可带 Bearer 令牌）；返回（状态码, 解析后的 JSON 或原文）。连不上返回 (0, 原因)。"""
    import json

    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def port_is_free(port: int) -> bool:
    """没有进程在监听 port，且服务可以立刻重新绑定它。

    刚用过的端口上会留下 TIME_WAIT 连接（健康检查、测试里的请求），不带 SO_REUSEADDR 的 bind 会因此失败整整一分钟；
    uvicorn 绑端口时是带的，所以这里也带——否则会把"端口上只剩 TIME_WAIT"误判成"服务没停干净"。
    """
    if listening_addresses(port):
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# ------------------------------------------------------------------ 一套临时部署

class Stack:
    """一套临时部署：临时 var、三个空闲端口、脚本运行用的环境。"""

    def __init__(self, tmp_path: Path, **extra: str | None) -> None:
        self.root = tmp_path
        self.var = tmp_path / "var"
        self.ports = dict(zip(NAMES, free_ports(3)))
        self.env = script_env(self.var, self.ports, **extra)
        self.leaked: list[int] = []

    @property
    def run_dir(self) -> Path:
        return self.var / "run"

    @property
    def log_dir(self) -> Path:
        return self.var / "log"

    def pid_file(self, name: str) -> Path:
        return self.run_dir / f"{name}.pid"

    def pid(self, name: str) -> int | None:
        return read_pid_file(self.pid_file(name))

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.ports[name]}"

    def run(self, script: str, *args: str, timeout: float = 120, env: Mapping[str, str | None] | None = None
            ) -> subprocess.CompletedProcess[str]:
        """从一个无关的目录运行 deploy/<script>（脚本自己 cd 到工程根）；stdout、stderr 都收集。"""
        merged = dict(self.env)
        for key, value in (env or {}).items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return subprocess.run([str(DEPLOY / script), *args], env=merged, cwd=self.root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)

    def read_log(self, name: str) -> str:
        try:
            return (self.log_dir / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def shutdown(self) -> None:
        """先正常停，再把漏网的强杀；self.leaked 记下正常停止之后还剩的进程。"""
        try:
            self.run("dev_stop.sh", timeout=120)
        except Exception:  # noqa: BLE001 - 清理路径：任何失败都继续往下强杀
            pass
        self.leaked = leftover_pids(self.var)
        for pid in self.leaked:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.monotonic() + 10
        while leftover_pids(self.var) and time.monotonic() < deadline:
            time.sleep(0.1)
