"""内核启动配置（第 0a 层）。只读环境变量，不读取任何密钥文件的内容。

环境变量（都可省略，省略即取默认）：

=============================  ==========================================================
DMR_ENV                        standin（默认，替身环境）| testnet | mainnet_shadow | mainnet_live
DMR_ALLOW_LIVE                 1 = 实盘开关；mainnet_live 没有它就拒绝启动（4.12「实盘开关要真正实现」）
DMR_VAR_DIR                    运行期目录，默认 webui/var
DMR_DB_PATH                    账本，默认 <var>/kernel/ledger.sqlite
DMR_BACKUP_DIR                 在线备份目录（在 var 之外），默认 webui/backup
DMR_KERNEL_HOST / _PORT        控制接口监听，默认 127.0.0.1:18200，只允许回环
DMR_INSTANCE_ID                实例号，1–2 位字母数字，默认 A（写进订单编号）
DMR_LIST_URL                   名单接口，只允许 http://127.0.0.1:18080/api/v1/onlycoin/live
DMR_STANDIN_URL                替身交易所地址，默认 http://127.0.0.1:18220（只允许回环）
DMR_STANDIN_TOKEN_FILE         替身交易所给内核的访问令牌文件，默认 <var>/standin/kernel.token
DMR_MODEL3_DEPLOY_SWITCH       1 = 允许修改模型 3 的档位（部署级开关），默认关
DMR_ALERT_URLS                 Apprise 告警地址（逗号分隔）；本轮不配置
DMR_CRASHPOINT                 测试用崩溃点（只在非实盘环境可用）
DMR_KERNEL_SHELL               1 = 只启动空壳（T0.1 自检），不装配任何业务组件
DMR_LOG_LEVEL                  默认 INFO
=============================  ==========================================================
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dmr_kernel.core.types import Environment

LIST_URL_DEFAULT = "http://127.0.0.1:18080/api/v1/onlycoin/live"
LIST_PATH = "/api/v1/onlycoin/live"
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9]{1,2}$")


class SettingsError(ValueError):
    pass


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    environment: Environment
    allow_live: bool
    webui_root: Path
    var_dir: Path
    db_path: Path
    backup_dir: Path
    kernel_host: str
    kernel_port: int
    instance_id: str
    list_url: str
    standin_url: str
    standin_token_path: Path
    model3_deploy_switch: bool
    crashpoint: str | None
    shell_only: bool
    log_level: str
    alert_urls: tuple[str, ...] = field(default=(), repr=False)

    @property
    def is_live(self) -> bool:
        return self.environment is Environment.MAINNET_LIVE

    @property
    def dev_tools_enabled(self) -> bool:
        """信号注入、测试专用造仓入口、模拟手动下单面板：只在替身环境存在。"""
        return self.environment is Environment.STANDIN

    @property
    def legacy_mutex_enabled(self) -> bool:
        """旧入口互斥（7 把锁、停用标记）只在实盘启用；替身环境不打开 data/runtime_state 下的任何文件。"""
        return self.environment is Environment.MAINNET_LIVE

    @property
    def dmr_root(self) -> Path:
        return self.webui_root.parent

    @property
    def kernel_lock_path(self) -> Path:
        return self.var_dir / "kernel.lock"

    @property
    def local_token_path(self) -> Path:
        return self.var_dir / "kernel" / "local.token"

    @property
    def log_dir(self) -> Path:
        return self.var_dir / "log"


def load_settings(env: Mapping[str, str], *, webui_root: Path) -> Settings:
    def get(name: str, default: str = "") -> str:
        return env.get(name, default).strip()

    try:
        environment = Environment(get("DMR_ENV", "standin") or "standin")
    except ValueError as exc:
        raise SettingsError(f"DMR_ENV 取值无效：{get('DMR_ENV')!r}") from exc
    allow_live = get("DMR_ALLOW_LIVE") == "1"
    if environment is Environment.MAINNET_LIVE and not allow_live:
        raise SettingsError("主网实盘需要实盘开关 DMR_ALLOW_LIVE=1；未打开时拒绝连接主网交易接口")

    var_dir = Path(get("DMR_VAR_DIR") or webui_root / "var")
    db_path = Path(get("DMR_DB_PATH") or var_dir / "kernel" / "ledger.sqlite")
    backup_dir = Path(get("DMR_BACKUP_DIR") or webui_root / "backup")

    host = get("DMR_KERNEL_HOST", "127.0.0.1") or "127.0.0.1"
    if not _is_loopback(host):
        raise SettingsError(f"控制接口只允许绑定回环地址，拒绝 {host!r}")
    try:
        port = int(get("DMR_KERNEL_PORT", "18200") or "18200")
    except ValueError as exc:
        raise SettingsError("DMR_KERNEL_PORT 必须是整数") from exc

    instance_id = get("DMR_INSTANCE_ID", "A")
    if not _INSTANCE_RE.fullmatch(instance_id):
        raise SettingsError("DMR_INSTANCE_ID 必须是 1–2 位字母或数字")

    list_url = get("DMR_LIST_URL") or LIST_URL_DEFAULT
    parts = urlsplit(list_url)
    if (parts.scheme != "http" or not _is_loopback(parts.hostname or "") or parts.path != LIST_PATH
            or parts.query or parts.fragment):
        raise SettingsError(f"名单接口只允许本机 GET {LIST_PATH}，且不带任何查询参数：{list_url!r}")

    standin_url = get("DMR_STANDIN_URL") or "http://127.0.0.1:18220"
    sp = urlsplit(standin_url)
    if sp.scheme != "http" or not _is_loopback(sp.hostname or ""):
        raise SettingsError(f"替身交易所只允许回环地址：{standin_url!r}")
    standin_token_path = Path(get("DMR_STANDIN_TOKEN_FILE") or var_dir / "standin" / "kernel.token")

    crashpoint = get("DMR_CRASHPOINT") or None
    if crashpoint and environment is Environment.MAINNET_LIVE:
        raise SettingsError("故障注入（崩溃点）在实盘环境下不可用")

    alert_urls = tuple(u.strip() for u in get("DMR_ALERT_URLS").split(",") if u.strip())
    return Settings(
        environment=environment,
        allow_live=allow_live,
        webui_root=webui_root,
        var_dir=var_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        kernel_host=host,
        kernel_port=port,
        instance_id=instance_id,
        list_url=list_url,
        standin_url=standin_url.rstrip("/"),
        standin_token_path=standin_token_path,
        model3_deploy_switch=get("DMR_MODEL3_DEPLOY_SWITCH") == "1",
        crashpoint=crashpoint,
        shell_only=get("DMR_KERNEL_SHELL") == "1",
        log_level=get("DMR_LOG_LEVEL", "INFO") or "INFO",
        alert_urls=alert_urls,
    )
