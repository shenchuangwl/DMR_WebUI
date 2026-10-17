"""``python -m dmr_console``：启动 Web 控制台（NiceGUI），监听 127.0.0.1:18210，只经 HTTP/WebSocket 调内核。

- 监听只允许回环地址；端口可由 ``DMR_CONSOLE_PORT`` 改。
- 会话签名密钥首次启动随机生成，写进 ``var/console/session.secret``（0600），之后沿用。
- 自动重载关闭，单进程。控制台重启只丢掉各浏览器的登录（令牌只在内存里），内核不受影响。
- 日志：标准库 logging，只在这里配置处理器（``var/log/console.log`` 按大小轮转 10MB×5，时间一律 UTC，同时写标准错误）；
  不记令牌、口令、事件流地址。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dmr_console.labels import TITLE
from dmr_console.secret import SecretError, load_or_create_secret
from dmr_console.settings import ConsoleSettings, SettingsError, load_settings

WEBUI_ROOT = Path(__file__).resolve().parents[1]
SESSION_COOKIE = "dmr_console"  # 与同机其它服务的 Cookie 区分：Cookie 不分端口
SESSION_MAX_AGE_S = 12 * 3600
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5
QUIET_LOGGERS = ("httpx", "httpcore", "aiohttp", "h11", "wsproto", "websockets")


def run_kwargs(settings: ConsoleSettings, secret: str) -> dict[str, Any]:
    """交给 ``ui.run`` 的参数。关键约束：只绑回环、不自动重载、不弹浏览器、单进程（不传 workers）。"""
    return {
        "host": settings.host,
        "port": settings.port,
        "title": TITLE,
        "reload": False,
        "show": False,
        "show_welcome_message": False,
        "native": False,
        "dark": None,
        "storage_secret": secret,
        "session_middleware_kwargs": {"session_cookie": SESSION_COOKIE, "same_site": "strict", "max_age": SESSION_MAX_AGE_S},
        "uvicorn_logging_level": "warning",
        "access_log": False,
    }


def configure_logging(settings: ConsoleSettings, logger: logging.Logger | None = None) -> None:
    """只在这里配置处理器（约定第 6 节）：按大小轮转的 ``var/log/console.log`` + 标准错误。重复调用不会叠加处理器。"""
    root = logger or logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_dmr_console", False)]:
        root.removeHandler(handler)
        handler.close()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime  # 日志时间一律 UTC
    for handler in (
        logging.handlers.RotatingFileHandler(settings.log_dir / "console.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                                             encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ):
        handler.setFormatter(formatter)
        handler._dmr_console = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(settings.log_level)
    for name in QUIET_LOGGERS:  # 传输层库的日志带请求地址等，不论总级别多低都只留警告以上
        logging.getLogger(name).setLevel(logging.WARNING)


def redirect_nicegui_storage(path: Path) -> None:
    """让 NiceGUI 自己的存储目录落在 var/console/nicegui 下，不在当前目录建 .nicegui。控制台并不往里写任何东西。"""
    os.environ["NICEGUI_STORAGE_PATH"] = str(path)
    from nicegui import app
    from nicegui.storage import Storage

    Storage.path = Path(path).resolve()
    app.storage = Storage()


def main(env: Mapping[str, str] | None = None) -> int:
    try:
        settings = load_settings(os.environ if env is None else env, webui_root=WEBUI_ROOT)
        secret = load_or_create_secret(settings.secret_path)
    except (SettingsError, SecretError) as exc:
        print(f"dmr_console: 启动失败：{exc}", file=sys.stderr)
        return 2
    configure_logging(settings)
    redirect_nicegui_storage(settings.nicegui_storage_dir)

    from nicegui import app, ui

    from dmr_console.app import build_console, register_pages

    console = build_console(settings)
    register_pages(console)
    app.on_shutdown(console.aclose)
    logging.getLogger(__name__).info("控制台启动：http://%s:%d（内核 %s）", settings.host, settings.port, settings.kernel_base_url)
    ui.run(**run_kwargs(settings, secret))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
