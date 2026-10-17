"""控制台启动配置：监听只绑回环；端口可由环境变量改，但 0 与越界一律拒绝；内核地址也只允许回环。"""
from __future__ import annotations

from pathlib import Path

import pytest

from dmr_console.settings import ConsoleSettings, SettingsError, load_settings

ROOT = Path("/opt/webui")


def load(**env: str) -> ConsoleSettings:
    return load_settings(env, webui_root=ROOT)


def test_defaults_bind_loopback_18210_and_find_kernel_on_18200():
    s = load()
    assert (s.host, s.port) == ("127.0.0.1", 18210)
    assert (s.kernel_host, s.kernel_port) == ("127.0.0.1", 18200)
    assert s.kernel_base_url == "http://127.0.0.1:18200/api/v1"
    assert s.var_dir == ROOT / "var"
    assert s.secret_path == ROOT / "var" / "console" / "session.secret"
    assert s.nicegui_storage_dir == ROOT / "var" / "console" / "nicegui"
    assert s.log_dir == ROOT / "var" / "log"
    assert s.poll_interval_s == 5.0
    assert s.request_timeout_s == 3.0
    assert s.log_level == "INFO"


def test_port_can_be_changed_by_environment():
    assert load(DMR_CONSOLE_PORT="18999").port == 18999
    assert load(DMR_CONSOLE_PORT=" 20000 ").port == 20000


@pytest.mark.parametrize("bad", ["0", "-1", "65536", "abc", "18210.5", "1e3", "０"])
def test_port_must_be_an_integer_in_range_and_zero_is_refused(bad):
    # 0 在 NiceGUI 里会被当成默认的 8080，所以直接拒绝
    with pytest.raises(SettingsError, match="DMR_CONSOLE_PORT"):
        load(DMR_CONSOLE_PORT=bad)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1"])
def test_loopback_hosts_are_accepted(host):
    s = load(DMR_CONSOLE_HOST=host)
    assert s.host in ("127.0.0.1", "127.0.0.2", "::1")


def test_localhost_is_normalised_to_an_address():
    assert load(DMR_CONSOLE_HOST="localhost").host == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "10.0.0.1", "example.com", "*", "127.0.0.1.evil.com"])
def test_non_loopback_listen_hosts_are_refused(host):
    with pytest.raises(SettingsError, match="回环"):
        load(DMR_CONSOLE_HOST=host)


def test_empty_host_means_default_not_all_interfaces():
    assert load(DMR_CONSOLE_HOST="").host == "127.0.0.1"
    assert load(DMR_CONSOLE_HOST="   ").host == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "kernel.example.com", "8.8.8.8"])
def test_kernel_address_must_be_loopback_so_tokens_never_leave_the_machine(host):
    with pytest.raises(SettingsError, match="回环"):
        load(DMR_KERNEL_HOST=host)


def test_kernel_address_follows_the_same_variables_as_the_kernel():
    s = load(DMR_KERNEL_HOST="127.0.0.1", DMR_KERNEL_PORT="18301")
    assert s.kernel_base_url == "http://127.0.0.1:18301/api/v1"


def test_ipv6_kernel_host_is_bracketed_in_the_url():
    assert load(DMR_KERNEL_HOST="::1").kernel_base_url == "http://[::1]:18200/api/v1"


@pytest.mark.parametrize("bad", ["0", "70000", "x"])
def test_kernel_port_validated(bad):
    with pytest.raises(SettingsError, match="DMR_KERNEL_PORT"):
        load(DMR_KERNEL_PORT=bad)


def test_var_dir_override(tmp_path):
    s = load(DMR_VAR_DIR=str(tmp_path / "v"))
    assert s.secret_path == tmp_path / "v" / "console" / "session.secret"
    assert s.log_dir == tmp_path / "v" / "log"


@pytest.mark.parametrize("name,value", [
    ("DMR_CONSOLE_POLL_S", "0"), ("DMR_CONSOLE_POLL_S", "0.5"), ("DMR_CONSOLE_POLL_S", "120"), ("DMR_CONSOLE_POLL_S", "nan"),
    ("DMR_CONSOLE_POLL_S", "inf"), ("DMR_CONSOLE_POLL_S", "abc"),
    ("DMR_CONSOLE_TIMEOUT_S", "0"), ("DMR_CONSOLE_TIMEOUT_S", "0.1"), ("DMR_CONSOLE_TIMEOUT_S", "99"),
    ("DMR_CONSOLE_TIMEOUT_S", "nan"), ("DMR_CONSOLE_TIMEOUT_S", "-3"),
])
def test_intervals_have_sane_bounds(name, value):
    with pytest.raises(SettingsError, match=name):
        load(**{name: value})


def test_intervals_accept_values_inside_the_bounds():
    s = load(DMR_CONSOLE_POLL_S="2", DMR_CONSOLE_TIMEOUT_S="1.5")
    assert (s.poll_interval_s, s.request_timeout_s) == (2.0, 1.5)


def test_log_level_validated_and_normalised():
    assert load(DMR_LOG_LEVEL="debug").log_level == "DEBUG"
    with pytest.raises(SettingsError, match="DMR_LOG_LEVEL"):
        load(DMR_LOG_LEVEL="LOUD")


def test_settings_are_immutable():
    s = load()
    with pytest.raises(Exception):
        s.port = 1  # type: ignore[misc]


def test_the_standin_address_and_manual_token_path_for_the_dev_page():
    s = load()
    assert s.standin_url == "http://127.0.0.1:18220"
    assert s.standin_token_path == ROOT / "var" / "standin" / "manual.token"
    assert load(DMR_STANDIN_URL="http://127.0.0.1:19001/").standin_url == "http://127.0.0.1:19001"


@pytest.mark.parametrize("value", ["http://example.org:18220", "https://127.0.0.1:18220", "ftp://127.0.0.1"])
def test_a_non_loopback_or_non_http_standin_address_is_refused(value):
    with pytest.raises(SettingsError):
        load(DMR_STANDIN_URL=value)
