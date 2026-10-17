"""M5 内核全流程（M5K）：整套装配的内核 + 进程内替身，经控制接口（HTTP，ASGI 传输）。

造仓 → 平仓 → 逐笔交易 /trades（字段、duration_ms、return_pct、筛选、排序、分页）→ /stats（三项在前、分组、混合仓单列）
→ /candles（K 线、DMR、进出场标记、止损线）→ /state 的模型 3 → /dev/shadow_intent 留痕且替身上没有任何订单。
报文约定见 docs/plans/M5-完善与模型3-测试先行计划.md 第 2 节。
"""
from __future__ import annotations

from decimal import Decimal

from dmr_kernel.core.types import Candle
from dmr_kernel.ledger import repo_audit, repo_candles
from tests.support.kernel import api_client, make_standin_world, settle, started_kernel

D = Decimal
BASE = "/api/v1"
WRITE_METHODS = ("POST", "PUT", "DELETE", "PATCH")


class Api:
    def __init__(self, kernel, client) -> None:
        self.kernel, self.client = kernel, client
        self.token = kernel.app.state.ctx.local_token
        self._seq = 0

    def op(self, prefix="op") -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def get(self, path: str, **params):
        return await self.client.get(BASE + path, params=params, headers=self.headers())

    async def ok_get(self, path: str, **params) -> dict:
        response = await self.get(path, **params)
        assert response.status_code == 200, (path, response.status_code, response.text)
        return response.json()

    async def post(self, path: str, body: dict):
        return await self.client.post(BASE + path, json=body, headers=self.headers())

    async def ok_post(self, path: str, body: dict) -> dict:
        response = await self.post(path, body)
        assert response.status_code == 200, (path, response.status_code, response.text)
        return response.json()

    async def round_trip(self, symbol: str, notional: str) -> dict:
        opened = await self.ok_post("/dev/test_open", {"op_id": self.op("open"), "symbol": symbol, "side": "LONG",
                                                       "notional": notional})
        assert opened["result"]["state"] == "COMPLETED", opened
        await self.kernel.jobs["backfill_trades"]()
        await settle(self.kernel)
        position = next(p for p in (await self.ok_get("/positions"))["items"] if p["symbol"] == symbol)
        closed = await self.ok_post("/ops/close_one", {"op_id": self.op("close"), "position_id": position["id"],
                                                       "position_version": position["version"]})
        assert closed["result"]["outcome"] == "closed", closed
        await self.kernel.jobs["backfill_trades"]()
        await settle(self.kernel)
        return position


