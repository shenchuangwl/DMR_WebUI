"""M2 全流程（M2I）：整套装配的内核（``tests/support/kernel.py``）+ 进程内替身，全部经控制接口（HTTP，ASGI 传输）走一遍。

登录 → 申请席位 → ``/dev/test_open`` 造两个策略仓 → 部分平仓预览与确认 50% → 两个会话（以及同一会话的两个标签页）并发部分平仓
只有一个生效 → 平掉此仓 → 再造仓 → 一键全平两档范围（预览、确认、逐仓结果）→ 模型切换预览与确认（keep 与 close_all_first）
→ 启动（自检）与停止 → ``/ops/recover`` 在非恢复状态被拒。另：4.6 表随机抽 6 格做 HTTP 级断言（单元层已逐格），
``/ops/recover`` 的操作记录类型是 ``recover``，``/ops/stop`` 带 ``close_all`` 走装配好的一键全平。

报文约定见 docs/plans/M2-统一操作-测试先行计划.md 第 2 节。这里不手工接任何操作服务：用的都是装配层（``core.scheduler``）接好的。
"""
from __future__ import annotations

import asyncio
import random
from decimal import Decimal
from typing import Any

from dmr_kernel.core.modelstate import allowed_operations
from dmr_kernel.core.types import Gate, Model, OpLock, OperationType, PositionSide, Suppression
from dmr_kernel.ops import startstop
from tests.support.kernel import RecoveryStub, api_client, make_standin_world, settle, started_kernel

D = Decimal
LONG = PositionSide.LONG
BASE = "/api/v1"
PASSWORD = "correct horse battery"


class Api:
    """经控制接口说话：``token=None`` 用本机令牌（cli，不占席位、免口令）。"""

    def __init__(self, kernel, client) -> None:
        self.kernel, self.client = kernel, client
        self.cli = kernel.app.state.ctx.local_token
        self._seq = 0

    def op(self, prefix: str = "op") -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    async def login(self, name: str) -> str:
        await self.kernel.app.state.services.auth.create_operator(name, PASSWORD)
        response = await self.client.post(BASE + "/auth/login", json={"username": name, "password": PASSWORD})
        assert response.status_code == 200, response.text
        return response.json()["token"]

    async def post(self, path: str, body: Any, token: str | None = None):
        return await self.client.post(BASE + path, json=body,
                                      headers={"Authorization": f"Bearer {token or self.cli}"})

    async def get(self, path: str, token: str | None = None):
        return await self.client.get(BASE + path, headers={"Authorization": f"Bearer {token or self.cli}"})

    async def ok(self, path: str, body: Any, token: str | None = None) -> dict:
        response = await self.post(path, body, token)
        assert response.status_code == 200, (path, response.status_code, response.text)
        return response.json()

    async def version(self) -> int:
        return (await self.state())["model"]["version"]

    async def state(self) -> dict:
        response = await self.get("/state")
        assert response.status_code == 200, response.text
        return response.json()

    async def positions(self) -> dict[str, dict]:
        response = await self.get("/positions")
        assert response.status_code == 200, response.text
        body = response.json()
        items = body["items"] if isinstance(body, dict) else body
        return {item["symbol"]: item for item in items}

    async def operation(self, op_id: str) -> dict:
        response = await self.get(f"/operations/{op_id}")
        assert response.status_code == 200, response.text
        return response.json()

    async def test_open(self, token: str, symbol: str, notional: str) -> dict:
        body = await self.ok("/dev/test_open", {"op_id": self.op("open"), "symbol": symbol, "side": "LONG",
                                                "notional": notional}, token)
        assert body["result"]["state"] == "COMPLETED", body
        await self.kernel.jobs["backfill_trades"]()  # 没有数据流时，成交由补拉带进来
        await settle(self.kernel)
        return (await self.positions())[symbol]

    async def reconcile(self) -> None:
        await self.kernel.jobs["reconcile_positions"]()
        await self.kernel.jobs["reconcile_orders"]()
        await settle(self.kernel)

    async def exchange_qty(self, symbol: str) -> Decimal:
        return (await self.kernel.coordinator.live_position(symbol, LONG)).value


def _opened(tmp_path, **kwargs):
    return started_kernel(tmp_path, standin=make_standin_world(equity="100000"), **kwargs)


def _model_scope(body: dict) -> set[str]:
    return {item["symbol"] for item in body["included"]}


