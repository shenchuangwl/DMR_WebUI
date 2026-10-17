"""控制接口的配置修改与操作员创建：``PUT /config/{key}``、``POST /operators``（经 starlette TestClient，进程内；
服务是真实实现 + 临时账本，见 api_kit.Kit）。

PUT /config/{key} {op_id, value}：会话需持有操作席位，本机令牌（cli 主体）不占席位；校验失败 422 invalid（ConfigError 原文）；
同一个 op_id 只执行一次。POST /operators {username, password}：只允许本机令牌，会话令牌一律 403，用户名已存在 409。
"""
from __future__ import annotations

import io
import logging
import stat
from datetime import timezone

import pytest

from api_kit import PASSWORD, T0
from dmr_kernel.cli.client import HttpResponse
from dmr_kernel.cli.main import main as cli_main
from dmr_kernel.core import configdefs
from dmr_kernel.core.configdefs import APPLY_MODES, ConfigError
from dmr_kernel.core.types import Environment
from dmr_kernel.ledger import repo_audit, repo_auth, repo_operations

MIN = 60_000


def error_shape(response) -> tuple[int, str]:
    body = response.json()
    assert set(body) >= {"code", "message"} and isinstance(body["message"], str) and body["message"]
    return response.status_code, body["code"]


def config_error_text(key: str, value) -> str:
    with pytest.raises(ConfigError) as caught:
        configdefs.validate_value(key, value)
    return str(caught.value)


def initial(key: str):
    return configdefs.initial_values(Environment.STANDIN)[key]


def change(who, key: str, value, op_id: str = "op-1"):
    return who.put(f"/config/{key}", json={"op_id": op_id, "value": value})


@pytest.fixture
def seated(kit):
    """已登录且持有操作席位的会话。"""
    sess = kit.login("alice")
    assert sess.post("/seat/acquire").status_code == 200
    return sess


def audit(kit, **filters):
    return kit.ledger.read_sync(repo_audit.recent, **filters)


def operation_count(kit) -> int:
    return kit.row_counts()["operation"]


# ====================================================================== PUT /config/{key}


def test_changing_config_needs_a_token_and_checks_it_before_looking_at_the_body(kit):
    for body in ({"op_id": "op-1", "value": 3}, {"junk": True}, None):
        response = kit.put("/config/leverage", json=body)
        assert error_shape(response) == (401, "unauthorized")
    assert error_shape(kit.put("/config/leverage", token="wrong-token", json={"op_id": "op-1", "value": 3})) == (
        401, "unauthorized")
    assert kit.config.get("leverage") == 2


def test_a_session_without_the_seat_cannot_change_config(kit):
    sess = kit.login("alice")
    before = kit.dump()
    response = change(sess, "leverage", 3)
    assert error_shape(response) == (409, "seat_required")
    assert kit.config.get("leverage") == 2
    assert kit.dump() == before  # 什么都没记：没有操作记录，没有审计


def test_the_seat_is_checked_before_the_body_is_validated(kit):
    sess = kit.login("alice")
    for body in ({"junk": True}, {"op_id": "has space", "value": 3}, [1, 2]):
        assert error_shape(sess.put("/config/leverage", json=body)) == (409, "seat_required")


def test_the_seat_holder_changes_config_and_is_told_when_it_takes_effect(kit, seated):
    response = change(seated, "leverage", 3)
    assert response.status_code == 200
    assert response.json() == {"key": "leverage", "old": 2, "new": 3,
                               "applies": APPLY_MODES["next_signal"], "affects_existing": False}
    assert kit.config.get("leverage") == 3
    shown = {item["key"]: item for item in seated.get("/config").json()["items"]}
    assert shown["leverage"]["value"] == 3


def test_a_change_that_affects_existing_positions_says_so(kit, seated):
    body = change(seated, "stop_trigger_price_type", "MARK_PRICE").json()
    assert body["affects_existing"] is True and body["applies"] == APPLY_MODES["next_stop"]


