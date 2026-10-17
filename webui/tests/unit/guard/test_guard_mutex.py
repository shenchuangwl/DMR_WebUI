"""LegacyMutex：旧入口 7 把单实例锁的内核侧（报告 5.1「第一层」与「停用标记」）。

只用 tmp_path 下模拟的 data/runtime_state；另一个进程持锁用 subprocess 起当前（内核环境的）解释器。
"""
from __future__ import annotations

import ast
import builtins
import contextlib
import fcntl
import os
import shutil
import stat
from pathlib import Path

import pytest

from dmr_kernel.core.guard import LegacyMutex
from lock_kit import MARKER, NAMES, lock_path, make_runtime_dir, put_marker


@pytest.fixture
def mutex(runtime_dir):
    held = LegacyMutex(runtime_dir, enabled=True)
    yield held
    held.release_all()


def listing(directory: Path) -> dict[str, tuple[int, int, int]]:
    """目录里每个文件的（inode、修改时间、大小），用来证明某个调用没有改动它们。"""
    out = {}
    for entry in sorted(os.listdir(directory)):
        info = os.stat(directory / entry)
        out[entry] = (info.st_ino, info.st_mtime_ns, info.st_size)
    return out


def mentions(problems: list[str], *needles: str) -> list[str]:
    return [p for p in problems if all(n in p for n in needles)]


# ---------------------------------------------------------------- 常量


def test_lock_names_are_the_seven_legacy_entrypoints():
    assert LegacyMutex.LOCK_NAMES == NAMES
    assert len(set(LegacyMutex.LOCK_NAMES)) == 7


# ---------------------------------------------------------------- 取锁


def test_acquire_all_holds_seven_exclusive_locks_and_records_our_pid(mutex, runtime_dir):
    assert mutex.acquire_all() == []

    assert mutex.held == NAMES
    for name in NAMES:
        path = lock_path(runtime_dir, name)
        assert path.read_text().strip() == str(os.getpid())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # 另开一个文件描述再取同一把锁（旧入口就是这么做的）必须被拒
        fd = os.open(path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_acquire_all_reuses_an_existing_lock_file_and_overwrites_the_stale_pid(mutex, runtime_dir):
    stale = lock_path(runtime_dir, "main")
    stale.write_text("99999")
    inode = stale.stat().st_ino

    assert mutex.acquire_all() == []

    assert stale.stat().st_ino == inode  # 沿用旧入口留下的文件，不另建
    assert stale.read_text().strip() == str(os.getpid())


def test_acquire_all_is_idempotent_for_locks_already_held(mutex, runtime_dir):
    assert mutex.acquire_all() == []
    before = listing(runtime_dir)

    assert mutex.acquire_all() == []

    assert mutex.held == NAMES
    assert listing(runtime_dir) == before
    assert mutex.check() == []


@pytest.mark.parametrize("name", NAMES)
def test_a_lock_held_by_another_process_is_reported_with_its_pid(mutex, runtime_dir, holders, name):
    holder = holders(lock_path(runtime_dir, name))

    problems = mutex.acquire_all()

    assert len(problems) == 1
    assert name in problems[0]
    assert str(holder.pid) in problems[0]
    assert mutex.held == tuple(n for n in NAMES if n != name)  # 其余六把照常持有
    # 取不到的那把在复核里同样是问题
    assert mentions(mutex.check(), name)


def test_a_busy_lock_without_a_recorded_pid_says_the_pid_is_unknown(mutex, runtime_dir, holders):
    holders(lock_path(runtime_dir, "first_short_term"), write_pid=False)

    problems = mutex.acquire_all()

    assert len(problems) == 1
    assert "first_short_term" in problems[0]
    assert "未知" in problems[0]


def test_every_busy_lock_is_reported_and_none_is_held(mutex, runtime_dir, holders):
    running = {name: holders(lock_path(runtime_dir, name)) for name in NAMES}

    problems = mutex.acquire_all()

    assert len(problems) == 7
    for name, holder in running.items():
        assert mentions(problems, name, str(holder.pid))
    assert mutex.held == ()


def test_a_busy_lock_can_be_taken_once_the_old_entry_exits(mutex, runtime_dir, holders):
    holder = holders(lock_path(runtime_dir, "bit_long_term"))
    assert mutex.acquire_all()
    assert "bit_long_term" not in mutex.held

    holder.stop()

    assert mutex.acquire_all() == []
    assert mutex.held == NAMES


# ---------------------------------------------------------------- 释放


def test_release_all_frees_every_lock_for_other_processes(mutex, runtime_dir, holders):
    assert mutex.acquire_all() == []

    mutex.release_all()

    assert mutex.held == ()
    for name in NAMES:  # 旧入口现在取得到了：持锁子进程能就绪就是证明
        holders(lock_path(runtime_dir, name)).stop()


def test_release_all_is_safe_before_acquire_and_when_repeated(mutex):
    mutex.release_all()
    assert mutex.acquire_all() == []
    mutex.release_all()
    mutex.release_all()
    assert mutex.held == ()


def test_locks_can_be_acquired_again_after_release(mutex):
    assert mutex.acquire_all() == []
    mutex.release_all()
    assert mutex.acquire_all() == []
    assert mutex.held == NAMES


# ---------------------------------------------------------------- 目录自检


def test_a_missing_directory_is_reported_and_never_created(tmp_path):
    missing = tmp_path / "typo" / "runtime_state"
    mutex = LegacyMutex(missing, enabled=True)

    problems = mutex.acquire_all()

    assert len(problems) == 1 and "不存在" in problems[0] and str(missing) in problems[0]
    assert not (tmp_path / "typo").exists()
    assert mutex.held == ()
    assert len(mutex.check()) == 1 and "不存在" in mutex.check()[0]
    mutex.release_all()


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o777, 0o701, 0o500, 0o600, 0o000])
def test_the_directory_must_be_exactly_0700(runtime_dir, mode):
    mutex = LegacyMutex(runtime_dir, enabled=True)
    os.chmod(runtime_dir, mode)
    try:
        problems = mutex.acquire_all()
        assert len(problems) == 1
        assert "0700" in problems[0] and f"{mode:04o}" in problems[0]
        assert mutex.held == ()
        assert mentions(mutex.check(), "0700")
    finally:
        os.chmod(runtime_dir, 0o700)
    assert not list(runtime_dir.glob("instance_*.lock"))  # 权限不对时一把锁文件都没有建
    assert mutex.acquire_all() == []  # 改回 0700 就通过
    mutex.release_all()