async def _finish_close_all(api: Api, op_id: str) -> dict:
    await asyncio.wait_for(api.kernel.close_all.wait(op_id), 60)
    await settle(api.kernel)
    record = await api.operation(op_id)
    assert record["op_type"] == "close_all" and record["status"] == "succeeded", record
    return record


# ====================================================================== 全流程


async def test_the_whole_m2_flow_over_http(tmp_path):
    async with _opened(tmp_path) as (kernel, _):
        async with api_client(kernel) as client:
            api = Api(kernel, client)
            await api.reconcile()  # 合约数上限的快照、对账状态（自检要「对账无差异」）

            # ---- 登录 → 申请席位
            alice = await api.login("alice")
            bob = await api.login("bob")
            seat = await api.ok("/seat/acquire", {}, alice)
            assert seat is not None
            assert (await api.post("/seat/acquire", {}, bob)).status_code == 409  # 席位只有一个

            # ---- 造两个策略仓
            btc = await api.test_open(alice, "BTCUSDT", "800")
            eth = await api.test_open(alice, "ETHUSDT", "300")
            assert await api.exchange_qty("BTCUSDT") == D("0.016")
            assert btc["ownership"] == "strategy" and eth["ownership"] == "strategy"

            # ---- 部分平仓 50%：预览 → 确认
            preview = await api.ok("/ops/partial_close/preview", {"position_id": btc["id"], "ratio": 0.5}, alice)
            assert preview["position_id"] == btc["id"]
            assert preview["close_qty"] == "0.008" and preview["remaining_qty"] == "0.008"
            confirm = {"op_id": api.op("pc"), "position_id": btc["id"], "position_version": preview["position_version"],
                       "preview_qty": preview["live_qty"], "close_qty": preview["close_qty"]}
            done = await api.ok("/ops/partial_close/confirm", confirm, alice)
            assert done["op_id"] == confirm["op_id"] and done["status"] == "succeeded"
            assert done["result"]["outcome"] == "closed" and done["result"]["filled_qty"] == "0.008"
            assert await api.ok("/ops/partial_close/confirm", confirm, alice) == done  # 同一操作号只执行一次
            assert await api.exchange_qty("BTCUSDT") == D("0.008")
            await kernel.jobs["backfill_trades"]()
            await settle(kernel)

            # ---- 两个会话（及同一会话的两个标签页）并发部分平仓：只有一个生效
            btc = (await api.positions())["BTCUSDT"]
            preview = await api.ok("/ops/partial_close/preview", {"position_id": btc["id"], "ratio": 0.5}, alice)
            body = {"position_id": btc["id"], "position_version": preview["position_version"],
                    "preview_qty": preview["live_qty"], "close_qty": preview["close_qty"]}
            tab1, tab2, other = await asyncio.gather(
                api.post("/ops/partial_close/confirm", {**body, "op_id": "tab-1"}, alice),
                api.post("/ops/partial_close/confirm", {**body, "op_id": "tab-2"}, alice),
                api.post("/ops/partial_close/confirm", {**body, "op_id": "bob-1"}, bob))
            assert other.status_code == 409 and other.json()["code"] == "seat_required"
            assert sorted([tab1.status_code, tab2.status_code]) == [200, 409]
            loser = tab1 if tab1.status_code == 409 else tab2
            assert loser.json()["code"] in ("stale_version", "not_allowed")
            assert await api.exchange_qty("BTCUSDT") == D("0.004")
            await kernel.jobs["backfill_trades"]()
            await settle(kernel)

            # ---- 平掉此仓
            btc = (await api.positions())["BTCUSDT"]
            closed = await api.ok("/ops/close_one", {"op_id": api.op("c1"), "position_id": btc["id"],
                                                     "position_version": btc["version"]}, alice)
            assert closed["result"]["outcome"] == "closed"
            assert await api.exchange_qty("BTCUSDT") == D("0")
            await kernel.jobs["backfill_trades"]()
            await settle(kernel)
            assert "BTCUSDT" not in await api.positions()

            # ---- 再造仓；一键全平「当前模型归属」：预览 → 确认（口令）→ 逐仓结果
            await api.test_open(alice, "DOGEUSDT", "200")
            shown = await api.ok("/ops/close_all/preview", {"scope": "model"}, alice)
            assert shown["scope"] == "model" and _model_scope(shown) == {"ETHUSDT", "DOGEUSDT"}
            assert shown["excluded"] == [] and shown["mixed_included"] == []
            wide = await api.ok("/ops/close_all/preview", {"scope": "all_managed"}, alice)
            assert _model_scope(wide) == {"ETHUSDT", "DOGEUSDT"}
            no_password = await api.post("/ops/close_all/confirm", {"op_id": api.op("ca"), "scope": "model",
                                                                     "model_version": shown["model_version"]}, alice)
            assert no_password.status_code == 403 and no_password.json()["code"] == "reauth_required"
            wrong = await api.post("/ops/close_all/confirm", {"op_id": api.op("ca"), "scope": "model",
                                                               "password": "wrong", "model_version": shown["model_version"]},
                                   alice)
            assert wrong.status_code == 401 and wrong.json()["code"] == "bad_credentials"
            ca_id = api.op("ca")
            started = await api.ok("/ops/close_all/confirm", {"op_id": ca_id, "scope": "model", "password": PASSWORD,
                                                              "model_version": shown["model_version"]}, alice)
            assert started == {"op_id": ca_id, "status": "running", "result": {"status": "running"}}
            record = await _finish_close_all(api, ca_id)
            rows = {row["symbol"]: row for row in record["per_position"]}
            assert set(rows) == {"ETHUSDT", "DOGEUSDT"}
            assert {row["result"] for row in rows.values()} == {"closed"}
            assert all({"position_id", "symbol", "side", "result", "detail"} <= set(r) for r in rows.values())
            assert "duration_ms" in record["result"] and "unknown_open_orders" in record["result"]
            assert await api.exchange_qty("ETHUSDT") == D("0") and await api.exchange_qty("DOGEUSDT") == D("0")
            state = await api.state()
            assert state["model"]["gate"] == "stopped" and state["model"]["op_locks"] == []

            # ---- 一键全平「全部托管持仓」
            await api.test_open(alice, "BTCUSDT", "200")
            wide = await api.ok("/ops/close_all/preview", {"scope": "all_managed"}, alice)
            assert _model_scope(wide) == {"BTCUSDT"}
            ca2 = api.op("ca")
            started = await api.ok("/ops/close_all/confirm", {"op_id": ca2, "scope": "all_managed",
                                                              "password": PASSWORD,
                                                              "model_version": wide["model_version"]}, alice)
            assert started["status"] == "running"
            record = await _finish_close_all(api, ca2)
            assert [(row["symbol"], row["result"]) for row in record["per_position"]] == [("BTCUSDT", "closed")]
            assert await api.exchange_qty("BTCUSDT") == D("0")

            # ---- 模型切换：保留原规则（keep）
            await api.test_open(alice, "ETHUSDT", "300")
            pv = await api.ok("/ops/switch/preview", {"op_id": api.op("sp"), "target": "m2"}, alice)
            assert pv["status"] == "succeeded" and pv["result"]["counts_by_ownership"]["strategy"] == 1
            assert [lock["code"] for lock in (await api.state())["model"]["op_locks"]] == ["switch_preview"]
            second_preview = await api.post("/ops/switch/preview", {"op_id": api.op("sp"), "target": "m3"}, alice)
            assert second_preview.status_code == 409 and second_preview.json()["code"] == "not_allowed"
            switched = await api.ok("/ops/switch/confirm", {"op_id": api.op("sc"), "mode": "keep",
                                                            "password": PASSWORD,
                                                            "model_version": pv["result"]["model_version"]}, alice)
            assert switched["status"] == "succeeded"
            assert switched["result"]["active_model"] == "m2" and switched["result"]["gate"] == "stopped"
            assert kernel.modelstate.current().active_model is Model.M2
            assert await api.exchange_qty("ETHUSDT") > 0  # 持仓没动

            # ---- 模型切换：先全部平仓再切换（close_all_first）
            pv = await api.ok("/ops/switch/preview", {"op_id": api.op("sp"), "target": "m1"}, alice)
            sc = api.op("sc")
            switching = await api.ok("/ops/switch/confirm", {"op_id": sc, "mode": "close_all_first",
                                                             "password": PASSWORD,
                                                             "model_version": pv["result"]["model_version"]}, alice)
            assert switching["status"] == "running"
            await asyncio.wait_for(kernel.switch.wait(sc), 60)
            await settle(kernel)
            record = await api.operation(sc)
            assert record["op_type"] == "switch" and record["status"] == "succeeded", record
            assert kernel.modelstate.current().active_model is Model.M1
            assert kernel.modelstate.current().gate is Gate.STOPPED
            assert await api.exchange_qty("ETHUSDT") == D("0")
            await kernel.jobs["backfill_trades"]()
            await settle(kernel)

            # ---- 启动（自检）与停止
            kernel.app.state.ops_services.deps.list_valid = lambda: True  # 名单在 M4 接入；这里当作有效
            await api.reconcile()
            v = await api.version()
            refused = await api.post("/ops/start", {"op_id": api.op("st"), "model_version": v}, bob)
            assert refused.status_code == 409 and refused.json()["code"] == "seat_required"
            started = await api.ok("/ops/start", {"op_id": api.op("st"), "model_version": v}, alice)
            assert started["result"]["gate"] == "running" and started["result"]["model"] == "m1"
            assert started["result"]["checks"] and all(c["ok"] for c in started["result"]["checks"])
            stopped = await api.ok("/ops/stop", {"op_id": api.op("sp"), "model_version": await api.version()}, alice)
            assert stopped["result"]["gate"] == "stopped"
            assert kernel.modelstate.current().gate is Gate.STOPPED

            # ---- 不在恢复中：/ops/recover 被拒
            denied = await api.post("/ops/recover", {"op_id": api.op("rc")}, alice)
            assert denied.status_code == 409 and denied.json()["code"] == "not_allowed"


