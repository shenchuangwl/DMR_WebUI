"""Guard.status()：界面状态条和控制接口读的观察值。读不到的项是 unknown/None，不是「正常」。"""
from __future__ import annotations

import json

from dmr_kernel.core.types import OrderOrigin, RestrictionInfo, Unknown
from guard_kit import fill_event, stream_event
from lock_kit import MARKER, NAMES
from tests.support.ledger import START_MS


async def test_before_anything_was_checked_everything_is_unknown_not_fine(make_env):
    env = make_env()
    await env.boot()

    status = env.guard.status()

    assert status["position_mode"] == "unknown" and status["position_mode_checked_ms"] is None
    assert status["restricted"] is None and status["restrictions_checked_ms"] is None
    assert status["restriction_detail"] == {}
    assert status["legacy_mutex"] == {"enabled": False, "held": [], "problems": [], "checked_ms": None}
    assert status["stream"] == {"configured": True, "state": None, "state_ms": None, "stable_since_interruption": False}
    assert status["legacy_fills"] == {"count": 0, "last_ms": None}
    assert status["periodic_ms"] is None
    json.dumps(status)  # 可直接转 JSON


async def test_after_a_periodic_run_it_reports_what_was_read(make_env):
    env = make_env()
    env.adapter.set_read("fetch_restrictions", RestrictionInfo(True, (("rule", "order_rate"), ("until", "3600"))))
    await env.boot()

    await env.guard.periodic()

    status = env.guard.status()
    assert status["position_mode"] == "hedge" and status["position_mode_checked_ms"] == START_MS
    assert status["restricted"] is True and status["restrictions_checked_ms"] == START_MS
    assert status["restriction_detail"] == {"rule": "order_rate", "until": "3600"}
    assert status["periodic_ms"] == START_MS
    json.dumps(status)


async def test_it_reports_a_position_mode_that_is_not_hedge(make_env):
    env = make_env()
    env.adapter.set_read("fetch_position_mode", False)
    await env.boot()

    await env.guard.periodic()

    assert env.guard.status()["position_mode"] == "one_way"


async def test_a_failed_read_after_a_good_one_goes_back_to_unknown(make_env):
    env = make_env()
    await env.boot()
    await env.guard.periodic()
    env.adapter.set_read("fetch_position_mode", Unknown("超时"))
    env.clock.advance(60_000)

    await env.guard.periodic()

    status = env.guard.status()
    assert status["position_mode"] == "unknown"
    assert status["position_mode_checked_ms"] == START_MS  # 最近一次读到的时间不变


async def test_in_live_it_lists_the_held_locks_and_then_the_problems(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()

    status = env.guard.status()["legacy_mutex"]
    assert status == {"enabled": True, "held": list(NAMES), "problems": [], "checked_ms": START_MS}

    (env.runtime_dir / MARKER).unlink()
    env.clock.advance(60_000)
    await env.guard.periodic()

    status = env.guard.status()["legacy_mutex"]
    assert len(status["problems"]) == 1 and MARKER in status["problems"][0]
    assert status["checked_ms"] == START_MS + 60_000
    json.dumps(env.guard.status())


async def test_it_tracks_the_stream_and_the_legacy_fills(make_env):
    env = make_env()
    await env.boot()

    await env.emit(stream_event("interrupted", "静默", at_ms=123))
    assert env.guard.status()["stream"] == {"configured": True, "state": "interrupted", "state_ms": 123,
                                            "stable_since_interruption": False}

    await env.emit(stream_event("recovered_stable", at_ms=456), fill_event(OrderOrigin.LEGACY),
                   fill_event(OrderOrigin.SYSTEM, client_order_id="DKA-1-1-o", fill_id=2))
    status = env.guard.status()
    assert status["stream"]["state"] == "recovered_stable" and status["stream"]["stable_since_interruption"] is True
    assert status["legacy_fills"] == {"count": 1, "last_ms": START_MS}  # 只数旧程序格式的


async def test_it_reports_the_stream_as_not_configured_when_it_is_disabled(make_env):
    env = make_env(config={"ex_stream_enabled": False})
    await env.boot()

    assert env.guard.status()["stream"]["configured"] is False


async def test_the_returned_status_is_a_copy(make_env):
    env = make_env(environment="mainnet_live")
    await env.boot()
    await env.guard.startup()
    status = env.guard.status()

    status["restriction_detail"]["x"] = "y"
    status["legacy_mutex"]["problems"].append("z")
    status["legacy_mutex"]["held"].clear()

    again = env.guard.status()
    assert again["restriction_detail"] == {} and again["legacy_mutex"]["problems"] == []
    assert again["legacy_mutex"]["held"] == list(NAMES)
