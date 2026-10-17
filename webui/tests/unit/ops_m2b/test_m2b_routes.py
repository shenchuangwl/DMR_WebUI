"""``api/routes_bulk.py``：一键全平与模型切换的控制接口（计划第 2 节的报文约定）。

建一个只挂这个路由的小应用（真实的登录与席位服务 + 假世界里的 CloseAll/Switch），用 httpx 的 ASGI 传输在同一个事件循环里访问。
覆盖统一步骤的路由部分：鉴权、席位（409 seat_required）、口令（403 reauth_required / 401 bad_credentials / 近期校验过则免）、
内核侧拒绝的错误码、报文形状、本机令牌（不占席位、免口令；切换除外）、口令不落库。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from dmr_kernel.api import errors, routes_bulk
from dmr_kernel.api.errors import ApiError
from dmr_kernel.ledger import repo_auth
from dmr_kernel.core.types import Gate, Model, OperationStatus, OpLock, Ownership, PositionStatus
from dmr_kernel.ops.switch import BulkOps
from tests.unit.api.api_kit import PASSWORD, AuthRig
from tests.unit.ops_m2b.m2b_kit import LONG, SHORT, Behavior, build_world, drive, settle_tasks

BASE = "/api/v1"


class Rig:
    def __init__(self, w, auth: AuthRig, app: FastAPI) -> None:
        self.w, self.auth, self.app = w, auth, app
        self.tokens: dict[str, str] = {}

    async def session(self, name="alice", *, seat=True, reauth=False) -> dict[str, str]:
        if name not in self.tokens:
            if self.auth.ledger.read_sync(repo_auth.get_operator_by_name, name) is None:
                await self.auth.operator(name)
            self.tokens[name] = (await self.auth.login(name, PASSWORD)).token
        principal = await self.auth.auth.authenticate(self.tokens[name])
        if seat:
            await self.auth.seat.acquire(principal)
        if reauth:
            await self.auth.auth.reauth(principal, PASSWORD)
        return {"Authorization": f"Bearer {self.tokens[name]}"}

    @property
    def cli(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.local_token}"}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://kernel.test")


async def make_rig(ledger, clock, bus) -> Rig:
    w = await build_world(ledger, clock, bus)
    auth = AuthRig.make(ledger, clock, bus)
    app = FastAPI()
    app.state.services = SimpleNamespace(auth=auth.auth, seat=auth.seat)
    app.state.bulk_ops = BulkOps(w.close_all, w.switch)

    async def api_error(request, exc: ApiError):
        return JSONResponse(exc.body(), status_code=exc.status)

    async def validation_error(request, exc: RequestValidationError):
        return JSONResponse(errors.invalid("请求参数无效").body(), status_code=422)

    app.add_exception_handler(ApiError, api_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.include_router(routes_bulk.router, prefix=BASE)
    return Rig(w, auth, app)


def body(**kw):
    return kw


# ====================================================================== 鉴权、席位、口令


@pytest.mark.parametrize("path, payload", [
    ("/ops/close_all/preview", {"scope": "model"}),
    ("/ops/close_all/confirm", {"op_id": "a", "model_version": 1, "scope": "model"}),
    ("/ops/abort", {"op_id": "a", "target_op_id": "b"}),
    ("/ops/switch/preview", {"op_id": "a", "target": "m2"}),
    ("/ops/switch/confirm", {"op_id": "a", "model_version": 1, "mode": "keep"}),
    ("/ops/switch/cancel", {"op_id": "a"}),
])
async def test_every_route_needs_a_token(ledger, clock, bus, path, payload):
    rig = await make_rig(ledger, clock, bus)
    async with rig.client() as client:
        response = await client.post(BASE + path, json=payload)
    assert response.status_code == 401 and response.json()["code"] == "unauthorized"


async def test_close_all_confirm_needs_the_seat(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    headers = await rig.session(seat=False, reauth=True)
    version = rig.w.modelstate.current().version
    async with rig.client() as client:
        response = await client.post(BASE + "/ops/close_all/confirm", headers=headers,
                                     json=body(op_id="ca-1", model_version=version, scope="model"))
    assert response.status_code == 409 and response.json()["code"] == "seat_required"
    assert await rig.w.operations.get("ca-1") is None


async def test_close_all_confirm_needs_a_recent_password_or_one_in_the_request(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    headers = await rig.session()
    version = rig.w.modelstate.current().version
    async with rig.client() as client:
        missing = await client.post(BASE + "/ops/close_all/confirm", headers=headers,
                                    json=body(op_id="ca-1", model_version=version, scope="model"))
        wrong = await client.post(BASE + "/ops/close_all/confirm", headers=headers,
                                  json=body(op_id="ca-1", model_version=version, scope="model", password="nope"))
        right = await client.post(BASE + "/ops/close_all/confirm", headers=headers,
                                  json=body(op_id="ca-1", model_version=version, scope="model", password=PASSWORD))
    assert missing.status_code == 403 and missing.json()["code"] == "reauth_required"
    assert wrong.status_code == 401 and wrong.json()["code"] == "bad_credentials"
    assert right.status_code == 200
    assert right.json() == {"op_id": "ca-1", "status": "running", "result": {"status": "running"}}


async def test_a_recent_reauth_makes_the_password_unnecessary(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    headers = await rig.session(reauth=True)
    version = rig.w.modelstate.current().version
    async with rig.client() as client:
        response = await client.post(BASE + "/ops/close_all/confirm", headers=headers,
                                     json=body(op_id="ca-1", model_version=version, scope="model"))
    assert response.status_code == 200


async def test_the_password_never_reaches_the_operation_record(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    headers = await rig.session()
    version = rig.w.modelstate.current().version
    async with rig.client() as client:
        await client.post(BASE + "/ops/close_all/confirm", headers=headers,
                          json=body(op_id="ca-1", model_version=version, scope="model", password=PASSWORD))
    await drive(clock, rig.w.close_all.wait("ca-1"))
    record = await rig.w.operations.get("ca-1")
    assert PASSWORD not in repr(record)


async def test_the_command_line_token_needs_no_seat_and_no_password_for_close_all(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    rig.w.add_position("BTCUSDT", LONG)
    version = rig.w.modelstate.current().version
    async with rig.client() as client:
        response = await client.post(BASE + "/ops/close_all/confirm", headers=rig.cli,
                                     json=body(op_id="ca-1", model_version=version, scope="all_managed"))
    assert response.status_code == 200
    await drive(clock, rig.w.close_all.wait("ca-1"))
    record = await rig.w.operations.get("ca-1")
    assert record.operator == "cli" and record.session_id is None
    assert record.status is OperationStatus.SUCCEEDED


# ====================================================================== 内核侧拒绝的错误码


async def test_a_stale_version_is_409_stale_version(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    headers = await rig.session(reauth=True)
    async with rig.client() as client:
        response = await client.post(BASE + "/ops/close_all/confirm", headers=headers,
                                     json=body(op_id="ca-1", model_version=0, scope="model"))
    assert response.status_code == 409 and response.json()["code"] == "stale_version"


async def test_a_state_that_does_not_allow_close_all_is_409_not_allowed_with_the_reason(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    headers = await rig.session(reauth=True)
    await rig.w.modelstate.acquire_op_lock(OpLock.SWITCHING, owner="x")
    version = rig.w.modelstate.current().version
    async with rig.client() as client:
        response = await client.post(BASE + "/ops/close_all/confirm", headers=headers,
                                     json=body(op_id="ca-1", model_version=version, scope="model"))
        preview = await client.post(BASE + "/ops/close_all/preview", headers=headers, json=body(scope="model"))
    assert response.status_code == 409 and response.json()["code"] == "not_allowed"
    assert "切换" in response.json()["message"]
    assert preview.status_code == 409


@pytest.mark.parametrize("payload", [
    {"model_version": 1, "scope": "model"},  # 缺 op_id
    {"op_id": "a b", "model_version": 1, "scope": "model"},  # 操作号不合法
    {"op_id": "a", "model_version": 1, "scope": "bogus"},
    {"op_id": "a", "scope": "model"},  # 缺版本
    {"op_id": "a", "model_version": "x", "scope": "model"},
])
async def test_bad_request_bodies_are_422_invalid_and_nothing_is_recorded(ledger, clock, bus, payload):
    rig = await make_rig(ledger, clock, bus)
    headers = await rig.session(reauth=True)
    async with rig.client() as client:
        response = await client.post(BASE + "/ops/close_all/confirm", headers=headers, json=payload)
    assert response.status_code == 422 and response.json()["code"] == "invalid"
    assert rig.w.modelstate.current().op_locks == frozenset()


# ====================================================================== 报文形状


async def test_close_all_preview_body_has_the_included_excluded_and_mixed_lists(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    rig.w.registry.add("BTCUSDT", LONG, ownership=Ownership.STRATEGY)
    rig.w.registry.add("ETHUSDT", LONG, ownership=Ownership.STRATEGY, is_mixed=True)
    rig.w.registry.add("SOLUSDT", SHORT, ownership=Ownership.TAKEOVER)
    rig.w.registry.add("DOGEUSDT", LONG, ownership=Ownership.NONE, status=PositionStatus.PENDING)
    headers = await rig.session(seat=False)  # 预览不改状态，不要席位
    async with rig.client() as client:
        response = await client.post(BASE + "/ops/close_all/preview", headers=headers, json=body(scope="model"))
    assert response.status_code == 200
    data = response.json()
    assert data["scope"] == "model" and data["model_version"] == rig.w.modelstate.current().version
    assert {p["symbol"] for p in data["included"]} == {"BTCUSDT", "ETHUSDT"}
    assert [p["symbol"] for p in data["mixed_included"]] == ["ETHUSDT"] and data["mixed_included"][0]["is_mixed"] is True
    excluded = {item["position"]["symbol"]: item["reason"] for item in data["excluded"]}
    assert set(excluded) == {"SOLUSDT", "DOGEUSDT"} and all(excluded.values())
    assert {"id", "side", "status", "ownership", "qty", "version"} <= set(data["included"][0])  # 持仓报文同只读接口


async def test_close_all_runs_and_the_abort_route_stops_it(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    rig.w.add_position("BTCUSDT", LONG)
    rig.w.coordinator.behave("BTCUSDT", LONG, "stuck")
    headers = await rig.session(reauth=True)
    version = rig.w.modelstate.current().version
    async with rig.client() as client:
        started = await client.post(BASE + "/ops/close_all/confirm", headers=headers,
                                    json=body(op_id="ca-1", model_version=version, scope="all_managed"))
        await settle_tasks()
        aborted = await client.post(BASE + "/ops/abort", headers=headers, json=body(op_id="ab-1", target_op_id="ca-1"))
        nothing = await client.post(BASE + "/ops/abort", headers=headers, json=body(op_id="ab-2", target_op_id="ca-1"))
    assert started.json()["status"] == "running"
    assert aborted.status_code == 200
    assert aborted.json() == {"op_id": "ab-1", "status": "succeeded", "result": {"aborted": True}}
    assert nothing.status_code == 409 and nothing.json()["code"] == "not_allowed"
    assert rig.w.modelstate.current().op_locks == frozenset()
    assert (await rig.w.operations.get("ca-1")).status is OperationStatus.ABORTED


async def test_a_repeated_close_all_confirm_returns_the_first_answer(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    rig.w.add_position("BTCUSDT", LONG)
    headers = await rig.session(reauth=True)
    version = rig.w.modelstate.current().version
    payload = body(op_id="ca-1", model_version=version, scope="all_managed")
    async with rig.client() as client:
        first = await client.post(BASE + "/ops/close_all/confirm", headers=headers, json=payload)
        await drive(clock, rig.w.close_all.wait("ca-1"))
        again = await client.post(BASE + "/ops/close_all/confirm", headers=headers, json=payload)
    assert first.json()["status"] == "running"
    assert again.status_code == 200 and again.json()["op_id"] == "ca-1"
    assert again.json()["status"] == "succeeded"
    assert again.json()["result"]["per_position"][0]["result"] == "closed"
    assert len(rig.w.coordinator.submitted) == 1


# ====================================================================== 切换


async def test_the_whole_switch_flow_over_http(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    headers = await rig.session()
    async with rig.client() as client:
        pv = await client.post(BASE + "/ops/switch/preview", headers=headers, json=body(op_id="pv-1", target="m3"))
        assert pv.status_code == 200
        data = pv.json()
        assert data["op_id"] == "pv-1" and data["status"] == "succeeded"
        result = data["result"]
        assert {"model_version", "current", "counts_by_ownership", "open_orders", "expires_ms", "notes"} <= set(result)
        no_password = await client.post(BASE + "/ops/switch/confirm", headers=headers,
                                        json=body(op_id="cf-1", model_version=result["model_version"], mode="keep"))
        assert no_password.status_code == 403 and no_password.json()["code"] == "reauth_required"
        task = asyncio_task(client.post(BASE + "/ops/switch/confirm", headers=headers, json=body(
            op_id="cf-1", model_version=result["model_version"], mode="keep", password=PASSWORD)))
        done = await drive(clock, task)
    assert done.status_code == 200
    out = done.json()
    assert out["op_id"] == "cf-1" and out["status"] == "succeeded"
    assert out["result"]["status"] == "succeeded" and out["result"]["active_model"] == "m3"
    assert out["result"]["gate"] == "stopped"
    assert rig.w.modelstate.current().active_model is Model.M3


def asyncio_task(coro):
    import asyncio

    return asyncio.ensure_future(coro)


async def test_a_switch_that_cannot_settle_answers_200_with_status_failed_and_the_old_model(ledger, clock, bus):
    from dmr_kernel.core.types import IntentAction, IntentState
    from tests.unit.ops_m2b.m2b_kit import FakeIntent

    rig = await make_rig(ledger, clock, bus)
    rig.w.intents.opens.append(FakeIntent(5, IntentState.ACTIVE, symbol="BTCUSDT", side=LONG, action=IntentAction.OPEN))
    headers = await rig.session(reauth=True)
    async with rig.client() as client:
        pv = (await client.post(BASE + "/ops/switch/preview", headers=headers, json=body(op_id="pv-1", target="m2"))).json()
        done = await drive(clock, asyncio_task(client.post(
            BASE + "/ops/switch/confirm", headers=headers,
            json=body(op_id="cf-1", model_version=pv["result"]["model_version"], mode="keep"))))
    assert done.status_code == 200
    assert done.json()["status"] == "failed"
    assert done.json()["result"]["status"] == "failed" and done.json()["result"]["active_model"] == "m1"
    assert done.json()["result"]["reason"]


async def test_switch_close_all_first_answers_running_and_finishes_in_the_background(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    rig.w.add_position("BTCUSDT", LONG)
    headers = await rig.session(reauth=True)
    async with rig.client() as client:
        pv = (await client.post(BASE + "/ops/switch/preview", headers=headers, json=body(op_id="pv-1", target="m2"))).json()
        response = await client.post(BASE + "/ops/switch/confirm", headers=headers, json=body(
            op_id="cf-1", model_version=pv["result"]["model_version"], mode="close_all_first"))
    assert response.status_code == 200
    assert response.json()["status"] == "running" and response.json()["result"]["status"] == "running"
    assert response.json()["result"]["active_model"] == "m1"
    await drive(clock, rig.w.switch.wait("cf-1"))
    assert rig.w.modelstate.current().active_model is Model.M2


async def test_switch_routes_need_the_seat_and_a_session(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    no_seat = await rig.session(seat=False)
    async with rig.client() as client:
        a = await client.post(BASE + "/ops/switch/preview", headers=no_seat, json=body(op_id="pv-1", target="m2"))
        b = await client.post(BASE + "/ops/switch/preview", headers=rig.cli, json=body(op_id="pv-2", target="m2"))
        c = await client.post(BASE + "/ops/switch/cancel", headers=no_seat, json=body(op_id="cx-1"))
    assert a.status_code == 409 and a.json()["code"] == "seat_required"
    assert b.status_code == 409 and b.json()["code"] == "seat_required"  # 本机令牌不能切换
    assert c.status_code == 409 and c.json()["code"] == "seat_required"
    assert rig.w.modelstate.current().op_locks == frozenset()


async def test_switch_cancel_over_http(ledger, clock, bus):
    rig = await make_rig(ledger, clock, bus)
    headers = await rig.session()
    async with rig.client() as client:
        await client.post(BASE + "/ops/switch/preview", headers=headers, json=body(op_id="pv-1", target="m2"))
        cancel = await client.post(BASE + "/ops/switch/cancel", headers=headers, json=body(op_id="cx-1"))
        again = await client.post(BASE + "/ops/switch/cancel", headers=headers, json=body(op_id="cx-2"))
    assert cancel.status_code == 200
    assert cancel.json() == {"op_id": "cx-1", "status": "succeeded", "result": {"cancelled": True}}
    assert again.status_code == 409 and again.json()["code"] == "not_allowed"


async def test_losing_the_seat_through_the_service_cancels_the_preview_via_the_bus(ledger, clock, bus):
    """真实的席位服务发布 SeatChanged，切换服务据此取消预览（端到端地验证事件名与字段对得上）。"""
    rig = await make_rig(ledger, clock, bus)
    await bus.start()
    await rig.w.switch.start()
    headers = await rig.session()
    async with rig.client() as client:
        await client.post(BASE + "/ops/switch/preview", headers=headers, json=body(op_id="pv-1", target="m2"))
        assert OpLock.SWITCH_PREVIEW in rig.w.modelstate.current().op_locks
        principal = await rig.auth.auth.authenticate(rig.tokens["alice"])
        await rig.auth.seat.release(principal)
        await bus.drain()
    assert rig.w.modelstate.current().op_locks == frozenset()
    await rig.w.switch.stop()
    await bus.stop()