# ====================================================================== 4.6 表：HTTP 级抽查


STATES = {
    "running": lambda ms: ms.set_gate(Gate.RUNNING, reason="t"),
    "stopped": None,
    "recovering": lambda ms: ms.add_suppression(Suppression.RECOVERING, reason="t"),
    "mismatch": lambda ms: ms.add_suppression(Suppression.RECONCILE_MISMATCH, reason="t"),
    "bulk_closing": lambda ms: ms.acquire_op_lock(OpLock.BULK_CLOSING, owner="t"),
    "switching": lambda ms: ms.acquire_op_lock(OpLock.SWITCHING, owner="t"),
}
UNDO = {
    "running": lambda ms: ms.set_gate(Gate.STOPPED, reason="t"),
    "recovering": lambda ms: ms.remove_suppression(Suppression.RECOVERING, reason="t"),
    "mismatch": lambda ms: ms.remove_suppression(Suppression.RECONCILE_MISMATCH, reason="t"),
    "bulk_closing": lambda ms: ms.release_op_lock(OpLock.BULK_CLOSING),
    "switching": lambda ms: ms.release_op_lock(OpLock.SWITCHING),
}


def _request_for(key: str, op_id: str, version: int, position: dict) -> tuple[str, dict]:
    return {
        "switch": ("/ops/switch/preview", {"op_id": op_id, "target": "m2"}),
        "switch_confirm": ("/ops/switch/confirm", {"op_id": op_id, "model_version": version, "mode": "keep",
                                                   "password": PASSWORD}),
        "switch_cancel": ("/ops/switch/cancel", {"op_id": op_id}),
        "start": ("/ops/start", {"op_id": op_id, "model_version": version}),
        "stop": ("/ops/stop", {"op_id": op_id, "model_version": version}),
        "partial_close": ("/ops/partial_close/preview", {"position_id": position["id"], "ratio": 0.5}),
        "close_one": ("/ops/close_one", {"op_id": op_id, "position_id": position["id"],
                                         "position_version": position["version"]}),
        "close_all": ("/ops/close_all/preview", {"scope": "model"}),
        "abort": ("/ops/abort", {"op_id": op_id, "target_op_id": "nothing-running"}),
        # 接管类（M3 计划第 3 节）：{op_id, position_id, position_version}，取消托管另带 mode
        **{name: (f"/ops/{name}", {"op_id": op_id, "position_id": position["id"],
                                   "position_version": position["version"]})
           for name in ("takeover", "ignore", "restore", "hand_stop_to_system")},
        "cancel_takeover": ("/ops/cancel_takeover", {"op_id": op_id, "position_id": position["id"],
                                                     "position_version": position["version"], "mode": "handover"}),
    }[key]


