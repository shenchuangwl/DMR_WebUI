"""ListSync.poll 与 current：采用条件、成员解析、名单无效时的沿用、告警（F10 + 附录 C）。"""
from __future__ import annotations

from decimal import Decimal

from dmr_kernel.core.types import PositionSide
from listsync_world import SAMPLE_NOW_MS, SAMPLE_SYMBOLS, URL, iso_ms, make_instrument, member, payload, sample, variant

MIN = 60_000


# ------------------------------------------------------------------ 采用样例


async def test_the_recorded_sample_is_adopted_with_members_ranked_by_score(make_sync):
    sync, http = make_sync((200, sample()))
    view = await sync.poll()
    assert view.valid is True and view.reason == ""
    assert [m.symbol for m in view.members] == ["LUMIAUSDT", "MUBARAKUSDT", "ZKUSDT", "TIAUSDT", "PYTHUSDT", "APRUSDT"]
    scores = [m.score for m in view.members]
    assert scores == sorted(scores, reverse=True) and all(isinstance(s, Decimal) for s in scores)
    assert view.business_date == "2026-10-16" and view.status == "live"
    assert view.valid_until_ms == iso_ms("2026-10-16T22:30:00Z") and view.fetched_ms == SAMPLE_NOW_MS
    assert view.snapshot_id is not None and view.rejected == ()
    assert http.calls == [(URL, 10)]  # 一次 GET，不带参数，超时 10 秒


async def test_direction_comes_from_the_member_and_first_selected_is_parsed(make_sync):
    sync, _ = make_sync((200, sample()))
    view = await sync.poll()
    by = {m.symbol: m for m in view.members}
    assert by["TIAUSDT"].direction is PositionSide.LONG and by["APRUSDT"].direction is PositionSide.SHORT
    assert by["TIAUSDT"].first_selected_ms == iso_ms("2026-10-16T01:15:00Z")
    assert by["TIAUSDT"].raw_code == "TIAUSDT"


async def test_member_lookup_and_current_return_the_adopted_view(make_sync):
    sync, _ = make_sync((200, sample()))
    assert sync.current().valid is False  # 还没拉过
    view = await sync.poll()
    assert sync.current() == view
    assert sync.member("ZKUSDT").symbol == "ZKUSDT" and sync.member("NOPEUSDT") is None


async def test_a_valid_empty_list_is_a_normal_state(make_sync):
    sync, _ = make_sync((200, payload()))
    view = await sync.poll()
    assert view.valid is True and view.members == () and view.status == "empty"


async def test_symbols_array_and_member_expiry_are_not_used_and_arrays_are_deduplicated(make_sync):
    body = payload(onlycoin=[member("TIAUSDT", "LONG", 70.0), member("APRUSDT", "SHORT", 60.0)],
                   long=[member("TIAUSDT", "LONG", 70.0), member("ZKUSDT", "LONG", 65.0)],
                   short=[member("APRUSDT", "SHORT", 60.0)])
    body["symbols"] = ["TIAUSDT", "APRUSDT", "PYTHUSDT"]  # 不带方向的数组：不用
    sync, _ = make_sync((200, body))
    view = await sync.poll()
    # 成员自带的 expires_at_utc 早已过去，仍然有效；三个数组的并集去重
    assert [m.symbol for m in view.members] == ["TIAUSDT", "ZKUSDT", "APRUSDT"]


# ------------------------------------------------------------------ 无效的几种


async def test_http_503_is_invalid_with_a_chinese_reason(make_sync):
    sync, _ = make_sync((503, {"detail": "not ready"}))
    view = await sync.poll()
    assert view.valid is False and "503" in view.reason and view.members == () and view.snapshot_id is None


async def test_valid_false_is_invalid(make_sync):
    sync, _ = make_sync((200, variant(valid=False)))
    view = await sync.poll()
    assert view.valid is False and "valid=false" in view.reason and view.members == ()


async def test_an_already_expired_lease_is_invalid(make_sync, clock):
    clock.set(iso_ms("2026-10-16T22:30:00Z"))  # 当前时间不早于 valid_until
    sync, _ = make_sync((200, sample()))
    view = await sync.poll()
    assert view.valid is False and "valid_until_utc" in view.reason and view.members == ()


async def test_an_unreachable_endpoint_is_invalid(make_sync):
    sync, _ = make_sync(ConnectionError("refused"))
    view = await sync.poll()
    assert view.valid is False and "不可达" in view.reason


