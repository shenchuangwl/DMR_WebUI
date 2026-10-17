"""deploy/ 的静态检查：脚本的写法约定、systemd 用户级单元模板、README。不起任何进程（只跑 ``bash -n`` 做语法检查）。"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from deploy_kit import DEPLOY, WEBUI_ROOT

pytestmark = pytest.mark.e2e

SCRIPTS = ("dev_start.sh", "dev_stop.sh", "dev_status.sh", "supervise.sh", "backup_drill.sh")
ALL_SHELL = SCRIPTS + ("lib.sh",)
UNITS = ("dmr-standin.service", "dmr-kernel.service", "dmr-console.service", "dmr-heartbeat.service",
         "dmr-heartbeat.timer")
SYSTEMD = DEPLOY / "systemd"
PLACEHOLDER = "@WEBUI_ROOT@"


def code_lines(path: Path) -> list[str]:
    """去掉整行注释和空行后的脚本行。"""
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


# ------------------------------------------------------------------ 脚本

@pytest.mark.parametrize("name", SCRIPTS)
def test_entry_scripts_are_executable_bash_with_strict_mode(name):
    path = DEPLOY / name
    assert path.is_file(), f"缺少 deploy/{name}"
    assert os.stat(path).st_mode & stat.S_IXUSR, f"deploy/{name} 没有可执行权限"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert re.search(r"^set -euo pipefail$", text, re.M), "必须 set -euo pipefail"


@pytest.mark.parametrize("name", ALL_SHELL)
def test_scripts_pass_the_bash_syntax_check(name):
    result = subprocess.run(["/usr/bin/bash", "-n", str(DEPLOY / name)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ALL_SHELL)
def test_scripts_only_use_the_two_project_interpreters_by_absolute_path(name):
    lines = code_lines(DEPLOY / name)
    text = "\n".join(lines)
    bare = re.findall(r"(?<![\w./$\"'-])(python3?|pip3?|pytest|py\.test)(?![\w.-])", text)
    assert not bare, f"deploy/{name} 里有裸的 {sorted(set(bare))}（只能用 .venv-kernel / .venv-console 下的绝对路径）"
    for forbidden in ("DMR_WebUI/venv", "/usr/bin/python", "/usr/local/bin/python", "activate"):
        assert forbidden not in text, f"deploy/{name} 不应出现 {forbidden!r}"


def test_interpreter_variables_point_into_the_project_venvs():
    text = (DEPLOY / "lib.sh").read_text(encoding="utf-8")
    assert re.search(r'^KERNEL_PY="\$WEBUI_ROOT/\.venv-kernel/bin/python"$', text, re.M)
    assert re.search(r'^CONSOLE_PY="\$WEBUI_ROOT/\.venv-console/bin/python"$', text, re.M)


@pytest.mark.parametrize("name", ALL_SHELL)
def test_scripts_stay_inside_the_rules_of_this_round(name):
    text = "\n".join(code_lines(DEPLOY / name))
    for forbidden in (".env", "runtime_state", "18080", "onlycoin", "BINANCE", "api_key", "0.0.0.0",
                      "deploy_and_run", "cleanup_deployment", "start_trading", "switch_model", "_main.py"):
        assert forbidden not in text, f"deploy/{name} 不应出现 {forbidden!r}"
    assert not re.search(r"\bgit\s+(add|commit|push|reset|clean|stash|checkout)\b", text)
    assert not re.search(r"\b(pip|apt|apt-get|npm|conda)\s+install\b", text)


def test_scripts_pin_their_own_path_so_a_foreign_path_cannot_supply_python():
    for name in SCRIPTS:
        text = (DEPLOY / name).read_text(encoding="utf-8")
        assert re.search(r"^export PATH=/usr/bin:/bin$", text, re.M), f"deploy/{name} 应固定 PATH=/usr/bin:/bin"


# ------------------------------------------------------------------ systemd 用户级单元模板

def parse_unit(path: Path) -> list[tuple[str, str, str]]:
    items, section = [], ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        assert sep, f"{path.name}：不是 键=值 的行：{raw!r}"
        items.append((section, key.strip(), value.strip()))
    return items


def values(unit: str, section: str, key: str) -> list[str]:
    return [v for s, k, v in parse_unit(SYSTEMD / unit) if s == section and k == key]


@pytest.mark.parametrize("unit", UNITS)
def test_unit_templates_exist_and_use_only_the_webui_root_placeholder(unit):
    text = (SYSTEMD / unit).read_text(encoding="utf-8")
    assert set(re.findall(r"@[A-Z_]+@", text)) <= {PLACEHOLDER}, "模板里只允许 @WEBUI_ROOT@ 这一个占位符"
    parse_unit(SYSTEMD / unit)  # 语法：每个非注释行都是节名或 键=值


@pytest.mark.parametrize("unit", UNITS)
def test_units_bind_loopback_only_and_never_leave_the_standin_environment(unit):
    text = (SYSTEMD / unit).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "0.0.0.0" not in code and "[::]" not in code and "::" not in code
    for host in re.findall(r"--host[ =](\S+)", code):
        assert host == "127.0.0.1", f"{unit}：--host 只能是 127.0.0.1"
    for host in re.findall(r"DMR_\w*HOST=(\S+)", code):
        assert host == "127.0.0.1"
    for env in re.findall(r"DMR_ENV=(\S+)", code):
        assert env == "standin", "本轮只实现替身环境"
    assert "DMR_ALLOW_LIVE" not in code
    assert not re.search(r"^(Requires|BindsTo|PartOf|Upholds)=.*dmr-", code, re.M), \
        f"{unit}：三个服务互不连带（重启控制台不能影响内核）：只许用 Wants/After"


@pytest.mark.parametrize("unit, interpreter, module, port", [
    ("dmr-standin.service", ".venv-kernel", "dmr_standin", "18220"),
    ("dmr-kernel.service", ".venv-kernel", "dmr_kernel", "18200"),
    ("dmr-console.service", ".venv-console", "dmr_console", "18210"),
])
def test_long_running_units_restart_on_failure_with_the_right_interpreter(unit, interpreter, module, port):
    assert values(unit, "Service", "Restart") == ["on-failure"]
    assert values(unit, "Service", "WorkingDirectory") == [PLACEHOLDER]
    (start,) = values(unit, "Service", "ExecStart")
    assert start.startswith(f"{PLACEHOLDER}/{interpreter}/bin/python -m {module}"), start
    assert values(unit, "Install", "WantedBy") == ["default.target"]
    assert values(unit, "Service", "RestartSec"), "要有重启间隔，免得崩溃循环空转"
    assert any(port in v for v in values(unit, "Service", "Environment") + [start]), f"{unit} 里应写明端口 {port}"
    assert values(unit, "Service", "NoNewPrivileges") == ["yes"]


def test_standin_unit_uses_the_var_standin_state_dir_and_the_auto_feed():
    (start,) = values("dmr-standin.service", "Service", "ExecStart")
    assert "--state-dir " + PLACEHOLDER + "/var/standin" in start and "--feed auto" in start and "--port 18220" in start


def test_kernel_unit_honours_the_nosupervise_marker_and_waits_for_the_standin():
    (condition,) = values("dmr-kernel.service", "Service", "ExecCondition")
    assert "kernel.nosupervise" in condition and condition.split()[0].startswith("/"), condition
    assert "dmr-standin.service" in " ".join(values("dmr-kernel.service", "Unit", "After"))
    environment = " ".join(values("dmr-kernel.service", "Service", "Environment"))
    assert "DMR_KERNEL_PORT=18200" in environment and "DMR_STANDIN_URL=http://127.0.0.1:18220" in environment
    assert int(values("dmr-kernel.service", "Service", "TimeoutStopSec")[0].rstrip("s")) >= 30, \
        "内核关停要给足时间（等在途意图、做检查点）"


def test_console_unit_does_not_bind_the_kernel_lifetime():
    unit = "dmr-console.service"
    assert "dmr-kernel.service" in " ".join(values(unit, "Unit", "After"))
    environment = " ".join(values(unit, "Service", "Environment"))
    assert "DMR_CONSOLE_PORT=18210" in environment and "DMR_KERNEL_PORT=18200" in environment


def test_heartbeat_pair_is_a_oneshot_checker_on_a_thirty_second_timer():
    assert values("dmr-heartbeat.service", "Service", "Type") == ["oneshot"]
    assert not values("dmr-heartbeat.service", "Service", "Restart"), "定时器本来就会再跑；失败重启只会重复推送"
    (start,) = values("dmr-heartbeat.service", "Service", "ExecStart")
    assert start.startswith(f"{PLACEHOLDER}/.venv-kernel/bin/python {PLACEHOLDER}/deploy/heartbeat_watch.py"), start
    env_files = values("dmr-heartbeat.service", "Service", "EnvironmentFile")
    assert env_files and all(v.startswith("-") for v in env_files), "告警地址文件可以不存在（前缀 -），且不在仓库里"
    assert not values("dmr-heartbeat.service", "Install", "WantedBy"), "由定时器触发，不单独开机自启"
    assert values("dmr-heartbeat.timer", "Timer", "Unit") == ["dmr-heartbeat.service"]
    assert values("dmr-heartbeat.timer", "Timer", "OnUnitActiveSec") == ["30s"]
    assert values("dmr-heartbeat.timer", "Timer", "OnBootSec")
    assert values("dmr-heartbeat.timer", "Install", "WantedBy") == ["timers.target"]


def test_no_units_beyond_the_five_templates():
    assert sorted(p.name for p in SYSTEMD.iterdir()) == sorted(UNITS)


# ------------------------------------------------------------------ README

def test_readme_documents_scripts_environment_variables_units_and_the_drill():
    text = (DEPLOY / "README.md").read_text(encoding="utf-8")
    for needle in (*SCRIPTS, *UNITS, "var/run", "var/log", "kernel.nosupervise", "DMR_STANDIN_PORT", "DMR_KERNEL_PORT",
                   "DMR_CONSOLE_PORT", "DMR_VAR_DIR", "DMR_STANDIN_FEED", "DMR_KERNEL_SHELL", "systemctl --user",
                   "daemon-reload", "enable-linger", "@WEBUI_ROOT@", "heartbeat_watch.py", "10MB"):
        assert needle in text, f"deploy/README.md 缺少 {needle!r}"


def test_project_readme_still_points_at_the_deploy_scripts():
    assert "deploy/" in (WEBUI_ROOT / "README.md").read_text(encoding="utf-8")
