"""settings：启动配置与环境变量（只读环境变量，不读任何密钥文件内容）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from dmr_kernel import settings as s
from dmr_kernel.core.types import Environment


def load(env: dict[str, str] | None = None, root: Path | None = None) -> s.Settings:
    return s.load_settings(env or {}, webui_root=root or Path("/srv/webui"))


def test_defaults_are_standin_on_loopback():
    st = load()
    assert st.environment is Environment.STANDIN
    assert (st.kernel_host, st.kernel_port) == ("127.0.0.1", 18200)
    assert st.list_url == "http://127.0.0.1:18080/api/v1/onlycoin/live"
    assert st.standin_url == "http://127.0.0.1:18220"
    assert st.dev_tools_enabled and not st.is_live
    assert not st.legacy_mutex_enabled


def test_paths_derive_from_webui_root():
    st = load(root=Path("/srv/webui"))
    assert st.var_dir == Path("/srv/webui/var")
    assert st.db_path == Path("/srv/webui/var/kernel/ledger.sqlite")
    assert st.kernel_lock_path == Path("/srv/webui/var/kernel.lock")
    assert st.local_token_path == Path("/srv/webui/var/kernel/local.token")
    assert st.backup_dir == Path("/srv/webui/backup")
    assert st.standin_token_path == Path("/srv/webui/var/standin/kernel.token")
    assert st.dmr_root == Path("/srv")


def test_non_loopback_host_is_rejected():
    with pytest.raises(s.SettingsError):
        load({"DMR_KERNEL_HOST": "0.0.0.0"})


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:18080/api/v1/onlycoin/live?x=1",
    "http://127.0.0.1:18080/api/v1/onlycoin/sources/screener-y/status",
    "http://127.0.0.1:18080/api/v1/onlycoin/live/",
])
def test_list_url_must_be_the_read_only_live_endpoint(url):
    with pytest.raises(s.SettingsError):
        load({"DMR_LIST_URL": url})


def test_unknown_environment_is_rejected():
    with pytest.raises(s.SettingsError):
        load({"DMR_ENV": "prod"})


def test_mainnet_live_requires_live_switch():
    with pytest.raises(s.SettingsError):
        load({"DMR_ENV": "mainnet_live"})
    st = load({"DMR_ENV": "mainnet_live", "DMR_ALLOW_LIVE": "1"})
    assert st.is_live and st.legacy_mutex_enabled and not st.dev_tools_enabled


def test_testnet_has_no_dev_tools_and_no_legacy_mutex():
    st = load({"DMR_ENV": "testnet"})
    assert not st.dev_tools_enabled and not st.is_live and not st.legacy_mutex_enabled


def test_crashpoint_is_refused_in_live():
    with pytest.raises(s.SettingsError):
        load({"DMR_ENV": "mainnet_live", "DMR_ALLOW_LIVE": "1", "DMR_CRASHPOINT": "open_sent"})
    assert load({"DMR_CRASHPOINT": "open_sent"}).crashpoint == "open_sent"


def test_instance_id_is_short_alnum():
    assert load({"DMR_INSTANCE_ID": "B2"}).instance_id == "B2"
    for bad in ("", "toolong", "a-b"):
        with pytest.raises(s.SettingsError):
            load({"DMR_INSTANCE_ID": bad})


def test_model3_deploy_switch_defaults_off():
    assert load().model3_deploy_switch is False
    assert load({"DMR_MODEL3_DEPLOY_SWITCH": "1"}).model3_deploy_switch is True


def test_settings_never_expose_secret_values_in_repr():
    st = load({"DMR_ALERT_URLS": "tgram://secret-token/123"})
    assert "secret-token" not in repr(st)