def test_amounts_are_json_strings_like_everywhere_else_in_the_interface(kit, seated):
    body = change(seated, "trade_notional_usdt", "25.5").json()
    assert body["old"] == "10" and body["new"] == "25.5"
    assert change(seated, "trade_notional_usdt", "25.5").json() == body  # 同一个操作号的重放：同样的 JSON


def test_a_session_whose_seat_lease_has_lapsed_is_refused(kit, seated):
    kit.clock.advance(60_000 + 1)
    assert error_shape(change(seated, "leverage", 3)) == (409, "seat_required")
    assert kit.config.get("leverage") == 2


def test_a_session_cannot_use_a_seat_somebody_else_holds(kit, seated):
    bob = kit.login("bob")
    assert error_shape(change(bob, "leverage", 3)) == (409, "seat_required")
    assert seated.post("/seat/release").status_code == 200
    assert bob.post("/seat/acquire").status_code == 200
    assert change(bob, "leverage", 3).status_code == 200


def test_a_forced_takeover_of_the_seat_moves_the_right_to_change_config(kit, seated):
    bob = kit.login("bob")
    assert bob.post("/seat/force", json={"password": PASSWORD}).status_code == 200
    assert error_shape(change(seated, "leverage", 3)) == (409, "seat_required")
    assert change(bob, "leverage", 3).status_code == 200


def test_the_local_token_needs_no_seat_and_is_recorded_as_cli(kit):
    cli = kit.cli()
    response = change(cli, "leverage", 4)
    assert response.status_code == 200 and response.json()["new"] == 4
    assert kit.config.get("leverage") == 4
    row = kit.ledger.read_sync(repo_audit.recent, action="config.set")[0]
    assert row.operator == "cli" and row.target == "leverage"
    operation = kit.ledger.read_sync(repo_operations.get, "op-1")
    assert operation.operator == "cli" and operation.session_id is None  # 本机令牌没有会话


def test_the_local_token_does_not_take_the_seat(kit):
    change(kit.cli(), "leverage", 4)
    assert kit.login("alice").get("/auth/me").json()["seat"]["holder_session_id"] is None


def test_the_operation_log_names_the_session_operator(kit, seated):
    change(seated, "leverage", 3)
    items = seated.get("/oplog").json()["items"]
    changes = [item for item in items if item["action"] == "config.set"]
    assert len(changes) == 1 and changes[0]["operator"] == "alice" and changes[0]["target"] == "leverage"
    assert changes[0]["before"] == {"value": 2} and changes[0]["after"] == {"value": 3}
    actions = [item["action"] for item in reversed(items) if item["action"].startswith(("operation.", "config."))]
    assert actions == ["operation.begin", "config.set", "operation.finish"]


# ---------------------------------------------------------------- 校验失败：422 invalid，ConfigError 原文


@pytest.mark.parametrize("key, value", [
    ("leverage", 99), ("leverage", "abc"), ("leverage", None), ("margin_mode", "weird"),
    ("trade_notional_usdt", "0.5"), ("trade_notional_usdt", "abc"), ("takeover_symbols", ["bad symbol!"]),
    ("correction_signal", True)])
def test_a_bad_value_is_422_invalid_with_the_config_error_text(kit, seated, key, value):
    response = change(seated, key, value)
    assert error_shape(response) == (422, "invalid")
    assert response.json()["message"] == config_error_text(key, value)
    assert kit.config.get(key) == initial(key)


def test_a_conflict_with_other_items_is_422_with_the_reason(kit, seated):
    response = change(seated, "leverage", 20)
    assert error_shape(response) == (422, "invalid")
    assert "强平距离" in response.json()["message"] and "与其它配置冲突" in response.json()["message"]
    assert kit.config.get("leverage") == 2


def test_the_stop_band_must_stay_ordered(kit, seated):
    response = change(seated, "init_stop_min_pct", "6")
    assert error_shape(response) == (422, "invalid") and "A < B" in response.json()["message"]


