"""会话存储：内核令牌只存在控制台服务端内存里，按浏览器会话（app.storage.browser 的 id）区分；不落盘、不进 repr。"""
from __future__ import annotations

import os

import pytest

from console_kit import ManualClock
from dmr_console.client import LoginResult
from dmr_console.session import DEFAULT_IDLE_MIN, SessionStore

TOKEN_A = "tok-AAAA-secret-1111"
TOKEN_B = "tok-BBBB-secret-2222"


def login(token: str = TOKEN_A, operator: str = "admin", idle_min: float | None = 30) -> LoginResult:
    return LoginResult(token=token, operator=operator, session_id=f"sess-{operator}", idle_timeout_min=idle_min)


def test_a_stored_session_is_found_by_its_browser_id():
    store = SessionStore(ManualClock())
    stored = store.open("browser-1", login())
    found = store.get("browser-1")
    assert found is stored
    assert (found.token, found.operator, found.session_id) == (TOKEN_A, "admin", "sess-admin")
    assert "browser-1" in store and len(store) == 1


@pytest.mark.parametrize("missing", [None, "", "nope"])
def test_unknown_browser_ids_have_no_session(missing):
    store = SessionStore(ManualClock())
    store.open("browser-1", login())
    assert store.get(missing) is None
    assert missing not in store


@pytest.mark.parametrize("bad", ["", None, 5])
def test_opening_a_session_needs_a_real_browser_id(bad):
    with pytest.raises(ValueError):
        SessionStore(ManualClock()).open(bad, login())  # type: ignore[arg-type]


def test_browsers_are_isolated_from_each_other():
    store = SessionStore(ManualClock())
    store.open("browser-1", login(TOKEN_A, "admin"))
    store.open("browser-2", login(TOKEN_B, "bob"))
    assert store.get("browser-1").token == TOKEN_A and store.get("browser-2").token == TOKEN_B
    assert store.drop("browser-1").token == TOKEN_A
    assert store.get("browser-1") is None
    assert store.get("browser-2").token == TOKEN_B


def test_logging_in_again_from_the_same_browser_replaces_the_old_session():
    store = SessionStore(ManualClock())
    store.open("browser-1", login(TOKEN_A))
    store.open("browser-1", login(TOKEN_B, "bob"))
    assert len(store) == 1
    assert store.get("browser-1").token == TOKEN_B and store.get("browser-1").operator == "bob"


def test_drop_returns_the_session_once_and_unknown_ids_are_harmless():
    store = SessionStore(ManualClock())
    store.open("browser-1", login())
    assert store.drop("browser-1").token == TOKEN_A
    assert store.drop("browser-1") is None
    assert store.drop(None) is None
    assert len(store) == 0


def test_the_token_never_appears_in_repr_or_str():
    store = SessionStore(ManualClock())
    session = store.open("browser-1", login())
    for text in (repr(session), str(session), repr(store), str(store), repr(login()), str(login()), f"{session!r}{store!r}"):
        assert TOKEN_A not in text
    assert "admin" in repr(session)  # 其它信息仍然可读，方便排错


def test_an_idle_session_expires_after_the_kernel_idle_timeout_and_activity_keeps_it_alive():
    clock = ManualClock()
    store = SessionStore(clock)
    store.open("browser-1", login(idle_min=30))
    clock.advance(29 * 60)
    assert store.get("browser-1") is not None  # 取用算活动
    clock.advance(29 * 60)
    assert store.get("browser-1") is not None
    clock.advance(31 * 60)
    assert store.get("browser-1") is None and len(store) == 0


def test_touch_counts_as_activity_without_fetching():
    clock = ManualClock()
    store = SessionStore(clock)
    store.open("browser-1", login(idle_min=10))
    clock.advance(9 * 60)
    store.touch("browser-1")
    clock.advance(9 * 60)
    assert store.get("browser-1") is not None
    store.touch("nobody")  # 不存在的 id：什么也不发生


def test_the_default_idle_timeout_is_used_when_the_kernel_did_not_say():
    clock = ManualClock()
    store = SessionStore(clock)
    store.open("browser-1", login(idle_min=None))
    clock.advance(DEFAULT_IDLE_MIN * 60 - 1)
    assert store.get("browser-1") is not None
    clock.advance(DEFAULT_IDLE_MIN * 60 + 1)
    assert store.get("browser-1") is None


def test_the_store_is_bounded_and_evicts_the_least_recently_seen_session():
    clock = ManualClock()
    store = SessionStore(clock, max_sessions=2)
    store.open("b1", login(TOKEN_A))
    clock.advance(1)
    store.open("b2", login(TOKEN_B))
    clock.advance(1)
    assert store.get("b1") is not None  # b1 更新近了
    clock.advance(1)
    store.open("b3", login("tok-CCCC"))
    assert "b2" not in store and "b1" in store and "b3" in store and len(store) == 2


def test_expired_sessions_are_pruned_when_others_are_added():
    clock = ManualClock()
    store = SessionStore(clock, max_sessions=10)
    store.open("old", login(idle_min=1))
    clock.advance(120)
    store.open("new", login(TOKEN_B))
    assert "old" not in store and len(store) == 1


def test_the_store_never_touches_the_filesystem(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SessionStore(ManualClock())
    store.open("browser-1", login())
    store.get("browser-1")
    store.drop("browser-1")
    assert list(tmp_path.iterdir()) == []
    assert os.listdir(tmp_path) == []


def test_browser_ids_lists_current_sessions():
    store = SessionStore(ManualClock())
    store.open("b1", login())
    store.open("b2", login(TOKEN_B))
    assert sorted(store.browser_ids()) == ["b1", "b2"]