async def test_trades_stats_candles_state_and_shadow(tmp_path):
    async with started_kernel(tmp_path, standin=make_standin_world(equity="100000")) as (kernel, standin):
        async with api_client(kernel) as client:
            api = Api(kernel, client)
            await api.kernel.jobs["reconcile_positions"]()
            await api.kernel.jobs["reconcile_orders"]()
            await settle(kernel)

            # ---------------------------------------------------------- 逐笔交易
            assert await api.ok_get("/trades") == {"items": [], "total": 0}
            btc = await api.round_trip("BTCUSDT", "800")
            await api.round_trip("ETHUSDT", "300")
            body = await api.ok_get("/trades")
            assert body["total"] == 2 and len(body["items"]) == 2
            newest, oldest = body["items"]
            assert newest["symbol"] == "ETHUSDT" and oldest["symbol"] == "BTCUSDT"  # 默认新的在前
            trade = oldest
            for key in ("id", "position_id", "symbol", "side", "ownership", "model", "is_mixed", "open_ms", "close_ms",
                        "open_time_estimated", "max_qty", "open_avg", "close_avg", "open_notional", "gross_pnl", "fees",
                        "funding", "net_pnl", "entry_reason", "exit_reason", "list_snapshot_id", "list_direction",
                        "list_score", "duration_ms", "return_pct"):
                assert key in trade, key
            assert trade["position_id"] == btc["id"] and trade["side"] == "LONG" and trade["ownership"] == "strategy"
            assert isinstance(trade["open_ms"], int) and isinstance(trade["close_ms"], int)
            assert trade["duration_ms"] == trade["close_ms"] - trade["open_ms"]
            assert isinstance(trade["net_pnl"], str) and isinstance(trade["max_qty"], str)
            assert D(trade["return_pct"]) == D(trade["net_pnl"]) / D(trade["open_notional"])

            assert (await api.ok_get("/trades", symbol="BTCUSDT"))["total"] == 1
            assert (await api.ok_get("/trades", ownership="takeover"))["total"] == 0
            assert (await api.ok_get("/trades", side="SHORT"))["total"] == 0
            assert (await api.ok_get("/trades", model="m1"))["total"] == 2
            page = await api.ok_get("/trades", order="asc", limit=1)
            assert page["total"] == 2 and [t["symbol"] for t in page["items"]] == ["BTCUSDT"]
            assert (await api.ok_get("/trades", since_ms=trade["close_ms"] + 10**9))["total"] == 0
            assert (await api.get("/trades", limit="0")).status_code == 422
            assert (await api.get("/trades", order="sideways")).status_code == 422
            assert (await api.get("/trades", side="UP")).status_code == 422
            assert (await client.get(BASE + "/trades")).status_code == 401

            # ---------------------------------------------------------- 统计
            stats = await api.ok_get("/stats", group_by="symbol")
            assert list(stats["overall"])[:3] == ["profit_loss_ratio", "profit_rate", "win_rate"]  # 顺序：盈亏比、盈利率、胜率
            assert stats["overall"]["count"] == 2
            nets = [D(t["net_pnl"]) for t in body["items"]]
            assert D(stats["overall"]["net_pnl"]) == sum(nets)
            assert {g["key"] for g in stats["groups"]} == {"BTCUSDT", "ETHUSDT"}
            assert stats["mixed"]["count"] == 0
            default = await api.ok_get("/stats")  # group_by 缺省按模型
            assert default["overall"]["count"] == 2
            assert (await api.get("/stats", group_by="colour")).status_code == 422
            assert (await api.get("/stats", since_ms="x")).status_code == 422

            # ---------------------------------------------------------- K 线
            period = 15 * 60_000
            start = (trade["open_ms"] // period - 30) * period
            bars = [Candle(open_time_ms=start + i * period, open=100 + i, high=110 + i, low=90 + i, close=105 + i,
                           volume=10 + i, close_time_ms=start + (i + 1) * period - 1) for i in range(40)]
            await kernel.ledger.write(repo_candles.upsert_bars, "15m", {"BTCUSDT": bars})
            candles = await api.ok_get("/candles", symbol="BTCUSDT", tf="15m", limit=40)
            assert len(candles["candles"]) == 40
            assert candles["candles"][0][0] == start and len(candles["candles"][0]) == 6
            assert len(candles["dmr"]) == 40
            assert all(v is None for _, v in candles["dmr"][:11])  # 短周期 n=12：前 11 个为空
            assert candles["dmr"][11][1] is not None and candles["dmr"][0][0] == start
            marks = candles["trades"]
            assert [m["position_id"] for m in marks] == [btc["id"]]
            assert set(marks[0]) == {"position_id", "side", "open_ms", "open_price", "close_ms", "close_price",
                                     "exit_reason"}
            assert marks[0]["open_ms"] == trade["open_ms"] and marks[0]["close_price"] == trade["close_avg"]
            assert candles["stops"], "平仓前保护单管理挂过止损，应有止损线"
            for stop in candles["stops"]:
                assert set(stop) == {"position_id", "price", "from_ms", "to_ms"} and stop["position_id"] == btc["id"]
            assert (await api.get("/candles", symbol="BTCUSDT", tf="4h")).status_code == 422
            assert (await api.get("/candles", tf="15m")).status_code == 422
            empty = await api.ok_get("/candles", symbol="DOGEUSDT", tf="1h")
            assert empty["candles"] == [] and empty["dmr"] == [] and empty["trades"] == []

            # ---------------------------------------------------------- 模型 3 状态
            state = await api.ok_get("/state")
            m3 = state["model3"]
            assert m3["form"] == "strategy_exec_ai_veto" and m3["ai_tier"] == "off" and m3["exec_tier"] == "off"
            assert m3["master_switch"] is False and m3["deploy_switch"] is False
            assert m3["startable"] is False and "预留" in m3["start_refusal"]

            # ---------------------------------------------------------- 影子记录：留痕，绝不下单
            before = len([r for r in standin.transport.requests if r[0] in WRITE_METHODS])
            shadow = await api.ok_post("/dev/shadow_intent", {"op_id": "shadow-1", "symbol": "BTCUSDT", "side": "LONG",
                                                              "reason": "AI 看多", "ai_eval_ref": "eval-9"})
            await settle(kernel)
            assert shadow["state"] == "REJECTED" and shadow["reason"] == "影子记录：只留痕不下单"
            assert shadow["idem_key"] == "shadow:shadow-1"
            again = await api.ok_post("/dev/shadow_intent", {"op_id": "shadow-1", "symbol": "BTCUSDT", "side": "LONG"})
            assert again["intent_id"] == shadow["intent_id"]
            after = len([r for r in standin.transport.requests if r[0] in WRITE_METHODS])
            assert after == before  # 替身上没有任何订单：连一个写请求都没发
            assert (await api.ok_get("/orders"))["system"] == []
            assert await kernel.orders.for_intent(shadow["intent_id"]) == []
            audit = await kernel.ledger.read(repo_audit.recent, limit=20, action="shadow.intent")
            assert len(audit) == 1
            oplog = (await api.ok_get("/oplog"))["items"]
            assert any(item["action"] == "shadow.intent" for item in oplog)
            assert (await api.get("/positions")).json()["items"] == []
            bad = await api.post("/dev/shadow_intent", {"op_id": "x y", "symbol": "BTCUSDT", "side": "LONG"})
            assert bad.status_code == 422
            bad = await api.post("/dev/shadow_intent", {"op_id": "shadow-2", "symbol": "NOPEUSDT", "side": "LONG"})
            assert bad.status_code == 422