def test_an_unknown_key_is_422_and_leaves_no_trace(kit, seated):
    before = kit.dump()
    response = change(seated, "no_such_item", 1)
    assert error_shape(response) == (422, "invalid") and "未知的配置项" in response.json()["message"]
    assert kit.dump() == before


@pytest.mark.parametrize("key, value", [("m3_ai_tier", "shadow"), ("m3_master_switch", True),
                                        ("m3_max_notional_usdt", "100")])
def test_model3_tiers_are_refused_without_the_deploy_switch(kit, seated, key, value):
    for who in (seated, kit.cli()):
        response = change(who, key, value, op_id=f"op-{who.username}")
        assert error_shape(response) == (422, "invalid") and "部署级开关" in response.json()["message"]
    assert kit.config.get(key) == initial(key)


def test_a_rejected_change_is_recorded_as_a_rejected_operation(kit, seated):
    change(seated, "leverage", 99, op_id="op-bad")
    rows = [r for r in audit(kit) if r.target == "op-bad"]
    assert [r.action for r in reversed(rows)] == ["operation.begin", "operation.finish"]
    assert rows[0].result == "rejected" and rows[0].operator == "alice"
    assert not audit(kit, action="config.set")


# ---------------------------------------------------------------- 同一个 op_id 只执行一次


def test_a_repeated_op_id_returns_the_first_answer_and_changes_nothing(kit, seated):
    first = change(seated, "leverage", 3)
    before = kit.dump()
    second = change(seated, "leverage", 5)
    assert second.status_code == 200 and second.json() == first.json()
    assert kit.config.get("leverage") == 3
    assert kit.dump() == before


def test_a_replay_is_not_undone_by_a_later_change(kit, seated):
    first = change(seated, "leverage", 3, op_id="op-a")
    change(seated, "leverage", 4, op_id="op-b")
    assert change(seated, "leverage", 3, op_id="op-a").json() == first.json()
    assert kit.config.get("leverage") == 4


def test_a_rejected_op_id_replays_as_the_same_422(kit, seated):
    first = change(seated, "leverage", 99)
    again = change(seated, "leverage", 3)  # 同一个操作号：第一次的拒绝为准，不重新执行
    assert (again.status_code, again.json()) == (first.status_code, first.json()) and first.status_code == 422
    assert kit.config.get("leverage") == 2


def test_the_local_token_and_sessions_share_op_id_deduplication(kit, seated):
    first = change(kit.cli(), "leverage", 3)
    assert change(seated, "leverage", 5).json() == first.json()
    assert kit.config.get("leverage") == 3


# ---------------------------------------------------------------- 请求体


@pytest.mark.parametrize("body", [
    None, [], [1, 2], "text", {}, {"value": 3}, {"op_id": "op-1"}, {"op_id": "", "value": 3},
    {"op_id": "has space", "value": 3}, {"op_id": "x" * 65, "value": 3}, {"op_id": 5, "value": 3},
    {"op_id": "带中文", "value": 3}, {"op_id": None, "value": 3}])
def test_a_missing_or_malformed_body_is_422_and_leaves_no_trace(kit, seated, body):
    before = kit.dump()
    response = seated.put("/config/leverage", json=body)
    assert error_shape(response) == (422, "invalid")
    assert kit.config.get("leverage") == 2
    assert kit.dump() == before


def test_a_body_that_is_not_json_at_all_is_422(kit, seated):
    response = kit.put("/config/leverage", token=seated.token, content=b"{not json",
                       headers={"Content-Type": "application/json"})
    assert error_shape(response) == (422, "invalid")


def test_extra_fields_in_the_body_are_ignored(kit, seated):
    assert seated.put("/config/leverage", json={"op_id": "op-1", "value": 3, "role": "root"}).status_code == 200


def test_an_explicit_null_value_is_a_value_error_not_a_missing_field(kit, seated):
    response = change(seated, "leverage", None)
    assert error_shape(response) == (422, "invalid") and "不能为空" in response.json()["message"]


