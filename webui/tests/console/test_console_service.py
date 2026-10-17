"""控制台服务（不含界面）：登录/退出、按浏览器会话取控制器。错误文案、令牌/口令不进日志、失联时的处理。"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta, timezone

import pytest

from console_kit import GOOD_TOKEN, FakeKernelClient, ManualClock, lost
from dmr_console.client import KernelError, LoginResult
from dmr_console.service import ConsoleService
from dmr_console.session import SessionStore
from dmr_console.state import LOST_MESSAGE

UTC7 = timezone(timedelta(hours=7))
PASSWORD = "pw-admin"


class FakeStream:
    instances: list["FakeStream"] = []

    def __init__(self, controller) -> None:
        self.controller = controller
        self.cancelled = False
        FakeStream.instances.append(self)

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def make(api: FakeKernelClient | None = None, *, stream_factory=None) -> tuple[ConsoleService, FakeKernelClient, ManualClock, SessionStore]:
    api = api or FakeKernelClient()
    clock = ManualClock()
    sessions = SessionStore(clock)
    service = ConsoleService(api=api, clock=clock, sessions=sessions, tz=UTC7, stream_factory=stream_factory,
                             stream_grace_s=0)
    return service, api, clock, sessions


# ------------------------------------------------------------------ 登录

async def test_a_successful_login_stores_the_kernel_token_under_the_browser_id_only_on_the_server():
    service, api, _, sessions = make()
    outcome = await service.login("browser-1", "admin", PASSWORD)
    assert outcome.ok and outcome.code == "ok"
    assert api.calls_of("login") == [("admin", PASSWORD)]
    session = service.session_for("browser-1")
    assert session.token == GOOD_TOKEN and session.operator == "admin" and sessions.get("browser-1") is session
    assert GOOD_TOKEN not in repr(outcome) and GOOD_TOKEN not in outcome.message


async def test_the_controller_belongs_to_the_session_and_is_reused():
    service, _, _, _ = make()
    await service.login("browser-1", "admin", PASSWORD)
    first = service.controller_for("browser-1")
    assert first is not None and first.session.token == GOOD_TOKEN
    assert service.controller_for("browser-1") is first


async def test_browsers_get_separate_sessions_and_controllers():
    api = FakeKernelClient(accounts={"admin": PASSWORD, "bob": "pw-bob"})
    service, _, _, _ = make(api)
    await service.login("b1", "admin", PASSWORD)
    await service.login("b2", "bob", "pw-bob")
    assert service.session_for("b1").operator == "admin" and service.session_for("b2").operator == "bob"
    assert service.controller_for("b1") is not service.controller_for("b2")


async def test_unknown_browsers_have_neither_session_nor_controller():
    service, _, _, _ = make()
    assert service.session_for("nobody") is None and service.controller_for("nobody") is None
    assert service.session_for(None) is None and service.controller_for(None) is None


async def test_the_username_is_trimmed_but_the_password_is_sent_exactly_as_typed():
    service, api, _, _ = make(FakeKernelClient(accounts={"admin": " pw with spaces "}))
    outcome = await service.login("b1", "  admin ", " pw with spaces ")
    assert outcome.ok
    assert api.calls_of("login") == [("admin", " pw with spaces ")]


@pytest.mark.parametrize("user, pw", [("", PASSWORD), ("   ", PASSWORD), ("admin", ""), ("", "")])
async def test_empty_credentials_never_reach_the_kernel(user, pw):
    service, api, _, _ = make()
    outcome = await service.login("b1", user, pw)
    assert not outcome.ok and outcome.code == "empty" and "用户名" in outcome.message
    assert api.calls_of("login") == [] and service.session_for("b1") is None


async def test_login_without_a_browser_id_is_refused_without_calling_the_kernel():
    service, api, _, _ = make()
    outcome = await service.login("", "admin", PASSWORD)
    assert not outcome.ok and outcome.code == "no_browser" and api.calls_of("login") == []


async def test_wrong_credentials_are_reported_and_nothing_is_stored():
    service, _, _, sessions = make()
    outcome = await service.login("b1", "admin", "wrong")
    assert not outcome.ok and outcome.code == "bad_credentials" and outcome.message == "用户名或口令错误"
    assert not outcome.unreachable and len(sessions) == 0


async def test_rate_limiting_tells_the_operator_how_long_to_wait():
    api = FakeKernelClient()
    api.set("login", KernelError(429, "rate_limited", "失败次数过多", {"retry_after_s": 37}))
    service, _, _, sessions = make(api)
    outcome = await service.login("b1", "admin", "x")
    assert outcome.code == "rate_limited" and outcome.retry_after_s == 37 and "37" in outcome.message and len(sessions) == 0
    api.set("login", KernelError(429, "rate_limited", "失败次数过多"))
    outcome = await service.login("b1", "admin", "x")
    assert outcome.code == "rate_limited" and outcome.retry_after_s is None and "稍后" in outcome.message


async def test_other_kernel_refusals_show_the_kernels_own_reason():
    api = FakeKernelClient()
    api.set("login", KernelError(503, "unavailable", "账本暂时不可读"))
    service, _, _, _ = make(api)
    outcome = await service.login("b1", "admin", PASSWORD)
    assert not outcome.ok and outcome.code == "unavailable" and "账本暂时不可读" in outcome.message and not outcome.unreachable
    api.set("login", KernelError(500, "internal", ""))
    outcome = await service.login("b1", "admin", PASSWORD)
    assert "internal" in outcome.message


@pytest.mark.parametrize("kind", ["connect", "timeout", "protocol"])
async def test_an_unreachable_kernel_is_reported_with_the_standard_lost_message(kind):
    api = FakeKernelClient()
    api.set("login", lost(kind))
    service, _, _, sessions = make(api)
    outcome = await service.login("b1", "admin", PASSWORD)
    assert not outcome.ok and outcome.unreachable and outcome.code == "unreachable"
    assert outcome.message == LOST_MESSAGE and outcome.detail and len(sessions) == 0


async def test_an_unexpected_error_is_logged_and_neither_password_nor_token_leaks(caplog):
    api = FakeKernelClient()
    api.set("login", RuntimeError("boom"))
    service, _, _, _ = make(api)
    with caplog.at_level(logging.DEBUG):
        outcome = await service.login("b1", "admin", PASSWORD)
    assert not outcome.ok and outcome.code == "error" and "内部错误" in outcome.message
    assert PASSWORD not in caplog.text and PASSWORD not in outcome.message and PASSWORD not in repr(outcome)
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_no_log_line_ever_contains_the_password_or_the_token(caplog):
    service, _, _, _ = make()
    with caplog.at_level(logging.DEBUG):
        await service.login("b1", "admin", "wrong-password-xyz")
        await service.login("b1", "admin", PASSWORD)
        await service.controller_for("b1").refresh()
        await service.logout("b1")
    assert "wrong-password-xyz" not in caplog.text and PASSWORD not in caplog.text and GOOD_TOKEN not in caplog.text


async def test_logging_in_again_replaces_the_session_and_closes_the_old_controller():
    api = FakeKernelClient(accounts={"admin": PASSWORD, "bob": "pw-bob"})
    service, _, _, sessions = make(api)
    await service.login("b1", "admin", PASSWORD)
    old = service.controller_for("b1")
    await service.login("b1", "bob", "pw-bob")
    assert old.closed
    new = service.controller_for("b1")
    assert new is not old and new.session.operator == "bob" and len(sessions) == 1


# ------------------------------------------------------------------ 退出与过期

async def test_logout_revokes_the_token_and_forgets_everything():
    service, api, _, sessions = make()
    await service.login("b1", "admin", PASSWORD)
    controller = service.controller_for("b1")
    outcome = await service.logout("b1")
    assert outcome.confirmed and api.calls_of("logout") == [(GOOD_TOKEN,)]
    assert controller.closed and service.session_for("b1") is None and service.controller_for("b1") is None and len(sessions) == 0


async def test_logout_while_the_kernel_is_unreachable_still_forgets_locally_but_says_it_is_unconfirmed():
    service, api, _, _ = make()
    await service.login("b1", "admin", PASSWORD)
    api.set("logout", lost("timeout"))
    outcome = await service.logout("b1")
    assert not outcome.confirmed and "闲置" in outcome.message
    assert service.session_for("b1") is None


async def test_logout_of_an_unknown_browser_is_harmless():
    service, api, _, _ = make()
    outcome = await service.logout("nobody")
    assert outcome.confirmed and api.calls_of("logout") == []


async def test_end_session_forgets_a_dead_session_without_calling_the_kernel():
    service, api, _, _ = make()
    await service.login("b1", "admin", PASSWORD)
    controller = service.controller_for("b1")
    await service.end_session("b1")
    assert controller.closed and service.session_for("b1") is None and api.calls_of("logout") == []


async def test_an_expired_session_has_no_controller_and_its_old_controller_is_closed():
    service, _, clock, _ = make()
    await service.login("b1", "admin", PASSWORD)
    controller = service.controller_for("b1")
    clock.advance(31 * 60)
    assert service.controller_for("b1") is None
    assert controller.closed


async def test_closing_the_service_closes_every_controller():
    api = FakeKernelClient(accounts={"admin": PASSWORD, "bob": "pw-bob"})
    service, _, _, _ = make(api)
    await service.login("b1", "admin", PASSWORD)
    await service.login("b2", "bob", "pw-bob")
    controllers = [service.controller_for("b1"), service.controller_for("b2")]
    await service.aclose()
    assert all(c.closed for c in controllers)


async def test_controllers_get_the_event_stream_factory():
    FakeStream.instances.clear()
    service, _, _, _ = make(stream_factory=FakeStream)
    await service.login("b1", "admin", PASSWORD)
    controller = service.controller_for("b1")
    controller.attach()
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(FakeStream.instances) == 1 and FakeStream.instances[0].controller is controller
    await service.logout("b1")
    for _ in range(5):
        await asyncio.sleep(0)
    assert FakeStream.instances[0].cancelled


async def test_login_result_type_is_what_the_client_returns():
    result = await FakeKernelClient().login("admin", PASSWORD)
    assert isinstance(result, LoginResult)
