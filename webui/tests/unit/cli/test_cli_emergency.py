"""``emergency close``（内核不可用时的命令行应急路径，T1.7 第二条）。

顺序：写停用标记 -> 按进程号文件 SIGTERM 内核、卡死则 SIGKILL 并确认进程已不在 -> 取 var/kernel.lock（取不到拒绝）-> 用
``WriteCapability("emergency")`` 建适配层、读全账户持仓逐仓列出、操作者逐个确认后发市价平仓单（应急编号）-> 某仓归零后撤该合约
该方向上带系统前缀的条件单 -> 打印结果并写 var/log/emergency.log；不自动删停用标记；不打开数据库。适配层与进程操作都是假的。
"""
from __future__ import annotations

import fcntl
import os

import pytest

from emergency_kit import KERNEL_PID, Emergency, FakeExchange, World, algo, pos
from cli_kit import Rig
from dmr_kernel.core.lifecycle import acquire_kernel_lock
from dmr_kernel.exchange import ids

SYSTEM_STOP = "DKAx-1-abc-s"
OTHER_SYSTEM_STOP = "DKAx-1-def-s"


@pytest.fixture
def em(tmp_path):
    rig = Rig(tmp_path)
    exchange = FakeExchange(
        [pos("BTCUSDT", "LONG", "1.5"), pos("ETHUSDT", "SHORT", "20"), pos("BTCUSDT", "SHORT", "0.2")],
        [algo("BTCUSDT", "LONG", SYSTEM_STOP, "1"),            # 该仓的系统止损：要撤
         algo("BTCUSDT", "SHORT", OTHER_SYSTEM_STOP, "2"),     # 另一个方向（它自己也会被平，自己的单跟着撤）
         algo("BTCUSDT", "LONG", None, "3"),                   # 用户自己的条件单：不动
         algo("BTCUSDT", "LONG", "web_abc123", "4"),           # 用户网页单：不动
         algo("ETHUSDT", "SHORT", "DKAx-1-ghi-s", "5")])
    return Emergency(rig, exchange, World())


def writes(em, name):
    return em.exchange.write_calls(name)


# ---------------------------------------------------------------- 第 1 步：停内核


def test_writes_the_stop_marker_before_any_signal_is_sent(em):
    em.write_pid()
    seen = {}
    em.world.on_signal = lambda pid, sig: seen.setdefault("marker", em.nosupervise.exists())
    result = em.run(["--yes"])
    assert result.code == 0, result.text
    assert seen["marker"] is True


def test_sigterms_the_kernel_named_in_the_pid_file_and_waits_for_it_to_leave(em):
    em.write_pid()
    em.run(["--yes"])
    assert em.world.signals == [(KERNEL_PID, 15)]
    assert KERNEL_PID not in em.world.alive


def test_escalates_to_sigkill_when_the_kernel_will_not_leave_and_confirms_it_is_gone(em):
    em.world.term_works = False
    em.write_pid()
    result = em.run(["--yes"])
    assert result.code == 0, result.text
    assert em.world.signals == [(KERNEL_PID, 15), (KERNEL_PID, 9)]
    assert KERNEL_PID not in em.world.alive
    assert sum(em.world.sleeps) >= 30  # SIGTERM 后给足了时间才升级


def test_refuses_to_go_on_if_the_kernel_survives_sigkill(em):
    em.world.term_works = False
    em.world.kill_works = False
    em.write_pid()
    result = em.run(["--yes"])
    assert result.code == 1 and "仍然存在" in result.err
    assert em.factory_calls == 0 and not em.exchange.writes


def test_never_signals_a_process_that_is_not_the_kernel(em):
    em.world.cmdline = "/usr/bin/vim notes.txt"
    em.write_pid()
    result = em.run(["--yes"])
    assert result.code == 1 and em.world.signals == []
    assert em.factory_calls == 0 and not em.exchange.writes
    assert "不是内核" in result.err


def test_unreadable_command_line_of_a_live_process_is_also_refused(em):
    em.world.cmdline = None
    em.write_pid()
    result = em.run(["--yes"])
    assert result.code == 1 and em.world.signals == []


def test_a_stale_pid_file_is_fine(em):
    em.world.alive.clear()
    em.write_pid()
    result = em.run(["--yes"])
    assert result.code == 0 and em.world.signals == []