def test_other_methods_on_the_config_path_are_not_there(kit, seated):
    assert error_shape(seated.post("/config/leverage", json={"op_id": "op-1", "value": 3})) == (404, "not_found")
    assert error_shape(seated.get("/config/leverage")) == (404, "not_found")


def test_listing_the_config_still_works_next_to_the_new_route(kit, seated):
    change(seated, "leverage", 3)
    assert {item["key"] for item in seated.get("/config").json()["items"]} == set(configdefs.DEFINITIONS)


# ====================================================================== POST /operators

NEW_PASSWORD = "a-fresh-Password-123"


def create(who, username: str = "carol", password: str = NEW_PASSWORD):
    return who.post("/operators", json={"username": username, "password": password})


def test_creating_an_operator_needs_a_token(kit):
    assert error_shape(kit.post("/operators", json={"username": "carol", "password": NEW_PASSWORD})) == (
        401, "unauthorized")
    assert error_shape(kit.post("/operators", token="wrong-token", json={})) == (401, "unauthorized")
    assert kit.ledger.read_sync(repo_auth.list_operators) == []


def test_the_local_token_creates_an_operator_who_can_then_log_in(kit):
    response = create(kit.cli())
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "username", "role", "created_ms"}
    assert body["username"] == "carol" and body["role"] == "admin" and body["created_ms"] == T0
    login = kit.post("/auth/login", json={"username": "carol", "password": NEW_PASSWORD})
    assert login.status_code == 200 and login.json()["operator"] == "carol"


def test_the_password_is_stored_as_a_scrypt_hash_and_never_returned(kit):
    response = create(kit.cli())
    assert NEW_PASSWORD not in response.text
    stored = kit.ledger.read_sync(repo_auth.get_operator_by_name, "carol")
    assert stored.password_hash.startswith("scrypt$") and NEW_PASSWORD not in stored.password_hash
    assert NEW_PASSWORD not in repr(kit.dump())  # 任何一张表里都没有明文
    assert "password" not in response.text.lower()


def test_creating_an_operator_is_audited_as_done_by_cli(kit):
    create(kit.cli())
    row = kit.ledger.read_sync(repo_audit.recent, action="operator.create")[0]
    assert row.operator == "cli" and row.target == "operator:carol"
    assert row.after == {"username": "carol", "role": "admin"} and row.result == "ok"
    assert NEW_PASSWORD not in repr(kit.dump())


def test_no_session_may_create_operators_not_even_one_holding_the_seat(kit, seated):
    before = kit.dump()
    response = create(seated)
    assert error_shape(response) == (403, "forbidden")
    assert "本机" in response.json()["message"]
    assert kit.ledger.read_sync(repo_auth.get_operator_by_name, "carol") is None
    assert kit.dump() == before


def test_the_session_refusal_comes_before_the_body_is_looked_at(kit):
    sess = kit.login("alice")
    for body in (None, {}, {"username": "x"}, {"username": "carol", "password": "short"}):
        assert error_shape(sess.post("/operators", json=body)) == (403, "forbidden")


def test_an_operator_created_this_way_is_a_session_too_and_equally_refused(kit):
    create(kit.cli())
    carol = kit.login("carol", NEW_PASSWORD, create=False)
    assert error_shape(create(carol, "dave")) == (403, "forbidden")


def test_a_taken_username_is_409_and_the_existing_operator_is_untouched(kit):
    create(kit.cli())
    before = kit.dump()
    response = create(kit.cli(), password="another-Password-456")
    assert error_shape(response) == (409, "operator_exists") and "carol" in response.json()["message"]
    assert kit.dump() == before
    assert kit.post("/auth/login", json={"username": "carol", "password": NEW_PASSWORD}).status_code == 200
    assert kit.post("/auth/login", json={"username": "carol", "password": "another-Password-456"}).status_code == 401