async def test_a_malformed_body_is_invalid(make_sync):
    sync, _ = make_sync((200, None))
    assert (await sync.poll()).valid is False
    sync2, _ = make_sync((200, {"valid": True, "valid_until_utc": "garbage"}))
    assert (await sync2.poll()).valid is False


# ------------------------------------------------------------------ 沿用上一份有效快照


async def test_members_are_kept_after_a_failed_poll_until_the_old_lease_expires(make_sync, clock):
    sync, _ = make_sync((200, sample()), (503, {}))
    good = await sync.poll()
    clock.advance(MIN)
    view = await sync.poll()
    assert view.valid is False and "503" in view.reason
    assert view.members == good.members and view.snapshot_id == good.snapshot_id  # 沿用
    assert sync.member("ZKUSDT") is not None
    clock.set(iso_ms("2026-10-16T22:30:00Z"))  # 旧快照过期：成员清空，无需再拉
    after = sync.current()
    assert after.valid is False and after.members == () and sync.member("ZKUSDT") is None


async def test_current_turns_invalid_by_itself_when_the_lease_passes(make_sync, clock):
    sync, _ = make_sync((200, sample()))
    await sync.poll()
    clock.set(iso_ms("2026-10-16T22:30:00Z"))
    view = sync.current()
    assert view.valid is False and "valid_until_utc" in view.reason and view.members == ()


async def test_recovery_after_a_failure_adopts_the_new_snapshot(make_sync, clock):
    sync, _ = make_sync((503, {}), (200, sample()))
    assert (await sync.poll()).valid is False
    clock.advance(MIN)
    assert (await sync.poll()).valid is True


# ------------------------------------------------------------------ 合约名转换


async def test_chinese_and_1000_prefixed_contract_names_are_looked_up_exactly(make_sync, rules):
    for code in ("币安人生USDT", "1000PEPEUSDT"):
        rules._by_symbol[code] = make_instrument(code)
    body = payload(onlycoin=[member("币安人生USDT", "LONG", 80.0), member("1000PEPEUSDT", "SHORT", 75.0)])
    sync, _ = make_sync((200, body))
    view = await sync.poll()
    assert [(m.symbol, m.raw_code) for m in view.members] == [("币安人生USDT", "币安人生USDT"), ("1000PEPEUSDT", "1000PEPEUSDT")]


async def test_codes_are_never_guessed_or_completed(make_sync, rules):
    rules._by_symbol["1000PEPEUSDT"] = make_instrument("1000PEPEUSDT")
    body = payload(onlycoin=[member("PEPEUSDT", "LONG", 80.0), member("TIA", "LONG", 70.0), member("tiausdt", "LONG", 60.0)])
    sync, _ = make_sync((200, body))
    view = await sync.poll()
    assert view.members == () and [r[0] for r in view.rejected] == ["PEPEUSDT", "TIA", "tiausdt"]
    assert all("查不到" in reason for _, reason in view.rejected)


async def test_non_trading_contracts_are_rejected_with_a_reason(make_sync, rules):
    rules._by_symbol["ZKUSDT"] = make_instrument("ZKUSDT", status="SETTLING")
    rules._by_symbol["PYTHUSDT"] = make_instrument("PYTHUSDT", contract_type="CURRENT_QUARTER")
    sync, _ = make_sync((200, sample()))
    view = await sync.poll()
    assert "ZKUSDT" not in [m.symbol for m in view.members] and "PYTHUSDT" not in [m.symbol for m in view.members]
    reasons = dict(view.rejected)
    assert "非交易状态" in reasons["ZKUSDT"] and "非交易状态" in reasons["PYTHUSDT"]
    assert sync.member("ZKUSDT") is None


async def test_bad_direction_and_missing_score_are_rejected(make_sync):
    bad = member("TIAUSDT", "BOTH", 70.0)
    noscore = member("APRUSDT", "SHORT", 60.0)
    del noscore["total_score"]
    sync, _ = make_sync((200, payload(onlycoin=[bad, noscore, member("ZKUSDT", "LONG", 50.0)])))
    view = await sync.poll()
    assert [m.symbol for m in view.members] == ["ZKUSDT"] and {c for c, _ in view.rejected} == {"TIAUSDT", "APRUSDT"}


