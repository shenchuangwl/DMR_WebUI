"""tests/support/ledger.py：临时账本帮手能打开、写、读、关闭，并能用同一文件重新打开（重启场景）。"""
from __future__ import annotations

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.ledger import repo_meta
from tests.support.ledger import START_MS, open_ledger, temp_ledger


async def test_temp_ledger_round_trip_and_reopen(tmp_path):
    clock = ManualClock(START_MS)
    with temp_ledger(tmp_path, clock) as led:
        nonce = led.nonce
        assert len(nonce) == 2
    again = open_ledger(tmp_path, clock)
    try:
        assert again.nonce == nonce
        assert await again.read(repo_meta.nonce) == nonce
    finally:
        again.close_sync()