def test_an_operator_added_by_other_means_also_blocks_the_name(kit):
    kit.add_operator("carol")
    assert error_shape(create(kit.cli())) == (409, "operator_exists")


@pytest.mark.parametrize("username", ["a b", "x" * 33, "名字", "a/b", "semi;colon", "tab\tname"])
def test_a_username_outside_the_allowed_characters_is_422(kit, username):
    response = create(kit.cli(), username)
    assert error_shape(response) == (422, "invalid") and "用户名" in response.json()["message"]
    assert kit.ledger.read_sync(repo_auth.list_operators) == []


@pytest.mark.parametrize("password, fragment", [("short", "口令长度"), ("1234567", "口令长度"),
                                                pytest.param("x" * 1025, None, id="too-long")])
def test_a_password_of_the_wrong_length_is_422_and_not_echoed(kit, password, fragment):
    response = create(kit.cli(), password=password)
    assert error_shape(response) == (422, "invalid")
    if fragment:
        assert fragment in response.json()["message"]
    assert password not in response.text
    assert kit.ledger.read_sync(repo_auth.list_operators) == []


@pytest.mark.parametrize("body", [None, [], {}, {"username": "carol"}, {"password": NEW_PASSWORD},
                                  {"username": "", "password": NEW_PASSWORD}, {"username": "carol", "password": ""},
                                  {"username": 5, "password": NEW_PASSWORD}, {"username": "carol", "password": 12345678}])
def test_a_missing_or_malformed_body_is_422(kit, body):
    response = kit.cli().post("/operators", json=body)
    assert error_shape(response) == (422, "invalid")
    assert kit.ledger.read_sync(repo_auth.list_operators) == []


def test_the_requested_role_is_ignored_every_operator_is_an_admin(kit):
    response = kit.cli().post("/operators", json={"username": "carol", "password": NEW_PASSWORD, "role": "root"})
    assert response.status_code == 201 and response.json()["role"] == "admin"


def test_the_password_never_reaches_the_log_whether_creation_works_or_not(kit, caplog):
    caplog.set_level(logging.DEBUG)
    create(kit.cli())
    create(kit.cli())  # 409
    create(kit.cli(), "bad name", NEW_PASSWORD + "-2")  # 422
    kit.cli().post("/operators", json={"username": "erin", "password": NEW_PASSWORD + "-3", "junk": [1]})
    for secret in (NEW_PASSWORD, NEW_PASSWORD + "-2", NEW_PASSWORD + "-3"):
        assert secret not in caplog.text
        assert all(secret not in str(record.args) for record in caplog.records)


def test_the_operators_path_does_not_answer_other_methods(kit):
    assert error_shape(kit.cli().get("/operators")) == (404, "not_found")
    assert error_shape(kit.request("DELETE", "/operators", token=kit.local_token)) == (404, "not_found")


# ====================================================================== 命令行 <-> 真实的控制接口应用（进程内，不开套接字）


class AppTransport:
    """命令行的传输：请求直接交给进程内的应用，走完整的鉴权、校验与落库，代替真实的套接字。"""

    def __init__(self, kit) -> None:
        self.kit = kit

    def request(self, method, path, *, headers, body=None):
        reply = self.kit.client.request(method, path, headers=dict(headers), content=body)
        return HttpResponse(reply.status_code, reply.content)


