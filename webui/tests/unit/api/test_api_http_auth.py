"""控制接口的会话与席位（经 starlette TestClient，进程内；服务是真实实现 + 临时账本）。

报文约定：docs/plans/M1-控制接口只读部分.md 第 1、2 节。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from api_kit import BASE, PASSWORD, T0
from dmr_kernel.api.common import KERNEL_VERSION
from dmr_kernel.ledger import repo_audit

MIN = 60_000
LOGIN_KEYS = {"token", "operator", "session_id", "idle_timeout_min"}
SEAT_KEYS = {"holder_session_id", "holder_operator", "lease_until_ms", "mine"}

# 除健康检查外的各种接口：没有令牌一律 401（含不存在的路径：不泄露哪些路径存在）
PROTECTED = [
    ("GET", "/state"), ("GET", "/positions"), ("GET", "/positions/external"), ("GET", "/orders"),
    ("GET", "/oplog"), ("GET", "/alerts"), ("GET", "/config"), ("GET", "/operations/abc"), ("GET", "/auth/me"),
    ("GET", "/watchlist"), ("GET", "/trades"), ("GET", "/stats"), ("GET", "/candles"),
    ("POST", "/auth/logout"), ("POST", "/auth/reauth"), ("POST", "/seat/acquire"), ("POST", "/seat/renew"),
    ("POST", "/seat/release"), ("POST", "/seat/force"), ("POST", "/ops/start"), ("POST", "/ops/close_all/confirm"),
    ("PUT", "/config/leverage"), ("POST", "/dev/inject_signal"), ("GET", "/no/such/path"), ("DELETE", "/positions"),
]


def error_shape(response) -> tuple[int, str]:
    body = response.json()
    assert set(body) >= {"code", "message"} and isinstance(body["message"], str) and body["message"]
    return response.status_code, body["code"]


# ---------------------------------------------------------------- 健康检查与令牌

def test_health_needs_no_token_and_reports_environment_version_and_time(kit):
    response = kit.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "env": "standin", "version": KERNEL_VERSION, "time_ms": T0}


@pytest.mark.parametrize("method, path", PROTECTED)
def test_without_a_token_only_the_health_check_is_reachable(kit, method, path):
    response = kit.request(method, path)
    assert error_shape(response) == (401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["Bearer", "Bearer a b", "Basic dXNlcjpwdw==", "bearer", "Token abc", "",
                                    "Bearer " + "A" * 600, "Bearer wrong-token"])
def test_malformed_or_unknown_tokens_are_unauthorized(kit, header):
    response = kit.get("/state", headers={"Authorization": header})
    assert error_shape(response) == (401, "unauthorized")


def test_a_request_is_authenticated_exactly_once(kit, monkeypatch):
    sess = kit.login()
    real, calls = kit.services.auth.authenticate, []

    async def counting(token, **kw):
        calls.append(token)
        return await real(token, **kw)

    monkeypatch.setattr(kit.services.auth, "authenticate", counting)
    assert sess.get("/auth/me").status_code == 200
    assert sess.post("/seat/acquire").status_code == 200
    assert len(calls) == 2  # 路由器级和参数级的依赖共用一次鉴权


def test_the_bearer_scheme_is_case_insensitive(kit):
    token = kit.login().token
    assert kit.get("/auth/me", headers={"Authorization": f"bearer {token}"}).status_code == 200


def test_a_token_in_the_query_string_is_not_accepted_for_http(kit):
    token = kit.login().token
    assert kit.get(f"/state?token={token}").status_code == 401


def test_the_local_token_works_as_the_cli_principal(kit):
    response = kit.get("/auth/me", token=kit.local_token)
    assert response.status_code == 200
    assert response.json() == {"operator": "cli", "session_id": None, "principal": "cli",
                               "reauth_valid_until_ms": None,
                               "seat": {"holder_session_id": None, "holder_operator": None,
                                        "lease_until_ms": None, "mine": False}}


# ---------------------------------------------------------------- 登录

def test_login_returns_exactly_the_agreed_fields(kit):
    kit.add_operator("admin")
    response = kit.post("/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == LOGIN_KEYS
    assert body["operator"] == "admin" and body["idle_timeout_min"] == 30 and body["session_id"] >= 1
    assert len(body["token"]) >= 43
    assert kit.get("/auth/me", token=body["token"]).json()["operator"] == "admin"


def test_login_failure_is_the_same_for_a_wrong_password_and_an_unknown_user(kit):
    kit.add_operator("admin")
    wrong = kit.post("/auth/login", json={"username": "admin", "password": "not-the-password"})
    unknown = kit.post("/auth/login", json={"username": "ghost", "password": PASSWORD})
    assert error_shape(wrong) == (401, "bad_credentials")
    assert unknown.status_code == wrong.status_code and unknown.json() == wrong.json()


@pytest.mark.parametrize("payload", [
    {}, {"username": "admin"}, {"password": PASSWORD}, {"username": "", "password": PASSWORD},
    {"username": "admin", "password": ""}, {"username": 5, "password": PASSWORD}, {"username": "x" * 65, "password": "p"},
    {"username": "admin", "password": "p" * 1025}, [], "text",
])
def test_malformed_login_bodies_are_422_invalid(kit, payload):
    response = kit.post("/auth/login", json=payload)
    assert error_shape(response) == (422, "invalid")


def test_a_login_body_that_is_not_json_is_422_invalid(kit):
    response = kit.client.post(BASE + "/auth/login", content=b"username=a&password=b",
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert error_shape(response) == (422, "invalid")


def test_validation_errors_never_echo_the_submitted_password(kit):
    secret = "s3cret-" + "p" * 1100
    response = kit.post("/auth/login", json={"username": "admin", "password": secret})
    assert error_shape(response) == (422, "invalid")
    assert "s3cret" not in response.text


def test_five_wrong_passwords_lock_the_username_and_the_answer_says_for_how_long(kit):
    kit.add_operator("admin")
    for _ in range(5):
        assert kit.post("/auth/login", json={"username": "admin", "password": "wrong-password"}).status_code == 401
    locked = kit.post("/auth/login", json={"username": "admin", "password": PASSWORD})  # 口令对也不放行
    assert error_shape(locked) == (429, "rate_limited")
    assert locked.json()["retry_after_s"] == 60 and locked.headers["retry-after"] == "60"
    kit.clock.advance(60_000)
    assert kit.post("/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 200


def test_the_login_lock_is_per_username(kit):
    kit.add_operator("admin")
    kit.add_operator("other")
    for _ in range(5):
        kit.post("/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert kit.post("/auth/login", json={"username": "other", "password": PASSWORD}).status_code == 200


def test_the_token_is_stored_only_as_a_hash(kit):
    sess = kit.login()
    base = Path(str(kit.ledger.path))
    raw = b"".join(path.read_bytes() for path in (base, Path(str(base) + "-wal")) if path.exists())
    assert sess.token.encode() not in raw and PASSWORD.encode() not in raw
    assert hashlib.sha256(sess.token.encode()).hexdigest().encode() in raw


# ---------------------------------------------------------------- 我是谁、退出、重新校验

def test_me_for_a_session(kit):
    sess = kit.login()
    body = sess.get("/auth/me").json()
    assert body == {"operator": "admin", "session_id": sess.session_id, "principal": "session",
                    "reauth_valid_until_ms": None,
                    "seat": {"holder_session_id": None, "holder_operator": None, "lease_until_ms": None,
                             "mine": False}}


def test_logout_returns_an_empty_object_and_kills_the_token(kit):
    sess = kit.login()
    response = sess.post("/auth/logout")
    assert response.status_code == 200 and response.json() == {}
    assert error_shape(sess.get("/auth/me")) == (401, "unauthorized")
    assert error_shape(sess.post("/auth/logout")) == (401, "unauthorized")  # 令牌已经失效


def test_the_cli_token_cannot_log_out_or_reauth(kit):
    cli = kit.cli()
    assert error_shape(cli.post("/auth/logout")) == (409, "not_allowed")
    assert error_shape(cli.post("/auth/reauth", json={"password": PASSWORD})) == (409, "not_allowed")


def test_reauth_returns_the_end_of_the_window_and_me_shows_it(kit):
    sess = kit.login()
    kit.clock.advance(7_000)
    response = sess.post("/auth/reauth", json={"password": PASSWORD})
    assert response.status_code == 200 and response.json() == {"reauth_valid_until_ms": T0 + 7_000 + 300_000}
    assert sess.get("/auth/me").json()["reauth_valid_until_ms"] == T0 + 7_000 + 300_000
    kit.clock.advance(300_000)  # 窗口到点即失效
    assert sess.get("/auth/me").json()["reauth_valid_until_ms"] is None


def test_reauth_with_the_wrong_password_is_401_and_records_nothing(kit):
    sess = kit.login()
    assert error_shape(sess.post("/auth/reauth", json={"password": "nope-nope"})) == (401, "bad_credentials")
    assert sess.get("/auth/me").json()["reauth_valid_until_ms"] is None


@pytest.mark.parametrize("payload", [None, {}, {"password": ""}, {"password": 1}])
def test_reauth_needs_a_password_in_the_body(kit, payload):
    sess = kit.login()
    assert error_shape(sess.post("/auth/reauth", json=payload)) == (422, "invalid")


def test_reauth_failures_count_against_the_login_limit(kit):
    sess = kit.login()
    for _ in range(5):
        sess.post("/auth/reauth", json={"password": "nope-nope"})
    assert error_shape(sess.post("/auth/reauth", json={"password": PASSWORD})) == (429, "rate_limited")
    assert error_shape(kit.post("/auth/login", json={"username": "admin", "password": PASSWORD})) == (
        429, "rate_limited")


def test_an_idle_session_expires_after_the_configured_minutes_and_activity_extends_it(kit):
    sess = kit.login()
    kit.clock.advance(29 * MIN)
    assert sess.get("/auth/me").status_code == 200
    kit.clock.advance(29 * MIN)  # 距登录 58 分钟，距上次活动 29 分钟
    assert sess.get("/auth/me").status_code == 200
    kit.clock.advance(30 * MIN + 1)
    response = sess.get("/auth/me")
    assert error_shape(response) == (401, "unauthorized") and "过期" in response.json()["message"]


# ---------------------------------------------------------------- 席位

def test_acquire_gives_the_seat_with_the_lease(kit):
    sess = kit.login()
    response = sess.post("/seat/acquire")
    assert response.status_code == 200
    assert response.json() == {"holder_session_id": sess.session_id, "holder_operator": "admin",
                               "lease_until_ms": T0 + 60_000, "mine": True}
    assert sess.get("/auth/me").json()["seat"] == response.json()


def test_only_one_session_holds_the_seat_and_the_refusal_names_the_holder(kit):
    alice, bob = kit.login("alice"), kit.login("bob")
    alice.post("/seat/acquire")
    response = bob.post("/seat/acquire")
    assert error_shape(response) == (409, "seat_taken")
    assert response.json()["holder_operator"] == "alice" and response.json()["lease_until_ms"] == T0 + 60_000
    seen_by_bob = bob.get("/auth/me").json()["seat"]
    assert seen_by_bob["holder_operator"] == "alice" and seen_by_bob["mine"] is False


def test_renew_extends_the_lease_and_only_the_holder_may(kit):
    alice, bob = kit.login("alice"), kit.login("bob")
    assert error_shape(alice.post("/seat/renew")) == (409, "seat_required")  # 还没申请
    alice.post("/seat/acquire")
    kit.clock.advance(20_000)
    assert alice.post("/seat/renew").json()["lease_until_ms"] == T0 + 20_000 + 60_000
    assert error_shape(bob.post("/seat/renew")) == (409, "seat_required")


def test_release_frees_the_seat_for_the_next_session(kit):
    alice, bob = kit.login("alice"), kit.login("bob")
    alice.post("/seat/acquire")
    released = alice.post("/seat/release")
    assert released.status_code == 200 and set(released.json()) == SEAT_KEYS
    assert released.json()["holder_session_id"] is None and released.json()["mine"] is False
    assert bob.post("/seat/acquire").json()["mine"] is True


def test_releasing_a_seat_you_do_not_hold_changes_nothing(kit):
    alice, bob = kit.login("alice"), kit.login("bob")
    alice.post("/seat/acquire")
    assert bob.post("/seat/release").json()["holder_operator"] == "alice"
    assert alice.get("/auth/me").json()["seat"]["mine"] is True


def test_the_lease_lapses_by_itself_and_the_seat_can_be_taken(kit):
    alice, bob = kit.login("alice"), kit.login("bob")
    alice.post("/seat/acquire")
    kit.clock.advance(59_999)
    assert error_shape(bob.post("/seat/acquire")) == (409, "seat_taken")
    kit.clock.advance(1)
    assert bob.get("/auth/me").json()["seat"]["holder_session_id"] is None
    assert error_shape(alice.post("/seat/renew")) == (409, "seat_required")  # 到期后不能续
    assert bob.post("/seat/acquire").json()["mine"] is True


def test_logging_out_releases_the_seat(kit):
    alice, bob = kit.login("alice"), kit.login("bob")
    alice.post("/seat/acquire")
    alice.post("/auth/logout")
    assert bob.get("/auth/me").json()["seat"]["holder_session_id"] is None


def test_the_cli_token_can_look_but_not_take_the_seat(kit):
    kit.login("alice").post("/seat/acquire")
    cli = kit.cli()
    assert cli.get("/auth/me").json()["seat"]["holder_operator"] == "alice"
    for path in ("/seat/acquire", "/seat/renew", "/seat/release"):
        assert error_shape(cli.post(path)) == (409, "not_allowed")
    assert error_shape(cli.post("/seat/force", json={"password": PASSWORD})) == (409, "not_allowed")


# ---------------------------------------------------------------- 强制接管

def audit(kit, **filters):
    return kit.ledger.read_sync(repo_audit.recent, limit=100, **filters)


def test_force_needs_the_password_checked_on_the_spot(kit):
    alice, bob = kit.login("alice"), kit.login("bob")
    alice.post("/seat/acquire")
    response = bob.post("/seat/force", json={"password": "not-bobs-password"})
    assert error_shape(response) == (401, "bad_credentials")
    assert alice.get("/auth/me").json()["seat"]["mine"] is True  # 口令错：席位纹丝不动
    assert audit(kit, action="seat.force") == []


def test_force_with_the_right_password_takes_the_seat_refreshes_reauth_and_is_logged(kit):
    alice, bob = kit.login("alice"), kit.login("bob")
    alice.post("/seat/acquire")
    kit.clock.advance(5_000)
    response = bob.post("/seat/force", json={"password": PASSWORD}, headers={"X-Request-Id": "req-42"})
    assert response.status_code == 200 and set(response.json()) == SEAT_KEYS
    assert response.json()["holder_session_id"] == bob.session_id and response.json()["mine"] is True
    assert alice.get("/auth/me").json()["seat"]["mine"] is False
    assert bob.get("/auth/me").json()["reauth_valid_until_ms"] == T0 + 5_000 + 300_000  # 当场校验并刷新
    (forced,) = audit(kit, action="seat.force")
    assert forced.operator == "bob" and forced.request_id == "req-42"
    assert forced.before["holder_operator"] == "alice" and forced.after["holder_operator"] == "bob"
    (reauth,) = audit(kit, action="auth.reauth")
    assert reauth.operator == "bob" and reauth.after == {"via": "seat_force"}
    shown = [item["action"] for item in bob.get("/oplog").json()["items"]]
    assert "seat.force" in shown  # 操作日志里看得到


def test_force_on_a_free_seat_works_too(kit):
    bob = kit.login("bob")
    assert bob.post("/seat/force", json={"password": PASSWORD}).json()["mine"] is True


@pytest.mark.parametrize("payload", [None, {}, {"password": ""}])
def test_force_without_a_password_is_422(kit, payload):
    assert error_shape(kit.login().post("/seat/force", json=payload)) == (422, "invalid")


def test_a_request_id_that_is_not_plain_text_is_not_recorded(kit):
    bob = kit.login("bob")
    bob.post("/seat/force", json={"password": PASSWORD}, headers={"X-Request-Id": "bad id with spaces\t"})
    (forced,) = audit(kit, action="seat.force")
    assert forced.request_id is None
