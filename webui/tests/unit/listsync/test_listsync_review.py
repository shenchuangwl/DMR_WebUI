"""复核补充：失效名单绝不当有效（意外异常、类型不对的字段）、成员有效时长缺首次入选时间、业务日缺失时的新增上限、不跟随重定向。"""
from __future__ import annotations

import json

from listsync_world import URL, iso_ms, make_instrument, member, payload, sample, variant
from test_listsync_client import FakeResponse, client_with

MIN = 60_000


async def test_an_unexpected_fetch_error_marks_the_list_invalid_instead_of_keeping_it_valid(make_sync, clock):
    sync, _ = make_sync((200, sample()), RuntimeError("bug in transport"))
    assert (await sync.poll()).valid is True
    clock.advance(MIN)
    view = await sync.poll()
    assert view.valid is False and "不可达" in view.reason
    assert sync.current().valid is False


async def test_valid_must_be_the_json_boolean_true_and_valid_until_must_carry_a_timezone(make_sync):
    sync, _ = make_sync((200, variant(valid="true")))
    assert (await sync.poll()).valid is False
    sync2, _ = make_sync((200, variant(valid_until_utc="2026-10-16T22:30:00")))
    assert (await sync2.poll()).valid is False


async def test_with_a_member_ttl_a_member_without_first_selected_is_not_tradable(make_sync, config):
    config["list_member_ttl_h"] = 2
    nofirst = member("ZKUSDT", "LONG", 65.0)
    del nofirst["first_selected_at"]
    body = payload(onlycoin=[nofirst, member("TIAUSDT", "LONG", 70.0, "2026-10-16T21:00:00Z")])
    sync, _ = make_sync((200, body))
    view = await sync.poll()
    assert [m.symbol for m in view.members] == ["TIAUSDT"]  # 无法判断有效时长：按过期处理
    assert sync.member("ZKUSDT") is None


async def test_the_new_coin_cap_still_applies_when_business_date_is_missing(make_sync, rules, config, clock):
    config["list_max_new_per_poll"] = 3
    first = payload(onlycoin=many(2, rules))
    second = payload(onlycoin=many(2, rules) + many(4, rules, 10))
    first.pop("business_date")
    second.pop("business_date")
    sync, _ = make_sync((200, first), (200, second))
    assert (await sync.poll()).valid is True
    clock.advance(MIN)
    view = await sync.poll()
    assert view.valid is False and "拒收" in view.reason


def many(n: int, rules, start: int = 0):
    codes = [f"C{start + i:03d}USDT" for i in range(n)]
    for c in codes:
        rules._by_symbol[c] = make_instrument(c)
    return [member(c, "LONG", 50.0 + i * 0.01) for i, c in enumerate(codes)]


async def test_the_client_never_follows_redirects():
    client, made = client_with(lambda: FakeResponse(200, json.dumps({"valid": True}).encode()))
    await client.get_json(URL, timeout_s=10)
    assert made[0].requests[0][1].get("allow_redirects") is False
    client, _ = client_with(lambda: FakeResponse(302, b""))
    assert await client.get_json(URL, timeout_s=10) == (302, None)  # 3xx 当作非 200：名单无效


async def test_expired_snapshot_never_reports_valid_even_after_ttl_zero(make_sync, clock):
    sync, _ = make_sync((200, sample()))
    await sync.poll()
    clock.set(iso_ms("2026-10-16T22:30:00Z") + 1)
    assert sync.current().valid is False and sync.member("TIAUSDT") is None
