"""模型 2 接管测试的公共设施：完整装配的内核（真实恢复，账户数据流在线）+ 进程内替身 + 控制接口客户端。

``m2_rig(tmp_path, ...)``：异步上下文，给出 ``Rig``。内核已切到模型 2；``start=True`` 时已启动模型 2（经 ``/ops/start``，走启动自检）。
配置缺省：接管清单 BTCUSDT，名义金额 5–500 USDT。手动开仓一律经替身的 ``/dev/manual/*``（manual 令牌），与用户在网页上下单等价。
时间用共享的 ``ManualClock``：``rig.advance(毫秒)``。
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from dmr_kernel.core.types import Model, PositionSide
from tests.support.inproc import AUTH_HEADER
from tests.support.kernel import api_client, make_standin_world, settle, started_kernel

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
BASE = "/api/v1"

DEFAULT_CONFIG: dict[str, Any] = {
    "takeover_symbols": ["BTCUSDT"], "takeover_min_notional": "5", "takeover_max_notional": "500",
}


class Rig:
    def __init__(self, kernel, standin, client) -> None:
        self.kernel, self.standin, self.client = kernel, standin, client
        self.clock = standin.clock
        self.cli_token = kernel.app.state.ctx.local_token
        self._seq = 0

    # ------------------------------------------------------------ 控制接口

    def op_id(self, prefix: str = "op") -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    async def post(self, path: str, body: Any = None):
        return await self.client.post(BASE + path, json={} if body is None else body,
                                      headers={"Authorization": f"Bearer {self.cli_token}"})

    async def get(self, path: str):
        return await self.client.get(BASE + path, headers={"Authorization": f"Bearer {self.cli_token}"})

    async def ok(self, path: str, body: Any = None) -> dict:
        response = await self.post(path, body)
        assert response.status_code == 200, (path, response.status_code, response.text)
        return response.json()

    async def state(self) -> dict:
        return (await self.get("/state")).json()

    async def start_model(self) -> dict:
        version = self.kernel.modelstate.current().version
        body = await self.ok("/ops/start", {"op_id": self.op_id("start"), "model_version": version})
        await self.settle()
        return body

    async def stop_model(self) -> dict:
        version = self.kernel.modelstate.current().version
        body = await self.ok("/ops/stop", {"op_id": self.op_id("stop"), "model_version": version})
        await self.settle()
        return body

    async def takeover_op(self, name: str, position_id: int, **extra: Any):
        rec = self.kernel.registry.by_id(position_id)
        body = {"op_id": self.op_id(name), "position_id": position_id, "position_version": rec.version, **extra}
        return await self.post(f"/ops/{name}", body)

    # ------------------------------------------------------------ 替身上的手动操作

    async def manual(self, path: str, body: dict, method: str = "POST") -> Any:
        raw = json.dumps(body).encode("utf-8")
        status, payload, _ = await self.standin.disp.handle(method, path, {}, raw,
                                                            {AUTH_HEADER: self.standin.manual_token})
        assert status == 200, payload
        return payload

    async def manual_open(self, symbol: str = "BTCUSDT", qty: str = "0.002", side: PositionSide = LONG,
                          settle_after: bool = True) -> None:
        await self.manual("/dev/manual/order", {"symbol": symbol, "side": "BUY" if side is LONG else "SELL",
                                                "position_side": side.value, "qty": qty})
        if settle_after:
            await self.settle()

    async def manual_reduce(self, symbol: str = "BTCUSDT", qty: str = "0.001", side: PositionSide = LONG) -> None:
        await self.manual("/dev/manual/order", {"symbol": symbol, "side": "SELL" if side is LONG else "BUY",
                                                "position_side": side.value, "qty": qty})
        await self.settle()

    async def manual_stop(self, symbol: str, trigger: str, side: PositionSide = LONG, *, close_position: bool = True,
                          qty: str | None = None, algo_type: str = "STOP_MARKET") -> dict:
        body: dict[str, Any] = {"symbol": symbol, "side": "SELL" if side is LONG else "BUY",
                                "position_side": side.value, "type": algo_type, "trigger_price": trigger,
                                "close_position": close_position}
        if qty is not None:
            body["qty"] = qty
        return await self.manual("/dev/manual/algo_order", body)

    def engine_order(self, symbol: str, side: str, position_side: str, qty: str, client_order_id: str) -> None:
        """直接在替身引擎上以指定的自定义编号下单（旧程序格式、ccxt 默认编号之类）。"""
        self.standin.engine.place_market_order(symbol=symbol, side=side, position_side=position_side, qty=qty,
                                               client_order_id=client_order_id, source="manual")
        self.standin.disp._publish()  # 引擎上直接下的单：把事件推给数据流订阅者

    # ------------------------------------------------------------ 观察

    async def settle(self, rounds: int = 4) -> None:
        await settle(self.kernel, rounds)

    def advance(self, ms: int) -> None:
        self.clock.advance(ms)

    def pos(self, symbol: str = "BTCUSDT", side: PositionSide = LONG):
        return self.kernel.registry.get_active(symbol, side)

    async def algos(self, symbol: str = "BTCUSDT") -> list:
        result = await self.kernel.adapter.fetch_open_algo_orders(symbol)
        return list(result.value)

    async def exchange_qty(self, symbol: str = "BTCUSDT", side: PositionSide = LONG) -> Decimal:
        return (await self.kernel.coordinator.live_position(symbol, side)).value


@asynccontextmanager
async def m2_rig(tmp_path, *, start: bool = True, config: Mapping[str, Any] | None = None, equity: str = "100000",
                 prepare=None, recovery_factory: Any = None):
    """``prepare(standin)`` 在内核启动之前改交易所（例如造一个启动时已有的仓）。"""
    standin = make_standin_world(equity=equity)
    if prepare is not None:
        prepare(standin)
    overrides = {**DEFAULT_CONFIG, **(config or {})}
    async with started_kernel(tmp_path, standin=standin, recovery_factory=recovery_factory,
                              config_overrides=overrides) as (kernel, standin):
        await kernel.modelstate.set_active_model(Model.M2, expected_version=kernel.modelstate.current().version,
                                                 operator="test", op_id="to-m2")
        await kernel.jobs["reconcile_positions"]()
        await kernel.jobs["reconcile_orders"]()
        async with api_client(kernel, kernel.app.state.ctx.local_token) as client:
            rig = Rig(kernel, standin, client)
            if start:
                await rig.start_model()
                rig.advance(1_000)
            yield rig
