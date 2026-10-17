"""M2A 操作经控制接口走通（整套内核 + 替身交易所）：启动、停止、测试造仓、部分平仓、平掉此仓、重新恢复。

覆盖报文约定（M2 计划第 2 节）、错误形状（409 not_allowed / seat_required / stale_version，422 invalid，401，501 未装配）、
两个会话同时部分平仓只有一个生效、同一操作号重复提交只执行一次、停止后不再开仓而已有仓照常。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from dmr_kernel.core.types import Gate, PositionSide, Suppression
from tests.support.kernel import RecoveryStub, settle
from tests.unit.ops_m2a.m2a_kit import rig

D = Decimal
LONG = PositionSide.LONG


async def wired(r):
    # 装配层在构造时就建好了操作服务（app.state.ops_services），之后改 ctx.ops_wiring 不再生效：直接改它的 deps
    r.kernel.app.state.ops_services.deps.list_valid = lambda: True
    return r


async def test_unwired_and_unauthenticated_requests(tmp_path):
    async with rig(tmp_path, wire=False) as r:
        ctx = r.kernel.app.state.ctx
        ctx.ops_wiring = None
        response = await r.post("/ops/start", {"op_id": "a", "model_version": 1})
        assert response.status_code == 501 and response.json()["code"] == "not_implemented"
        response = await r.client.post("/api/v1/ops/start", json={}, headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401


async def test_start_and_stop_over_http(tmp_path):
    async with rig(tmp_path) as r:
        await wired(r)
        who = await r.login()
        v = r.kernel.modelstate.current().version
        denied = await r.post("/ops/start", {"op_id": "s-1", "model_version": v}, who)
        assert denied.status_code == 409 and denied.json()["code"] == "seat_required"
        await r.seat(who)
        stale = await r.post("/ops/start", {"op_id": "s-1", "model_version": v + 5}, who)
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        ok = await r.post("/ops/start", {"op_id": "s-1", "model_version": v}, who)
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["op_id"] == "s-1" and body["status"] == "succeeded"
        assert body["result"]["gate"] == "running" and body["result"]["model"] == "m1"
        assert all({"name", "ok", "detail"} <= set(c) for c in body["result"]["checks"])
        again = await r.post("/ops/start", {"op_id": "s-1", "model_version": v}, who)
        assert again.status_code == 200 and again.json() == body
        v2 = r.kernel.modelstate.current().version
        bad = await r.post("/ops/stop", {"op_id": "p-1"}, who)
        assert bad.status_code == 422 and bad.json()["code"] == "invalid"
        stopped = await r.post("/ops/stop", {"op_id": "p-1", "model_version": v2}, who)
        assert stopped.status_code == 200
        assert stopped.json()["result"] == {"gate": "stopped", "cancelled_open_orders": 0, "managed_remaining": 0}
        assert r.kernel.modelstate.current().gate is Gate.STOPPED


async def test_a_failing_self_check_returns_409_with_the_checks(tmp_path):
    async with rig(tmp_path) as r:  # 没有名单来源：模型 1 的名单项不过
        v = r.kernel.modelstate.current().version
        response = await r.post("/ops/start", {"op_id": "s-1", "model_version": v})
        body = response.json()
        assert response.status_code == 409 and body["code"] == "not_allowed"
        assert any(not c["ok"] for c in body["checks"])
        assert r.kernel.modelstate.current().gate is Gate.STOPPED


async def test_test_open_partial_close_and_close_one_over_http(tmp_path):
    async with rig(tmp_path) as r:
        who = await r.login()
        await r.seat(who)
        opened = await r.post("/dev/test_open", {"op_id": "t-1", "symbol": "BTCUSDT", "side": "LONG",
                                                 "notional": "400"}, who)
        assert opened.status_code == 200 and opened.json()["result"]["state"] == "COMPLETED"
        await r.kernel.jobs["backfill_trades"]()
        await settle(r.kernel)
        position = r.kernel.registry.get_active("BTCUSDT", LONG)
        assert position is not None
        pv = await r.post("/ops/partial_close/preview", {"position_id": position.id, "ratio": 0.5}, who)
        assert pv.status_code == 200, pv.text
        preview = pv.json()
        assert preview["close_qty"] == "0.004" and preview["remaining_qty"] == "0.004"
        bad = await r.post("/ops/partial_close/preview", {"position_id": position.id}, who)
        assert bad.status_code == 422
        confirm = {"op_id": "pc-1", "position_id": position.id, "position_version": preview["position_version"],
                   "preview_qty": preview["live_qty"], "close_qty": preview["close_qty"]}
        done = await r.post("/ops/partial_close/confirm", confirm, who)
        assert done.status_code == 200, done.text
        result = done.json()["result"]
        assert result["outcome"] == "closed" and result["filled_qty"] == "0.004" and result["state"] == "COMPLETED"
        assert await r.exchange_qty() == D("0.004")
        repeat = await r.post("/ops/partial_close/confirm", confirm, who)
        assert repeat.status_code == 200 and repeat.json() == done.json() and await r.exchange_qty() == D("0.004")
        await r.kernel.jobs["backfill_trades"]()
        await settle(r.kernel)
        position = r.kernel.registry.get_active("BTCUSDT", LONG)
        stale = await r.post("/ops/close_one", {"op_id": "c-0", "position_id": position.id,
                                                "position_version": position.version + 9}, who)
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        closed = await r.post("/ops/close_one", {"op_id": "c-1", "position_id": position.id,
                                                 "position_version": position.version}, who)
        assert closed.status_code == 200 and closed.json()["result"]["outcome"] == "closed"
        assert await r.exchange_qty() == D("0")


async def test_two_sessions_confirming_the_same_partial_close_let_only_one_through(tmp_path):
    async with rig(tmp_path) as r:
        a = await r.login("alice")
        await r.seat(a)
        position = await r.open_position(notional="400")
        preview = (await r.post("/ops/partial_close/preview", {"position_id": position.id, "ratio": 0.5}, a)).json()
        body = {"position_id": position.id, "position_version": preview["position_version"],
                "preview_qty": preview["live_qty"], "close_qty": preview["close_qty"]}
        # 第二个标签页用同一个会话（同一个席位），各自的操作号
        first, second = await asyncio.gather(
            r.post("/ops/partial_close/confirm", {**body, "op_id": "tab-1"}, a),
            r.post("/ops/partial_close/confirm", {**body, "op_id": "tab-2"}, a))
        codes = sorted([first.status_code, second.status_code])
        assert codes == [200, 409]
        loser = first if first.status_code == 409 else second
        assert loser.json()["code"] in ("stale_version", "not_allowed")
        assert await r.exchange_qty() == D("0.004")


async def test_a_second_stop_is_refused_by_the_table(tmp_path):
    async with rig(tmp_path) as r:
        await wired(r)
        v = r.kernel.modelstate.current().version
        assert (await r.post("/ops/start", {"op_id": "s-1", "model_version": v})).status_code == 200
        v = r.kernel.modelstate.current().version
        assert (await r.post("/ops/stop", {"op_id": "p-1", "model_version": v})).status_code == 200
        v = r.kernel.modelstate.current().version
        again = await r.post("/ops/stop", {"op_id": "p-2", "model_version": v})
        assert again.status_code == 409 and again.json()["code"] == "not_allowed"


async def test_recover_over_http(tmp_path):
    async with rig(tmp_path) as r:
        deps = r.kernel.app.state.ops_services.deps
        bad = RecoveryStub(tradable=False, problems=("快照读不到",))
        last = [bad]

        async def recover():
            last[0] = RecoveryStub()
            return last[0]

        deps.recover = recover
        deps.last_recovery = lambda: last[0]
        refused = await r.post("/ops/recover", {"op_id": "r-0"})
        assert refused.status_code == 409 and refused.json()["code"] == "not_allowed"
        await r.kernel.modelstate.add_suppression(Suppression.RECOVERING, reason="t")
        who = await r.login()
        assert (await r.post("/ops/recover", {"op_id": "r-1"}, who)).json()["code"] == "seat_required"
        done = await r.post("/ops/recover", {"op_id": "r-1"})
        assert done.status_code == 200 and done.json()["result"]["tradable"] is True
