"""启动入口：ui.run 的参数（只绑回环、不自动重载、单进程）、会话签名密钥、日志、NiceGUI 存储目录、装配。"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from dmr_console import __main__ as entry
from dmr_console.settings import load_settings

ROOT = Path("/opt/webui")


def settings_for(tmp_path: Path, **env: str):
    return load_settings({"DMR_VAR_DIR": str(tmp_path / "var"), **env}, webui_root=tmp_path)


# ------------------------------------------------------------------ ui.run 的参数

def test_run_kwargs_listen_on_loopback_only_without_reload_or_browser_or_workers():
    kwargs = entry.run_kwargs(load_settings({}, webui_root=ROOT), "s" * 40)
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 18210)
    assert kwargs["reload"] is False and kwargs["show"] is False and kwargs["native"] is False
    assert "workers" not in kwargs and "on_air" not in kwargs and "ssl_certfile" not in kwargs
    assert kwargs["storage_secret"] == "s" * 40
    assert kwargs["show_welcome_message"] is False


def test_the_port_comes_from_the_environment_but_the_host_stays_on_loopback():
    kwargs = entry.run_kwargs(load_settings({"DMR_CONSOLE_PORT": "19999"}, webui_root=ROOT), "s" * 40)
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 19999)
    assert entry.run_kwargs(load_settings({"DMR_CONSOLE_HOST": "::1"}, webui_root=ROOT), "s" * 40)["host"] == "::1"


def test_the_session_cookie_has_its_own_name_and_is_same_site_strict():
    kwargs = entry.run_kwargs(load_settings({}, webui_root=ROOT), "s" * 40)["session_middleware_kwargs"]
    assert kwargs["session_cookie"] == "dmr_console" and kwargs["same_site"] == "strict" and 0 < kwargs["max_age"] <= 24 * 3600


# ------------------------------------------------------------------ 日志

def test_logging_goes_to_a_rotating_file_under_var_log_and_is_not_stacked_on_repeat(tmp_path):
    settings = settings_for(tmp_path, DMR_LOG_LEVEL="DEBUG")
    logger = logging.getLogger("dmr_console_test_root")
    logger.propagate = False
    try:
        entry.configure_logging(settings, logger)
        entry.configure_logging(settings, logger)
        mine = [h for h in logger.handlers if getattr(h, "_dmr_console", False)]
        assert len(mine) == 2
        file_handler = next(h for h in mine if isinstance(h, logging.handlers.RotatingFileHandler))
        assert Path(file_handler.baseFilename) == tmp_path / "var" / "log" / "console.log"
        assert file_handler.maxBytes == entry.LOG_MAX_BYTES and file_handler.backupCount == entry.LOG_BACKUPS
        assert logger.level == logging.DEBUG
        logger.info("hello")
        file_handler.flush()
        assert "hello" in (tmp_path / "var" / "log" / "console.log").read_text(encoding="utf-8")
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def test_transport_libraries_are_kept_quiet_so_urls_do_not_reach_the_log(tmp_path):
    logger = logging.getLogger("dmr_console_test_root2")
    logger.propagate = False
    saved = {name: logging.getLogger(name).level for name in entry.QUIET_LOGGERS}
    try:
        entry.configure_logging(settings_for(tmp_path, DMR_LOG_LEVEL="DEBUG"), logger)
        assert all(logging.getLogger(name).level == logging.WARNING for name in entry.QUIET_LOGGERS)
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


# ------------------------------------------------------------------ NiceGUI 存储目录

def test_nicegui_storage_is_redirected_under_var_console(tmp_path, monkeypatch):
    from nicegui import app
    from nicegui.storage import Storage

    monkeypatch.setattr(Storage, "path", Storage.path)
    monkeypatch.setattr(app, "storage", app.storage)
    monkeypatch.setenv("NICEGUI_STORAGE_PATH", "")
    target = tmp_path / "var" / "console" / "nicegui"
    entry.redirect_nicegui_storage(target)
    assert Storage.path == target.resolve() and os.environ["NICEGUI_STORAGE_PATH"] == str(target)
    assert not target.exists()  # 只是指路，控制台不往里写


# ------------------------------------------------------------------ main()

class RunRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs) -> None:
        assert not args
        self.calls.append(kwargs)


@pytest.fixture
def isolated_nicegui(monkeypatch, tmp_path):
    """main() 会往 NiceGUI 的全局 app 上注册页面与关停钩子：在重置上下文里跑，测完恢复。"""
    from nicegui import ui
    from nicegui.storage import Storage
    from nicegui.testing.general import nicegui_reset_globals

    recorder = RunRecorder()
    monkeypatch.setattr(ui, "run", recorder)
    monkeypatch.setattr(entry, "configure_logging", lambda settings, logger=None: None)
    monkeypatch.setattr(Storage, "path", Storage.path)
    monkeypatch.setenv("NICEGUI_STORAGE_PATH", "")
    monkeypatch.chdir(tmp_path)
    with nicegui_reset_globals():
        yield recorder


def test_main_starts_the_app_with_a_fresh_secret_and_registers_the_pages(isolated_nicegui, tmp_path):
    from nicegui import app
    from nicegui.client import Client

    env = {"DMR_VAR_DIR": str(tmp_path / "var"), "DMR_CONSOLE_PORT": "19876"}
    assert entry.main(env) == 0
    assert len(isolated_nicegui.calls) == 1
    kwargs = isolated_nicegui.calls[0]
    assert (kwargs["host"], kwargs["port"], kwargs["reload"]) == ("127.0.0.1", 19876, False)
    secret_path = tmp_path / "var" / "console" / "session.secret"
    assert secret_path.read_text(encoding="ascii").strip() == kwargs["storage_secret"] and len(kwargs["storage_secret"]) >= 32
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert sorted(Client.page_routes.values()) == ["/", "/dev", "/kline", "/login", "/oplog", "/positions", "/settings", "/stats", "/trades", "/watchlist"]
    assert app._shutdown_handlers  # 关停时要关掉内核客户端与各会话的控制器


def test_main_reuses_the_secret_on_the_next_start(isolated_nicegui, tmp_path):
    env = {"DMR_VAR_DIR": str(tmp_path / "var")}
    entry.main(env)
    first = isolated_nicegui.calls[-1]["storage_secret"]
    from nicegui.testing.general import nicegui_reset_globals

    with nicegui_reset_globals():
        entry.main(env)
    assert isolated_nicegui.calls[-1]["storage_secret"] == first


def test_main_refuses_a_non_loopback_listen_address_before_creating_anything(isolated_nicegui, tmp_path, capsys):
    env = {"DMR_VAR_DIR": str(tmp_path / "var"), "DMR_CONSOLE_HOST": "0.0.0.0"}
    assert entry.main(env) == 2
    assert isolated_nicegui.calls == []
    assert "回环" in capsys.readouterr().err
    assert not (tmp_path / "var").exists()


def test_main_reports_an_unusable_secret_file_instead_of_crashing(isolated_nicegui, tmp_path, capsys):
    console_dir = tmp_path / "var" / "console"
    console_dir.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.write_text("x" * 64, encoding="ascii")
    (console_dir / "session.secret").symlink_to(target)
    assert entry.main({"DMR_VAR_DIR": str(tmp_path / "var")}) == 2
    assert isolated_nicegui.calls == []
    assert "符号链接" in capsys.readouterr().err


def test_main_leaves_no_nicegui_storage_files_behind(isolated_nicegui, tmp_path):
    entry.main({"DMR_VAR_DIR": str(tmp_path / "var")})
    leftovers = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert leftovers == ["var/console/session.secret"]
    assert not (tmp_path / ".nicegui").exists()


def test_build_console_wires_the_kernel_client_to_the_configured_loopback_address(tmp_path):
    from dmr_console.app import build_console

    settings = settings_for(tmp_path, DMR_KERNEL_PORT="18999", DMR_CONSOLE_POLL_S="7", DMR_CONSOLE_TIMEOUT_S="2")
    console = build_console(settings)
    try:
        assert str(console.service.api.http.base_url) == "http://127.0.0.1:18999/api/v1/"
        assert console.config.poll_interval_s == 7.0
        assert console.service._stream_factory is not None  # 事件流工厂随控制器带上
    finally:
        import asyncio

        asyncio.run(console.service.api.aclose())


@pytest.fixture
def uvicorn_stub(monkeypatch, tmp_path):
    """真跑 ui.run 的参数处理与服务器配置，只把 uvicorn 的 run 换成记录配置的空函数：不开任何 socket。"""
    import uvicorn
    from nicegui import helpers
    from nicegui.storage import Storage
    from nicegui.testing.general import nicegui_reset_globals

    seen: list = []

    def fake_run(self, sockets=None) -> None:
        seen.append(self.config)
        self.started = True

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.setattr(entry, "configure_logging", lambda settings, logger=None: None)
    monkeypatch.setattr(Storage, "path", Storage.path)
    monkeypatch.setenv("NICEGUI_STORAGE_PATH", "")
    monkeypatch.chdir(tmp_path)
    with nicegui_reset_globals():
        # is_pytest 只能在重置上下文「里面」才改成假：重置本身（进出各一次 app.reset → storage.clear）在 is_pytest 为假时
        # 会去碰 context.client，凭空造出一个伪客户端并把 NiceGUI 切到脚本模式，之后 ui.page 与 ui.run 就互相冲突。
        with monkeypatch.context() as inner:
            inner.setattr(helpers, "is_pytest", lambda: False)
            yield seen


def test_ui_run_accepts_our_arguments_and_uvicorn_is_configured_for_loopback_single_process(uvicorn_stub, tmp_path):
    from nicegui import core
    from starlette.middleware.sessions import SessionMiddleware

    env = {"DMR_VAR_DIR": str(tmp_path / "var"), "DMR_CONSOLE_PORT": "19877"}
    assert entry.main(env) == 0
    assert len(uvicorn_stub) == 1
    config = uvicorn_stub[0]
    assert (config.host, config.port) == ("127.0.0.1", 19877)
    assert config.reload is False and config.workers == 1 and config.access_log is False
    assert config.ssl_certfile is None and config.uds is None and config.fd is None
    sessions = [m for m in core.app.user_middleware if m.cls is SessionMiddleware]
    assert len(sessions) == 1
    options = sessions[0].kwargs
    assert options["session_cookie"] == "dmr_console" and options["same_site"] == "strict"
    assert 0 < options["max_age"] <= 24 * 3600
    assert not options.get("https_only", False)  # 回环上是明文 HTTP：设成只走 HTTPS 的话浏览器根本不会回传这个 Cookie
    secret = (tmp_path / "var" / "console" / "session.secret").read_text(encoding="ascii").strip()
    assert options["secret_key"] == secret
    assert core.app.config.reload is False
    assert os.environ.get("NICEGUI_HOST") is None  # ui.run 用完就清掉它的内部环境变量