def test_a_regular_file_in_place_of_the_directory_is_rejected(tmp_path):
    path = tmp_path / "runtime_state"
    path.write_text("not a directory")
    mutex = LegacyMutex(path, enabled=True)

    problems = mutex.acquire_all()

    assert len(problems) == 1 and "不是目录" in problems[0]
    assert path.read_text() == "not a directory"


# ---------------------------------------------------------------- 周期复核

def test_check_is_clean_when_every_lock_and_the_marker_are_in_place(mutex):
    assert mutex.acquire_all() == []
    assert mutex.check() == []


def test_check_before_any_lock_is_taken_reports_all_seven_as_not_held(mutex):
    problems = mutex.check()

    assert len(problems) == 7
    for name in NAMES:
        assert mentions(problems, name, "没有持有")


def test_check_reports_a_missing_stop_marker(mutex, runtime_dir):
    assert mutex.acquire_all() == []
    (runtime_dir / MARKER).unlink()

    problems = mutex.check()

    assert len(problems) == 1 and MARKER in problems[0]


def test_the_stop_marker_is_never_created_or_deleted_by_the_kernel(tmp_path):
    bare = make_runtime_dir(tmp_path / "bare", marker=False)
    absent = LegacyMutex(bare, enabled=True)
    absent.acquire_all()
    absent.check()
    absent.release_all()
    assert not (bare / MARKER).exists()

    kept = make_runtime_dir(tmp_path / "kept")
    content = (kept / MARKER).read_text()
    present = LegacyMutex(kept, enabled=True)
    present.acquire_all()
    assert present.check() == []
    present.release_all()
    assert (kept / MARKER).read_text() == content


def test_check_reports_a_deleted_lock_file(mutex, runtime_dir):
    assert mutex.acquire_all() == []
    lock_path(runtime_dir, "bit_short_term").unlink()

    problems = mutex.check()

    assert len(problems) == 1
    assert "bit_short_term" in problems[0] and "删除" in problems[0]


