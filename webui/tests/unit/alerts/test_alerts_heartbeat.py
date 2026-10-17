"""alerts.heartbeat：心跳文件的原子写、权限 0600、读取与超时判定（T0.8）。"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from dmr_kernel.alerts.heartbeat import Heartbeat
from dmr_kernel.core.clock import ManualClock

START = 1_760_000_000_000


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def make(tmp_path, *, pid=4242, start=START):
    clock = ManualClock(start)
    path = tmp_path / "kernel" / "heartbeat.json"
    return Heartbeat(path, clock, pid=pid), clock, path


# ====================================================================== 写


def test_beat_writes_pid_and_clock_time_and_creates_the_directory(tmp_path):
    hb, clock, path = make(tmp_path)
    assert not path.parent.exists()
    hb.beat()
    assert json.loads(path.read_text(encoding="utf-8")) == {"pid": 4242, "at_ms": START}


def test_the_heartbeat_file_is_private(tmp_path):
    hb, _, path = make(tmp_path)
    hb.beat()
    assert mode_of(path) == 0o600
    assert mode_of(path.parent) == 0o700  # 目录是这次新建的，只给自己


def test_an_existing_directory_is_left_alone(tmp_path):
    path = tmp_path / "shared" / "heartbeat.json"
    path.parent.mkdir(mode=0o755)
    os.chmod(path.parent, 0o755)
    Heartbeat(path, ManualClock(START), pid=1).beat()
    assert mode_of(path.parent) == 0o755 and mode_of(path) == 0o600


def test_every_beat_replaces_the_previous_one(tmp_path):
    hb, clock, path = make(tmp_path)
    hb.beat()
    clock.advance(30_000)
    hb.beat()
    assert Heartbeat.read(path) == {"pid": 4242, "at_ms": START + 30_000}
    assert sorted(p.name for p in path.parent.iterdir()) == ["heartbeat.json"]  # 没有留下临时文件


def test_a_beat_replaces_a_looser_existing_file_with_a_private_one(tmp_path):
    hb, _, path = make(tmp_path)
    path.parent.mkdir()
    path.write_text("old", encoding="utf-8")
    os.chmod(path, 0o644)
    hb.beat()
    assert mode_of(path) == 0o600 and Heartbeat.read(path) == {"pid": 4242, "at_ms": START}


def test_the_write_is_atomic_temp_file_then_rename(tmp_path, monkeypatch):
    hb, clock, path = make(tmp_path)
    hb.beat()
    clock.advance(30_000)
    seen: dict = {}
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        seen["src"], seen["dst"] = Path(src), Path(dst)
        seen["src_text"] = Path(src).read_text(encoding="utf-8")  # 改名时临时文件已经写完整
        seen["dst_text"] = Path(dst).read_text(encoding="utf-8")  # 目标文件在改名前还是旧内容
        seen["src_mode"] = mode_of(Path(src))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    hb.beat()
    assert seen["dst"] == path and seen["src"].parent == path.parent and seen["src"] != path
    assert json.loads(seen["src_text"]) == {"pid": 4242, "at_ms": START + 30_000}
    assert json.loads(seen["dst_text"]) == {"pid": 4242, "at_ms": START}
    assert seen["src_mode"] == 0o600  # 临时文件从一开始就是 0600
    assert not seen["src"].exists()


def test_a_failed_beat_keeps_the_previous_one_and_leaves_no_temp_file(tmp_path, monkeypatch):
    hb, clock, path = make(tmp_path)
    hb.beat()
    clock.advance(30_000)

    def boom(*args, **kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        hb.beat()
    monkeypatch.undo()
    assert Heartbeat.read(path) == {"pid": 4242, "at_ms": START}  # 旧的完好
    assert sorted(p.name for p in path.parent.iterdir()) == ["heartbeat.json"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受目录权限限制")
def test_beating_into_a_read_only_directory_raises_instead_of_failing_silently(tmp_path):
    hb, _, path = make(tmp_path)
    hb.beat()
    os.chmod(path.parent, 0o500)
    try:
        with pytest.raises(OSError):
            hb.beat()
    finally:
        os.chmod(path.parent, 0o700)


@pytest.mark.parametrize("pid", [0, -1, True, "12", 1.5])
def test_pid_must_be_a_positive_integer(tmp_path, pid):
    with pytest.raises(ValueError):
        Heartbeat(tmp_path / "hb.json", ManualClock(START), pid=pid)  # type: ignore[arg-type]


# ====================================================================== 读


def test_read_returns_pid_and_time(tmp_path):
    hb, _, path = make(tmp_path, pid=777)
    hb.beat()
    data = Heartbeat.read(path)
    assert data == {"pid": 777, "at_ms": START}


def test_read_a_missing_file_is_none(tmp_path):
    assert Heartbeat.read(tmp_path / "nope.json") is None


@pytest.mark.parametrize("content", [
    "", "not json", "[]", "null", "42", '{"pid": 1}', '{"at_ms": 1}', '{"pid": "1", "at_ms": 2}',
    '{"pid": 1, "at_ms": "2"}', '{"pid": true, "at_ms": 2}', '{"pid": 1, "at_ms": 2.5}', '{"pid": 0, "at_ms": 2}',
    '{"pid": 1, "at_ms": -5}',
])
def test_read_a_damaged_file_is_none(tmp_path, content):
    path = tmp_path / "hb.json"
    path.write_text(content, encoding="utf-8")
    assert Heartbeat.read(path) is None


def test_read_accepts_a_directory_path_as_missing(tmp_path):
    assert Heartbeat.read(tmp_path) is None


# ====================================================================== 判定


def test_stale_reports_age_in_seconds(tmp_path):
    hb, _, path = make(tmp_path)
    hb.beat()
    assert Heartbeat.stale(path, START, 60) == (False, 0.0)
    assert Heartbeat.stale(path, START + 30_000, 60) == (False, 30.0)
    assert Heartbeat.stale(path, START + 61_500, 60) == (True, 61.5)


@pytest.mark.parametrize("age_ms, expected", [
    (59_999, False), (60_000, False), (60_001, True), (600_000, True),
])
def test_stale_boundary_is_strictly_beyond_the_alert_time(tmp_path, age_ms, expected):
    hb, _, path = make(tmp_path)
    hb.beat()
    is_stale, age = Heartbeat.stale(path, START + age_ms, 60)
    assert is_stale is expected and age == age_ms / 1000


def test_stale_without_a_heartbeat_is_stale_with_unknown_age(tmp_path):
    assert Heartbeat.stale(tmp_path / "nope.json", START, 60) == (True, None)


def test_stale_with_a_damaged_heartbeat_is_stale_with_unknown_age(tmp_path):
    path = tmp_path / "hb.json"
    path.write_text("garbage", encoding="utf-8")
    assert Heartbeat.stale(path, START, 60) == (True, None)


def test_stale_with_a_heartbeat_from_the_future_is_not_stale(tmp_path):
    hb, _, path = make(tmp_path)
    hb.beat()
    assert Heartbeat.stale(path, START - 5_000, 60) == (False, 0.0)  # 时钟回拨：距今秒数不为负


def test_stale_and_read_work_without_an_instance(tmp_path):
    """看护方（另一个进程或脚本）不持有 Heartbeat 实例，只给路径。"""
    hb, _, path = make(tmp_path)
    hb.beat()
    assert Heartbeat.read(path)["pid"] == 4242
    assert Heartbeat.stale(path, START + 1_000, 10)[0] is False


def test_the_beat_loop_pattern_keeps_the_heartbeat_fresh(tmp_path):
    """内核每 heartbeat_s 秒 beat 一次；看护方按 heartbeat_alert_after_s 判断：正常运行时从不超时，停止后超时。"""
    hb, clock, path = make(tmp_path)
    for _ in range(10):
        hb.beat()
        clock.advance(30_000)
        assert Heartbeat.stale(path, clock.now_ms(), 60)[0] is False
    clock.advance(31_000)  # 内核停了：距上一次 beat 已 61 秒
    assert Heartbeat.stale(path, clock.now_ms(), 60) == (True, 61.0)