def cli(kit, *argv: str, token: str | None = None) -> tuple[int, str, str]:
    """在内核的运行目录上跑一次命令行，令牌文件里放 ``token``（缺省是内核的本机令牌）；返回（退出码，标准输出，标准错误）。"""
    token_path = kit.settings.local_token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text((token or kit.local_token) + "\n", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    code = cli_main(list(argv), env={"DMR_VAR_DIR": str(kit.settings.var_dir)}, transport_factory=lambda _: AppTransport(kit),
                    stdout=out, stderr=err, tz=timezone.utc)
    return code, out.getvalue(), err.getvalue()


def password_file(kit, username: str = "carol"):
    return kit.settings.var_dir / "operators" / f"{username}.password"


def test_the_command_line_status_prints_the_real_state_of_the_app_in_chinese(kit):
    kit.add_position()
    code, out, err = cli(kit, "status")
    assert (code, err) == (0, "")
    lines = {line.split("：", 1)[0]: line for line in out.splitlines() if "：" in line}
    assert "替身环境 · 非真实交易" in lines["环境"]
    assert "模型 1" in lines["生效模型"] and "已停止开仓" in lines["开仓闸"] and "托管中 1" in lines["开仓闸"]
    counts = kit.cli().get("/state").json()["counts"]  # 命令行显示的数就是接口给的数
    assert counts["managed"] == 1
    for name, key in (("托管中", "managed"), ("无保护", "unprotected"), ("接管中", "taking_over"), ("平仓中", "closing"),
                      ("待确认", "pending"), ("外部", "external")):
        assert f"{name} {counts[key]}" in lines["持仓"]
    assert "无人持有" in lines["操作席位"]
    assert "cli" in lines["当前身份"] and "本机令牌" in lines["当前身份"]
    assert kit.local_token not in out


def test_the_command_line_status_changes_nothing_in_the_app(kit):
    before = kit.dump()
    cli(kit, "status")
    assert kit.dump() == before


def test_an_operator_created_by_the_command_line_can_log_in_with_the_password_in_the_file(kit):
    code, out, err = cli(kit, "create-operator", "--username", "carol")
    assert (code, err) == (0, "")
    path = password_file(kit)
    password = path.read_text(encoding="utf-8").strip()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert str(path) in out and "carol" in out and password not in out + err and kit.local_token not in out + err
    login = kit.post("/auth/login", json={"username": "carol", "password": password})
    assert login.status_code == 200 and login.json()["operator"] == "carol"
    row = kit.ledger.read_sync(repo_audit.recent, action="operator.create")[0]
    assert row.operator == "cli" and row.target == "operator:carol"
    stored = kit.ledger.read_sync(repo_auth.get_operator_by_name, "carol")
    assert stored.password_hash.startswith("scrypt$") and password not in repr(kit.dump())


def test_the_password_the_command_line_makes_passes_the_kernels_checks_every_time(kit):
    for index in range(3):
        assert cli(kit, "create-operator", "--username", f"user{index}")[0] == 0
    assert {op.username for op in kit.ledger.read_sync(repo_auth.list_operators)} == {"user0", "user1", "user2"}


def test_a_taken_name_is_explained_in_the_kernels_words_and_leaves_no_file_behind(kit):
    kit.add_operator("carol")
    code, out, err = cli(kit, "create-operator", "--username", "carol")
    assert code == 1 and out == ""
    assert "用户名已存在：carol" in err and "operator_exists" in err and "口令文件已删除" in err
    assert not password_file(kit).exists()
    assert kit.post("/auth/login", json={"username": "carol", "password": PASSWORD}).status_code == 200  # 原来的口令不受影响


def test_a_wrong_token_in_the_token_file_is_refused_and_explained(kit):
    wrong = "not-the-kernels-token-" + "x" * 30
    for argv in (("status",), ("create-operator", "--username", "carol")):
        code, out, err = cli(kit, *argv, token=wrong)
        assert code == 1 and out == "" and "本机令牌" in err and "local.token" in err
        assert wrong not in err
    assert not password_file(kit).exists() and kit.ledger.read_sync(repo_auth.list_operators) == []


def test_a_session_token_in_the_token_file_cannot_create_operators(kit):
    session = kit.login("alice")
    code, out, err = cli(kit, "create-operator", "--username", "carol", token=session.token)
    assert code == 1 and "forbidden" in err and "只有本机命令行" in err and "口令文件已删除" in err
    assert not password_file(kit).exists() and kit.ledger.read_sync(repo_auth.get_operator_by_name, "carol") is None
    assert session.token not in err