@pytest.mark.parametrize("how", ["delete_then_create", "rename_away_then_create"])
def test_check_reports_a_recreated_lock_file(mutex, runtime_dir, how):
    assert mutex.acquire_all() == []
    path = lock_path(runtime_dir, "legacy_long_term")
    if how == "delete_then_create":
        path.unlink()
    else:
        path.rename(runtime_dir / "moved-away")
    path.write_text("")  # 旧入口 open('a+') 会这样建出一个新文件，并取到它上面的锁

    problems = mutex.check()

    assert len(problems) == 1
    assert "legacy_long_term" in problems[0] and "替换" in problems[0]


def test_check_reports_every_lock_when_the_directory_content_is_replaced(mutex, runtime_dir):
    assert mutex.acquire_all() == []
    shutil.rmtree(runtime_dir)
    runtime_dir.mkdir()
    os.chmod(runtime_dir, 0o700)
    put_marker(runtime_dir)

    problems = mutex.check()

    assert len(problems) == 7
    for name in NAMES:
        assert mentions(problems, name)


def test_check_reports_a_removed_directory_once(mutex, runtime_dir):
    assert mutex.acquire_all() == []
    shutil.rmtree(runtime_dir)

    problems = mutex.check()

    assert len(problems) == 1 and "不存在" in problems[0]


def test_check_reports_a_directory_that_lost_its_0700_mode(mutex, runtime_dir):
    assert mutex.acquire_all() == []
    os.chmod(runtime_dir, 0o755)
    try:
        problems = mutex.check()
    finally:
        os.chmod(runtime_dir, 0o700)

    assert len(problems) == 1 and "0700" in problems[0]


def test_check_reports_all_problems_at_once(mutex, runtime_dir):
    assert mutex.acquire_all() == []
    lock_path(runtime_dir, "main").unlink()
    (runtime_dir / MARKER).unlink()

    problems = mutex.check()

    assert len(problems) == 2
    assert mentions(problems, "main") and mentions(problems, MARKER)


def test_check_changes_nothing_on_disk(mutex, runtime_dir):
    assert mutex.acquire_all() == []
    lock_path(runtime_dir, "main").unlink()
    (runtime_dir / MARKER).unlink()
    before = listing(runtime_dir)

    assert mutex.check()

    assert listing(runtime_dir) == before
    assert not (runtime_dir / MARKER).exists()
    assert not lock_path(runtime_dir, "main").exists()


# ---------------------------------------------------------------- 重新取锁

def test_acquire_all_takes_over_a_replaced_lock_file(mutex, runtime_dir):
    assert mutex.acquire_all() == []
    path = lock_path(runtime_dir, "first_long_term")
    path.unlink()
    path.write_text("")
    assert mutex.check()

    assert mutex.acquire_all() == []

    assert mutex.check() == []
    assert mutex.held == NAMES
    assert path.read_text().strip() == str(os.getpid())


def test_acquire_all_reports_the_old_entry_that_got_the_recreated_file(mutex, runtime_dir, holders):
    assert mutex.acquire_all() == []
    path = lock_path(runtime_dir, "legacy_short_term")
    path.unlink()
    holder = holders(path)  # 旧入口在新文件上取到了锁

    problems = mutex.acquire_all()

    assert len(problems) == 1
    assert "legacy_short_term" in problems[0] and str(holder.pid) in problems[0]
    assert "legacy_short_term" not in mutex.held  # 过期的那把已经丢掉，没拿着假象
    assert mentions(mutex.check(), "legacy_short_term")


# ---------------------------------------------------------------- 与真实旧代码对拍

def load_legacy_single_instance(dmr_root: Path, state_root: Path):
    """只读 DMR 的 common/atomic_state.py 源码文本，按名字取出 state_path 与 single_instance，在隔离命名空间里编译执行。

    不 import 任何 DMR 模块，不写字节码；STATE_ROOT 指向临时目录，绝不碰真实的 data/runtime_state。
    """
    source = dmr_root / "common" / "atomic_state.py"
    if not source.is_file():
        pytest.skip("找不到 DMR 的 common/atomic_state.py，跳过与旧代码的对拍")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("state_path", "single_instance")]
    assert {n.name for n in wanted} == {"state_path", "single_instance"}
    namespace = {"os": os, "fcntl": fcntl, "Path": Path, "contextmanager": contextlib.contextmanager,
                 "STATE_ROOT": state_root}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(source), "exec"), namespace)  # noqa: S102
    assert namespace["STATE_ROOT"] == state_root
    assert not state_root.resolve().is_relative_to((dmr_root / "data").resolve())  # 绝不是真实的 data/runtime_state
    return namespace["single_instance"]