def test_no_pid_file_is_fine_and_the_marker_is_still_written(em):
    result = em.run(["--yes"])
    assert result.code == 0 and em.nosupervise.exists()


@pytest.mark.parametrize("junk", ["", "abc", "-5", "0"])
def test_a_junk_pid_file_is_treated_as_no_pid_and_nothing_is_signalled(em, junk):
    em.write_pid(junk)
    result = em.run(["--yes"])
    assert result.code == 0 and em.world.signals == []


# ---------------------------------------------------------------- 第 2 步：内核锁


def test_refuses_when_the_kernel_lock_cannot_be_taken(em):
    holder = acquire_kernel_lock(em.var / "kernel.lock")  # 别的进程（这里是本测试）占着锁
    try:
        result = em.run(["--yes"])
    finally:
        holder.release()
    assert result.code == 1 and "内核锁" in result.err
    assert em.factory_calls == 0 and not em.exchange.writes
    assert em.nosupervise.exists()  # 标记已经写了：不让守护在我们拿不到锁时再拉起内核


def test_holds_the_lock_while_working_and_releases_it_at_the_end(em):
    held = {}
    original = em.exchange._create_market_order

    async def spy(**kw):
        fd = os.open(em.var / "kernel.lock", os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held["during"] = False
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                held["during"] = True
        finally:
            os.close(fd)
        return await original(**kw)

    em.exchange._create_market_order = spy
    em.run(["--yes"])
    assert held["during"] is True
    after = acquire_kernel_lock(em.var / "kernel.lock")  # 结束后释放：能再取到
    after.release()


# ---------------------------------------------------------------- 第 3 步：平仓


def test_builds_the_adapter_with_the_emergency_capability_only(em):
    em.run(["--yes"])
    assert len(em.capabilities) == 1 and em.capabilities[0].owner == "emergency"


def test_closes_every_position_with_a_market_order_in_the_opposite_direction(em):
    result = em.run(["--yes"])
    assert result.code == 0, result.text
    orders = sorted((c["symbol"], c["position_side"].value, c["side"].value, str(c["qty"])) for c in writes(em, "create_market_order"))
    assert orders == [("BTCUSDT", "LONG", "SELL", "1.5"), ("BTCUSDT", "SHORT", "BUY", "0.2"),
                      ("ETHUSDT", "SHORT", "BUY", "20")]
    assert em.exchange.positions == []


def test_orders_carry_distinct_emergency_ids(em):
    em.run(["--yes"])
    cids = [c["client_order_id"] for c in writes(em, "create_market_order")]
    assert len(set(cids)) == 3 and all(ids.is_emergency_id(c) and len(c) <= 36 for c in cids)


def test_cancels_only_system_prefixed_conditional_orders_of_that_symbol_and_side(em):
    em.run(["--yes"])
    cancelled = sorted(c["client_algo_id"] for c in writes(em, "cancel_algo_order"))
    assert cancelled == sorted([SYSTEM_STOP, OTHER_SYSTEM_STOP, "DKAx-1-ghi-s"])
    left = sorted(a.algo_id for a in em.exchange.algos)
    assert left == ["3", "4"]  # 用户自己的条件单原样保留


def test_does_not_cancel_a_conditional_order_before_its_position_is_zero(em):
    em.exchange.fills = False  # 市价单发出去了，但持仓一直不归零
    result = em.run(["--yes"])
    assert result.code == 1
    assert writes(em, "cancel_algo_order") == []  # 先平后撤：没归零就不撤保护
    assert "未归零" in result.out + result.err


def test_cancel_follows_the_close_of_the_same_position(em):
    em.run(["--yes"])
    ev = em.exchange.events
    assert ev.index("market:BTCUSDT:LONG") < ev.index(f"cancel_algo:{SYSTEM_STOP}")


def test_a_rejected_close_is_reported_and_leaves_the_protection_in_place(em):
    em.exchange.reject_market = True
    result = em.run(["--yes"])
    assert result.code == 1 and "无仓可平" in result.out + result.err
    assert writes(em, "cancel_algo_order") == []


def test_lists_every_position_before_asking(em):
    em.answers = ["y", "y", "y"]
    result = em.run([])
    assert result.code == 0
    assert len(em.asked) == 3
    for text in ("BTCUSDT", "ETHUSDT", "1.5", "20"):
        assert text in result.out


def test_each_position_is_confirmed_one_by_one_and_a_no_skips_it(em):
    em.answers = ["y", "n", "y"]
    result = em.run([])
    assert result.code == 1  # 有仓没处理
    assert len(writes(em, "create_market_order")) == 2
    assert len(em.exchange.positions) == 1
    assert "跳过" in result.out


def test_quit_stops_asking_and_sends_nothing_further(em):
    em.answers = ["y", "q"]
    result = em.run([])
    assert result.code == 1 and len(writes(em, "create_market_order")) == 1 and len(em.asked) == 2


def test_end_of_input_means_no(em):
    result = em.run([])
    assert result.code == 1 and writes(em, "create_market_order") == []


def test_without_yes_nothing_is_sent_unless_confirmed_with_y_or_yes(em):
    em.answers = ["Y", "yes", "是"]
    em.run([])
    assert len(writes(em, "create_market_order")) == 3


def test_no_positions_means_nothing_to_do(em):
    em.exchange.positions = []
    result = em.run(["--yes"])
    assert result.code == 0 and "没有持仓" in result.out and em.exchange.writes == []


def test_unreadable_positions_abort_without_any_write(em):
    em.exchange.positions_unknown = True
    result = em.run(["--yes"])
    assert result.code == 3 and em.exchange.writes == [] and "读不到" in result.err


# ---------------------------------------------------------------- 第 4 步：收尾


def test_prints_the_marker_reminder_and_never_removes_the_marker(em):
    result = em.run(["--yes"])
    assert "停用标记仍在，确认后删除 var/run/kernel.nosupervise 再启动内核" in result.out
    assert em.nosupervise.exists()


def test_writes_the_result_to_var_log_emergency_log(em):
    em.run(["--yes"])
    text = em.log_file.read_text(encoding="utf-8")
    for fragment in ("BTCUSDT", "ETHUSDT", "DKE-", "已撤", SYSTEM_STOP):
        assert fragment in text
    assert text.endswith("\n")


def test_the_log_is_appended_not_overwritten(em):
    em.run(["--yes"])
    first = em.log_file.read_text(encoding="utf-8")
    em.exchange.positions = [pos("SOLUSDT", "LONG", "3")]
    em.run(["--yes"])
    second = em.log_file.read_text(encoding="utf-8")
    assert second.startswith(first) and "SOLUSDT" in second


def test_does_not_need_the_local_token_and_never_opens_the_database(em):
    assert not em.rig.token_path.exists()
    result = em.run(["--yes"])
    assert result.code == 0
    created = sorted(p.name for p in em.var.rglob("*") if p.is_file())
    assert not any(n.endswith((".sqlite", ".db", "-wal", "-shm")) for n in created)


def test_the_standin_token_file_is_required_when_no_factory_is_injected(tmp_path):
    from io import StringIO
    from dmr_kernel.cli.main import main
    from dmr_kernel.cli.emergency import EmergencyDeps
    rig = Rig(tmp_path)
    world = World(kernel_alive=False)
    out, err = StringIO(), StringIO()
    code = main(["emergency", "close", "--yes"], env=rig.env, stdout=out, stderr=err,
                emergency_deps=EmergencyDeps(send_signal=world.send_signal, pid_alive=world.pid_alive,
                                             cmdline_of=world.cmdline_of, sleep=world.sleep,
                                             monotonic=world.monotonic))
    assert code == 1 and "替身" in err.getvalue() and "kernel.token" in err.getvalue()
    assert (rig.var / "run" / "kernel.nosupervise").exists()


def test_the_token_never_appears_in_the_output(em):
    secret = "SEKRET-standin-token-value"
    token = em.var / "standin" / "kernel.token"
    token.parent.mkdir(parents=True)
    token.write_text(secret + "\n")
    result = em.run(["--yes"])
    assert secret not in result.text and secret not in em.log_file.read_text()


# ---------------------------------------------------------------- 命令行形状


def test_emergency_needs_the_close_subcommand(tmp_path, capsys):
    from dmr_kernel.cli.main import main
    assert main(["emergency"], env=Rig(tmp_path).env) == 2


def test_yes_is_only_accepted_after_close(tmp_path):
    from dmr_kernel.cli.main import main
    assert main(["emergency", "--yes", "close"], env=Rig(tmp_path).env) == 2