async def test_six_random_cells_of_the_4_6_table_are_enforced_over_http(tmp_path):
    async with _opened(tmp_path) as (kernel, _):
        async with api_client(kernel) as client:
            api = Api(kernel, client)
            await api.reconcile()
            alice = await api.login("alice")
            await api.ok("/seat/acquire", {}, alice)
            position = await api.test_open(alice, "BTCUSDT", "400")
            ms = kernel.modelstate

            # 每种状态下表里不允许的格子（原因原文），固定种子随机抽 6 格
            pool = []
            for name, setup in STATES.items():
                if setup is not None:
                    await setup(ms)
                for key, permission in allowed_operations(ms.current()).items():
                    if not permission.allowed:
                        pool.append((name, key, permission.reason))
                if name in UNDO:
                    await UNDO[name](ms)
            cells = random.Random(46).sample(sorted(pool), 6)
            assert len({key for _, key, _ in cells}) >= 3, cells  # 抽到的格子覆盖几种不同的操作

            before = await api.state()
            for name, key, reason in cells:
                if STATES[name] is not None:
                    await STATES[name](ms)
                state = await api.state()
                assert state["allowed"][key]["allowed"] is False and state["allowed"][key]["reason"] == reason
                path, body = _request_for(key, api.op(f"cell-{key}"), state["model"]["version"], position)
                response = await api.post(path, body, alice)
                assert response.status_code == 409, (name, key, response.text)
                assert response.json()["code"] == "not_allowed" and response.json()["message"] == reason, (name, key)
                if name in UNDO:
                    await UNDO[name](ms)
            after = await api.state()
            assert after["model"]["gate"] == before["model"]["gate"] and after["model"]["op_locks"] == []
            assert await api.exchange_qty("BTCUSDT") == D(position["qty"])  # 被拒的请求什么也没做


