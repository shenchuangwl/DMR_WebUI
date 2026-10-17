"""控制接口的收尾改进（FU5）：

- ``errors.forbidden`` / ``errors.operator_exists`` 两个帮手与代码表，``routes_admin`` 改用它们；
- ``routes_ops`` 删掉被 ``routes_config`` 遮蔽的 ``PUT /config/{key}`` 占位路由；
- ``GET /operations/{op_id}`` 接上 ``ops.operations.Operations.get``：返回操作记录，不存在 404，只读。

服务是真实实现 + 临时账本（见 api_kit.Kit）；操作记录经 ``Operations`` 或真实的 ``PUT /config/{key}`` 造出来。
"""
from __future__ import annotations

import pytest

from api_kit import PASSWORD, T0
from dmr_kernel.api import errors, routes_ops
from dmr_kernel.api.errors import ApiError
from dmr_kernel.core.events import OperationProgress
from dmr_kernel.core.types import OperationStatus, OperationType
from dmr_kernel.ledger.records import OperationRecord
from dmr_kernel.ops.operations import Operations

OPERATION_KEYS = {"op_id", "op_type", "status", "operator", "started_ms", "ended_ms", "params", "result",
                  "per_position"}


def error_shape(response) -> tuple[int, str]:
    body = response.json()
    assert set(body) >= {"code", "message"} and isinstance(body["message"], str) and body["message"]
    return response.status_code, body["code"]


# ====================================================================== errors.forbidden / errors.operator_exists


def test_forbidden_is_a_403_carrying_the_given_message():
    error = errors.forbidden("只有本机命令行可以这样做")
    assert (error.status, error.code) == (403, "forbidden")
    assert error.body() == {"code": "forbidden", "message": "只有本机命令行可以这样做"}


def test_operator_exists_is_a_409_that_names_the_username():
    error = errors.operator_exists("carol")
    assert (error.status, error.code) == (409, "operator_exists")
    body = error.body()
    assert set(body) == {"code", "message"} and "carol" in body["message"]


def test_the_code_table_and_the_exports_list_the_two_new_codes():
    for code, status in (("forbidden", 403), ("operator_exists", 409)):
        assert f"{code} {status}" in errors.__doc__
        assert code in errors.__all__
        assert callable(getattr(errors, code))


def test_the_admin_route_raises_its_errors_through_the_shared_helpers(kit, monkeypatch):
    """换成能认出来的版本：路由若还自己拼 ``ApiError``，响应里就看不到这两句。"""
    monkeypatch.setattr(errors, "forbidden", lambda *args, **kwargs: ApiError(403, "forbidden", "FORBIDDEN-HELPER"))
    monkeypatch.setattr(errors, "operator_exists",
                        lambda *args, **kwargs: ApiError(409, "operator_exists", "EXISTS-HELPER"))
    body = {"username": "carol", "password": PASSWORD}
    refused = kit.login("alice").post("/operators", json=body)
    assert error_shape(refused) == (403, "forbidden") and refused.json()["message"] == "FORBIDDEN-HELPER"
    kit.add_operator("carol")
    taken = kit.cli().post("/operators", json=body)
    assert error_shape(taken) == (409, "operator_exists") and taken.json()["message"] == "EXISTS-HELPER"


def test_the_admin_errors_keep_their_wire_format_with_the_real_helpers(kit):
    """正向：不换帮手时，报文与之前完全一样（403 讲清楚只有本机命令行可以，409 带用户名）。"""
    body = {"username": "carol", "password": PASSWORD}
    refused = kit.login("alice").post("/operators", json=body)
    assert error_shape(refused) == (403, "forbidden") and "本机" in refused.json()["message"]
    assert kit.cli().post("/operators", json=body).status_code == 201
    taken = kit.cli().post("/operators", json=body)
    assert error_shape(taken) == (409, "operator_exists") and "carol" in taken.json()["message"]


# ====================================================================== routes_ops：没有被遮蔽的 PUT /config/{key} 占位


def test_routes_ops_has_no_placeholder_for_the_config_path():
    for router in (routes_ops.ops, routes_ops.dev):
        assert [route.path for route in router.routes if route.path.startswith("/config")] == []
        assert not any("PUT" in route.methods for route in router.routes)


