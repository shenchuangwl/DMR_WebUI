"""名单同步测试的夹具：临时账本、时钟、总线、合约清单、假告警器与工厂。"""
from __future__ import annotations

import pytest

from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus
from dmr_kernel.exchange.rules import InstrumentRules
from listsync_world import SAMPLE_NOW_MS, SAMPLE_SYMBOLS, URL, FakeHttp, FakeNotifier, make_instrument
from tests.support.ledger import temp_ledger


@pytest.fixture
def clock():
    return ManualClock(SAMPLE_NOW_MS)


@pytest.fixture
def ledger(tmp_path, clock):
    with temp_ledger(tmp_path, clock) as opened:
        yield opened


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def rules(clock):
    table = InstrumentRules(clock)
    table._by_symbol = {s: make_instrument(s) for s in SAMPLE_SYMBOLS}  # 测试直接塞清单，不经适配层
    return table


@pytest.fixture
def config():
    return {"list_poll_s": 60, "list_member_ttl_h": 0, "list_max_new_per_poll": 40, "list_invalid_alert_min": 30}


@pytest.fixture
def make_sync(ledger, bus, clock, config, rules, notifier):
    from dmr_kernel.listsync import ListSync

    def make(*responses, **kwargs) -> tuple["ListSync", FakeHttp]:
        http = FakeHttp(*responses)
        sync = ListSync(url=URL, ledger=ledger, bus=bus, clock=clock, config_get=config.__getitem__, rules=rules,
                        notifier=notifier, http=http, **kwargs)
        return sync, http

    return make
