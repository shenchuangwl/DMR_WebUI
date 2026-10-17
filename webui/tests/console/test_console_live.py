"""控制器（每个浏览器会话一个）：拉取只读数据、判定与内核的连通、保留失联前的数据、数据版本号、事件提示驱动的局部刷新。

用接口级假客户端 FakeKernelClient 和手动时钟，不连网、不 sleep 等业务时间。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta, timezone

import pytest

from console_kit import (GOOD_TOKEN, START_MS, FakeKernelClient, ManualClock, lost, make_alert, make_oplog_item, make_position,
                         make_state)
from dmr_console.client import KernelError
from dmr_console.events import PARTS
from dmr_console.live import HealthProbe, LiveController
from dmr_console.session import ConsoleSession
from dmr_console.state import LOST_MESSAGE, NO_STATE_REASON

UTC7 = timezone(timedelta(hours=7))


def make(api: FakeKernelClient | None = None, *, clock: ManualClock | None = None, stream_factory=None, grace_s: float = 0.0,
         oplog_page: int = 3, env_hint: str | None = None) -> tuple[LiveController, FakeKernelClient, ManualClock]:
    api = api or FakeKernelClient()
    clock = clock or ManualClock()
    session = ConsoleSession(browser_id="b1", token=GOOD_TOKEN, operator="admin", session_id="sess-1", idle_timeout_min=30,
                             opened_ms=clock.now_ms(), last_seen_ms=clock.now_ms())
    controller = LiveController(api, session, clock, tz=UTC7, oplog_page=oplog_page, stream_factory=stream_factory,
                                stream_grace_s=grace_s, env_hint=env_hint)
    return controller, api, clock


def called(api: FakeKernelClient) -> list[str]:
    return [name for (name, _, _) in api.calls]


def seed(api: FakeKernelClient) -> None:
    api.set("positions", [make_position(id=1), make_position(id=2, symbol="ETHUSDT", ownership="takeover")])
    api.set("positions_external", [make_position(id=9, status="pending", ownership="none", pending_reason="out_of_scope")])
    api.set("oplog", [make_oplog_item(id=i) for i in (12, 11, 10)])
    api.set("alerts", [make_alert(id=3, level="critical")])


# ------------------------------------------------------------------ 第一次拉取

async def test_before_the_first_refresh_nothing_is_known_and_every_button_is_disabled():
    controller, _, _ = make()
    assert controller.state is None and controller.positions is None and not controller.loaded("state")
    assert not controller.status_bar().has_data
    buttons = controller.buttons()
    assert buttons and all(not b.enabled and b.reason == NO_STATE_REASON for b in buttons.values())
    assert all(g.rows == () for g in controller.position_groups())


async def test_first_refresh_loads_every_part_with_the_session_token():
    controller, api, clock = make()
    seed(api)
    await controller.refresh()
    assert controller.state["env"] == "standin"
    assert [p["id"] for p in controller.positions] == [1, 2] and [p["id"] for p in controller.external] == [9]
    assert [i["id"] for i in controller.oplog] == [12, 11, 10] and controller.alerts[0]["id"] == 3
    assert all(controller.loaded(p) for p in PARTS)
    assert all(controller.fetched_ms[p] == clock.now_ms() for p in PARTS)
    assert not controller.link.status.lost and controller.link.status.last_ok_ms == clock.now_ms()
    for name in ("state", "positions", "positions_external", "oplog", "alerts"):
        assert api.calls_of(name)[0][0] == GOOD_TOKEN
    kwargs = {name: kw for (name, _, kw) in api.calls if name in ("oplog", "alerts")}
    assert kwargs == {"oplog": {"limit": 3, "before_id": None}, "alerts": {"limit": 50}}


async def test_view_helpers_are_wired_to_the_loaded_data():
    controller, api, _ = make()
    seed(api)
    await controller.refresh()
    groups = {g.key: g for g in controller.position_groups()}
    assert [r["id"] for r in groups["strategy"].rows] == [1] and [r["id"] for r in groups["takeover"].rows] == [2]
    assert [r["id"] for r in groups["pending"].rows] == [9]
    assert controller.oplog_rows()[0]["id"] == 12 and controller.oplog_rows()[0]["time"].startswith("2025-10-09 15:5")
    assert controller.alert_rows()[0]["level"] == "严重"
    assert controller.status_bar().has_data and controller.status_bar().chip("model").text.startswith("模型 1")


async def test_buttons_are_read_only_in_this_version_even_when_the_kernel_allows_everything():
    controller, api, _ = make()
    api.set("state", make_state(allowed={"start": {"allowed": True, "reason": "", "needs_live_read": False, "note": ""}},
                                seat={"mine": True, "holder_operator": "admin", "holder_session_id": "s", "lease_until_ms": START_MS + 9}))
    await controller.refresh()
    assert controller.buttons()["start"].kernel_allowed is True
    assert all(not b.enabled for b in controller.buttons().values())


# ------------------------------------------------------------------ 失联

@pytest.mark.parametrize("kind", ["connect", "timeout", "protocol"])
async def test_any_transport_failure_marks_the_link_lost_and_disables_all_buttons(kind):
    controller, api, clock = make()
    await controller.refresh()
    assert controller.link.status.lost is False
    api.set("state", lost(kind))
    clock.advance(5)
    await controller.refresh({"state"})
    status = controller.link.status
    assert status.lost and status.kind == kind and status.lost_since_ms == clock.now_ms()
    bar = controller.status_bar()
    assert bar.banner("lost").text == LOST_MESSAGE and bar.stale
    assert all(not b.enabled and b.reason == LOST_MESSAGE for b in controller.buttons().values())


async def test_data_from_before_the_loss_stays_visible_but_is_marked_stale():
    controller, api, clock = make()
    seed(api)
    await controller.refresh()
    before = controller.state
    fetched = controller.fetched_ms["state"]
    api.fail_all(lost("connect"))
    clock.advance(30)
    await controller.refresh()
    assert controller.state == before and controller.fetched_ms["state"] == fetched
    assert [p["id"] for p in controller.positions] == [1, 2]
    bar = controller.status_bar()
    assert bar.has_data and bar.stale and bar.updated_text.startswith("失联前的数据")
    assert bar.chip("account").text.startswith("1,000.00")  # 失联前的数字还在，不是「未知」
    assert controller.status_bar().banner("standin") is not None  # 环境横幅不因失联消失


async def test_recovery_clears_the_loss_and_replaces_the_stale_data():
    controller, api, clock = make()
    await controller.refresh()
    api.fail_all(lost("timeout"))
    clock.advance(10)
    await controller.refresh()
    assert controller.link.status.lost
    api.heal()
    api.set("state", make_state(model={"gate": "running"}))
    clock.advance(10)
    await controller.refresh()
    assert not controller.link.status.lost and controller.link.status.failures == 0
    assert controller.state["model"]["gate"] == "running"
    assert controller.status_bar().banner("lost") is None and not controller.status_bar().stale


async def test_one_failed_part_in_a_batch_keeps_the_link_lost_even_if_the_others_succeed():
    controller, api, _ = make()
    api.set("positions", lost("timeout"))
    await controller.refresh()
    assert controller.link.status.lost and controller.link.status.kind == "timeout"
    assert controller.loaded("state") and not controller.loaded("positions")  # 成功的部分照常入库


async def test_lost_from_the_very_start_shows_the_banner_with_the_environment_hint():
    controller, api, _ = make(env_hint="standin")
    api.fail_all(lost("connect"))
    await controller.refresh()
    bar = controller.status_bar()
    assert not bar.has_data and bar.banner("lost") is not None and bar.banner("standin") is not None
    assert all(not b.enabled and b.reason == LOST_MESSAGE for b in controller.buttons().values())


# ------------------------------------------------------------------ 内核回了错误 / 会话失效 / 程序错误

async def test_the_session_expiring_is_not_a_loss_of_the_kernel():
    controller, api, _ = make()
    await controller.refresh()
    api.valid_tokens.clear()
    await controller.refresh()
    assert controller.session_invalid is True
    assert not controller.link.status.lost
    n = len(api.calls)
    await controller.refresh()
    assert len(api.calls) == n  # 令牌已死，不再拿它去敲内核


async def test_a_kernel_refusal_keeps_the_link_ok_and_is_remembered_per_part_until_it_clears():
    controller, api, _ = make()
    api.set("positions", KernelError(503, "unavailable", "持仓快照暂时读不到"))
    await controller.refresh()
    assert not controller.link.status.lost
    assert controller.errors == {"positions": "持仓快照暂时读不到"}
    assert controller.loaded("state") and not controller.loaded("positions")
    api.heal()
    await controller.refresh()
    assert controller.errors == {} and controller.loaded("positions")


async def test_an_unexpected_exception_is_logged_and_does_not_break_the_refresh(caplog):
    controller, api, _ = make()
    api.set("alerts", ValueError("bug in the client"))
    with caplog.at_level(logging.ERROR):
        await controller.refresh()
    assert "alerts" in controller.errors and "ValueError" in controller.errors["alerts"]
    assert not controller.link.status.lost and controller.loaded("state")
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    assert GOOD_TOKEN not in caplog.text


# ------------------------------------------------------------------ 版本号

async def test_part_versions_move_only_when_the_data_changes():
    controller, api, clock = make()
    seed(api)
    await controller.refresh()
    first = dict(controller.versions)
    assert all(first[p] >= 1 for p in PARTS)
    clock.advance(5)
    await controller.refresh()
    assert controller.versions == first  # 数据没变：版本不动，表格不用重画
    api.set("positions", [make_position(id=1, qty="0.020")])
    await controller.refresh()
    assert controller.versions["positions"] == first["positions"] + 1
    assert all(controller.versions[p] == first[p] for p in PARTS if p != "positions")


async def test_link_version_moves_on_loss_and_recovery_but_not_on_repeated_failures():
    controller, api, _ = make()
    await controller.refresh()
    v0 = controller.link_version
    api.fail_all(lost("connect"))
    await controller.refresh()
    v1 = controller.link_version
    assert v1 > v0
    await controller.refresh()
    await controller.refresh()
    assert controller.link_version == v1
    api.heal()
    await controller.refresh()
    assert controller.link_version > v1


# ------------------------------------------------------------------ 局部刷新与合并

async def test_refresh_only_asks_for_the_requested_parts_and_ignores_unknown_names():
    controller, api, _ = make()
    await controller.refresh({"state"})
    assert called(api) == ["state"]
    api.calls.clear()
    await controller.refresh(["positions", "nonsense"])
    assert called(api) == ["positions"]
    api.calls.clear()
    await controller.refresh([])
    assert called(api) == []


async def test_concurrent_refreshes_share_one_round_trip():
    controller, api, _ = make()
    api.delay_s = 0.05
    await asyncio.gather(controller.refresh(), controller.refresh(), controller.refresh({"state"}))
    for name in ("state", "positions", "positions_external", "oplog", "alerts"):
        assert len(api.calls_of(name)) == 1, name


async def test_a_refresh_for_other_parts_waits_for_the_running_one_instead_of_overlapping():
    controller, api, _ = make()
    api.delay_s = 0.03
    await asyncio.gather(controller.refresh({"state"}), controller.refresh({"alerts"}))
    assert called(api) == ["state", "alerts"]


async def test_refresh_if_due_skips_parts_that_were_asked_recently():
    controller, api, clock = make()
    assert await controller.refresh_if_due(["state", "alerts"], max_age_s=4) is True
    assert called(api) == ["state", "alerts"]
    api.calls.clear()
    clock.advance(2)
    assert await controller.refresh_if_due(["state", "alerts"], max_age_s=4) is False and called(api) == []
    assert await controller.refresh_if_due(["state", "positions"], max_age_s=4) is True
    assert called(api) == ["positions"]  # state 刚拉过，只补 positions
    api.calls.clear()
    clock.advance(3)
    assert await controller.refresh_if_due(["state", "alerts"], max_age_s=4) is True
    assert sorted(called(api)) == ["alerts", "state"]  # positions 是 2 秒前拉的，还不到期


async def test_refresh_if_due_retries_a_part_whose_last_attempt_failed_only_after_the_interval():
    controller, api, clock = make()
    api.set("state", lost("timeout"))
    await controller.refresh_if_due(["state"], max_age_s=4)
    api.calls.clear()
    assert await controller.refresh_if_due(["state"], max_age_s=4) is False  # 刚失败过：不连环敲
    clock.advance(4)
    assert await controller.refresh_if_due(["state"], max_age_s=4) is True


async def test_oplog_pages_merge_and_loading_more_walks_back_by_id():
    controller, api, _ = make(oplog_page=3)
    pages = {None: [make_oplog_item(id=i) for i in (10, 9, 8)], 8: [make_oplog_item(id=i) for i in (7, 6)]}
    api.set("oplog", lambda token, limit=3, before_id=None: pages.get(before_id, []))
    await controller.refresh({"oplog"})
    assert [i["id"] for i in controller.oplog] == [10, 9, 8] and controller.oplog_exhausted is False
    await controller.load_more_oplog()
    assert api.calls_of("oplog")[-1] == (GOOD_TOKEN,) and api.calls[-1][2] == {"limit": 3, "before_id": 8}
    assert [i["id"] for i in controller.oplog] == [10, 9, 8, 7, 6] and controller.oplog_exhausted is True
    n = len(api.calls)
    await controller.load_more_oplog()
    assert len(api.calls) == n  # 到头了：不再请求
    pages[None] = [make_oplog_item(id=i) for i in (12, 11, 10)]
    await controller.refresh({"oplog"})
    assert [i["id"] for i in controller.oplog] == [12, 11, 10, 9, 8, 7, 6]  # 新记录排前面，翻出来的旧页还在


async def test_a_short_first_oplog_page_means_there_is_nothing_older():
    controller, api, _ = make(oplog_page=3)
    api.set("oplog", [make_oplog_item(id=2), make_oplog_item(id=1)])
    await controller.refresh({"oplog"})
    assert controller.oplog_exhausted is True


async def test_loading_more_while_the_kernel_is_lost_marks_the_link_and_keeps_the_list():
    controller, api, _ = make(oplog_page=2)
    api.set("oplog", lambda token, limit=2, before_id=None: [make_oplog_item(id=5), make_oplog_item(id=4)])
    await controller.refresh({"oplog"})
    api.set("oplog", lost("timeout"))
    await controller.load_more_oplog()
    assert controller.link.status.lost and [i["id"] for i in controller.oplog] == [5, 4] and not controller.oplog_exhausted


# ------------------------------------------------------------------ 事件提示

async def test_event_frames_refresh_only_what_they_can_change():
    controller, api, _ = make()
    await controller.handle_frame({"type": "position", "position_id": 1, "at_ms": 1})
    assert sorted(called(api)) == ["positions", "positions_external", "state"]
    api.calls.clear()
    await controller.handle_frame({"type": "ping", "at_ms": 2})
    await controller.handle_frame("junk")
    assert called(api) == []
    await controller.handle_frame({"type": "something_new"})
    assert sorted(called(api)) == ["alerts", "oplog", "positions", "positions_external", "state"]


async def test_connecting_the_event_stream_triggers_a_full_refresh():
    controller, api, _ = make()
    await controller.handle_connected()
    assert sorted(called(api)) == ["alerts", "oplog", "positions", "positions_external", "state"]


async def test_event_frames_after_the_session_died_do_nothing():
    controller, api, _ = make()
    await controller.refresh()
    api.valid_tokens.clear()
    await controller.refresh()
    n = len(api.calls)
    await controller.handle_frame({"type": "state"})
    await controller.handle_connected()
    assert len(api.calls) == n


# ------------------------------------------------------------------ 事件流的生命周期

class FakeStream:
    instances: list["FakeStream"] = []

    def __init__(self, controller: LiveController) -> None:
        self.controller = controller
        self.started = False
        self.cancelled = False
        FakeStream.instances.append(self)

    async def run(self) -> None:
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_the_stream_starts_with_the_first_page_and_stops_after_the_last_one_leaves():
    FakeStream.instances.clear()
    controller, _, _ = make(stream_factory=FakeStream, grace_s=0)
    controller.attach()
    controller.attach()
    await settle()
    assert len(FakeStream.instances) == 1 and FakeStream.instances[0].started
    controller.detach()
    await settle()
    assert not FakeStream.instances[0].cancelled  # 还有一个页面在
    controller.detach()
    await settle()
    assert FakeStream.instances[0].cancelled


async def test_a_short_gap_between_pages_does_not_restart_the_stream():
    FakeStream.instances.clear()
    controller, _, _ = make(stream_factory=FakeStream, grace_s=0.05)
    controller.attach()
    await settle()
    controller.detach()  # 换页：旧页面先走
    controller.attach()  # 新页面随后到
    await asyncio.sleep(0.12)
    assert len(FakeStream.instances) == 1 and not FakeStream.instances[0].cancelled


async def test_the_stream_stops_after_the_grace_period_when_nobody_comes_back():
    FakeStream.instances.clear()
    controller, _, _ = make(stream_factory=FakeStream, grace_s=0.03)
    controller.attach()
    await settle()
    controller.detach()
    assert not FakeStream.instances[0].cancelled
    await asyncio.sleep(0.12)
    assert FakeStream.instances[0].cancelled


async def test_closing_stops_the_stream_and_makes_refresh_a_no_op():
    FakeStream.instances.clear()
    controller, api, _ = make(stream_factory=FakeStream)
    controller.attach()
    await settle()
    await controller.aclose()
    assert FakeStream.instances[0].cancelled and controller.closed
    await controller.refresh()
    assert api.calls == []
    controller.attach()  # 关闭后再 attach 也不会再起流
    await settle()
    assert len(FakeStream.instances) == 1


async def test_a_dead_session_stops_the_stream():
    FakeStream.instances.clear()
    controller, api, _ = make(stream_factory=FakeStream)
    controller.attach()
    await settle()
    api.valid_tokens.clear()
    await controller.refresh()
    await settle()
    assert FakeStream.instances[0].cancelled


async def test_without_a_stream_factory_attach_and_detach_are_harmless():
    controller, _, _ = make(stream_factory=None)
    controller.attach()
    controller.detach()
    controller.detach()
    assert not controller.closed  # 多余的 detach 不会把控制器关掉
    await controller.aclose()
    assert controller.closed


async def test_the_controller_repr_does_not_show_the_token():
    controller, _, _ = make()
    assert GOOD_TOKEN not in repr(controller) and GOOD_TOKEN not in str(controller)


# ------------------------------------------------------------------ 登录页用的内核探活

async def test_health_probe_learns_the_environment_and_follows_the_link():
    clock = ManualClock()
    api = FakeKernelClient()
    probe = HealthProbe(api, clock)
    assert probe.env is None and not probe.link.status.lost
    await probe.check()
    assert probe.env == "standin" and not probe.link.status.lost
    api.set("health", lost("connect"))
    await probe.check()
    assert probe.link.status.lost and probe.env == "standin"  # 环境记着：失联时登录页还能标注替身环境
    api.heal()
    await probe.check()
    assert not probe.link.status.lost


async def test_health_probe_treats_a_kernel_error_as_alive_and_junk_as_unknown_environment():
    api = FakeKernelClient()
    probe = HealthProbe(api, ManualClock())
    api.set("health", KernelError(500, "internal", "x"))
    await probe.check()
    assert not probe.link.status.lost
    api.set("health", {"status": "ok", "env": 5})
    await probe.check()
    assert probe.env is None


# ------------------------------------------------------------------ 会话活动

async def test_successful_polls_keep_the_local_session_alive_like_they_keep_the_kernel_session_alive():
    from dmr_console.client import LoginResult
    from dmr_console.session import SessionStore

    clock = ManualClock()
    api = FakeKernelClient()
    store = SessionStore(clock)
    session = store.open("b1", LoginResult(token=GOOD_TOKEN, operator="admin", session_id="s", idle_timeout_min=30))
    controller = LiveController(api, session, clock, tz=UTC7)
    for _ in range(4):
        clock.advance(25 * 60)  # 每次都不到 30 分钟；累计 100 分钟
        await controller.refresh()
        assert store.get("b1") is session


async def test_polls_that_fail_do_not_keep_the_local_session_alive():
    from dmr_console.client import LoginResult
    from dmr_console.session import SessionStore

    clock = ManualClock()
    api = FakeKernelClient()
    store = SessionStore(clock)
    session = store.open("b1", LoginResult(token=GOOD_TOKEN, operator="admin", session_id="s", idle_timeout_min=30))
    controller = LiveController(api, session, clock, tz=UTC7)
    api.fail_all(lost("connect"))
    clock.advance(20 * 60)
    await controller.refresh()
    clock.advance(20 * 60)
    await controller.refresh()
    assert store.get("b1") is None  # 内核根本没收到请求，它那边的闲置计时照走
