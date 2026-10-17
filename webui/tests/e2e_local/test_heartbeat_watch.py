"""deploy/heartbeat_watch.py：dmr-heartbeat.timer 每 30 秒运行的心跳看护。

内核心跳文件（var/kernel/heartbeat.json）超过 60 秒没更新就退出 1 并推送告警（只读环境变量 DMR_ALERT_URLS）；
持续缺失期间按重复间隔限速，恢复后清零。推送函数可注入（测试用假的），告警地址（里面有令牌）绝不输出。
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import time
from pathlib import Path

import pytest
from deploy_kit import DEPLOY, KERNEL_PY

from dmr_kernel.alerts.heartbeat import Heartbeat
from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.types import AlertLevel

pytestmark = pytest.mark.e2e

URL = "tgram://123456:SECRET-TOKEN/42"
T0 = 1_760_000_000_000


@pytest.fixture(scope="module")
def watch():
    spec = importlib.util.spec_from_file_location("heartbeat_watch", DEPLOY / "heartbeat_watch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_beat(path: Path, at_ms: int) -> None:
    Heartbeat(path, ManualClock(at_ms), pid=4242).beat()


class Sender:
    def __init__(self, *, delivered: bool = True) -> None:
        self.calls: list[tuple[str, str, str, AlertLevel]] = []
        self.delivered = delivered

    def __call__(self, url: str, title: str, body: str, level: AlertLevel) -> bool:
        self.calls.append((url, title, body, level))
        return self.delivered


def run_watch(watch, tmp_path, *, now_ms, urls=(URL,), sender, repeat_s=900, alert_after_s=60):
    out = io.StringIO()
    code = watch.run(tmp_path / "heartbeat.json", now_ms=now_ms, alert_after_s=alert_after_s, urls=urls, sender=sender,
                     state_path=tmp_path / "state.json", repeat_s=repeat_s, out=out)
    return code, out.getvalue()


def test_a_fresh_heartbeat_is_silent_and_sends_nothing(watch, tmp_path):
    write_beat(tmp_path / "heartbeat.json", T0)
    sender = Sender()
    code, output = run_watch(watch, tmp_path, now_ms=T0 + 30_000, sender=sender)
    assert code == 0 and output == "" and sender.calls == []


def test_exactly_at_the_threshold_is_not_stale_and_one_millisecond_later_is(watch, tmp_path):
    write_beat(tmp_path / "heartbeat.json", T0)
    sender = Sender()
    assert run_watch(watch, tmp_path, now_ms=T0 + 60_000, sender=sender)[0] == 0
    code, output = run_watch(watch, tmp_path, now_ms=T0 + 60_001, sender=sender)
    assert code == 1 and "心跳" in output and "60" in output
    assert len(sender.calls) == 1


def test_a_stale_heartbeat_pushes_a_critical_alert_to_every_url_without_printing_the_urls(watch, tmp_path):
    write_beat(tmp_path / "heartbeat.json", T0)
    sender = Sender()
    urls = (URL, "mailto://user:SECRET-PASSWORD@example.org")
    code, output = run_watch(watch, tmp_path, now_ms=T0 + 95_000, urls=urls, sender=sender)
    assert code == 1
    assert [call[0] for call in sender.calls] == list(urls)
    assert all(call[3] is AlertLevel.CRITICAL for call in sender.calls)
    assert "95" in sender.calls[0][2], "正文要写明已停了多少秒"
    shown = output + "".join(call[1] + call[2] for call in sender.calls)
    assert "SECRET" not in shown, "告警地址里有令牌：不能出现在输出或告警正文里"


def test_a_missing_or_damaged_heartbeat_counts_as_stale(watch, tmp_path):
    sender = Sender()
    code, output = run_watch(watch, tmp_path, now_ms=T0, sender=sender)
    assert code == 1 and "没有" in output and len(sender.calls) == 1
    (tmp_path / "heartbeat.json").write_text("{not json")
    (tmp_path / "state.json").unlink()
    code, output = run_watch(watch, tmp_path, now_ms=T0, sender=Sender())
    assert code == 1


def test_repeats_are_rate_limited_and_the_window_restarts_after_recovery(watch, tmp_path):
    beat = tmp_path / "heartbeat.json"
    write_beat(beat, T0)
    sender = Sender()
    stale_at = T0 + 100_000
    assert run_watch(watch, tmp_path, now_ms=stale_at, sender=sender)[0] == 1
    assert len(sender.calls) == 1
    assert run_watch(watch, tmp_path, now_ms=stale_at + 30_000, sender=sender)[0] == 1
    assert run_watch(watch, tmp_path, now_ms=stale_at + 899_000, sender=sender)[0] == 1
    assert len(sender.calls) == 1, "重复间隔内不再推送（定时器每 30 秒跑一次，不能刷屏）"
    assert run_watch(watch, tmp_path, now_ms=stale_at + 900_000, sender=sender)[0] == 1
    assert len(sender.calls) == 2, "过了重复间隔，仍然没有心跳就再提醒一次"

    write_beat(beat, stale_at + 950_000)  # 恢复
    assert run_watch(watch, tmp_path, now_ms=stale_at + 960_000, sender=sender)[0] == 0
    assert run_watch(watch, tmp_path, now_ms=stale_at + 1_100_000, sender=sender)[0] == 1
    assert len(sender.calls) == 3, "恢复后再次停止，应立即告警，不受上一轮重复间隔的影响"


def test_a_push_that_was_not_delivered_is_retried_on_the_next_run(watch, tmp_path):
    write_beat(tmp_path / "heartbeat.json", T0)
    failing = Sender(delivered=False)
    assert run_watch(watch, tmp_path, now_ms=T0 + 100_000, sender=failing)[0] == 1
    working = Sender()
    assert run_watch(watch, tmp_path, now_ms=T0 + 130_000, sender=working)[0] == 1
    assert len(working.calls) == 1, "没送达就不记已告警，下次继续试"


def test_a_sender_that_raises_does_not_break_the_check(watch, tmp_path):
    write_beat(tmp_path / "heartbeat.json", T0)

    def boom(url, title, body, level):
        raise RuntimeError(f"connection to {url} failed")

    code, output = run_watch(watch, tmp_path, now_ms=T0 + 100_000, sender=boom)
    assert code == 1 and "SECRET" not in output
    assert "RuntimeError" in output, "只记异常类型，不记内容（内容里可能带地址）"


def test_without_urls_it_still_reports_but_sends_nothing(watch, tmp_path):
    write_beat(tmp_path / "heartbeat.json", T0)
    sender = Sender()
    code, output = run_watch(watch, tmp_path, now_ms=T0 + 100_000, urls=(), sender=sender)
    assert code == 1 and "心跳" in output and sender.calls == []


# ------------------------------------------------------------------ 作为命令运行（systemd 就是这样调用它的）

def run_cli(tmp_path, *args, **env):
    full_env = {"PATH": "/usr/bin:/bin", "DMR_VAR_DIR": str(tmp_path / "var"), "LANG": "C.UTF-8", **env}
    return subprocess.run([str(KERNEL_PY), "-B", str(DEPLOY / "heartbeat_watch.py"), *args], env=full_env, cwd="/",
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_cli_exit_codes_follow_the_heartbeat_file_under_var_kernel(tmp_path):
    beat = tmp_path / "var" / "kernel" / "heartbeat.json"
    write_beat(beat, int(time.time() * 1000) - 5_000)
    fresh = run_cli(tmp_path)
    assert fresh.returncode == 0 and fresh.stdout == "" and fresh.stderr == ""
    write_beat(beat, int(time.time() * 1000) - 120_000)
    stale = run_cli(tmp_path)
    assert stale.returncode == 1 and "心跳" in stale.stderr
    assert not (tmp_path / "var" / "run" / "heartbeat_alert.json").exists(), "没配告警地址就没有推送，也就不记已告警"


def test_cli_without_a_heartbeat_file_is_stale_and_bad_options_are_refused(tmp_path):
    missing = run_cli(tmp_path)
    assert missing.returncode == 1 and "没有" in missing.stderr
    for bad in (("--alert-after-s", "abc"), ("--alert-after-s", "0"), ("--repeat-s", "-1"), ("--no-such-option",)):
        assert run_cli(tmp_path, *bad).returncode == 2, bad


def test_cli_state_file_is_written_only_after_a_delivered_push_and_removed_on_recovery(tmp_path, watch):
    beat = tmp_path / "var" / "kernel" / "heartbeat.json"
    state = tmp_path / "var" / "run" / "heartbeat_alert.json"
    write_beat(beat, T0)
    sender = Sender()
    out = io.StringIO()
    argv_env = {"DMR_VAR_DIR": str(tmp_path / "var"), "DMR_ALERT_URLS": f" {URL} , ,mailto://a:b@example.org "}
    assert watch.main([], env=argv_env, now_ms=T0 + 100_000, sender=sender, out=out) == 1
    assert [call[0] for call in sender.calls] == [URL, "mailto://a:b@example.org"], "地址按逗号分隔，去掉空白与空项"
    assert json.loads(state.read_text()) == {"last_alert_ms": T0 + 100_000}
    write_beat(beat, T0 + 110_000)
    assert watch.main([], env=argv_env, now_ms=T0 + 115_000, sender=sender, out=out) == 0
    assert not state.exists()