def test_routes_ops_still_holds_every_operation_and_dev_placeholder():
    """只删了配置那一条：其余占位都还在，仍然只收 POST。"""
    assert sorted(route.path for route in routes_ops.ops.routes) == sorted(routes_ops.OPS_PATHS)
    assert sorted(route.path for route in routes_ops.dev.routes) == sorted(routes_ops.DEV_PATHS)
    assert all(route.methods == {"POST"} for route in [*routes_ops.ops.routes, *routes_ops.dev.routes])


def test_the_config_route_still_answers_through_routes_config(kit):
    """删掉占位之后 ``PUT /config/{key}`` 照常由 routes_config 处理（不是 501，也不是 404）。"""
    response = kit.cli().put("/config/leverage", json={"op_id": "op-1", "value": 3})
    assert response.status_code == 200 and response.json()["new"] == 3
    assert kit.config.get("leverage") == 3


# ====================================================================== GET /operations/{op_id}


def seed_operation(kit, op_id: str = "op-1", *, op_type=OperationType.CLOSE_ALL, operator: str = "alice",
                   session_id: int | None = 1, scope: str | None = "managed", params=None, finish=None,
                   elapsed_ms: int = 5_000) -> OperationRecord:
    """经 ``Operations`` 登记一个操作（可选地结束它）：``finish`` = (状态, 结果, 逐仓结果)。"""
    operations = Operations(kit.ledger, kit.clock, kit.bus)

    async def go() -> OperationRecord:
        record, _ = await operations.begin(op_id, op_type, operator=operator, session_id=session_id, scope=scope,
                                           state_version=7, params={"scope": "managed"} if params is None else params)
        if finish is not None:
            kit.clock.advance(elapsed_ms)
            status, result, per_position = finish
            record = await operations.finish(op_id, status, result=result, per_position=per_position)
        return record

    return kit.run(go())


def test_a_running_operation_is_returned_with_its_record_fields(kit):
    seed_operation(kit)
    response = kit.login("alice").get("/operations/op-1")
    assert response.status_code == 200
    assert response.json() == {"op_id": "op-1", "op_type": "close_all", "status": "running", "operator": "alice",
                               "started_ms": T0, "ended_ms": None, "params": {"scope": "managed"}, "result": None,
                               "per_position": None}


def test_a_finished_operation_carries_its_result_and_the_per_position_results(kit):
    per_position = [{"position_id": 1, "symbol": "BTCUSDT", "result": "closed"},
                    {"position_id": 2, "symbol": "ETHUSDT", "result": "failed", "detail": "被拒绝"}]
    seed_operation(kit, finish=(OperationStatus.PARTIAL, {"closed": 1, "failed": 1}, per_position))
    body = kit.login("alice").get("/operations/op-1").json()
    assert set(body) == OPERATION_KEYS
    assert body["status"] == "partial" and body["started_ms"] == T0 and body["ended_ms"] == T0 + 5_000
    assert body["result"] == {"closed": 1, "failed": 1} and body["per_position"] == per_position


def test_every_operation_status_and_type_comes_out_as_its_plain_value(kit):
    seed_operation(kit, "op-a", op_type=OperationType.SWITCH, params={},
                   finish=(OperationStatus.ABORTED, {"reason": "用户中止"}, None))
    body = kit.login("alice").get("/operations/op-a").json()
    assert (body["op_type"], body["status"], body["per_position"]) == ("switch", "aborted", None)


def test_the_local_token_can_read_operations_too_and_a_session_sees_the_clis(kit):
    seed_operation(kit, "cli-op", operator="cli", session_id=None, op_type=OperationType.CONFIG, params={"key": "x"})
    for who in (kit.cli(), kit.login("alice")):
        body = who.get("/operations/cli-op").json()
        assert body["operator"] == "cli" and body["op_type"] == "config" and body["params"] == {"key": "x"}


def test_the_operation_of_a_real_config_change_can_be_read_back(kit):
    cli = kit.cli()
    assert cli.put("/config/leverage", json={"op_id": "cfg-1", "value": 3}).status_code == 200
    body = cli.get("/operations/cfg-1").json()
    assert set(body) == OPERATION_KEYS
    assert (body["op_type"], body["status"], body["operator"]) == ("config", "succeeded", "cli")
    assert body["params"] == {"key": "leverage", "value": 3}
    assert body["result"]["new"] == 3 and body["ended_ms"] is not None


