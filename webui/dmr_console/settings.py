"""控制台启动配置。只读环境变量，不读取任何密钥文件的内容。

环境变量（都可省略，省略即取默认；与内核共用的变量名和含义和内核一致）：

=============================  ==========================================================
DMR_CONSOLE_HOST / _PORT       控制台监听，默认 127.0.0.1:18210，只允许回环；端口 1–65535（0 会被 NiceGUI 当成 8080，拒绝）
DMR_KERNEL_HOST / _PORT        内核控制接口的地址，默认 127.0.0.1:18200，只允许回环（令牌不出本机）
DMR_VAR_DIR                    运行期目录，默认 webui/var；会话签名密钥在 <var>/console/session.secret
DMR_CONSOLE_POLL_S             定时刷新间隔（秒），1–60，默认 5；事件流只是刷新提示，定时刷新兜底
DMR_CONSOLE_TIMEOUT_S          单次请求内核的总时限（秒），0.5–30，默认 3；超时按失联处理
DMR_STANDIN_URL                替身交易所地址（开发工具页用，仅 env=standin 显示），默认 http://127.0.0.1:18220，只允许回环的 http
DMR_LOG_LEVEL                  默认 INFO
=============================  ==========================================================
"""
from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18210
DEFAULT_KERNEL_PORT = 18200
DEFAULT_STANDIN_URL = "http://127.0.0.1:18220"
API_PREFIX = "/api/v1"
POLL_BOUNDS_S = (1.0, 60.0)
TIMEOUT_BOUNDS_S = (0.5, 30.0)
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_DIGITS = re.compile(r"[0-9]{1,5}")


class SettingsError(ValueError):
    pass


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ConsoleSettings:
    webui_root: Path
    var_dir: Path
    host: str
    port: int
    kernel_host: str
    kernel_port: int
    request_timeout_s: float
    poll_interval_s: float
    log_level: str
    standin_url: str = DEFAULT_STANDIN_URL

    @property
    def standin_token_path(self) -> Path:
        """替身交易所的手动令牌（开发工具页读它；与内核令牌是两把）。"""
        return self.var_dir / "standin" / "manual.token"

    @property
    def kernel_base_url(self) -> str:
        host = f"[{self.kernel_host}]" if ":" in self.kernel_host else self.kernel_host
        return f"http://{host}:{self.kernel_port}{API_PREFIX}"

    @property
    def console_dir(self) -> Path:
        return self.var_dir / "console"

    @property
    def secret_path(self) -> Path:
        return self.console_dir / "session.secret"

    @property
    def nicegui_storage_dir(self) -> Path:
        """NiceGUI 自己的存储目录；本控制台不使用 app.storage.user/general，目录只为让它不在别处落文件。"""
        return self.console_dir / "nicegui"

    @property
    def log_dir(self) -> Path:
        return self.var_dir / "log"


def _port(value: str, name: str) -> int:
    if not _DIGITS.fullmatch(value) or not 1 <= int(value) <= 65535:
        raise SettingsError(f"{name} 必须是 1–65535 的整数，收到 {value!r}")
    return int(value)


def _seconds(value: str, name: str, bounds: tuple[float, float]) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise SettingsError(f"{name} 必须是数字，收到 {value!r}") from exc
    low, high = bounds
    if not math.isfinite(number) or not low <= number <= high:
        raise SettingsError(f"{name} 必须在 {low:g}–{high:g} 秒之间，收到 {value!r}")
    return number


def _loopback(value: str, name: str, what: str) -> str:
    if not is_loopback_host(value):
        raise SettingsError(f"{what}只允许回环地址（{name}），拒绝 {value!r}")
    return "127.0.0.1" if value == "localhost" else value


def parse_standin_url(value: str) -> str:
    url = (value or "").strip().rstrip("/") or DEFAULT_STANDIN_URL
    parts = urlsplit(url)
    if parts.scheme != "http" or not is_loopback_host(parts.hostname or ""):
        raise SettingsError(f"替身交易所只允许回环上的 http（DMR_STANDIN_URL），拒绝 {url!r}")
    return url


def load_settings(env: Mapping[str, str], *, webui_root: Path) -> ConsoleSettings:
    def get(name: str, default: str = "") -> str:
        return (env.get(name) or "").strip() or default

    host = _loopback(get("DMR_CONSOLE_HOST", DEFAULT_HOST), "DMR_CONSOLE_HOST", "控制台监听")
    port = _port(get("DMR_CONSOLE_PORT", str(DEFAULT_PORT)), "DMR_CONSOLE_PORT")
    kernel_host = _loopback(get("DMR_KERNEL_HOST", DEFAULT_HOST), "DMR_KERNEL_HOST", "内核控制接口")
    kernel_port = _port(get("DMR_KERNEL_PORT", str(DEFAULT_KERNEL_PORT)), "DMR_KERNEL_PORT")
    poll = _seconds(get("DMR_CONSOLE_POLL_S", "5"), "DMR_CONSOLE_POLL_S", POLL_BOUNDS_S)
    timeout = _seconds(get("DMR_CONSOLE_TIMEOUT_S", "3"), "DMR_CONSOLE_TIMEOUT_S", TIMEOUT_BOUNDS_S)
    level = get("DMR_LOG_LEVEL", "INFO").upper()
    if level not in _LOG_LEVELS:
        raise SettingsError(f"DMR_LOG_LEVEL 取值无效：{get('DMR_LOG_LEVEL')!r}")
    return ConsoleSettings(
        webui_root=webui_root,
        var_dir=Path(get("DMR_VAR_DIR") or webui_root / "var"),
        host=host,
        port=port,
        kernel_host=kernel_host,
        kernel_port=kernel_port,
        request_timeout_s=timeout,
        poll_interval_s=poll,
        log_level=level,
        standin_url=parse_standin_url(get("DMR_STANDIN_URL")),
    )
