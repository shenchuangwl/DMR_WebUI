"""模型 1 的账本小仓储（meta 表）：出场判定进度、待补判、注入序号。"""
from __future__ import annotations

from dmr_kernel.core.types import PositionSide
from dmr_kernel.ledger import repo_m1
from tests.support.ledger import temp_ledger

L, S = PositionSide.LONG, PositionSide.SHORT


async def test_exit_progress_defaults_to_nothing(tmp_path):
    with temp_ledger(tmp_path) as ledger:
        state = await ledger.read(repo_m1.get_exit_state, "BTCUSDT", L, 1)
        assert state.judged_ms is None and state.pending == ()


async def test_progress_is_kept_per_symbol_side_and_generation_and_only_moves_forward(tmp_path):
    with temp_ledger(tmp_path) as ledger:
        await ledger.write(repo_m1.save_exit_state, "BTCUSDT", L, 1, judged_ms=500, pending=(300,))
        await ledger.write(repo_m1.save_exit_state, "BTCUSDT", L, 1, judged_ms=400, pending=(300, 450))
        state = await ledger.read(repo_m1.get_exit_state, "BTCUSDT", L, 1)
        assert state.judged_ms == 500 and state.pending == (300, 450)
        for other in (("BTCUSDT", S, 1), ("BTCUSDT", L, 2), ("ETHUSDT", L, 1)):
            assert (await ledger.read(repo_m1.get_exit_state, *other)).judged_ms is None


async def test_progress_can_be_dropped(tmp_path):
    with temp_ledger(tmp_path) as ledger:
        await ledger.write(repo_m1.save_exit_state, "BTCUSDT", L, 1, judged_ms=500, pending=())
        await ledger.write(repo_m1.drop_exit_state, "BTCUSDT", L, 1)
        await ledger.write(repo_m1.drop_exit_state, "BTCUSDT", L, 1)  # 重复无妨
        assert (await ledger.read(repo_m1.get_exit_state, "BTCUSDT", L, 1)).judged_ms is None


async def test_injection_sequence_counts_up_and_survives_reopen(tmp_path):
    with temp_ledger(tmp_path) as ledger:
        assert [await ledger.write(repo_m1.next_inject_seq) for _ in range(3)] == [1, 2, 3]
    with temp_ledger(tmp_path) as ledger:
        assert await ledger.write(repo_m1.next_inject_seq) == 4
