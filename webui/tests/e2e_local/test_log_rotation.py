"""三个进程各自的日志轮转配置：var/log/<名>.log，10MB×5，时间一律 UTC（不随本机时区，本机是 +07）。

在子进程里用各自的环境调用各自 __main__ 的 configure_logging，再读日志文件。内核早已如此，替身交易所与控制台是 T0.25 补上的。
"""
from __future__ import annotations

import json
import re
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest
from deploy_kit import CONSOLE_PY, KERNEL_PY, UTC_LINE, WEBUI_ROOT

pytestmark = pytest.mark.e2e

MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5

SNIPPETS = {
    "standin": (KERNEL_PY, "standin.log", """
        from dmr_standin import __main__ as entry
        entry.configure_logging(Path(sys.argv[1]) / "log")
    """),
    "console": (CONSOLE_PY, "console.log", """
        from dmr_console import __main__ as entry
        from dmr_console.settings import load_settings
        entry.configure_logging(load_settings({"DMR_VAR_DIR": sys.argv[1]}, webui_root=Path(sys.argv[2])))
    """),
    "kernel": (KERNEL_PY, "kernel.log", """
        from dmr_kernel import __main__ as entry
        from dmr_kernel.settings import load_settings
        entry.configure_logging(load_settings({"DMR_VAR_DIR": sys.argv[1]}, webui_root=Path(sys.argv[2])))
    """),
}

COMMON_TAIL = """
    log = logging.getLogger("rotation_probe")
    log.info("hello from the probe")
    for handler in logging.getLogger().handlers:
        handler.flush()
    files = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    print(json.dumps([[h.baseFilename, h.maxBytes, h.backupCount] for h in files]))
"""


def run_probe(name: str, var: Path) -> list[list]:
    interpreter, _, body = SNIPPETS[name]
    code = ("import json, logging, logging.handlers, sys\nfrom pathlib import Path\n" + textwrap.dedent(body)
            + textwrap.dedent(COMMON_TAIL))
    result = subprocess.run([str(interpreter), "-B", "-c", code, str(var), str(WEBUI_ROOT)], cwd=WEBUI_ROOT,
                            env={"PATH": "/usr/bin:/bin", "TZ": "Asia/Bangkok", "LANG": "C.UTF-8"},
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("name", ["standin", "console", "kernel"])
def test_each_process_logs_to_var_log_with_rotation_10mb_by_5_and_utc_timestamps(name, tmp_path):
    var = tmp_path / "var"
    handlers = run_probe(name, var)
    expected_file = var / "log" / SNIPPETS[name][1]
    assert handlers == [[str(expected_file), MAX_BYTES, BACKUPS]]
    text = expected_file.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "hello from the probe" in line)
    assert re.match(UTC_LINE, line), f"日志行应以 UTC 时间戳开头：{line!r}"
    stamp = datetime.strptime(line[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    skew = abs((datetime.now(timezone.utc) - stamp).total_seconds())
    assert skew < 120, f"时间戳 {line[:20]} 与 UTC 现在相差 {skew:.0f} 秒（TZ=Asia/Bangkok 下应仍是 UTC，不是 +07）"
    assert "rotation_probe" in line and line.rstrip().endswith("hello from the probe")


@pytest.mark.parametrize("name", ["standin", "console", "kernel"])
def test_configuring_twice_does_not_stack_file_handlers(name, tmp_path):
    interpreter, _, body = SNIPPETS[name]
    snippet = textwrap.dedent(body)
    code = ("import json, logging, logging.handlers, sys\nfrom pathlib import Path\n" + snippet + snippet
            + "\nprint(sum(isinstance(h, logging.handlers.RotatingFileHandler) for h in logging.getLogger().handlers))\n")
    result = subprocess.run([str(interpreter), "-B", "-c", code, str(tmp_path / "var"), str(WEBUI_ROOT)], cwd=WEBUI_ROOT,
                            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "1"


# ------------------------------------------------------------------ 替身交易所的日志目录选项

STANDIN_LOG_DIR_PROBE = """
import sys
from dmr_standin.__main__ import parse_args
print(parse_args(sys.argv[1:]).log_dir)
"""


def standin_log_dir(*argv: str, var_dir: str | None) -> Path:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    if var_dir is not None:
        env["DMR_VAR_DIR"] = var_dir
    result = subprocess.run([str(KERNEL_PY), "-B", "-c", STANDIN_LOG_DIR_PROBE, *argv], cwd=WEBUI_ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return Path(result.stdout.strip())


def test_standin_log_dir_follows_dmr_var_dir_and_the_option_overrides_it(tmp_path):
    assert standin_log_dir(var_dir=None) == WEBUI_ROOT / "var" / "log"
    assert standin_log_dir(var_dir=str(tmp_path / "v")) == tmp_path / "v" / "log"
    assert standin_log_dir(var_dir="rel/var") == WEBUI_ROOT / "rel" / "var" / "log", "相对路径按工程根解析"
    assert standin_log_dir(var_dir="   ") == WEBUI_ROOT / "var" / "log", "只有空白等于没设"
    explicit = tmp_path / "elsewhere"
    assert standin_log_dir("--log-dir", str(explicit), var_dir=str(tmp_path / "v")) == explicit
