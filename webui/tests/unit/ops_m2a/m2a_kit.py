"""M2A（启停、部分平仓、平掉此仓、测试造仓）测试的公共设施：完整装配的内核 + 进程内替身 + 控制接口客户端。

- ``rig(tmp_path, ...)``：异步上下文，给出 ``Rig``（内核、替身、控制接口的 httpx 客户端）。进入时给内核接上操作服务所需的接线
  （``ctx.ops_wiring``，装配层以后自己接；已接就不动），并跑一轮持仓与挂单对账（合约数上限的快照需要）。
- ``Rig.login(name)``：建操作员并经控制接口登录，返回 ``Who``（令牌、会话号）；``Rig.seat(who)`` 申请席位。
- ``Rig.post(path, body, who=None)``：``who`` 缺省用本机令牌（cli，不占席位）。
- ``Rig.open_position(...)``：经协调者的测试专用入口开一个仓，补拉成交让登记簿登记，返回登记记录。
- ``make_deps(kernel, **overrides)``：从内核拼出 ``OpsDeps``（可换掉其中的部分，例如替换适配层的读方法）。
- ``StubAdapter``：包住真实适配层，可以让指定的读方法返回指定的结果或抛错。
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from dmr_kernel.core.intents import IntentSpec, key_op
from dmr_kernel.core.types import IntentAction, IntentSource, IntentState, PositionSide
from dmr_kernel.ops.common import OpsDeps, OpsWiring
from tests.support.inproc import AUTH_HEADER
from tests.support.kernel import api_client, settle, started_kernel

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
PASSWORD = "correct horse battery"
BASE = "/api/v1"


@dataclass(frozen=True)
class Who:
    name: str
    token: str
    session_id: int


class StubAdapter:
    """包住真实适配层：``set(name, value)`` 让这个读方法返回 value（可以是可等待的函数），其余照旧。"""

    def __init__(self, real: Any) -> None:
        self._real = real
        self._overrides: dict[str, Any] = {}
        self.calls: list[str] = []

    def set(self, name: str, value: Any) -> None:
        self._overrides[name] = value

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            if name in self._overrides:
                value = self._overrides[name]
                if callable(value):
                    result = value(*args, **kwargs)
                    return await result if hasattr(result, "__await__") else result
                return value
            return await attr(*args, **kwargs)

        return call


def make_deps(kernel, **overrides: Any) -> OpsDeps:
    wiring = OpsWiring.from_kernel(kernel)
    deps = OpsDeps(
        clock=kernel.clock, modelstate=kernel.modelstate, registry=kernel.registry, operations=kernel.operations,
        config_get=kernel.config.get, models=kernel.models, coordinator=wiring.coordinator, adapter=wiring.adapter,
        rules=wiring.rules, orders=wiring.orders, is_live=kernel.settings.is_live)
    return dataclasses.replace(deps, **overrides) if overrides else deps


class Rig:
    def __init__(self, kernel, standin, client) -> None:
        self.kernel, self.standin, self.client = kernel, standin, client
        self.clock = standin.clock
        self.cli_token = kernel.app.state.ctx.local_token
        self._seq = 0

    # ------------------------------------------------------------ 会话与席位

    async def login(self, name: str = "alice", password: str = PASSWORD) -> Who:
        auth = self.kernel.app.state.services.auth
        try:
            await auth.create_operator(name, password)
        except Exception:  # 已存在
            pass
        response = await self.client.post(f"{BASE}/auth/login", json={"username": name, "password": password},
                                          headers={})
        assert response.status_code == 200, response.text
        body = response.json()
        return Who(name, body["token"], body["session_id"])

    async def seat(self, who: Who) -> dict:
        response = await self.call("POST", "/seat/acquire", None, who)
        assert response.status_code == 200, response.text
        return response.json()

    async def call(self, method: str, path: str, body: Any, who: Who | None = None):
        token = self.cli_token if who is None else who.token
        return await self.client.request(method, BASE + path, json=body, headers={"Authorization": f"Bearer {token}"})

    async def post(self, path: str, body: Any = None, who: Who | None = None):
        return await self.call("POST", path, {} if body is None else body, who)

    async def get(self, path: str, who: Who | None = None):
        return await self.call("GET", path, None, who)

    def op_id(self, prefix: str = "op") -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    async def state(self) -> dict:
        response = await self.get("/state")
        assert response.status_code == 200, response.text
        return response.json()

    # ------------------------------------------------------------ 造仓与标定

    async def open_position(self, symbol: str = "BTCUSDT", side: PositionSide = LONG, notional: str = "100",
                            model: str = "m1"):
        """经协调者的测试专用入口开仓，补拉成交让登记簿登记；返回登记记录。"""
        op_id = self.op_id("seed")
        spec = IntentSpec(action=IntentAction.OPEN, model=model, source=IntentSource.TEST_ENTRY, symbol=symbol, side=side,
                          idem_key=key_op(op_id, symbol, side), params={"notional": notional}, reason="seed",
                          operator="test", op_id=op_id)
        submitted = await self.kernel.coordinator.submit(spec)
        record = await self.kernel.coordinator.wait_terminal(submitted.intent_id, 5)
        assert record.state is IntentState.COMPLETED, record.state_reason
        await self.kernel.jobs["backfill_trades"]()
        await settle(self.kernel)
        position = self.kernel.registry.get_active(symbol, side)
        assert position is not None
        return position

    async def exchange_qty(self, symbol: str = "BTCUSDT", side: PositionSide = LONG) -> Decimal:
        live = await self.kernel.coordinator.live_position(symbol, side)
        return live.value

    async def manual(self, path: str, body: dict, method: str = "POST") -> Any:
        """模拟用户在 Binance 网页上的操作（替身的开发控制，用 manual.token）。"""
        raw = json.dumps(body).encode("utf-8")
        status, payload, _ = await self.standin.disp.handle(method, path, {}, raw,
                                                            {AUTH_HEADER: self.standin.manual_token})
        assert status == 200, payload
        return payload

    async def manual_reduce(self, symbol: str, side: PositionSide, qty: str) -> None:
        """模拟用户在 Binance 上手动减仓。"""
        await self.manual("/dev/manual/order", {"symbol": symbol, "side": "SELL" if side is LONG else "BUY",
                                                "position_side": side.value, "qty": qty})


@asynccontextmanager
async def rig(tmp_path, *, wire: bool = True, reconcile: bool = True, **kernel_kwargs: Any):
    async with started_kernel(tmp_path, **kernel_kwargs) as (kernel, standin):
        ctx = kernel.app.state.ctx
        if wire and getattr(ctx, "ops_wiring", None) is None:
            ctx.ops_wiring = OpsWiring.from_kernel(kernel)
        if reconcile:
            await kernel.jobs["reconcile_positions"]()
            await kernel.jobs["reconcile_orders"]()
        async with api_client(kernel, ctx.local_token) as client:
            yield Rig(kernel, standin, client)


async def start_gate(kernel) -> None:
    from dmr_kernel.core.types import Gate

    await kernel.modelstate.set_gate(Gate.RUNNING, reason="test")


Check = Callable[[dict], bool]


class CoordinatorProxy:
    """包住真实协调者：指定的属性换成替身（值或函数），其余照旧；``calls`` 记下被换掉的方法的调用。"""

    def __init__(self, real: Any, **overrides: Any) -> None:
        self._real = real
        self._overrides = overrides
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            value = self._overrides[name]
            if not callable(value):
                return value

            def call(*args: Any, **kwargs: Any) -> Any:
                self.calls.append((name, args, kwargs))
                return value(*args, **kwargs)

            return call
        return getattr(self._real, name)