@pytest.fixture
def legacy_single_instance(dmr_root, runtime_dir):
    return load_legacy_single_instance(dmr_root, runtime_dir)


@pytest.mark.parametrize("name", NAMES)
def test_the_real_legacy_single_instance_lock_is_refused_while_the_kernel_holds_it(
        mutex, legacy_single_instance, name):
    assert mutex.acquire_all() == []

    with pytest.raises(RuntimeError, match="another instance"):
        with legacy_single_instance(name):
            pytest.fail("旧入口不该取到内核持有的锁")


def test_the_real_legacy_entry_runs_again_once_the_kernel_lets_go(mutex, legacy_single_instance, runtime_dir):
    assert mutex.acquire_all() == []
    mutex.release_all()

    with legacy_single_instance("bit_short_term"):
        assert lock_path(runtime_dir, "bit_short_term").read_text().strip() == str(os.getpid())

    assert mutex.acquire_all() == []  # 旧入口退出后锁又空了


def test_the_real_legacy_entry_gets_in_after_the_lock_file_is_recreated_and_check_catches_it(
        mutex, legacy_single_instance, runtime_dir):
    assert mutex.acquire_all() == []
    path = lock_path(runtime_dir, "main")
    path.unlink()  # 例如 git clean -x 或手工清理

    with legacy_single_instance("main"):  # 这正是第一层单靠锁防不住的空隙
        problems = mutex.check()

    assert len(problems) == 1 and "main" in problems[0] and "替换" in problems[0]


# ---------------------------------------------------------------- 停用：不触碰任何路径

def forbid_filesystem(patch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("旧入口互斥已停用，却触碰了路径或文件")

    for name in ("stat", "lstat", "fstat", "open", "access", "mkdir", "makedirs", "listdir", "scandir", "chmod",
                 "unlink", "remove", "rename", "replace", "rmdir", "readlink", "pread", "pwrite", "ftruncate",
                 "close"):
        patch.setattr(os, name, boom)
    patch.setattr(fcntl, "flock", boom)
    patch.setattr(builtins, "open", boom)


def test_disabled_mutex_touches_no_path_even_for_a_directory_that_does_not_exist(tmp_path, monkeypatch):
    missing = tmp_path / "never" / "created" / "runtime_state"
    mutex = LegacyMutex(missing, enabled=False)

    with monkeypatch.context() as patch:
        forbid_filesystem(patch)
        with pytest.raises(AssertionError):  # 先证明上面的拦截确实生效
            os.stat(missing)
        acquired = mutex.acquire_all()
        checked = mutex.check()
        released = mutex.release_all()

    assert acquired == [] and checked == [] and released is None
    assert mutex.held == ()
    assert not (tmp_path / "never").exists()


def test_disabled_mutex_leaves_an_existing_directory_exactly_as_it_was(runtime_dir):
    os.chmod(runtime_dir, 0o755)  # 即使权限不对、没有停用标记也不过问
    (runtime_dir / MARKER).unlink()
    before = listing(runtime_dir)
    mutex = LegacyMutex(runtime_dir, enabled=False)
    try:
        assert mutex.acquire_all() == []
        assert mutex.check() == []
        mutex.release_all()

        assert listing(runtime_dir) == before
        assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o755
        assert not list(runtime_dir.glob("instance_*.lock"))
    finally:
        os.chmod(runtime_dir, 0o700)


def test_disabled_mutex_never_blocks_the_legacy_entrypoints(runtime_dir, legacy_single_instance):
    mutex = LegacyMutex(runtime_dir, enabled=False)
    mutex.acquire_all()

    with legacy_single_instance("main"):  # 没有被占
        pass


def test_the_enabled_flag_is_exposed(tmp_path):
    assert LegacyMutex(tmp_path, enabled=True).enabled is True
    assert LegacyMutex(tmp_path, enabled=False).enabled is False