# ------------------------------------------------------------------ 单次新增上限


def many(n: int, rules=None, start: int = 0):
    codes = [f"C{start + i:03d}USDT" for i in range(n)]
    if rules is not None:
        for c in codes:
            rules._by_symbol[c] = make_instrument(c)
    return [member(c, "LONG", 50.0 + i * 0.01) for i, c in enumerate(codes)]


async def test_too_many_new_coins_reject_the_whole_snapshot_and_alert(make_sync, rules, config, notifier, clock):
    config["list_max_new_per_poll"] = 3
    sync, _ = make_sync((200, payload(onlycoin=many(2, rules))), (200, payload(onlycoin=many(2, rules) + many(4, rules, 10))))
    first = await sync.poll()
    clock.advance(MIN)
    view = await sync.poll()
    assert view.valid is False and "新增" in view.reason and "拒收" in view.reason
    assert view.members == first.members  # 沿用上一份
    assert len(notifier.alerts) == 1 and notifier.alerts[0]["dedupe_key"]


async def test_new_count_at_the_cap_is_accepted_and_only_new_codes_count(make_sync, rules, config, clock):
    config["list_max_new_per_poll"] = 3
    base = many(2, rules)
    sync, _ = make_sync((200, payload(onlycoin=base)), (200, payload(onlycoin=base + many(3, rules, 10))))
    await sync.poll()
    clock.advance(MIN)
    view = await sync.poll()
    assert view.valid is True and len(view.members) == 5


async def test_the_first_load_and_a_new_business_day_have_no_baseline(make_sync, rules, config, clock):
    config["list_max_new_per_poll"] = 3
    sync, _ = make_sync((200, payload(onlycoin=many(10, rules))))
    assert (await sync.poll()).valid is True  # 启动后第一份：没有可比的基线
    sync2, _ = make_sync((200, payload(onlycoin=many(2, rules))),
                         (200, payload(onlycoin=many(10, rules, 50), business_date="2026-10-17",
                                       valid_until_utc="2026-10-17T22:30:00Z")))
    await sync2.poll()
    clock.set(iso_ms("2026-10-17T00:05:00Z"))
    assert (await sync2.poll()).valid is True  # 换了业务日：不比较


# ------------------------------------------------------------------ 成员有效时长


async def test_member_ttl_zero_means_valid_for_the_day(make_sync, config):
    config["list_member_ttl_h"] = 0
    sync, _ = make_sync((200, sample()))
    view = await sync.poll()
    assert len(view.members) == 6


async def test_member_ttl_counts_from_first_selected(make_sync, config, clock):
    config["list_member_ttl_h"] = 2
    body = payload(onlycoin=[member("TIAUSDT", "LONG", 70.0, "2026-10-16T01:15:00Z"),
                             member("ZKUSDT", "LONG", 65.0, "2026-10-16T21:00:00Z")],
                   valid_until_utc="2026-10-16T23:59:00Z")
    sync, _ = make_sync((200, body))
    view = await sync.poll()
    assert [m.symbol for m in view.members] == ["ZKUSDT"]  # TIA 入选已超过 2 小时
    clock.set(iso_ms("2026-10-16T23:00:00Z"))  # ZK 满 2 小时：不用再拉，current() 也按时间过滤
    assert sync.current().members == () and sync.member("ZKUSDT") is None


# ------------------------------------------------------------------ 连续无效告警


async def test_long_lasting_invalid_list_alerts_once_the_threshold_passes(make_sync, config, notifier, clock):
    config["list_invalid_alert_min"] = 30
    sync, _ = make_sync((503, {}))
    await sync.poll()
    clock.advance(29 * MIN)
    await sync.poll()
    assert notifier.alerts == []
    clock.advance(1 * MIN)
    await sync.poll()
    assert len(notifier.alerts) == 1 and "名单" in notifier.alerts[0]["message"]
    assert notifier.alerts[0]["dedupe_key"]


async def test_a_valid_poll_resets_the_invalid_timer(make_sync, config, notifier, clock):
    config["list_invalid_alert_min"] = 30
    sync, _ = make_sync((503, {}), (200, payload(valid_until_utc="2026-10-17T22:30:00Z")), (503, {}))
    await sync.poll()
    clock.advance(20 * MIN)
    await sync.poll()
    clock.advance(20 * MIN)
    await sync.poll()
    assert notifier.alerts == []