# ====================================================================== 重新恢复与停止带一键全平


async def test_recover_is_recorded_with_its_own_operation_type(tmp_path):
    assert OperationType.RECOVER.value == "recover"
    assert startstop.RECOVER_OP_TYPE is OperationType.RECOVER
    stuck = RecoveryStub(tradable=False, problems=("快照读不到",))
    async with _opened(tmp_path) as (kernel, _):
        async with api_client(kernel) as client:
            api = Api(kernel, client)
            deps = kernel.app.state.ops_services.deps
            calls = []

            async def recover():
                calls.append(1)
                kernel.recovery_result = RecoveryStub()
                return kernel.recovery_result

            kernel.recovery_result = stuck
            deps.recover = recover
            await kernel.modelstate.add_suppression(Suppression.RECOVERING, reason="t")
            done = await api.ok("/ops/recover", {"op_id": "rc-1"})
            assert done["result"]["tradable"] is True and calls == [1]
            assert (await api.operation("rc-1"))["op_type"] == "recover"


async def test_the_real_rerun_recovery_is_what_the_recover_route_calls(tmp_path):
    async with _opened(tmp_path) as (kernel, _):
        deps = kernel.app.state.ops_services.deps
        assert deps.recover == kernel.rerun_recovery
        kernel.recovery_result = "sentinel"
        assert deps.last_recovery() == "sentinel"


async def test_stop_with_close_all_runs_the_assembled_close_all(tmp_path):
    async with _opened(tmp_path) as (kernel, _):
        async with api_client(kernel) as client:
            api = Api(kernel, client)
            await api.reconcile()
            alice = await api.login("alice")
            await api.ok("/seat/acquire", {}, alice)
            await api.test_open(alice, "BTCUSDT", "200")
            await kernel.modelstate.set_gate(Gate.RUNNING, reason="t")
            body = await api.ok("/ops/stop", {"op_id": "stop-1", "model_version": await api.version(),
                                              "close_all": True, "password": PASSWORD}, alice)
            assert body["result"]["gate"] == "stopped"
            child = body["result"]["close_all_op_id"]
            record = await _finish_close_all(api, child)
            assert [(row["symbol"], row["result"]) for row in record["per_position"]] == [("BTCUSDT", "closed")]
            assert await api.exchange_qty("BTCUSDT") == D("0")


async def test_bulk_routes_answer_501_when_the_bulk_services_are_not_wired(tmp_path):
    async with _opened(tmp_path) as (kernel, _):
        async with api_client(kernel) as client:
            api = Api(kernel, client)
            del kernel.app.state.bulk_ops
            response = await api.post("/ops/close_all/preview", {"scope": "model"})
            assert response.status_code == 501 and response.json()["code"] == "not_implemented"