def test_an_unknown_operation_is_404_not_found(kit):
    sess = kit.login("alice")
    seed_operation(kit, "op-known")
    response = sess.get("/operations/op-unknown")
    assert error_shape(response) == (404, "not_found")
    assert sess.get("/operations/op-known").status_code == 200  # 有记录的不受影响


@pytest.mark.parametrize("op_id", ["bad%20id", "x" * 65, "semi;colon", "%E6%93%8D%E4%BD%9C", "a%2Fb"])
def test_an_operation_number_that_could_never_exist_is_404_not_a_server_error(kit, op_id):
    assert error_shape(kit.login("alice").get(f"/operations/{op_id}")) == (404, "not_found")


def test_the_longest_legal_operation_number_can_be_read(kit):
    long_id = "A" * 32 + ".:-_" * 8  # 64 个字符，全是合法字符
    assert len(long_id) == 64
    seed_operation(kit, long_id)
    assert kit.login("alice").get(f"/operations/{long_id}").json()["op_id"] == long_id


def test_secrets_in_operation_params_stay_masked_when_read_back(kit):
    seed_operation(kit, params={"password": "hunter2", "scope": "all", "nested": {"api_token": "tok-123"}})
    response = kit.login("alice").get("/operations/op-1")
    assert response.json()["params"] == {"password": "***", "scope": "all", "nested": {"api_token": "***"}}
    assert "hunter2" not in response.text and "tok-123" not in response.text


def test_reading_an_operation_writes_nothing_and_publishes_nothing(kit):
    sess = kit.login("alice")
    seed_operation(kit, finish=(OperationStatus.SUCCEEDED, {"ok": True}, None))
    kit.settle()
    seen: list = []

    async def watch() -> None:  # 总线已经在应用的事件循环里跑起来：订阅要在那个循环里做
        kit.bus.subscribe(OperationProgress, seen.append, name="test.progress")

    kit.run(watch())
    before_dump, before_counts = kit.dump(), kit.row_counts()
    for _ in range(3):
        kit.clock.advance(40_000)
        assert sess.get("/operations/op-1").status_code == 200
        assert sess.get("/operations/never-seen").status_code == 404
    kit.settle()
    assert kit.dump() == before_dump and kit.row_counts() == before_counts  # 没有审计行，没有新记录
    assert seen == []


def test_a_repeated_read_returns_the_same_record(kit):
    seed_operation(kit, finish=(OperationStatus.SUCCEEDED, {"ok": True}, None))
    sess = kit.login("alice")
    first, second = sess.get("/operations/op-1"), sess.get("/operations/op-1")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and first.json()["status"] == "succeeded"


def test_the_route_needs_a_token(kit):
    seed_operation(kit)
    assert error_shape(kit.get("/operations/op-1")) == (401, "unauthorized")
    assert error_shape(kit.get("/operations/op-1", token="not-a-token")) == (401, "unauthorized")


def test_the_route_uses_the_operations_service_when_the_assembly_provides_one(kit):
    """装配层以后把 ``operations`` 放进 ``app.state.services`` 时，路由用它（不再自己从账本构造）。"""
    stored = OperationRecord(id=1, op_id="from-services", op_type=OperationType.STOP, scope=None,
                             status=OperationStatus.SUCCEEDED, operator="zed", session_id=None, state_version=None,
                             params={"close_all": False}, per_position=None, result={"gate": "stopped"},
                             started_ms=T0, ended_ms=T0 + 1)

    class FakeOperations:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def get(self, op_id: str):
            self.asked.append(op_id)
            return stored if op_id == stored.op_id else None

    fake = FakeOperations()
    kit.services.operations = fake
    sess = kit.login("alice")
    body = sess.get("/operations/from-services").json()
    assert body["operator"] == "zed" and body["result"] == {"gate": "stopped"} and body["op_type"] == "stop"
    assert error_shape(sess.get("/operations/other")) == (404, "not_found")
    assert fake.asked == ["from-services", "other"]
